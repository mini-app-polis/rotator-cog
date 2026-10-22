#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Rotate the fleet's secrets and record that it happened.

    uv run scripts/rotate.py list [--due] [--tier N]
    uv run scripts/rotate.py check [--config prd]
    uv run scripts/rotate.py rotate NAME [NAME ...] --reason TEXT [--compromised] [--config prd] [--dry-run]
    uv run scripts/rotate.py rotate --tier 1 --kind machine-key --reason TEXT ...

secrets/inventory.yaml says what each secret is and how to rotate it;
secrets/rotations.jsonl is the record, one line per rotation, appended here
and committed. Neither file ever holds a value.

Values never pass through stdout, an argument list or a file: a minted value
goes to Doppler (and to GitHub secrets) on stdin, a pasted value is read
with getpass, and verify commands run under `doppler run`, so the value
reaches them as an environment variable.

Routine rotation keeps the old credential alive until the new one verifies.
--compromised kills the old one as soon as the new one is stored, whether
or not anything has picked it up yet: an outage is the price, and overlap is
what an attacker wants.

Needs on PATH: doppler (logged in), and as the secrets require them aws
(profile from AWS_PROFILE, default miniapppolis), gh and curl.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import secrets as pysecrets
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "secrets" / "inventory.yaml"
LEDGER = ROOT / "secrets" / "rotations.jsonl"
AWS_PROFILE = os.environ.get("AWS_PROFILE", "miniapppolis")
USER_AGENT = "mini-app-polis-rotate/1"  # Cloudflare blocks default library UAs


# --------------------------------------------------------------------------
# Inventory and ledger
# --------------------------------------------------------------------------


def load_inventory() -> dict:
    return yaml.safe_load(INVENTORY.read_text())


def entry(inv: dict, name: str) -> dict:
    try:
        e = dict(inv["secrets"][name])
    except KeyError:
        sys.exit(f"{name} is not in {INVENTORY.relative_to(ROOT)}; add it first")
    e.setdefault("configs", ["prd"])
    e.setdefault("redeploy", [])
    e.setdefault("github", [])
    return e


def ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]


def last_rotated(rows: list[dict]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for r in rows:
        key = (r["name"], r["config"])
        out[key] = max(out.get(key, ""), r["at"])
    return out


def record(row: dict) -> None:
    with LEDGER.open("a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def run(cmd: list[str], *, stdin: str | None = None, capture: bool = True) -> str:
    p = subprocess.run(cmd, input=stdin, text=True, capture_output=capture, check=False)
    if p.returncode != 0:
        # Never echo stdin; stderr from these tools does not contain it.
        raise SystemExit(f"failed: {' '.join(cmd)}\n{(p.stderr or '').strip()}")
    return p.stdout if capture else ""


def doppler_set(project: str, config: str, name: str, value: str) -> None:
    run(
        ["doppler", "secrets", "set", name, "--project", project, "--config", config, "--silent"], stdin=value
    )


def doppler_names(project: str, config: str) -> set[str]:
    out = run(["doppler", "secrets", "--project", project, "--config", config, "--only-names", "--json"])
    data = json.loads(out)
    names = set(data) if isinstance(data, (dict, list)) else set()
    return names - {"DOPPLER_PROJECT", "DOPPLER_CONFIG", "DOPPLER_ENVIRONMENT"}


def github_targets(e: dict, name: str, config: str) -> list[tuple[str, str]]:
    """[(flag-args, secret-name)] for entries like 'org:x', 'repo:o/r as NAME @dev'."""
    out = []
    for spec in e["github"]:
        spec_cfg = "prd"
        if " @" in spec:
            spec, spec_cfg = spec.rsplit(" @", 1)
        if spec_cfg != config:
            continue
        gh_name = name
        if " as " in spec:
            spec, gh_name = spec.split(" as ", 1)
        out.append((spec.strip(), gh_name.strip()))
    return out


def gh_set(target: str, gh_name: str, value: str) -> None:
    kind, where = target.split(":", 1)
    if kind == "org":
        args = ["--org", where, "--visibility", "all"]
    else:
        args = ["--repo", where]
    run(["gh", "secret", "set", gh_name, *args], stdin=value)


def aws(*args: str) -> dict:
    return json.loads(run(["aws", "--profile", AWS_PROFILE, "--output", "json", *args]) or "{}")


def verify(project: str, config: str, command: str) -> bool:
    p = subprocess.run(
        ["doppler", "run", "--project", project, "--config", config, "--", "sh", "-c", command], check=False
    )
    return p.returncode == 0


def machine_name(var: str) -> str:
    return var.removesuffix("_API_KEY").lower().replace("_", "-")


def machine_verify(inv: dict, var: str, config: str) -> str:
    base = inv["api_base_url"] if config == "prd" else "${KAIANO_API_BASE_URL_DEV:-$KAIANO_API_BASE_URL}"
    return (
        f'curl -fsS -A {USER_AGENT} -H "Authorization: Bearer ${var}" '
        f'"{base}/v1/identity/whoami" | grep -q \'"{machine_name(var)}"\''
    )


def confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_list(args, inv) -> None:
    seen = last_rotated(ledger())
    today = dt.datetime.now(dt.UTC)
    rows = []
    for name in inv["secrets"]:
        e = entry(inv, name)
        if e["kind"] == "not-rotated" or (args.tier is not None and e["tier"] != args.tier):
            continue
        cadence = inv["cadence_days"].get(e["kind"])
        for cfg in e["configs"] or ["-"]:
            last = seen.get((name, cfg))
            if last:
                age = (today - dt.datetime.fromisoformat(last)).days
                due = cadence is not None and age >= cadence
            else:
                age, due = None, True
            if args.due and not due:
                continue
            rows.append(
                (
                    e["tier"],
                    name,
                    cfg,
                    e["kind"],
                    last[:10] if last else "never",
                    "DUE" if due else f"{cadence - age}d",
                )
            )
    for r in sorted(rows):
        print(f"t{r[0]}  {r[1]:<38} {r[2]:<4} {r[3]:<15} {r[4]:<11} {r[5]}")


def cmd_check(args, inv) -> int:
    names = doppler_names(inv["doppler_project"], args.config)
    known = {n for n, e in inv["secrets"].items()}
    known |= {e["id_var"] for e in inv["secrets"].values() if e.get("id_var")}
    known |= {a for e in inv["secrets"].values() for a in e.get("also", [])}
    # Non-secret settings are fine to leave out; only report names that look secret.
    secretish = (
        "KEY",
        "SECRET",
        "TOKEN",
        "PASSWORD",
        "DSN",
        "WEBHOOK",
        "CREDENTIALS",
        "DATABASE_URL",
        "HEALTHCHECKS_URL",
    )
    missing = sorted(n for n in names - known if any(s in n for s in secretish))
    stale = sorted(
        n
        for n, e in inv["secrets"].items()
        if args.config in (e.get("configs") or ["prd"]) and n not in names
    )
    for n in missing:
        print(f"not in inventory: {n}")
    for n in stale:
        print(f"in inventory, not in Doppler {args.config}: {n}")
    if not (missing or stale):
        print(f"inventory matches Doppler {args.config}")
    return 1 if missing else 0


def cmd_rotate(args, inv) -> int:
    project = inv["doppler_project"]
    names = list(args.names)
    if args.tier is not None or args.kind or args.railway_exposed:
        names += [
            n
            for n, e in inv["secrets"].items()
            if e["kind"] != "not-rotated"
            and (args.tier is None or e["tier"] == args.tier)
            and (not args.kind or e["kind"] == args.kind)
            and (not args.railway_exposed or e.get("configs", ["prd"]) != [] or e.get("railway"))
            and n not in names
            and (not e.get("configs", ["prd"]) or args.config in e.get("configs", ["prd"]))
        ]
    if not names:
        sys.exit("nothing selected")
    entries = {n: entry(inv, n) for n in names}
    for n, e in entries.items():
        if e["kind"] == "not-rotated":
            sys.exit(f"{n} is marked not-rotated")
        if e["configs"] and args.config not in e["configs"]:
            sys.exit(f"{n} is not held in Doppler {args.config} (configs: {e['configs']})")
    mode = "compromised" if args.compromised else "routine"
    cfg = args.config

    print(f"{mode} rotation, Doppler {project}/{cfg}:")
    redeploy: set[str] = set()
    for n, e in sorted(entries.items(), key=lambda kv: (kv[1]["tier"], kv[0])):
        gh = ", ".join(f"{t} as {g}" for t, g in github_targets(e, n, cfg))
        print(f"  t{e['tier']} {n} ({e['kind']}){'  + GitHub ' + gh if gh else ''}")
        redeploy |= set(e["redeploy"])
    if redeploy:
        print(f"  then redeploy: {', '.join(sorted(redeploy))}")
    if args.dry_run:
        return 0
    if not confirm("go?"):
        return 1

    done: dict[str, dict] = {}

    # 1. mint and store -------------------------------------------------------
    for n, e in sorted(entries.items(), key=lambda kv: (kv[1]["tier"], kv[0])):
        state = {"old_aws_keys": [], "stored": False}
        kind = e["kind"]
        print(f"\n== {n}")
        if kind in ("machine-key", "generated"):
            value = pysecrets.token_urlsafe(32)  # no dots: routes as a machine key
            doppler_set(project, cfg, n, value)
            for target, gh_name in github_targets(e, n, cfg):
                gh_set(target, gh_name, value)
                print(f"   GitHub {target} {gh_name} set")
            del value
            state["stored"] = True
            if e.get("steps"):
                print(f"   ALSO: {e['steps']}")
                input("   press Enter when done ")
        elif kind == "aws-access-key":
            user = e["iam_user"]
            existing = aws("iam", "list-access-keys", "--user-name", user)["AccessKeyMetadata"]
            if len(existing) >= 2:
                print(f"   {user} already has two keys; delete the unused one first:")
                for k in existing:
                    print(f"     {k['AccessKeyId']} {k['Status']} created {k['CreateDate']}")
                continue
            created = aws("iam", "create-access-key", "--user-name", user)["AccessKey"]
            doppler_set(project, cfg, e["id_var"], created["AccessKeyId"])
            doppler_set(project, cfg, n, created["SecretAccessKey"])
            del created["SecretAccessKey"]
            state.update(
                stored=True, new_key=created["AccessKeyId"], old_aws_keys=[k["AccessKeyId"] for k in existing]
            )
            print(f"   new key {created['AccessKeyId']} stored")
            if args.compromised:
                for old in state["old_aws_keys"]:
                    aws(
                        "iam",
                        "update-access-key",
                        "--user-name",
                        user,
                        "--access-key-id",
                        old,
                        "--status",
                        "Inactive",
                    )
                    print(f"   old key {old} deactivated")
        elif kind == "manual":
            print(f"   mint: {e.get('steps', 'see the provider console')}")
            if e["configs"] or github_targets(e, n, cfg):
                value = getpass.getpass(f"   paste new {n} (hidden, blank to skip): ")
                if not value.strip():
                    print("   skipped")
                    continue
                if e["configs"]:
                    doppler_set(project, cfg, n, value)
                for target, gh_name in github_targets(e, n, cfg):
                    gh_set(target, gh_name, value)
                    print(f"   GitHub {target} {gh_name} set")
                del value
                state["stored"] = True
            else:
                state["stored"] = confirm("   done?")
                if not state["stored"]:
                    continue
            if args.compromised:
                print(f"   REVOKE NOW: {e.get('revoke', 'revoke the old value at the provider')}")
                state["revoked"] = confirm("   old value revoked?")
        done[n] = state

    if not done:
        print("\nnothing stored")
        return 1

    # 2. redeploy -------------------------------------------------------------
    needed = sorted({s for n in done for s in entries[n]["redeploy"]})
    if needed:
        print(f"\nRedeploy on Railway ({cfg}): {', '.join(needed)}")
        print(
            "Doppler's sync updates the shared variables; a running container keeps the old ones until it restarts."
        )
        input("press Enter once they are redeployed ")

    # 3. verify ---------------------------------------------------------------
    for n, state in done.items():
        e = entries[n]
        command = e.get("verify") or (machine_verify(inv, n, cfg) if e["kind"] == "machine-key" else None)
        if command:
            state["verified"] = verify(project, cfg, command)
            print(f"{n}: {'verified' if state['verified'] else 'VERIFY FAILED'}")
        else:
            state["verified"] = None
            print(f"{n}: no automatic check — confirm it by hand")

    # 4. revoke the old value (routine) ---------------------------------------
    for n, state in done.items():
        e = entries[n]
        if e["kind"] == "aws-access-key":
            if state.get("verified") is False and not args.compromised:
                print(f"{n}: verify failed, old key left active")
                state["revoked"] = False
                continue
            for old in state["old_aws_keys"]:
                aws("iam", "delete-access-key", "--user-name", e["iam_user"], "--access-key-id", old)
                print(f"{n}: old key {old} deleted")
            state["revoked"] = True
        elif e["kind"] == "manual" and "revoked" not in state:
            print(f"{n}: revoke the old value — {e.get('revoke', 'at the provider')}")
            state["revoked"] = confirm("   revoked?")
        elif e["kind"] in ("machine-key", "generated"):
            state["revoked"] = True  # one value at a time: overwriting it is the revoke

    # 5. record ---------------------------------------------------------------
    by = run(["git", "-C", str(ROOT), "config", "user.email"]).strip() or os.environ.get("USER", "")
    for n, state in done.items():
        row = {
            "at": now(),
            "name": n,
            "config": cfg,
            "kind": entries[n]["kind"],
            "mode": mode,
            "reason": args.reason,
            "by": by,
            "verified": state.get("verified"),
            "revoked": state.get("revoked", False),
        }
        if state.get("new_key"):
            row["aws_key_id"] = state["new_key"]
        record(row)
    print(f"\nrecorded {len(done)} rotation(s) in {LEDGER.relative_to(ROOT)} — commit it")
    failed = [n for n, s in done.items() if s.get("verified") is False or not s.get("revoked")]
    if failed:
        print(f"needs attention: {', '.join(failed)}")
        return 1
    return 0


def cmd_verify(args, inv) -> int:
    """Re-run the checks for secrets already rotated, and record the result."""
    project, cfg = inv["doppler_project"], args.config
    names = list(args.names) or [
        n
        for n, e in inv["secrets"].items()
        if (not args.kind or e["kind"] == args.kind)
        and cfg in e.get("configs", ["prd"])
        and (e.get("verify") or e["kind"] == "machine-key")
    ]
    by = run(["git", "-C", str(ROOT), "config", "user.email"]).strip() or os.environ.get("USER", "")
    failed = []
    for n in names:
        e = entry(inv, n)
        command = e.get("verify") or (machine_verify(inv, n, cfg) if e["kind"] == "machine-key" else None)
        if not command:
            print(f"{n}: no automatic check")
            continue
        ok = verify(project, cfg, command)
        print(f"{n}: {'verified' if ok else 'VERIFY FAILED'}")
        record(
            {
                "at": now(),
                "name": n,
                "config": cfg,
                "kind": e["kind"],
                "mode": "verify",
                "by": by,
                "verified": ok,
            }
        )
        if not ok:
            failed.append(n)
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="every secret, last rotation and when it is due")
    pl.add_argument("--due", action="store_true")
    pl.add_argument("--tier", type=int)

    pc = sub.add_parser("check", help="compare the inventory with Doppler's names")
    pc.add_argument("--config", default="prd")

    pr = sub.add_parser("rotate", help="rotate one or more secrets")
    pr.add_argument("names", nargs="*")
    pr.add_argument("--reason", required=True)
    pr.add_argument("--compromised", action="store_true")
    pr.add_argument("--config", default="prd")
    pr.add_argument("--tier", type=int)
    pr.add_argument("--kind")
    pr.add_argument(
        "--railway-exposed",
        action="store_true",
        help="only what Railway held: everything in Doppler (synced to its shared variables) "
        "and entries marked railway: true",
    )
    pr.add_argument("--dry-run", action="store_true")

    pv = sub.add_parser("verify", help="re-run checks for rotated secrets and record the result")
    pv.add_argument("names", nargs="*")
    pv.add_argument("--kind")
    pv.add_argument("--config", default="prd")

    args = p.parse_args()
    inv = load_inventory()
    if args.cmd == "list":
        cmd_list(args, inv)
        return 0
    if args.cmd == "check":
        return cmd_check(args, inv)
    if args.cmd == "verify":
        return cmd_verify(args, inv)
    return cmd_rotate(args, inv)


if __name__ == "__main__":
    sys.exit(main())
