"""Turns a run's raw files into the numbers every report prints.

Single source of truth: the per-request samples k6 wrote to <key>.csv.gz.
Percentiles use k6's method (linear interpolation at pct*(n-1)), so every value
can be checked against k6's own <key>.summary.json (see cross_check()).

Which samples count:
- requests tagged with a case id; AUX requests are set-up steps and excluded;
- single-user runs: phase 'measure' (warm-up excluded);
- journeys: the phases named in the run's metadata (normally 'steady').
A request is an error when k6 marked it unexpected (expected_response=false):
by default any status outside 200-399, or outside the case's `expect:` list.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..core import read_json, read_jsonl

DIST_BUCKETS = [(0, 100, "< 100 ms"), (100, 250, "100–250 ms"), (250, 500, "250–500 ms"),
                (500, 1000, "500 ms–1 s"), (1000, 2000, "1–2 s"), (2000, 5000, "2–5 s"), (5000, math.inf, "> 5 s")]


def percentile(sorted_values: list[float], pct: float) -> float | None:
    n = len(sorted_values)
    if n == 0:
        return None
    i = pct * (n - 1)
    lo, hi = math.floor(i), math.ceil(i)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (i - lo)


@dataclass
class Stats:
    count: int = 0
    errors: int = 0
    min: float | None = None
    avg: float | None = None
    p50: float | None = None
    p90: float | None = None
    p95: float | None = None
    p99: float | None = None
    max: float | None = None
    bytes_avg: float | None = None
    status: dict = field(default_factory=dict)
    dist: list = field(default_factory=list)

    @property
    def error_rate(self) -> float | None:
        return self.errors / self.count if self.count else None


def make_stats(durations: list[float], ok: list[bool] | None = None, statuses: list[str] | None = None,
               sizes: list[float] | None = None) -> Stats:
    s = Stats()
    if not durations:
        return s
    d = sorted(durations)
    s.count = len(d)
    s.min, s.max = d[0], d[-1]
    s.avg = sum(d) / len(d)
    s.p50, s.p90, s.p95, s.p99 = (percentile(d, p) for p in (0.5, 0.9, 0.95, 0.99))
    s.errors = sum(1 for x in (ok or []) if not x)
    s.status = dict(sorted(Counter(statuses or []).items()))
    if sizes:
        s.bytes_avg = sum(sizes) / len(sizes)
    s.dist = [sum(1 for v in d if lo <= v < hi) for lo, hi, _ in DIST_BUCKETS]
    return s


def _tags(extra: str) -> dict:
    out = {}
    for part in (extra or "").split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def _ts(value: str) -> int:
    v = float(value)
    return int(v * 1000) if v < 1e11 else int(v)


def iso(ts_ms: int | None) -> str:
    if ts_ms is None:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class RunData:
    key: str
    meta: dict
    measured: list
    cases: dict = field(default_factory=dict)            # case -> Stats, measured phases
    overall: Stats = field(default_factory=Stats)
    phases: dict = field(default_factory=dict)           # phase -> Stats (every phase seen)
    phase_cases: dict = field(default_factory=dict)      # phase -> case -> Stats
    windows: list = field(default_factory=list)          # soak: [(label, Stats, throughput)]
    window_s: float | None = None
    throughput: float | None = None
    window_start: int | None = None
    window_end: int | None = None
    iterations: int = 0
    iteration_s: Stats = field(default_factory=Stats)
    journey_active: Stats = field(default_factory=Stats)
    completed: dict = field(default_factory=dict)        # case -> Stats (polled jobs)
    errors: list = field(default_factory=list)
    timeseries: list = field(default_factory=list)
    resources: dict = field(default_factory=dict)
    pg_conn: dict = field(default_factory=dict)
    db: dict = field(default_factory=dict)
    all_samples_p95: float | None = None
    k6_summary: dict = field(default_factory=dict)

    @property
    def users(self) -> int:
        return int(self.meta.get("users") or 1)

    @property
    def label(self) -> str:
        return self.meta.get("label") or self.key

    @property
    def is_journey(self) -> bool:
        return self.meta.get("script") == "journey.js"


def load_run(raw: Path, meta: dict) -> RunData:
    key = meta["key"]
    cfg = meta.get("config", {})
    measured = cfg.get("measured_phases") or ["measure"]
    rd = RunData(key=key, meta=meta, measured=measured)
    path = raw / f"{key}.csv.gz"
    if not path.exists():
        return rd
    total_s = sum(s["duration"] for s in cfg.get("stages") or []) or 0
    bucket_s = 10 if total_s <= 1800 else max(10, int(total_s / 180 / 10) * 10)

    per_case = defaultdict(lambda: [[], [], [], []])      # durations, ok, statuses, sizes
    per_phase = defaultdict(lambda: defaultdict(lambda: [[], [], [], []]))
    sizes = defaultdict(list)
    all_durations, buckets = [], defaultdict(lambda: {"n": 0, "err": 0, "d": [], "vus": 0})
    iter_d, active_d, completed, ts_measured, measured_samples = [], [], defaultdict(list), [], []
    errors: dict[tuple, dict] = {}
    t0 = None
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            metric = row["metric_name"]
            if metric not in ("http_req_duration", "response_bytes", "iteration_duration", "journey_active_ms",
                              "error_detail", "completed_ms", "vus"):
                continue
            ts = _ts(row["timestamp"])
            t0 = ts if t0 is None else min(t0, ts)
            tags = _tags(row.get("extra_tags", ""))
            phase, tc = tags.get("phase", ""), tags.get("tc", "")
            value = float(row["metric_value"])
            bucket = buckets[(ts // 1000) // bucket_s]
            if metric == "vus":
                bucket["vus"] = max(bucket["vus"], int(value))
                continue
            measured_case = tc not in ("", "AUX") and phase in measured
            if metric == "http_req_duration":
                all_durations.append(value)
                status = row.get("status") or "0"
                exp = row.get("expected_response")
                ok = (exp == "true") if exp in ("true", "false") else status.startswith(("2", "3"))
                if tc not in ("", "AUX"):
                    bucket["n"] += 1
                    bucket["d"].append(value)
                    bucket["err"] += 0 if ok else 1
                    pp = per_phase[phase][tc]
                    pp[0].append(value), pp[1].append(ok), pp[2].append(status)
                if measured_case:
                    pc = per_case[tc]
                    pc[0].append(value), pc[1].append(ok), pc[2].append(status)
                    ts_measured.append(ts)
                    measured_samples.append((ts, value, ok))
            elif metric == "response_bytes" and measured_case:
                sizes[tc].append(value)
            elif metric == "iteration_duration" and phase in measured:
                iter_d.append(value / 1000)
            elif metric == "journey_active_ms" and phase in measured:
                active_d.append(value)
            elif metric == "completed_ms" and measured_case:
                completed[tc].append(value)
            elif metric == "error_detail" and measured_case:
                k = (tc, tags.get("status", ""), tags.get("msg", ""))
                e = errors.setdefault(k, {"tc": tc, "status": k[1], "msg": k[2], "count": 0, "first": ts})
                e["count"] += int(value)
                e["first"] = min(e["first"], ts)

    rd.cases = {tc: make_stats(d, o, s, sizes.get(tc)) for tc, (d, o, s, _) in sorted(per_case.items())}
    all_d, all_o, all_s = [], [], []
    for d, o, s, _ in per_case.values():
        all_d += d
        all_o += o
        all_s += s
    rd.overall = make_stats(all_d, all_o, all_s, [v for vs in sizes.values() for v in vs])
    for ph, cases in per_phase.items():
        rd.phase_cases[ph] = {tc: make_stats(d, o, s) for tc, (d, o, s, _) in sorted(cases.items())}
        pd, po = [], []
        for d, o, _, _ in cases.values():
            pd += d
            po += o
        rd.phases[ph] = make_stats(pd, po)
    rd.all_samples_p95 = percentile(sorted(all_durations), 0.95)
    if ts_measured:
        rd.window_start, rd.window_end = min(ts_measured), max(ts_measured)
    if rd.is_journey:
        rd.window_s = float(cfg.get("measured_seconds") or 0) or None
    elif ts_measured:
        rd.window_s = max(0.001, (max(ts_measured) - min(ts_measured)) / 1000)
    if rd.window_s and rd.overall.count and meta.get("script") != "login.js":   # sign-ins are paced on purpose
        rd.throughput = rd.overall.count / rd.window_s
    n_windows = int(meta.get("windows") or 0)
    if n_windows > 1 and measured_samples and rd.window_end > rd.window_start:
        span = (rd.window_end - rd.window_start) / n_windows
        groups = defaultdict(list)
        for ts, v, ok in measured_samples:
            groups[min(n_windows - 1, int((ts - rd.window_start) / span))].append((v, ok))
        for i in range(n_windows):
            vals = groups.get(i, [])
            st = make_stats([v for v, _ in vals], [ok for _, ok in vals])
            a, b = i * span / 1000, (i + 1) * span / 1000
            lbl = f"{a / 60:,.0f}–{b / 60:,.0f} min" if span >= 120_000 else f"{a:,.0f}–{b:,.0f} s"
            rd.windows.append((lbl, st, len(vals) / (span / 1000) if span else None))
    rd.iterations = len(iter_d)
    rd.iteration_s = make_stats(iter_d)
    rd.journey_active = make_stats(active_d)
    rd.completed = {tc: make_stats(v) for tc, v in sorted(completed.items())}
    rd.errors = sorted(errors.values(), key=lambda e: (-e["count"], e["tc"]))
    if t0 is not None:
        start_bucket = (t0 // 1000) // bucket_s
        for b in sorted(buckets):
            v = buckets[b]
            rd.timeseries.append({"t_s": (b - start_bucket) * bucket_s, "vus": v["vus"], "rps": v["n"] / bucket_s,
                                  "p95": percentile(sorted(v["d"]), 0.95), "errors": v["err"]})
    _load_resources(raw, rd)
    rd.db = read_json(raw / f"{key}.db.json")
    rd.k6_summary = read_json(raw / f"{key}.summary.json")
    return rd


def _load_resources(raw: Path, rd: RunData) -> None:
    path = raw / f"{rd.key}.resources.csv"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        all_rows = list(csv.DictReader(fh))
    lo, hi = rd.window_start or 0, rd.window_end or float("inf")
    chosen = [r for r in all_rows if lo <= int(r["ts_ms"]) <= hi]
    if not chosen:
        # A run shorter than the sampling interval: use every sample of the run.
        chosen = all_rows
    window: dict[str, list] = defaultdict(list)
    pg_rows = []
    for row in chosen:
        if row["source"] == "pg":
            try:
                pg_rows.append(json.loads(row["extra"]))
            except ValueError:
                pass
            continue
        cpu = float(row["cpu_pct"]) if row["cpu_pct"] else None
        mem = float(row["mem_mb"]) if row["mem_mb"] else None
        limit = float(row["mem_limit_mb"]) if row["mem_limit_mb"] else None
        window[row["source"]].append((cpu, mem, limit))
    for src, vals in window.items():
        cpus = [c for c, _, _ in vals if c is not None]
        mems = [m for _, m, _ in vals if m is not None]
        rd.resources[src] = {
            "samples": len(vals), "cpu_avg": sum(cpus) / len(cpus) if cpus else None,
            "cpu_max": max(cpus) if cpus else None, "mem_avg": sum(mems) / len(mems) if mems else None,
            "mem_max": max(mems) if mems else None, "mem_limit": next((x for _, _, x in vals if x), None),
        }
    if pg_rows:
        rd.pg_conn = {
            "total_max": max(r["total"] for r in pg_rows), "total_avg": sum(r["total"] for r in pg_rows) / len(pg_rows),
            "active_max": max(r["active"] for r in pg_rows), "active_avg": sum(r["active"] for r in pg_rows) / len(pg_rows),
            "idle_in_tx_max": max(r["idle_in_tx"] for r in pg_rows),
        }


@dataclass
class Suite:
    run_id: str
    dir: Path
    environment: dict
    plan: dict
    runs: dict
    seeds: list
    objects: dict

    def by_test(self, test: str) -> list[RunData]:
        return [r for r in self.runs.values() if r.meta.get("test") == test]

    def tests(self) -> list[str]:
        seen = []
        for r in self.runs.values():
            if r.meta.get("test") not in seen:
                seen.append(r.meta.get("test"))
        return seen

    def window(self, test: str) -> tuple[str, str]:
        rs = self.by_test(test)
        prefix = test.upper() + "-"
        seeds = [s for s in self.seeds if s["key"].startswith(prefix)]
        starts = [r.meta["started"] for r in rs] + [s["started"] for s in seeds if s.get("started")]
        ends = [r.meta["ended"] for r in rs] + [s["ended"] for s in seeds if s.get("ended")]
        return (min(starts) if starts else "", max(ends) if ends else "")


def load_suite(run_dir: Path) -> Suite:
    raw = run_dir / "raw"
    runs = {m["key"]: load_run(raw, m) for m in read_jsonl(raw / "runs.jsonl")}
    seeds = read_jsonl(raw / "seed.jsonl")
    return Suite(run_id=run_dir.name, dir=run_dir, environment=read_json(raw / "environment.json"),
                 plan=read_json(raw / "plan.json"), runs=runs, seeds=seeds, objects=read_json(raw / "test_objects.json"))


def cross_check(rd: RunData) -> dict | None:
    """Our p95 over every HTTP request vs k6's own p95 for the same run."""
    k6 = rd.k6_summary.get("metrics", {}).get("http_req_duration", {}).get("values", {})
    if "p(95)" not in k6 or rd.all_samples_p95 is None:
        return None
    return {"k6_p95": k6["p(95)"], "computed_p95": rd.all_samples_p95,
            "difference_ms": abs(k6["p(95)"] - rd.all_samples_p95)}
