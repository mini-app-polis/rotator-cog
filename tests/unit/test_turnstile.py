"""TURNSTILE_SECRET_KEY: Cloudflare's rotate-secret, siteverify, and the worker's rotation.

A fake Cloudflare answers both; no request leaves the test.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest
from mini_app_polis.api.contract import RotationRequest
from test_doppler import FakeDoppler

from rotator_cog import doppler, turnstile, worker

#: Stand-ins the tests check never reach an error, a report or a log line.
CF_TOKEN = "cf-token-for-tests"
OLD, NEW = "0x4AAA-old-secret", "0x4AAA-new-secret"
NAME = "TURNSTILE_SECRET_KEY"
NOW = dt.datetime(2026, 10, 27, 14, 0, tzinfo=dt.UTC)

ENTRY = {
    "kind": "provider-api",
    "provider": "cloudflare-turnstile",
    "redeploy": ["api-kaianolevine-com"],
}
INVENTORY = {"secrets": {NAME: ENTRY}, "doppler_project": "proj"}


class FakeCloudflare:
    """rotate_secret and siteverify, and every request made."""

    def __init__(
        self,
        *,
        rotate_status: int = 200,
        rotated: dict[str, Any] | None = None,
        verify_codes: list[list[str]] | None = None,
        verify_status: int = 200,
    ) -> None:
        self.rotate_status = rotate_status
        self.rotated = (
            rotated
            if rotated is not None
            else {
                "success": True,
                "result": {"sitekey": "0x4AAA-site", "secret": NEW},
            }
        )
        self.verify_codes = verify_codes or [["invalid-input-response"]]
        self.verify_status = verify_status
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/rotate_secret"):
            if self.rotate_status != 200:
                return httpx.Response(
                    self.rotate_status, json={"success": False, "errors": []}
                )
            return httpx.Response(200, json=self.rotated)
        codes = (
            self.verify_codes.pop(0)
            if len(self.verify_codes) > 1
            else self.verify_codes[0]
        )
        return httpx.Response(
            self.verify_status, json={"success": False, "error-codes": codes}
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


# -- rotate_secret -----------------------------------------------------------


def test_rotate_secret_asks_for_the_widget_and_returns_its_new_secret() -> None:
    cf = FakeCloudflare()
    secret = turnstile.rotate_secret(
        "acct",
        "0x4AAA-site",
        CF_TOKEN,
        invalidate_immediately=False,
        transport=cf.transport(),
    )
    assert secret == NEW
    (request,) = cf.requests
    assert request.method == "POST"
    assert request.url.path == (
        "/client/v4/accounts/acct/challenges/widgets/0x4AAA-site/rotate_secret"
    )
    assert request.headers["Authorization"] == f"Bearer {CF_TOKEN}"
    assert json.loads(request.content) == {"invalidate_immediately": False}


def test_a_compromised_rotation_invalidates_the_old_secret_at_once() -> None:
    cf = FakeCloudflare()
    turnstile.rotate_secret(
        "acct", "site", CF_TOKEN, invalidate_immediately=True, transport=cf.transport()
    )
    assert json.loads(cf.requests[0].content) == {"invalidate_immediately": True}


@pytest.mark.parametrize(
    ("cf", "reason"),
    [
        (FakeCloudflare(rotate_status=403), "answered HTTP 403"),
        (FakeCloudflare(rotated={"success": True, "result": {}}), "without a secret"),
        (FakeCloudflare(rotated={"success": False}), "did not succeed"),
    ],
)
def test_a_refused_rotation_says_why_without_the_token(
    cf: FakeCloudflare, reason: str
) -> None:
    with pytest.raises(turnstile.TurnstileError, match=reason) as raised:
        turnstile.rotate_secret(
            "acct",
            "site",
            CF_TOKEN,
            invalidate_immediately=False,
            transport=cf.transport(),
        )
    assert CF_TOKEN not in str(raised.value)


def test_a_cloudflare_that_cannot_be_reached_is_named_by_the_error_type() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused, token {CF_TOKEN}")

    with pytest.raises(turnstile.TurnstileError, match="ConnectError") as raised:
        turnstile.rotate_secret(
            "acct",
            "site",
            CF_TOKEN,
            invalidate_immediately=False,
            transport=httpx.MockTransport(down),
        )
    assert CF_TOKEN not in str(raised.value)


# -- siteverify --------------------------------------------------------------


def _verifier(cf: FakeCloudflare) -> Any:
    return turnstile.siteverify_verifier(transport=cf.transport(), sleep=lambda s: None)


def test_a_secret_siteverify_knows_is_verified() -> None:
    cf = FakeCloudflare()
    assert _verifier(cf)(NEW) is None
    (request,) = cf.requests
    assert request.url == turnstile.SITEVERIFY
    form = dict(httpx.QueryParams(request.content.decode()))
    assert form == {"secret": NEW, "response": turnstile.CHECK_RESPONSE}


def test_a_secret_not_there_yet_is_asked_again() -> None:
    cf = FakeCloudflare(
        verify_codes=[["invalid-input-secret"], ["invalid-input-response"]]
    )
    assert _verifier(cf)(NEW) is None
    assert len(cf.requests) == 2


def test_a_secret_siteverify_never_knows_fails_after_every_attempt() -> None:
    cf = FakeCloudflare(verify_codes=[["invalid-input-secret"]])
    reason = _verifier(cf)(NEW)
    assert reason == "siteverify does not know the new secret"
    assert len(cf.requests) == turnstile.VERIFY_ATTEMPTS
    assert NEW not in reason


def test_any_other_answer_is_a_failed_check_without_the_secret() -> None:
    reason = _verifier(FakeCloudflare(verify_codes=[["internal-error"]]))(NEW)
    assert reason is not None and "internal-error" in reason and NEW not in reason
    reason = _verifier(FakeCloudflare(verify_status=400))(NEW)
    assert reason == "siteverify answered HTTP 400"


# -- the worker's rotation ---------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def issue(self, reason: str, item: str | None = None, **kw: Any) -> None:
        self.lines.append(("issue", reason))

    def updated(self, kind: str, item: str) -> None:
        self.lines.append(("updated", kind))


def _rotate(
    fake: FakeDoppler,
    mode: str = "routine",
    *,
    mint: Any = None,
    verified: str | None = None,
    redeploy_fails: bool = False,
    check: Any = None,
) -> tuple[Report, list[Any]]:
    steps: list[Any] = []
    report = Report()

    def mint_secret(invalidate_now: bool) -> str:
        steps.append(("mint", invalidate_now))
        return NEW

    def redeploy(cfg: str, targets: Any) -> None:
        steps.append(("redeploy", cfg, dict(fake.secrets), targets))
        if redeploy_fails:
            raise RuntimeError("redeploy failed")

    def verify(secret: str) -> str | None:
        steps.append(("verify", secret == fake.secrets[NAME]))
        return verified

    job = RotationRequest(name=NAME, config="prd", mode=mode)  # type: ignore[arg-type]
    worker._rotate(
        job,
        ENTRY,
        "dp.st.token",
        inventory=INVENTORY,
        report=report,
        redeploy=redeploy,
        record=lambda event: steps.append(("record", event)),
        doppler_factory=lambda project, cfg, token: doppler.Doppler(
            project, cfg, token, transport=httpx.MockTransport(fake.handle)
        ),
        now=lambda: NOW,
        check=check,
        provider_mint=mint or mint_secret,
        provider_verify=verify,
    )
    return report, steps


def test_a_turnstile_job_is_one_the_worker_takes() -> None:
    job = RotationRequest(name=NAME, config="prd", mode="routine")  # type: ignore[arg-type]
    assert worker.validate(job, INVENTORY)["kind"] == "provider-api"
    other = {"secrets": {NAME: {**ENTRY, "provider": "elsewhere"}}}
    with pytest.raises(worker.InvalidJob, match="provider elsewhere"):
        worker.validate(job, other)


def test_the_inventory_has_the_worker_rotate_turnstile_through_cloudflare() -> None:
    entry = worker.load_inventory()["secrets"][NAME]
    assert entry["kind"] == "provider-api"
    assert entry["provider"] in worker.PROVIDERS
    assert not entry.get("steps")


def test_a_routine_rotation_is_one_write_one_redeploy_and_the_check() -> None:
    fake = FakeDoppler({NAME: OLD})
    report, steps = _rotate(fake)
    assert fake.secrets == {NAME: NEW}
    assert len(fake.writes()) == 1
    assert steps[0] == ("mint", False)
    assert steps[1] == (
        "redeploy",
        "prd",
        {NAME: NEW},
        {"api-kaianolevine-com": {NAME: NEW}},
    )
    assert steps[2] == ("verify", True)
    event = steps[3][1]
    assert (event.name, event.kind, event.mode, event.actor) == (
        NAME,
        "provider-api",
        "routine",
        "rotator-cog",
    )
    # The old secret lasts Cloudflare's grace period, then retires itself.
    assert event.minted == NOW and event.retired == NOW + turnstile.GRACE
    assert event.verification == "ok"
    assert report.lines == [("updated", "rotated")]


def test_a_compromised_rotation_invalidates_the_old_secret_as_it_mints() -> None:
    fake = FakeDoppler({NAME: OLD})
    report, steps = _rotate(fake, "compromised")
    assert steps[0] == ("mint", True)
    event = steps[-1][1]
    assert event.mode == "compromised" and event.retired == NOW
    assert report.lines == [("updated", "rotated")]


def test_a_failed_check_is_recorded_failed_and_reported() -> None:
    report, steps = _rotate(
        FakeDoppler({NAME: OLD}), verified="siteverify does not know the new secret"
    )
    event = steps[-1][1]
    assert event.verification == "failed"
    assert event.verification_reason == "siteverify does not know the new secret"
    assert report.lines == [("issue", "verification failed")]


def test_a_refused_mint_writes_nothing_and_is_recorded_failed() -> None:
    fake = FakeDoppler({NAME: OLD})

    def refused(invalidate_now: bool) -> str:
        raise turnstile.TurnstileError("Cloudflare's rotate-secret answered HTTP 403")

    with pytest.raises(turnstile.TurnstileError):
        _rotate(fake, mint=refused)
    assert fake.secrets == {NAME: OLD} and fake.writes() == []


def test_a_secret_that_cannot_be_stored_says_when_the_old_one_stops() -> None:
    fake = FakeDoppler({NAME: OLD})
    fake.status = 200
    report = Report()
    recorded: list[Any] = []

    class Unwritable(doppler.Doppler):
        def change(self, name: str, changes: list[dict[str, Any]]) -> None:
            raise doppler.DopplerError("Doppler answered HTTP 500")

    job = RotationRequest(name=NAME, config="prd", mode="routine")  # type: ignore[arg-type]
    with pytest.raises(doppler.DopplerError):
        worker._rotate(
            job,
            ENTRY,
            "dp.st.token",
            inventory=INVENTORY,
            report=report,
            redeploy=lambda *a: pytest.fail("redeployed"),
            record=recorded.append,
            doppler_factory=lambda project, cfg, token: Unwritable(
                project, cfg, token, transport=httpx.MockTransport(fake.handle)
            ),
            now=lambda: NOW,
            provider_mint=lambda now: NEW,
            provider_verify=lambda s: None,
        )
    (line,) = report.lines
    assert line[0] == "issue" and "16:00 UTC" in line[1]
    assert recorded[0].verification_reason == "Doppler answered HTTP 500"


def test_a_secret_missing_from_doppler_mints_nothing() -> None:
    report, steps = _rotate(FakeDoppler())
    assert report.lines == [("issue", "not in Doppler")]
    assert steps == []


def test_a_rotation_the_services_are_not_ready_for_mints_nothing() -> None:
    report, steps = _rotate(
        FakeDoppler({NAME: OLD}),
        check=lambda cfg, needs: [f"needs {needs}"],
    )
    assert report.lines == [("issue", "not ready to rotate; nothing was written")]
    assert steps == []


def test_without_the_provider_steps_nothing_is_minted() -> None:
    report = Report()
    worker._rotate(
        RotationRequest(name=NAME, config="prd", mode="routine"),  # type: ignore[arg-type]
        ENTRY,
        "dp.st.token",
        inventory=INVENTORY,
        report=report,
        redeploy=lambda *a: None,
        record=lambda e: None,
        doppler_factory=lambda *a: pytest.fail("Doppler reached"),
    )
    assert report.lines == [("issue", "rotation not built yet")]


def test_no_secret_or_token_reaches_a_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("DEBUG"):
        _rotate(FakeDoppler({NAME: OLD}))
    assert NEW not in caplog.text and OLD not in caplog.text


# -- the Cloudflare token and widget -------------------------------------------


def test_the_minter_reads_the_token_and_widget_when_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cf = FakeCloudflare()
    read: list[str] = []

    def token_reader(var: str) -> str:
        read.append(var)
        return CF_TOKEN

    mint = worker.cloudflare_turnstile(
        token_reader=token_reader, transport=cf.transport()
    )
    assert read == []
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("TURNSTILE_SITEKEY", "0x4AAA-site")
    assert mint(False) == NEW
    assert read == ["CLOUDFLARE_TURNSTILE_TOKEN_PARAMETER"]
    assert "/accounts/acct/challenges/widgets/0x4AAA-site/" in str(cf.requests[0].url)


def test_a_minter_with_no_widget_named_asks_cloudflare_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("TURNSTILE_SITEKEY", raising=False)
    mint = worker.cloudflare_turnstile(
        token_reader=lambda var: pytest.fail("token read"),
        transport=httpx.MockTransport(lambda r: pytest.fail("Cloudflare asked")),
    )
    with pytest.raises(turnstile.TurnstileError, match="must be set"):
        mint(False)
