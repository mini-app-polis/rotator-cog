"""The inventory and the record are data rotate.py trusts; check their shape."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

import rotator_cog

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "secrets" / "inventory.yaml"
LEDGER = ROOT / "secrets" / "rotations.jsonl"

#: Field names that would mean a row carries a value rather than a fact
#: about one.
_VALUE_FIELDS = {"value", "secret", "password", "token", "key"}


def _inventory() -> dict:
    return yaml.safe_load(INVENTORY.read_text())


def _rows() -> list[dict]:
    return [
        json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()
    ]


def test_the_package_has_a_version() -> None:
    assert isinstance(rotator_cog.__version__, str)


def test_every_rotated_kind_has_a_cadence() -> None:
    inv = _inventory()
    cadences = inv["cadence_days"]
    for name, entry in inv["secrets"].items():
        kind = entry["kind"]
        if kind == "not-rotated":
            continue
        assert kind in cadences, f"{name}: kind {kind} has no cadence_days entry"
        assert cadences[kind] > 0


def test_every_secret_has_a_tier() -> None:
    for name, entry in _inventory()["secrets"].items():
        if entry["kind"] != "not-rotated":
            assert isinstance(entry.get("tier"), int), f"{name} has no tier"


def test_the_record_holds_no_values() -> None:
    for i, row in enumerate(_rows(), start=1):
        held = _VALUE_FIELDS & set(row)
        assert not held, f"rotations.jsonl line {i} has {sorted(held)}"
        assert row["name"] and row["at"] and row["config"]
