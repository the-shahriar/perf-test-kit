"""Builds one report per test in a run folder: <doc_prefix>-<TEST>.xlsx and .html.

The reports state measured values. Pass/fail appears only when the project
defines `slo:`; without it the reports carry no targets or verdicts.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .loader import DIST_BUCKETS, RunData, Suite, cross_check, iso, load_suite
from .model import Chart, Col, Doc, Section, Table

TYPE_TITLES = {
    "smoke": ("Smoke Test Report", "Checks that every case and the journey run end to end."),
    "performance": ("Performance Test Report", "Response time of each operation for a single user."),
    "load": ("Load Test Report", "How response time, throughput and errors change with concurrent users."),
    "stress": ("Stress Test Report", "Where the system stops coping as concurrent users keep rising."),
    "spike": ("Spike Test Report", "How the system handles a sudden surge of users, and whether it recovers."),
    "soak": ("Soak Test Report", "Whether performance holds steady over a long period at constant load."),
    "volume": ("Volume Test Report", "How response time changes as the amount of data grows."),
}

STATS_COLS = [Col("Requests", "int"), Col("Errors", "int"), Col("Error rate", "pct"), Col("Min ms", "ms"),
              Col("Avg ms", "ms"), Col("p50 ms", "ms"), Col("p90 ms", "ms"), Col("p95 ms", "ms"),
              Col("p99 ms", "ms"), Col("Max ms", "ms")]


def _stats_row(s) -> list:
    return [s.count, s.errors, s.error_rate, s.min, s.avg, s.p50, s.p90, s.p95, s.p99, s.max]


def _fmt_ts(ts: str) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).strftime("%d %b %Y %H:%M UTC")
    except ValueError:
        return ts


class Builder:
    def __init__(self, suite: Suite, test: str):
        self.s = suite
        self.test = test
        self.runs = suite.by_test(test)
        self.type = self.runs[0].meta.get("type") if self.runs else "performance"
        self.tcfg = (suite.plan.get("tests") or {}).get(test, {})
        self.cases = suite.plan.get("cases") or {}
        self.slo = suite.plan.get("slo") or None
        self.report_cfg = suite.plan.get("report") or {}

    def label(self, tc: str) -> str:
        if tc == "LOGIN":
            return "Sign in"
        return (self.cases.get(tc) or {}).get("label", tc)

    def slo_for(self, tc: str) -> dict:
        if not self.slo:
            return {}
        return {**{k: v for k, v in self.slo.items() if k != "cases"}, **((self.slo.get("cases") or {}).get(tc) or {})}

    def slo_met(self, tc: str, s) -> str:
        t = self.slo_for(tc)
        if not t or not s.count:
            return ""
        ok = True
        if t.get("p95_ms") is not None and s.p95 is not None:
            ok &= s.p95 <= t["p95_ms"]
        if t.get("error_rate") is not None and s.error_rate is not None:
            ok &= s.error_rate <= t["error_rate"]
        return "met" if ok else "NOT met"

    # ── sections ──
    def summary(self) -> Section:
        title, purpose = TYPE_TITLES[self.type]
        sec = Section("Summary", intro=[purpose])
        start, end = self.s.window(self.test)
        env = self.s.environment
        git = env.get("git") or {}
        sec.fields = [
            ("Project", env.get("project", {}).get("name")),
            ("Test", f"{self.test} ({self.type})" + (" — quick run" if self.tcfg.get("quick") else "")),
            ("Target", f"{env.get('target', {}).get('name')} · {env.get('target', {}).get('base_url')}"),
            ("Run ID", self.s.run_id),
            ("Test window", f"{_fmt_ts(start)} – {_fmt_ts(end)}"),
        ]
        if git:
            sec.fields.append(("Code under test", f"{git.get('branch')} @ {git.get('commit_short')}"
                                                  + (" (uncommitted changes)" if git.get("dirty") else "")))
        outcome = self.tcfg.get("outcome")
        if outcome:
            sec.fields.append(("Stress outcome", (f"stopped after {outcome['stopped_at_users']} users: "
                                                  if outcome.get("stopped_at_users") else "") + outcome["reason"]))
        rows = []
        for r in self.runs:
            rows.append([r.key, r.label, r.users, r.meta.get("stage_records"), r.overall.count, r.overall.error_rate,
                         r.overall.p50, r.overall.p95, r.overall.max, r.throughput, r.meta.get("k6_exit_code")])
        sec.blocks.append(Table("Runs at a glance", [
            Col("Run"), Col("What ran"), Col("Users", "int"), Col("Records", "int"), Col("Requests", "int"),
            Col("Error rate", "pct"), Col("p50 ms", "ms"), Col("p95 ms", "ms"), Col("Max ms", "ms"),
            Col("Req/s", "num2"), Col("k6 exit", "int")], rows,
            note="Measured samples only: warm-up, ramp-up and ramp-down are excluded; helper requests (AUX) are excluded."))
        if self.slo:
            met = total = 0
            for r in self.runs:
                for tc, s in r.cases.items():
                    v = self.slo_met(tc, s)
                    if v:
                        total += 1
                        met += v == "met"
            sec.fields.append(("Service levels", f"{met} of {total} case results met their SLO"))
        return sec

    def results_by_case(self) -> Section:
        sec = Section("Results by case", intro=["One table per run. Times are milliseconds."])
        slo_cols = [Col("SLO p95 ms", "ms"), Col("SLO")] if self.slo else []
        for r in self.runs:
            rows = []
            for tc, s in r.cases.items():
                row = [tc, self.label(tc), *_stats_row(s), s.bytes_avg,
                       (s.count / r.window_s) if r.window_s else None]
                if self.slo:
                    row += [self.slo_for(tc).get("p95_ms"), self.slo_met(tc, s)]
                rows.append(row)
            rows.append(["ALL", "All cases", *_stats_row(r.overall), r.overall.bytes_avg, r.throughput]
                        + (["", ""] if self.slo else []))
            sec.blocks.append(Table(f"{r.key} — {r.label}", [Col("Case"), Col("Operation"), *STATS_COLS,
                                                              Col("Avg KB", "kb"), Col("Req/s", "num2"), *slo_cols], rows))
        return sec

    def comparison(self) -> Section | None:
        journeys = [r for r in self.runs if r.is_journey]
        singles = [r for r in self.runs if not r.is_journey and r.meta.get("script") == "single-user.js"]
        if self.type in ("load", "stress") and len(journeys) > 1:
            sec = Section("Comparison across user tiers")
            x = [r.users for r in journeys]
            sec.blocks.append(Table("Tiers", [Col("Users", "int"), Col("Requests", "int"), Col("Req/s", "num2"),
                                              Col("Error rate", "pct"), Col("p50 ms", "ms"), Col("p95 ms", "ms"),
                                              Col("p99 ms", "ms"), Col("Journey active ms (avg)", "ms")],
                                    [[r.users, r.overall.count, r.throughput, r.overall.error_rate, r.overall.p50,
                                      r.overall.p95, r.overall.p99, r.journey_active.avg] for r in journeys]))
            sec.blocks.append(Chart("p95 response time by users", "Concurrent users", "ms", x,
                                    {"p95": [r.overall.p95 for r in journeys], "p50": [r.overall.p50 for r in journeys]}))
            sec.blocks.append(Chart("Throughput by users", "Concurrent users", "requests/s", x,
                                    {"Req/s": [r.throughput for r in journeys]}))
            sec.blocks.append(self._matrix("p95 by case and tier (ms)", journeys, lambda r: f"{r.users} users"))
            return sec
        if self.type == "volume" and self.runs:
            sec = Section("Comparison across data stages")
            for group, runs in (("1 user", singles), ("concurrent", journeys)):
                if not runs:
                    continue
                by_users = {}
                for r in runs:
                    by_users.setdefault(r.users, []).append(r)
                for users, rs in by_users.items():
                    x = [r.meta.get("stage_records") for r in rs]
                    sec.blocks.append(Table(f"Stages — {users} user(s)", [
                        Col("Records", "int"), Col("Requests", "int"), Col("Req/s", "num2"), Col("Error rate", "pct"),
                        Col("p50 ms", "ms"), Col("p95 ms", "ms"), Col("Max ms", "ms")],
                        [[r.meta.get("stage_records"), r.overall.count, r.throughput, r.overall.error_rate,
                          r.overall.p50, r.overall.p95, r.overall.max] for r in rs]))
                    sec.blocks.append(Chart(f"p95 by record count — {users} user(s)", "Records", "ms", x,
                                            {"p95": [r.overall.p95 for r in rs], "p50": [r.overall.p50 for r in rs]}))
                    sec.blocks.append(self._matrix(f"p95 by case and stage — {users} user(s) (ms)", rs,
                                                   lambda r: f"{r.meta.get('stage_records', 0):,}"))
            return sec
        return None

    def _matrix(self, title, runs: list[RunData], head) -> Table:
        cases = []
        for r in runs:
            for tc in r.cases:
                if tc not in cases:
                    cases.append(tc)
        return Table(title, [Col("Case"), Col("Operation")] + [Col(head(r), "ms") for r in runs],
                     [[tc, self.label(tc)] + [(r.cases.get(tc).p95 if r.cases.get(tc) else None) for r in runs]
                      for tc in cases])

    def phases(self) -> Section | None:
        journeys = [r for r in self.runs if r.is_journey]
        if not journeys:
            return None
        sec = Section("Phases", intro=["Every phase of each concurrent run, including those the result tables exclude."])
        order = ["rampup", "steady", "baseline", "spike", "recovery", "rampdown"]
        for r in journeys:
            phases = sorted(r.phases, key=lambda p: order.index(p) if p in order else 99)
            sec.blocks.append(Table(f"{r.key} — {r.label}", [Col("Phase"), Col("Measured"), *STATS_COLS],
                                    [[p, "yes" if p in r.measured else "no", *_stats_row(r.phases[p])] for p in phases]))
            if self.type == "spike":
                cases = sorted({tc for p in r.phase_cases.values() for tc in p})
                ph = [p for p in phases if p in ("baseline", "spike", "recovery")]
                sec.blocks.append(Table("p95 by case and phase (ms)", [Col("Case"), Col("Operation")] + [Col(p, "ms") for p in ph],
                                        [[tc, self.label(tc)] + [getattr(r.phase_cases.get(p, {}).get(tc), "p95", None) for p in ph]
                                         for tc in cases]))
            if r.windows:
                sec.blocks.append(Table("Steady phase in equal time windows", [
                    Col("Window"), Col("Req/s", "num2"), *STATS_COLS],
                    [[lbl, thr, *_stats_row(st)] for lbl, st, thr in r.windows],
                    note="A rising p95 or error rate from the first window to the last points to degradation over time "
                         "(leaks, growing queues or tables, exhausted pools)."))
                sec.blocks.append(Chart("p95 per time window", "Window", "ms", [w[0] for w in r.windows],
                                        {"p95": [w[1].p95 for w in r.windows], "p50": [w[1].p50 for w in r.windows]}))
        return sec

    def timeseries(self) -> Section | None:
        journeys = [r for r in self.runs if r.is_journey and r.timeseries]
        if not journeys:
            return None
        sec = Section("Over time", intro=["Every request with a case id, in fixed time buckets from the start of the run "
                                          "(all phases)."])
        for r in journeys:
            ts = r.timeseries
            x = [p["t_s"] for p in ts]
            sec.blocks.append(Chart(f"{r.key}: p95 response time over time", "Seconds", "ms", x,
                                    {"p95 ms": [p["p95"] for p in ts]}))
            sec.blocks.append(Chart(f"{r.key}: active users and throughput", "Seconds", "users / requests per second",
                                    x, {"Users": [p["vus"] for p in ts], "Req/s": [p["rps"] for p in ts]}))
            if any(p["errors"] for p in ts):
                sec.blocks.append(Chart(f"{r.key}: errors per bucket", "Seconds", "errors", x,
                                        {"Errors": [p["errors"] for p in ts]}))
            sec.blocks.append(Table(f"{r.key} — buckets", [Col("t (s)", "int"), Col("Users", "int"), Col("Req/s", "num2"),
                                                           Col("p95 ms", "ms"), Col("Errors", "int")],
                                    [[p["t_s"], p["vus"], p["rps"], p["p95"], p["errors"]] for p in ts]))
        return sec

    def distribution(self) -> Section | None:
        singles = [r for r in self.runs if not r.is_journey]
        if not singles:
            return None
        sec = Section("Response-time distribution")
        for r in singles:
            sec.blocks.append(Table(f"{r.key} — requests per band", [Col("Case"), Col("Operation")]
                                    + [Col(b[2], "int") for b in DIST_BUCKETS] + [Col("Total", "int")],
                                    [[tc, self.label(tc), *s.dist, s.count] for tc, s in r.cases.items() if s.count]))
        return sec

    def jobs(self) -> Section | None:
        rows = [[r.key, tc, self.label(tc), *_stats_row(s)[0:1], s.avg, s.p50, s.p95, s.max]
                for r in self.runs for tc, s in r.completed.items() if s.count]
        if not rows:
            return None
        return Section("Background jobs", intro=["Cases with `poll:` — time from the request to the job reaching its "
                                                 "done state, as the user waits for it."],
                       blocks=[Table("Time to completion", [Col("Run"), Col("Case"), Col("Operation"), Col("Jobs", "int"),
                                                            Col("Avg ms", "ms"), Col("p50 ms", "ms"), Col("p95 ms", "ms"),
                                                            Col("Max ms", "ms")], rows)])

    def errors(self) -> Section:
        rows = [[r.key, e["tc"], self.label(e["tc"]), e["status"], e["count"], e["msg"], iso(e["first"])]
                for r in self.runs for e in r.errors]
        status_rows = []
        for r in self.runs:
            for tc, s in r.cases.items():
                for code, n in s.status.items():
                    status_rows.append([r.key, tc, code, n])
        sec = Section("Errors", intro=["No errors in the measured samples."] if not rows else [])
        if rows:
            sec.blocks.append(Table("Errors by case, status and message", [
                Col("Run"), Col("Case"), Col("Operation"), Col("Status"), Col("Count", "int"), Col("Message"),
                Col("First seen")], rows))
        sec.blocks.append(Table("HTTP status codes", [Col("Run"), Col("Case"), Col("Status"), Col("Count", "int")],
                                status_rows))
        return sec

    def resources(self) -> Section:
        sec = Section("Resources", intro=["Sampled every few seconds during each run's measured window. Container rows "
                                          "appear only when monitoring.docker is configured; loadgen is the machine "
                                          "running k6."])
        rows = [[r.key, src, v["samples"], v["cpu_avg"], v["cpu_max"], v["mem_avg"], v["mem_max"], v["mem_limit"]]
                for r in self.runs for src, v in sorted(r.resources.items())]
        sec.blocks.append(Table("CPU and memory", [Col("Run"), Col("Source"), Col("Samples", "int"), Col("CPU avg %", "num1"),
                                                   Col("CPU max %", "num1"), Col("Mem avg MB", "mb"), Col("Mem max MB", "mb"),
                                                   Col("Mem limit MB", "mb")], rows))
        pg = [[r.key, r.pg_conn.get("total_avg"), r.pg_conn.get("total_max"), r.pg_conn.get("active_avg"),
               r.pg_conn.get("active_max"), r.pg_conn.get("idle_in_tx_max")] for r in self.runs if r.pg_conn]
        if pg:
            sec.blocks.append(Table("Database connections", [Col("Run"), Col("Total avg", "num1"), Col("Total max", "int"),
                                                             Col("Active avg", "num1"), Col("Active max", "int"),
                                                             Col("Idle in tx max", "int")], pg))
        top = [[r.key, q.get("calls"), q.get("total_ms"), q.get("mean_ms"), q.get("max_ms"), q.get("query")]
               for r in self.runs for q in (r.db.get("top_sql") or [])[:5]]
        if top:
            sec.blocks.append(Table("Top SQL by total time (5 per run)", [
                Col("Run"), Col("Calls", "int"), Col("Total ms", "ms"), Col("Mean ms", "num2"), Col("Max ms", "num2"),
                Col("Query")], top))
        tables = [[r.key, t.get("table"), t.get("live_rows"), t.get("total_bytes", 0) / 1048576,
                   t.get("index_bytes", 0) / 1048576] for r in self.runs for t in (r.db.get("tables") or [])]
        if tables:
            sec.blocks.append(Table("Table sizes after each run", [Col("Run"), Col("Table"), Col("Live rows", "int"),
                                                                   Col("Total MB", "num1"), Col("Index MB", "num1")], tables))
        return sec

    def data(self) -> Section | None:
        prefix = self.test.upper() + "-"
        seeds = [s for s in self.s.seeds if s["key"].startswith(prefix)]
        if not seeds:
            return None
        rows = [[s.get("stage"), s.get("dataset"), s.get("target"), s.get("start_count"), s.get("count_after"),
                 s.get("added"), s.get("seconds"), (s.get("added") or 0) / s["seconds"] if s.get("seconds") else None]
                for s in seeds]
        return Section("Test data", intro=["Data grown through the project's seed_to hook before the runs of each stage."],
                       blocks=[Table("Data stages", [Col("Stage", "int"), Col("Dataset"), Col("Target", "int"),
                                                     Col("Before", "int"), Col("After", "int"), Col("Added", "int"),
                                                     Col("Seconds", "num1"), Col("Rows/s", "num1")], rows)])

    def design(self) -> Section:
        sec = Section("Test design")
        used = {}
        for r in self.runs:
            for tc in r.cases:
                used.setdefault(tc, []).append(r.key)
        rows = []
        for tc, c in self.cases.items():
            if tc in used:
                rows.append([tc, c.get("label"), c.get("kind"), str(c.get("method", "")).upper(), c.get("path"),
                             ", ".join(sorted(set(used[tc])))])
        if "LOGIN" in used:
            rows.insert(0, ["LOGIN", "Sign in", "auth", "", "auth.login", ", ".join(used["LOGIN"])])
        sec.blocks.append(Table("Cases", [Col("Case"), Col("Operation"), Col("Kind"), Col("Method"), Col("Path"),
                                          Col("Runs")], rows))
        journey = self.s.plan.get("journey") or {}
        if any(r.is_journey for r in self.runs) and journey.get("steps"):
            jrows = []
            for i, st in enumerate(journey["steps"], 1):
                ids = ([st["case"]] if st.get("case") else []) + list(st.get("cases") or [])
                jrows.append([i, ", ".join(f"{c} {self.label(c)}" for c in ids), st.get("probability", 1),
                              st.get("repeat", 1), "no" if st.get("think") is False else "yes"])
            sec.blocks.append(Table("User journey (each virtual user repeats it)", [
                Col("Step", "int"), Col("Cases"), Col("Probability", "num2"), Col("Repeat", "int"), Col("Think after")],
                jrows))
        cfg = self.tcfg.get("config") or {}
        sec.fields = [(k.replace("_", " "), v if not isinstance(v, (list, dict)) else str(v)) for k, v in cfg.items()]
        runs = [[r.key, r.meta.get("script"), r.meta.get("started"), r.meta.get("ended"), r.meta.get("duration_s"),
                 r.meta.get("records_before"), r.meta.get("records_after"), r.meta.get("records_removed_after_run"),
                 r.meta.get("command")] for r in self.runs]
        sec.blocks.append(Table("Execution log", [Col("Run"), Col("Script"), Col("Started"), Col("Ended"),
                                                  Col("Seconds", "num1"), Col("Records before", "int"),
                                                  Col("Records after", "int"), Col("Removed after", "int"),
                                                  Col("Command")], runs))
        return sec

    def environment(self) -> Section:
        env = self.s.environment
        sec = Section("Environment")
        for group in ("target", "load_generator", "network", "git", "extra"):
            data = env.get(group) or {}
            if group == "target":
                facts = data.get("facts") or {}
                data = {k: v for k, v in data.items() if k != "facts"} | {f"fact: {k}": v for k, v in facts.items()}
            for k, v in data.items():
                sec.fields.append((f"{group.replace('_', ' ')} · {k}", v if not isinstance(v, (list, dict)) else str(v)))
        return sec

    def method(self) -> Section:
        rows = []
        for r in self.runs:
            c = cross_check(r)
            if c:
                rows.append([r.key, c["k6_p95"], c["computed_p95"], c["difference_ms"]])
        return Section("Method and verification", intro=[
            "Every number comes from the per-request samples k6 wrote (raw/<run>.csv.gz).",
            "Percentiles use k6's method (linear interpolation between closest ranks).",
            "Single-user runs exclude warm-up iterations; concurrent runs report the measured phases only.",
            "An error is a response k6 marked unexpected: a status outside 200–399, or outside a case's `expect` list.",
            "The cross-check below compares the p95 over every HTTP request (including helper and warm-up requests) "
            "computed here with k6's own p95 for the same run.",
        ], blocks=[Table("Cross-check with k6", [Col("Run"), Col("k6 p95 ms", "ms"), Col("Computed p95 ms", "ms"),
                                                 Col("Difference ms", "num2")], rows)])

    def build(self) -> Doc:
        title, _ = TYPE_TITLES[self.type]
        rc = self.report_cfg
        doc_id = f"{rc.get('doc_prefix', 'PT')}-{self.test.upper()}"
        sections = [self.summary(), self.results_by_case(), self.comparison(), self.phases(), self.jobs(),
                    self.distribution(), self.timeseries(), self.errors(), self.resources(), self.data(),
                    self.design(), self.environment(), self.method()]
        env = self.s.environment
        return Doc(
            doc_id=doc_id, title=title, subtitle=env.get("project", {}).get("name", ""),
            meta=[("Document", doc_id), ("Run ID", self.s.run_id), ("Issued", datetime.now().strftime("%d %B %Y")),
                  ("Prepared for", rc.get("prepared_for")), ("Organisation", rc.get("organisation")),
                  ("Classification", rc.get("classification"))],
            sections=[s for s in sections if s is not None], branding=rc,
        )


def build_all(run_dir: Path, project_dir: Path, formats=None) -> list[Path]:
    from .excel import write_xlsx
    from .html import write_html

    suite = load_suite(run_dir)
    if not suite.runs:
        raise RuntimeError(f"No completed runs in {run_dir / 'raw' / 'runs.jsonl'}")
    formats = formats or (suite.plan.get("report") or {}).get("formats") or ["xlsx", "html"]
    out = []
    for test in suite.tests():
        doc = Builder(suite, test).build()
        base = run_dir / f"{doc.doc_id}-{suite.run_id}"
        if "xlsx" in formats:
            out.append(write_xlsx(doc, base.with_suffix(".xlsx"), project_dir))
        if "html" in formats:
            out.append(write_html(doc, base.with_suffix(".html"), project_dir))
    return out
