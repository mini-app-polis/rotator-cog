"""Doppler's API against a fake transport: never the real Doppler."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from rotator_cog import doppler

TOKEN = "dp.st.dev.token-for-tests"
VALUE = "dev_value-for-tests"


class FakeDoppler:
    """One config's secrets, and every request made."""

    def __init__(self, secrets: dict[str, str] | None = None, *, status: int = 200):
        self.secrets = dict(secrets or {})
        self.status = status
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"messages": [VALUE]})
        if request.method == "GET" and request.url.path.endswith("/names"):
            return httpx.Response(200, json={"names": sorted(self.secrets)})
        if request.method == "GET":
            name = request.url.params["name"]
            if name not in self.secrets:
                return httpx.Response(404, json={"messages": ["not found"]})
            raw = self.secrets[name]
            return httpx.Response(200, json={"name": name, "value": {"raw": raw}})
        body = json.loads(request.content)
        for change in body["change_requests"]:
            if change.get("shouldDelete"):
                del self.secrets[change["name"]]
            else:
                self.secrets[change["name"]] = change["value"]
        echoed = {k: {"raw": v} for k, v in self.secrets.items()}
        return httpx.Response(200, json={"secrets": echoed})

    def client(self) -> doppler.Doppler:
        return doppler.Doppler(
            "proj", "dev", TOKEN, transport=httpx.MockTransport(self.handle)
        )

    def writes(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


def test_requests_carry_the_token_and_name_the_project_and_config() -> None:
    fake = FakeDoppler({"KEY": VALUE})
    assert fake.client().get("KEY") == VALUE
    (request,) = fake.requests
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert dict(request.url.params) == {
        "project": "proj",
        "config": "dev",
        "name": "KEY",
    }


def test_a_secret_that_is_not_set_reads_as_none() -> None:
    assert FakeDoppler().client().get("KEY") is None


def test_stage_puts_the_new_value_in_the_previous_slot_only() -> None:
    fake = FakeDoppler({"KEY": "old"})
    doppler.stage(fake.client(), "KEY", "new")
    assert fake.secrets == {"KEY": "old", "KEY_PREVIOUS": "new"}
    ((write,),) = [w["change_requests"] for w in fake.writes()]
    assert write == {"name": "KEY_PREVIOUS", "originalName": None, "value": "new"}


def test_stage_overwrites_a_previous_slot_already_there() -> None:
    fake = FakeDoppler({"KEY": "old", "KEY_PREVIOUS": "older"})
    doppler.stage(fake.client(), "KEY", "new")
    assert fake.secrets["KEY_PREVIOUS"] == "new"
    assert fake.writes()[0]["change_requests"][0]["originalName"] == "KEY_PREVIOUS"


def test_promote_swaps_the_slots_in_one_write() -> None:
    fake = FakeDoppler({"KEY": "old", "KEY_PREVIOUS": "new"})
    doppler.promote(fake.client(), "KEY", "new", "old")
    assert fake.secrets == {"KEY": "new", "KEY_PREVIOUS": "old"}
    assert len(fake.writes()) == 1


def test_replace_sets_the_value_and_empties_the_previous_slot_in_one_write() -> None:
    fake = FakeDoppler({"KEY": "old", "KEY_PREVIOUS": "older"})
    doppler.replace(fake.client(), "KEY", "new")
    assert fake.secrets == {"KEY": "new", "KEY_PREVIOUS": ""}
    assert len(fake.writes()) == 1


def test_replace_with_no_previous_slot_creates_it_empty() -> None:
    fake = FakeDoppler({"KEY": "old"})
    doppler.replace(fake.client(), "KEY", "new")
    assert fake.secrets == {"KEY": "new", "KEY_PREVIOUS": ""}
    (write,) = fake.writes()
    assert not any(c.get("shouldDelete") for c in write["change_requests"])


@pytest.mark.parametrize("status", [401, 403, 500])
def test_a_refused_request_names_the_secret_and_status_and_no_value(
    status: int,
) -> None:
    fake = FakeDoppler({"KEY": VALUE}, status=status)
    with pytest.raises(doppler.DopplerError) as caught:
        fake.client().change("KEY", [{"name": "KEY", "value": VALUE}])
    message = str(caught.value)
    assert f"HTTP {status}" in message and "KEY" in message
    assert VALUE not in message and TOKEN not in message


def test_a_request_that_cannot_be_made_names_only_its_type() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no route for {TOKEN}")

    client = doppler.Doppler("proj", "dev", TOKEN, transport=httpx.MockTransport(fail))
    with pytest.raises(doppler.DopplerError, match="ConnectError") as caught:
        client.get("KEY")
    assert TOKEN not in str(caught.value)
    assert caught.value.__suppress_context__


def test_an_unexpected_answer_is_an_error() -> None:
    client = doppler.Doppler(
        "proj",
        "dev",
        TOKEN,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    with pytest.raises(doppler.DopplerError, match="unexpected response"):
        client.get("KEY")


def test_names_lists_the_config_without_reading_values() -> None:
    fake = FakeDoppler({"KEY": VALUE, "OTHER": "x"})
    assert fake.client().names() == {"KEY", "OTHER"}
    assert fake.requests[0].url.path == "/v3/configs/config/secrets/names"


def test_retire_empties_every_previous_slot_in_one_write() -> None:
    fake = FakeDoppler({"A": "a", "A_PREVIOUS": "x", "B": "b", "B_PREVIOUS": "y"})
    doppler.retire(fake.client(), ["A", "B"])
    assert fake.secrets == {"A": "a", "A_PREVIOUS": "", "B": "b", "B_PREVIOUS": ""}
    (write,) = fake.writes()
    assert not any(c.get("shouldDelete") for c in write["change_requests"])


# -- retries ---------------------------------------------------------------


def _flaky(failures: list[Any], fake: FakeDoppler) -> httpx.MockTransport:
    """Fail with each of ``failures`` in turn (an exception, or a status), then ``fake``."""

    def handle(request: httpx.Request) -> httpx.Response:
        if failures:
            failure = failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure, json={"messages": ["busy"]})
        return fake.handle(request)

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), 429, 503]
)
def test_a_blip_is_asked_again(failure: Any) -> None:
    fake = FakeDoppler({"KEY": VALUE})
    client = doppler.Doppler("proj", "dev", TOKEN, transport=_flaky([failure], fake))
    assert client.get("KEY") == VALUE


def test_a_blip_that_lasts_fails_after_every_attempt() -> None:
    fake = FakeDoppler({"KEY": VALUE}, status=503)
    with pytest.raises(doppler.DopplerError, match="HTTP 503"):
        fake.client().get("KEY")
    assert len(fake.requests) == doppler.ATTEMPTS


def test_a_refusal_is_not_asked_again() -> None:
    fake = FakeDoppler({"KEY": VALUE}, status=401)
    with pytest.raises(doppler.DopplerError, match="HTTP 401"):
        fake.client().get("KEY")
    assert len(fake.requests) == 1


def test_a_write_asked_again_lands_once() -> None:
    fake = FakeDoppler({"KEY": "old"})
    client = doppler.Doppler(
        "proj", "dev", TOKEN, transport=_flaky([httpx.ReadTimeout("slow")], fake)
    )
    doppler.promote(client, "KEY", VALUE, "old")
    assert fake.secrets == {"KEY": VALUE, "KEY_PREVIOUS": "old"}


def test_an_answer_that_does_not_validate_never_quotes_the_value() -> None:
    client = doppler.Doppler(
        "proj",
        "dev",
        TOKEN,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"value": {"raw": [VALUE]}})
        ),
    )
    with pytest.raises(doppler.DopplerError, match="unexpected response") as caught:
        client.get("KEY")
    assert VALUE not in str(caught.value)
    assert caught.value.__suppress_context__
