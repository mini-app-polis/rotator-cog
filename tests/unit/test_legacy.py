"""The ledger import: what it sends, what it leaves out, and that it is safe to repeat."""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from rotator_cog import events, legacy

ROOT = Path(__file__).resolve().parents[2]

INVENTORY = {
    "secrets": {
        "DEEJAY_COG_API_KEY": {"kind": "machine-key"},
        "OPENAI_API_KEY": {"kind": "manual"},
        "DB_BACKUP_AWS_SECRET_ACCESS_KEY": {"kind": "aws-access-key"},
        "TICK_SECRET": {"kind": "generated"},
        "POSTGRES_KAIANOLEVINE_PASSWORD": {"kind": "manual", "configs": []},
        "SPOTIFY_CLIENT_ID": {"kind": "not-rotated"},
    }
}

ME = "owner@example.com"
#: Stands in for a value a malformed row might carry. It must never be sent
#: or printed.
PLANTED = "planted-value-must-not-leak"


def row(name: str, at: str, **fields: Any) -> dict[str, Any]:
    out = {
        "at": f"2026-10-22T{at}+00:00",
        "by": ME,
        "config": "prd",
        "kind": INVENTORY["secrets"].get(name, {}).get("kind", "manual"),
        "mode": "compromised",
        "name": name,
        "reason": "incident",
        "revoked": True,
        "verified": None,
    }
    out.update(fields)
    return out


def check(at: str, name: str, ok: bool, config: str = "prd") -> dict[str, Any]:
    return {
        "at": f"2026-10-22T{at}+00:00",
        "by": ME,
        "config": config,
        "kind": "machine-key",
        "mode": "verify",
        "name": name,
        "verified": ok,
    }


# The contract's RotationEventCreate (miniapppolis-common-utils), as rules a
# test can check without the package: its fields, sets and patterns, and its
# consistency rules.
_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ACTOR = re.compile(
    r"^(?:[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})$"
)


def assert_conforms(e: dict[str, Any]) -> None:
    assert set(e) == set(events.CREATE_FIELDS)
    assert _NAME.match(e["name"]) and len(e["name"]) <= 128
    assert e["config"] in ("prd", "dev")
    assert e["kind"] in legacy.KINDS
    assert e["mode"] in ("routine", "compromised")
    assert _ACTOR.match(e["actor"])
    minted = dt.datetime.fromisoformat(e["minted"])
    assert e["verification"] in ("ok", "failed")
    if e["verification"] == "ok":
        assert e["verification_reason"] is None
    if e["verification_reason"] is not None:
        assert len(e["verification_reason"]) <= 500
    if e["aws_key_last4"] is not None:
        assert e["kind"] == "aws-access-key"
        assert re.match(r"^[A-Z0-9]{4}$", e["aws_key_last4"])
    if e["retired"] is not None:
        assert dt.datetime.fromisoformat(e["retired"]) >= minted


class FakeApi:
    """The record's two endpoints over a dict keyed as the API keys events."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.writes = 0

    def record(self, event: dict[str, Any]) -> dict[str, Any]:
        assert_conforms(event)
        self.writes += 1
        key = (event["name"], event["config"], event["minted"])
        self.store[key] = dict(event)
        return {**event, "id": "x", "recorded_at": event["minted"]}

    def list(
        self,
        *,
        name: str | None = None,
        config: str | None = None,
        latest: bool = False,
    ) -> list[dict[str, Any]]:
        found = sorted(
            (
                e
                for e in self.store.values()
                if (name is None or e["name"] == name)
                and (config is None or e["config"] == config)
            ),
            key=lambda e: e["minted"],
            reverse=True,
        )
        if latest:
            seen: set[tuple[str, str]] = set()
            newest = []
            for e in found:
                if (e["name"], e["config"]) not in seen:
                    seen.add((e["name"], e["config"]))
                    newest.append(e)
            found = newest
        return found


def by_name(p: legacy.Plan) -> dict[tuple[str, str], dict[str, Any]]:
    return {(e["name"], e["config"]): e for e in p.events}


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


def test_only_the_latest_rotation_per_secret_and_config_is_sent() -> None:
    p = legacy.plan(
        [
            row("TICK_SECRET", "10:00:00"),
            row("TICK_SECRET", "12:00:00", mode="routine", verified=True),
            row("TICK_SECRET", "11:00:00"),
            row("TICK_SECRET", "09:00:00", config="dev"),
        ],
        INVENTORY,
    )
    got = by_name(p)
    assert set(got) == {("TICK_SECRET", "prd"), ("TICK_SECRET", "dev")}
    assert got["TICK_SECRET", "prd"]["minted"] == "2026-10-22T12:00:00+00:00"
    assert got["TICK_SECRET", "prd"]["mode"] == "routine"
    assert p.skipped == {legacy.SKIP_SUPERSEDED: 2}


def test_rows_without_a_doppler_config_are_skipped() -> None:
    p = legacy.plan(
        [row("POSTGRES_KAIANOLEVINE_PASSWORD", "10:00:00", config="-")], INVENTORY
    )
    assert p.events == []
    assert p.skipped == {legacy.SKIP_CONFIG: 1}


def test_postgres_role_rows_and_unknown_names_are_skipped() -> None:
    p = legacy.plan(
        [
            row(
                "DB_BACKUP_DATABASE_URL_KAIANOLEVINE", "10:00:00", kind="postgres-role"
            ),
            row("CLERK_SECRET_KEY", "10:00:00"),
            row("SPOTIFY_CLIENT_ID", "10:00:00"),
            row("OPENAI_API_KEY", "10:00:00", mode="something-else"),
        ],
        INVENTORY,
    )
    assert p.events == []
    assert p.skipped == {
        legacy.SKIP_POSTGRES_ROLE: 1,
        legacy.SKIP_NOT_IN_INVENTORY: 1,
        legacy.SKIP_NOT_ROTATED: 1,
        legacy.SKIP_MODE: 1,
    }


def test_the_kind_is_the_inventorys() -> None:
    p = legacy.plan([row("TICK_SECRET", "10:00:00", kind="manual")], INVENTORY)
    assert p.events[0]["kind"] == "generated"


def test_a_later_passing_recheck_makes_the_rotation_ok() -> None:
    p = legacy.plan(
        [
            row("DEEJAY_COG_API_KEY", "10:00:00", verified=False),
            check("10:05:00", "DEEJAY_COG_API_KEY", False),
            check("10:10:00", "DEEJAY_COG_API_KEY", True),
        ],
        INVENTORY,
    )
    e = p.events[0]
    assert (e["verification"], e["verification_reason"]) == ("ok", None)
    assert p.folded == 2
    assert p.skipped == {}


def test_a_later_failing_recheck_makes_the_rotation_failed() -> None:
    p = legacy.plan(
        [
            row("DEEJAY_COG_API_KEY", "10:00:00", verified=True),
            check("10:05:00", "DEEJAY_COG_API_KEY", False),
        ],
        INVENTORY,
    )
    e = p.events[0]
    assert e["verification"] == "failed"
    assert e["verification_reason"] == events.RECHECK_FAILED


def test_a_recheck_before_the_latest_rotation_does_not_count() -> None:
    p = legacy.plan(
        [
            row("DEEJAY_COG_API_KEY", "09:00:00", verified=False),
            check("09:30:00", "DEEJAY_COG_API_KEY", True),
            row("DEEJAY_COG_API_KEY", "10:00:00", verified=False),
            check("10:00:00", "DEEJAY_COG_API_KEY", True, config="dev"),
        ],
        INVENTORY,
    )
    e = by_name(p)["DEEJAY_COG_API_KEY", "prd"]
    assert e["verification"] == "failed"
    assert e["verification_reason"] == events.CHECK_FAILED
    assert p.folded == 0
    assert p.skipped == {
        legacy.SKIP_SUPERSEDED: 1,
        legacy.SKIP_EARLY_VERIFY: 1,
        legacy.SKIP_ORPHAN_VERIFY: 1,
    }


def test_a_rotation_nothing_checked_is_failed_saying_so() -> None:
    p = legacy.plan([row("OPENAI_API_KEY", "10:00:00", verified=None)], INVENTORY)
    e = p.events[0]
    assert e["verification"] == "failed"
    assert e["verification_reason"] == events.NO_CHECK


def test_fields_map_onto_the_contract() -> None:
    p = legacy.plan(
        [
            row(
                "DB_BACKUP_AWS_SECRET_ACCESS_KEY",
                "10:00:00",
                mode="routine",
                verified=True,
                aws_key_id_last4="AB12",
                via="dashboard",
                note=PLANTED,
            ),
            row("OPENAI_API_KEY", "10:00:00", revoked=False, aws_key_id_last4="CD34"),
        ],
        INVENTORY,
    )
    aws = by_name(p)["DB_BACKUP_AWS_SECRET_ACCESS_KEY", "prd"]
    assert aws == {
        "name": "DB_BACKUP_AWS_SECRET_ACCESS_KEY",
        "config": "prd",
        "kind": "aws-access-key",
        "mode": "routine",
        "actor": ME,
        "minted": "2026-10-22T10:00:00+00:00",
        "retired": "2026-10-22T10:00:00+00:00",
        "verification": "ok",
        "verification_reason": None,
        "aws_key_last4": "AB12",
    }
    manual = by_name(p)["OPENAI_API_KEY", "prd"]
    assert manual["retired"] is None
    assert manual["aws_key_last4"] is None
    for e in p.events:
        assert_conforms(e)


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

ROWS = [
    row("DEEJAY_COG_API_KEY", "10:00:00", verified=False),
    check("10:05:00", "DEEJAY_COG_API_KEY", True),
    row("OPENAI_API_KEY", "10:00:00", reason=PLANTED),
    row("POSTGRES_KAIANOLEVINE_PASSWORD", "10:00:00", config="-"),
]


def test_a_dry_run_prints_what_it_would_send_and_sends_nothing() -> None:
    lines: list[str] = []
    assert legacy.run(ROWS, INVENTORY, None, dry_run=True, out=lines.append) == 0
    text = "\n".join(lines)
    assert "DEEJAY_COG_API_KEY" in text and "OPENAI_API_KEY" in text
    assert "2026-10-22T10:00:00+00:00" in text
    assert lines[-1] == "would send 2 event(s)"
    assert f"skipped 1 row(s): {legacy.SKIP_CONFIG}" in lines
    # Nothing but names, configs, kinds and times: no reason, no actor, and
    # never anything a row carried beyond the contract.
    assert PLANTED not in text
    assert ME not in text


def test_a_real_run_sends_then_reads_the_latest_back() -> None:
    api = FakeApi()
    lines: list[str] = []
    assert legacy.run(ROWS, INVENTORY, api, dry_run=False, out=lines.append) == 0
    assert api.writes == 2
    assert len(api.store) == 2
    assert lines[-1] == "the API's latest events: 2; 2 of the 2 imported are there"
    for e in api.store.values():
        assert PLANTED not in json.dumps(e)


def test_running_it_again_adds_nothing() -> None:
    api = FakeApi()
    legacy.run(ROWS, INVENTORY, api, dry_run=False, out=lambda _: None)
    first = dict(api.store)
    assert legacy.run(ROWS, INVENTORY, api, dry_run=False, out=lambda _: None) == 0
    assert api.store == first


def test_a_newer_rotation_in_the_api_counts_as_there() -> None:
    api = FakeApi()
    api.record(
        events.event(
            name="OPENAI_API_KEY",
            config="prd",
            kind="manual",
            mode="routine",
            actor=ME,
            minted="2026-10-24T08:00:00+00:00",
            retired=None,
            verified=True,
        )
    )
    lines: list[str] = []
    assert legacy.run(ROWS, INVENTORY, api, dry_run=False, out=lines.append) == 0
    assert lines[-1].endswith("2 of the 2 imported are there (1 rotated again since)")


def test_an_event_missing_afterwards_fails_the_run() -> None:
    class Forgetful(FakeApi):
        def record(self, event: dict[str, Any]) -> dict[str, Any]:
            return event

    lines: list[str] = []
    assert (
        legacy.run(ROWS, INVENTORY, Forgetful(), dry_run=False, out=lines.append) == 1
    )
    assert "missing from the API: DEEJAY_COG_API_KEY prd" in lines


def test_a_real_run_needs_the_api() -> None:
    with pytest.raises(ValueError):
        legacy.run(ROWS, INVENTORY, None, dry_run=False, out=lambda _: None)


def test_the_ledger_in_this_repository_imports_cleanly() -> None:
    rows = [
        json.loads(line)
        for line in (ROOT / "secrets" / "rotations.jsonl").read_text().splitlines()
        if line.strip()
    ]
    inventory = yaml.safe_load((ROOT / "secrets" / "inventory.yaml").read_text())
    p = legacy.plan(rows, inventory)
    assert p.events
    for e in p.events:
        assert_conforms(e)
    keys = [(e["name"], e["config"], e["minted"]) for e in p.events]
    assert len(keys) == len(set(keys))
    # Every row is accounted for: sent, skipped with a reason, or folded.
    assert len(p.events) + sum(p.skipped.values()) + p.folded == len(rows)


def test_rotate_py_runs_the_import_as_a_dry_run() -> None:
    """The script finds the package beside it and sends nothing on a dry run."""
    done = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "rotate.py"),
            "import-ledger",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1].startswith("would send ")
