"""The daily sweep of one Doppler config: retire, rotate what is due, check drift.

Infra's schedule sends one ``rotation.sweep`` per config each day. A sweep:

1. **Retires previous keys.** A routine rotation leaves the old key in
   NAME_PREVIOUS so callers can move over. The worker's own rotations are
   retired once nothing has used the old key for a day: in prd, from the
   API's audit trail (``GET /v1/secrets/key-use``); in dev, a day after the
   rotation, since the rotator holds no key for the development API. At
   seven days a key still in use is retired anyway, and the run reports
   which machine was still on it. Retiring empties the previous slots in
   Doppler (ADR-0005), redeploys the API once Railway shows them gone, and records
   ``retired`` on each rotation event. With a daily sweep, a key is retired
   24 to 48 hours after its last use.
2. **Rotates one due key**, the most overdue, inline, and only on a day that
   retired nothing, so a run fits the function's timeout; a backlog clears a
   key a day per config. A key whose services are not ready for it (one not
   in the config's Railway environment, or a variable not referenced) is
   reported and passed over for the next most overdue. A key is due once its cadence has passed since its
   latest rotation, or when it has never been rotated. A key whose previous
   key is still live is not rotated again: a second handover would cut its
   callers off. A latest rotation that failed is reported, not retried: it
   needs a person.
3. **Asks for a task per due manual secret.** A secret minted in a
   provider's console cannot be rotated here, so when one is due (never
   rotated, past its cadence, or its last check failed) the sweep asks the
   task manager for one task telling a person how: the rotate.py command,
   the steps and the revoke, never a value. The task's key names the
   rotation it follows, so a daily sweep asks for the same task until the
   secret is rotated. A secret held outside Doppler (``configs: []``) is
   asked for by the prd sweep. One run asks for at most the task limit
   (``ROTATOR_SWEEP_TASK_LIMIT``, default 3): the most overdue first (never
   rotated, then the oldest latest rotation), then the lowest tier, then by
   name. The rest are reported as due but not yet asked. Asking again for a
   task that exists creates nothing, so at most that many are open at once,
   and the next are asked for as those are rotated.

   Each manual secret it looks after that has a latest rotation is also
   sent to its task as an update, every day: under the key prefix
   ``secret-rotation.<config>.<name>.``, to Review, with the comment
   ``Rotated <ISO timestamp>; verification <ok|failed|unchecked>`` and, for
   a failed rotation, a second line naming the task that follows it. The
   task manager acts only on an open task created before the rotation, so
   a repeat changes nothing; an update that is not sent is reported and
   sent again by the next sweep.
4. **Reports drift**: names in Doppler that look secret and have no
   inventory entry, and inventory entries the config should hold but does
   not, as ``scripts/rotate.py check`` does.
5. **Judges the secret changes Doppler reported** (its secrets.update
   webhook, read from ``GET /v1/secrets/changes``) once they are a day old,
   so each is judged once and a rotation recorded late still counts. A
   change to an inventory secret is explained by a rotation of it minted
   from two hours before the change to five minutes after; a change to its
   ``_PREVIOUS`` slot, or to a name written with it (its key id, its
   copies), by its rotation in the eight days before. A change nothing
   explains was made outside the rotation process: it is reported, and one
   task asks a person to record it with rotate.py or revert it. Names only;
   no value is ever read.
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
    SecretChange,
    TaskCreateRequest,
    TaskDraft,
    TaskUpdateRequest,
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

#: A change is judged once it is this old, so a rotation recorded after its
#: check and redeploy still explains it; the daily sweep reads one day of them.
CHANGE_JUDGED_AFTER = timedelta(hours=24)
CHANGE_READ_SPAN = timedelta(hours=24)
#: A rotation explains a change to its secret minted this long before it...
EXPLAINED_BEFORE = timedelta(hours=2)
#: ...or this long after (the two clocks are not the same clock).
EXPLAINED_AFTER = timedelta(minutes=5)
#: A rotation explains changes to its previous slot and the names written
#: with it over the whole handover: a previous key is retired within seven
#: days, then deleted.
HANDOVER_EXPLAINS = timedelta(days=8)

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

    def request_task(self, request: TaskCreateRequest) -> None:
        """Ask the task manager for one task; asking again makes no second."""
        ...

    def update_task(self, request: TaskUpdateRequest) -> None:
        """Send the task manager an update to a task it created."""
        ...

    def events(self, config: str) -> list[RotationEvent]:
        """The rotation events in ``config``, newest minted first."""
        ...

    def changes(
        self, config: str, *, since: datetime, until: datetime
    ) -> list[SecretChange]:
        """The secret names Doppler reported changed in ``config``."""
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

    def request_task(self, request: TaskCreateRequest) -> None:
        self._client.request_task(request)

    def update_task(self, request: TaskUpdateRequest) -> None:
        self._client.post(
            "/v1/tasks/updates", request.model_dump(mode="json", exclude_unset=True)
        )

    def events(self, config: str) -> list[RotationEvent]:
        return self._client.list_rotation_events(config=config)  # type: ignore[arg-type]

    def changes(
        self, config: str, *, since: datetime, until: datetime
    ) -> list[SecretChange]:
        return self._client.list_secret_changes(
            config=config,  # type: ignore[arg-type]
            since=since,
            until=until,
        )


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
    """(Due names, most overdue first; names whose latest rotation failed).

    A latest rotation that is ``unchecked`` counts as rotated, as ``ok`` does.
    """
    ready: list[tuple[datetime, str]] = []
    failed: list[str] = []
    for name in names:
        last = latest.get(name)
        if last is None:
            ready.append((datetime.min.replace(tzinfo=now.tzinfo), name))
            continue
        if last.verification == "failed":
            failed.append(name)
            continue
        if last.actor == actor and last.mode == "routine" and last.retired is None:
            continue
        if now - last.minted >= timedelta(days=cadence_days[name]):
            ready.append((last.minted, name))
    return [name for _, name in sorted(ready)], sorted(failed)


@dataclass(frozen=True)
class ManualDue:
    """A manual secret that is due, and which rotation it follows."""

    name: str
    #: Names the rotation this one follows, so the task's key stays the same
    #: until the secret is rotated: ``never``, ``after-<date>`` or
    #: ``failed-<date>``.
    cycle: str
    why: str
    #: When its latest rotation was minted; None when it never was.
    last: datetime | None = None
    #: Its inventory tier; an entry that names none counts as the last tier.
    tier: int = 3


#: How many manual-rotation tasks one sweep asks for when the setting is
#: unset or not a whole number of at least 1.
TASK_LIMIT_DEFAULT = 3


def task_limit(raw: str | None) -> int:
    """How many manual-rotation tasks one sweep asks for, from the setting."""
    try:
        limit = int((raw or "").strip())
    except ValueError:
        return TASK_LIMIT_DEFAULT
    return limit if limit >= 1 else TASK_LIMIT_DEFAULT


def most_overdue_first(due: Iterable[ManualDue]) -> list[ManualDue]:
    """``due`` in the order the sweep asks for tasks.

    Never rotated first, then the oldest latest rotation, then the lowest
    tier, then by name.
    """

    def order(d: ManualDue) -> tuple[bool, float, int, str]:
        minted = d.last.timestamp() if d.last is not None else 0.0
        return (d.last is not None, minted, d.tier, d.name)

    return sorted(due, key=order)


def held_for_sweep(entry: Mapping[str, Any], config: str) -> bool:
    """Whether ``config``'s sweep looks after ``entry``.

    An entry held outside Doppler (``configs: []``) is recorded under prd,
    so the prd sweep looks after it.
    """
    configs = entry.get("configs")
    if configs is None:
        return config == "prd"
    return config in configs or (not configs and config == "prd")


def manual_due(
    inventory: Mapping[str, Any],
    latest: Mapping[str, RotationEvent],
    config: str,
    *,
    now: datetime,
) -> list[ManualDue]:
    """The manual secrets ``config``'s sweep should ask a person to rotate."""
    cadence = timedelta(days=int(inventory["cadence_days"]["manual"]))
    out: list[ManualDue] = []
    for name, entry in sorted(inventory["secrets"].items()):
        if entry.get("kind") != "manual" or not held_for_sweep(entry, config):
            continue
        last = latest.get(name)
        tier = int(entry.get("tier", 3))
        if last is None:
            out.append(
                ManualDue(
                    name,
                    "never",
                    "It has never been recorded as rotated.",
                    tier=tier,
                )
            )
        elif last.verification == "failed":
            day = last.minted.date().isoformat()
            out.append(
                ManualDue(
                    name,
                    f"failed-{day}",
                    f"Its rotation of {day} was recorded as failed.",
                    last=last.minted,
                    tier=tier,
                )
            )
        elif now - last.minted >= cadence:
            day = last.minted.date().isoformat()
            out.append(
                ManualDue(
                    name,
                    f"after-{day}",
                    f"It was last rotated on {day}, and is due every "
                    f"{cadence.days} days.",
                    last=last.minted,
                    tier=tier,
                )
            )
    return out


def task_for(
    due: ManualDue, entry: Mapping[str, Any], config: str
) -> TaskCreateRequest:
    """The task asking a person to rotate ``due``: how, never a value."""
    command = f"uv run scripts/rotate.py rotate {due.name} --reason scheduled"
    if config != "prd":
        command += f" --config {config}"
    lines = [
        f"{due.why} It is minted in a provider's console, so rotator-cog "
        "cannot rotate it itself.",
        "",
        f"Run from a rotator-cog checkout: `{command}`",
        "It prints the steps, takes the new value at a hidden prompt, writes "
        "it where the inventory says, and records the rotation.",
        "",
        f"Steps: {entry.get('steps', 'see the provider console')}",
        f"Revoke: {entry.get('revoke', 'revoke the old value at the provider')}",
    ]
    if entry.get("can"):
        lines.append(f"What it can do: {entry['can']}")
    return TaskCreateRequest(
        source="secret-rotation",
        external_id=f"secret-rotation.{config}.{due.name}.{due.cycle}",
        draft=TaskDraft(
            title=f"Rotate {due.name} in {config}",
            done_when=(
                f"`{command}` has recorded a rotation of {due.name} in "
                f"{config}, and the old value is revoked."
            ),
            constraints=(
                "The new value goes in only at rotate.py's hidden prompt: "
                "never into a chat, a ticket or a file."
            ),
            effort="S",
            body="\n".join(lines),
            labels=["person"],
        ),
    )


#: The column a rotated secret's task moves to.
ROTATED_SECTION = "Review"


def update_for(event: RotationEvent, config: str) -> TaskUpdateRequest:
    """The update telling a secret's task that ``event`` rotated it.

    The key is a prefix naming the secret, not one cycle: the task manager
    finds the secret's open task under it. The comment holds the rotation's
    time and verification and, for a failed rotation, the key of the task
    that follows it; never a value.
    """
    prefix = f"secret-rotation.{config}.{event.name}."
    lines = [f"Rotated {event.minted.isoformat()}; verification {event.verification}"]
    if event.verification == "failed":
        day = event.minted.date().isoformat()
        lines.append(f"The rotation failed; {prefix}failed-{day} follows it.")
    return TaskUpdateRequest(
        external_id=prefix, comment="\n".join(lines), section=ROTATED_SECTION
    )


def rotation_updates(
    inventory: Mapping[str, Any],
    latest: Mapping[str, RotationEvent],
    config: str,
) -> list[tuple[str, TaskUpdateRequest]]:
    """(Name, update) for each manual secret ``config``'s sweep looks after
    that has a latest rotation, by name."""
    out: list[tuple[str, TaskUpdateRequest]] = []
    for name, entry in sorted(inventory["secrets"].items()):
        if entry.get("kind") != "manual" or not held_for_sweep(entry, config):
            continue
        last = latest.get(name)
        if last is not None:
            out.append((name, update_for(last, config)))
    return out


def incident_task_for(
    name: str,
    entry: Mapping[str, Any],
    config: str,
    incident_id: str,
    *,
    why: str,
) -> TaskCreateRequest:
    """The task asking a person to invalidate ``name`` in ``config`` for an
    incident: how, never a value.

    ``why`` says why a person does it (the worker does not rotate its kind,
    or the worker's job for it failed). The task's key ends in the incident
    id, so asking again for the same incident makes no second task, and an
    incident task and an ordinary cycle task for the secret can both be
    open: one rotation closes both.
    """
    command = (
        f"uv run scripts/rotate.py rotate {name} --compromised "
        f"--reason incident-{incident_id}"
    )
    if config != "prd":
        command += f" --config {config}"
    lines = [
        f"Rotation incident {incident_id}: every secret is being invalidated.",
        why,
        "",
        f"Run from a rotator-cog checkout: `{command}`",
        "It prints the steps, takes the new value at a hidden prompt, writes "
        "it where the inventory says, and records the rotation.",
        "",
        f"Steps: {entry.get('steps', 'see the provider console')}",
        f"Revoke: {entry.get('revoke', 'revoke the old value at the provider')}",
    ]
    if entry.get("can"):
        lines.append(f"What it can do: {entry['can']}")
    return TaskCreateRequest(
        source="secret-rotation",
        external_id=f"secret-rotation.{config}.{name}.{incident_id}",
        draft=TaskDraft(
            title=f"Incident: invalidate {name} in {config}",
            done_when=(
                f"A rotation of {name} in {config} is recorded after the "
                f"incident started, and the old value is revoked; or, for a "
                "secret that cannot be invalidated, the task says why."
            ),
            constraints=(
                "The new value goes in only at rotate.py's hidden prompt: "
                "never into a chat, a ticket or a file."
            ),
            effort="S",
            body="\n".join(lines),
            labels=["person"],
        ),
    )


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
        if config in (e.get("configs") if e.get("configs") is not None else ["prd"])
        and n not in names
    )
    return unknown, missing


def change_window(now: datetime) -> tuple[datetime, datetime]:
    """(since, until) of the changes a sweep at ``now`` judges: one day of
    them, a day old."""
    until = now - CHANGE_JUDGED_AFTER
    return until - CHANGE_READ_SPAN, until


def owner_of(name: str, inventory: Mapping[str, Any]) -> tuple[str, bool] | None:
    """(Inventory secret ``name`` belongs to, whether it is the secret itself).

    None for a name the inventory does not list, which drift reports. A
    secret's ``_PREVIOUS`` slot, its key id variable and its copies belong to
    it but are not it.
    """
    secrets: Mapping[str, Any] = inventory["secrets"]
    if name in secrets:
        return name, True
    for secret, entry in secrets.items():
        if name == doppler_mod.previous_name(secret):
            return secret, False
        if name == entry.get("id_var") or name in entry.get("also", []):
            return secret, False
    return None


def unexplained(
    changes: Iterable[SecretChange],
    events: Iterable[RotationEvent],
    inventory: Mapping[str, Any],
) -> list[SecretChange]:
    """The changes to inventory names that no recorded rotation explains."""
    minted: dict[str, list[datetime]] = {}
    for event in events:
        minted.setdefault(event.name, []).append(event.minted)
    out: list[SecretChange] = []
    for change in changes:
        owner = owner_of(change.name, inventory)
        if owner is None:
            continue
        secret, itself = owner
        before = EXPLAINED_BEFORE if itself else HANDOVER_EXPLAINS
        earliest = change.changed_at - before
        latest = change.changed_at + EXPLAINED_AFTER
        if not any(earliest <= m <= latest for m in minted.get(secret, [])):
            out.append(change)
    return out


def change_task_for(change: SecretChange, config: str) -> TaskCreateRequest:
    """The task asking a person to record or revert an unexplained change."""
    at = change.changed_at.strftime("%Y%m%dT%H%M%SZ")
    when = change.changed_at.strftime("%Y-%m-%d %H:%M UTC")
    record = f"uv run scripts/rotate.py rotate {change.name} --reason recorded"
    if config != "prd":
        record += f" --config {config}"
    lines = [
        f"Doppler reported {change.name} {change.change} in {config} at {when}, "
        "and no recorded rotation explains it: the value was changed outside "
        "the rotation process.",
        "",
        "If the change was meant, rotate the secret properly so it is "
        f"recorded: `{record}` from a rotator-cog checkout.",
        "If it was not, revert it in Doppler, then find out who changed it "
        "(Doppler's activity log).",
    ]
    return TaskCreateRequest(
        source="secret-rotation",
        external_id=f"secret-rotation.{config}.{change.name}.changed-{at}",
        draft=TaskDraft(
            title=f"Record or revert the change to {change.name} in {config}",
            done_when=(
                f"The change to {change.name} in {config} at {when} is either "
                "recorded as a rotation or reverted."
            ),
            constraints=(
                "A new value goes in only at rotate.py's hidden prompt: never "
                "into a chat, a ticket or a file."
            ),
            effort="S",
            body="\n".join(lines),
            labels=["person"],
        ),
    )


def retired_event(event: RotationEvent, at: datetime) -> RotationEventCreate:
    """The same event (same key), now with its old value retired."""
    fields = RotationEventCreate.model_fields
    kept = {k: v for k, v in event.model_dump().items() if k in fields}
    return RotationEventCreate(**{**kept, "retired": at})


def request_for(name: str, config: str) -> RotationRequest:
    """The routine rotation job a sweep runs for ``name`` in ``config``."""
    return RotationRequest(name=name, config=config, mode="routine")  # type: ignore[arg-type]
