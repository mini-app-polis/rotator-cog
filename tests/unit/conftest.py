"""Unit-test settings shared by every module here."""

from __future__ import annotations

import pytest
from mini_app_polis.api import client as api_client

from rotator_cog import doppler, railway


@pytest.fixture(autouse=True)
def no_retry_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries still happen; the waits between them do not."""
    monkeypatch.setattr(doppler, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(railway, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(api_client, "_sleep", lambda seconds: None)
