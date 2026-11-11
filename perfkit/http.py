"""HTTP client for set-up work (never for measurements: those go through k6)."""

from __future__ import annotations

import re

import requests

from .core import HarnessError, lookup
from .project import assert_allowed

_FULL = re.compile(r"^\{\{\s*([^{}]+?)\s*\}\}$")
_PART = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def render(value, scope: dict):
    """Python side of the template syntax: {{user.email}}, {{ctx.module_id}}, {{env.NAME}}.
    A string that is exactly one placeholder keeps the value's type."""
    import os

    def resolve(expr: str):
        if expr.startswith("env."):
            return os.environ.get(expr[4:], "")
        return lookup(scope, expr)

    if isinstance(value, str):
        m = _FULL.match(value)
        if m:
            return resolve(m.group(1))
        return _PART.sub(lambda mm: "" if resolve(mm.group(1)) is None else str(resolve(mm.group(1))), value)
    if isinstance(value, dict):
        return {k: render(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, scope) for v in value]
    return value


class Api:
    def __init__(self, cfg: dict, token: str | None = None):
        assert_allowed(cfg["base_url"], cfg)
        self.cfg = cfg
        self.base = cfg["base_url"].rstrip("/") + (cfg.get("api_prefix") or "")
        self.session = requests.Session()
        self.session.verify = cfg.get("tls_verify", True)
        self.token = token
        self.unwrap = (cfg.get("response") or {}).get("unwrap")

    def headers(self) -> dict:
        h = {"Content-Type": "application/json", **(self.cfg.get("headers") or {})}
        a = self.cfg["auth"]
        if self.token:
            h[a["header"]] = a["format"].format(token=self.token)
        return h

    def url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else self.base + path

    def call(self, method: str, path: str, *, params=None, body=None, ok=None, raw=False, timeout=300):
        resp = self.session.request(method.upper(), self.url(path), params=params, json=body,
                                    headers=self.headers(), timeout=timeout)
        if raw:
            return resp
        good = ok or range(200, 300)
        if resp.status_code not in good:
            raise HarnessError(f"{method.upper()} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 204 or not resp.content:
            return None
        data = resp.json()
        return lookup(data, self.unwrap) if self.unwrap and isinstance(data, dict) and self.unwrap in data else data

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body=body, **kw)

    def put(self, path, body=None, **kw):
        return self.call("PUT", path, body=body, **kw)

    def patch(self, path, body=None, **kw):
        return self.call("PATCH", path, body=body, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", path, **kw)

    def request(self, spec: dict, scope: dict, raw: bool = True):
        """Sends a request described like a case ({method, path, query, body}) after rendering it."""
        r = render(spec, scope)
        return self.call(r["method"], r["path"], params=r.get("query"), body=r.get("body"), raw=raw)
