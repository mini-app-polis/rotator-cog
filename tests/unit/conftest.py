"""Unit-test settings shared by every module here."""

from __future__ import annotations

import pytest

from rotator_cog import doppler, railway, worker


@pytest.fixture(autouse=True)
def no_retry_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries still happen; the waits between them do not."""
    monkeypatch.setattr(doppler, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(railway, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(worker, "VERIFY_RETRY_SECONDS", 0)
