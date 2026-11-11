"""Test accounts and their sessions.

auth.type:
  none    no authentication; every virtual user sends no auth header
  token   pre-issued tokens (auth.tokens_env / auth.tokens_file); no sign-in
  login   each test account signs in through auth.login (or a `login` hook)

Where the accounts come from (first that applies):
  1. a `users(ctx)` hook        -- creates/provisions accounts, returns [{email, password, ...}]
  2. users.file                 -- CSV or JSON next to project.yaml
  3. users.email + users.password / users.password_env, numbered 1..users.count
"""

from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path

import requests

from .core import HarnessError, Pacer, log, lookup, now_iso, read_json, write_json
from .http import Api, render


def _users_file(ctx) -> Path:
    return ctx.state_dir / "users.json"


def _tokens_file(ctx) -> Path:
    return ctx.state_dir / "tokens.json"


def configured_users(ctx) -> list[dict]:
    u = ctx.cfg["users"]
    if u.get("file"):
        path = ctx.project.dir / u["file"]
        if path.suffix.lower() == ".json":
            rows = json.loads(path.read_text(encoding="utf-8"))
        else:
            with path.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
        return [{"n": i, **row} for i, row in enumerate(rows, 1)][: int(u.get("count") or len(rows))]
    ident = [k for k in ("email", "username") if u.get(k)]
    if not ident:
        return [{"n": n} for n in range(1, int(u["count"]) + 1)]
    password = u.get("password")
    if u.get("password_env"):
        password = os.environ.get(u["password_env"])
        if not password:
            raise HarnessError(f"Set {u['password_env']} to the test accounts' password")
    out = []
    for n in range(1, int(u["count"]) + 1):
        row = {"n": n, "password": password, **{k: u[k].format(n=n) for k in ident}}
        for k, v in (u.get("fields") or {}).items():
            row[k] = v.format(n=n) if isinstance(v, str) else v
        out.append(row)
    return out


def provision_users(ctx) -> list[dict]:
    """Setup step: creates or lists the test accounts and stores them in .state."""
    users = ctx.call("users") if ctx.hook("users") else configured_users(ctx)
    if not users:
        raise HarnessError("No test users: configure `users:` or a users hook")
    for i, u in enumerate(users, 1):
        u.setdefault("n", i)
    write_json(_users_file(ctx), users)
    log(f"{len(users)} test account(s) ready")
    return users


def users(ctx) -> list[dict]:
    data = read_json(_users_file(ctx), [])
    return data or provision_users(ctx)


def wait_for_api(ctx, timeout: int = 1800) -> None:
    """After an overload run the API may answer 502/503/504 for a while; wait it out."""
    url = ctx.cfg["base_url"].rstrip("/") + (ctx.cfg.get("health") or "/")
    deadline, waited = time.monotonic() + timeout, False
    while True:
        try:
            status = requests.get(url, timeout=30, verify=ctx.cfg.get("tls_verify", True)).status_code
        except requests.RequestException:
            status = None
        if status is not None and status not in (502, 503, 504):
            if waited:
                log(f"API answering again (HTTP {status})")
            return
        if time.monotonic() > deadline:
            raise HarnessError(f"API still unavailable after {timeout} s (last status {status})")
        if not waited:
            log(f"API busy (HTTP {status}); waiting for it to recover")
            waited = True
        time.sleep(15)


def login(ctx, user: dict) -> str:
    """One sign-in: the `login` hook when the project has one, else auth.login."""
    if ctx.hook("login"):
        return ctx.call("login", user)
    spec = ctx.cfg["auth"]["login"]
    resp = Api(ctx.cfg).request(spec, {"user": user, "ctx": ctx.dataset_vars()})
    if resp.status_code == 429:
        raise HarnessError("Sign-in rate limit reached; lower auth.per_minute")
    if not 200 <= resp.status_code < 300:
        raise HarnessError(f"Sign-in failed for {user.get('email') or user.get('username')}: "
                           f"HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    token = lookup(data, spec.get("token", "token"))
    if not token:
        raise HarnessError(f"Sign-in response has no token at `{spec.get('token', 'token')}`")
    return ctx.call("after_login", user, token, data, default=None) or token


def token_alive(ctx, token: str) -> bool:
    check = ctx.cfg["auth"].get("check")
    if not check:
        return True
    resp = Api(ctx.cfg, token).request(check, {"ctx": ctx.dataset_vars()})
    return 200 <= resp.status_code < 300


def sign_in_all(ctx, only_missing: bool = True) -> list[dict]:
    a = ctx.cfg["auth"]
    if a["type"] == "none":
        tokens = [{"n": u["n"], "token": ""} for u in users(ctx)]
        write_json(_tokens_file(ctx), tokens)
        return tokens
    if a["type"] == "token":
        raw = os.environ.get(a.get("tokens_env") or "", "")
        if a.get("tokens_file"):
            raw = (ctx.project.dir / a["tokens_file"]).read_text(encoding="utf-8")
        values = [t.strip() for t in raw.replace(",", "\n").splitlines() if t.strip()]
        if not values:
            raise HarnessError("auth.type is token but no tokens found (auth.tokens_env / auth.tokens_file)")
        tokens = [{"n": i, "token": t} for i, t in enumerate(values, 1)]
        write_json(_tokens_file(ctx), tokens)
        return tokens
    wait_for_api(ctx)
    accounts = users(ctx)
    tokens = {t["n"]: t for t in read_json(_tokens_file(ctx), [])}
    todo = [u for u in accounts if not (only_missing and u["n"] in tokens)]
    pacer = Pacer(a.get("per_minute"))
    if todo:
        per = a.get("per_minute")
        log(f"Signing in {len(todo)} account(s)" + (f" at {per}/min (~{len(todo) / per:.0f} min)" if per else ""))
    for i, u in enumerate(todo, 1):
        pacer.wait()
        token = login(ctx, u)
        tokens[u["n"]] = {"n": u["n"], "email": u.get("email"), "token": token, "signed_in_at": now_iso()}
        write_json(_tokens_file(ctx), sorted(tokens.values(), key=lambda t: t["n"]))
        write_json(_users_file(ctx), accounts)  # a login hook may have updated a user (password change)
        if i % 10 == 0:
            log(f"{i}/{len(todo)} signed in")
    return sorted(tokens.values(), key=lambda t: t["n"])


def refresh(ctx) -> dict:
    """Before every run: touch each session (auth.check), sign in again those that died."""
    tokens = read_json(_tokens_file(ctx), [])
    if not tokens:
        tokens = sign_in_all(ctx)
        return {"checked": len(tokens), "renewed": len(tokens)}
    if ctx.cfg["auth"]["type"] != "login":
        return {"checked": len(tokens), "renewed": 0}
    wait_for_api(ctx)
    dead = [t["n"] for t in tokens if not token_alive(ctx, t["token"])]
    if dead:
        log(f"{len(dead)} session(s) expired; signing them in again")
        write_json(_tokens_file(ctx), [t for t in tokens if t["n"] not in dead])
        sign_in_all(ctx)
    return {"checked": len(tokens), "renewed": len(dead)}


def tokens(ctx) -> list[str]:
    return [t["token"] for t in read_json(_tokens_file(ctx), [])]


def admin_user(ctx) -> dict | None:
    adm = ctx.cfg["auth"].get("admin") or {}
    if ctx.hook("admin_credentials"):
        return ctx.call("admin_credentials")
    if adm.get("email_env") or adm.get("username_env"):
        key = "email" if adm.get("email_env") else "username"
        ident = os.environ.get(adm.get("email_env") or adm.get("username_env"))
        password = os.environ.get(adm.get("password_env") or "")
        if not ident or not password:
            raise HarnessError(f"Set {adm.get('email_env') or adm.get('username_env')} and "
                               f"{adm.get('password_env')} for the admin account")
        return {key: ident, "email": ident, "username": ident, "password": password, "admin": True}
    if adm.get("email") and adm.get("password"):
        return {"email": adm["email"], "username": adm["email"], "password": adm["password"], "admin": True}
    return None


def admin_api(ctx) -> Api:
    a = ctx.cfg["auth"]
    if a["type"] == "none":
        return Api(ctx.cfg)
    if a["type"] == "token":
        env = (a.get("admin") or {}).get("token_env")
        token = os.environ.get(env or "")
        if not token:
            raise HarnessError("Set auth.admin.token_env and that environment variable for set-up calls")
        return Api(ctx.cfg, token)
    wait_for_api(ctx)
    cached = ctx.state.get("admin_token")
    if cached and token_alive(ctx, cached):
        return Api(ctx.cfg, cached)
    user = admin_user(ctx)
    if not user:
        raise HarnessError("This step needs an admin account: configure auth.admin (email_env/password_env)")
    token = login(ctx, user)
    ctx.update_state(admin_token=token)
    return Api(ctx.cfg, token)
