"""Doppler's API, as the worker writes a rotated secret with a config's token.

    GET  /v3/configs/config/secret    one secret's value, or 404
    POST /v3/configs/config/secrets   change secrets (change_requests)

The token is a service token for one config, sent as a bearer token. A
value passes through here only on its way to or from Doppler: no error
raised here carries a value, a token or a response body (Doppler's answer to
a write echoes the values it holds), and nothing here logs.
"""

from __future__ import annotations

from typing import Any

import httpx

BASE_URL = "https://api.doppler.com"
TIMEOUT_SECONDS = 15.0

#: The slot the API also accepts a key from while callers move to the new one.
PREVIOUS_SUFFIX = "_PREVIOUS"


class DopplerError(RuntimeError):
    """Doppler refused a request, or could not be asked. Names, never values."""


def previous_name(name: str) -> str:
    return f"{name}{PREVIOUS_SUFFIX}"


class Doppler:
    """One config of one project, reached with that config's token."""

    def __init__(
        self,
        project: str,
        config: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.project = project
        self.config = config
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    def _request(
        self, what: str, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise DopplerError(f"{what}: {type(exc).__name__}") from None

    def get(self, name: str) -> str | None:
        """``name``'s value in this config, or None when it is not set."""
        what = f"read {name} in {self.config}"
        response = self._request(
            what,
            "GET",
            "/v3/configs/config/secret",
            params={"project": self.project, "config": self.config, "name": name},
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise DopplerError(f"{what}: HTTP {response.status_code}")
        try:
            raw = response.json()["value"]["raw"]
        except (ValueError, KeyError, TypeError):
            raise DopplerError(f"{what}: unexpected response") from None
        return raw if isinstance(raw, str) and raw else None

    def names(self) -> set[str]:
        """Every secret name in this config (names only; no values are read)."""
        what = f"list the names in {self.config}"
        response = self._request(
            what,
            "GET",
            "/v3/configs/config/secrets/names",
            params={"project": self.project, "config": self.config},
        )
        if response.status_code != 200:
            raise DopplerError(f"{what}: HTTP {response.status_code}")
        try:
            names = response.json()["names"]
        except (ValueError, KeyError, TypeError):
            raise DopplerError(f"{what}: unexpected response") from None
        return {str(n) for n in names}

    def change(self, name: str, changes: list[dict[str, Any]]) -> None:
        """Apply ``changes`` in one request. The response is not kept."""
        what = f"write {name} in {self.config}"
        response = self._request(
            what,
            "POST",
            "/v3/configs/config/secrets",
            json={
                "project": self.project,
                "config": self.config,
                "change_requests": changes,
            },
        )
        if response.status_code != 200:
            raise DopplerError(f"{what}: HTTP {response.status_code}")


def _set(name: str, value: str, *, exists: bool) -> dict[str, Any]:
    return {"name": name, "originalName": name if exists else None, "value": value}


def _delete(name: str) -> dict[str, Any]:
    return {"name": name, "originalName": name, "value": None, "shouldDelete": True}


def stage(doppler: Doppler, name: str, new: str) -> None:
    """Routine, phase 1: the new value in NAME_PREVIOUS; callers keep NAME."""
    previous = previous_name(name)
    exists = doppler.get(previous) is not None
    doppler.change(name, [_set(previous, new, exists=exists)])


def promote(doppler: Doppler, name: str, new: str, old: str) -> None:
    """Routine, phase 2: NAME the new value, NAME_PREVIOUS the old one."""
    doppler.change(
        name,
        [
            _set(name, new, exists=True),
            _set(previous_name(name), old, exists=True),
        ],
    )


def replace(doppler: Doppler, name: str, new: str) -> None:
    """Compromised: NAME the new value and no previous slot, in one write."""
    previous = previous_name(name)
    changes = [_set(name, new, exists=True)]
    if doppler.get(previous) is not None:
        changes.append(_delete(previous))
    doppler.change(name, changes)


def retire(doppler: Doppler, names: list[str]) -> None:
    """Delete these previous slots in one write."""
    doppler.change(", ".join(names), [_delete(previous_name(name)) for name in names])
