"""The daily sweep of one Doppler config: retire, rotate what is due, check drift.

Infra's schedule sends one ``rotation.sweep`` per config each day. A sweep:

1. **Retires previous keys.** A routine rotation leaves the old key in
   NAME_PREVIOUS so callers can move over. The worker's own rotations are
   retired once nothing has used the old key for a day: in prd, from the
   API's audit trail (``GET /v1/secrets/key-use``); in dev, a day after the
   rotation, since the rotator holds no key for the development API. At
   seven days a key still in use is retired anyway, and the run reports
   which machine was still on it. Retiring deletes the previous slots in
   Doppler, redeploys the API once Railway shows them gone, and records
   ``retired`` on each rotation event. With a daily sweep, a key is retired
   24 to 48 hours after its last use.
2. **Rotates one due key**, the most overdue, inline, and only on a day that
   retired nothing, so a run fits the function's timeout; a backlog clears a
   key a day per config. A key is due once its cadence has passed since its
   latest rotation, or when it has never been rotated. A key whose previous
   key is still live is not rotated again: a second handover would cut its
   callers off. A latest rotation that failed is reported, not retried: it
   needs a person.
3. **Reports drift**: names in Doppler that look secret and have no
   inventory entry, and inventory entries the config should hold but does
   not, as ``scripts/rotate.py check`` does.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.contract import (
    RotationEvent,
    RotationEventCreate,
    RotationRequest,
)

from . import doppler as doppler_mod

#: A previous key is retired once nothing has used it for this long.
QUIET_BEFORE_RETIRING = timedelta(hours=24)
#: And retired anyway, with an alert, once its rotation is this old.
FORCE_RETIRE_AFTER = timedelta(days=7)

#: Doppler names that look like credentials. Settings that do not are not
#: drift: the inventory lists credentials only.
SECRETISH = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "DSN",
    "WEBHOOK",
    "CREDENTIALS",
    "DATABASE_URL",
    "HEALTHCHECKS_URL",
)

#: Names Doppler adds to every config.
DOPPLER_OWN = frozenset({"DOPPLER_PROJECT", "DOPPLER_CONFIG", "DOPPLER_ENVIRONMENT"})


class SweepApi(Protocol):
    """The API reads a sweep makes: the API, or a fake in a test."""

    def latest(self, config: str) -> list[RotationEvent]:
        """The newest rotation event per secret in ``config``."""
        ...

    def last_previous_use(self, machine: str) -> datetime | None:
        """When ``machine`` last proved itself with its previous key."""
        ...


class _Api:
    def __init__(self, machine_name: str) -> None:
        self._client = KaianoApiClient.from_env(machine_name)

    def latest(self, config: str) -> list[RotationEvent]:
        return self._client.list_rotation_events(config=config, latest=True)  # type: ignore[arg-type]

    def last_previous_use(self, machine: str) -> datetime | None:
        data = self._client.get("/v1/secrets/key-use", {"machine": machine})["data"]
        last = data.get("last_used")
        return datetime.fromisoformat(last) if last else None


def api(machine_name: str) -> SweepApi:
    """The API a sweep reads, as ``machine_name``, with its own key."""
    return _Api(machine_name)


@dataclass(frozen=True)
class Retirement:
    """One previous key to retire, and why now."""

    event: RotationEvent
    machine: str
    forced: bool
    last_used: datetime | None


def retirements(
    events: Iterable[RotationEvent],
    config: str,
    *,
    actor: str,
    machine_of: Callable[[str], str],
    last_use: Callable[[str], datetime | None],
    now: datetime,
) -> list[Retirement]:
    """The worker's routine rotations whose previous key is ready to go."""
    found: list[Retirement] = []
    for event in events:
        if (
            event.actor != actor
            or event.mode != "routine"
            or event.verification != "ok"
            or event.retired is not None
        ):
            continue
        machine = machine_of(event.name)
        last = last_use(machine) if config == "prd" else None
        quiet_since = max(event.minted, last) if last else event.minted
        forced = now - event.minted >= FORCE_RETIRE_AFTER
        if forced or now - quiet_since >= QUIET_BEFORE_RETIRING:
            # Forced only when the key was in fact still in use.
            still_used = forced and now - quiet_since < QUIET_BEFORE_RETIRING
            found.append(Retirement(event, machine, still_used, last))
    return found


def due(
    names: Iterable[str],
    latest: Mapping[str, RotationEvent],
    cadence_days: Mapping[str, int],
    *,
    actor: str,
    now: datetime,
) -> tuple[list[str], list[str]]:
    """(Due names, most overdue first; names whose latest rotation failed)."""
    ready: list[tuple[datetime, str]] = []
    failed: list[str] = []
    for name in names:
        last = latest.get(name)
        if last is None:
            ready.append((datetime.min.replace(tzinfo=now.tzinfo), name))
            continue
        if last.verification != "ok":
            failed.append(name)
            continue
        if last.actor == actor and last.mode == "routine" and last.retired is None:
            continue
        if now - last.minted >= timedelta(days=cadence_days[name]):
            ready.append((last.minted, name))
    return [name for _, name in sorted(ready)], sorted(failed)


def drift(
    names: set[str], inventory: Mapping[str, Any], config: str
) -> tuple[list[str], list[str]]:
    """(Secret-looking names with no inventory entry; entries not in Doppler)."""
    secrets: Mapping[str, Any] = inventory["secrets"]
    known = set(secrets)
    known |= {e["id_var"] for e in secrets.values() if e.get("id_var")}
    known |= {a for e in secrets.values() for a in e.get("also", [])}
    known |= {doppler_mod.previous_name(n) for n in secrets}
    unknown = sorted(
        n for n in names - known - DOPPLER_OWN if any(part in n for part in SECRETISH)
    )
    missing = sorted(
        n
        for n, e in secrets.items()
        if e.get("kind") != "not-rotated"
        and config in (e.get("configs") if e.get("configs") is not None else ["prd"])
        and n not in names
    )
    return unknown, missing


def retired_event(event: RotationEvent, at: datetime) -> RotationEventCreate:
    """The same event (same key), now with its old value retired."""
    fields = RotationEventCreate.model_fields
    kept = {k: v for k, v in event.model_dump().items() if k in fields}
    return RotationEventCreate(**{**kept, "retired": at})


def request_for(name: str, config: str) -> RotationRequest:
    """The routine rotation job a sweep runs for ``name`` in ``config``."""
    return RotationRequest(name=name, config=config, mode="routine")  # type: ignore[arg-type]
