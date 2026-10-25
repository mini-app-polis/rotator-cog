"""Seed the API's record from the legacy ledger, secrets/rotations.jsonl.

The ledger was rotate.py's record before the API kept one. Its rows do not
fit the API's contract one for one: re-checks were rows of their own, some
rows name database URLs rather than inventory entries, and some have no
Doppler config. So the import does not copy rows. It sends one event per
(name, config), the latest rotation, which is all the record needs to say
what is due:

- a row whose config is not prd or dev is skipped (``-`` marked a secret
  held outside Doppler);
- a ``postgres-role`` row names a database URL variable, which is not an
  inventory entry, and is skipped; so is any name the inventory no longer
  lists, or lists as not rotated;
- a ``verify`` row is a re-check, not a rotation. One that comes after the
  latest rotation decides that rotation's verification;
- the kind is the inventory's; the actor is who ran it; the old value was
  retired when the row was written if the row says it was revoked;
- a rotation nothing checked is ``failed``, saying no check ran: only a
  check makes a rotation ``ok``.

Events are keyed on name, config and minted, so running the import again
sends the same events and the API adds nothing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from rotator_cog.events import event, last_minted, utc, verification

CONFIGS = ("prd", "dev")
MODES = ("routine", "compromised")
#: The kinds an event can have: the inventory's, less not-rotated.
KINDS = ("machine-key", "generated", "aws-access-key", "manual")

SKIP_CONFIG = "config is not prd or dev"
SKIP_POSTGRES_ROLE = "postgres-role: a database URL, not an inventory entry"
SKIP_NOT_IN_INVENTORY = "no longer in the inventory"
SKIP_NOT_ROTATED = "the inventory does not rotate it"
SKIP_MODE = "mode is not routine, compromised or verify"
SKIP_SUPERSEDED = "an earlier rotation of the same secret and config"
SKIP_EARLY_VERIFY = "a re-check of an earlier rotation"
SKIP_ORPHAN_VERIFY = "a re-check of a secret with no rotation"


class Api(Protocol):
    def record(self, event: dict[str, Any]) -> dict[str, Any]: ...

    def list(
        self,
        *,
        name: str | None = None,
        config: str | None = None,
        latest: bool = False,
    ) -> list[dict[str, Any]]: ...


@dataclass
class Plan:
    """What the import sends, and what it leaves out."""

    events: list[dict[str, Any]] = field(default_factory=list)
    #: Rows left out, by reason.
    skipped: Counter[str] = field(default_factory=Counter)
    #: Re-check rows that decided an event's verification.
    folded: int = 0


def plan(rows: Iterable[Mapping[str, Any]], inventory: Mapping[str, Any]) -> Plan:
    """The events the ledger's ``rows`` seed the record with."""
    entries = inventory["secrets"]
    out = Plan()
    rotations: dict[tuple[str, str], list[tuple[tuple[str, int], Mapping]]] = {}
    checks: dict[tuple[str, str], list[tuple[tuple[str, int], Mapping]]] = {}

    for i, row in enumerate(rows):
        name, config, mode = row["name"], row["config"], row.get("mode")
        if config not in CONFIGS:
            out.skipped[SKIP_CONFIG] += 1
        elif row.get("kind") == "postgres-role":
            out.skipped[SKIP_POSTGRES_ROLE] += 1
        elif name not in entries:
            out.skipped[SKIP_NOT_IN_INVENTORY] += 1
        elif entries[name].get("kind") not in KINDS:
            out.skipped[SKIP_NOT_ROTATED] += 1
        elif mode == "verify":
            checks.setdefault((name, config), []).append(((utc(row["at"]), i), row))
        elif mode in MODES:
            rotations.setdefault((name, config), []).append(((utc(row["at"]), i), row))
        else:
            out.skipped[SKIP_MODE] += 1

    for key in checks.keys() - rotations.keys():
        out.skipped[SKIP_ORPHAN_VERIFY] += len(checks[key])

    for (name, config), found in rotations.items():
        order, row = max(found, key=lambda f: f[0])
        out.skipped[SKIP_SUPERSEDED] += len(found) - 1
        later = sorted(
            (c for c in checks.get((name, config), []) if c[0] > order),
            key=lambda c: c[0],
        )
        out.skipped[SKIP_EARLY_VERIFY] += len(checks.get((name, config), []))
        out.skipped[SKIP_EARLY_VERIFY] -= len(later)
        out.folded += len(later)

        kind = entries[name]["kind"]
        minted = order[0]
        e = event(
            name=name,
            config=config,
            kind=kind,
            mode=row["mode"],
            actor=row["by"],
            minted=minted,
            retired=minted if row.get("revoked") is True else None,
            verified=row.get("verified"),
            aws_key_last4=(
                row.get("aws_key_id_last4") if kind == "aws-access-key" else None
            ),
        )
        if later:
            e.update(verification(later[-1][1].get("verified"), recheck=True))
        out.events.append(e)

    out.events.sort(key=lambda e: (e["minted"], e["name"], e["config"]))
    out.skipped = +out.skipped  # drop the reasons nothing was skipped for
    return out


def describe(e: Mapping[str, Any]) -> str:
    """One event as a line: configs, kinds and times only (name redacted)."""
    retired = f"retired {e['retired']}" if e["retired"] else "not retired"
    redacted_name = "[REDACTED]"
    return (
        f"{e['minted']}  {e['config']:<3}  {redacted_name:<38} {e['kind']:<15} "
        f"{e['mode']:<11} {e['verification']:<6}  {retired}"
    )


def run(
    rows: Iterable[Mapping[str, Any]],
    inventory: Mapping[str, Any],
    api: Api | None,
    *,
    dry_run: bool,
    out: Callable[[str], None] = print,
) -> int:
    """Send the plan's events; with ``dry_run`` print them and send nothing.

    After a real run, read the latest event per secret and config back and
    check every imported one is there (or has been rotated since).
    """
    p = plan(rows, inventory)
    for e in p.events:
        out(describe(e))
    for reason, n in sorted(p.skipped.items()):
        out(f"skipped {n} row(s): {reason}")
    out(f"folded {p.folded} re-check row(s) into the events above")
    if dry_run:
        out(f"would send {len(p.events)} event(s)")
        return 0
    if api is None:
        raise ValueError("a real run needs the API")

    for e in p.events:
        api.record(e)
    out(f"sent {len(p.events)} event(s)")

    stored = api.list(latest=True)
    latest = last_minted(stored)
    missing = [
        e for e in p.events if latest.get((e["name"], e["config"]), "") < e["minted"]
    ]
    newer = sum(
        1 for e in p.events if latest.get((e["name"], e["config"]), "") > e["minted"]
    )
    out(
        f"the API's latest events: {len(stored)}; "
        f"{len(p.events) - len(missing)} of the {len(p.events)} imported are there"
        + (f" ({newer} rotated again since)" if newer else "")
    )
    for e in missing:
        out(f"missing from the API: {e['name']} {e['config']}")
    return 1 if missing else 0
