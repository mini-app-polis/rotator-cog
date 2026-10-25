# rotator-cog

[![CI](https://github.com/mini-app-polis/rotator-cog/actions/workflows/ci.yml/badge.svg)](https://github.com/mini-app-polis/rotator-cog/actions/workflows/ci.yml)
[![Version](https://img.shields.io/github/v/tag/mini-app-polis/rotator-cog?label=version)](https://github.com/mini-app-polis/rotator-cog/releases)

Rotates the fleet's secrets and records each rotation. This repository
holds the inventory of every credential the fleet holds and the script that
rotates them; the record of every rotation is kept by the API
(`/v1/secrets/rotation-events`). None of it ever holds a value: values live
in Doppler.

- [`secrets/`](secrets/README.md): the inventory, the legacy record the API
  is seeded from, and the compromise runbook.
- [`docs/provider-assessment.md`](docs/provider-assessment.md): which
  manually minted secrets the rotator could take over, and why each stays
  manual.
- `scripts/rotate.py`: lists what is due, checks Doppler against the
  inventory, and rotates, verifies and records a secret.
- `src/rotator_cog`: the record's client, the events rotate.py writes, and
  the import of the legacy record. Those three modules are standard library
  only, so rotate.py imports them and stays a plain `uv run` script.
- `src/rotator_cog/worker.py`: the Lambda worker behind rotator-cog's queue
  (declared in mini-app-polis/infra). It takes `rotation.job` and
  `rotation.sweep` messages. A job is checked against the inventory packaged
  with it: machine keys and IAM access keys, in a config the key is held
  in, and only keys that live in Doppler alone (the rest are rotated with
  rotate.py). An access key is created beside the old one, and the old one
  deleted once the new one has verified. A routine
  rotation hands the new key over in two phases (`NAME_PREVIOUS` first, so
  the API accepts it before callers switch); a compromised one replaces it
  at once. After each write the worker waits for Railway to show the new
  value, then redeploys the services that read it with that config's
  Railway project token, reusing their last image. It then checks the key
  with `GET /v1/identity/whoami` at the config's API and records the
  rotation as ok or failed. A daily sweep per config retires previous keys
  nothing uses any more, rotates the most overdue key and reports drift
  (`src/rotator_cog/sweep.py`, and the runbook in `secrets/`). CI deploys
  the worker after each release.

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

## Development

```sh
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
uv run mypy src/
```

The tests check the inventory's and the legacy record's shape (every
rotated kind has a cadence, and no row carries a value), the events
rotate.py and the import write against the API's contract, and the client
against a fake transport, and the worker's message handling, job checks
and token reads against fakes.
