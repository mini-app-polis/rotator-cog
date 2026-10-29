# ADR-0001: A Lambda worker for keys it can rotate alone; rotate.py for the rest

Date: 2026-10-25

## Status

Accepted

> **Amended (2026-10-29).** The vault is now two Doppler projects:
> `mini-app-polis-secrets` holds every value that has to be rotated after
> a breach, and `mini-app-polis-settings` everything else. The worker's
> Doppler service token per config is scoped to `mini-app-polis-secrets`
> and reaches no setting. The decision below is otherwise unchanged.

## Context

Every credential the fleet holds is listed in `secrets/inventory.yaml`,
and until now each was rotated by a person running `scripts/rotate.py`.
Most of them can only be rotated that way: a provider's console has to
mint the new value, the value is also held in GitHub, or a step such as a
temporary TCP proxy has to be done by hand. Two kinds need none of that:
a machine key is a random string the API checks, and an IAM access key is
minted by an API call. Those are the ones that come due most often. A
generated secret needs no person either, but its check runs under the
doppler CLI, which the function does not have.

Rotating a value means writing to Doppler and redeploying the Railway
services that read it at boot. Whatever does that holds tokens that can
write the fleet's secrets, which makes them the most sensitive
credentials in it.

## Decision

- rotator-cog runs on Lambda behind its own FIFO queue, declared in
  mini-app-polis/infra like the other pipeline cogs. A job comes from the
  API (`POST /v1/secrets/rotations`); the daily sweep from the cog's own
  schedule.
- The worker rotates the `machine-key` and `aws-access-key` kinds, and
  only entries held in Doppler alone: an entry also held in GitHub, or
  with a manual step, is refused and stays on `rotate.py`. Every job is
  checked against the inventory packaged with the function.
- The worker writes with a Doppler service token per config (prd, dev)
  and redeploys with a Railway project token per environment, rather
  than one workspace token. Each token is a SecureString in Parameter
  Store outside `/mini-app-polis/`, readable by the rotator's role alone,
  and the function's environment holds only the paths. None of them is
  ever in Doppler, where the rotator would be rotating its own access.
- After each write the worker waits until Railway shows the new value,
  then redeploys the services the inventory lists, reusing their last
  image: a changed variable needs no build.
- A new machine key is checked with `GET /v1/identity/whoami`, a new IAM
  key with `sts get-caller-identity`, and every rotation is recorded with
  the API as ok or failed.

## Consequences

- The most frequent rotations need no person; the rest keep the
  procedure in `secrets/README.md`.
- A token per config means a leaked one reaches one environment, and the
  worker checks the Railway token reaches the environment it expects
  before it redeploys anything.
- The rotator's own tokens are rotated by hand, outside Doppler.
- Taking a kind over from `rotate.py` means a worker change and an
  inventory change; `docs/provider-assessment.md` records why each
  manual kind stays manual.
