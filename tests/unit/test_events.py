"""The events rotate.py writes, and reading the latest back."""

from __future__ import annotations

import datetime as dt

from rotator_cog import events


def test_verification_is_ok_only_when_a_check_passed() -> None:
    assert events.verification(True) == {
        "verification": "ok",
        "verification_reason": None,
    }
    assert events.verification(False)["verification_reason"] == events.CHECK_FAILED
    assert (
        events.verification(False, recheck=True)["verification_reason"]
        == events.RECHECK_FAILED
    )
    assert events.verification(None) == {
        "verification": "failed",
        "verification_reason": events.NO_CHECK,
    }


def test_an_event_has_exactly_the_contracts_fields() -> None:
    e = events.event(
        name="TICK_SECRET",
        config="dev",
        kind="generated",
        mode="routine",
        actor="owner@example.com",
        minted="2026-10-25T10:00:00+00:00",
        retired=None,
        verified=True,
    )
    assert tuple(e) == events.CREATE_FIELDS
    assert e["aws_key_last4"] is None


def test_a_recheck_rewrites_the_stored_event_without_its_extras() -> None:
    stored = {
        "id": "00000000-0000-0000-0000-000000000000",
        "recorded_at": "2026-10-25T10:01:00Z",
        "name": "DEEJAY_COG_API_KEY",
        "config": "prd",
        "kind": "machine-key",
        "mode": "compromised",
        "actor": "owner@example.com",
        "minted": "2026-10-25T10:00:00Z",
        "retired": "2026-10-25T10:00:00Z",
        "verification": "failed",
        "verification_reason": events.CHECK_FAILED,
        "aws_key_last4": None,
        "added_by_a_newer_api": 1,
    }
    out = events.rechecked(stored, True)
    assert tuple(out) == events.CREATE_FIELDS
    assert out["minted"] == stored["minted"]  # the same key: an update
    assert (out["verification"], out["verification_reason"]) == ("ok", None)
    failed = events.rechecked(stored, False)
    assert failed["verification_reason"] == events.RECHECK_FAILED


def test_last_minted_is_the_newest_per_secret_and_config() -> None:
    got = events.last_minted(
        [
            {"name": "A", "config": "prd", "minted": "2026-10-25T10:00:00Z"},
            {"name": "A", "config": "prd", "minted": "2026-10-24T10:00:00+00:00"},
            {"name": "A", "config": "dev", "minted": "2026-10-25T07:00:00-05:00"},
        ]
    )
    assert got == {
        ("A", "prd"): "2026-10-25T10:00:00+00:00",
        ("A", "dev"): "2026-10-25T12:00:00+00:00",
    }


def test_now_is_utc_to_the_second() -> None:
    parsed = dt.datetime.fromisoformat(events.now())
    assert parsed.utcoffset() == dt.timedelta(0)
    assert parsed.microsecond == 0
