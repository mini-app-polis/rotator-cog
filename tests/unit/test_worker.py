"""The Lambda worker: messages, job checks and the Doppler tokens. No AWS, no API."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mini_app_polis.api.contract import RotationRequest

from rotator_cog import worker

#: A stand-in token; tests check it never reaches an error or a report.
TOKEN = "dp.st.token-for-tests"

INVENTORY = {
    "secrets": {
        "DEEJAY_COG_API_KEY": {"kind": "machine-key", "configs": ["prd", "dev"]},
        "TICK_SECRET": {"kind": "generated"},
        "OPENAI_API_KEY": {"kind": "manual"},
    }
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
