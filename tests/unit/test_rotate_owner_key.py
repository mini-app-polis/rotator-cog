"""rotate.py stops before it changes anything when the owner key is missing."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rotate.py"


@pytest.fixture(scope="module")
def rotate() -> Any:
    spec = importlib.util.spec_from_file_location("rotate_script_owner", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["rotate_script_owner"] = module
    spec.loader.exec_module(module)
    return module


def _args(**kw: Any) -> argparse.Namespace:
    base = {"api": "https://api.example.test", "dry_run": False}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def _fresh(rotate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rotate._record.clear()
    monkeypatch.delenv(rotate.OWNER_KEY_VAR, raising=False)


@pytest.mark.parametrize("cmd", ["rotate", "pg-roles", "verify", "list"])
def test_stops_before_the_command_without_the_owner_key(
    rotate: Any, monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    ran: list[str] = []
    for fn in ("cmd_rotate", "cmd_pg_roles", "cmd_verify", "cmd_list"):
        monkeypatch.setattr(rotate, fn, lambda *a, _fn=fn: ran.append(_fn) or 0)
    with pytest.raises(SystemExit, match=rotate.OWNER_KEY_VAR):
        rotate.dispatch(_args(cmd=cmd), {})
    assert ran == []


@pytest.mark.parametrize(
    ("cmd", "fn", "kw"),
    [("check", "cmd_check", {}), ("rotate", "cmd_rotate", {"dry_run": True})],
)
def test_check_and_dry_run_need_no_owner_key(
    rotate: Any, monkeypatch: pytest.MonkeyPatch, cmd: str, fn: str, kw: dict
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(rotate, fn, lambda *a: ran.append(fn) or 0)
    assert rotate.dispatch(_args(cmd=cmd, **kw), {}) == 0
    assert ran == [fn]
