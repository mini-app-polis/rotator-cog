"""The Lambda worker: messages, job checks and the Doppler tokens. No AWS, no API."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest
from mini_app_polis.api.contract import RotationRequest
from test_doppler import FakeDoppler

from rotator_cog import doppler, worker

#: A stand-in token; tests check it never reaches an error or a report.
TOKEN = "dp.st.token-for-tests"

INVENTORY = {
    "secrets": {
        "DEEJAY_COG_API_KEY": {
            "kind": "machine-key",
            "configs": ["prd", "dev"],
            "redeploy": ["api-kaianolevine-com", "db-poller"],
        },
        "TICK_SECRET": {"kind": "generated", "redeploy": ["api-kaianolevine-com"]},
        "LONE_API_KEY": {"kind": "machine-key", "redeploy": ["db-poller"]},
        "OPENAI_API_KEY": {"kind": "manual"},
        "CI_VALIDATOR_API_KEY": {
            "kind": "machine-key",
            "configs": ["prd", "dev"],
            "github": ["org:mini-app-polis"],
        },
        "MANUAL_STEP_API_KEY": {"kind": "machine-key", "steps": "paste it"},
    },
    "doppler_project": "proj",
    "key_prefix": {"dev": "dev_"},
    "api_base_url": "https://api.test.invalid",
    "api_base_url_dev": "https://dev-api.test.invalid",
}


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Run reports and findings, kept in the test and out of the API."""
    sent: list[tuple] = []

    def fake_post_run_finding(*args: Any, **kwargs: Any) -> None:
        sent.append((args, kwargs))

    monkeypatch.setattr(
        "mini_app_polis.pipeline_status.post_run_finding", fake_post_run_finding
    )
    monkeypatch.setattr(worker, "post_run_finding", fake_post_run_finding)
    return sent


def _job(name: str = "DEEJAY_COG_API_KEY", config: str = "prd") -> RotationRequest:
    return RotationRequest(name=name, config=config, mode="routine")  # type: ignore[arg-type]


def _body(message_type: str = "rotation.job", **payload: Any) -> str:
    if not payload:
        payload = {"name": "DEEJAY_COG_API_KEY", "config": "prd", "mode": "routine"}
    return json.dumps({"type": message_type, "version": 1, "payload": payload})


class FakeSsm:
    def __init__(self, value: str | None = TOKEN, error: Exception | None = None):
        self.value = value
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get_parameter(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Parameter": {"Value": self.value}}


# -- the inventory ---------------------------------------------------------


def test_the_inventory_loads_and_holds_every_kind_the_worker_rotates() -> None:
    inventory = worker.load_inventory()
    kinds = {e["kind"] for e in inventory["secrets"].values()}
    assert worker.WORKER_KINDS <= kinds
    assert inventory["api_base_url_dev"].startswith("https://")


def test_every_machine_key_the_worker_takes_names_its_machine_and_the_api() -> None:
    inventory = worker.load_inventory()
    taken = [
        n
        for n, e in inventory["secrets"].items()
        if e["kind"] == "machine-key" and not e.get("github") and not e.get("steps")
    ]
    assert taken
    for n in taken:
        assert n.endswith("_API_KEY")
        assert worker.ACCEPTING_SERVICE in inventory["secrets"][n]["redeploy"]


def test_the_wheel_carries_the_inventory_inside_the_package() -> None:
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    )
    included = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert included["secrets/inventory.yaml"] == "rotator_cog/inventory.yaml"


# -- checking a job --------------------------------------------------------


def test_a_job_for_a_listed_worker_kind_in_a_config_it_is_held_in_passes() -> None:
    assert worker.validate(_job(config="dev"), INVENTORY)["kind"] == "machine-key"
    assert worker.validate(_job(config="prd"), INVENTORY)["kind"] == "machine-key"


@pytest.mark.parametrize(
    ("job", "reason"),
    [
        (_job("NOT_LISTED"), "not in the inventory"),
        (_job("OPENAI_API_KEY"), "kind manual is not rotated by the worker"),
        (_job("TICK_SECRET"), "kind generated is not rotated by the worker"),
        # No configs means prd only.
        (_job("LONE_API_KEY", "dev"), "not held in dev"),
        (_job("CI_VALIDATOR_API_KEY"), "also held in GitHub"),
        (_job("MANUAL_STEP_API_KEY"), "needs a manual step"),
        (_job("LONE_API_KEY"), "api-kaianolevine-com is not in its redeploy list"),
    ],
)
def test_a_job_the_worker_cannot_rotate_is_refused(
    job: RotationRequest, reason: str
) -> None:
    with pytest.raises(worker.InvalidJob, match=reason):
        worker.validate(job, INVENTORY)


# -- the Doppler tokens ----------------------------------------------------


def test_the_token_is_read_from_the_configs_parameter_decrypted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOPPLER_TOKEN_DEV_PARAMETER", "/rotator/DOPPLER_TOKEN_DEV")
    ssm = FakeSsm()
    assert worker.doppler_token("dev", client=ssm) == TOKEN
    assert ssm.calls == [{"Name": "/rotator/DOPPLER_TOKEN_DEV", "WithDecryption": True}]


def test_no_parameter_named_means_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOPPLER_TOKEN_PRD_PARAMETER", raising=False)
    with pytest.raises(worker.TokenUnavailable, match="DOPPLER_TOKEN_PRD_PARAMETER"):
        worker.doppler_token("prd", client=FakeSsm())


def test_a_failed_read_names_the_parameter_and_not_the_aws_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOPPLER_TOKEN_PRD_PARAMETER", "/rotator/DOPPLER_TOKEN_PRD")
    with pytest.raises(worker.TokenUnavailable) as caught:
        worker.doppler_token("prd", client=FakeSsm(error=RuntimeError(TOKEN)))
    assert "/rotator/DOPPLER_TOKEN_PRD" in str(caught.value)
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_an_empty_parameter_is_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOPPLER_TOKEN_PRD_PARAMETER", "/rotator/DOPPLER_TOKEN_PRD")
    with pytest.raises(worker.TokenUnavailable, match="has no value"):
        worker.doppler_token("prd", client=FakeSsm(value=None))


# -- one job ---------------------------------------------------------------


def _rotate(job: RotationRequest, read: list[str]) -> None:
    def token_reader(config: str) -> str:
        read.append(config)
        return TOKEN

    worker.rotate(
        job, run_id="m-1", inventory_loader=lambda: INVENTORY, token_reader=token_reader
    )


def test_a_rejected_job_is_reported_and_reads_no_token(posted: list[tuple]) -> None:
    read: list[str] = []
    _rotate(_job("OPENAI_API_KEY"), read)
    assert read == []
    ((args, kwargs),) = posted
    assert str(args[1]).endswith("WARN")
    assert "rotation job rejected" in kwargs["text"]
    assert "OPENAI_API_KEY in prd" in kwargs["text"]


def test_a_valid_job_reads_its_configs_token_and_runs_every_step(
    monkeypatch: pytest.MonkeyPatch, posted: list[tuple]
) -> None:
    seen: dict[str, Any] = {}

    def fake_rotate(job: Any, entry: Any, token: str, **kwargs: Any) -> None:
        seen.update(token=token, **kwargs)

    monkeypatch.setattr(worker, "_rotate", fake_rotate)
    read: list[str] = []
    _rotate(_job(config="dev"), read)
    assert read == ["dev"] and seen["token"] == TOKEN
    for step in ("redeploy", "verify", "record"):
        assert callable(seen[step])


def test_a_token_that_cannot_be_read_fails_the_job(posted: list[tuple]) -> None:
    def token_reader(config: str) -> str:
        raise worker.TokenUnavailable("DOPPLER_TOKEN_PRD_PARAMETER is not set")

    with pytest.raises(worker.TokenUnavailable):
        worker.rotate(
            _job(),
            run_id="m-1",
            inventory_loader=lambda: INVENTORY,
            token_reader=token_reader,
        )
    assert len(posted) == 1


# -- minting and the handover ---------------------------------------------


def test_minting_follows_the_configs_prefix() -> None:
    assert worker.mint(INVENTORY, "dev").startswith("dev_")
    prd = worker.mint(INVENTORY, "prd")
    assert not prd.startswith("dev_") and "." not in prd and len(prd) == 43


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def issue(self, reason: str, item: str | None = None, **kw: Any) -> None:
        self.lines.append(("issue", reason))

    def updated(self, kind: str, item: str) -> None:
        self.lines.append(("updated", kind))


def _handover(
    fake: FakeDoppler,
    mode: str = "routine",
    *,
    config: str = "dev",
    ok: bool = True,
    redeploy_fails: bool = False,
) -> tuple[Report, list[Any]]:
    """Run a job through _rotate, recording each redeploy against Doppler's state."""
    steps: list[Any] = []
    global _LAST_STEPS
    _LAST_STEPS = steps
    report = Report()

    def redeploy(cfg: str, targets: Any) -> None:
        steps.append(("redeploy", cfg, dict(fake.secrets), targets))
        if redeploy_fails:
            raise RuntimeError("redeploy failed")

    def verify(name: str, entry: Any, cfg: str, value: str) -> str | None:
        steps.append(("verify", value == fake.secrets[name]))
        return None if ok else "whoami answered HTTP 401"

    def record(event: Any) -> None:
        steps.append(("record", event))

    job = RotationRequest(name="DEEJAY_COG_API_KEY", config=config, mode=mode)  # type: ignore[arg-type]
    worker._rotate(
        job,
        INVENTORY["secrets"]["DEEJAY_COG_API_KEY"],
        TOKEN,
        inventory=INVENTORY,
        report=report,
        redeploy=redeploy,
        verify=verify,
        record=record,
        doppler_factory=lambda project, cfg, token: doppler.Doppler(
            project, cfg, token, transport=httpx.MockTransport(fake.handle)
        ),
        now=lambda: NOW,
    )
    return report, steps


NOW = dt.datetime(2026, 10, 25, 14, 0, tzinfo=dt.UTC)


KEY, PREV = "DEEJAY_COG_API_KEY", "DEEJAY_COG_API_KEY_PREVIOUS"
_LAST_STEPS: list[Any] = []


def test_a_routine_rotation_hands_over_in_two_phases() -> None:
    fake = FakeDoppler({KEY: "old"})
    report, steps = _handover(fake)
    new = fake.secrets[KEY]
    assert new.startswith("dev_") and new != "old"
    # Phase 1: only the API is redeployed, once it holds both keys; callers
    # still read the old one.
    assert steps[0] == (
        "redeploy",
        "dev",
        {KEY: "old", PREV: new},
        {"api-kaianolevine-com": {KEY: "old", PREV: new}},
    )
    # Phase 2: callers switch, and the API still accepts the old key.
    assert steps[1] == (
        "redeploy",
        "dev",
        {KEY: new, PREV: "old"},
        {"api-kaianolevine-com": {KEY: new, PREV: "old"}, "db-poller": {KEY: new}},
    )
    assert steps[2] == ("verify", True)
    event = steps[3][1]
    assert (event.name, event.config, event.kind, event.mode) == (
        KEY,
        "dev",
        "machine-key",
        "routine",
    )
    assert (event.actor, event.minted, event.retired) == ("rotator-cog", NOW, None)
    assert (event.verification, event.verification_reason) == ("ok", None)
    assert report.lines == [("updated", "rotated")]


def test_a_compromised_rotation_is_one_write_with_no_previous_slot() -> None:
    fake = FakeDoppler({KEY: "old", PREV: "older"})
    report, steps = _handover(fake, "compromised", config="prd")
    new = fake.secrets[KEY]
    assert fake.secrets == {KEY: new} and not new.startswith("dev_")
    assert len(fake.writes()) == 1
    assert steps[0] == (
        "redeploy",
        "prd",
        {KEY: new},
        {"api-kaianolevine-com": {KEY: new, PREV: None}, "db-poller": {KEY: new}},
    )
    assert steps[1] == ("verify", True)
    event = steps[2][1]
    assert event.mode == "compromised" and event.retired == NOW
    assert event.verification == "ok"
    assert report.lines == [("updated", "rotated")]


def test_a_failed_verification_is_recorded_failed_and_reported() -> None:
    report, steps = _handover(FakeDoppler({KEY: "old"}), ok=False)
    event = steps[-1][1]
    assert event.verification == "failed"
    assert event.verification_reason == "whoami answered HTTP 401"
    assert report.lines == [("issue", "verification failed")]


def test_a_failed_redeploy_is_recorded_failed_before_callers_switch() -> None:
    fake = FakeDoppler({KEY: "old"})
    with pytest.raises(RuntimeError, match="redeploy failed"):
        _handover(fake, redeploy_fails=True)
    assert fake.secrets[KEY] == "old"
    # Its own message is not trusted to be free of values: only its type.
    (event,) = [s[1] for s in _LAST_STEPS if s[0] == "record"]
    assert event.verification == "failed"
    assert event.verification_reason == "RuntimeError during the rotation"


def test_a_railway_error_is_recorded_with_its_own_message() -> None:
    from rotator_cog.railway import RailwayError

    recorded: list[Any] = []

    def redeploy(cfg: str, targets: Any) -> None:
        raise RailwayError("deployment d-1 ended CRASHED")

    with pytest.raises(RailwayError):
        worker._rotate(
            _job(config="dev"),
            INVENTORY["secrets"][KEY],
            TOKEN,
            inventory=INVENTORY,
            report=Report(),
            redeploy=redeploy,
            verify=lambda *a: None,
            record=recorded.append,
            doppler_factory=lambda project, cfg, token: doppler.Doppler(
                project,
                cfg,
                token,
                transport=httpx.MockTransport(FakeDoppler({KEY: "old"}).handle),
            ),
            now=lambda: NOW,
        )
    assert recorded[0].verification_reason == "deployment d-1 ended CRASHED"


def test_a_secret_missing_from_doppler_is_reported_and_nothing_is_written() -> None:
    fake = FakeDoppler()
    report, steps = _handover(fake)
    assert report.lines == [("issue", "not in Doppler")]
    assert steps == [] and fake.writes() == []


def test_without_redeploy_verify_and_record_nothing_is_written() -> None:
    report = Report()
    worker._rotate(
        _job(config="dev"),
        INVENTORY["secrets"]["DEEJAY_COG_API_KEY"],
        TOKEN,
        inventory=INVENTORY,
        report=report,
        doppler_factory=lambda *a: pytest.fail("Doppler reached"),
    )
    assert report.lines == [("issue", "rotation not built yet")]


def test_no_value_reaches_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeDoppler({KEY: "old"})
    with caplog.at_level("DEBUG"):
        _handover(fake)
    logged = caplog.text
    assert fake.secrets[KEY] not in logged and TOKEN not in logged


# -- redeploying through Railway -------------------------------------------


class FakeRailway:
    def __init__(self, environment: str = "development", services: Any = None):
        self.environment = environment
        self.services = services or {
            "api-kaianolevine-com": "s-api",
            "db-poller": "s-poll",
        }
        self.calls: list[tuple] = []

    def environment_name(self) -> str:
        return self.environment

    def service_ids(self) -> dict[str, str]:
        return dict(self.services)

    def wait_for_variables(
        self, service_id: str, expected: Any, *, timeout: float
    ) -> None:
        self.calls.append(("vars", service_id, dict(expected)))

    def redeploy(self, service_id: str) -> str:
        self.calls.append(("redeploy", service_id))
        return f"d-{service_id}"

    def wait_for_deployments(self, ids: list[str], *, timeout: float) -> None:
        self.calls.append(("wait", ids))


def _redeployer(fake: FakeRailway, read: list[str] | None = None) -> Any:
    def token_reader(config: str) -> str:
        (read if read is not None else []).append(config)
        return TOKEN

    return worker.railway_redeployer(
        token_reader=token_reader, railway_factory=lambda token: fake
    )


def test_the_redeployer_waits_for_values_then_redeploys_and_waits() -> None:
    from rotator_cog.railway import digest

    fake = FakeRailway()
    read: list[str] = []
    _redeployer(fake, read)(
        "dev",
        {"api-kaianolevine-com": {KEY: "new", PREV: None}, "db-poller": {KEY: "new"}},
    )
    assert read == ["dev"]
    assert fake.calls == [
        ("vars", "s-api", {KEY: digest("new"), PREV: None}),
        ("vars", "s-poll", {KEY: digest("new")}),
        ("redeploy", "s-api"),
        ("redeploy", "s-poll"),
        ("wait", ["d-s-api", "d-s-poll"]),
    ]


def test_a_token_for_the_wrong_environment_is_refused() -> None:
    from rotator_cog.railway import RailwayError

    fake = FakeRailway(environment="production")
    with pytest.raises(RailwayError, match="reaches production, not development"):
        _redeployer(fake)("dev", {"api-kaianolevine-com": {KEY: "new"}})
    assert fake.calls == []


def test_a_service_missing_from_railway_is_refused_before_any_redeploy() -> None:
    from rotator_cog.railway import RailwayError

    fake = FakeRailway(services={"api-kaianolevine-com": "s-api"})
    with pytest.raises(RailwayError, match="db-poller"):
        _redeployer(fake)("dev", {"api-kaianolevine-com": {}, "db-poller": {}})
    assert fake.calls == []


def test_the_railway_token_is_read_from_the_configs_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAILWAY_TOKEN_PRD_PARAMETER", "/rotator/RAILWAY_TOKEN_PRD")
    ssm = FakeSsm()
    assert worker.railway_token("prd", client=ssm) == TOKEN
    assert ssm.calls[0]["Name"] == "/rotator/RAILWAY_TOKEN_PRD"


# -- checking a machine key ------------------------------------------------


def _whoami(status: int = 200, body: Any = None, seen: list | None = None) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        payload = {"verified": {"subject": "deejay-cog"}} if body is None else body
        return httpx.Response(status, json=payload)

    return worker.machine_verifier(INVENTORY, transport=httpx.MockTransport(handle))


def test_machine_names_follow_the_variable() -> None:
    assert worker.machine_name("DEEJAY_COG_API_KEY") == "deejay-cog"
    assert worker.machine_name("DB_OPS_API_KEY") == "db-ops"


def test_a_key_that_names_its_machine_is_verified_at_the_configs_api() -> None:
    seen: list[httpx.Request] = []
    assert _whoami(seen=seen)(KEY, {}, "dev", "dev_new") is None
    (request,) = seen
    assert str(request.url) == "https://dev-api.test.invalid/v1/identity/whoami"
    assert request.headers["Authorization"] == "Bearer dev_new"
    assert request.headers["User-Agent"] == worker.USER_AGENT
    seen.clear()
    _whoami(seen=seen)(KEY, {}, "prd", "new")
    assert seen[0].url.host == "api.test.invalid"


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (401, {"detail": "no"}, "whoami answered HTTP 401"),
        (200, {"verified": {"subject": "watcher-cog"}}, "another machine"),
        (200, {"nothing": 1}, "without a subject"),
    ],
)
def test_a_key_that_fails_whoami_says_why_without_the_value(
    status: int, body: Any, reason: str
) -> None:
    got = _whoami(status, body)(KEY, {}, "prd", "secret-new-value")
    assert got is not None and reason in got and "secret-new-value" not in got


def test_an_api_that_cannot_be_reached_is_a_failed_check() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    verify = worker.machine_verifier(INVENTORY, transport=httpx.MockTransport(fail))
    assert verify(KEY, {}, "prd", "v") == "whoami could not be asked: ConnectError"


# -- messages and the handler ----------------------------------------------


def test_a_job_message_is_rotated(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[RotationRequest, str]] = []
    monkeypatch.setattr(
        worker, "rotate", lambda job, *, run_id, **kw: seen.append((job, run_id))
    )
    worker.process_message(_body(), run_id="m-1")
    assert seen == [(_job(), "m-1")]


def test_a_sweep_is_acknowledged_and_rotates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "rotate", lambda *a, **kw: pytest.fail("rotated"))
    worker.process_message(_body("rotation.sweep", config="prd"), run_id="m-1")


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        _body("task.groom"),
        _body(name="lower_case", config="prd", mode="routine"),
        _body(name="DEEJAY_COG_API_KEY", config="stg", mode="routine"),
        _body("rotation.sweep", config="prd", extra=True),
    ],
)
def test_unreadable_messages_are_refused(body: str) -> None:
    with pytest.raises(worker.UnprocessableMessage):
        worker.process_message(body, run_id="m-1")


def test_the_handler_acknowledges_a_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "load_secrets", lambda refresh: [])
    event = {
        "Records": [
            {
                "messageId": "m-1",
                "body": _body("rotation.sweep", config="dev"),
                "attributes": {"ApproximateReceiveCount": "1"},
            }
        ]
    }
    assert worker.lambda_handler(event, None) == {"batchItemFailures": []}


def test_the_handler_reports_an_unreadable_message_and_names_it(
    monkeypatch: pytest.MonkeyPatch, posted: list[tuple]
) -> None:
    monkeypatch.setattr(worker, "load_secrets", lambda refresh: [])
    event = {"Records": [{"messageId": "m-1", "body": "not json", "attributes": {}}]}
    assert worker.lambda_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "m-1"}]
    }
    ((args, _),) = posted
    assert args[1] == "ERROR"


def test_the_handler_names_a_job_that_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "load_secrets", lambda refresh: [])

    def crash(body: str, *, run_id: str, **kwargs: Any) -> None:
        raise worker.TokenUnavailable("DOPPLER_TOKEN_PRD_PARAMETER is not set")

    monkeypatch.setattr(worker, "process_message", crash)
    event = {"Records": [{"messageId": "m-2", "body": _body(), "attributes": {}}]}
    assert worker.lambda_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "m-2"}]
    }


def test_the_handler_names_everything_when_settings_cannot_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(refresh: bool) -> list[str]:
        raise RuntimeError("ssm")

    monkeypatch.setattr(worker, "load_secrets", fail)
    event = {
        "Records": [{"messageId": "a", "body": "x"}, {"messageId": "b", "body": "y"}]
    }
    assert worker.lambda_handler(event, None)["batchItemFailures"] == [
        {"itemIdentifier": "a"},
        {"itemIdentifier": "b"},
    ]
