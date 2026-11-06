"""rotate.py redeploys the services a rotation lists, and waits for them."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from rotator_cog import railway

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rotate.py"


@pytest.fixture(scope="module")
def rotate() -> Any:
    spec = importlib.util.spec_from_file_location("rotate_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["rotate_script"] = module
    spec.loader.exec_module(module)
    return module


class FakeRailway:
    """A Railway that records what it was asked, and can fail a deployment."""

    def __init__(self, *, environment: str = "production", fail: bool = False):
        self.environment = environment
        self.fail = fail
        self.waited: list[tuple[str, dict[str, str | None]]] = []
        self.redeployed: list[str] = []
        self.deployments: list[list[str]] = []

    def environment_name(self) -> str:
        return self.environment

    def service_ids(self) -> dict[str, str]:
        return {"api-kaianolevine-com": "svc-api", "db-backup": "svc-backup"}

    def wait_for_variables(
        self, service_id: str, expected: dict[str, str | None], *, timeout: float
    ) -> None:
        self.waited.append((service_id, expected))

    def redeploy(self, service_id: str) -> str:
        self.redeployed.append(service_id)
        return f"dep-{service_id}"

    def wait_for_deployments(self, ids: list[str], *, timeout: float) -> None:
        self.deployments.append(ids)
        if self.fail:
            raise railway.RailwayError(f"deployment {ids[0]} ended FAILED")


INVENTORY = {
    "doppler_project": "mini-app-polis-secrets",
    "secrets": {
        "BREVO_API_KEY": {
            "kind": "manual",
            "tier": 3,
            "configs": ["prd"],
            "redeploy": ["api-kaianolevine-com"],
            "steps": "Brevo > SMTP & API > API keys > Generate",
            "revoke": "delete the old key",
        }
    },
}


def _args(**kw: Any) -> argparse.Namespace:
    base = dict(
        names=["BREVO_API_KEY"],
        tier=None,
        kind=None,
        railway_exposed=False,
        value_file=None,
        compromised=False,
        config="prd",
        dry_run=False,
        reason="scheduled",
        api=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def fakes(rotate: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"stored": [], "recorded": [], "redeploys": []}
    monkeypatch.setattr(rotate, "confirm", lambda prompt: True)
    monkeypatch.setattr(rotate.getpass, "getpass", lambda prompt: "new-value")
    monkeypatch.setattr(
        rotate,
        "doppler_set",
        lambda project, config, name, value: seen["stored"].append(name),
    )
    monkeypatch.setattr(
        rotate,
        "write_events",
        lambda args, record: seen["recorded"].extend(record) or True,
    )
    monkeypatch.setattr(rotate, "actor", lambda: "owner")
    return seen


def test_the_services_are_redeployed_once_they_show_the_new_values(
    rotate: Any,
) -> None:
    fake = FakeRailway()
    rotate.railway_redeploy(
        "prd",
        {"api-kaianolevine-com": {"BREVO_API_KEY": "digest"}, "db-backup": {}},
        token_reader=lambda config: "token",
        railway_factory=lambda token: fake,
    )
    assert fake.waited == [("svc-api", {"BREVO_API_KEY": "digest"})]
    assert fake.redeployed == ["svc-api", "svc-backup"]
    assert fake.deployments == [["dep-svc-api", "dep-svc-backup"]]


@pytest.mark.parametrize(
    ("fake", "services", "message"),
    [
        (FakeRailway(fail=True), {"api-kaianolevine-com": {}}, "ended FAILED"),
        (
            FakeRailway(environment="development"),
            {"api-kaianolevine-com": {}},
            "reaches development",
        ),
        (FakeRailway(), {"watcher-cog": {}}, "not in Railway production"),
    ],
)
def test_a_redeploy_that_cannot_finish_raises_naming_what(
    rotate: Any, fake: FakeRailway, services: dict, message: str
) -> None:
    with pytest.raises(railway.RailwayError, match=message):
        rotate.railway_redeploy(
            "prd",
            services,
            token_reader=lambda config: "token",
            railway_factory=lambda token: fake,
        )


def test_a_rotation_redeploys_with_the_stored_values_digest_and_asks_nothing(
    rotate: Any, fakes: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rotate,
        "railway_redeploy",
        lambda config, services: fakes["redeploys"].append((config, services)),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": pytest.fail(f"asked for Enter: {prompt}"),
    )
    rotate.cmd_rotate(_args(), INVENTORY)
    assert fakes["redeploys"] == [
        (
            "prd",
            {"api-kaianolevine-com": {"BREVO_API_KEY": railway.digest("new-value")}},
        )
    ]
    (event,) = fakes["recorded"]
    assert event["name"] == "BREVO_API_KEY"


def test_a_failed_redeploy_is_printed_and_waits_for_a_person(
    rotate: Any,
    fakes: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(config: str, services: Any) -> None:
        raise railway.RailwayError("deployment dep-svc-api ended FAILED")

    asked: list[str] = []
    monkeypatch.setattr(rotate, "railway_redeploy", fail)
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt))
    rotate.cmd_rotate(_args(), INVENTORY)
    out = capsys.readouterr().out
    assert "redeploy failed: deployment dep-svc-api ended FAILED" in out
    assert "Redeploy by hand on Railway (prd): api-kaianolevine-com" in out
    assert asked == ["press Enter once they are redeployed "]
    assert "new-value" not in out
    assert len(fakes["recorded"]) == 1


def test_a_dry_run_names_the_services_and_redeploys_nothing(
    rotate: Any,
    fakes: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        rotate,
        "railway_redeploy",
        lambda *a, **kw: pytest.fail("a dry run redeployed"),
    )
    assert rotate.cmd_rotate(_args(dry_run=True), INVENTORY) == 0
    assert "then redeploy: api-kaianolevine-com" in capsys.readouterr().out
    assert fakes["stored"] == [] and fakes["recorded"] == []
