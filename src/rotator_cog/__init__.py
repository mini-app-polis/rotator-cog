"""Rotates the fleet's secrets and records each rotation.

The inventory and the record live in ``secrets/``; ``scripts/rotate.py``
does the rotation.
"""

from rotator_cog._version import __version__

__all__ = ["__version__"]
