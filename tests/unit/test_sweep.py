"""The daily sweep: what it retires, what is due, what is drift, and one run."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from mini_app_polis.api.contract import RotationEvent, SecretChange
from test_doppler import FakeDoppler

from rotator_cog import doppler, sweep, worker

NOW = dt.datetime(2026, 10, 25, 14, 0, tzinfo=dt.UTC)
TOKEN = "dp.st.token-for-tests"
ROTATOR = "rotator-cog"
#: rotate.py records a rotation under the person who ran it.
PERSON = "owner@example.com"


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
        if verification != "failed"
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


def test_an_unchecked_rotation_counts_as_rotated() -> None:
    """Not reported as failed, and due again only once its cadence passes."""
    recent = {
        "A_API_KEY": _event(
            "A_API_KEY",
            age=dt.timedelta(days=10),
            actor=PERSON,
            verification="unchecked",
        )
    }
    assert sweep.due(
        ["A_API_KEY"], recent, {"A_API_KEY": 90}, actor=ROTATOR, now=NOW
    ) == ([], [])
    old = {
        "A_API_KEY": _event(
            "A_API_KEY",
            age=dt.timedelta(days=100),
            actor=PERSON,
            verification="unchecked",
        )
    }
    assert sweep.due(["A_API_KEY"], old, {"A_API_KEY": 90}, actor=ROTATOR, now=NOW) == (
        ["A_API_KEY"],
        [],
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
    "cadence_days": {"machine-key": 90, "manual": 365},
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
    def __init__(
        self,
        events: list[RotationEvent],
        last: Any = None,
        *,
        reported: list[Any] | None = None,
        all_events: list[RotationEvent] | None = None,
    ) -> None:
        self.events_ = events
        self.last = last
        self.asked: list[Any] = []
        self.reported = reported or []
        self.all_events = all_events
        self.change_reads: list[Any] = []

    def latest(self, config: str) -> list[RotationEvent]:
        return self.events_

    def last_previous_use(self, machine: str) -> Any:
        return self.last

    def request_task(self, request: Any) -> None:
        self.asked.append(request)

    def events(self, config: str) -> list[RotationEvent]:
        return self.all_events if self.all_events is not None else self.events_

    def changes(self, config: str, *, since: Any, until: Any) -> list[Any]:
        self.change_reads.append((config, since, until))
        return self.reported


def _run(
    fake: FakeDoppler,
    api: FakeApi,
    posted: list,
    check: Any = None,
    *,
    config: str = "prd",
    inventory: Any = None,
) -> list[Any]:
    steps: list[Any] = []

    def redeploy(cfg: str, targets: Any) -> None:
        steps.append(("redeploy", dict(fake.secrets), targets))

    worker.run_sweep(
        config,
        run_id="s-1",
        inventory_loader=lambda: inventory or INVENTORY,
        token_reader=lambda config: TOKEN,
        api_factory=lambda machine: api,
        doppler_factory=lambda project, cfg, token: doppler.Doppler(
            project, cfg, token, transport=httpx.MockTransport(fake.handle)
        ),
        redeploy_factory=lambda: redeploy,
        checker_factory=lambda: check or (lambda cfg, needs: []),
        verifier_factory=lambda inventory: lambda *a: None,
        recorder_factory=lambda: lambda event: steps.append(("record", event)),
        now=lambda: NOW,
    )
    return steps


@pytest.fixture(autouse=True)
def _rotation_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(worker.SWEEP_ROTATE_VAR, "true")


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


def test_a_key_the_services_are_not_ready_for_is_passed_over(posted: list) -> None:
    """The most overdue key cannot hold up the rest: it is reported, and the
    next most overdue is rotated."""
    fake = FakeDoppler({"DEEJAY_COG_API_KEY": "old-d", "WATCHER_COG_API_KEY": "old-w"})
    api = FakeApi(
        [
            _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=95), retired=NOW),
            _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=120), retired=NOW),
        ]
    )
    asked: list[str] = []

    def check(cfg: str, needs: Any) -> list[str]:
        asked.append(next(iter(needs["api-kaianolevine-com"])))
        return (
            ["db-poller is not in Railway production"] if "db-poller" in needs else []
        )

    steps = _run(fake, api, posted, check)
    assert fake.secrets["WATCHER_COG_API_KEY"] == "old-w"
    assert fake.secrets["DEEJAY_COG_API_KEY"] != "old-d"
    (record,) = [s[1] for s in steps if s[0] == "record"]
    assert record.name == "DEEJAY_COG_API_KEY"
    assert asked[:2] == ["WATCHER_COG_API_KEY", "DEEJAY_COG_API_KEY"]
    ((args, kwargs),) = posted
    assert "not ready to rotate" in kwargs["text"]
    assert "WATCHER_COG_API_KEY in prd" in kwargs["text"]


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


@pytest.mark.parametrize("setting", [None, "", "false", "yes"])
def test_with_rotation_off_a_due_key_is_reported_not_rotated(
    posted: list, monkeypatch: pytest.MonkeyPatch, setting: str | None
) -> None:
    if setting is None:
        monkeypatch.delenv(worker.SWEEP_ROTATE_VAR, raising=False)
    else:
        monkeypatch.setenv(worker.SWEEP_ROTATE_VAR, setting)
    fake = FakeDoppler({"DEEJAY_COG_API_KEY": "old-d", "WATCHER_COG_API_KEY": "old-w"})
    api = FakeApi(
        [
            _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=95), retired=NOW),
            _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=120), retired=NOW),
        ]
    )
    steps = _run(fake, api, posted)
    assert fake.secrets["WATCHER_COG_API_KEY"] == "old-w"
    assert not [s for s in steps if s[0] in ("redeploy", "record")]


def test_with_rotation_off_retirement_still_happens(
    posted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(worker.SWEEP_ROTATE_VAR, raising=False)
    fake = FakeDoppler(
        {"DEEJAY_COG_API_KEY": "new-d", "DEEJAY_COG_API_KEY_PREVIOUS": "old-d"}
    )
    api = FakeApi(
        [_event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=2))],
        last=NOW - dt.timedelta(days=2),
    )
    _run(fake, api, posted)
    assert "DEEJAY_COG_API_KEY_PREVIOUS" not in fake.secrets


def test_a_consumer_not_deployed_in_a_config_is_not_checked_there(
    posted: list,
) -> None:
    """A key whose consumers run only in production is checked for readiness
    against the services its dev entry names, and raises no issue in dev."""
    inventory = {
        **INVENTORY,
        "secrets": {
            "DB_OPS_API_KEY": {
                "kind": "machine-key",
                "configs": ["prd", "dev"],
                "redeploy": {
                    "prd": ["api-kaianolevine-com", "db-poller"],
                    "dev": ["api-kaianolevine-com"],
                },
            },
        },
    }
    fake = FakeDoppler({"DB_OPS_API_KEY": "dev_old"})
    api = FakeApi(
        [
            _event(
                "DB_OPS_API_KEY",
                config="dev",
                age=dt.timedelta(days=120),
                retired=NOW,
            )
        ]
    )
    asked: list[set[str]] = []

    def check(cfg: str, needs: Any) -> list[str]:
        asked.append(set(needs))
        return [
            f"{s} is not in Railway development"
            for s in needs
            if s != "api-kaianolevine-com"
        ]

    _run(fake, api, posted, check, config="dev", inventory=inventory)
    assert asked == [{"api-kaianolevine-com"}]
    assert fake.secrets["DB_OPS_API_KEY"] != "dev_old"
    ((args, kwargs),) = posted
    assert "not ready to rotate" not in kwargs["text"]
    assert "db-poller" not in kwargs["text"]


def test_an_exempt_entry_is_never_due_and_never_drift(posted: list) -> None:
    """A not-rotated entry, never rotated and missing from Doppler, is neither
    rotated nor reported; the same name in Doppler is not unknown either."""
    inventory = {
        **INVENTORY,
        "secrets": {
            "DEEJAY_COG_API_KEY": INVENTORY["secrets"]["DEEJAY_COG_API_KEY"],
            "PUBLIC_SITE_KEY": {"kind": "not-rotated", "configs": ["prd", "dev"]},
            "PUBLIC_OTHER_KEY": {"kind": "not-rotated", "configs": ["prd", "dev"]},
        },
    }
    fake = FakeDoppler({"DEEJAY_COG_API_KEY": "d", "PUBLIC_OTHER_KEY": "public"})
    api = FakeApi([_event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW)])
    steps = _run(fake, api, posted, inventory=inventory)
    assert not [s for s in steps if s[0] in ("redeploy", "record")]
    assert fake.secrets["PUBLIC_OTHER_KEY"] == "public"
    ((args, kwargs),) = posted
    assert "PUBLIC_SITE_KEY" not in kwargs["text"]
    assert "PUBLIC_OTHER_KEY" not in kwargs["text"]
    assert "WARN" not in kwargs["text"] and "issue" not in kwargs["text"].lower()


# -- manual secrets ---------------------------------------------------------

MANUAL = {
    "cadence_days": {"manual": 365},
    "secrets": {
        "BREVO_API_KEY": {
            "kind": "manual",
            "steps": "Brevo > SMTP & API > API keys > Generate",
            "revoke": "Brevo > delete the old key",
            "can": "send email as your domains",
        },
        "DEEJAYTOOLS_CLERK_SECRET_KEY": {"kind": "manual", "configs": ["prd", "dev"]},
        "RAILWAY_TOKEN": {"kind": "manual", "configs": []},
        "TICK_SECRET": {"kind": "generated"},
    },
}


def _manual_event(name: str, *, age_days: int, verification: str = "unchecked"):
    return _event(
        name, age=dt.timedelta(days=age_days), actor=PERSON, verification=verification
    )


def test_a_manual_secret_is_due_when_never_rotated_past_cadence_or_failed() -> None:
    latest = {
        "BREVO_API_KEY": _manual_event("BREVO_API_KEY", age_days=400),
        "DEEJAYTOOLS_CLERK_SECRET_KEY": _manual_event(
            "DEEJAYTOOLS_CLERK_SECRET_KEY", age_days=5, verification="failed"
        ),
    }
    due = {d.name: d for d in sweep.manual_due(MANUAL, latest, "prd", now=NOW)}
    assert set(due) == {
        "BREVO_API_KEY",
        "DEEJAYTOOLS_CLERK_SECRET_KEY",
        "RAILWAY_TOKEN",
    }
    assert due["BREVO_API_KEY"].cycle == "after-2025-09-20"
    assert due["DEEJAYTOOLS_CLERK_SECRET_KEY"].cycle == "failed-2026-10-20"
    assert due["RAILWAY_TOKEN"].cycle == "never"


def test_a_manual_secret_inside_its_cadence_is_not_due() -> None:
    latest = {
        n: _manual_event(n, age_days=10)
        for n in ("BREVO_API_KEY", "DEEJAYTOOLS_CLERK_SECRET_KEY", "RAILWAY_TOKEN")
    }
    assert sweep.manual_due(MANUAL, latest, "prd", now=NOW) == []


def test_a_secret_held_outside_doppler_is_the_prd_sweeps() -> None:
    names = [d.name for d in sweep.manual_due(MANUAL, {}, "dev", now=NOW)]
    assert names == ["DEEJAYTOOLS_CLERK_SECRET_KEY"]


def test_the_task_says_how_to_rotate_and_carries_no_value() -> None:
    (due,) = [
        d
        for d in sweep.manual_due(MANUAL, {}, "prd", now=NOW)
        if d.name == "BREVO_API_KEY"
    ]
    request = sweep.task_for(due, MANUAL["secrets"]["BREVO_API_KEY"], "prd")
    assert request.source == "secret-rotation"
    assert request.external_id == "secret-rotation.prd.BREVO_API_KEY.never"
    draft = request.draft
    assert draft.title == "Rotate BREVO_API_KEY in prd"
    assert draft.labels == ["person"]
    assert draft.effort == "S" and draft.done_when and draft.constraints
    assert "uv run scripts/rotate.py rotate BREVO_API_KEY --reason scheduled`" in (
        draft.body or ""
    )
    assert "Brevo > SMTP & API > API keys > Generate" in (draft.body or "")
    assert "Brevo > delete the old key" in (draft.body or "")


def test_a_dev_task_names_the_config_in_its_command() -> None:
    (due,) = sweep.manual_due(MANUAL, {}, "dev", now=NOW)
    request = sweep.task_for(
        due, MANUAL["secrets"]["DEEJAYTOOLS_CLERK_SECRET_KEY"], "dev"
    )
    assert "--reason scheduled --config dev`" in (request.draft.body or "")


def test_sweeps_ask_for_one_task_per_due_secret_with_a_stable_key(
    posted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every day's sweep asks under the same key, and the task manager makes
    one task per key, so a due secret has one task until it is rotated."""
    monkeypatch.setenv(worker.SWEEP_TASKS_VAR, "true")
    fake = FakeDoppler({"DEEJAY_COG_API_KEY": "d", "WATCHER_COG_API_KEY": "w"})
    events = [
        _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
        _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
    ]
    first, second = FakeApi(list(events)), FakeApi(list(events))
    _run(fake, first, posted)
    _run(fake, second, posted)
    assert [r.external_id for r in first.asked] == [
        "secret-rotation.prd.OPENAI_API_KEY.never"
    ]
    assert [r.external_id for r in second.asked] == [r.external_id for r in first.asked]


def test_a_manual_secret_rotated_recently_asks_for_nothing(
    posted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(worker.SWEEP_TASKS_VAR, "true")
    fake = FakeDoppler({"DEEJAY_COG_API_KEY": "d", "WATCHER_COG_API_KEY": "w"})
    api = FakeApi(
        [
            _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
            _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
            _manual_event("OPENAI_API_KEY", age_days=30),
        ]
    )
    _run(fake, api, posted)
    assert api.asked == []


def test_with_tasks_off_a_due_secret_is_reported_not_asked_for(
    posted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(worker.SWEEP_TASKS_VAR, raising=False)
    fake = FakeDoppler({"DEEJAY_COG_API_KEY": "d", "WATCHER_COG_API_KEY": "w"})
    api = FakeApi(
        [
            _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
            _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
        ]
    )
    _run(fake, api, posted)
    assert api.asked == []
    ((args, kwargs),) = posted
    assert "OPENAI_API_KEY in prd" in kwargs["text"]
    assert worker.SWEEP_TASKS_VAR in kwargs["text"]


def test_a_refused_task_request_is_reported_and_the_sweep_goes_on(
    posted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(worker.SWEEP_TASKS_VAR, "true")
    fake = FakeDoppler(
        {"DEEJAY_COG_API_KEY": "d", "WATCHER_COG_API_KEY": "w", "NEW_TOKEN": "x"}
    )
    api = FakeApi(
        [
            _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
            _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
        ]
    )

    def refuse(request: Any) -> None:
        raise RuntimeError("the API answered 503")

    api.request_task = refuse  # type: ignore[method-assign]
    _run(fake, api, posted)
    ((args, kwargs),) = posted
    assert "rotation task not asked for" in kwargs["text"]
    assert "NEW_TOKEN" in kwargs["text"]  # drift still reported after it


# ── changes Doppler reported ─────────────────────────────────────────────

CHANGE_INVENTORY = {
    "secrets": {
        "DEEJAY_COG_API_KEY": {"kind": "machine-key"},
        "AWS_SECRET_ACCESS_KEY": {
            "kind": "aws-access-key",
            "id_var": "AWS_ACCESS_KEY_ID",
            "also": ["AWS_SECRET_ACCESS_KEY_MIRROR"],
        },
    }
}


def _change(
    name: str = "DEEJAY_COG_API_KEY",
    *,
    at: dt.datetime = NOW - dt.timedelta(hours=30),
    config: str = "prd",
    change: str = "updated",
) -> SecretChange:
    return SecretChange(
        name=name,
        config=config,  # type: ignore[arg-type]
        change=change,  # type: ignore[arg-type]
        changed_at=at,
        received_id="00000000-0000-4000-8000-0000000000aa",
    )


def _minted(name: str, at: dt.datetime) -> RotationEvent:
    return _event(name, age=NOW - at)


def test_the_sweep_judges_one_day_of_changes_a_day_old() -> None:
    since, until = sweep.change_window(NOW)
    assert until == NOW - dt.timedelta(hours=24)
    assert since == NOW - dt.timedelta(hours=48)


def test_a_change_with_its_rotation_is_explained() -> None:
    change = _change()
    events = [_minted("DEEJAY_COG_API_KEY", change.changed_at - dt.timedelta(hours=1))]
    assert sweep.unexplained([change], events, CHANGE_INVENTORY) == []


def test_a_change_just_before_its_rotation_is_minted_is_explained() -> None:
    """The two clocks differ: a few minutes either way still matches."""
    change = _change()
    events = [
        _minted("DEEJAY_COG_API_KEY", change.changed_at + dt.timedelta(minutes=3))
    ]
    assert sweep.unexplained([change], events, CHANGE_INVENTORY) == []


def test_a_change_long_after_its_last_rotation_is_not_explained() -> None:
    change = _change()
    events = [_minted("DEEJAY_COG_API_KEY", change.changed_at - dt.timedelta(hours=3))]
    assert sweep.unexplained([change], events, CHANGE_INVENTORY) == [change]


def test_a_rotation_of_another_secret_explains_nothing() -> None:
    change = _change()
    events = [_minted("AWS_SECRET_ACCESS_KEY", change.changed_at)]
    assert sweep.unexplained([change], events, CHANGE_INVENTORY) == [change]


@pytest.mark.parametrize(
    "name",
    [
        "DEEJAY_COG_API_KEY_PREVIOUS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY_MIRROR",
    ],
)
def test_names_written_with_a_secret_are_explained_over_its_handover(
    name: str,
) -> None:
    """The previous slot is deleted days after the rotation; a key id and its
    copies move with the secret."""
    owner = (
        "DEEJAY_COG_API_KEY" if name.startswith("DEEJAY") else "AWS_SECRET_ACCESS_KEY"
    )
    change = _change(name, change="removed")
    events = [_minted(owner, change.changed_at - dt.timedelta(days=6))]
    assert sweep.unexplained([change], events, CHANGE_INVENTORY) == []
    stale = [_minted(owner, change.changed_at - dt.timedelta(days=9))]
    assert sweep.unexplained([change], stale, CHANGE_INVENTORY) == [change]


def test_a_name_the_inventory_does_not_list_is_left_to_drift() -> None:
    assert sweep.unexplained([_change("SOME_SETTING")], [], CHANGE_INVENTORY) == []


def test_the_change_task_is_keyed_by_the_change_and_carries_no_value() -> None:
    change = _change(at=dt.datetime(2026, 10, 24, 8, 30, 15, tzinfo=dt.UTC))
    request = sweep.change_task_for(change, "dev")

    assert request.external_id == (
        "secret-rotation.dev.DEEJAY_COG_API_KEY.changed-20261024T083015Z"
    )
    assert request.draft.labels == ["person"]
    body = request.draft.body or ""
    assert "--reason recorded --config dev`" in body
    assert "revert it in Doppler" in body


def test_a_sweep_asks_for_a_task_per_unexplained_change(
    posted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(worker.SWEEP_TASKS_VAR, "true")
    fake = FakeDoppler({"DEEJAY_COG_API_KEY": "d", "WATCHER_COG_API_KEY": "w"})
    latest = [
        _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
        _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
        _manual_event("OPENAI_API_KEY", age_days=30),
    ]
    explained = _change("WATCHER_COG_API_KEY")
    stray = _change("DEEJAY_COG_API_KEY")
    api = FakeApi(
        latest,
        reported=[explained, stray],
        all_events=[
            *latest,
            _minted(
                "WATCHER_COG_API_KEY", explained.changed_at - dt.timedelta(minutes=20)
            ),
        ],
    )

    _run(fake, api, posted)

    assert [r.external_id for r in api.asked] == [
        sweep.change_task_for(stray, "prd").external_id
    ]
    ((config, since, until),) = api.change_reads
    assert (config, since, until) == ("prd", *sweep.change_window(NOW))
    ((args, kwargs),) = posted
    assert "changed outside a rotation" in kwargs["text"]
    assert "DEEJAY_COG_API_KEY in prd" in kwargs["text"]


def test_with_tasks_off_an_unexplained_change_is_only_reported(
    posted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(worker.SWEEP_TASKS_VAR, raising=False)
    fake = FakeDoppler({"DEEJAY_COG_API_KEY": "d", "WATCHER_COG_API_KEY": "w"})
    latest = [
        _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
        _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
    ]
    api = FakeApi(latest, reported=[_change("DEEJAY_COG_API_KEY")])
    _run(fake, api, posted)
    assert api.asked == []
    ((args, kwargs),) = posted
    assert "changed outside a rotation" in kwargs["text"]


def test_changes_that_cannot_be_read_are_reported_and_the_sweep_goes_on(
    posted: list,
) -> None:
    fake = FakeDoppler(
        {"DEEJAY_COG_API_KEY": "d", "WATCHER_COG_API_KEY": "w", "NEW_TOKEN": "x"}
    )
    api = FakeApi(
        [
            _event("DEEJAY_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
            _event("WATCHER_COG_API_KEY", age=dt.timedelta(days=5), retired=NOW),
        ]
    )

    def unavailable(config: str, **kwargs: Any) -> list[Any]:
        raise RuntimeError("the API answered 404")

    api.changes = unavailable  # type: ignore[method-assign]
    _run(fake, api, posted)
    ((args, kwargs),) = posted
    assert "secret changes not read" in kwargs["text"]
    assert "NEW_TOKEN" in kwargs["text"]
