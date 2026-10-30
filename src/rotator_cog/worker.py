"""The Lambda entrypoint: rotation jobs and sweeps from rotator-cog's queue.

rotator-cog runs on Lambda behind its own FIFO queue, declared in
mini-app-polis/infra. Two kinds of message arrive on it:

    {"type": "rotation.job",      "version": 1, "payload": <RotationRequest>}
    {"type": "rotation.sweep",    "version": 1, "payload": {"config": "prd"}}
    {"type": "rotation.incident", "version": 1, "payload": {"incident_id": ...}}

A job asks for one secret to be rotated in one Doppler config. It is checked
against the inventory packaged with the function (secrets/inventory.yaml):
the name must be listed, its kind must be one the worker rotates, the config
must be one the secret is held in, and it must need no manual step (one that
does is rotated with scripts/rotate.py). Everything else that holds the
secret, GitHub Actions included, reads a reference to it that Doppler's syncs
keep current, so a write to Doppler reaches it. A job that fails the check is reported and acknowledged:
it is the producer's mistake, and sending it again would fail the same way.

**Two phases for a routine rotation.** Callers on Lambda read Parameter
Store on every invocation, so they would present a new key within moments of
Doppler's sync, while the API learns it only when it is redeployed. So the
new value first goes to NAME_PREVIOUS and the API is redeployed, accepting
both keys; then NAME takes the new value and NAME_PREVIOUS the old one, the
callers switch, and the API is redeployed again. A compromised rotation is
one write: NAME takes the new value and NAME_PREVIOUS is emptied, and the
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

**Duplicates.** The queue is FIFO with content-based deduplication, so the
same job asked twice within five minutes is one message. Each rotation is
recorded keyed on name, config and minted, so recording it again updates
the one event. The sweep never rotates a key whose previous key is still
live, and an IAM user with two keys already is refused (docs/decisions,
ADR-0003).

After each Doppler write the services that read the secret at boot are
redeployed in the config's Railway environment, once Railway shows the new
value (railway.py), with a Railway project token per config from Parameter
Store (RAILWAY_TOKEN_PRD_PARAMETER, RAILWAY_TOKEN_DEV_PARAMETER). Phase 1
redeploys only the service that accepts the key; phase 2 and a compromised
rotation redeploy every service the entry lists.

**Ready before anything is written.** Before a value is minted, the config's
Railway environment is checked: every service the rotation will redeploy is
there, and each holds every variable it will be waited on for. A service
sees a shared variable only once it references it, which in development is
set by hand, so a rotation whose NAME_PREVIOUS no service references would
otherwise write to Doppler and then time out. A rotation that is not ready
is reported, with the reference to add, and nothing is written.

Then the new key is checked: ``GET /v1/identity/whoami`` with it, at the
config's API, must name the machine. The rotation is recorded with the API
(``POST /v1/secrets/rotation-events``) as ok or failed, and any failure after
the value is minted (a write, a redeploy, the check) is recorded as failed
and fails the job. The run report goes to the runs channel, and a failure's
to errors as well.

**A provider-api secret** is minted by its provider through the provider's
API: TURNSTILE_SECRET_KEY, through Cloudflare's rotate-secret (turnstile.py),
with a Cloudflare token of the rotator's own read from the Parameter Store
path CLOUDFLARE_TURNSTILE_TOKEN_PARAMETER names, for the widget
CLOUDFLARE_ACCOUNT_ID and TURNSTILE_SITEKEY name. Cloudflare keeps the old
secret valid for two hours after a routine rotation, so there is no handover:
one write, one redeploy, and the event records the old secret as retired
when the grace period ends. A compromised rotation invalidates it at once.
The check is siteverify, which must know the new secret.

A sweep retires previous keys, rotates the most overdue key and reports
drift, for one config (sweep.py).

An incident is expanded into a compromised job per secret the worker
rotates, sent to this cog's own queue (ROTATOR_JOBS_QUEUE_URL, set by
infra) in its one message group, and a board task per secret a person
rotates (incident.py).
"""

from __future__ import annotations

import importlib
import os
import secrets as pysecrets
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import yaml
from mini_app_polis import load_secrets, timing
from mini_app_polis import logger as logger_mod
from mini_app_polis.api import KaianoApiClient, KaianoApiError
from mini_app_polis.api.contract import (
    RotationEventCreate,
    RotationIncidentMessage,
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
from pydantic import BaseModel, Field, ValidationError

from . import aws_keys, entries, turnstile
from . import doppler as doppler_mod
from . import incident as incident_mod
from . import railway as railway_mod
from . import sweep as sweep_mod
from ._version import __version__

log = logger_mod.get_logger()

REPO = "rotator-cog"
FLOW_NAME = "rotate-secret"
SWEEP_FLOW_NAME = "rotation-sweep"
INCIDENT_FLOW_NAME = "rotation-incident"

#: The environment variable naming this cog's own jobs queue (infra's
#: own_queue_url_variable): an incident's jobs are sent there.
JOBS_QUEUE_URL_VAR = "ROTATOR_JOBS_QUEUE_URL"

#: The message group the API sends rotation jobs in. One group keeps the
#: queue serial, so an incident's jobs run in the order they were sent.
MESSAGE_GROUP = "rotator"

#: This cog's name to the API, which picks its key; also the actor recorded.
MACHINE_NAME = "rotator-cog"

#: The kinds the worker rotates. The others are rotated with scripts/rotate.py:
#: a generated secret's check runs under the doppler CLI, which the function
#: does not have.
WORKER_KINDS = frozenset({"machine-key", "aws-access-key", "provider-api"})

#: The providers whose API the worker mints a provider-api secret through.
PROVIDERS = frozenset({"cloudflare-turnstile"})

#: Where the Cloudflare token that rotates the Turnstile secret is, and the
#: widget it belongs to: environment variables infra sets. None is secret.
TURNSTILE_TOKEN_PARAMETER_VAR = "CLOUDFLARE_TURNSTILE_TOKEN_PARAMETER"
CLOUDFLARE_ACCOUNT_VAR = "CLOUDFLARE_ACCOUNT_ID"
TURNSTILE_SITEKEY_VAR = "TURNSTILE_SITEKEY"

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

#: The sweep rotates what is due only when this is "true". Unset, it still
#: retires previous keys and reports what is due and any drift, so the
#: sweep's own rotations can wait until a rotation has been proven by hand.
SWEEP_ROTATE_VAR = "ROTATOR_SWEEP_ROTATE"

#: The sweep asks for a task per due manual secret only when this is
#: "true". Unset, it reports which are due, so the first ask can be checked.
SWEEP_TASKS_VAR = "ROTATOR_SWEEP_TASKS"

#: The most manual-rotation tasks one sweep asks for, most overdue first.
#: Unset or not a whole number of at least 1, it is
#: ``sweep.TASK_LIMIT_DEFAULT``.
SWEEP_TASK_LIMIT_VAR = "ROTATOR_SWEEP_TASK_LIMIT"

#: The sweep report's sections: the old keys it retired, and the Doppler
#: changes it judged. Names only, never a value.
RETIRED = "Retired old keys (no longer in use)"
RETIRED_FORCED = "Retired old keys (still in use after the longest overlap)"
DOPPLER_CHANGES = "Doppler changes"

#: How long Railway may take to show a value Doppler has written, and a
#: redeploy to be running. Each phase waits for both, so the function's
#: timeout in mini-app-polis/infra covers two of each.
SYNC_TIMEOUT_SECONDS = 120.0
DEPLOY_TIMEOUT_SECONDS = 240.0

#: The whoami check of a new key: how long one request may take. Retries
#: are KaianoApiClient's: a refused connection, 429, 502, 503 and 504 are
#: asked again, any other answer is final.
VERIFY_TIMEOUT_SECONDS = 15.0
WHOAMI_PATH = "/v1/identity/whoami"

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
#: Asks a provider for a new secret: (invalidate the old one now) -> secret.
ProviderMinter = Callable[[bool], str]
#: Checks a provider-minted secret: None when it works, else why not.
SecretVerifier = Callable[[str], str | None]
#: Checks, before anything is written, that each service is in the config's
#: environment and holds each variable named for it (a reference counts, even
#: one Doppler has not filled): (config, {service: names}) -> what is missing,
#: one sentence each; empty when ready.
Checker = Callable[[str, Mapping[str, Collection[str]]], list[str]]


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
    """The job's inventory entry, its redeploy list resolved for the job's config.

    Raises InvalidJob when the worker cannot rotate it.
    """
    entry = (inventory.get("secrets") or {}).get(job.name)
    if entry is None:
        raise InvalidJob("not in the inventory")
    kind = entry.get("kind")
    if kind not in WORKER_KINDS:
        raise InvalidJob(f"kind {kind} is not rotated by the worker")
    if job.config not in entry.get("configs", ["prd"]):
        raise InvalidJob(f"not held in {job.config}")
    if entry.get("steps"):
        raise InvalidJob("needs a manual step: rotate it with rotate.py")
    resolved = entries.for_config(entry, job.config)
    if kind == "machine-key" and ACCEPTING_SERVICE not in resolved["redeploy"]:
        raise InvalidJob(f"{ACCEPTING_SERVICE} is not in its redeploy list")
    if kind == "aws-access-key" and not (entry.get("iam_user") and entry.get("id_var")):
        raise InvalidJob("an AWS key entry needs iam_user and id_var")
    if kind == "provider-api" and entry.get("provider") not in PROVIDERS:
        raise InvalidJob(
            f"provider {entry.get('provider')} is not one the worker mints through"
        )
    return resolved


#: Checks a new AWS key: (user, key id, secret) -> None, or why not.
AwsVerifier = Callable[[str, str, str], str | None]

#: Puts one rotation job on this cog's own queue; raises on failure.
JobSender = Callable[[RotationRequest], None]


def _iam() -> Any:
    return importlib.import_module("boto3").client("iam")


def queue_sender(*, client: Any | None = None) -> JobSender:
    """Send jobs to this cog's own queue, in its one message group.

    The queue deduplicates on content, so the same job sent twice within
    five minutes is one message.
    """
    url = os.environ.get(JOBS_QUEUE_URL_VAR, "").strip()
    if not url:
        raise RuntimeError(f"{JOBS_QUEUE_URL_VAR} is not set: infra sets it")
    sqs = client or importlib.import_module("boto3").client("sqs")

    def send(job: RotationRequest) -> None:
        sqs.send_message(
            QueueUrl=url,
            MessageBody=RotationJobMessage(payload=job).model_dump_json(),
            MessageGroupId=MESSAGE_GROUP,
        )

    return send


def worker_rotates(inventory: Mapping[str, Any]) -> Callable[[str, str], bool]:
    """Whether the worker would accept a job for (name, config)."""

    def rotates(name: str, config: str) -> bool:
        try:
            validate(sweep_mod.request_for(name, config), inventory)
        except InvalidJob:
            return False
        return True

    return rotates


def machine_name(var: str) -> str:
    """The machine a key belongs to: DEEJAY_COG_API_KEY -> deejay-cog."""
    return var.removesuffix("_API_KEY").lower().replace("_", "-")


class _Verified(BaseModel):
    """Who the presented credential proved the caller to be."""

    subject: str = Field(..., description="The machine or person's subject.")


class _WhoAmI(BaseModel):
    """GET /v1/identity/whoami: the part the check reads."""

    verified: _Verified = Field(..., description="The verified caller.")


def machine_verifier(inventory: Mapping[str, Any]) -> Verifier:
    """Check a machine key against the config's API: whoami must name it.

    The request presents the key being checked, never this cog's own.
    """
    bases = {"prd": inventory["api_base_url"], "dev": inventory["api_base_url_dev"]}

    def verify(
        name: str, entry: Mapping[str, Any], config: str, value: str
    ) -> str | None:
        machine = machine_name(name)
        if not value:
            # KaianoApiClient falls back to the environment's key without one.
            return "no key to check"
        # Named for the machine whose key is under check; the explicit
        # api_key is what is presented, so machine_name resolves nothing.
        client = KaianoApiClient(
            base_url=bases[config],
            api_key=value,
            machine_name=machine,
            timeout=VERIFY_TIMEOUT_SECONDS,
            user_agent=USER_AGENT,
        )
        try:
            body = client.get(WHOAMI_PATH)
        except KaianoApiError as exc:
            if exc.status_code:
                return f"whoami answered HTTP {exc.status_code}"
            cause = exc.__cause__
            return f"whoami could not be asked: {type(cause or exc).__name__}"
        except ValueError:
            return "whoami answered without a subject"
        try:
            subject = _WhoAmI.model_validate(body).verified.subject
        except ValidationError:
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
    if isinstance(
        exc,
        doppler_mod.DopplerError
        | railway_mod.RailwayError
        | turnstile.TurnstileError
        | TokenUnavailable,
    ):
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
    # A token piped in from a CLI arrives with its trailing newline, which
    # no header may carry.
    value = str((response.get("Parameter") or {}).get("Value") or "").strip()
    if not value:
        raise TokenUnavailable(f"{name} has no value")
    return value


def cloudflare_turnstile(
    *,
    token_reader: Callable[[str], str] = _secure_parameter,
    transport: httpx.BaseTransport | None = None,
) -> ProviderMinter:
    """Rotate the Turnstile widget's secret, with the rotator's Cloudflare token.

    The token and the widget are read when a rotation runs, so a sweep with
    nothing to rotate never reads the token.
    """

    def mint_secret(invalidate_now: bool) -> str:
        account = os.environ.get(CLOUDFLARE_ACCOUNT_VAR, "").strip()
        sitekey = os.environ.get(TURNSTILE_SITEKEY_VAR, "").strip()
        if not account or not sitekey:
            raise turnstile.TurnstileError(
                f"{CLOUDFLARE_ACCOUNT_VAR} and {TURNSTILE_SITEKEY_VAR} must be set"
            )
        return turnstile.rotate_secret(
            account,
            sitekey,
            token_reader(TURNSTILE_TOKEN_PARAMETER_VAR),
            invalidate_immediately=invalidate_now,
            transport=transport,
        )

    return mint_secret


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


def railway_checker(
    *,
    token_reader: Callable[[str], str] = railway_token,
    railway_factory: Callable[[str], railway_mod.Railway] = railway_mod.Railway,
) -> Checker:
    """Check the config's Railway environment is ready, with its project token."""

    def check(config: str, needs: Mapping[str, Collection[str]]) -> list[str]:
        railway = railway_factory(token_reader(config))
        environment = railway.environment_name()
        if environment != RAILWAY_ENVIRONMENTS[config]:
            return [
                f"the {config} Railway token reaches {environment}, "
                f"not {RAILWAY_ENVIRONMENTS[config]}"
            ]
        ids = railway.service_ids()
        gaps: list[str] = []
        for service, names in needs.items():
            if service not in ids:
                gaps.append(f"{service} is not in Railway {environment}")
                continue
            held = railway.variable_names(ids[service])
            gaps.extend(
                f"{service} in {environment} does not hold {name}: "
                f"reference it in the service's {config} Doppler config"
                for name in sorted(set(names) - held)
            )
        return gaps

    return check


def _needs(job: RotationRequest, entry: Mapping[str, Any]) -> dict[str, list[str]]:
    """What each service must hold before the job's rotation can start.

    Every service must hold NAME. NAME_PREVIOUS is not checked: an empty
    slot, which is what it holds between handovers (ADR-0005), is no
    variable at all on Railway, so its absence proves nothing. A routine
    handover proves the slot reaches the accepting service when phase 1
    waits for it. If the service's config does not reference it, that wait
    times out with nothing written but an unused key in the slot, which the
    sweep empties. An AWS key's services take both of its parts.
    """
    name = job.name
    if entry["kind"] == "aws-access-key":
        return {service: [entry["id_var"], name] for service in entry["redeploy"]}
    return {service: [name] for service in entry["redeploy"]}


def _not_ready(
    check: Checker,
    config: str,
    needs: Mapping[str, Collection[str]],
    *,
    report: Any,
    label: str,
) -> bool:
    """Report what keeps the services from a rotation. True when anything does."""
    try:
        gaps = check(config, needs)
    except railway_mod.RailwayError as exc:
        gaps = [str(exc)[:500]]
    for gap in gaps:
        report.issue("not ready to rotate; nothing was written", label, detail=gap)
    return bool(gaps)


class _IncidentReport:
    """A run report whose every line names the incident first."""

    def __init__(self, report: Any, incident_id: str) -> None:
        self._report = report
        self._prefix = f"incident {incident_id}: "

    def __getattr__(self, name: str) -> Any:
        return getattr(self._report, name)

    def note(self, reason: str, item: str | None = None) -> None:
        self._report.note(self._prefix + reason, item)

    def notice(self, reason: str, item: str | None = None) -> None:
        self._report.notice(self._prefix + reason, item)

    def issue(
        self, reason: str, item: str | None = None, *, detail: str | None = None
    ) -> None:
        self._report.issue(self._prefix + reason, item, detail=detail)

    def created(self, kind: str, item: str | None = None, **kw: Any) -> None:
        self._report.created(self._prefix + kind, item, **kw)

    def updated(self, kind: str, item: str | None = None, **kw: Any) -> None:
        self._report.updated(self._prefix + kind, item, **kw)

    def removed(self, kind: str, item: str | None = None, **kw: Any) -> None:
        self._report.removed(self._prefix + kind, item, **kw)


def rotate(
    job: RotationRequest,
    *,
    run_id: str,
    inventory_loader: Callable[[], Mapping[str, Any]] = load_inventory,
    token_reader: Callable[[str], str] = doppler_token,
    rotator: Callable[..., None] | None = None,
    incident_api_factory: Callable[[str], incident_mod.IncidentApi] | None = None,
) -> None:
    """Check one job and rotate its secret. Never redelivered.

    A job that carries an incident id is rotated compromised whatever its
    mode says, and its event records the incident. When it fails, however
    it fails, the failure is recorded and reported, a person gets a task
    to rotate the secret (incident_task_for, keyed on the incident, so a
    repeat makes no second task) and the entry is marked with the API.
    The job is then acknowledged rather than dead-lettered: the jobs queued
    behind it in the incident still run.
    """
    incident_id = job.incident_id
    if incident_id is not None and job.mode != "compromised":
        job = job.model_copy(update={"mode": "compromised"})
    label = f"{job.name} in {job.config}"
    rotator = rotator or _rotate
    with run_report(FLOW_NAME, repo=REPO, run_id=run_id, version=__version__) as raw:
        report: Any = raw
        if incident_id is not None:
            raw.count("incident", incident_id)
            report = _IncidentReport(raw, incident_id)
        inventory = inventory_loader()
        try:
            try:
                entry = validate(job, inventory)
            except InvalidJob as exc:
                report.issue("rotation job rejected", label, detail=str(exc))
                return
            token = token_reader(job.config)
            rotator(
                job,
                entry,
                token,
                inventory=inventory,
                report=report,
                redeploy=railway_redeployer(),
                verify=machine_verifier(inventory),
                record=api_recorder(),
                iam_factory=_iam,
                aws_verify=aws_keys.sts_verifier(),
                check=railway_checker(),
                provider_mint=cloudflare_turnstile(),
                provider_verify=turnstile.siteverify_verifier(),
            )
        except Exception as exc:
            if incident_id is None:
                raise
            # The failure was recorded as an event where a value was minted;
            # here it is reported, and handed to a person below.
            report.issue("rotation failed", label, detail=_reason(exc))
        finally:
            if incident_id is not None and raw.issues:
                _hand_to_person(
                    job,
                    inventory,
                    incident_id,
                    report,
                    incident_api_factory or incident_mod.api,
                )


def _hand_to_person(
    job: RotationRequest,
    inventory: Mapping[str, Any],
    incident_id: str,
    report: Any,
    api_factory: Callable[[str], incident_mod.IncidentApi],
) -> None:
    """Ask for a person's task for an incident job that failed, and mark it.

    Never raises: the job is done either way, and a task that could not be
    asked for is reported.
    """
    label = f"{job.name} in {job.config}"
    entry = (inventory.get("secrets") or {}).get(job.name) or {}
    task = sweep_mod.incident_task_for(
        job.name,
        entry,
        job.config,
        incident_id,
        why=(
            "rotator-cog's compromised rotation of it failed (see the run "
            "report), so a person rotates it."
        ),
    )
    try:
        api = api_factory(MACHINE_NAME)
        api.request_task(task)
        api.mark_task_requested(incident_id, job.name, job.config)
    except Exception as exc:  # noqa: BLE001 — reported; the job is done either way
        report.issue(
            "task not asked for",
            label,
            detail=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
        return
    report.created("task opened", label)


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
    iam_factory: Callable[[], Any] | None = None,
    aws_verify: AwsVerifier | None = None,
    check: Checker | None = None,
    provider_mint: ProviderMinter | None = None,
    provider_verify: SecretVerifier | None = None,
) -> None:
    """Mint the new value, hand it over in Doppler, redeploy, verify and record.

    Nothing is written unless redeploy, verify and record are all there: a
    handover stopped between its phases would leave a live key in
    NAME_PREVIOUS that nothing retires. Nor is anything written when
    ``check`` finds the services not ready for it.
    """
    label = f"{job.name} in {job.config}"
    if entry["kind"] == "aws-access-key":
        if (
            redeploy is None
            or record is None
            or iam_factory is None
            or aws_verify is None
        ):
            report.issue("rotation not built yet", label)
            return
        _rotate_aws(
            job,
            entry,
            doppler_factory(inventory["doppler_project"], job.config, token),
            iam_factory(),
            report=report,
            redeploy=redeploy,
            verify=aws_verify,
            record=record,
            now=now,
            check=check,
        )
        return
    if entry["kind"] == "provider-api":
        if (
            redeploy is None
            or record is None
            or provider_mint is None
            or provider_verify is None
        ):
            report.issue("rotation not built yet", label)
            return
        _rotate_provider(
            job,
            entry,
            doppler_factory(inventory["doppler_project"], job.config, token),
            report=report,
            redeploy=redeploy,
            mint_secret=provider_mint,
            verify=provider_verify,
            record=record,
            now=now,
            check=check,
        )
        return
    if redeploy is None or verify is None or record is None:
        report.issue("rotation not built yet", label)
        return
    store = doppler_factory(inventory["doppler_project"], job.config, token)
    name, previous = job.name, doppler_mod.previous_name(job.name)
    callers = [s for s in entry["redeploy"] if s != ACCEPTING_SERVICE]
    if check is not None and _not_ready(
        check, job.config, _needs(job, entry), report=report, label=label
    ):
        return
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
            incident_id=job.incident_id,
        )

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


def _rotate_aws(
    job: RotationRequest,
    entry: Mapping[str, Any],
    store: doppler_mod.Doppler,
    iam: Any,
    *,
    report: Any,
    redeploy: Redeployer,
    verify: AwsVerifier,
    record: Recorder,
    now: Callable[[], datetime],
    check: Checker | None = None,
) -> None:
    """Rotate one IAM user's access key.

    The new key is created beside the old one and both of its parts go to
    Doppler in one write; the entry's services are redeployed once Railway
    shows them. A routine rotation deletes the old key only after the new
    one has verified, and leaves it active if it did not. A compromised one
    deactivates and deletes the old key as soon as the new one is stored.
    """
    label = f"{job.name} in {job.config}"
    user, id_var = entry["iam_user"], entry["id_var"]
    if check is not None and _not_ready(
        check, job.config, _needs(job, entry), report=report, label=label
    ):
        return
    old_keys = aws_keys.existing(iam, user)
    if len(old_keys) >= 2:
        report.issue(
            "the IAM user already has two access keys; a person must delete one",
            f"{label} ({user})",
            detail=aws_keys.describe(old_keys),
        )
        return
    minted = now()
    retired: datetime | None = None
    key_id, secret = aws_keys.create(iam, user)

    def event(reason: str | None) -> RotationEventCreate:
        return RotationEventCreate(
            name=job.name,
            config=job.config,
            kind="aws-access-key",
            mode=job.mode,
            actor=MACHINE_NAME,
            minted=minted,
            retired=retired,
            verification="ok" if reason is None else "failed",
            verification_reason=reason,
            aws_key_last4=aws_keys.last4(key_id),
            incident_id=job.incident_id,
        )

    try:
        try:
            doppler_mod.write(store, job.name, {id_var: key_id, job.name: secret})
        except Exception:
            # Nothing holds the new key yet: take it back, so the user is not
            # left at its two-key limit by a write that never landed.
            aws_keys.delete(iam, user, key_id)
            raise
        if job.mode == "compromised":
            for old in old_keys:
                aws_keys.deactivate(iam, user, old["AccessKeyId"])
                aws_keys.delete(iam, user, old["AccessKeyId"])
            retired = now()
        redeploy(
            job.config,
            {
                service: {id_var: key_id, job.name: secret}
                for service in entry["redeploy"]
            },
        )
        reason = verify(user, key_id, secret)
        if reason is None and job.mode == "routine":
            for old in old_keys:
                aws_keys.delete(iam, user, old["AccessKeyId"])
            retired = now()
    except Exception as exc:
        record(event(_reason(exc)))
        raise
    finally:
        del secret
    record(event(reason))
    if reason is None:
        report.updated("rotated", f"{label} (key …{aws_keys.last4(key_id)})")
    else:
        report.issue(
            "verification failed; the old key is left active", label, detail=reason
        )


def _rotate_provider(
    job: RotationRequest,
    entry: Mapping[str, Any],
    store: doppler_mod.Doppler,
    *,
    report: Any,
    redeploy: Redeployer,
    mint_secret: ProviderMinter,
    verify: SecretVerifier,
    record: Recorder,
    now: Callable[[], datetime],
    check: Checker | None = None,
) -> None:
    """Rotate a secret its provider mints: one write, one redeploy, the check.

    The provider keeps the old secret valid for its grace period after a
    routine rotation, so the services move over in one redeploy, and the
    event records the old secret as retired when that period ends. A
    compromised rotation invalidates it as the new one is minted. A new
    secret that is not stored is lost: the provider never shows it again, so
    the failure says when the old one stops working.
    """
    label = f"{job.name} in {job.config}"
    if check is not None and _not_ready(
        check, job.config, _needs(job, entry), report=report, label=label
    ):
        return
    if job.name not in store.names():
        report.issue("not in Doppler", label)
        return
    compromised = job.mode == "compromised"
    minted = now()
    retired: datetime | None = None

    def event(reason: str | None) -> RotationEventCreate:
        return RotationEventCreate(
            name=job.name,
            config=job.config,
            kind="provider-api",
            mode=job.mode,
            actor=MACHINE_NAME,
            minted=minted,
            retired=retired,
            verification="ok" if reason is None else "failed",
            verification_reason=reason,
            incident_id=job.incident_id,
        )

    try:
        new = mint_secret(compromised)
    except Exception as exc:
        # Whether the provider minted one is unknown: the old secret may
        # stop working when its grace period ends.
        record(event(_reason(exc)))
        report.issue(
            "rotate-secret failed; if the provider did rotate it, the old "
            f"secret stops working by {minted + turnstile.GRACE:%H:%M} UTC",
            label,
            detail=_reason(exc),
        )
        raise
    retired = now() if compromised else minted + turnstile.GRACE
    try:
        try:
            doppler_mod.write(store, job.name, {job.name: new})
        except Exception:
            report.issue(
                "the new secret was not stored and is lost; rotate again before "
                f"the old one stops working at {retired:%H:%M} UTC",
                label,
            )
            raise
        redeploy(
            job.config, {service: {job.name: new} for service in entry["redeploy"]}
        )
        reason = verify(new)
    except Exception as exc:
        record(event(_reason(exc)))
        raise
    finally:
        del new
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
    checker_factory: Callable[[], Checker] = railway_checker,
    verifier_factory: Callable[[Mapping[str, Any]], Verifier] = machine_verifier,
    recorder_factory: Callable[[], Recorder] = api_recorder,
    now: Callable[[], datetime] = _now,
    iam_factory: Callable[[], Any] = _iam,
    aws_verify: AwsVerifier | None = None,
    provider_mint_factory: Callable[[], ProviderMinter] = cloudflare_turnstile,
    provider_verifier_factory: Callable[[], SecretVerifier] = (
        turnstile.siteverify_verifier
    ),
) -> None:
    """Sweep one config: retire, rotate the most overdue key, ask for manual
    rotations, report drift and changes no rotation explains."""
    with run_report(
        SWEEP_FLOW_NAME, repo=REPO, run_id=run_id, version=__version__
    ) as report:
        inventory = inventory_loader()
        api = api_factory(MACHINE_NAME)
        token = token_reader(config)
        store = doppler_factory(inventory["doppler_project"], config, token)
        redeploy, record = redeploy_factory(), recorder_factory()
        aws_verify = aws_verify or aws_keys.sts_verifier()
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
                report.removed(
                    RETIRED_FORCED if r.forced else RETIRED,
                    f"{r.event.name} in {config}",
                )
                if r.forced:
                    report.issue(
                        "previous key retired while still in use",
                        r.machine,
                        detail=f"last used {r.last_used:%Y-%m-%d %H:%M} UTC"
                        if r.last_used
                        else None,
                    )

        # 2. Rotate the most overdue key the services are ready for, on a day
        #    that retired nothing. A key they are not ready for is reported
        #    and passed over, so it cannot hold up the rest.
        eligible = []
        for name in inventory["secrets"]:
            try:
                validate(sweep_mod.request_for(name, config), inventory)
            except InvalidJob:
                continue
            eligible.append(name)
        cadences = {
            n: int(inventory["cadence_days"][inventory["secrets"][n]["kind"]])
            for n in eligible
        }
        overdue, failed = sweep_mod.due(
            eligible, latest, cadences, actor=MACHINE_NAME, now=at
        )
        for name in failed:
            report.issue("last rotation failed; needs a person", f"{name} in {config}")
        report.count("due", len(overdue))
        rotating = os.environ.get(SWEEP_ROTATE_VAR, "").strip().lower() == "true"
        if overdue and not rotating:
            report.note(f"due, not rotated: {SWEEP_ROTATE_VAR} is off", overdue[0])
        if overdue and not ready and rotating:
            check = checker_factory()
            for name in overdue:
                job = sweep_mod.request_for(name, config)
                entry = validate(job, inventory)
                if _not_ready(
                    check,
                    config,
                    _needs(job, entry),
                    report=report,
                    label=f"{name} in {config}",
                ):
                    continue
                _rotate(
                    job,
                    entry,
                    token,
                    inventory=inventory,
                    report=report,
                    redeploy=redeploy,
                    verify=verifier_factory(inventory),
                    record=record,
                    doppler_factory=doppler_factory,
                    now=now,
                    iam_factory=iam_factory,
                    aws_verify=aws_verify,
                    provider_mint=provider_mint_factory(),
                    provider_verify=provider_verifier_factory(),
                )
                break

        # 3. A task for each of the most overdue manual secrets, up to the
        # limit: a person rotates it.
        manual = sweep_mod.most_overdue_first(
            sweep_mod.manual_due(inventory, latest, config, now=at)
        )
        report.count("manual_due", len(manual))
        asking = os.environ.get(SWEEP_TASKS_VAR, "").strip().lower() == "true"
        limit = sweep_mod.task_limit(os.environ.get(SWEEP_TASK_LIMIT_VAR))
        if asking and len(manual) > limit:
            # One line naming them all: past the limit, the report keeps only
            # the first few items of a reason.
            held = ", ".join(due.name for due in manual[limit:])
            report.notice(
                f"manual secrets due, task not yet asked (a sweep asks for {limit})",
                f"{held} in {config}",
            )
            manual = manual[:limit]
        for due in manual:
            label = f"{due.name} in {config}"
            if not asking:
                report.note(
                    f"manual secret due, no task asked: {SWEEP_TASKS_VAR} is off", label
                )
                continue
            entry = inventory["secrets"][due.name]
            try:
                api.request_task(sweep_mod.task_for(due, entry, config))
            except Exception as exc:  # noqa: BLE001 — one refusal must not stop the rest
                report.issue(
                    "rotation task not asked for", label, detail=str(exc)[:500]
                )
                continue
            report.note("rotation task asked for", label)

        # 4. Drift between Doppler and the inventory.
        unknown, missing = sweep_mod.drift(store.names(), inventory, config)
        for name in unknown:
            report.issue("in Doppler, not in the inventory", f"{name} in {config}")
        for name in missing:
            report.issue("in the inventory, not in Doppler", f"{name} in {config}")

        # 5. Changes Doppler reported that no rotation explains.
        since, until = sweep_mod.change_window(at)
        try:
            changes = api.changes(config, since=since, until=until)
            events = api.events(config)
        except Exception as exc:  # noqa: BLE001 — the rest of the sweep still counts
            report.issue("secret changes not read", config, detail=str(exc)[:500])
        else:
            odd = sweep_mod.unexplained(changes, events, inventory)
            report.count(
                "changes",
                len(changes),
                label=(
                    "{} in the last day"
                    if odd
                    else "{} in the last day, all explained by rotations"
                ),
                section=DOPPLER_CHANGES,
            )
            report.count(
                "changes_unexplained",
                len(odd),
                label="{} not explained by a rotation (named above)",
                section=DOPPLER_CHANGES,
            )
            for change in odd:
                label = f"{change.name} in {config}"
                report.issue("changed outside a rotation", label)
                if not asking:
                    report.note(
                        f"no task asked for the change: {SWEEP_TASKS_VAR} is off",
                        label,
                    )
                    continue
                try:
                    api.request_task(sweep_mod.change_task_for(change, config))
                except Exception as exc:  # noqa: BLE001 — one refusal must not stop the rest
                    report.issue(
                        "change task not asked for", label, detail=str(exc)[:500]
                    )


def expand_incident(
    incident_id: str,
    *,
    run_id: str,
    inventory_loader: Callable[[], Mapping[str, Any]] = load_inventory,
    api_factory: Callable[[str], incident_mod.IncidentApi] = incident_mod.api,
    sender_factory: Callable[[], JobSender] = queue_sender,
) -> None:
    """Expand one incident into jobs and tasks (incident.py).

    The run report names the incident first and each secret with what this
    run did for it. A failure for one entry is reported and the walk goes
    on; only a failure to register the list stops it, since nothing can be
    tracked without it.
    """
    heading = f"incident {incident_id}"
    with run_report(
        INCIDENT_FLOW_NAME, repo=REPO, run_id=run_id, version=__version__
    ) as report:
        report.count("incident", incident_id)
        inventory = inventory_loader()
        listed = incident_mod.entries_for(inventory, worker_rotates(inventory))
        api = api_factory(MACHINE_NAME)
        registered = api.register(incident_id, incident_mod.registration(listed))
        known: dict[tuple[str, str], Any] = {
            (e.name, e.config): e for e in registered.entries
        }
        send: JobSender | None = None
        counts = {"queued": 0, "tasks": 0, "skipped": 0, "failed": 0}
        for entry in listed:
            label = f"{entry.name} in {entry.config}"
            state = known.get((entry.name, entry.config))
            if state is not None and state.state == "rotated":
                report.note(f"{heading}: already rotated", label)
                counts["skipped"] += 1
                continue
            if state is not None and state.task_requested:
                report.note(f"{heading}: task already opened", label)
                counts["skipped"] += 1
                continue
            log.info(
                "worker: %s: %s (%s, tier %s)",
                heading,
                label,
                entry.handling,
                entry.tier,
            )
            if entry.handling == "worker":
                try:
                    send = send or sender_factory()
                    send(incident_mod.job_for(entry, incident_id))
                except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
                    report.issue(
                        f"{heading}: rotation not queued",
                        label,
                        detail=f"{type(exc).__name__}: {str(exc)[:300]}",
                    )
                    counts["failed"] += 1
                    continue
                report.created(f"{heading}: rotation queued", label)
                counts["queued"] += 1
                continue
            secret = inventory["secrets"][entry.name]
            task = sweep_mod.incident_task_for(
                entry.name,
                secret,
                entry.config,
                incident_id,
                why=incident_mod.why_task(secret),
            )
            try:
                api.request_task(task)
                api.mark_task_requested(incident_id, entry.name, entry.config)
            except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
                report.issue(
                    f"{heading}: task not asked for",
                    label,
                    detail=f"{type(exc).__name__}: {str(exc)[:300]}",
                )
                counts["failed"] += 1
                continue
            report.created(f"{heading}: task opened", label)
            counts["tasks"] += 1
        for key, value in counts.items():
            report.count(key, value)


def process_message(body: str, *, run_id: str, **kwargs: Any) -> None:
    """Handle one message."""
    message = parse_message(
        body, RotationJobMessage, RotationSweepMessage, RotationIncidentMessage
    )
    if isinstance(message, RotationIncidentMessage):
        log.info(
            "worker: run %s expands incident %s", run_id, message.payload.incident_id
        )
        expand_incident(message.payload.incident_id, run_id=run_id)
        return
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
