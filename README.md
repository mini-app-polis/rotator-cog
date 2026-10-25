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
- `scripts/rotate.py`: lists what is due, checks Doppler against the
  inventory, and rotates, verifies and records a secret.
- `src/rotator_cog`: the record's client, the events rotate.py writes, and
  the import of the legacy record. Standard library only, so rotate.py
  imports it and stays a plain `uv run` script.

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
against a fake transport.
