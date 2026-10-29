# ADR-0006: Doppler delivers the GitHub copies; rotation writes Doppler alone

Date: 2026-10-27

## Status

Accepted. Narrows ADR-0001: the worker no longer refuses a secret because
GitHub also holds it.

> **Amended (2026-10-29).** The single vault this ADR names,
> `mini-app-polis-ecosystem`, is split in two: `mini-app-polis-secrets`
> holds every value that has to be rotated after a breach, and
> `mini-app-polis-settings` everything else. Readers reference whichever
> holds each value, and Doppler's syncs deliver both the same way. A
> rotation writes `mini-app-polis-secrets` and nothing else; the rotator
> covers the secrets project only. The decision below is otherwise
> unchanged.

## Context

Some secrets are read by GitHub Actions as well as by services: machine
keys that workflows present to the API, the Railway token the IaC plans
use, the Cloudflare tokens. The inventory listed those GitHub copies under
`github`. `rotate.py` wrote each one with `gh secret set` after writing
Doppler, and the worker refused such a secret, since it had no way to
write GitHub.

The ecosystem's secrets are now laid out so that each value lives once,
in Doppler's `mini-app-polis-ecosystem`, and every reader holds a
reference to it in a Doppler project of its own. Each repository's
Actions secrets and variables are synced from that repository's project,
and the organisation's from `github-org`. A change to the ecosystem
project re-syncs every config that references it, GitHub's included.

## Decision

- A rotation writes Doppler and nothing else. `rotate.py` no longer calls
  `gh`, and the inventory's `github` field is gone.
- The worker rotates any `machine-key` or `aws-access-key` entry that
  needs no manual step, whether or not GitHub Actions also reads it.
- A secret GitHub alone used to hold (`RAILWAY_TOKEN`,
  `CLOUDFLARE_API_TOKEN` and the like) lives in the ecosystem project's
  `prd` like any other.

## Consequences

- Keys that CI presents, such as `CI_VALIDATOR_API_KEY`,
  `WIKI_CURATOR_COG_API_KEY` and `TASK_AGENT_API_KEY`, can be rotated by
  the worker and the daily sweep.
- A workflow reads the new value from its next run. The two-phase
  handover (ADR-0002) keeps the old key accepted while that happens.
- A GitHub copy that no Doppler config references is not rotated at all,
  and nothing reports it. Reference a new GitHub reader in Doppler before
  relying on rotation to reach it.
- `rotate.py` no longer needs `gh` or an `admin:org` login.
