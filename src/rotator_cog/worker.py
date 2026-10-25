"""The Lambda entrypoint: rotation jobs and sweeps from rotator-cog's queue.

rotator-cog runs on Lambda behind its own FIFO queue, declared in
mini-app-polis/infra. Two kinds of message arrive on it:

    {"type": "rotation.job",   "version": 1, "payload": <RotationRequest>}
    {"type": "rotation.sweep", "version": 1, "payload": {"config": "prd"}}

A job asks for one secret to be rotated in one Doppler config. It is checked
against the inventory packaged with the function (secrets/inventory.yaml):
the name must be listed, its kind must be one the worker rotates, the config
must be one the secret is held in, and the secret must live in Doppler alone
(an entry also held in GitHub, or with a manual step, is rotated with
scripts/rotate.py). A job that fails the check is reported and acknowledged:
it is the producer's mistake, and sending it again would fail the same way.

**Two phases for a routine rotation.** Callers on Lambda read Parameter
Store on every invocation, so they would present a new key within moments of
Doppler's sync, while the API learns it only when it is redeployed. So the
new value first goes to NAME_PREVIOUS and the API is redeployed, accepting
both keys; then NAME takes the new value and NAME_PREVIOUS the old one, the
callers switch, and the API is redeployed again. A compromised rotation is
one write: NAME takes the new value and NAME_PREVIOUS is deleted, and the
401s until the redeploy are the price.

The worker writes new values with a Doppler token of its own, one per
config, read from the Parameter Store path its environment names
(DOPPLER_TOKEN_PRD_PARAMETER, DOPPLER_TOKEN_DEV_PARAMETER). The tokens are
never in Doppler or the environment, and never logged.

**One delivery.** A rotation that fails partway can leave a secret half
rotated, which needs a person, so the queue delivers each message once
(``max_receive_count = 1`` in mini-app-polis/infra). A job that crashes is
named in ``batchItemFailures`` and lands in the dead-letter queue, whose
alarm says so.

Redeploying and verifying are not built yet, so a valid job is reported as
not rotated and nothing is written; a sweep is acknowledged and logged.
"""

from __future__ import annotations

import importlib
import os
import secrets as pysecrets
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from mini_app_polis import load_secrets, timing
from mini_app_polis import logger as logger_mod
from mini_app_polis.api.contract import (
    RotationJobMessage,
    RotationRequest,
    RotationSweepMessage,
)
from mini_app_polis.lambda_worker import (
    UnprocessableMessage,
    all_failed,
    deadline,
    parse_message,
    process_records,
)
from mini_app_polis.pipeline_status import post_run_finding, run_report
from mini_app_polis.sentry import init_sentry

from . import doppler as doppler_mod
from ._version import __version__

log = logger_mod.get_logger()

REPO = "rotator-cog"
FLOW_NAME = "rotate-secret"

#: The kinds the worker rotates. The others are rotated with scripts/rotate.py.
WORKER_KINDS = frozenset({"machine-key", "generated"})

#: Where each config's Doppler token is: the environment variable naming
#: its Parameter Store path.
TOKEN_PARAMETER_VARS = {
    "prd": "DOPPLER_TOKEN_PRD_PARAMETER",
    "dev": "DOPPLER_TOKEN_DEV_PARAMETER",
}

#: How long before the function's timeout a run is stopped, leaving room for
#: the run report and the handler's return.
DEADLINE_MARGIN_SECONDS = 20

#: The inventory in a checkout, for tests and local runs; the wheel carries
#: its own copy inside the package.
_REPO_INVENTORY = Path(__file__).resolve().parents[2] / "secrets" / "inventory.yaml"

init_sentry(REPO)


#: Redeploys the entry's services in the config's environment; raises on failure.
Redeployer = Callable[[Mapping[str, Any], str], None]
#: Whether the new value works: (name, entry, config, value) -> ok.
Verifier = Callable[[str, Mapping[str, Any], str, str], bool]


class InvalidJob(Exception):
    """The job asks for something the worker does not rotate."""


class TokenUnavailable(RuntimeError):
    """A Doppler token could not be read. Names the parameter, never a value."""


def load_inventory() -> dict[str, Any]:
    """The inventory packaged with the function, or the checkout's copy."""
    packaged = resources.files("rotator_cog").joinpath("inventory.yaml")
    text = packaged.read_text() if packaged.is_file() else _REPO_INVENTORY.read_text()
    inventory: dict[str, Any] = yaml.safe_load(text)
    return inventory


def validate(job: RotationRequest, inventory: Mapping[str, Any]) -> dict[str, Any]:
    """The job's inventory entry. Raises InvalidJob when the worker cannot rotate it."""
    entry = (inventory.get("secrets") or {}).get(job.name)
    if entry is None:
        raise InvalidJob("not in the inventory")
    kind = entry.get("kind")
    if kind not in WORKER_KINDS:
        raise InvalidJob(f"kind {kind} is not rotated by the worker")
    if job.config not in entry.get("configs", ["prd"]):
        raise InvalidJob(f"not held in {job.config}")
    if entry.get("github"):
        raise InvalidJob("also held in GitHub: rotate it with rotate.py")
    if entry.get("steps"):
        raise InvalidJob("needs a manual step: rotate it with rotate.py")
    return dict(entry)


def mint(inventory: Mapping[str, Any], config: str) -> str:
    """A new value, as rotate.py mints one: the config's key prefix, then random.

    No dots in either part: a dotless credential routes as a machine key.
    """
    prefix = (inventory.get("key_prefix") or {}).get(config, "")
    return f"{prefix}{pysecrets.token_urlsafe(32)}"


def doppler_token(config: str, *, client: Any | None = None) -> str:
    """The Doppler token for ``config``, from Parameter Store.

    boto3 ships in the Lambda runtime and is imported only here; tests pass a
    fake ``client``. No error raised here carries the value or the AWS
    error's text.
    """
    var = TOKEN_PARAMETER_VARS[config]
    name = os.environ.get(var)
    if not name:
        raise TokenUnavailable(f"{var} is not set")
    if client is None:
        client = importlib.import_module("boto3").client("ssm")
    try:
        response = client.get_parameter(Name=name, WithDecryption=True)
    except Exception as exc:  # noqa: BLE001 — named by its type alone
        raise TokenUnavailable(f"could not read {name}: {type(exc).__name__}") from None
    value = (response.get("Parameter") or {}).get("Value")
    if not value:
        raise TokenUnavailable(f"{name} has no value")
    return str(value)


def rotate(
    job: RotationRequest,
    *,
    run_id: str,
    inventory_loader: Callable[[], Mapping[str, Any]] = load_inventory,
    token_reader: Callable[[str], str] = doppler_token,
) -> None:
    """Check one job and rotate its secret. Never redelivered."""
    label = f"{job.name} in {job.config}"
    with run_report(FLOW_NAME, repo=REPO, run_id=run_id, version=__version__) as report:
        inventory = inventory_loader()
        try:
            entry = validate(job, inventory)
        except InvalidJob as exc:
            report.issue("rotation job rejected", label, detail=str(exc))
            return
        token = token_reader(job.config)
        _rotate(job, entry, token, inventory=inventory, report=report)


def _rotate(
    job: RotationRequest,
    entry: Mapping[str, Any],
    token: str,
    *,
    inventory: Mapping[str, Any],
    report: Any,
    redeploy: Redeployer | None = None,
    verify: Verifier | None = None,
    doppler_factory: Callable[[str, str, str], doppler_mod.Doppler] = (
        doppler_mod.Doppler
    ),
) -> None:
    """Mint the new value, hand it over in Doppler, redeploy and verify.

    Nothing is written unless both redeploy and verify are there: a
    handover stopped between its phases would leave a live key in
    NAME_PREVIOUS that nothing retires.
    """
    label = f"{job.name} in {job.config}"
    if redeploy is None or verify is None:
        report.issue("rotation not built yet", label)
        return
    store = doppler_factory(inventory["doppler_project"], job.config, token)
    old = store.get(job.name)
    if old is None:
        report.issue("not in Doppler", label)
        return
    new = mint(inventory, job.config)
    if job.mode == "compromised":
        doppler_mod.replace(store, job.name, new)
    else:
        doppler_mod.stage(store, job.name, new)
        redeploy(entry, job.config)
        doppler_mod.promote(store, job.name, new, old)
    del old
    redeploy(entry, job.config)
    ok = verify(job.name, entry, job.config, new)
    del new
    if ok:
        report.updated("rotated", label)
    else:
        report.issue("verification failed", label)


def process_message(body: str, *, run_id: str, **kwargs: Any) -> None:
    """Handle one message."""
    message = parse_message(body, RotationJobMessage, RotationSweepMessage)
    if isinstance(message, RotationSweepMessage):
        log.warning(
            "worker: run %s: the sweep of %s is not built yet; acknowledged",
            run_id,
            message.payload.config,
        )
        return
    job = message.payload
    log.info("worker: run %s rotates %s in %s", run_id, job.name, job.config)
    rotate(job, run_id=run_id, **kwargs)


def _report_unprocessable(exc: BaseException, run_id: str) -> None:
    try:
        post_run_finding(
            FLOW_NAME,
            "ERROR",
            f"an unprocessable message failed: {type(exc).__name__}: {exc}",
            repo=REPO,
            source="queue_consumer",
            run_id=run_id,
            version=__version__,
        )
    except Exception:  # noqa: BLE001 — the notification is not the job
        log.exception("worker: could not report the unprocessable message")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Run each record. Never raises.

    A record named in ``batchItemFailures`` goes to the dead-letter queue,
    since the queue allows one receive.
    """
    try:
        load_secrets(refresh=True)
    except Exception:  # noqa: BLE001 — nothing can run without its settings
        log.exception("worker: could not refresh settings from SSM")
        return all_failed(event)

    def handle(record: dict[str, Any]) -> None:
        message_id = str(record.get("messageId") or "")
        attempt = (record.get("attributes") or {}).get("ApproximateReceiveCount", "?")
        with timing.invocation(
            cog="rotator", source="rotation", attempt=attempt
        ) as timed:
            try:
                with deadline(context, margin_seconds=DEADLINE_MARGIN_SECONDS):
                    process_message(record.get("body") or "", run_id=message_id)
            except UnprocessableMessage:
                timed.label(outcome="unprocessable")
                raise
            except BaseException:
                timed.label(outcome="failed")
                raise
            timed.label(outcome="ok")

    def on_unprocessable(exc: UnprocessableMessage, record: dict[str, Any]) -> None:
        _report_unprocessable(exc, str(record.get("messageId") or ""))

    return process_records(event, handle, on_unprocessable=on_unprocessable)
