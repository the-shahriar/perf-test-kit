# Setup guide

This guide covers two things: installing the kit on the machine that will
generate load, and onboarding a new project (an API you want to test). Follow it
top to bottom the first time. After that, [PROCESS.md](PROCESS.md) is the
checklist for each test engagement.

---

## Part 1: Install the kit

### 1.1 Where to run it

The machine that runs the kit is the **load generator**. Its CPU, network and
distance from the system under test affect the results, so choose it on purpose:

| Situation | Load generator |
|---|---|
| Trying the kit, local development | Your PC, against a local copy of the system |
| Realistic numbers for a server | A separate VM in the same region or network as the server |
| Quick check against a server | Your PC is fine, but the report records the network path. Do not compare these numbers with numbers from a same-region generator. |
| Unattended long runs (soak, full volume) | A VM or the server's own host, under `systemd`/`tmux`/`screen` so the run survives a disconnect |

Rough sizing: k6 needs about 1 vCPU and 0.5–1 GB RAM per 200–400 virtual users
for JSON APIs with think time. The report records the load generator's CPU
(`loadgen`) and k6's own CPU, so you can tell when the generator itself became
the bottleneck.

### 1.2 Python

Python 3.11 or newer.

```text
cd perf-test-kit
python -m venv .venv
# Windows:  .venv\Scripts\activate        Linux/macOS:  source .venv/bin/activate
pip install -r requirements.txt
```

`psycopg2-binary` is needed only for PostgreSQL monitoring, and `pillow` only for
a logo on the Excel cover. If either fails to install and you don't need it,
remove it from `requirements.txt`.

### 1.3 k6

The kit looks for k6 in this order: the `PERF_K6` environment variable,
`tools/k6/k6` (or `k6.exe`) inside the kit, then `k6` on `PATH`. Version 1.0 or
newer is required.

**Windows (PowerShell):**

```powershell
Invoke-WebRequest https://github.com/grafana/k6/releases/download/v1.0.0/k6-v1.0.0-windows-amd64.zip -OutFile $env:TEMP\k6.zip
Expand-Archive $env:TEMP\k6.zip $env:TEMP\k6x -Force
New-Item -ItemType Directory -Force tools\k6 | Out-Null
Copy-Item (Get-ChildItem $env:TEMP\k6x -Recurse -Filter k6.exe)[0].FullName tools\k6\
```

**Linux:**

```bash
curl -sL https://github.com/grafana/k6/releases/download/v1.0.0/k6-v1.0.0-linux-amd64.tar.gz \
  | tar xz --strip-components=1 -C tools/k6 k6-v1.0.0-linux-amd64/k6
```

**macOS:** `brew install k6`

### 1.4 Verify with the demo project

```text
python projects/demo/demo_api.py                     # leave running (terminal 1)
python perfkit.py check demo                         # terminal 2: every line PASS or SKIP
python perfkit.py setup demo
python perfkit.py run demo smoke --run-id verify
```

Open `projects/demo/results/verify/DEMO-PT-SMOKE-verify.html`. If it shows
results for cases D-01 … D-09 with no errors, the installation works. To clear
the demo's state, run `python perfkit.py cleanup demo --yes`.

---

## Part 2: Onboard a new project

You will write one file, `project.yaml`. You may also write `hooks.py` if the
tests need data created first. Keep [CONFIG.md](CONFIG.md) open as the reference.
[project.sample.yaml](../project.sample.yaml) is a commented sample for a typical REST API,
and `projects/demo/project.yaml` is a working example you can run.

### 2.1 Gather the facts first

Before writing config, collect these. Most onboarding problems come from a
missing item on this list.

| Fact | Example | Where it goes |
|---|---|---|
| Base URL and API prefix of each environment | `https://staging.example.com`, `/api/v1` | `targets.<name>.base_url`, `api_prefix` |
| A cheap URL that answers when the system is up | `/health` | `targets.<name>.health` |
| How a user signs in, and where the token is in the response | `POST /auth/login` → `data.access_token` | `auth.login` |
| How the token is sent | `Authorization: Bearer <t>` or `X-Api-Key: <t>` | `auth.header`, `auth.format` |
| Rate limits (sign-in per minute, requests per user per minute, admin writes) | 10/min sign-in | `auth.per_minute`, `pacing` |
| Session lifetime and idle timeout | 30 min idle | kit re-checks sessions before each run (`auth.check`) |
| Test accounts: exist already, or must be created? Which role? | 50 accounts `lt001…lt050`, role "agent" | `users`, or a `users` hook |
| The 5–20 operations that matter most to users | list orders, search, open order, create, update | `cases` |
| What a typical user does in a session, and how often they write | browse 80%, create 15%, export 5% | `journey` |
| Does the API wrap responses? | `{"data": …, "meta": …}` | `response.unwrap` |
| Containers or hosts to watch, and an optional read-only DB login | `api`, `worker`, `postgres` | `monitoring` |
| Targets, if the project has them | p95 < 800 ms, errors < 1% | `slo` (optional) |

### 2.2 Create the project folder

```text
python perfkit.py init my-api
```

This creates `projects/my-api/project.yaml` and `hooks.py` from the template. To
keep the test definition in the application's own repository instead, run
`python perfkit.py init my-api --dir ../my-api-repo/perf` and always refer to
the project by its path: `python perfkit.py check ../my-api-repo/perf`.

### 2.3 Targets

One entry per environment. Anything inside a target overrides the top level for
that target only, so you can set different users, monitoring or test sizes per
environment.

```yaml
default_target: local
targets:
  local:
    base_url: http://127.0.0.1:8000
    api_prefix: /api/v1
    health: /health
  staging:
    base_url: https://staging.example.com
    api_prefix: /api/v1
    health: /health
    allowed_hosts: [staging.example.com]     # required: the kit refuses unlisted hosts
    facts:                                    # copied into the report as-is
      hosting: AWS ECS Fargate, 2 tasks x 1 vCPU / 2 GB; RDS db.t3.medium
      approved_by_and_window: J. Smith, 2026-11-20 22:00–02:00 UTC
    tests:
      load: {tiers: [1, 10, 25, 50]}          # smaller tiers on staging
```

### 2.4 Authentication and test accounts

Pick the `auth.type` that matches the API.

**No auth:** `type: none`.

**Pre-issued tokens (API keys, long-lived JWTs):**

```yaml
auth:
  type: token
  tokens_env: PERF_TOKENS          # "tok1,tok2,..." — one per virtual user
  header: X-Api-Key
  format: "{token}"
```

**Sign-in per user:**

```yaml
auth:
  type: login
  per_minute: 9                    # a little under the real limit
  login:
    method: POST
    path: /auth/login
    body: {email: "{{user.email}}", password: "{{user.password}}"}
    token: data.access_token
  check: {method: GET, path: /auth/me}
  logout: {method: POST, path: /auth/logout}
  admin: {email_env: PERF_ADMIN_EMAIL, password_env: PERF_ADMIN_PASSWORD}

users:
  count: 50
  email: "loadtest{n:03d}@example.com"
  password_env: PERF_USER_PASSWORD
```

If the accounts must be created, or if sign-in involves more than one request
(MFA bypass for test accounts, forced password change, choosing a tenant), write a
`users` and/or `login` hook. See [HOOKS.md](HOOKS.md), which has an example for
accounts that must change their password at first sign-in.

Plan the account count around the per-user rate limit. In single-user runs the
kit spreads requests across all accounts. In concurrent runs, virtual user *n*
always uses account *n*, and accounts are reused when there are more users than
accounts.

### 2.5 Cases

A case is one measured operation. Give it a stable id, because ids are what the
reports compare across runs and test types.

```yaml
cases:
  ORD-01:
    label: List orders
    method: GET
    path: /orders
    query: {limit: 50}
    extract: {cursor: meta.next_cursor}         # save for later steps
  ORD-02:
    label: Next page
    method: GET
    path: /orders
    query: {cursor: "{{var.cursor}}", limit: 50}
    requires: [cursor]                          # skip when there is no next page
    standalone: {before: [{case: ORD-01}]}      # on its own, fetch page 1 first (unmeasured)
  ORD-03:
    label: Order detail
    method: GET
    path: /orders/{{var.id}}
    standalone: {set: {id: "{{pick:records}}"}}
  ORD-04:
    label: Create order
    method: POST
    path: /orders
    body: {ref: "LT-{{seq}}", qty: "{{int:1:50}}", placed: "{{today}}"}
    expect: [201]
```

Guidelines:

- **Measure what users wait for.** If a screen makes three calls, use three cases
  and put them in one journey step.
- **`standalone`** is what a case needs when it runs alone, as in `performance`
  and `volume` single-user runs. Journeys don't use it, because the earlier
  steps already provide the state.
- **Background jobs:** add `poll:` and the report shows the request time and the
  time until the job finished.
- **Writes that grow data:** give created records a recognisable marker (here
  `LT-…`) and remove them in the `after_run` hook. Otherwise each run starts
  larger than the previous one and the runs are not comparable.
- **Placeholders** such as `{{pick:...}}` and `{{int:a:b}}` are listed in
  [CONFIG.md § Templates](CONFIG.md#templates).

### 2.6 Journey

The journey is what one concurrent user does, repeated until the run ends.
`load`, `stress`, `spike`, `soak` and `volume` all use it.

```yaml
think_time: [3, 5]                 # seconds between steps, uniformly random
journey:
  steps:
    - cases: [APP-01, APP-02]      # calls made together, then think
    - case: ORD-01
    - case: ORD-02
      repeat: 2
      while: cursor                # stop paging when there is no next page
    - set: {id: "{{pick:records}}"}
      case: ORD-03
    - probability: 0.15            # 15% of iterations create an order
      case: ORD-04
    - probability: 0.10
      set: {id: "{{pick:own_records}}"}   # each user edits only its own slice
      cases: [ORD-03, ORD-05]
```

Base the probabilities on real usage if you have it (access logs, analytics). If
you don't, state the assumption in the test plan. The report prints the journey
table so readers see what was simulated.

### 2.7 Hooks (only if you need them)

| You need | Write |
|---|---|
| Test objects created once (tenant, project, catalogue) | `setup` |
| Test accounts created | `users` (and `login` if sign-in is multi-step) |
| `{{pick:records}}` (open/edit existing records) | `record_ids` |
| `volume` tests, or `records:` on any test | `seed_to` and `record_count` |
| Writes removed after each run | `after_run` |
| `perfkit cleanup` removes test objects and accounts | `cleanup` |

See [HOOKS.md](HOOKS.md). The demo project's `hooks.py` is about 60 lines.

### 2.8 Monitoring (recommended)

```yaml
monitoring:
  interval_seconds: 5
  docker:
    containers: {api: myapp_api, worker: myapp_worker, db: myapp_db}
    ssh: deploy@app.example.com        # omit when the containers run on the load generator
  database:
    host: 127.0.0.1
    port: 5432
    name: myapp
    user_env: PERF_DB_USER             # a read-only login
    password_env: PERF_DB_PASSWORD
    top_sql: read                      # off | read | reset (reset clears pg_stat_statements per run)
    tables: [orders, order_lines]
allowed_db_hosts: [127.0.0.1]          # for a non-local database host
```

SSH must use key-based login, because the sampler runs non-interactively. Without
monitoring the reports still contain the load generator's CPU and memory, and
they state that server resources were not collected.

### 2.9 Report branding

```yaml
report:
  organisation: Example Corp · Platform Engineering
  prepared_for: Head of Engineering
  classification: Internal
  doc_prefix: EX-PT                  # report file names: EX-PT-LOAD-<run-id>.xlsx
  accent: "#0B6E4F"
  logo: logo.png                     # next to project.yaml; PNG for Excel, PNG/SVG for HTML
```

### 2.10 Prove it

```text
python perfkit.py show my-api           # does the kit see what you meant?
python perfkit.py check my-api          # every line PASS or SKIP
python perfkit.py setup my-api
python perfkit.py run my-api smoke --run-id onboard-smoke
```

Then read the smoke report:

- **Results by case:** every case has requests and 0 errors. A case with no rows
  was skipped, usually because a `requires:` variable was never extracted. Check
  the `extract` path against a real response.
- **Errors:** the message column shows the API's own error text.
- **HTTP status codes:** look for 401 (sessions), 404 (ids or paths) and 429 (rate
  limits).

When the smoke run is clean, run each test you plan to use with `--quick`
before the real run:

```text
python perfkit.py run my-api load volume --run-id onboard-quick --quick
```

The project is onboarded. Continue with [PROCESS.md](PROCESS.md) for real runs.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `Refusing to contact 'host'` | Add the host to the target's `allowed_hosts` (`allowed_db_hosts` for the database) |
| `Sign-in response has no token at ...` | Fix `auth.login.token`: it is a dotted path into the JSON response |
| Many 401s mid-run | Sessions expired. Add `auth.check` so the kit re-signs dead sessions before each run, or use longer-lived test tokens |
| 429s | You hit a rate limit. Use more accounts (per-user limits), lower `auth.per_minute` (sign-in), or have the limit raised for the test window |
| A case shows 0 requests | Its `requires:` variable was empty. Check the `extract` path, or the `standalone.before` steps |
| `{{pick:records}}` sends an empty id | No `record_ids` hook, or the dataset is empty. Add `records:` to the test or seed first |
| `loadgen`/`k6` CPU near 100% | The generator is the bottleneck. Use a bigger machine or fewer users per generator |
| A run was interrupted | Rerun the same command with the same `--run-id`. Finished runs are skipped |
| Container stats missing | Container names don't match `docker ps`, or SSH asks for a password (key-based login only) |
