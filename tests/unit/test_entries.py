"""Inventory entries resolved for one Doppler config."""

from __future__ import annotations

from rotator_cog import entries


def test_a_list_of_consumers_holds_in_every_config() -> None:
    entry = {"redeploy": ["api-kaianolevine-com", "db-poller"]}
    assert entries.redeploy_for(entry, "prd") == ["api-kaianolevine-com", "db-poller"]
    assert entries.redeploy_for(entry, "dev") == ["api-kaianolevine-com", "db-poller"]


def test_a_mapping_names_the_consumers_per_config() -> None:
    entry = {
        "redeploy": {
            "prd": ["api-kaianolevine-com", "db-poller"],
            "dev": ["api-kaianolevine-com"],
        }
    }
    assert entries.redeploy_for(entry, "prd") == ["api-kaianolevine-com", "db-poller"]
    assert entries.redeploy_for(entry, "dev") == ["api-kaianolevine-com"]


def test_a_config_the_mapping_leaves_out_has_no_consumers() -> None:
    entry = {"redeploy": {"prd": ["db-poller"]}}
    assert entries.redeploy_for(entry, "dev") == []
    assert entries.redeploy_for({}, "prd") == []


def test_for_config_resolves_redeploy_and_keeps_the_rest() -> None:
    entry = {"kind": "machine-key", "redeploy": {"dev": ["api-kaianolevine-com"]}}
    resolved = entries.for_config(entry, "dev")
    assert resolved == {"kind": "machine-key", "redeploy": ["api-kaianolevine-com"]}
    assert entry["redeploy"] == {"dev": ["api-kaianolevine-com"]}


def test_the_previous_slot_is_the_name_with_its_suffix() -> None:
    assert entries.previous_name("DEEJAY_COG_API_KEY") == "DEEJAY_COG_API_KEY_PREVIOUS"
