"""The test types. Each turns a test definition (defaults.yaml + project.yaml) into k6 runs.

    smoke        pipeline check: every case once or twice, then a short 2-user journey
    performance  1 user, every case N times (plus a sign-in baseline)
    load         the journey at each user tier, one tier per run
    stress       tiers climb until a tier breaks stop_when (errors or p95)
    spike        baseline -> sudden peak -> back to baseline, one run, three phases
    soak         a fixed user count for a long time; the report compares time windows
    volume       grow a dataset stage by stage; 1 user + N concurrent users at each stage
"""

from __future__ import annotations

import copy
import time

from .core import HarnessError, log, read_json
from .runner import Runner


def select_cases(cfg: dict, which) -> list[str]:
    cases = cfg["cases"]
    if isinstance(which, list):
        unknown = [c for c in which if c not in cases]
        if unknown:
            raise HarnessError(f"Unknown case(s) {unknown}")
        return which
    eligible = [cid for cid, c in cases.items() if c.get("single_user", True)]
    if which == "read":
        return [cid for cid in eligible if cases[cid]["kind"] == "read"]
    if which in (None, "all"):
        return eligible
    raise HarnessError(f"`cases` must be all, read or a list of case ids, not {which!r}")


def journey_stages(ramp: int, steady: int, down: int, users: int) -> list[dict]:
    return [
        {"duration": int(ramp), "target": int(users), "phase": "rampup"},
        {"duration": int(steady), "target": int(users), "phase": "steady"},
        {"duration": int(down), "target": 0, "phase": "rampdown"},
    ]


def quick(t: dict) -> dict:
    """--quick: the same test, scaled down to minutes, to check the set-up end to end."""
    q = copy.deepcopy(t)
    for k in ("ramp_up_seconds", "ramp_down_seconds", "spike_ramp_seconds"):
        if k in q:
            q[k] = min(q[k], 10)
    for k in ("steady_seconds", "baseline_seconds", "spike_seconds", "recovery_seconds"):
        if k in q:
            q[k] = min(q[k], 30)
    for k in ("cooldown_seconds",):
        if k in q:
            q[k] = min(q[k], 5)
    if "iterations" in q:
        q["iterations"] = min(q["iterations"], 3)
    if "warmup" in q:
        q["warmup"] = min(q["warmup"], 1)
    if "login_samples" in q:
        q["login_samples"] = min(q["login_samples"] or 0, 2)
    if "tiers" in q:
        tiers = sorted({min(x, 5) for x in q["tiers"][:2]})
        q["tiers"] = [1, tiers[0]] if len(tiers) == 1 and len(t["tiers"]) > 1 and tiers[0] > 1 else tiers
    for k in ("users", "base_users", "peak_users"):
        if isinstance(q.get(k), list):
            q[k] = [min(x, 5) for x in q[k][:1]]
        elif isinstance(q.get(k), int):
            q[k] = min(q[k], 2 if k == "base_users" else 5)
    if "stages" in q:
        q["stages"] = q["stages"][:2]
    if q.get("records"):
        q["records"] = min(q["records"], q["stages"][0] if q.get("stages") else q["records"])
    if "windows" in q:
        q["windows"] = min(q["windows"], 3)
    return q


class Plan:
    def __init__(self, ctx, run_id: str, name: str, t: dict, is_quick: bool):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.name = name
        self.t = t
        self.type = t["type"]
        self.prefix = name.upper()
        self.dataset = t.get("dataset") or "default"
        self.r = Runner(ctx, run_id)
        self.r.record_plan(name, t, is_quick)

    def k6(self, key, script, run_cfg, meta):
        full_key = f"{self.prefix}-{key}"
        summary = self.r.k6(full_key, self.name, self.type, script, run_cfg, meta, self.dataset)
        return summary if summary is not None else read_json(self.r.raw / f"{full_key}.summary.json")

    def ensure_records(self) -> None:
        if self.t.get("records"):
            self.r.seed(f"{self.prefix}-SEED", int(self.t["records"]), self.dataset)

    def journey(self, key, users, ramp, steady, down, meta, stages=None, measure="steady"):
        stages = stages or journey_stages(ramp, steady, down, users)
        measured = sum(s["duration"] for s in stages if s["phase"] in (measure if isinstance(measure, list) else [measure]))
        run_cfg = {"stages": stages, "measured_phases": measure if isinstance(measure, list) else [measure],
                   "measured_seconds": measured, "users": users,
                   "think_time": self.t.get("think_time") or self.cfg.get("think_time") or [1, 3]}
        return self.k6(key, "journey.js", run_cfg, {"users": users, **meta})

    def single(self, key, which, iterations, warmup, meta, extra=None):
        cases = select_cases(self.cfg, which) + list(extra or [])
        return self.k6(key, "single-user.js",
                       {"case_list": cases, "iterations": int(iterations), "warmup": int(warmup or 0)},
                       {"users": 1, **meta})

    def login_baseline(self, samples) -> None:
        if not samples or self.cfg["auth"]["type"] != "login" or not self.cfg["auth"].get("login"):
            return
        self.k6("LOGIN", "login.js", {"samples": int(samples)}, {"users": 1, "label": f"Sign-in, {samples} samples"})

    def records_now(self):
        return self.r.record_count(self.dataset)

    # ── types ──
    def smoke(self):
        t = self.t
        self.ensure_records()
        self.login_baseline(t.get("login_samples"))
        self.single("1U", "all", t["iterations"], 0, {"label": f"Every case x{t['iterations']}"})
        if (self.cfg.get("journey") or {}).get("steps"):
            self.journey("JOURNEY", t["users"], 5, t["steady_seconds"], 5,
                         {"label": f"Journey, {t['users']} users, {t['steady_seconds']} s"})

    def performance(self):
        t = self.t
        self.ensure_records()
        self.login_baseline(t.get("login_samples"))
        self.single("1U", t.get("cases", "all"), t["iterations"], t.get("warmup", 0),
                    {"label": f"1 user, {t['iterations']} iterations per case", "stage_records": self.records_now()},
                    t.get("extra_cases"))

    def load(self):
        t = self.t
        self.ensure_records()
        for users in t["tiers"]:
            self.journey(f"{users:03d}U", users, t["ramp_up_seconds"], t["steady_seconds"], t["ramp_down_seconds"],
                         {"label": f"{users} concurrent users", "stage_records": self.records_now()})
            time.sleep(t["cooldown_seconds"])

    def stress(self):
        t = self.t
        self.ensure_records()
        stop = t.get("stop_when") or {}
        for users in t["tiers"]:
            summary = self.journey(f"{users:03d}U", users, t["ramp_up_seconds"], t["steady_seconds"],
                                   t["ramp_down_seconds"], {"label": f"{users} concurrent users",
                                                            "stage_records": self.records_now()})
            m = (summary or {}).get("metrics", {})
            err = ((m.get("http_req_failed") or {}).get("values") or {}).get("rate")
            p95 = ((m.get("http_req_duration") or {}).get("values") or {}).get("p(95)")
            reasons = []
            if err is not None and stop.get("error_rate") is not None and err > stop["error_rate"]:
                reasons.append(f"error rate {err:.1%} > {stop['error_rate']:.0%}")
            if p95 is not None and stop.get("p95_ms") is not None and p95 > stop["p95_ms"]:
                reasons.append(f"p95 {p95:,.0f} ms > {stop['p95_ms']:,} ms")
            if reasons:
                log(f"Stress: stopping after {users} users ({'; '.join(reasons)})")
                self._note({"stopped_at_users": users, "reason": "; ".join(reasons)})
                return
            time.sleep(t["cooldown_seconds"])
        self._note({"stopped_at_users": None, "reason": "every tier ran within stop_when"})

    def spike(self):
        t = self.t
        self.ensure_records()
        base, peak = t["base_users"], t["peak_users"]
        stages = [
            {"duration": int(t["ramp_up_seconds"]), "target": base, "phase": "rampup"},
            {"duration": int(t["baseline_seconds"]), "target": base, "phase": "baseline"},
            {"duration": int(t["spike_ramp_seconds"]), "target": peak, "phase": "spike"},
            {"duration": int(t["spike_seconds"]), "target": peak, "phase": "spike"},
            {"duration": int(t["spike_ramp_seconds"]), "target": base, "phase": "recovery"},
            {"duration": int(t["recovery_seconds"]), "target": base, "phase": "recovery"},
            {"duration": 10, "target": 0, "phase": "rampdown"},
        ]
        self.journey(f"{base}-{peak}U", peak, 0, 0, 0, {"label": f"{base} -> {peak} -> {base} users",
                                                       "stage_records": self.records_now()},
                     stages=stages, measure=["baseline", "spike", "recovery"])

    def soak(self):
        t = self.t
        self.ensure_records()
        secs = t["steady_seconds"]
        span = f"{secs / 3600:,.1f} h" if secs >= 7200 else (f"{secs / 60:,.0f} min" if secs >= 120 else f"{secs} s")
        self.journey(f"{t['users']:03d}U", t["users"], t["ramp_up_seconds"], t["steady_seconds"],
                     t["ramp_down_seconds"], {"label": f"{t['users']} users for {span}",
                                              "windows": t.get("windows", 6), "stage_records": self.records_now()})

    def volume(self):
        t = self.t
        users_list = t["users"] if isinstance(t["users"], list) else [t["users"]]
        for idx, target in enumerate(t["stages"], 1):
            log(f"══ {self.name} stage {idx}: {target:,} records ══")
            self.r.seed(f"{self.prefix}-SEED-S{idx}", int(target), self.dataset, idx)
            actual = self.records_now()
            if actual is not None and actual > target:
                log(f"WARNING: dataset '{self.dataset}' already holds {actual:,} records, more than this stage's "
                    f"{target:,}. Stages only grow: restore the data or run volume tests first. "
                    f"The stage is reported at its actual size.")
            target = actual if actual is not None else target
            label = f"Stage {idx}: {target:,} records"
            self.single(f"S{idx}-1U", t.get("cases", "read"), t["iterations"], t.get("warmup", 0),
                        {"label": f"{label}, 1 user", "stage": idx, "stage_records": target}, t.get("extra_cases"))
            for users in users_list:
                self.journey(f"S{idx}-{users:03d}U", users, t["ramp_up_seconds"], t["steady_seconds"],
                             t["ramp_down_seconds"], {"label": f"{label}, {users} users", "stage": idx,
                                                      "stage_records": target})
                time.sleep(t["cooldown_seconds"])

    def _note(self, note: dict) -> None:
        from .core import write_json

        path = self.r.raw / "plan.json"
        plan = read_json(path)
        plan["tests"][self.name]["outcome"] = note
        write_json(path, plan)

    def run(self) -> None:
        getattr(self, self.type)()
        log(f"{self.name} complete. Raw data in {self.r.raw}")


def run_test(ctx, run_id: str, name: str, is_quick: bool = False) -> None:
    tests = ctx.cfg["tests"]
    if name not in tests:
        raise HarnessError(f"Unknown test '{name}'. Defined: {', '.join(tests)}")
    t = quick(tests[name]) if is_quick else tests[name]
    if t["type"] == "volume" and not ctx.hook("seed_to"):
        raise HarnessError("A volume test needs hooks.py with seed_to(ctx, target, dataset) and record_count(...)")
    Plan(ctx, run_id, name, t, is_quick).run()
