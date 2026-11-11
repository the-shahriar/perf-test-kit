"""One test run = a results folder of k6 runs, resumable by key.

results/<run-id>/raw/
  runs.jsonl            one line per finished k6 run (the resume ledger)
  seed.jsonl            one line per data stage (volume tests)
  <key>.csv.gz          every k6 sample
  <key>.summary.json    k6's own summary (cross-check)
  <key>.resources.csv   resource samples
  <key>.db.json         database snapshot after the run
  environment.json, plan.json, test_objects.json, target.json
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from . import auth, db
from .core import K6_SCRIPTS, HarnessError, append_jsonl, find_k6, log, now_iso, read_json, read_jsonl, write_json
from .monitor import Monitor

SYSTEM_TAGS = "status,method,name,group,scenario,expected_response,error_code"


class Runner:
    def __init__(self, ctx, run_id: str):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.run_id = run_id
        self.dir = ctx.project.results_dir / run_id
        self.raw = self.dir / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        tfile = self.raw / "target.json"
        known = read_json(tfile)
        if known and known.get("target") != ctx.target:
            raise HarnessError(f"Run '{run_id}' was started against target '{known['target']}'; "
                               f"use a new --run-id for '{ctx.target}'")
        write_json(tfile, {"target": ctx.target, "base_url": self.cfg["base_url"]})
        self.done = {r["key"] for r in read_jsonl(self.raw / "runs.jsonl")}

    # ── metadata ──
    def record_plan(self, name: str, tcfg: dict, quick: bool) -> None:
        from .environment import collect

        env_path = self.raw / "environment.json"
        if not env_path.exists():
            write_json(env_path, collect(self.ctx))
        plan = read_json(self.raw / "plan.json")
        plan.setdefault("tests", {})[name] = {"config": tcfg, "quick": quick, "recorded": now_iso()}
        plan["project"] = self.ctx.project.name
        plan["cases"] = self.cfg["cases"]
        plan["journey"] = self.cfg.get("journey")
        plan["slo"] = self.cfg.get("slo")
        plan["report"] = self.cfg.get("report")
        write_json(self.raw / "plan.json", plan)
        objects = self.ctx.call("describe", default=None)
        if objects is not None:
            write_json(self.raw / "test_objects.json", objects)

    # ── data hooks ──
    def record_count(self, dataset: str) -> int | None:
        return self.ctx.call("record_count", dataset, default=None)

    def record_ids(self, dataset: str, limit: int = 5000) -> list:
        ids = self.ctx.call("record_ids", dataset, limit, default=None)
        if ids is None:
            ids = (self.cfg.get("pools") or {}).get("records") or []
        return list(ids)[:limit]

    def seed(self, key: str, target: int, dataset: str, stage: int = 1) -> None:
        if key in {s["key"] for s in read_jsonl(self.raw / "seed.jsonl")}:
            return
        if not self.ctx.hook("seed_to"):
            raise HarnessError("This test grows data, so hooks.py needs seed_to(ctx, target, dataset)")
        log(f"Growing dataset '{dataset}' to {target:,} records")
        t0 = time.monotonic()
        started = now_iso()
        result = self.ctx.call("seed_to", target, dataset) or {}
        snap = db.snapshot(self.cfg, "not taken")
        append_jsonl(self.raw / "seed.jsonl", {
            "key": key, "stage": stage, "dataset": dataset, "target": target, "started": started, "ended": now_iso(),
            "seconds": round(time.monotonic() - t0, 2), "count_after": self.record_count(dataset), **result,
            "database": snap,
        })

    # ── one k6 run ──
    def k6(self, key: str, test: str, ttype: str, script: str, run_cfg: dict, meta: dict,
           dataset: str = "default") -> dict | None:
        if key in self.done:
            log(f"skip {key} (already completed in this run)")
            return None
        k6_bin = find_k6()
        if not k6_bin.exists():
            raise HarnessError(f"k6 not found at {k6_bin}. See docs/SETUP.md (Install k6).")
        sessions = auth.refresh(self.ctx)
        a = self.cfg["auth"]
        full = {
            "base_url": self.cfg["base_url"], "api_prefix": self.cfg.get("api_prefix") or "",
            "tls_verify": self.cfg.get("tls_verify", True), "timeout": self.cfg.get("timeout"),
            "headers": self.cfg.get("headers") or {}, "auth": {"header": a["header"], "format": a["format"]},
            "cases": self.cfg["cases"], "journey": self.cfg.get("journey") or {"steps": []},
            "pools": self.cfg.get("pools") or {}, "vars": self.ctx.dataset_vars(dataset),
            "random_seed": self.cfg["random_seed"], "think_time": self.cfg.get("think_time") or [1, 3],
            **run_cfg,
        }
        if script == "login.js":
            accounts = auth.users(self.ctx)
            full.update(accounts=accounts, login=a["login"], logout=a.get("logout"), per_minute=a.get("per_minute"))
            if sessions["renewed"] and a.get("per_minute"):
                log("Waiting 65 s so the sign-in baseline starts with an empty rate-limit window")
                time.sleep(65)
        else:
            full["tokens"] = auth.tokens(self.ctx)
            full["record_ids"] = self.record_ids(dataset)
        # Tokens and passwords never go into results/: the k6 input lives in .state.
        cfg_path = self.ctx.state_dir / "k6" / f"{self.run_id}_{key}.json"
        write_json(cfg_path, full)
        public = {k: v for k, v in run_cfg.items()}
        public["record_id_pool"] = len(full.get("record_ids", []))
        public["accounts"] = len(full.get("tokens", full.get("accounts", [])))

        before = self.record_count(dataset)
        top_sql = db.reset_statements(self.cfg)
        csv_path = self.raw / f"{key}.csv.gz"
        summary_path = self.raw / f"{key}.summary.json"
        env = dict(os.environ, K6_CSV_TIME_FORMAT="unix_milli", K6_NO_USAGE_REPORT="true")
        cmd = [str(k6_bin), "run", "--quiet", "--no-color", "--out", f"csv={csv_path}", "--system-tags", SYSTEM_TAGS,
               "-e", f"RUN_CONFIG={cfg_path}", "-e", f"SUMMARY_FILE={summary_path}", str(K6_SCRIPTS / script)]
        log(f"▶ {key}  ({test}: {meta.get('label', '')})")
        started = now_iso()
        t0 = time.monotonic()
        proc = subprocess.Popen(cmd, env=env, cwd=str(K6_SCRIPTS))
        mon = Monitor(self.cfg, self.raw / f"{key}.resources.csv", proc.pid).start()
        code = proc.wait()
        mon.stop()
        ended = now_iso()
        write_json(self.raw / f"{key}.db.json", db.snapshot(self.cfg, top_sql))
        # Remove what the run created so the next run starts at the same size.
        removed = self.ctx.call("after_run", dataset, default=None)
        after = self.record_count(dataset)
        summary = read_json(summary_path)
        record = {
            "key": key, "test": test, "type": ttype, "script": script, "dataset": dataset, **meta,
            "started": started, "ended": ended, "duration_s": round(time.monotonic() - t0, 1), "k6_exit_code": code,
            "records_before": before, "records_after": after, "records_removed_after_run": removed,
            "sessions_checked": sessions["checked"], "sessions_renewed": sessions["renewed"], "config": public,
            "command": f"k6 run --out csv=raw/{key}.csv.gz --system-tags {SYSTEM_TAGS} k6/{script}",
        }
        append_jsonl(self.raw / "runs.jsonl", record)
        self.done.add(key)
        log(f"■ {key} finished, exit {code}" + (f", records {before:,} -> {after:,}" if before is not None and after is not None else ""))
        return summary
