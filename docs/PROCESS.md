# Test process

The procedure for one test engagement, from the request to the archived report.
It applies to every project and test type. Steps marked **GATE** need a named
person's decision before you continue. Record who decided and when in the test
plan (§1.3).

```text
1 Plan ─► 2 Approve (GATE) ─► 3 Configure ─► 4 Pre-flight (GATE) ─► 5 Setup ─► 6 Smoke
   ─► 7 Execute ─► 8 Monitor ─► 9 Report ─► 10 Review (GATE) ─► 11 Clean up ─► 12 Archive
```

---

## 1. Plan

### 1.1 Define the question

Write down the question the test must answer. The question decides the test type:

| Question | Test type |
|---|---|
| "How fast is each screen/endpoint?" | `performance` |
| "Can we support N concurrent users?" | `load` with tiers up to and past N |
| "What is the maximum, and how does it fail?" | `stress` |
| "What happens when a campaign or shift change floods us?" | `spike` |
| "Does it degrade over a working day?" | `soak` |
| "Will it still be fast with a year's data?" | `volume` with stages up to the projected size |
| "Did this release make it slower?" | Same test, same data, same target, before and after (see §7.4) |

Most engagements combine types. The usual order is
`smoke → performance → load → volume`, with `stress`, `spike` and `soak` added
when the question needs them.

### 1.2 Define the scope

- **Cases:** the operations users wait for. Each case needs a stable id.
- **Journey:** what a typical user does, and the read/write mix. Use real usage
  data where you have it, and state assumptions where you don't.
- **Data:** the record counts to test at. For volume tests, the stages should
  include today's production size and the projected size.
- **Users:** the concurrent-user tiers. Include the expected peak and at least
  one tier above it.
- **Targets (optional):** SLOs per case (`slo:` in `project.yaml`). Without
  them, reports show measured values only, and the reader judges them.

### 1.3 Write the test plan

One page per engagement. Keep it next to the results (`results/<run-id>/PLAN.md`)
or in your ticket system. Use this template:

```text
Test plan — <project> — <date>
Question:            ...
Test types and order: smoke, performance, load (1/5/10/25/50/100), volume (1k → 200k)
Target environment:  <name>, <URL>, deployed version <commit/tag>
Load generator:      <machine>, <location relative to target>
Data:                <dataset>, sizes per stage; created by <hook / restore>
Accounts:            <N> accounts, role <...>, created by <...>
Window:              <date> <start>–<end> UTC (estimated duration: <see §1.4>)
Side effects:        <emails/webhooks/payments disabled? how?>
Abort criteria:      <see §8.3>
Approved by:         <name, role, date>          (GATE §2)
Pre-flight passed:   <name, date>                (GATE §4)
Report reviewed by:  <name, date>                (GATE §10)
```

### 1.4 Estimate duration

| Test | Rough duration |
|---|---|
| smoke | 2–5 min |
| performance | cases × iterations × average response time, plus sign-in samples ÷ `per_minute` |
| load | per tier: ramp-up + steady + ramp-down + cool-down. With the defaults, 5 tiers ≈ 40 min |
| stress | the same per tier, up to the tier where it stops |
| spike | the sum of its phases (default ≈ 8 min) |
| soak | ramp-up + steady (default 2 h) |
| volume | per stage: seeding time + single-user run + journey per user count. Seeding often dominates |
| setup | account creation, plus sign-ins ÷ `auth.per_minute` (100 accounts at 9/min ≈ 11 min) |

Add the `--quick` smoke time and 20% margin.

---

## 2. Approve (GATE)

Shared, staging and production-like environments need approval before any run.
The approver confirms:

- [ ] **Window:** date, time and duration. Users of the environment are told.
- [ ] **Isolation:** what else runs on the same hosts and database, and whether
      degrading them during the window is acceptable.
- [ ] **Side effects:** emails, SMS, webhooks, payments and third-party calls
      triggered by the journey are disabled, sandboxed or acceptable.
- [ ] **Data:** what the test creates (accounts, records), how much, and that
      `cleanup` removes it or that it may remain.
- [ ] **Rate limits / WAF:** the load generator's IP is allowed, and limits are
      raised for the window if the test needs it.
- [ ] **Monitoring access:** SSH key or read-only database login, if used.
- [ ] **Abort criteria and contact:** who can stop the test, and how to reach them.

Production: test there only with explicit written approval, read-mostly
journeys, a defined user ceiling and someone watching. Prefer a
production-sized staging environment.

---

## 3. Configure

1. Onboard the project if it is new ([SETUP.md](SETUP.md), Part 2).
2. Add or update the target in `project.yaml`: `base_url`, `allowed_hosts`, and
   `facts` (hosting, instance sizes, deployed version, approval). Facts are
   printed in every report.
3. Set test sizes for this engagement under `tests:` (or `targets.<t>.tests:`).
4. Put secrets in environment variables, never in `project.yaml`:
   ```text
   PERF_ADMIN_EMAIL / PERF_ADMIN_PASSWORD   admin account for set-up hooks
   PERF_USER_PASSWORD                       existing test accounts' password
   PERF_TOKENS                              pre-issued tokens (auth.type: token)
   PERF_DB_USER / PERF_DB_PASSWORD          read-only database login (monitoring)
   ```
5. Run `python perfkit.py show <project> --target <t>` and confirm that the cases,
   journey and tests are the ones in the plan.

---

## 4. Pre-flight (GATE)

At the start of the window:

```text
python perfkit.py check <project> --target <t>
```

Every line must be PASS or SKIP. SKIP lines are features you chose not to use;
check that each SKIP matches the plan. Then confirm by hand:

- [ ] The deployed version matches the plan (`facts.deployed_commit`).
- [ ] The environment is idle, or at its agreed baseline. Look at its dashboards
      for 5 minutes.
- [ ] Data is in the agreed starting state: a fresh restore, or the counts from
      the last run.
- [ ] The load generator is otherwise idle and will stay awake: no sleep,
      updates or VPN timeouts.

Record "Pre-flight passed" in the plan.

---

## 5. Setup

```text
python perfkit.py setup <project> --target <t> [--yes]
```

Setup runs the `setup` hook (test objects), creates or lists the test accounts,
and signs them in at the paced rate. It can be repeated safely: existing objects
and sessions are reused. Use `--recreate` only for a fresh environment; it makes
the kit forget earlier object ids and sessions.

State is kept in `projects/<p>/.state/<target>/`. It holds session tokens and
generated passwords, so keep it private. It is excluded from git.

---

## 6. Smoke

Never start a long run without a clean smoke run on the same target, the same
day:

```text
python perfkit.py run <project> smoke --run-id <date>-smoke --target <t> [--yes]
```

Pass criteria:

- [ ] every case in the plan has requests and 0 errors;
- [ ] the journey ran (the `SMOKE-JOURNEY` run exists) without errors;
- [ ] record counts before/after each run are as expected, so `after_run` removed
      what the run created (Test design → Execution log);
- [ ] resources show the expected sources (containers, `pg`).

For a long or expensive test, also do a `--quick` run of that test type. It
uses the same code path and finishes in minutes:

```text
python perfkit.py run <project> volume --run-id <date>-quick --quick --target <t>
```

---

## 7. Execute

### 7.1 Run

```text
python perfkit.py run <project> performance load volume --run-id 2026-11-20-full --target <t> --yes
```

- **Run id:** one per engagement, for example `<date>-<purpose>`. Every test in
  the command writes to `results/<run-id>/`. A run id is bound to one target.
- **Order:** tests run in the order given. Put data-growing tests (`volume`)
  last, or after the tests that need a smaller dataset, because a volume test
  leaves the data at its largest stage.
- **Resume:** if anything interrupts the run (network, reboot, Ctrl+C), run the
  same command again. Finished k6 runs and seeded stages are skipped. The k6 run
  that was in progress starts over.

### 7.2 Unattended runs

Linux (keeps running after you disconnect):

```bash
nohup python perfkit.py run myproj soak --run-id 2026-11-20-soak --target staging --yes \
  > soak.log 2>&1 &
tail -f soak.log
```

or `tmux new -s perf`, or a `systemd` unit for multi-hour runs. On Windows, use
a dedicated session with sleep disabled, or Task Scheduler.

### 7.3 Data growth between tests

- `records:` on a test grows the dataset to that size before the test, and only
  grows it, never shrinks.
- Every run calls `after_run` to remove what it created, so successive runs
  start at the same size.
- After a volume test the dataset is at its largest stage. To run `load` at a
  smaller size again, restore the data or use another dataset.

### 7.4 Comparing two versions

Comparisons are only valid with the same:

- test definition (`project.yaml`, at the same commit of this kit's project folder),
- data (the same seed and size; restore between versions),
- target hardware and configuration,
- load-generator location.

Run the same command with a new run id for each version. Compare the
"Runs at a glance" and "Results by case" tables. Also check the "Environment"
section of both reports to confirm the conditions matched.

---

## 8. Monitor during the run

### 8.1 What to watch

- **The console:** each run prints `▶ key` at start and `■ key finished, exit N`
  at the end, with the request count and p95. A non-zero exit or a sudden jump
  in p95 needs a look.
- **The system's own dashboards:** CPU, memory, database connections, queue
  depth, error logs.
- **The load generator:** if its CPU stays above 80%, its measurements are no
  longer reliable.

### 8.2 Expected behaviour

- Response times rise near capacity. That is usually what the test is for, not
  a reason to stop.
- After an overload tier the API may answer 502/503 for a while. Before the next
  run's sign-ins the kit waits until the health URL stops returning gateway
  errors.
- Sessions can expire during long runs. Before each run the kit validates
  sessions (`auth.check`) and signs in again those that died.

### 8.3 Abort

Stop with Ctrl+C (or `kill <pid>`) when an abort criterion from the plan is met,
for example:

- real users of a shared environment are affected beyond what was agreed;
- side effects appear that should have been disabled (emails, payments);
- errors are caused by the test setup rather than the system (for example all
  401s or all 404s). Fix the setup and re-smoke;
- the load generator is saturated.

After an abort, write down the time and the reason. Then either resume later
with the same run id, or start a new run id once the cause is fixed. Partial
results can still be reported (`perfkit report`); the report covers only the
runs that finished.

---

## 9. Report

Reports are built automatically at the end of `run`. To rebuild them, for
example after changing `report:` branding or SLOs:

```text
python perfkit.py report <project> --run-id <id> [--format xlsx|html|both]
```

One report per test in the run: `<doc_prefix>-<TEST>-<run-id>.xlsx` and `.html`
in `results/<run-id>/`. The HTML prints to PDF from any browser, with each
section on a new page.

Sections of each report:

| Section | Contents |
|---|---|
| Summary | Project, target, window, code version, every run at a glance (users, records, requests, error rate, p50/p95/max, throughput); SLO summary when defined |
| Results by case | Per run, per case: count, errors, min/avg/p50/p90/p95/p99/max, response size, throughput, SLO |
| Comparison | load/stress: tiers side by side plus charts. volume: stages side by side plus charts. p95 matrix case × tier/stage |
| Phases | Every phase of each concurrent run. Spike: p95 per case in baseline/spike/recovery. Soak: equal time windows |
| Background jobs | Time to completion for cases with `poll:` |
| Distribution | Single-user runs: requests per response-time band |
| Over time | Concurrent runs: p95, users, throughput and errors in time buckets |
| Errors | By case, status and message; every HTTP status code |
| Resources | CPU/memory per container and the load generator; DB connections; top SQL; table sizes |
| Test data | Volume: stage sizes, seeding time and rate |
| Test design | Cases (method, path), the journey, the test parameters, the execution log with commands |
| Environment | Target facts, load generator, network latency, code version, hook facts |
| Method and verification | How numbers are computed; computed p95 vs k6's p95 for every run |

### 9.1 Writing the conclusion

The reports contain measurements. The conclusion is yours: write it in the
review document or the ticket, not in the report. Answer the question from §1.1
and cite the table each claim comes from. For example:

> At 50 concurrent users, p95 across all cases was 640 ms with 0.0% errors
> (Load report → Comparison → Tiers). At 100 users the error rate reached 4.2%,
> all HTTP 503 on ORD-04 (Errors), while API CPU stayed at 98% (Resources).

---

## 10. Review (GATE)

A second person checks before the results are shared:

- [ ] The question in the plan is answered by the measured tables.
- [ ] The Environment section matches the plan: target, version, generator
      location.
- [ ] The cross-check difference (Method and verification) is near 0 ms for
      every run.
- [ ] Error messages are understood: system behaviour, or a test-setup problem?
- [ ] The load generator was not saturated (Resources → `loadgen`, `k6`).
- [ ] For comparisons, the conditions in §7.4 were identical.

---

## 11. Clean up

```text
python perfkit.py cleanup <project> --target <t> --yes
```

This runs the project's `cleanup` hook, which removes the test objects and
deactivates or deletes the test accounts, as the project defines. It then
renames the local session state, so the next `setup` starts fresh. Without a
cleanup hook only local state is reset; remove server-side data as agreed in
the plan.

Also:

- [ ] Re-enable side effects that were disabled for the window.
- [ ] Revert raised rate limits and WAF allow-rules.
- [ ] Tell the environment's users that the window is over.

---

## 12. Archive

`results/<run-id>/` holds everything needed to rebuild or audit the reports:

```text
raw/runs.jsonl          each k6 run: config, timing, record counts, exit code, command
raw/<key>.csv.gz        every request sample
raw/<key>.summary.json  k6's own summary
raw/<key>.resources.csv resource samples
raw/<key>.db.json       database snapshot
raw/environment.json    load generator, target, code version
raw/plan.json           the test definitions, cases and journey as run
<prefix>-<TEST>-<id>.xlsx / .html
```

Archive the whole folder (zip it to the agreed share), together with the test
plan and the review. `results/` is excluded from git on purpose: raw samples are
large, and reports should be shared deliberately. Session state in `.state/` is
never archived or shared.
