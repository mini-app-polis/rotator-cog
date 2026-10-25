"""Rotates the fleet's secrets and records each rotation.

The inventory lives in ``secrets/`` and ``scripts/rotate.py`` does the
rotation. The API keeps the record of rotations: ``api`` reads and writes
it, ``events`` builds what is written, and ``legacy`` seeds it from the
ledger kept before the API did. ``worker`` is the Lambda entrypoint.
"""

import os

from rotator_cog._version import __version__

# In the Lambda worker, its secrets arrive from SSM Parameter Store here, at
# the first import of the package, before any module reads the environment at
# import (the logger, Sentry's init in the worker). Only there: rotate.py
# imports this package with the standard library alone, and
# mini_app_polis.ssm_secrets is a no-op without SSM_PARAMETERS anyway.
if os.environ.get("SSM_PARAMETERS"):
    from mini_app_polis import load_secrets

    load_secrets()

__all__ = ["__version__"]
