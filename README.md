# rotator-cog

[![CI](https://github.com/mini-app-polis/rotator-cog/actions/workflows/ci.yml/badge.svg)](https://github.com/mini-app-polis/rotator-cog/actions/workflows/ci.yml)
[![Version](https://img.shields.io/github/v/tag/mini-app-polis/rotator-cog?label=version)](https://github.com/mini-app-polis/rotator-cog/releases)

Rotates the fleet's secrets and records each rotation. This repository
holds the inventory of every credential the fleet holds and the script that
rotates them; the record of every rotation is kept by the API
(`/v1/secrets/rotation-events`). None of it ever holds a value: values live
in Doppler.

- [`secrets/`](secrets/README.md): the inventory and the compromise
  runbook.
- [`docs/provider-assessment.md`](docs/provider-assessment.md): which
  manually minted secrets the rotator could take over, and why each stays
  manual or was automated.
- `scripts/rotate.py`: lists what is due, checks Doppler against the
  inventory, and rotates, verifies and records a secret.
- `src/rotator_cog`: the record's client and the events rotate.py writes.
  Those two modules are standard library only, so rotate.py imports them
  and stays a plain `uv run` script.
- `src/rotator_cog/worker.py`: the Lambda worker behind rotator-cog's queue
  (declared in mini-app-polis/infra). It takes `rotation.job`,
  `rotation.sweep` and `rotation.incident` messages. A job is checked against the inventory packaged
  with it: machine keys, IAM access keys and provider-minted secrets, in a config the key is held
  in, and only keys that live in Doppler alone (the rest are rotated with
  rotate.py). An access key is created beside the old one, and the old one
  deleted once the new one has verified. A routine
  rotation hands the new key over in two phases (`NAME_PREVIOUS` first, so
  the API accepts it before callers switch); a compromised one replaces it
  at once. After each write the worker waits for Railway to show the new
  value, then redeploys the services that read it with that config's
  Railway project token, reusing their last image. It then checks the key
  with `GET /v1/identity/whoami` at the config's API and records the
  rotation as ok or failed. `TURNSTILE_SECRET_KEY` (`kind: provider-api`)
  is minted by Cloudflare's rotate-secret API instead
  (`src/rotator_cog/turnstile.py`): Cloudflare keeps the old secret valid
  for two hours, so it is one write and one redeploy, checked with
  siteverify. A daily sweep per config retires previous keys
  nothing uses any more, rotates the most overdue key and reports drift
  (`src/rotator_cog/sweep.py`, and the runbook in `secrets/`). A
  `rotation.incident`, which the owner starts through the API, invalidates
  every inventory secret in both configs, prd first and tier 0 first: a
  compromised job per secret the worker rotates, sent to its own queue, and
  a board task per secret a person rotates, all under the incident's id
  (`src/rotator_cog/incident.py`, ADR-0007). CI deploys the worker after
  each release.

These came here from mini-app-polis/infra with their history.

## Running a rotation

From a clone, with `doppler` logged in (and `aws`, `gh`, `curl` as the
secrets being rotated need them):

```sh
uv run scripts/rotate.py list --due
uv run scripts/rotate.py check
uv run scripts/rotate.py rotate OPENAI_API_KEY --reason "routine" --dry-run
```

[`secrets/README.md`](secrets/README.md) has the full procedure, including
what to do when a secret is compromised.

## Running locally

Requires Python 3.11+, [uv](https://github.com/astral-sh/uv) and the
[Doppler CLI](https://docs.doppler.com/docs/install-cli). Secrets come from
this repo's own Doppler `dev` config — nothing reads a `.env` file;
[`.env.example`](.env.example) lists the names.

```sh
brew install dopplerhq/cli/doppler   # once per machine
doppler login                        # once per machine

doppler setup                        # once per clone: reads doppler.yaml
uv sync
uv run pre-commit install            # once per clone: ruff and gitleaks on commit
uv run check-doppler-keys            # every required .env.example name is in dev

uv run pytest
uv run pre-commit run --all-files
uv run ruff check && uv run ruff format --check
uv run mypy src/
```

The tests need no secrets and reach nothing. `tests/unit/` checks the
inventory's shape (every rotated kind has a cadence and every secret a
tier), the events rotate.py writes against the API's contract, the
clients against fake transports,
and the worker's message handling, job checks, retries and token reads
against fakes. `tests/integration/` runs `lambda_handler` from an SQS
record through the real Doppler, Railway and API clients, with the far
ends faked at the wire by respx and wired to each other as the real ones
are.

The worker itself runs only on Lambda: its Doppler and Railway tokens are
in Parameter Store, never on a laptop.

## Decisions

[`docs/decisions/`](docs/decisions/README.md) records why the worker is
shaped as it is: what it rotates and what stays on rotate.py, the two-phase
handover, one delivery per job and how duplicates are handled, and the
daily sweep.
