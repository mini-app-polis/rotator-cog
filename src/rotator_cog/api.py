"""The rotation record's two endpoints, called as the person running rotate.py.

    POST /v1/secrets/rotation-events   record one rotation, or update it
    GET  /v1/secrets/rotation-events   events, newest minted first

The owner holds these scopes as a person (role wcs-admin), and a person
authenticates to the API with a Clerk session token, nothing else. Session
tokens last about a minute, so ``token`` is asked again whenever the API
answers 401, and the token is never printed, logged or put in an error.

The standard library only, so rotate.py stays a script ``uv run`` can start
with no more than it has today.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

PATH = "/v1/secrets/rotation-events"
#: Cloudflare blocks default library user agents.
USER_AGENT = "mini-app-polis-rotate/1"
#: The API's largest page.
PAGE = 500
#: How many times a 401 asks for a fresh token before giving up.
TOKEN_ATTEMPTS = 3


class ApiError(Exception):
    """The API refused a request, or could not be asked. Never carries the token."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(f"{status or 'no response'}: {message}")
        self.status = status


Opener = Callable[..., Any]


class RotationEvents:
    """Reads and writes rotation events.

    ``token`` returns a session token, or "" to stop; it is called on first
    use and after every 401. ``opener`` is ``urllib.request.urlopen`` unless a
    test passes its own.
    """

    def __init__(
        self,
        base_url: str,
        token: Callable[[], str],
        *,
        opener: Opener = urllib.request.urlopen,
        timeout: float = 30.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token_source = token
        self._token: str | None = None
        self._open = opener
        self._timeout = timeout

    def record(self, event: dict[str, Any]) -> dict[str, Any]:
        """Write one event, or update the one with the same key."""
        return self._call("POST", PATH, body=event)["data"]

    def list(
        self,
        *,
        name: str | None = None,
        config: str | None = None,
        latest: bool = False,
    ) -> list[dict[str, Any]]:
        """Every matching event, newest minted first, across pages."""
        query: dict[str, str] = {"limit": str(PAGE)}
        if name:
            query["name"] = name
        if config:
            query["config"] = config
        if latest:
            query["latest"] = "true"
        out: list[dict[str, Any]] = []
        while True:
            query["offset"] = str(len(out))
            page = self._call("GET", f"{PATH}?{urllib.parse.urlencode(query)}")
            data = page.get("data") or []
            out.extend(data)
            total = (page.get("meta") or {}).get("total", len(out))
            if not data or len(out) >= total:
                return out

    def _call(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body).encode()
        for _ in range(TOKEN_ATTEMPTS):
            if not self._token:
                self._token = self._token_source().strip()
                if not self._token:
                    raise ApiError(None, "no session token given")
            request = urllib.request.Request(
                self._base + path,
                data=payload,
                method=method,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                with self._open(request, timeout=self._timeout) as response:
                    return json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    # Expired, most likely: ask for a fresh one.
                    self._token = None
                    continue
                raise ApiError(exc.code, _error_message(exc)) from None
            except urllib.error.URLError as exc:
                raise ApiError(None, str(exc.reason)) from None
        raise ApiError(401, "the API refused every session token given")


def _error_message(exc: urllib.error.HTTPError) -> str:
    """The API's error code and message, or the HTTP reason."""
    try:
        body = json.loads(exc.read() or b"{}")
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("code"):
        return f"{err['code']}: {err.get('message', '')}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):
        # A validation error: where and why only, never the input it quotes.
        return "; ".join(
            f"{'.'.join(str(p) for p in d.get('loc', []))}: {d.get('msg', '')}"
            for d in detail
            if isinstance(d, dict)
        )
    return str(exc.reason)
