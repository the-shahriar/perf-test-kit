"""`perfkit check`: everything a run needs, verified without changing anything on the target."""

from __future__ import annotations

import subprocess

import requests

from . import auth, db
from .core import HarnessError, find_k6
from .project import assert_allowed


def _line(ok: bool | None, name: str, detail: str) -> bool:
    mark = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    print(f"  [{mark}] {name:<28} {detail}")
    return ok is not False


def run_checks(ctx) -> bool:
    cfg = ctx.cfg
    print(f"\nChecks for project '{ctx.project.name}', target '{ctx.target}' ({cfg['base_url']})\n")
    ok = True
    try:
        assert_allowed(cfg["base_url"], cfg)
        ok &= _line(True, "Target host allowed", "local" if ctx.is_local() else "listed in allowed_hosts")
    except HarnessError as exc:
        return _line(False, "Target host allowed", str(exc))

    k6 = find_k6()
    if k6.exists():
        ver = subprocess.run([str(k6), "version"], capture_output=True, text=True).stdout.strip()
        ok &= _line(True, "k6", ver)
    else:
        ok &= _line(False, "k6", f"not found ({k6}); see docs/SETUP.md")

    url = cfg["base_url"].rstrip("/") + (cfg.get("health") or "/")
    try:
        status = requests.get(url, timeout=15, verify=cfg.get("tls_verify", True)).status_code
        ok &= _line(status < 500, "Target reachable", f"GET {url} -> HTTP {status}")
    except requests.RequestException as exc:
        ok &= _line(False, "Target reachable", f"{url}: {type(exc).__name__}")

    a = cfg["auth"]
    if a["type"] == "none":
        _line(None, "Admin sign-in", "auth.type none")
    elif a.get("admin") or ctx.hook("admin_credentials"):
        try:
            ctx.admin()
            ok &= _line(True, "Admin sign-in", "ok")
        except HarnessError as exc:
            ok &= _line(False, "Admin sign-in", str(exc)[:150])
    else:
        _line(None, "Admin sign-in", "no auth.admin configured (only needed by hooks that create data)")

    if a["type"] == "login":
        try:
            accounts = auth.configured_users(ctx) if not ctx.hook("users") else None
            if accounts:
                token = auth.login(ctx, accounts[0])
                ok &= _line(bool(token), "Test account sign-in", f"{accounts[0].get('email') or accounts[0].get('username')}")
            else:
                _line(None, "Test account sign-in", "accounts are created by the users hook during setup")
        except HarnessError as exc:
            ok &= _line(False, "Test account sign-in", str(exc)[:150])

    if db.configured(cfg):
        try:
            conn = db.connect(cfg)
            conn.close()
            ok &= _line(True, "Database (monitoring)", "connected read-only")
        except Exception as exc:  # noqa: BLE001
            ok &= _line(False, "Database (monitoring)", f"{type(exc).__name__}: {str(exc)[:120]}")
    else:
        _line(None, "Database (monitoring)", "not configured")

    dock = (cfg.get("monitoring") or {}).get("docker") or {}
    if dock.get("containers"):
        from .monitor import docker_stats

        rows = docker_stats(dock["containers"], dock.get("ssh"))
        found = {r["source"] for r in rows}
        missing = [k for k in dock["containers"] if k not in found]
        ok &= _line(not missing, "Container stats", "all containers sampled" if not missing else f"missing: {missing}")
    else:
        _line(None, "Container stats", "monitoring.docker not configured")

    for item in ctx.call("check", default=[]) or []:
        good, name, detail = item
        ok &= _line(good, name, detail)

    print()
    return bool(ok)
