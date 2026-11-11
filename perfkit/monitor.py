"""Resource sampler that runs beside each k6 run (in a thread).

Every monitoring.interval_seconds it appends one row per source to
<raw>/<key>.resources.csv:  ts_ms, source, cpu_pct, mem_mb, mem_limit_mb, extra

Sources:
  <name>   each container in monitoring.docker.containers (docker stats; over SSH
           when monitoring.docker.ssh is set -- key-based login only)
  loadgen  the machine running k6
  k6       the k6 process
  pg       connection counts, when monitoring.database is configured
"""

from __future__ import annotations

import csv
import json
import subprocess
import threading
import time
from pathlib import Path

import psutil

from . import db

FIELDS = ["ts_ms", "source", "cpu_pct", "mem_mb", "mem_limit_mb", "extra"]


def _mb(text: str) -> float:
    text = text.strip()
    units = {"B": 1 / 1048576, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "TiB": 1048576,
             "kB": 1 / 1000 / 1.048576, "MB": 1 / 1.048576, "GB": 1000 / 1.048576}
    for unit in sorted(units, key=len, reverse=True):
        if text.endswith(unit):
            try:
                return float(text[: -len(unit)]) * units[unit]
            except ValueError:
                return 0.0
    try:
        return float(text or 0)
    except ValueError:
        return 0.0


def docker_stats(containers: dict[str, str], ssh: str | None = None) -> list[dict]:
    if not containers:
        return []
    cmd = ["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers.values()]
    if ssh:
        remote = "docker stats --no-stream --format '{{json .}}' " + " ".join(containers.values())
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh, remote]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    by_name = {v: k for k, v in containers.items()}
    result = []
    for line in out.splitlines():
        try:
            s = json.loads(line)
        except ValueError:
            continue
        used, _, limit = s.get("MemUsage", "0B / 0B").partition("/")
        result.append({"source": by_name.get(s.get("Name"), s.get("Name")),
                       "cpu_pct": float(s.get("CPUPerc", "0%").rstrip("%") or 0),
                       "mem_mb": round(_mb(used), 1), "mem_limit_mb": round(_mb(limit), 1), "extra": ""})
    return result


class Monitor:
    def __init__(self, cfg: dict, path: Path, k6_pid: int | None):
        self.cfg = cfg
        self.path = path
        self.k6_pid = k6_pid
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "Monitor":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=60)

    def _run(self) -> None:
        mon = self.cfg.get("monitoring") or {}
        interval = float(mon.get("interval_seconds") or 5)
        dock = mon.get("docker") or {}
        containers = dict(dock.get("containers") or {})
        ssh = dock.get("ssh")
        conn = None
        if db.configured(self.cfg):
            try:
                conn = db.connect(self.cfg)
            except Exception:  # noqa: BLE001
                conn = None
        try:
            k6 = psutil.Process(self.k6_pid) if self.k6_pid else None
            if k6:
                k6.cpu_percent(None)
        except psutil.Error:
            k6 = None
        psutil.cpu_percent(None)
        total_mb = psutil.virtual_memory().total / 1048576
        with self.path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            while not self._stop.is_set():
                t0 = time.monotonic()
                ts = int(time.time() * 1000)
                rows = docker_stats(containers, ssh)
                vm = psutil.virtual_memory()
                rows.append({"source": "loadgen", "cpu_pct": psutil.cpu_percent(None),
                             "mem_mb": round((vm.total - vm.available) / 1048576, 1),
                             "mem_limit_mb": round(total_mb, 1), "extra": ""})
                if k6:
                    try:
                        rows.append({"source": "k6", "cpu_pct": k6.cpu_percent(None),
                                     "mem_mb": round(k6.memory_info().rss / 1048576, 1), "mem_limit_mb": "",
                                     "extra": ""})
                    except psutil.Error:
                        k6 = None
                if conn is not None:
                    try:
                        rows.append({"source": "pg", "cpu_pct": "", "mem_mb": "", "mem_limit_mb": "",
                                     "extra": json.dumps(db.connection_counts(conn))})
                    except Exception:  # noqa: BLE001
                        pass
                for row in rows:
                    row["ts_ms"] = ts
                    writer.writerow(row)
                fh.flush()
                self._stop.wait(max(0.0, interval - (time.monotonic() - t0)))
        if conn is not None:
            conn.close()
