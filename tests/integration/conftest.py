"""The worker end to end: SQS event in, Doppler, Railway and the API out.

Everything the handler builds is the real thing — the inventory packaged
with the function, the Doppler and Railway clients, the whoami check, the
API client that records the rotation and delivers the run report. Only the
far ends are fakes (wiring.py): HTTP through respx, and Parameter Store
through a boto3 stand-in, since boto3 ships in the Lambda runtime and not
here.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator

import pytest
import respx
from wiring import API, World, ssm_client

from rotator_cog import doppler, railway, worker


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """The handler's whole surroundings, faked at the wire."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("KAIANO_API_BASE_URL", API)
    monkeypatch.setenv("ROTATOR_COG_API_KEY", "rotator-cog-key-for-tests")
    monkeypatch.setenv("DOPPLER_TOKEN_PRD_PARAMETER", "/rotator/doppler/prd")
    monkeypatch.setenv("RAILWAY_TOKEN_PRD_PARAMETER", "/rotator/railway/prd")
    monkeypatch.delenv("SSM_PARAMETERS", raising=False)
    monkeypatch.delenv("SENTRY_DSN_COGS", raising=False)

    boto3 = types.ModuleType("boto3")
    boto3.client = lambda service, **kw: ssm_client()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3)

    monkeypatch.setattr(doppler, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(railway, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(railway, "POLL_SECONDS", 0)
    monkeypatch.setattr(worker, "VERIFY_RETRY_SECONDS", 0)

    inventory = worker.load_inventory()
    w = World(inventory)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.route(host="api.doppler.com").mock(side_effect=w.doppler)
        mock.route(url=railway.URL).mock(side_effect=w.railway)
        mock.get(f"{inventory['api_base_url']}/v1/identity/whoami").mock(
            side_effect=w.whoami
        )
        mock.post(f"{API}/v1/secrets/rotation-events").mock(side_effect=w.record)
        mock.post(f"{API}/v1/notify").mock(side_effect=w.notify)
        yield w
