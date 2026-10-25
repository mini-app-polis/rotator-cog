"""The record's client, against a fake transport: never the real API."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

from rotator_cog import api

BASE = "https://api.test.invalid"
#: A stand-in session token; tests check it never reaches an error.
TOKEN = "session-token-for-tests"


class Response:
    def __init__(self, body: Any) -> None:
        self._body = json.dumps(body).encode()

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def http_error(code: int, body: Any = None) -> urllib.error.HTTPError:
    raw = json.dumps(body).encode() if body is not None else b""
    return urllib.error.HTTPError(
        BASE,
        code,
        "reason",
        {},
        io.BytesIO(raw),  # type: ignore[arg-type]
    )


class Transport:
    """Answers each request with the next of ``answers`` and keeps the requests."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Response(answer)


def tokens(*given: str):
    queue = list(given)
    asked: list[int] = []

    def token() -> str:
        asked.append(1)
        return queue.pop(0) if queue else ""

    return token, asked


EVENT = {"name": "TICK_SECRET", "config": "prd"}


def test_record_posts_the_event_with_the_session_token() -> None:
    t = Transport({"data": {**EVENT, "id": "x"}, "meta": {"count": 1, "total": 1}})
    token, asked = tokens(TOKEN)
    got = api.RotationEvents(BASE + "/", token, opener=t).record(EVENT)
    assert got["id"] == "x"
    (req,) = t.requests
    assert req.get_method() == "POST"
    assert req.full_url == BASE + api.PATH
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert req.headers["User-agent"] == api.USER_AGENT
    assert json.loads(req.data) == EVENT  # type: ignore[arg-type]
    assert len(asked) == 1


def test_the_token_is_asked_once_and_reused() -> None:
    page = {"data": [], "meta": {"total": 0}}
    t = Transport(page, page)
    token, asked = tokens(TOKEN)
    client = api.RotationEvents(BASE, token, opener=t)
    client.list()
    client.list()
    assert len(asked) == 1


def test_a_401_asks_for_a_fresh_token_and_retries() -> None:
    t = Transport(http_error(401), {"data": EVENT})
    token, asked = tokens("expired", TOKEN)
    assert api.RotationEvents(BASE, token, opener=t).record(EVENT) == EVENT
    assert len(asked) == 2
    assert t.requests[-1].headers["Authorization"] == f"Bearer {TOKEN}"


def test_a_blank_token_stops() -> None:
    token, _ = tokens()
    with pytest.raises(api.ApiError, match="no session token"):
        api.RotationEvents(BASE, token, opener=Transport()).record(EVENT)


def test_it_gives_up_after_repeated_401s() -> None:
    t = Transport(*(http_error(401) for _ in range(api.TOKEN_ATTEMPTS)))
    token, _ = tokens(*(TOKEN for _ in range(api.TOKEN_ATTEMPTS)))
    with pytest.raises(api.ApiError) as exc:
        api.RotationEvents(BASE, token, opener=t).record(EVENT)
    assert exc.value.status == 401
    assert TOKEN not in str(exc.value)


def test_an_api_error_carries_its_code_never_the_token() -> None:
    t = Transport(http_error(403, {"error": {"code": "forbidden", "message": "no"}}))
    token, _ = tokens(TOKEN)
    with pytest.raises(api.ApiError) as exc:
        api.RotationEvents(BASE, token, opener=t).record(EVENT)
    assert exc.value.status == 403
    assert "forbidden: no" in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_a_validation_error_names_the_field_not_the_input() -> None:
    body = {"detail": [{"loc": ["body", "actor"], "msg": "bad", "input": "x"}]}
    t = Transport(http_error(422, body))
    token, _ = tokens(TOKEN)
    with pytest.raises(api.ApiError, match=r"body\.actor: bad") as exc:
        api.RotationEvents(BASE, token, opener=t).record(EVENT)
    assert "'x'" not in str(exc.value)


def test_an_error_without_a_body_gives_the_reason() -> None:
    t = Transport(http_error(500))
    token, _ = tokens(TOKEN)
    with pytest.raises(api.ApiError, match="500: reason"):
        api.RotationEvents(BASE, token, opener=t).record(EVENT)


def test_an_unreachable_api_is_an_api_error() -> None:
    t = Transport(urllib.error.URLError("down"))
    token, _ = tokens(TOKEN)
    with pytest.raises(api.ApiError, match="no response: down"):
        api.RotationEvents(BASE, token, opener=t).record(EVENT)


def test_list_reads_every_page_with_its_filters() -> None:
    first = [{"n": i} for i in range(api.PAGE)]
    t = Transport(
        {"data": first, "meta": {"total": api.PAGE + 1}},
        {"data": [{"n": "last"}], "meta": {"total": api.PAGE + 1}},
    )
    token, _ = tokens(TOKEN)
    got = api.RotationEvents(BASE, token, opener=t).list(
        name="TICK_SECRET", config="dev", latest=True
    )
    assert len(got) == api.PAGE + 1
    queries = [
        dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.full_url).query))
        for r in t.requests
    ]
    assert queries[0] == {
        "limit": str(api.PAGE),
        "name": "TICK_SECRET",
        "config": "dev",
        "latest": "true",
        "offset": "0",
    }
    assert queries[1]["offset"] == str(api.PAGE)


def test_list_stops_on_an_empty_page() -> None:
    t = Transport({"data": [], "meta": {"total": 7}})
    token, _ = tokens(TOKEN)
    assert api.RotationEvents(BASE, token, opener=t).list() == []
