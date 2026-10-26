"""Inventory entries as one Doppler config sees them.

A handover keeps a key's outgoing value in NAME_PREVIOUS until it is
retired, so that slot belongs to the entry too.

An entry's ``redeploy`` lists the Railway services that read the secret at
boot. A list holds in every config the entry is held in. A mapping from
config to list says which services consume it in each config, for a
secret whose consumers are not deployed everywhere: a config the mapping
leaves out has no consumers there.

Standard library only, so ``scripts/rotate.py`` can import it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The slot the API also accepts a key from while callers move to the new one.
PREVIOUS_SUFFIX = "_PREVIOUS"


def previous_name(name: str) -> str:
    """The slot that holds ``name``'s outgoing value during a handover."""
    return f"{name}{PREVIOUS_SUFFIX}"


def redeploy_for(entry: Mapping[str, Any], config: str) -> list[str]:
    """The services that consume ``entry``'s secret in ``config``."""
    services = entry.get("redeploy") or []
    if isinstance(services, Mapping):
        return list(services.get(config) or [])
    return list(services)


def for_config(entry: Mapping[str, Any], config: str) -> dict[str, Any]:
    """A copy of ``entry`` whose ``redeploy`` is the list for ``config``."""
    return {**entry, "redeploy": redeploy_for(entry, config)}
