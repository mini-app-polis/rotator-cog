"""TURNSTILE_SECRET_KEY, rotated through Cloudflare's API.

Cloudflare mints a Turnstile widget's secret itself: ``rotate_secret`` asks
for a new one and returns it, once. A routine rotation keeps the old secret
valid for Cloudflare's grace period of two hours, long enough for the API to
take the new one, so a Turnstile secret needs no ``_PREVIOUS`` slot: its old
value retires itself. A compromised rotation invalidates the old secret at
once.

The Cloudflare token's only permission is Account > Turnstile Sites: Edit.
It is read from Parameter Store when a rotation runs, and is never logged or
put in an error, nor is a secret.

**The check.** ``siteverify`` with the new secret and a response token that
cannot be valid must refuse the response, not the secret: Cloudflare names
``invalid-input-response`` for a secret it knows and ``invalid-input-secret``
for one it does not. That proves Cloudflare holds the secret the API now
reads; the redeploy before it proves the API holds it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

import httpx
from pydantic import BaseModel, Field, ValidationError

#: How long a routine rotation keeps the old secret valid.
GRACE = timedelta(hours=2)

API = "https://api.cloudflare.com/client/v4"
SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
#: Cloudflare blocks default library user agents.
USER_AGENT = "mini-app-polis-rotator/1"
TIMEOUT_SECONDS = 15.0

#: A response token no widget ever issued: siteverify refuses it, and in
#: refusing it says whether it knew the secret.
CHECK_RESPONSE = "rotator-cog-secret-check"
#: A new secret can take a moment to reach siteverify; so can an answer
#: after a 5xx.
VERIFY_ATTEMPTS = 6
VERIFY_WAIT_SECONDS = 5.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class TurnstileError(RuntimeError):
    """Cloudflare refused, or could not be asked. Carries no token or secret."""


class _Rotated(BaseModel):
    secret: str = Field(..., min_length=1, description="The widget's new secret.")


class _RotateAnswer(BaseModel):
    success: bool = Field(..., description="Whether Cloudflare rotated it.")
    result: _Rotated | None = Field(default=None, description="The widget.")


class _VerifyAnswer(BaseModel):
    success: bool = Field(..., description="Whether the response passed.")
    error_codes: list[str] = Field(
        default_factory=list, alias="error-codes", description="Why not."
    )


def rotate_secret(
    account_id: str,
    sitekey: str,
    token: str,
    *,
    invalidate_immediately: bool,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """The widget's new secret. The old one lasts GRACE unless invalidated now."""
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = client.post(
                f"{API}/accounts/{account_id}/challenges/widgets/{sitekey}/rotate_secret",
                json={"invalidate_immediately": invalidate_immediately},
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": USER_AGENT,
                },
            )
    except httpx.HTTPError as exc:
        raise TurnstileError(
            f"Cloudflare's rotate-secret could not be asked: {type(exc).__name__}"
        ) from None
    if response.status_code != 200:
        raise TurnstileError(
            f"Cloudflare's rotate-secret answered HTTP {response.status_code}"
        )
    try:
        answer = _RotateAnswer.model_validate_json(response.content)
    except ValidationError:
        raise TurnstileError(
            "Cloudflare's rotate-secret answered without a secret"
        ) from None
    if not answer.success or answer.result is None:
        raise TurnstileError("Cloudflare's rotate-secret did not succeed")
    return answer.result.secret


def siteverify_verifier(
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[str], str | None]:
    """Check a secret against siteverify: None when Cloudflare knows it."""

    def verify(secret: str) -> str | None:
        failure = "no answer"
        for attempt in range(VERIFY_ATTEMPTS):
            if attempt:
                sleep(VERIFY_WAIT_SECONDS)
            try:
                with httpx.Client(
                    timeout=TIMEOUT_SECONDS, transport=transport
                ) as client:
                    response = client.post(
                        SITEVERIFY,
                        data={"secret": secret, "response": CHECK_RESPONSE},
                        headers={"User-Agent": USER_AGENT},
                    )
            except httpx.HTTPError as exc:
                failure = f"siteverify could not be asked: {type(exc).__name__}"
                continue
            if response.status_code in RETRY_STATUSES:
                failure = f"siteverify answered HTTP {response.status_code}"
                continue
            if response.status_code != 200:
                return f"siteverify answered HTTP {response.status_code}"
            try:
                answer = _VerifyAnswer.model_validate_json(response.content)
            except ValidationError:
                return "siteverify answered something unreadable"
            codes = set(answer.error_codes)
            if "invalid-input-secret" in codes or "missing-input-secret" in codes:
                # Not there yet, or not a secret Cloudflare knows: ask again.
                failure = "siteverify does not know the new secret"
                continue
            if "invalid-input-response" in codes:
                return None
            return (
                "siteverify answered "
                f"{', '.join(sorted(codes)) or 'without a refusal'}, "
                "not invalid-input-response"
            )
        return failure

    return verify
