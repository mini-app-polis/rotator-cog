"""An incident: every inventory secret invalidated, compromised, under one id.

The owner declares an incident with the API (``POST
/v1/secrets/rotation-incidents``), which mints its id and puts one
``rotation.incident`` on this cog's queue. The worker expands it here:

1. **The list.** Every inventory entry in every config it is held in, prd
   first, then dev; tier 0 to 3 within each, then by name. ``configs: []``
   (held outside Doppler) counts as prd. An entry with no tier goes last, as
   tier 3. An entry held in both configs is on the list once per config.
2. **Who rotates each.** The worker does (``worker``) when it would accept a
   job for it: a kind it rotates, no manual steps. Every other entry is a
   person's (``task``): manual kinds and entries with steps.
3. **Register, then act.** The list is registered with the API, which
   answers with what is already recorded for each entry, then walked in
   order: a compromised ``rotation.job`` carrying the incident id for each
   worker entry, sent to this cog's own queue in its one message group so
   tier order holds; one board task for each person's entry, marked with
   the API once the task manager accepts the ask. One failure never stops
   the entries after it.

**Repeating.** The same incident delivered again (or resumed by the owner)
re-runs this: an entry already rotated since the incident started gets no
second job, and an entry whose task was asked for gets no second ask. The
task's key ends in the incident id, so the task manager makes no second task
either.

No value is ever read or written here: names, configs and tiers only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.contract import (
    RotationIncident,
    RotationIncidentEntry,
    RotationIncidentRegistration,
    RotationIncidentTaskRequested,
    RotationRequest,
    TaskCreateRequest,
)

#: The configs an incident covers, in the order it covers them.
CONFIG_ORDER = ("prd", "dev")

#: The tier of an entry that names none: last.
UNTIERED = 3

Handling = Literal["worker", "task"]


@dataclass(frozen=True)
class Entry:
    """One secret in one config, as an incident walks it."""

    name: str
    config: str
    tier: int
    handling: Handling


class IncidentApi(Protocol):
    """The API calls an expansion makes: the API, or a fake in a test."""

    def register(
        self, incident_id: str, registration: RotationIncidentRegistration
    ) -> RotationIncident:
        """Register the incident's entries; the incident as it now stands."""
        ...

    def mark_task_requested(self, incident_id: str, name: str, config: str) -> None:
        """Record that the entry's task was asked for."""
        ...

    def request_task(self, request: TaskCreateRequest) -> None:
        """Ask the task manager for one task; asking again makes no second."""
        ...


class _Api:
    def __init__(self, machine_name: str) -> None:
        self._client = KaianoApiClient.from_env(machine_name)

    def register(
        self, incident_id: str, registration: RotationIncidentRegistration
    ) -> RotationIncident:
        return self._client.register_rotation_incident(incident_id, registration)

    def mark_task_requested(self, incident_id: str, name: str, config: str) -> None:
        self._client.mark_rotation_incident_task_requested(
            incident_id,
            RotationIncidentTaskRequested(name=name, config=config),  # type: ignore[arg-type]
        )

    def request_task(self, request: TaskCreateRequest) -> None:
        self._client.request_task(request)


def api(machine_name: str) -> IncidentApi:
    """The API an expansion calls, as ``machine_name``, with its own key."""
    return _Api(machine_name)


def held_in(entry: Mapping[str, Any]) -> list[str]:
    """The configs an incident covers ``entry`` in: ``[]`` and unset are prd."""
    configs = entry.get("configs")
    return list(configs) if configs else ["prd"]


def entries_for(
    inventory: Mapping[str, Any], worker_rotates: Callable[[str, str], bool]
) -> list[Entry]:
    """Every inventory entry in every config it is held in, in incident order.

    ``worker_rotates(name, config)`` says whether the worker would accept a
    job for it.
    """
    out: list[Entry] = []
    secrets: Mapping[str, Any] = inventory["secrets"]
    for config in CONFIG_ORDER:
        held = [
            (int(entry.get("tier", UNTIERED)), name)
            for name, entry in secrets.items()
            if config in held_in(entry)
        ]
        for tier, name in sorted(held):
            handling: Handling = "worker" if worker_rotates(name, config) else "task"
            out.append(Entry(name=name, config=config, tier=tier, handling=handling))
    return out


def registration(entries: list[Entry]) -> RotationIncidentRegistration:
    """The list as the API registers it."""
    return RotationIncidentRegistration(
        entries=[
            RotationIncidentEntry(
                name=e.name,
                config=e.config,  # type: ignore[arg-type]
                tier=e.tier,  # type: ignore[arg-type]
                handling=e.handling,
            )
            for e in entries
        ]
    )


def job_for(entry: Entry, incident_id: str) -> RotationRequest:
    """The compromised job the worker queues for one of its entries."""
    return RotationRequest(
        name=entry.name,
        config=entry.config,  # type: ignore[arg-type]
        mode="compromised",
        incident_id=incident_id,
    )


def why_task(entry: Mapping[str, Any]) -> str:
    """Why a person invalidates this entry rather than the worker."""
    kind = entry.get("kind")
    if entry.get("steps"):
        return "It needs a manual step, so rotator-cog cannot rotate it itself."
    return f"Its kind ({kind}) is rotated by a person, not by rotator-cog."
