# Test types

Each test type turns a test definition into one or more k6 runs. A definition
is the type's defaults (`perfkit/defaults.yaml`), with the project's `tests:`
entry merged over them, then the target's `tests:` entry over that.

```yaml
tests:
  load: {tiers: [1, 10, 50, 100], steady_seconds: 600}     # override a built-in test
  nightly-load:                                            # a named variant
    type: load
    tiers: [25]
    steady_seconds: 900
  archive-volume:                                          # volume on another dataset
    type: volume
    dataset: archive
    stages: [100000, 500000]
```

Run keys (the names in reports and in `raw/`) are `<TEST NAME>-<suffix>`, for
example `LOAD-050U` or `VOLUME-S3-1U`.

Options shared by all types:

| Key | Meaning |
|---|---|
| `type` | One of the seven below. Optional when the test name is the type |
| `dataset` | Which `datasets:` entry supplies `{{ctx.*}}` variables and the record pool. Default `default` |
| `records` | Grow the dataset to this size before the test (needs the `seed_to` hook). It only grows, never shrinks |
| `think_time` | Override the project's `think_time` for this test's journeys |

`--quick` on `run` shrinks any test to minutes: phases capped at 10–30 s, 3
iterations, the first two tiers or stages, at most 5 users. Use it to prove the
pipeline, never for results.

---

## smoke

**Answers:** does every case work, and does the journey run?

**Runs:** `SMOKE-LOGIN` (when `auth.type: login`), then `SMOKE-1U` (every case ×
`iterations`, no warm-up), then `SMOKE-JOURNEY` (`users` users, 5 s ramp,
`steady_seconds` steady).

| Key | Default |
|---|---|
| `iterations` | 2 |
| `users` | 2 |
| `steady_seconds` | 30 |
| `login_samples` | 2 |

**Read:** every case has requests and 0 errors. A case with no rows was skipped
because its `requires:` variable was empty.

---

## performance

**Answers:** how long does each operation take for a single user on an otherwise
idle system? This is the baseline for every other test.

**Runs:** `PERFORMANCE-LOGIN` (sign-in samples, paced at `auth.per_minute`), then
`PERFORMANCE-1U`: one virtual user runs each case `warmup` + `iterations` times,
back to back, with no think time. Each case runs in its standalone form
(`standalone.before/after`). Each execution uses the next account's session, so
no account hits a per-user rate limit.

| Key | Default | Meaning |
|---|---|---|
| `cases` | `all` | `all` (every case with `single_user` not false), `read` (cases of kind read), or a list of ids |
| `extra_cases` | — | Ids added to the list, e.g. a case marked `single_user: false` |
| `iterations` | 50 | Measured executions per case |
| `warmup` | 5 | Unmeasured executions first (caches, connection pools, JIT) |
| `login_samples` | 10 | Sign-in samples; 0 to skip |
| `records` | — | Dataset size to test at |

**Read:** Results by case gives p50/p95/max per operation. Distribution shows
whether a case has a long tail. Background jobs shows time to completion for
queued work.

---

## load

**Answers:** how do response time, throughput and errors change as concurrent
users increase to and past the expected peak?

**Runs:** one `LOAD-<users>U` per tier. Each virtual user repeats the journey with
think time; the user count ramps up, holds steady, then ramps down. Results use
the steady phase only. A cool-down separates tiers.

| Key | Default |
|---|---|
| `tiers` | [1, 5, 10, 20, 50] |
| `ramp_up_seconds` / `steady_seconds` / `ramp_down_seconds` | 60 / 300 / 30 |
| `cooldown_seconds` | 60 |
| `records` | — |

**Read:** Comparison → Tiers. Throughput should rise roughly in proportion to
users until the system saturates. After that, p95 climbs while throughput
flattens, and errors may follow. The tier where this happens is the practical
capacity. Check Resources to see which component saturated.

Pick `steady_seconds` long enough for caches, pools and autoscaling to settle:
at least 5 minutes, and 10 for decisions.

---

## stress

**Answers:** where does the system stop coping, and how does it fail?

**Runs:** like `load`, but tiers keep climbing until a tier breaks `stop_when`.
That tier is the last one run. The outcome (the users at the stopping tier and
the reason) appears in the report summary.

| Key | Default |
|---|---|
| `tiers` | [10, 25, 50, 100, 150, 200, 300] |
| `stop_when.error_rate` | 0.10 (over 10% of requests failed) |
| `stop_when.p95_ms` | 10000 |
| ramp/steady/down/cooldown | 60 / 180 / 30 / 60 |

The stop decision uses k6's own summary for the tier, which includes helper
requests. The report tables use measured cases only, so the figures can differ
slightly.

**Read:** the last tier's Errors section shows how it failed: timeouts (status 0),
503s from a proxy, 500s from the app, or 429s from a rate limiter. Over time
shows whether it failed gradually or suddenly. Re-run the tier below the
breaking point as a `load` test to confirm a stable capacity.

---

## spike

**Answers:** what happens when users jump suddenly from normal to peak, and does
the system recover afterwards?

**Runs:** one run, `SPIKE-<base>-<peak>U`, in phases: ramp-up → `baseline` (base
users) → `spike` (jump to peak users within `spike_ramp_seconds`) → `recovery`
(back to base users) → ramp-down.

| Key | Default |
|---|---|
| `base_users` / `peak_users` | 5 / 100 |
| `ramp_up_seconds` | 30 |
| `baseline_seconds` / `spike_seconds` / `recovery_seconds` | 120 / 120 / 180 |
| `spike_ramp_seconds` | 10 |

**Read:** Phases → p95 by case and phase. A healthy system has a recovery p95
close to the baseline p95. A recovery p95 that stays high means a queue, pool
or cache did not drain. Over time shows how long recovery took.

---

## soak

**Answers:** does performance stay stable over hours at a constant, realistic
load? It finds memory leaks, connection-pool exhaustion, growing tables, log or
disk growth, and token expiry.

**Runs:** one run, `SOAK-<users>U`: ramp-up, a long steady phase, ramp-down.

| Key | Default |
|---|---|
| `users` | 20 |
| `steady_seconds` | 7200 |
| `windows` | 6 (the steady phase is split into this many equal windows) |

**Read:** Phases → Steady phase in equal time windows. Compare p95 and errors in
the first and last windows. Resources: memory that only goes up is a leak
candidate. Over time: periodic spikes point to scheduled jobs, GC or backups.

Sessions must outlive the run, or the API must tolerate long sessions. The kit
validates sessions only *between* runs, so for a soak longer than the session
lifetime, use long-lived test tokens (`auth.type: token`).

---

## volume

**Answers:** how do response times change as the amount of data grows?

**Runs:** for each stage, in order:

1. `<TEST>-SEED-S<n>`: grow the dataset to the stage's size through the
   `seed_to` hook. Seeding time and rate are recorded.
2. `<TEST>-S<n>-1U`: one user, each case in `cases` (`read` by default),
   `iterations` times.
3. `<TEST>-S<n>-<users>U`: the journey, for each user count in `users`.

| Key | Default |
|---|---|
| `stages` | [1000, 10000, 50000, 100000] |
| `cases` | `read` |
| `extra_cases` | — |
| `iterations` / `warmup` | 30 / 2 |
| `users` | [50] (a list: one journey run per value) |
| ramp/steady/down/cooldown | 30 / 300 / 30 / 60 |
| `dataset` | `default` |

Requires the `seed_to` and `record_count` hooks. Stages only grow. The data is
left at the last stage, so run other tests first or restore afterwards.

**Read:** Comparison → p95 by record count. A case whose p95 grows in proportion
to the record count is doing a scan: a missing index, an unpaginated
aggregate, or a count over the whole table. Resources → table sizes and Top SQL
show which queries grew. Test data → seeding rate is a write-throughput figure
in its own right.
