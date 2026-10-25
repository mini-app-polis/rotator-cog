"""The daily sweep: what it retires, what is due, what is drift, and one run."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from mini_app_polis.api.contract import RotationEvent
from test_doppler import FakeDoppler

from rotator_cog import doppler, sweep, worker

NOW = dt.datetime(2026, 10, 25, 14, 0, tzinfo=dt.UTC)
TOKEN = "dp.st.token-for-tests"
ROTATOR = "rotator-cog"


def _event(
    name: str = "DEEJAY_COG_API_KEY",
    *,
    config: str = "prd",
    age: dt.timedelta = dt.timedelta(days=2),
    actor: str = ROTATOR,
    mode: str = "routine",
    verification: str = "ok",
    retired: dt.datetime | None = None,
) -> RotationEvent:
    return RotationEvent(
        id="00000000-0000-4000-8000-000000000001",
        name=name,
        config=config,  # type: ignore[arg-type]
        kind="machine-key",
        mode=mode,  # type: ignore[arg-type]
        actor=actor,
        minted=NOW - age,
        retired=retired,
        verification=verification,  # type: ignore[arg-type]
        verification_reason=None
        if verification == "ok"
        else "whoami answered HTTP 401",
        recorded_at=NOW - age,
    )


def _retire(events: list[RotationEvent], config: str = "prd", last: Any = None):
    return sweep.retirements(
        events,
        config,
        actor=ROTATOR,
        machine_of=worker.machine_name,
        last_use=lambda machine: last,
        now=NOW,
    )


# -- retiring ---------------------------------------------------------------


def test_a_previous_key_unused_for_a_day_is_retired() -> None:
    (r,) = _retire([_event()], last=NOW - dt.timedelta(hours=25))
    assert (r.machine, r.forced) == ("deejay-cog", False)


def test_a_previous_key_used_within_the_day_stays() -> None:
    assert _retire([_event()], last=NOW - dt.timedelta(hours=3)) == []


def test_a_previous_key_never_used_is_retired_a_day_after_the_rotation() -> None:
    assert _retire([_event(age=dt.timedelta(hours=23))]) == []
    assert len(_retire([_event(age=dt.timedelta(hours=25))])) == 1


def test_at_seven_days_a_key_still_in_use_is_retired_and_flagged() -> None:
    last = NOW - dt.timedelta(hours=1)
    (r,) = _retire([_event(age=dt.timedelta(days=7))], last=last)
    assert r.forced and r.last_used == last


def test_dev_retires_on_time_without_asking_about_use() -> None:
    def last_use(machine: str) -> Any:
        pytest.fail("asked about dev use")

    found = sweep.retirements(
        [_event(config="dev", age=dt.timedelta(hours=25))],
        "dev",
        actor=ROTATOR,
        machine_of=worker.machine_name,
        last_use=last_use,
        now=NOW,
    )
    assert len(found) == 1


@pytest.mark.parametrize(
    "event",
    [
        _event(actor="kaiano.levine@gmail.com"),
        _event(mode="compromised"),
        _event(verification="failed"),
        _event(retired=NOW - dt.timedelta(days=1)),
    ],
)
def test_only_the_workers_live_routine_handovers_are_retired(event) -> None:
    assert _retire([event], last=None) == []


def test_a_retired_event_keeps_its_key_and_gains_the_time() -> None:
    event = _event()
    retired = sweep.retired_event(event, NOW)
    assert (retired.name, retired.config, retired.minted) == (
        event.name,
        event.config,
        event.minted,
    )
    assert retired.retired == NOW


# -- what is due ------------------------------------------------------------


def test_due_is_never_rotated_or_past_cadence_most_overdue_first() -> None:
    latest = {
        "A_API_KEY": _event("A_API_KEY", age=dt.timedelta(days=91), retired=NOW),
        "B_API_KEY": _event("B_API_KEY", age=dt.timedelta(days=200), retired=NOW),
        "C_API_KEY": _event("C_API_KEY", age=dt.timedelta(days=10), retired=NOW),
    }
    names = ["A_API_KEY", "B_API_KEY", "C_API_KEY", "D_API_KEY"]
    overdue, failed = sweep.due(
        names, latest, dict.fromkeys(names, 90), actor=ROTATOR, now=NOW
    )
    assert overdue == ["D_API_KEY", "B_API_KEY", "A_API_KEY"] and failed == []


def test_a_key_mid_handover_is_not_rotated_again() -> None:
    latest = {"A_API_KEY": _event("A_API_KEY", age=dt.timedelta(days=100))}
    assert sweep.due(
        ["A_API_KEY"], latest, {"A_API_KEY": 90}, actor=ROTATOR, now=NOW
    ) == ([], [])


def test_a_failed_rotation_is_reported_not_retried() -> None:
    latest = {"A_API_KEY": _event("A_API_KEY", verification="failed")}
    assert sweep.due(
        ["A_API_KEY"], latest, {"A_API_KEY": 90}, actor=ROTATOR, now=NOW
    ) == (
        [],
        ["A_API_KEY"],
    )


# -- drift --------------------------------------------------------------------


def test_drift_is_secret_looking_names_unknown_and_entries_missing() -> None:
    inventory = {
        "secrets": {
            "A_API_KEY": {"kind": "machine-key", "configs": ["prd", "dev"]},
            "B_API_KEY": {"kind": "machine-key"},
            "DB_SECRET": {"kind": "aws-access-key", "id_var": "DB_KEY_ID"},
            "C_TOKEN": {"kind": "manual", "configs": []},
        }
    }
    names = {
        "A_API_KEY",
        "A_API_KEY_PREVIOUS",
        "DB_SECRET",
        "DB_KEY_ID",
        "LOGGING_LEVEL",
        "NEW_TOKEN",
        "DOPPLER_CONFIG",
    }
    assert sweep.drift(names, inventory, "prd") == (["NEW_TOKEN"], ["B_API_KEY"])
    assert sweep.drift(names, inventory, "dev") == (["NEW_TOKEN"], [])


# -- one run ------------------------------------------------------------------


INVENTORY = {
    "doppler_project": "proj",
    "key_prefix": {"dev": "dev_"},
    "cadence_days": {"machine-key": 90},
    "api_base_url": "https://api.test.invalid",
    "api_base_url_dev": "https://dev-api.test.invalid",
    "secrets": {
        "DEEJAY_COG_API_KEY": {
            "kind": "machine-key",
            "configs": ["prd", "dev"],
            "redeploy": ["api-kaianolevine-com"],
        },
        "WATCHER_COG_API_KEY": {
            "kind": "machine-key",
            "configs": ["prd", "dev"],
            "redeploy": ["api-kaianolevine-com", "db-poller"],
        },
        "OPENAI_API_KEY": {"kind": "manual"},
    },
}


class FakeApi:
    def __init__(self, events: list[RotationEvent], last: Any = None) -> None:
        self.events = events
        self.last = last

    def latest(self, config: str) -> list[RotationEvent]:
        return self.events

    def last_previous_use(self, machine: str) -> Any:
        return self.last


def _run(fake: FakeDoppler, api: FakeApi, posted: list) -> list[Any]:
    steps: list[Any] = []

    def redeploy(cfg: str, targets: Any) -> None:
        steps.append(("redeploy", dict(fake.secrets), targets))

    worker.run_sweep(
        "prd",
        run_id="s-1",
        inventory_loader=lambda: INVENTORY,
        token_reader=lambda config: TOKEN,
        api_factory=lambda machine: api,
        doppler_factory=lambda project, cfg, token: doppler.Doppler(
            project, cfg, token, transport=httpx.MockTransport(fake.handle)
        ),
        redeploy_factory=lambda: redeploy,
        verifier_factory=lambda inventory: lambda *a: None,
        recorder_factory=lambda: lambda event: steps.append(("record", event)),
        now=lambda: NOW,
    )
    return steps


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    sent: list[tuple] = []
    monkeypatch.setattr(
        "mini_app_polis.pipeline_status.post_run_finding",
        lambda *a, **kw: sent.append((a, kw)),
    )
    return sent


def test_a_run_rotates_the_most_overdue_key_and_keeps_the_old_one_accepted(
    posted: list,
) -> None:
    """A scheduled rotation of a due prd key leaves the old key in the
    previous slot, accepted by the API until it is retired: no 401s."""
    fake = FakeDoppler({"DEEJAY_COG_API_KEY": "old-d", "WATCHER_COG_API_KEY": "old-w"})
    api = FakeApi(
        [
            _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=95), retired=NOW),
            _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=120), retired=NOW),
        ]
    )
    steps = _run(fake, api, posted)
    new = fake.secrets["WATCHER_COG_API_KEY"]
    assert new != "old-w"
    assert fake.secrets["WATCHER_COG_API_KEY_PREVIOUS"] == "old-w"
    assert fake.secrets["DEEJAY_COG_API_KEY"] == "old-d"
    # Every redeploy keeps the old key accepted by the API.
    for _, _, targets in [s for s in steps if s[0] == "redeploy"]:
        held = targets["api-kaianolevine-com"]
        assert "old-w" in held.values()
    (record,) = [s[1] for s in steps if s[0] == "record"]
    assert record.name == "WATCHER_COG_API_KEY" and record.retired is None
    # A manual entry is never rotated by the sweep.
    assert "OPENAI_API_KEY" not in fake.secrets
    assert not [w for w in fake.writes() if "OPENAI" in str(w)]


def test_a_run_that_retires_rotates_nothing_that_day(posted: list) -> None:
    fake = FakeDoppler(
        {
            "DEEJAY_COG_API_KEY": "new-d",
            "DEEJAY_COG_API_KEY_PREVIOUS": "old-d",
            "WATCHER_COG_API_KEY": "old-w",
        }
    )
    api = FakeApi(
        [
            _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=2)),
            _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=120), retired=NOW),
        ],
        last=NOW - dt.timedelta(days=2),
    )
    steps = _run(fake, api, posted)
    assert "DEEJAY_COG_API_KEY_PREVIOUS" not in fake.secrets
    assert fake.secrets["WATCHER_COG_API_KEY"] == "old-w"
    (redeploy,) = [s for s in steps if s[0] == "redeploy"]
    assert redeploy[2] == {
        "api-kaianolevine-com": {"DEEJAY_COG_API_KEY_PREVIOUS": None}
    }
    (record,) = [s[1] for s in steps if s[0] == "record"]
    assert record.name == "DEEJAY_COG_API_KEY" and record.retired == NOW


def test_a_forced_retirement_names_the_machine_still_on_the_old_key(
    posted: list,
) -> None:
    fake = FakeDoppler(
        {"DEEJAY_COG_API_KEY": "new-d", "DEEJAY_COG_API_KEY_PREVIOUS": "old-d"}
    )
    api = FakeApi(
        [_event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=8))],
        last=NOW - dt.timedelta(hours=2),
    )
    _run(fake, api, posted)
    ((args, kwargs),) = posted
    assert str(args[1]).endswith("WARN")
    assert "previous key retired while still in use" in kwargs["text"]
    assert "deejay-cog" in kwargs["text"]
    assert "old-d" not in kwargs["text"] and "new-d" not in kwargs["text"]


def test_drift_is_reported_by_name(posted: list) -> None:
    fake = FakeDoppler(
        {
            "DEEJAY_COG_API_KEY": "d",
            "WATCHER_COG_API_KEY": "w",
            "SOMETHING_NEW_TOKEN": "x",
        }
    )
    api = FakeApi(
        [
            _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
            _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
        ]
    )
    steps = _run(fake, api, posted)
    assert not [s for s in steps if s[0] in ("redeploy", "record")]
    ((args, kwargs),) = posted
    assert "SOMETHING_NEW_TOKEN" in kwargs["text"]
    assert "OPENAI_API_KEY" in kwargs["text"]  # held in prd, missing from Doppler
