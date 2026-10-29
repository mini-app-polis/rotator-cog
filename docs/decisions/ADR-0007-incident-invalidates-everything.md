# ADR-0007: An incident invalidates every secret, in tier order, under one id

Date: 2026-10-29

## Status

Accepted

> **Amended (2026-10-29).** The inventory no longer has a `not-rotated`
> kind. A value that never needs rotating after a breach is a setting, held
> in `mini-app-polis-settings` outside the inventory, so an incident does
> not cover it. Every inventory entry has a tier, and the incident list and
> tasks below carry no `not-rotated` case. The decision below is otherwise
> unchanged.

## Context

The rotator exists so that a breach has a defined process to execute and
track. A compromised rotation of one secret (ADR-0001, `mode:
compromised`) covers one known leak; a breach of the secret store, a
laptop or a CI runner means every secret must be treated as known to
someone else. Invalidating them all matters more than keeping the fleet
fully working while it happens: an outage is the price, and nothing is
kept in an overlap slot.

Some secrets the worker rotates itself; most are minted in a provider's
console by a person (`manual`, entries with `steps`), and some are listed
as never rotated (`not-rotated`: public by design, identifiers). A breach
covers them all, in an order: what reaches the secret store and source
code first (tier 0), nuisance last (tier 3).

The incident is started by the owner through the API, which mints its id.
The API keeps no copy of the inventory; the rotator packages it.

## Decision

- The API puts one `rotation.incident` (`{incident_id}`) on this cog's
  queue. The worker expands it (`incident.py`):
  - the list is every inventory entry in every config it is held in, prd
    first, then dev, tier 0 to 3 within each, then by name; `configs: []`
    counts as prd, and an entry with no tier (`not-rotated`) as tier 3;
  - each entry is the worker's when it would accept a job for it (a kind
    it rotates, no steps), and a person's otherwise;
  - the list is registered with the API (the snapshot of what was in scope
    when the incident was declared), which answers with what is already
    recorded for each entry;
  - for each of its entries the worker sends one compromised
    `rotation.job` carrying the incident id to its own queue
    (`ROTATOR_JOBS_QUEUE_URL`, set by mini-app-polis/infra) in the one
    message group the API uses, so tier order holds;
  - for each person's entry it asks the task manager for one task, keyed
    `secret-rotation.<config>.<name>.<incident_id>`, with the rotate.py
    command (`--compromised`), the steps and the revoke, and marks the
    entry with the API once the ask is accepted. A `not-rotated` entry's
    task says to close it, saying why, if the secret truly cannot be
    invalidated.
  - One entry's failure is reported and the walk goes on.
- Repeating the incident (a second delivery, or the owner resuming it)
  re-runs the expansion: an entry already rotated since the incident
  started gets no second job, and an entry whose task was asked for gets
  no second ask. The task key ends in the incident id, so the task manager
  makes no second task either.
- Each incident job is rotated compromised whatever its mode says, and its
  rotation event carries the incident id. When it fails, however it fails
  (rejected, not ready, not in Doppler, a write, redeploy or check that
  fails), the failure is recorded where a value was minted and reported,
  a person's task is asked for under the same incident key, and the entry
  is marked with the API. The job is then acknowledged, not dead-lettered:
  the queue delivers each message once and keeps the incident's jobs in
  one group, so a dead-lettered job would hold the rest until its
  visibility timeout. A routine job's failure still fails the job, as
  before.
- An incident task and an ordinary cycle task for the same secret may both
  be open; one rotation recorded after both were created closes both.
- The run report names the incident first; its full per-secret state is
  the API's `GET /v1/secrets/rotation-incidents/{id}`.

## Consequences

- One request declares a breach response; its progress is one read.
- The incident's jobs queue behind each other and behind anything already
  on the queue. A full incident is many jobs, each up to the function's
  timeout, so it takes as long as they take one at a time.
- Compromised rotations cut callers off until their redeploys finish: the
  fleet is partly down while the incident runs, by design.
- Verifying this work never runs a real incident: tests use fakes.
- Known future direction, out of scope: targeted invalidations and
  rotations, an incident over a chosen set of secrets rather than every
  one. No board task tracks it.
