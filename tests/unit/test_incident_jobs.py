"""An incident's jobs: rotated compromised, recorded with the incident, and a
failure handed to a person without stopping the jobs behind it.

The real rotation code runs against fakes: Doppler over a mock transport,
and stand-ins for the redeploy, the check, the record and the API. Nothing
reaches a real queue, API or board.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
from typing import Any

import httpx
import pytest
from mini_app_polis.api.contract import RotationRequest
from test_doppler import FakeDoppler
from test_incident import FakeApi

from rotator_cog import doppler, worker

INCIDENT = "7c2d4e1a-9b3f-4a6c-8d5e-1f2a3b4c5d6e"
TOKEN = "dp.st.token-for-tests"
NOW = dt.datetime(2026, 10, 29, 17, 0, tzinfo=dt.UTC)
KEY, PREV = "DEEJAY_COG_API_KEY", "DEEJAY_COG_API_KEY_PREVIOUS"

INVENTORY: dict[str, Any] = {
    "doppler_project": "proj",
    "key_prefix": {"dev": "dev_"},
    "api_base_url": "https://api.test.invalid",
    "api_base_url_dev": "https://dev-api.test.invalid",
    "secrets": {
        # Tier 0 and tier 1 machine keys, as an incident queues them.
        "TIER0_API_KEY": {
            "kind": "machine-key",
            "tier": 0,
            "redeploy": ["api-kaianolevine-com"],
            "revoke": "delete it from Doppler",
        },
        KEY: {
            "kind": "machine-key",
            "tier": 1,
            "configs": ["prd", "dev"],
            "redeploy": ["api-kaianolevine-com", "db-poller"],
        },
    },
}


class World:
    """Doppler, the redeploys, the check and the record, in memory."""

    def __init__(self, secrets: dict[str, str], *, failing: set[str] = frozenset()):  # type: ignore[assignment]
        self.doppler = FakeDoppler(secrets)
        self.failing = failing
        self.events: list[Any] = []
        self.redeploys: list[Any] = []
        self.rotated: list[RotationRequest] = []
        self.api = FakeApi()

    def rotator(self, job: RotationRequest, entry: Any, token: str, **kw: Any) -> None:
        """worker._rotate itself, with this world's far ends."""
        self.rotated.append(job)

        def redeploy(cfg: str, targets: Any) -> None:
            self.redeploys.append(targets)
            if job.name in self.failing:
                raise RuntimeError("redeploy failed")

        worker._rotate(
            job,
            entry,
            token,
            inventory=INVENTORY,
            report=kw["report"],
            redeploy=redeploy,
            verify=lambda *a: None,
            record=self.events.append,
            doppler_factory=lambda project, cfg, tok: doppler.Doppler(
                project, cfg, tok, transport=httpx.MockTransport(self.doppler.handle)
            ),
            now=lambda: NOW,
        )

    def rotate(self, job: RotationRequest) -> None:
        worker.rotate(
            job,
            run_id="m-1",
            inventory_loader=lambda: INVENTORY,
            token_reader=lambda config: TOKEN,
            rotator=self.rotator,
            incident_api_factory=lambda machine: self.api,
        )


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    sent: list[tuple] = []
    monkeypatch.setattr(
        "mini_app_polis.pipeline_status.post_run_finding",
        lambda *a, **kw: sent.append((a, kw)),
    )
    return sent


def _job(
    name: str = KEY, *, mode: str = "routine", incident: str | None = INCIDENT
) -> RotationRequest:
    return RotationRequest(
        name=name,
        config="prd",
        mode=mode,  # type: ignore[arg-type]
        incident_id=incident,
    )


def _text(posted: list[tuple], i: int = 0) -> str:
    return str(posted[i][1]["text"])


# -- an incident job is compromised and recorded with its incident ----------


def test_an_incident_job_is_rotated_compromised_whatever_it_says(
    posted: list,
) -> None:
    world = World({KEY: "old", PREV: "older"})
    world.rotate(_job(mode="routine"))

    (job,) = world.rotated
    assert job.mode == "compromised"
    new = world.doppler.secrets[KEY]
    # No previous slot kept: emptied in the same write.
    assert world.doppler.secrets == {KEY: new, PREV: ""}
    assert len(world.redeploys) == 1
    (event,) = world.events
    assert event.mode == "compromised"
    assert event.incident_id == INCIDENT
    assert event.retired == NOW
    assert event.verification == "ok"
    kwargs = posted[0][1]
    assert kwargs["incident"] == INCIDENT
    assert f"incident {INCIDENT}: rotated\n  ~ {KEY} in prd" in _text(posted)
    assert world.api.tasks == []


def test_a_job_without_an_incident_is_unchanged(posted: list) -> None:
    world = World({KEY: "old"})
    world.rotate(_job(incident=None))
    (event,) = world.events
    assert event.mode == "routine" and event.incident_id is None
    assert "incident" not in _text(posted)
    assert "incident" not in posted[0][1]


def test_a_failure_without_an_incident_still_fails_the_job(posted: list) -> None:
    world = World({KEY: "old"}, failing={KEY})
    with pytest.raises(RuntimeError, match="redeploy failed"):
        world.rotate(_job(incident=None))
    assert world.api.tasks == []


# -- a failed incident job ---------------------------------------------------


def test_a_failed_incident_job_is_recorded_handed_to_a_person_and_acknowledged(
    posted: list,
) -> None:
    world = World({"TIER0_API_KEY": "old"}, failing={"TIER0_API_KEY"})

    world.rotate(_job("TIER0_API_KEY"))  # does not raise

    (event,) = world.events
    assert event.verification == "failed"
    assert event.incident_id == INCIDENT
    assert event.verification_reason == "RuntimeError during the rotation"
    (task,) = world.api.tasks
    assert task.external_id == f"secret-rotation.prd.TIER0_API_KEY.{INCIDENT}"
    assert "--compromised" in (task.draft.body or "")
    assert "Revoke: delete it from Doppler" in (task.draft.body or "")
    assert world.api.task_requested == {("TIER0_API_KEY", "prd")}
    text = _text(posted)
    assert str(posted[0][0][1]).endswith("WARN")
    assert f"incident {INCIDENT}: rotation failed\n  TIER0_API_KEY in prd" in text
    assert f"incident {INCIDENT}: task opened\n  + TIER0_API_KEY in prd" in text


def test_a_rejected_incident_job_is_handed_to_a_person(posted: list) -> None:
    world = World({})
    world.rotate(_job("NOT_IN_THE_INVENTORY_KEY"))
    assert world.rotated == []
    (task,) = world.api.tasks
    assert task.external_id.endswith(f".NOT_IN_THE_INVENTORY_KEY.{INCIDENT}")
    assert "rotation job rejected" in _text(posted)


def test_a_secret_missing_from_doppler_is_handed_to_a_person(posted: list) -> None:
    world = World({})
    world.rotate(_job())
    assert world.events == []
    assert [t.external_id for t in world.api.tasks] == [
        f"secret-rotation.prd.{KEY}.{INCIDENT}"
    ]


def test_a_task_that_cannot_be_asked_for_is_reported_not_raised(
    posted: list,
) -> None:
    world = World({"TIER0_API_KEY": "old"}, failing={"TIER0_API_KEY"})
    world.api.refuse_task_for = {"TIER0_API_KEY"}
    world.rotate(_job("TIER0_API_KEY"))
    assert world.api.task_requested == set()
    assert f"incident {INCIDENT}: task not asked for" in _text(posted)


def test_repeating_a_failed_job_makes_no_second_task(posted: list) -> None:
    world = World({"TIER0_API_KEY": "old"}, failing={"TIER0_API_KEY"})
    world.rotate(_job("TIER0_API_KEY"))
    world.rotate(_job("TIER0_API_KEY"))
    assert len(world.api.board) == 1
    assert world.api.task_requested == {("TIER0_API_KEY", "prd")}


def test_a_tier_0_failure_does_not_stop_tier_1(
    monkeypatch: pytest.MonkeyPatch, posted: list
) -> None:
    """Through the handler, as the queue delivers the incident's jobs."""
    world = World({"TIER0_API_KEY": "old0", KEY: "old1"}, failing={"TIER0_API_KEY"})
    monkeypatch.setattr(worker, "load_secrets", lambda refresh: [])
    monkeypatch.setattr(
        worker,
        "rotate",
        functools.partial(
            worker.rotate,
            inventory_loader=lambda: INVENTORY,
            token_reader=lambda config: TOKEN,
            rotator=world.rotator,
            incident_api_factory=lambda machine: world.api,
        ),
    )

    def record(message_id: str, name: str) -> dict[str, Any]:
        body = {
            "type": "rotation.job",
            "version": 1,
            "payload": _job(name).model_dump(mode="json", exclude_none=True),
        }
        return {
            "messageId": message_id,
            "body": json.dumps(body),
            "attributes": {"ApproximateReceiveCount": "1"},
        }

    result = worker.lambda_handler(
        {"Records": [record("m-0", "TIER0_API_KEY"), record("m-1", KEY)]}, None
    )

    # Nothing dead-lettered: the failed job was acknowledged.
    assert result == {"batchItemFailures": []}
    assert [j.name for j in world.rotated] == ["TIER0_API_KEY", KEY]
    assert [(e.name, e.verification) for e in world.events] == [
        ("TIER0_API_KEY", "failed"),
        (KEY, "ok"),
    ]
    assert [t.external_id for t in world.api.tasks] == [
        f"secret-rotation.prd.TIER0_API_KEY.{INCIDENT}"
    ]


def test_no_value_reaches_a_report_task_event_or_log(
    posted: list, caplog: pytest.LogCaptureFixture
) -> None:
    world = World({KEY: "old-secret-value", PREV: "older-secret-value"})
    world.rotate(_job())
    failing = World({"TIER0_API_KEY": "tier0-secret-value"}, failing={"TIER0_API_KEY"})
    failing.rotate(_job("TIER0_API_KEY"))
    values = [
        "old-secret-value",
        "older-secret-value",
        "tier0-secret-value",
        world.doppler.secrets[KEY],
        failing.doppler.secrets["TIER0_API_KEY"],
        TOKEN,
    ]
    everything = json.dumps(
        [
            [str(p) for p in posted],
            [t.model_dump(mode="json") for t in failing.api.tasks],
            [e.model_dump(mode="json") for e in world.events + failing.events],
            caplog.text,
        ]
    )
    for value in values:
        assert value not in everything
