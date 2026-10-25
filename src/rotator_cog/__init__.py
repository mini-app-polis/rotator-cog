"""Rotates the fleet's secrets and records each rotation.

The inventory lives in ``secrets/`` and ``scripts/rotate.py`` does the
rotation. The API keeps the record of rotations: ``api`` reads and writes
it, ``events`` builds what is written, and ``legacy`` seeds it from the
ledger kept before the API did.
"""

from rotator_cog._version import __version__

__all__ = ["__version__"]
