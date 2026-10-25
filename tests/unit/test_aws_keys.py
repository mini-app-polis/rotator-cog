"""Rotating an IAM user's access key, with AWS, Doppler and Railway faked."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest
from mini_app_polis.api.contract import RotationRequest
from test_doppler import FakeDoppler

from rotator_cog import aws_keys, doppler, worker

NOW = dt.datetime(2026, 10, 25, 14, 0, tzinfo=dt.UTC)
TOKEN = "dp.st.token-for-tests"
NAME, ID_VAR = "DB_POLLER_AWS_SECRET_ACCESS_KEY", "DB_POLLER_AWS_ACCESS_KEY_ID"
OLD_ID, NEW_ID = "AKIAOLDOLDOLDOLD1111", "AKIANEWNEWNEWNEW2222"
OLD_SECRET, NEW_SECRET = "old-secret-for-tests", "new-secret-for-tests"
ENTRY = {
    "kind": "aws-access-key",
    "iam_user": "db-poller",
    "id_var": ID_VAR,
    "redeploy": ["db-poller"],
}
INVENTORY = {"doppler_project": "proj", "secrets": {NAME: ENTRY}}


class FakeIam:
    def __init__(self, keys: list[str] | None = None) -> None:
        self.keys = {k: "Active" for k in (keys if keys is not None else [OLD_ID])}
        self.calls: list[tuple] = []

    def list_access_keys(self, UserName: str) -> dict[str, Any]:
        self.calls.append(("list", UserName))
        return {
            "AccessKeyMetadata": [
                {"AccessKeyId": k, "Status": s} for k, s in self.keys.items()
            ]
        }

    def create_access_key(self, UserName: str) -> dict[str, Any]:
        self.calls.append(("create", UserName))
        self.keys[NEW_ID] = "Active"
        return {"AccessKey": {"AccessKeyId": NEW_ID, "SecretAccessKey": NEW_SECRET}}

    def update_access_key(self, UserName: str, AccessKeyId: str, Status: str) -> None:
        self.calls.append(("update", AccessKeyId, Status))
        self.keys[AccessKeyId] = Status

    def delete_access_key(self, UserName: str, AccessKeyId: str) -> None:
        self.calls.append(("delete", AccessKeyId))
        del self.keys[AccessKeyId]


def _run(
    iam: FakeIam,
    fake: FakeDoppler,
    mode: str = "routine",
    *,
    reason: str | None = None,
    check: Any = None,
) -> tuple[list[Any], list[Any]]:
    steps: list[Any] = []
    report: list[Any] = []

    class Report:
        def issue(self, why: str, item: Any = None, **kw: Any) -> None:
            report.append(("issue", why, item, kw.get("detail")))

        def updated(self, kind: str, item: str) -> None:
            report.append(("updated", kind, item))

    def redeploy(cfg: str, targets: Any) -> None:
        steps.append(("redeploy", dict(iam.keys), targets))

    def verify(user: str, key_id: str, secret: str) -> str | None:
        steps.append(("verify", user, key_id, secret == NEW_SECRET))
        return reason

    worker._rotate(
        RotationRequest(name=NAME, config="prd", mode=mode),  # type: ignore[arg-type]
        ENTRY,
        TOKEN,
        inventory=INVENTORY,
        report=Report(),
        redeploy=redeploy,
        verify=lambda *a: pytest.fail("machine verifier used"),
        record=lambda event: steps.append(("record", event)),
        doppler_factory=lambda project, cfg, token: doppler.Doppler(
            project, cfg, token, transport=httpx.MockTransport(fake.handle)
        ),
        now=lambda: NOW,
        iam_factory=lambda: iam,
        aws_verify=verify,
        check=check,
    )
    return steps, report


def _fake() -> FakeDoppler:
    return FakeDoppler({NAME: OLD_SECRET, ID_VAR: OLD_ID})


def test_a_key_the_services_are_not_ready_for_is_not_created() -> None:
    iam, fake = FakeIam(), _fake()
    asked: list[Any] = []

    def check(cfg: str, needs: Any) -> list[str]:
        asked.append({s: list(n) for s, n in needs.items()})
        return [f"db-poller in production does not reference {ID_VAR}"]

    steps, report = _run(iam, fake, check=check)
    assert asked == [{"db-poller": [ID_VAR, NAME]}]
    assert iam.keys == {OLD_ID: "Active"} and fake.writes() == [] and steps == []
    assert [r[1] for r in report] == ["not ready to rotate; nothing was written"]


def test_a_routine_rotation_replaces_the_key_and_deletes_the_old_after_verifying() -> (
    None
):
    iam, fake = FakeIam(), _fake()
    steps, report = _run(iam, fake)
    assert fake.secrets == {NAME: NEW_SECRET, ID_VAR: NEW_ID}
    assert len(fake.writes()) == 1
    # While the services redeploy, the old key still works.
    (redeploy,) = [s for s in steps if s[0] == "redeploy"]
    assert redeploy[1] == {OLD_ID: "Active", NEW_ID: "Active"}
    assert redeploy[2] == {"db-poller": {ID_VAR: NEW_ID, NAME: NEW_SECRET}}
    assert ("verify", "db-poller", NEW_ID, True) in steps
    assert iam.keys == {NEW_ID: "Active"}
    (event,) = [s[1] for s in steps if s[0] == "record"]
    assert event.aws_key_last4 == "2222" and event.retired == NOW
    assert event.verification == "ok"
    assert report == [("updated", "rotated", f"{NAME} in prd (key …2222)")]


def test_a_user_with_two_keys_is_refused_before_anything_is_created() -> None:
    iam, fake = FakeIam([OLD_ID, "AKIAOTHEROTHEROTH3333"]), _fake()
    steps, report = _run(iam, fake)
    assert not [c for c in iam.calls if c[0] == "create"]
    assert steps == [] and fake.writes() == []
    ((kind, why, item, detail),) = report
    assert "already has two access keys" in why
    assert detail == "…1111 Active, …3333 Active"
    assert OLD_ID not in str(report)


def test_a_compromised_rotation_kills_the_old_key_at_once() -> None:
    iam, fake = FakeIam(), _fake()
    steps, _ = _run(iam, fake, "compromised")
    assert iam.calls[2:4] == [("update", OLD_ID, "Inactive"), ("delete", OLD_ID)]
    # Gone before the services were even redeployed.
    (redeploy,) = [s for s in steps if s[0] == "redeploy"]
    assert redeploy[1] == {NEW_ID: "Active"}
    (event,) = [s[1] for s in steps if s[0] == "record"]
    assert event.mode == "compromised" and event.retired == NOW


def test_a_failed_check_leaves_the_old_key_active_and_says_so() -> None:
    iam, fake = FakeIam(), _fake()
    steps, report = _run(iam, fake, reason="sts refused the new key: ClientError")
    assert iam.keys == {OLD_ID: "Active", NEW_ID: "Active"}
    (event,) = [s[1] for s in steps if s[0] == "record"]
    assert event.verification == "failed" and event.retired is None
    assert report[0][1] == "verification failed; the old key is left active"


def test_a_doppler_write_that_fails_takes_the_new_key_back() -> None:
    iam = FakeIam()
    fake = FakeDoppler({NAME: OLD_SECRET, ID_VAR: OLD_ID}, status=500)
    with pytest.raises(doppler.DopplerError):
        _run(iam, fake)
    assert iam.keys == {OLD_ID: "Active"}


def test_no_record_or_report_carries_a_whole_key_id_or_a_secret() -> None:
    iam, fake = FakeIam(), _fake()
    steps, report = _run(iam, fake)
    (event,) = [s[1] for s in steps if s[0] == "record"]
    text = json.dumps(event.model_dump(mode="json")) + str(report)
    for leaked in (OLD_ID, NEW_ID, OLD_SECRET, NEW_SECRET):
        assert leaked not in text


def test_an_aws_key_entry_must_name_its_user_and_id_variable() -> None:
    job = RotationRequest(name=NAME, config="prd", mode="routine")
    with pytest.raises(worker.InvalidJob, match="iam_user and id_var"):
        worker.validate(job, {"secrets": {NAME: {"kind": "aws-access-key"}}})
    assert worker.validate(job, INVENTORY)["iam_user"] == "db-poller"


# -- the check ------------------------------------------------------------------


class FakeSts:
    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers

    def get_caller_identity(self) -> dict[str, str]:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"Arn": answer}


def _verify(answers: list[Any], waits: list[float] | None = None) -> str | None:
    made: list[tuple] = []

    def factory(key_id: str, secret: str) -> FakeSts:
        made.append((key_id, secret))
        return FakeSts(answers)

    check = aws_keys.sts_verifier(
        sts_factory=factory, sleep=(waits if waits is not None else []).append
    )
    result = check("db-poller", NEW_ID, NEW_SECRET)
    assert made == [(NEW_ID, NEW_SECRET)]
    return result


def test_a_key_that_names_its_user_is_verified() -> None:
    assert _verify(["arn:aws:iam::400200465748:user/db-poller"]) is None


def test_a_new_key_is_given_time_to_start_working() -> None:
    waits: list[float] = []
    arn = "arn:aws:iam::400200465748:user/db-poller"
    assert _verify([RuntimeError("InvalidClientTokenId"), arn], waits) is None
    assert waits == [aws_keys.VERIFY_WAIT_SECONDS]


def test_a_key_for_another_identity_fails() -> None:
    got = _verify(["arn:aws:iam::400200465748:user/someone-else"])
    assert got == "the new key belongs to another identity, not db-poller"


def test_a_key_that_never_works_fails_naming_only_the_error_type() -> None:
    errors = [RuntimeError(NEW_ID)] * aws_keys.VERIFY_ATTEMPTS
    got = _verify(list(errors))
    assert got == "sts refused the new key: RuntimeError"
