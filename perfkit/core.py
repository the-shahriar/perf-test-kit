"""Small shared helpers: paths, logging, JSON files, pacing, k6 discovery."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
K6_SCRIPTS = KIT / "k6"
PROJECTS = KIT / "projects"
DEFAULTS_FILE = Path(__file__).resolve().parent / "defaults.yaml"

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# A piped Windows console defaults to cp1252; a progress line must never stop a long run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class HarnessError(RuntimeError):
    """An expected failure with a message meant for the person running the kit."""


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Pacer:
    """Keeps a loop at or under N calls per minute (most rate limits are per minute)."""

    def __init__(self, per_minute: float | None):
        self.interval = 60.0 / per_minute if per_minute else 0.0
        self._next = time.monotonic()

    def wait(self) -> None:
        if not self.interval:
            return
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = max(now, self._next) + self.interval


def find_k6() -> Path:
    """PERF_K6, then tools/k6/k6(.exe) in the kit, then k6 on PATH."""
    if os.environ.get("PERF_K6"):
        return Path(os.environ["PERF_K6"])
    bundled = KIT / "tools" / "k6" / ("k6.exe" if os.name == "nt" else "k6")
    if bundled.exists():
        return bundled
    found = shutil.which("k6")
    return Path(found) if found else bundled


def lookup(obj, path: str):
    """Dotted path into dicts/lists: 'data.items.0.id'. None when any part is missing."""
    if path in (None, "", "."):
        return obj
    for part in str(path).split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj
