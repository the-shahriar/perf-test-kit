"""Facts about where a run happened, written once per run to raw/environment.json."""

from __future__ import annotations

import os
import platform
import statistics
import subprocess
import time
from pathlib import Path

import psutil
import requests

from .core import find_k6, now_iso


def _cmd(args: list[str], cwd: Path | None = None) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20, cwd=cwd).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _git(repo: Path | None) -> dict:
    if not repo or not repo.exists():
        return {}
    return {
        "repository": str(repo),
        "branch": _cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo),
        "commit": _cmd(["git", "rev-parse", "HEAD"], repo),
        "commit_short": _cmd(["git", "rev-parse", "--short", "HEAD"], repo),
        "dirty": bool(_cmd(["git", "status", "--porcelain"], repo)),
    }


def _latency(cfg: dict, samples: int = 10) -> dict:
    url = cfg["base_url"].rstrip("/") + (cfg.get("health") or "/")
    times, status = [], None
    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            status = requests.get(url, timeout=10, verify=cfg.get("tls_verify", True)).status_code
            times.append((time.perf_counter() - t0) * 1000)
        except requests.RequestException:
            pass
    if not times:
        return {"url": url, "status": "unreachable"}
    return {"url": url, "status": status, "samples": len(times), "median_ms": round(statistics.median(times), 2),
            "min_ms": round(min(times), 2), "max_ms": round(max(times), 2)}


def collect(ctx) -> dict:
    cfg = ctx.cfg
    vm = psutil.virtual_memory()
    repo = (cfg.get("project") or {}).get("repo")
    repo_path = (ctx.project.dir / repo).resolve() if repo else None
    return {
        "collected_at": now_iso(),
        "project": {"name": ctx.project.name, "folder": str(ctx.project.dir), **(cfg.get("project") or {})},
        "target": {"name": ctx.target, "base_url": cfg["base_url"], "api_prefix": cfg.get("api_prefix"),
                   "facts": cfg.get("facts") or {}},
        "load_generator": {
            "host": platform.node(), "os": f"{platform.system()} {platform.release()}",
            "cpu": platform.processor() or platform.machine(), "cores_logical": psutil.cpu_count(),
            "cores_physical": psutil.cpu_count(logical=False), "memory_gb": round(vm.total / 1024 ** 3, 1),
            "python": platform.python_version(), "k6": _cmd([str(find_k6()), "version"]),
            "user": os.environ.get("USERNAME") or os.environ.get("USER"),
        },
        "network": _latency(cfg),
        "git": _git(repo_path),
        "extra": ctx.call("environment", default={}) or {},
    }
