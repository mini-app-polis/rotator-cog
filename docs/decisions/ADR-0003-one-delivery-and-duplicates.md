# ADR-0003: One delivery per job, and how a job asked twice rotates once

Date: 2026-10-25

## Status

Accepted

## Context

A rotation changes Doppler, Railway and, for an IAM key, AWS, in that
order. One that fails partway can leave a secret half rotated, and
running it again from the top would mint a third value on top of the
half-finished second. A queue's usual redelivery is the wrong recovery.

The same job can also reach the worker twice: a request submitted twice,
or an on-demand rotation on the day the sweep would have rotated the same
key. PY-013 asks for the strategy to be recorded.

## Decision

- **One delivery.** The queue delivers each message once
  (`max_receive_count = 1` in mini-app-polis/infra). A job that crashes
  is named in `batchItemFailures`, lands in the dead-letter queue, and
  its alarm says so. A person finishes it, with the runbook in
  `secrets/`.
- **Duplicate jobs.** The queue is FIFO with content-based
  deduplication and the API sends no deduplication id, so the same
  request asked again within five minutes is one job.
- **Duplicate rotations.** The sweep never rotates a key whose previous
  key is still live, since a second handover would cut its callers off,
  and never retries a rotation whose latest attempt failed: both need a
  person. An IAM user that already has two access keys is refused before
  anything is created.
- **Duplicate records.** A rotation event is keyed on name, config and
  minted, so recording it again updates the same event rather than adding
  one; the import of the legacy ledger relies on the same key.
- A job the inventory refuses is reported and acknowledged: it is the
  producer's mistake, and sending it again would fail the same way.

## Consequences

- A half-rotated secret is always visible (the alarm, a failed event)
  and never made worse by a retry.
- Transient failures are retried inside one delivery instead: each
  Doppler and Railway request, and the whoami check, is tried again on a
  connection failure, 429 or 5xx. A Railway redeploy is asked again only
  when the request never left, so it is never started twice.
- An on-demand routine rotation of a key whose previous key is still
  live is not refused by the worker; only the sweep checks. Asking for
  one is a person's decision, made with the rotation record in view.
