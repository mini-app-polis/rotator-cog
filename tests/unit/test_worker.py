"""The Lambda worker: messages, job checks and the Doppler tokens. No AWS, no API."""

from __future__ import annotations

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
        "GITHUB_WEBHOOK_SECRET": {"kind": "generated", "steps": "paste it"},
    },
    "doppler_project": "proj",
    "key_prefix": {"dev": "dev_"},
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
    kinds = {e["kind"] for e in worker.load_inventory()["secrets"].values()}
    assert worker.WORKER_KINDS <= kinds


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
    assert worker.validate(_job("TICK_SECRET"), INVENTORY)["kind"] == "generated"


@pytest.mark.parametrize(
    ("job", "reason"),
    [
        (_job("NOT_LISTED"), "not in the inventory"),
        (_job("OPENAI_API_KEY"), "kind manual is not rotated by the worker"),
        # No configs means prd only.
        (_job("TICK_SECRET", "dev"), "not held in dev"),
        (_job("CI_VALIDATOR_API_KEY"), "also held in GitHub"),
        (_job("GITHUB_WEBHOOK_SECRET"), "needs a manual step"),
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


def test_a_valid_job_reads_its_configs_token_and_is_reported_not_rotated(
    posted: list[tuple],
) -> None:
    read: list[str] = []
    _rotate(_job(config="dev"), read)
    assert read == ["dev"]
    ((args, kwargs),) = posted
    assert str(args[1]).endswith("WARN")
    assert "rotation not built yet" in kwargs["text"]
    assert TOKEN not in json.dumps([args, kwargs], default=str)


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
    report = Report()

    def redeploy(cfg: str, targets: Any) -> None:
        steps.append(("redeploy", cfg, dict(fake.secrets), targets))
        if redeploy_fails:
            raise RuntimeError("redeploy failed")

    def verify(name: str, entry: Any, cfg: str, value: str) -> bool:
        steps.append(("verify", value == fake.secrets[name]))
        return ok

    job = RotationRequest(name="DEEJAY_COG_API_KEY", config=config, mode=mode)  # type: ignore[arg-type]
    worker._rotate(
        job,
        INVENTORY["secrets"]["DEEJAY_COG_API_KEY"],
        TOKEN,
        inventory=INVENTORY,
        report=report,
        redeploy=redeploy,
        verify=verify,
        doppler_factory=lambda project, cfg, token: doppler.Doppler(
            project, cfg, token, transport=httpx.MockTransport(fake.handle)
        ),
    )
    return report, steps


KEY, PREV = "DEEJAY_COG_API_KEY", "DEEJAY_COG_API_KEY_PREVIOUS"


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
    assert report.lines == [("updated", "rotated")]


def test_a_failed_verification_is_reported() -> None:
    report, _ = _handover(FakeDoppler({KEY: "old"}), ok=False)
    assert report.lines == [("issue", "verification failed")]


def test_a_failed_redeploy_stops_before_callers_switch() -> None:
    fake = FakeDoppler({KEY: "old"})
    with pytest.raises(RuntimeError, match="redeploy failed"):
        _handover(fake, redeploy_fails=True)
    assert fake.secrets[KEY] == "old"


def test_a_secret_missing_from_doppler_is_reported_and_nothing_is_written() -> None:
    fake = FakeDoppler()
    report, steps = _handover(fake)
    assert report.lines == [("issue", "not in Doppler")]
    assert steps == [] and fake.writes() == []


def test_without_redeploy_and_verify_nothing_is_written() -> None:
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
