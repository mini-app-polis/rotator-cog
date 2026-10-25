# rotator-cog

[![CI](https://github.com/mini-app-polis/rotator-cog/actions/workflows/ci.yml/badge.svg)](https://github.com/mini-app-polis/rotator-cog/actions/workflows/ci.yml)
[![Version](https://img.shields.io/github/v/tag/mini-app-polis/rotator-cog?label=version)](https://github.com/mini-app-polis/rotator-cog/releases)

Rotates the fleet's secrets and records each rotation. This repository
holds the inventory of every credential the fleet holds, the record of
every rotation, and the script that rotates them. None of it ever holds a
value: values live in Doppler.

- [`secrets/`](secrets/README.md): the inventory, the record, and the
  compromise runbook.
- `scripts/rotate.py`: lists what is due, checks Doppler against the
  inventory, and rotates, verifies and records a secret.

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

The tests check the inventory's and the record's shape: every rotated
kind has a cadence, and no row in the record carries a value.
