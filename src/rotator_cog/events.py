"""Rotation events: the record of every rotation, kept by the API.

An event is what ``POST /v1/secrets/rotation-events`` takes (the contract's
``RotationEventCreate`` in miniapppolis-common-utils): one rotation of one
secret in one Doppler config, keyed on its name, its config and when the new
value was minted. Writing the same key again updates the event, so a
re-check rewrites the event it checked rather than adding one.

No event carries a value: names, configs, kinds, people, times, and a
bounded sentence when verification failed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

#: What the contract's create model declares. It refuses anything else, so
#: an event read back from the API (which adds its id and when it was
#: recorded) is cut down to these before it is written again.
CREATE_FIELDS = (
    "name",
    "config",
    "kind",
    "mode",
    "actor",
    "minted",
    "retired",
    "verification",
    "verification_reason",
    "aws_key_last4",
)

#: Why verification is "failed". Each is a fixed sentence: the contract
#: bounds the reason, and nothing a check printed belongs in it.
CHECK_FAILED = "The check failed when this was rotated."
RECHECK_FAILED = "A later re-check failed."
NO_CHECK = "No check ran when this was rotated by hand."


def now() -> str:
    """The current UTC time to the second, as the ISO string an event carries."""
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def verification(verified: bool | None, *, recheck: bool = False) -> dict[str, Any]:
    """The verification fields for a check's result.

    Only ``ok`` is a successful rotation, so a rotation nothing checked is
    ``failed`` with a reason that says no check ran, never ``ok``.
    """
    if verified is True:
        return {"verification": "ok", "verification_reason": None}
    if verified is False:
        reason = RECHECK_FAILED if recheck else CHECK_FAILED
        return {"verification": "failed", "verification_reason": reason}
    return {"verification": "failed", "verification_reason": NO_CHECK}


def event(
    *,
    name: str,
    config: str,
    kind: str,
    mode: str,
    actor: str,
    minted: str,
    retired: str | None,
    verified: bool | None,
    aws_key_last4: str | None = None,
) -> dict[str, Any]:
    """One event as the API takes it."""
    return {
        "name": name,
        "config": config,
        "kind": kind,
        "mode": mode,
        "actor": actor,
        "minted": minted,
        "retired": retired,
        **verification(verified),
        "aws_key_last4": aws_key_last4,
    }


def rechecked(stored: Mapping[str, Any], ok: bool) -> dict[str, Any]:
    """``stored`` (as the API returned it) with a re-check's result."""
    out = {f: stored.get(f) for f in CREATE_FIELDS}
    out.update(verification(ok, recheck=True))
    return out


def last_minted(events: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    """When each (name, config) was last minted, as ISO 8601 in UTC."""
    out: dict[tuple[str, str], str] = {}
    for e in events:
        key = (e["name"], e["config"])
        minted = utc(e["minted"])
        if minted > out.get(key, ""):
            out[key] = minted
    return out


def utc(when: str) -> str:
    """``when`` in one spelling, so times from the API and the ledger compare."""
    parsed = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
    return parsed.astimezone(dt.UTC).replace(microsecond=0).isoformat()
