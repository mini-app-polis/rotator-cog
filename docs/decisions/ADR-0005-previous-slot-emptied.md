# ADR-0005: Empty a previous slot rather than delete it

Date: 2026-10-27

## Status

Accepted. Changes how ADR-0002's compromised rotation and ADR-0004's
retirement clear `NAME_PREVIOUS`.

## Context

The services that accept machine keys stop reading the Doppler config's
shared variables. Each gets a Doppler config of its own, whose secrets are
references into the config the rotator writes (mini-app-polis/infra,
`doppler/`). Such a config references `NAME_PREVIOUS` for every key whose
previous value it must accept during a handover.

Doppler refuses a reference to a secret that does not exist. A slot that
comes and goes, created by a rotation and deleted when the old key is
retired, cannot be referenced.

## Decision

`NAME_PREVIOUS` always exists. A compromised rotation and a retirement
set it to the empty string instead of deleting it; a compromised rotation
that finds no slot creates it empty. Phase 1 of a routine rotation writes
the new value into it as before.

An empty slot means no previous key. The services already read it that
way: an empty `NAME_PREVIOUS` is the same as an unset one, and the
rotator's wait for Railway to show a value treats an empty one as gone.

## Consequences

- Every config the rotator writes holds a `NAME_PREVIOUS` for each key
  with a handover, empty while no rotation is in progress. They are seeded
  empty once.
- A service config can reference every previous slot it accepts, so a
  rotation reaches it through Doppler's sync like any other change.
- Nothing about the handover's order, the redeploys or the retirement
  rules changes.
