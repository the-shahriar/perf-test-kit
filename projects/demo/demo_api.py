"""A tiny in-memory orders API, used to try the kit end to end without a real system.

    python projects/demo/demo_api.py            # listens on http://127.0.0.1:8765

Accounts: user001 ... user999 with password demo-pass; admin / admin-pass.
Standard library only. Not for anything except exercising the kit.
"""

from __future__ import annotations

import json
import random
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
ORDERS: dict[int, dict] = {}
NEXT_ID = [1]
TOKENS: dict[str, str] = {}
EXPORTS: dict[str, dict] = {}


def _user_ok(username: str, password: str) -> bool:
    if username == "admin":
        return password == "admin-pass"
    return bool(re.fullmatch(r"user\d{3}", username or "")) and password == "demo-pass"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _send(self, status: int, payload=None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def _user(self) -> str | None:
        h = self.headers.get("Authorization") or ""
        return TOKENS.get(h[7:]) if h.startswith("Bearer ") else None

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str) -> None:
        url = urlparse(self.path)
        path, q = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
        body = self._body() if method in ("POST", "PATCH") else {}
        if path == "/health":
            return self._send(200, {"status": "ok", "orders": len(ORDERS)})
        if path == "/api/login" and method == "POST":
            if not _user_ok(body.get("username"), body.get("password")):
                return self._send(401, {"message": "invalid credentials"})
            token = secrets.token_hex(16)
            TOKENS[token] = body["username"]
            return self._send(200, {"token": token, "user": {"username": body["username"]}})
        user = self._user()
        if not user:
            return self._send(401, {"message": "not signed in"})
        if path == "/api/logout" and method == "POST":
            TOKENS.pop((self.headers.get("Authorization") or "")[7:], None)
            return self._send(204)
        if path == "/api/me":
            return self._send(200, {"username": user})
        if path == "/api/dashboard":
            with LOCK:
                rows = list(ORDERS.values())
            by_status: dict[str, int] = {}
            total = 0.0
            for r in rows:                       # deliberately O(n): grows with data volume
                by_status[r["status"]] = by_status.get(r["status"], 0) + 1
                total += r["amount"]
            return self._send(200, {"orders": len(rows), "by_status": by_status, "amount": round(total, 2)})
        if path == "/api/orders/count":
            return self._send(200, {"count": len(ORDERS)})
        if path == "/api/orders/bulk" and method == "POST":
            with LOCK:
                for item in body.get("items") or []:
                    oid = NEXT_ID[0]
                    NEXT_ID[0] += 1
                    ORDERS[oid] = {"id": oid, "version": 1, **item}
            return self._send(201, {"created": len(body.get("items") or [])})
        if path == "/api/orders/purge" and method == "POST":
            prefix = body.get("ref_prefix") or "\0"
            with LOCK:
                ids = [k for k, v in ORDERS.items() if str(v.get("ref", "")).startswith(prefix)]
                for k in ids:
                    del ORDERS[k]
            return self._send(200, {"deleted": len(ids)})
        if path == "/api/orders" and method == "GET":
            return self._list(q)
        if path == "/api/orders" and method == "POST":
            with LOCK:
                oid = NEXT_ID[0]
                NEXT_ID[0] += 1
                ORDERS[oid] = {"id": oid, "version": 1, **body}
            return self._send(201, ORDERS[oid])
        m = re.fullmatch(r"/api/orders/(\d+)", path)
        if m:
            oid = int(m.group(1))
            if oid not in ORDERS:
                return self._send(404, {"message": "no such order"})
            if method == "GET":
                return self._send(200, ORDERS[oid])
            if method == "PATCH":
                with LOCK:
                    o = ORDERS[oid]
                    if body.get("version") is not None and body["version"] != o["version"]:
                        return self._send(409, {"message": "version conflict"})
                    o.update({k: v for k, v in body.items() if k not in ("id", "version")})
                    o["version"] += 1
                return self._send(200, ORDERS[oid])
            if method == "DELETE":
                with LOCK:
                    ORDERS.pop(oid, None)
                return self._send(204)
        if path == "/api/exports" and method == "POST":
            eid = secrets.token_hex(6)
            EXPORTS[eid] = {"id": eid, "status": "pending", "ready_at": time.time() + random.uniform(0.5, 1.5)}
            return self._send(202, {"id": eid, "status": "pending"})
        m = re.fullmatch(r"/api/exports/(\w+)", path)
        if m and m.group(1) in EXPORTS:
            e = EXPORTS[m.group(1)]
            if e["status"] == "pending" and time.time() >= e["ready_at"]:
                e["status"] = "completed"
            return self._send(200, {"id": e["id"], "status": e["status"]})
        return self._send(404, {"message": f"no route {method} {path}"})

    def _list(self, q: dict) -> None:
        limit = min(int(q.get("limit", 50)), 5000)
        offset = int(q.get("offset", 0))
        with LOCK:
            rows = list(ORDERS.values())
        if q.get("status"):
            rows = [r for r in rows if r.get("status") == q["status"]]
        if q.get("min_amount"):
            rows = [r for r in rows if r.get("amount", 0) >= float(q["min_amount"])]
        if q.get("q"):
            term = q["q"].lower()
            rows = [r for r in rows if term in str(r.get("customer", "")).lower()]
        if q.get("sort"):
            key = q["sort"].lstrip("-")
            rows.sort(key=lambda r: r.get(key) or 0, reverse=q["sort"].startswith("-"))
        page = rows[offset: offset + limit]
        nxt = offset + limit if offset + limit < len(rows) else None
        if q.get("fields") == "id":
            page = [{"id": r["id"]} for r in page]
        self._send(200, {"items": page, "total": len(rows), "next_offset": nxt})


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    server.daemon_threads = True
    print(f"Demo API on http://127.0.0.1:{a.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
