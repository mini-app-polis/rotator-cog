# ADR-0004: A daily sweep that retires, rotates one key and reports drift

Date: 2026-10-25

## Status

Accepted

## Context

A routine rotation leaves the old key in `NAME_PREVIOUS` (ADR-0002), and
something has to retire it once nothing presents it. Keys also come due
on their cadence (`cadence_days` in the inventory) whether or not anyone
asks, and Doppler and the inventory drift apart when a secret is added or
removed in one and not the other.

A run has to fit the function's timeout, and a single rotation already
takes two rounds of redeploys.

## Decision

- mini-app-polis/infra schedules one `rotation.sweep` per Doppler config
  each day. A sweep, in order:
  1. retires previous keys nothing has used for 24 hours: in prd, read
     from the API's audit trail (`GET /v1/secrets/key-use`); in dev, a
     day after the rotation, since the rotator holds no key for the
     development API. At seven days a key still in use is retired anyway
     and the run says which machine was still on it;
  2. rotates the most overdue key, one per run, and only on a day that
     retired nothing; a key its services are not ready for is reported
     and passed over for the next;
  3. reports names in Doppler that look secret and have no inventory
     entry, and entries the config should hold but does not.
- The sweep rotates only when `ROTATOR_SWEEP_ROTATE` is `true`. Unset, it
  still retires and reports, and names what is due, so its own rotations
  wait until a rotation has been proven by hand.

## Consequences

- A key is retired 24 to 48 hours after its last use.
- A backlog clears one key a day per config.
- Turning the sweep's rotations on is a settings change, not a release.
