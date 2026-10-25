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

After each Doppler write the services that read the secret at boot are
redeployed in the config's Railway environment, once Railway shows the new
value (railway.py), with a Railway project token per config from Parameter
Store (RAILWAY_TOKEN_PRD_PARAMETER, RAILWAY_TOKEN_DEV_PARAMETER). Phase 1
redeploys only the service that accepts the key; phase 2 and a compromised
rotation redeploy every service the entry lists.

Then the new key is checked: ``GET /v1/identity/whoami`` with it, at the
config's API, must name the machine. The rotation is recorded with the API
(``POST /v1/secrets/rotation-events``) as ok or failed, and any failure after
the value is minted (a write, a redeploy, the check) is recorded as failed
and fails the job. The run report goes to the runs channel, and a failure's
to errors as well.

A sweep retires previous keys, rotates the most overdue key and reports
drift, for one config (sweep.py).
"""

from __future__ import annotations

import importlib
import os
import secrets as pysecrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import yaml
from mini_app_polis import load_secrets, timing
from mini_app_polis import logger as logger_mod
from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.contract import (
    RotationEventCreate,
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
from . import railway as railway_mod
from . import sweep as sweep_mod
from ._version import __version__

log = logger_mod.get_logger()

REPO = "rotator-cog"
FLOW_NAME = "rotate-secret"
SWEEP_FLOW_NAME = "rotation-sweep"

#: This cog's name to the API, which picks its key; also the actor recorded.
MACHINE_NAME = "rotator-cog"

#: The kinds the worker rotates. The others are rotated with scripts/rotate.py:
#: a generated secret's check runs under the doppler CLI, which the function
#: does not have.
WORKER_KINDS = frozenset({"machine-key"})

#: Cloudflare blocks default library user agents.
USER_AGENT = "mini-app-polis-rotator/1"

#: Where each config's Doppler token is: the environment variable naming
#: its Parameter Store path.
TOKEN_PARAMETER_VARS = {
    "prd": "DOPPLER_TOKEN_PRD_PARAMETER",
    "dev": "DOPPLER_TOKEN_DEV_PARAMETER",
}
#: The same for each config's Railway project token.
RAILWAY_TOKEN_PARAMETER_VARS = {
    "prd": "RAILWAY_TOKEN_PRD_PARAMETER",
    "dev": "RAILWAY_TOKEN_DEV_PARAMETER",
}
#: The Railway environment each Doppler config syncs to.
RAILWAY_ENVIRONMENTS = {"prd": "production", "dev": "development"}

#: The service that accepts a machine key, and so must know a new one
#: before its callers present it.
ACCEPTING_SERVICE = "api-kaianolevine-com"

#: How long Railway may take to show a value Doppler has written, and a
#: redeploy to be running. Each phase waits for both, so the function's
#: timeout in mini-app-polis/infra covers two of each.
SYNC_TIMEOUT_SECONDS = 120.0
DEPLOY_TIMEOUT_SECONDS = 240.0

#: How long before the function's timeout a run is stopped, leaving room for
#: the run report and the handler's return.
DEADLINE_MARGIN_SECONDS = 20

#: The inventory in a checkout, for tests and local runs; the wheel carries
#: its own copy inside the package.
_REPO_INVENTORY = Path(__file__).resolve().parents[2] / "secrets" / "inventory.yaml"

init_sentry(REPO)


#: Redeploys services in the config's environment once each holds the values
#: given for it (None: not set); raises on failure.
#: (config, {service: {variable: value or None}}) -> None
Redeployer = Callable[[str, Mapping[str, Mapping[str, str | None]]], None]
#: Checks the new value: (name, entry, config, value) -> None when it works,
#: else why not, in a sentence that never quotes the value.
Verifier = Callable[[str, Mapping[str, Any], str, str], str | None]
#: Records one rotation event with the API.
Recorder = Callable[[RotationEventCreate], None]


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
    if ACCEPTING_SERVICE not in entry.get("redeploy", []):
        raise InvalidJob(f"{ACCEPTING_SERVICE} is not in its redeploy list")
    return dict(entry)


def machine_name(var: str) -> str:
    """The machine a key belongs to: DEEJAY_COG_API_KEY -> deejay-cog."""
    return var.removesuffix("_API_KEY").lower().replace("_", "-")


def machine_verifier(
    inventory: Mapping[str, Any], *, transport: httpx.BaseTransport | None = None
) -> Verifier:
    """Check a machine key against the config's API: whoami must name it."""
    bases = {"prd": inventory["api_base_url"], "dev": inventory["api_base_url_dev"]}

    def verify(
        name: str, entry: Mapping[str, Any], config: str, value: str
    ) -> str | None:
        machine = machine_name(name)
        with httpx.Client(timeout=15.0, transport=transport) as client:
            try:
                response = client.get(
                    f"{bases[config]}/v1/identity/whoami",
                    headers={
                        "Authorization": f"Bearer {value}",
                        "User-Agent": USER_AGENT,
                    },
                )
            except httpx.HTTPError as exc:
                return f"whoami could not be asked: {type(exc).__name__}"
        if response.status_code != 200:
            return f"whoami answered HTTP {response.status_code}"
        try:
            subject = response.json()["verified"]["subject"]
        except (ValueError, KeyError, TypeError):
            return "whoami answered without a subject"
        if subject != machine:
            return f"whoami named another machine, not {machine}"
        return None

    return verify


def api_recorder() -> Recorder:
    """Record through the API with this cog's own key."""

    def record(event: RotationEventCreate) -> None:
        KaianoApiClient.from_env(MACHINE_NAME).record_rotation_event(event)

    return record


def _now() -> datetime:
    return datetime.now(UTC)


def _reason(exc: BaseException) -> str:
    """Why a rotation failed, from an error that names no value."""
    if isinstance(exc, doppler_mod.DopplerError | railway_mod.RailwayError):
        return str(exc)[:500]
    return f"{type(exc).__name__} during the rotation"


def mint(inventory: Mapping[str, Any], config: str) -> str:
    """A new value, as rotate.py mints one: the config's key prefix, then random.

    No dots in either part: a dotless credential routes as a machine key.
    """
    prefix = (inventory.get("key_prefix") or {}).get(config, "")
    return f"{prefix}{pysecrets.token_urlsafe(32)}"


def doppler_token(config: str, *, client: Any | None = None) -> str:
    """The Doppler token for ``config``, from Parameter Store."""
    return _secure_parameter(TOKEN_PARAMETER_VARS[config], client=client)


def railway_token(config: str, *, client: Any | None = None) -> str:
    """The Railway project token for ``config``, from Parameter Store."""
    return _secure_parameter(RAILWAY_TOKEN_PARAMETER_VARS[config], client=client)


def _secure_parameter(var: str, *, client: Any | None = None) -> str:
    """The SecureString at the path ``var`` names.

    boto3 ships in the Lambda runtime and is imported only here; tests pass a
    fake ``client``. No error raised here carries the value or the AWS
    error's text.
    """
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


def railway_redeployer(
    *,
    token_reader: Callable[[str], str] = railway_token,
    railway_factory: Callable[[str], railway_mod.Railway] = railway_mod.Railway,
) -> Redeployer:
    """Redeploy through Railway, with the config's project token."""

    def redeploy(config: str, targets: Mapping[str, Mapping[str, str | None]]) -> None:
        railway = railway_factory(token_reader(config))
        environment = railway.environment_name()
        if environment != RAILWAY_ENVIRONMENTS[config]:
            raise railway_mod.RailwayError(
                f"the {config} Railway token reaches {environment}, "
                f"not {RAILWAY_ENVIRONMENTS[config]}"
            )
        ids = railway.service_ids()
        unknown = sorted(set(targets) - set(ids))
        if unknown:
            raise railway_mod.RailwayError(
                f"not in Railway {environment}: {', '.join(unknown)}"
            )
        for service, values in targets.items():
            railway.wait_for_variables(
                ids[service],
                {
                    name: None if value is None else railway_mod.digest(value)
                    for name, value in values.items()
                },
                timeout=SYNC_TIMEOUT_SECONDS,
            )
        deployments = [railway.redeploy(ids[service]) for service in targets]
        railway.wait_for_deployments(deployments, timeout=DEPLOY_TIMEOUT_SECONDS)

    return redeploy


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
        _rotate(
            job,
            entry,
            token,
            inventory=inventory,
            report=report,
            redeploy=railway_redeployer(),
            verify=machine_verifier(inventory),
            record=api_recorder(),
        )


def _rotate(
    job: RotationRequest,
    entry: Mapping[str, Any],
    token: str,
    *,
    inventory: Mapping[str, Any],
    report: Any,
    redeploy: Redeployer | None = None,
    verify: Verifier | None = None,
    record: Recorder | None = None,
    doppler_factory: Callable[[str, str, str], doppler_mod.Doppler] = (
        doppler_mod.Doppler
    ),
    now: Callable[[], datetime] = _now,
) -> None:
    """Mint the new value, hand it over in Doppler, redeploy, verify and record.

    Nothing is written unless redeploy, verify and record are all there: a
    handover stopped between its phases would leave a live key in
    NAME_PREVIOUS that nothing retires.
    """
    label = f"{job.name} in {job.config}"
    if redeploy is None or verify is None or record is None:
        report.issue("rotation not built yet", label)
        return
    store = doppler_factory(inventory["doppler_project"], job.config, token)
    old = store.get(job.name)
    if old is None:
        report.issue("not in Doppler", label)
        return
    minted = now()
    retired: datetime | None = None
    new = mint(inventory, job.config)

    def event(reason: str | None) -> RotationEventCreate:
        return RotationEventCreate(
            name=job.name,
            config=job.config,
            kind=entry["kind"],
            mode=job.mode,
            actor=MACHINE_NAME,
            minted=minted,
            retired=retired,
            verification="ok" if reason is None else "failed",
            verification_reason=reason,
        )

    name, previous = job.name, doppler_mod.previous_name(job.name)
    callers = [s for s in entry["redeploy"] if s != ACCEPTING_SERVICE]
    try:
        if job.mode == "compromised":
            doppler_mod.replace(store, name, new)
            retired = now()
            accepted: dict[str, str | None] = {name: new, previous: None}
        else:
            doppler_mod.stage(store, name, new)
            # Phase 1: the accepting service learns the new key; callers keep NAME.
            redeploy(job.config, {ACCEPTING_SERVICE: {name: old, previous: new}})
            doppler_mod.promote(store, name, new, old)
            accepted = {name: new, previous: old}
        # Phase 2 (or the compromised write): every service takes the new key.
        redeploy(
            job.config,
            {ACCEPTING_SERVICE: accepted, **{s: {name: new} for s in callers}},
        )
        del accepted
        reason = verify(job.name, entry, job.config, new)
    except Exception as exc:
        record(event(_reason(exc)))
        raise
    finally:
        del old, new
    record(event(reason))
    if reason is None:
        report.updated("rotated", label)
    else:
        report.issue("verification failed", label, detail=reason)


def run_sweep(
    config: str,
    *,
    run_id: str,
    inventory_loader: Callable[[], Mapping[str, Any]] = load_inventory,
    token_reader: Callable[[str], str] = doppler_token,
    api_factory: Callable[[str], sweep_mod.SweepApi] = sweep_mod.api,
    doppler_factory: Callable[[str, str, str], doppler_mod.Doppler] = (
        doppler_mod.Doppler
    ),
    redeploy_factory: Callable[[], Redeployer] = railway_redeployer,
    verifier_factory: Callable[[Mapping[str, Any]], Verifier] = machine_verifier,
    recorder_factory: Callable[[], Recorder] = api_recorder,
    now: Callable[[], datetime] = _now,
) -> None:
    """Sweep one config: retire, rotate the most overdue key, report drift."""
    with run_report(
        SWEEP_FLOW_NAME, repo=REPO, run_id=run_id, version=__version__
    ) as report:
        inventory = inventory_loader()
        api = api_factory(MACHINE_NAME)
        token = token_reader(config)
        store = doppler_factory(inventory["doppler_project"], config, token)
        redeploy, record = redeploy_factory(), recorder_factory()
        latest = {e.name: e for e in api.latest(config)}
        at = now()

        # 1. Retire previous keys that nothing uses any more.
        ready = sweep_mod.retirements(
            latest.values(),
            config,
            actor=MACHINE_NAME,
            machine_of=machine_name,
            last_use=api.last_previous_use,
            now=at,
        )
        if ready:
            names = [r.event.name for r in ready]
            doppler_mod.retire(store, names)
            redeploy(
                config,
                {
                    ACCEPTING_SERVICE: {
                        doppler_mod.previous_name(n): None for n in names
                    }
                },
            )
            retired_at = now()
            for r in ready:
                record(sweep_mod.retired_event(r.event, retired_at))
                report.removed("previous key", f"{r.event.name} in {config}")
                if r.forced:
                    report.issue(
                        "previous key retired while still in use",
                        r.machine,
                        detail=f"last used {r.last_used:%Y-%m-%d %H:%M} UTC"
                        if r.last_used
                        else None,
                    )

        # 2. Rotate the most overdue key, on a day that retired nothing.
        eligible = []
        for name in inventory["secrets"]:
            try:
                validate(sweep_mod.request_for(name, config), inventory)
            except InvalidJob:
                continue
            eligible.append(name)
        cadence = int(inventory["cadence_days"]["machine-key"])
        overdue, failed = sweep_mod.due(
            eligible, latest, cadence, actor=MACHINE_NAME, now=at
        )
        for name in failed:
            report.issue("last rotation failed; needs a person", f"{name} in {config}")
        report.count("due", len(overdue))
        if overdue and not ready:
            job = sweep_mod.request_for(overdue[0], config)
            _rotate(
                job,
                validate(job, inventory),
                token,
                inventory=inventory,
                report=report,
                redeploy=redeploy,
                verify=verifier_factory(inventory),
                record=record,
                doppler_factory=doppler_factory,
                now=now,
            )

        # 3. Drift between Doppler and the inventory.
        unknown, missing = sweep_mod.drift(store.names(), inventory, config)
        for name in unknown:
            report.issue("in Doppler, not in the inventory", f"{name} in {config}")
        for name in missing:
            report.issue("in the inventory, not in Doppler", f"{name} in {config}")


def process_message(body: str, *, run_id: str, **kwargs: Any) -> None:
    """Handle one message."""
    message = parse_message(body, RotationJobMessage, RotationSweepMessage)
    if isinstance(message, RotationSweepMessage):
        log.info("worker: run %s sweeps %s", run_id, message.payload.config)
        run_sweep(message.payload.config, run_id=run_id)
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
