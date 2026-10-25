# ADR-0002: Hand a routine rotation over in two phases

Date: 2026-10-25

## Status

Accepted

## Context

A machine key is presented by its callers and accepted by the API, and
the two learn a new value at different times. Callers on Lambda read
Parameter Store on every invocation, so they present a new key within
moments of Doppler's sync. The API reads its keys at boot, so it learns
the new one only when it is redeployed. Writing the new value once would
leave every Lambda caller presenting a key the API does not yet know.

The API accepts a current and a previous key per machine
(`NAME` and `NAME_PREVIOUS`, miniapppolis-identity 2.2), which is what
makes an overlap possible.

## Decision

- **Routine, phase 1.** The new value goes to `NAME_PREVIOUS`; callers
  keep presenting `NAME`. The API alone is redeployed, and then accepts
  both.
- **Routine, phase 2.** `NAME` takes the new value and `NAME_PREVIOUS`
  the old one; every service that reads the key is redeployed. Callers
  switch to the new key while the old one still works.
- **Compromised.** One write: `NAME` takes the new value and
  `NAME_PREVIOUS` is deleted. The 401s until the redeploy are the price
  of the old key stopping at once.
- The old key in `NAME_PREVIOUS` is retired by the sweep once nothing
  presents it (ADR-0004).
- An IAM access key needs no handover: AWS allows two per user, so the
  new key is created beside the old one, and the old one is deleted once
  the new one has verified (at once, if compromised).

## Consequences

- A routine rotation never cuts a caller off.
- A rotation is two Doppler writes and two rounds of redeploys, which is
  why the function's timeout is Lambda's ceiling.
- Nothing is written unless redeploy, verify and record are all wired: a
  handover stopped between its phases would leave a live key in
  `NAME_PREVIOUS` that nothing retires.
