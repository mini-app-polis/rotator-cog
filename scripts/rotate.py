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

secrets/inventory.yaml says what each secret is and how to rotate it. The
API keeps the record: one event per rotation, written here through
POST /v1/secrets/rotation-events under the owner's own identity (a Clerk
session token pasted at a hidden prompt). Neither ever holds a value.

Values never pass through stdout, an argument list or a file: a minted value
goes to Doppler on stdin, a pasted value is read with getpass, and verify commands run under `doppler run`, so the value
reaches them as an environment variable.

Routine rotation keeps the old credential alive until the new one verifies.
--compromised kills the old one as soon as the new one is stored, whether
or not anything has picked it up yet: an outage is the price, and overlap is
what an attacker wants.

Doppler is the only place a value is written. Everything that reads one,
GitHub Actions included, holds a reference to it that Doppler's syncs keep
current.

Needs on PATH: doppler (logged in), and as the secrets require them aws
(profile from AWS_PROFILE, default miniapppolis) and curl.
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
import urllib.parse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "secrets" / "inventory.yaml"

# The repository's own package, standard library only: the record's client,
# the events it writes, and entries resolved per config.
sys.path.insert(0, str(ROOT / "src"))
from rotator_cog import api as record_api  # noqa: E402
from rotator_cog import entries, events  # noqa: E402

AWS_PROFILE = os.environ.get("AWS_PROFILE", "miniapppolis")
USER_AGENT = "mini-app-polis-rotate/1"  # Cloudflare blocks default library UAs


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------


def load_inventory() -> dict:
    return yaml.safe_load(INVENTORY.read_text())


def entry(inv: dict, name: str, config: str = "prd") -> dict:
    """``name``'s inventory entry, its redeploy list resolved for ``config``."""
    try:
        e = entries.for_config(inv["secrets"][name], config)
    except KeyError:
        sys.exit(f"{name} is not in {INVENTORY.relative_to(ROOT)}; add it first")
    e.setdefault("configs", ["prd"])
    return e


TOKEN_HELP = (
    "The API records rotations as you, and you sign in to it with a Clerk\n"
    "session token. On a page signed in to the wcs Clerk tenant, run\n"
    "`await Clerk.session.getToken()` in the browser console and paste the\n"
    "result. It lasts about a minute; you are asked again when it expires."
)


def record_api_for(args) -> record_api.RotationEvents:
    """The rotation record, as the person running this script."""
    shown = []

    def token() -> str:
        if not shown:
            print(TOKEN_HELP, file=sys.stderr)
            shown.append(True)
        return getpass.getpass("session token (hidden, blank to stop): ")

    return record_api.RotationEvents(args.api, token)


def actor() -> str:
    return run(
        ["git", "-C", str(ROOT), "config", "user.email"]
    ).strip() or os.environ.get("USER", "")


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
        [
            "doppler",
            "secrets",
            "set",
            name,
            "--project",
            project,
            "--config",
            config,
            "--silent",
        ],
        stdin=value,
    )


def doppler_names(project: str, config: str) -> set[str]:
    out = run(
        [
            "doppler",
            "secrets",
            "--project",
            project,
            "--config",
            config,
            "--only-names",
            "--json",
        ]
    )
    data = json.loads(out)
    names = set(data) if isinstance(data, (dict, list)) else set()
    return names - {"DOPPLER_PROJECT", "DOPPLER_CONFIG", "DOPPLER_ENVIRONMENT"}


def aws(*args: str) -> dict:
    return json.loads(
        run(["aws", "--profile", AWS_PROFILE, "--output", "json", *args]) or "{}"
    )


def verify(project: str, config: str, command: str) -> bool:
    p = subprocess.run(
        [
            "doppler",
            "run",
            "--project",
            project,
            "--config",
            config,
            "--",
            "sh",
            "-c",
            command,
        ],
        check=False,
    )
    return p.returncode == 0


def machine_name(var: str) -> str:
    return var.removesuffix("_API_KEY").lower().replace("_", "-")


def machine_verify(inv: dict, var: str, config: str) -> str:
    base = (
        inv["api_base_url"]
        if config == "prd"
        else "${KAIANO_API_BASE_URL_DEV:-$KAIANO_API_BASE_URL}"
    )
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
    seen = events.last_minted(record_api_for(args).list(latest=True))
    today = dt.datetime.now(dt.UTC)
    rows = []
    for name in inv["secrets"]:
        e = entry(inv, name)
        if e["kind"] == "not-rotated" or (
            args.tier is not None and e["tier"] != args.tier
        ):
            continue
        cadence = inv["cadence_days"].get(e["kind"])
        for cfg in e["configs"] or ["-"]:
            if e["configs"]:
                last = seen.get((name, cfg))
            else:
                # Held outside Doppler (by Railway, or placed by hand): any
                # config a rotation was recorded under counts.
                last = max(
                    (at for (n, _), at in seen.items() if n == name), default=None
                )
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
    # A handover's outgoing value sits in NAME_PREVIOUS until it is retired.
    known |= {entries.previous_name(n) for n in inv["secrets"]}
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
        # configs: [] means held outside Doppler, so it is never stale here.
        if e.get("kind") != "not-rotated"
        and args.config
        in (e.get("configs") if e.get("configs") is not None else ["prd"])
        and n not in names
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
            and (
                not args.railway_exposed
                or e.get("configs", ["prd"]) != []
                or e.get("railway")
            )
            and n not in names
            and (
                not e.get("configs", ["prd"])
                or args.config in e.get("configs", ["prd"])
            )
        ]
    if not names:
        sys.exit("nothing selected")
    chosen = {n: entry(inv, n, args.config) for n in names}
    for n, e in chosen.items():
        if e["kind"] == "not-rotated":
            sys.exit(f"{n} is marked not-rotated")
        if e["configs"] and args.config not in e["configs"]:
            sys.exit(
                f"{n} is not held in Doppler {args.config} (configs: {e['configs']})"
            )
    mode = "compromised" if args.compromised else "routine"
    cfg = args.config

    print(f"{mode} rotation, Doppler {project}/{cfg}:")
    redeploy: set[str] = set()
    for n, e in sorted(chosen.items(), key=lambda kv: (kv[1]["tier"], kv[0])):
        print(f"  t{e['tier']} {n} ({e['kind']})")
        redeploy |= set(e["redeploy"])
    if redeploy:
        print(f"  then redeploy: {', '.join(sorted(redeploy))}")
    if args.dry_run:
        return 0
    if not confirm("go?"):
        return 1

    done: dict[str, dict] = {}

    # 1. mint and store -------------------------------------------------------
    for n, e in sorted(chosen.items(), key=lambda kv: (kv[1]["tier"], kv[0])):
        state = {"old_aws_keys": [], "stored": False}
        kind = e["kind"]
        print(f"\n== {n}")
        if kind in ("machine-key", "generated"):
            # No dots in either part: a dotless credential routes as a machine key.
            value = inv.get("key_prefix", {}).get(cfg, "") + pysecrets.token_urlsafe(32)
            doppler_set(project, cfg, n, value)
            del value
            state["stored"] = True
            if e.get("steps"):
                print(f"   ALSO: {e['steps']}")
                input("   press Enter when done ")
        elif kind == "aws-access-key":
            user = e["iam_user"]
            existing = aws("iam", "list-access-keys", "--user-name", user)[
                "AccessKeyMetadata"
            ]
            if len(existing) >= 2:
                print(f"   {user} already has two keys; delete the unused one first:")
                for k in existing:
                    print(
                        f"     {k['AccessKeyId']} {k['Status']} created {k['CreateDate']}"
                    )
                continue
            created = aws("iam", "create-access-key", "--user-name", user)["AccessKey"]
            doppler_set(project, cfg, e["id_var"], created["AccessKeyId"])
            doppler_set(project, cfg, n, created["SecretAccessKey"])
            del created["SecretAccessKey"]
            state.update(
                stored=True,
                new_key=created["AccessKeyId"],
                old_aws_keys=[k["AccessKeyId"] for k in existing],
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
        elif kind in ("manual", "provider-api"):
            # A provider-api secret is the worker's to rotate; by hand, it is
            # minted in the provider's console like a manual one.
            print(
                f"   mint: {e.get('steps') or e.get('console', 'see the provider console')}"
            )
            if e["configs"]:
                value = getpass.getpass(f"   paste new {n} (hidden, blank to skip): ")
                if not value.strip():
                    print("   skipped")
                    continue
                doppler_set(project, cfg, n, value)
                del value
                state["stored"] = True
            else:
                state["stored"] = confirm("   done?")
                if not state["stored"]:
                    continue
            if args.compromised:
                print(
                    f"   REVOKE NOW: {e.get('revoke', 'revoke the old value at the provider')}"
                )
                state["revoked"] = confirm("   old value revoked?")
        state["minted"] = events.now()
        done[n] = state

    if not done:
        print("\nnothing stored")
        return 1

    # 2. redeploy -------------------------------------------------------------
    needed = sorted({s for n in done for s in chosen[n]["redeploy"]})
    if needed:
        print(f"\nRedeploy on Railway ({cfg}): {', '.join(needed)}")
        print(
            "Doppler's sync updates the shared variables; a running container keeps the old ones until it restarts."
        )
        input("press Enter once they are redeployed ")

    # 3. verify ---------------------------------------------------------------
    for n, state in done.items():
        e = chosen[n]
        command = e.get("verify") or (
            machine_verify(inv, n, cfg) if e["kind"] == "machine-key" else None
        )
        if command:
            state["verified"] = verify(project, cfg, command)
            print(f"{n}: {'verified' if state['verified'] else 'VERIFY FAILED'}")
        else:
            state["verified"] = None
            print(f"{n}: no automatic check — confirm it by hand")

    # 4. revoke the old value (routine) ---------------------------------------
    for n, state in done.items():
        e = chosen[n]
        if e["kind"] == "aws-access-key":
            if state.get("verified") is False and not args.compromised:
                print(f"{n}: verify failed, old key left active")
                state["revoked"] = False
                continue
            for old in state["old_aws_keys"]:
                aws(
                    "iam",
                    "delete-access-key",
                    "--user-name",
                    e["iam_user"],
                    "--access-key-id",
                    old,
                )
                print(f"{n}: old key {old} deleted")
            state["revoked"] = True
        elif e["kind"] in ("manual", "provider-api") and "revoked" not in state:
            print(f"{n}: revoke the old value — {e.get('revoke', 'at the provider')}")
            state["revoked"] = confirm("   revoked?")
        elif e["kind"] in ("machine-key", "generated"):
            state["revoked"] = True  # one value at a time: overwriting it is the revoke

    # 5. record ---------------------------------------------------------------
    by = actor()
    record = [
        events.event(
            name=n,
            # A secret held outside Doppler is recorded under the config it
            # was rotated for: the record has no other.
            config=cfg,
            kind=chosen[n]["kind"],
            mode=mode,
            actor=by,
            minted=state["minted"],
            retired=events.now() if state.get("revoked") else None,
            verified=state.get("verified"),
            # The last four only, as the AWS console shows it: a whole key
            # id is what secret scanners match.
            aws_key_last4=state["new_key"][-4:] if state.get("new_key") else None,
        )
        for n, state in done.items()
    ]
    if not write_events(args, record):
        return 1
    failed = [
        n for n, s in done.items() if s.get("verified") is False or not s.get("revoked")
    ]
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
    api = record_api_for(args)
    failed = []
    for n in names:
        e = entry(inv, n, cfg)
        command = e.get("verify") or (
            machine_verify(inv, n, cfg) if e["kind"] == "machine-key" else None
        )
        if not command:
            print(f"{n}: no automatic check")
            continue
        ok = verify(project, cfg, command)
        print(f"{n}: {'verified' if ok else 'VERIFY FAILED'}")
        # A re-check is not a rotation: it rewrites the verification of the
        # latest one, the event with the same key.
        latest = api.list(name=n, config=cfg, latest=True)
        if latest:
            api.record(events.rechecked(latest[0], ok))
        else:
            print(f"   no rotation of {n} recorded for {cfg}; nothing to update")
        if not ok:
            failed.append(n)
    return 1 if failed else 0


def _with_password(url: str, password: str, host_from: str | None = None) -> str:
    """``url`` with its password replaced; optionally its host:port taken from ``host_from``."""
    parts = urllib.parse.urlsplit(url)
    where = urllib.parse.urlsplit(host_from) if host_from else parts
    host = where.hostname + (f":{where.port}" if where.port else "")
    return parts._replace(netloc=f"{parts.username}:{password}@{host}").geturl()


def cmd_pg_roles(args, inv) -> int:
    """Rotate the passwords of the roles behind a Postgres entry's database URLs.

    Railway's Regenerate changes only the postgres superuser. Each URL in the
    entry's `also` list connects as its own role (its username); this sets a
    new password on that role and writes the URL back with it. The superuser
    connection is pasted at a hidden prompt (the service's DATABASE_PUBLIC_URL).
    """
    project, cfg = inv["doppler_project"], args.config
    e = entry(inv, args.name, cfg)
    urls = e.get("also", [])
    if not urls:
        sys.exit(f"{args.name} lists no database URLs under also:")
    admin = getpass.getpass(
        "paste the superuser DATABASE_PUBLIC_URL (hidden): "
    ).strip()
    run(["psql", admin, "-qAtc", "select 1"])
    mode = "compromised" if args.compromised else "routine"
    by = actor()
    record = []
    # The URL variables are the entry's `also` names, not entries of their
    # own, so one event records the entry, unless a variable is an entry.
    own: list[tuple[str, bool]] = []
    for var in urls:
        current = run(
            [
                "doppler",
                "secrets",
                "get",
                var,
                "--project",
                project,
                "--config",
                cfg,
                "--plain",
            ]
        ).strip()
        role = urllib.parse.urlsplit(current).username
        if not role or role == "postgres":
            print(f"{var}: connects as {role!r}, not a dedicated role; skipped")
            continue
        password = pysecrets.token_hex(24)
        minted = events.now()
        run(
            ["psql", admin, "-v", "ON_ERROR_STOP=1", "-q"],
            stdin=f"ALTER ROLE \"{role}\" PASSWORD '{password}';\n",
        )
        doppler_set(project, cfg, var, _with_password(current, password))
        # The URL's own host is private (*.railway.internal); check over the admin's public host.
        ok = (
            subprocess.run(
                [
                    "psql",
                    _with_password(current, password, host_from=admin),
                    "-qAtc",
                    "select 1",
                ],
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            == 0
        )
        del password
        print(f"{var}: role {role} rotated, {'verified' if ok else 'VERIFY FAILED'}")
        var_kind = inv["secrets"].get(var, {}).get("kind")
        if var_kind in events.KINDS:
            record.append(
                events.event(
                    name=var,
                    config=cfg,
                    kind=var_kind,
                    mode=mode,
                    actor=by,
                    minted=minted,
                    # ALTER ROLE ends the old password there and then.
                    retired=events.now(),
                    verified=ok,
                )
            )
        else:
            own.append((minted, ok))
    if own:
        record.append(
            events.event(
                name=args.name,
                config=cfg,
                kind=e["kind"],
                mode=mode,
                actor=by,
                minted=own[0][0],
                retired=events.now(),
                verified=all(ok for _, ok in own),
            )
        )
    print(f"Redeploy: {', '.join(e['redeploy'])}")
    return 0 if write_events(args, record) else 1


def write_events(args, record: list[dict]) -> bool:
    """Write each event to the API; on a failure, say which were not written."""
    api = record_api_for(args)
    for i, ev in enumerate(record):
        try:
            api.record(ev)
        except record_api.ApiError as exc:
            print(f"\nthe API did not record these rotations ({exc}):")
            for left in record[i:]:
                print(f"   {events.describe(left)}")
            return False
    print(f"\nrecorded {len(record)} rotation(s) in the API")
    return True


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--api",
        help="the API that keeps the record (default: the inventory's api_base_url)",
    )
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

    pv = sub.add_parser(
        "verify",
        help="re-run checks for rotated secrets and record the result on the "
        "latest rotation",
    )
    pv.add_argument("names", nargs="*")
    pv.add_argument("--kind")
    pv.add_argument("--config", default="prd")

    pp = sub.add_parser(
        "pg-roles", help="rotate the role passwords behind a Postgres entry's URLs"
    )
    pp.add_argument(
        "name", help="a Postgres entry, e.g. POSTGRES_KAIANOLEVINE_PASSWORD"
    )
    pp.add_argument("--reason", required=True)
    pp.add_argument("--compromised", action="store_true")
    pp.add_argument("--config", default="prd")

    args = p.parse_args()
    inv = load_inventory()
    args.api = args.api or inv["api_base_url"]
    try:
        return dispatch(args, inv)
    except record_api.ApiError as exc:
        sys.exit(f"the API refused: {exc}")


def dispatch(args, inv) -> int:
    if args.cmd == "list":
        cmd_list(args, inv)
        return 0
    if args.cmd == "check":
        return cmd_check(args, inv)
    if args.cmd == "pg-roles":
        return cmd_pg_roles(args, inv)
    if args.cmd == "verify":
        return cmd_verify(args, inv)
    return cmd_rotate(args, inv)


if __name__ == "__main__":
    sys.exit(main())
