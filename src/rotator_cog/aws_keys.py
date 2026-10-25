"""IAM access keys, as the worker rotates one user's key.

AWS allows a user two access keys, so a rotation creates the new key beside
the old one, hands the new one over, checks it, and only then deletes the
old one. A user that already has two keys is refused before anything is
created: one of them has to be dealt with by a person first.

A key id is not secret, but whole key ids are what secret scanners match,
so nothing here (or in any record or report) carries more than an id's last
four characters. A secret access key is never logged, raised or returned
anywhere but to the caller that writes it to Doppler.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from typing import Any

#: How long a new key may take to start working: IAM is eventually
#: consistent, and a key usually works within seconds.
VERIFY_ATTEMPTS = 12
VERIFY_WAIT_SECONDS = 5.0


def last4(key_id: str) -> str:
    """The last four characters of a key id: all of one any record carries."""
    return key_id[-4:]


def existing(iam: Any, user: str) -> list[dict[str, Any]]:
    """The user's access keys: id, status and creation time."""
    keys: list[dict[str, Any]] = iam.list_access_keys(UserName=user)[
        "AccessKeyMetadata"
    ]
    return keys


def describe(keys: list[dict[str, Any]]) -> str:
    """The keys by the last four of their ids and their status."""
    return ", ".join(f"…{last4(k['AccessKeyId'])} {k['Status']}" for k in keys)


def create(iam: Any, user: str) -> tuple[str, str]:
    """A new key for the user: (key id, secret access key)."""
    key = iam.create_access_key(UserName=user)["AccessKey"]
    return str(key["AccessKeyId"]), str(key["SecretAccessKey"])


def delete(iam: Any, user: str, key_id: str) -> None:
    """Delete one of the user's access keys. Nothing can use it again."""
    iam.delete_access_key(UserName=user, AccessKeyId=key_id)


def deactivate(iam: Any, user: str, key_id: str) -> None:
    """Make one of the user's access keys inactive, so it stops working at once."""
    iam.update_access_key(UserName=user, AccessKeyId=key_id, Status="Inactive")


def _sts(key_id: str, secret: str) -> Any:
    return importlib.import_module("boto3").client(
        "sts", aws_access_key_id=key_id, aws_secret_access_key=secret
    )


def sts_verifier(
    *,
    sts_factory: Callable[[str, str], Any] = _sts,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[str, str, str], str | None]:
    """Check a new key: sts get-caller-identity must name its user.

    Needs no IAM permission. None when it works, else why not, in a sentence
    that names no key.
    """

    def verify(user: str, key_id: str, secret: str) -> str | None:
        client = sts_factory(key_id, secret)
        failure = "no answer"
        for attempt in range(VERIFY_ATTEMPTS):
            try:
                arn = str(client.get_caller_identity()["Arn"])
            except Exception as exc:  # noqa: BLE001 — named by its type alone
                failure = type(exc).__name__
                if attempt + 1 < VERIFY_ATTEMPTS:
                    sleep(VERIFY_WAIT_SECONDS)
                continue
            if arn.endswith(f":user/{user}"):
                return None
            return f"the new key belongs to another identity, not {user}"
        return f"sts refused the new key: {failure}"

    return verify
