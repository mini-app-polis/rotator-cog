"""The inventory is data rotate.py trusts; check its shape."""

from __future__ import annotations

from pathlib import Path

import yaml

import rotator_cog

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "secrets" / "inventory.yaml"


def _inventory() -> dict:
    return yaml.safe_load(INVENTORY.read_text())


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
