# project.yaml reference

Configuration is merged in three layers. Later layers win, and mappings merge key
by key:

1. `perfkit/defaults.yaml`, the kit defaults;
2. the project's `project.yaml`;
3. the selected target's entry under `targets:`. Any top-level key can be
   overridden per target.

Run `python perfkit.py show <project> [--target T]` to see what the kit resolved.

---

## project

```yaml
project:
  name: Orders API                  # shown in reports
  owner: Platform QA                # free text, kept in environment.json
  repo: ../../path/to/app-repo      # optional; relative to the project folder. Reports show its branch@commit
```

## targets

```yaml
default_target: local
targets:
  <name>:
    base_url: https://staging.example.com      # required
    api_prefix: /api/v1                        # prepended to every relative case path
    health: /health                            # GET by check, latency sampling, and the overload wait
    tls_verify: true
    allowed_hosts: [staging.example.com]       # required for any host that is not 127.0.0.1/localhost/::1
    allowed_db_hosts: [db.internal]            # required for a non-local monitoring database
    facts: {hosting: ..., deployed_commit: ..., approved_by_and_window: ...}   # free-form, printed in reports
    # ...and any top-level key to override for this target (auth, users, monitoring, tests ...)
```

Safety rule: the kit sends requests only to the local machine and to hosts in
`allowed_hosts`. `setup`, `run` and `cleanup` against a non-local target also
need `--yes`.

## Requests (top level)

| Key | Default | Meaning |
|---|---|---|
| `timeout` | `120s` | Per-request timeout in k6 (k6 duration syntax). A case can set its own `timeout` |
| `headers` | `{}` | Sent with every request (set-up and measured) |
| `response.unwrap` | `null` | Key of the payload wrapper, e.g. `data`. Affects set-up calls only: `ctx.admin().get()` returns `response["data"]` |
| `random_seed` | `20260101` | Seeds every random choice in k6 (per virtual user) and in hooks (`ctx.seed`), so reruns make the same choices |
| `think_time` | `[1, 3]` | Seconds between journey steps, uniform random |

## auth

| Key | Meaning |
|---|---|
| `type` | `none` · `login` (each account signs in) · `token` (pre-issued tokens) |
| `header` | Header that carries the token. Default `Authorization` |
| `format` | Header value. `{token}` is replaced. Default `Bearer {token}` |
| `per_minute` | Pace for sign-ins during setup, session renewal and the sign-in baseline |
| `login` | Sign-in request (`method`, `path`, `query`, `body`, `headers`, `expect`) plus `token: <dotted path into the JSON response>`. Placeholders: `{{user.<field>}}`, `{{ctx.<var>}}`, `{{env.<NAME>}}` |
| `check` | Optional request that succeeds (2xx) only with a live session. Run for every session before each k6 run; failures are signed in again |
| `logout` | Optional. Used by the sign-in baseline so measuring sign-in leaves the working sessions untouched |
| `admin` | Admin account for hooks: `email_env` + `password_env` (or `username_env`), or literal `email` + `password` (demo only), or `token_env` for `type: token` |
| `tokens_env` / `tokens_file` | `type: token`: tokens separated by commas or newlines. One per virtual user, reused when there are more users than tokens |

## users

Where the test accounts come from (first match wins):

1. a `users(ctx)` hook;
2. `file:` a CSV (header row) or JSON list next to `project.yaml`, with
   `email`/`username` and `password` columns plus any others;
3. numbered accounts:

```yaml
users:
  count: 50
  email: "lt{n:03d}@example.com"      # and/or username: "lt{n:03d}"
  password_env: PERF_USER_PASSWORD    # or password: literal (demo only)
  fields: {tenant: acme}              # extra per-user fields; {n} is formatted
```

Every field is available as `{{user.<field>}}` in `auth.login`.

## pacing

```yaml
pacing:
  admin_writes_per_minute: 100        # ctx.pacer() default, for hooks that create objects
```

## vars, datasets, pools

```yaml
vars:                                 # {{ctx.<name>}} in every case
  page_size: 100
  tenant_id: $state.tenant_id         # "$state.<key>": from .state/<target>/state.json (written by hooks)
  region: $env.PERF_REGION            # "$env.<NAME>": environment variable

datasets:                             # tests choose one with `dataset:`; its vars override `vars`
  default:
    vars: {collection_id: $state.orders_id, new_item: {name: "lt-{{seq}}"}}
  archive:
    vars: {collection_id: $state.archive_id, new_item: {name: "lt-{{seq}}", archived: true}}

pools:                                # value lists for {{pick:<pool>}}
  surnames: [Haddad, Smith, Garcia]
```

A var can hold a whole template (an object or a string with placeholders). A
case that uses `"{{ctx.new_item}}"` renders that template, which lets each
dataset carry its own record shape.

Built-in pools: `records` (from the `record_ids` hook) and `own_records` (in
journeys, the slice of `records` belonging to this virtual user, so concurrent
users never edit the same record).

## cases

```yaml
cases:
  <ID>:
    label: Human-readable name          # default: the id
    kind: read                          # read | write | auth | other. Default: read for GET, write otherwise
    method: GET
    path: /orders/{{var.id}}            # relative to base_url + api_prefix, or an absolute URL
    query: {limit: 50, q: "{{pick:terms}}"}   # null/empty values are dropped
    headers: {X-Tenant: "{{ctx.tenant}}"}
    body: {...}                         # JSON; placeholders keep their type when they are the whole value
    expect: [200, 201]                  # statuses that are not errors. Default: 200–399
    timeout: 60s
    set: {id: "{{pick:records}}"}       # assign variables before the request, every time it runs
    requires: [cursor]                  # skip (no request, no sample) when any is empty
    extract:                            # variables from the response, for later steps
      cursor: meta.next_cursor          #   dotted path into the JSON body
      version: data.version
      etag: header:ETag                 #   or a response header
      code: status                      #   or the status code
    poll:                               # a queued job: follow it until done
      when_status: [202]                #   only poll on these statuses (default: any 2xx)
      method: GET
      path: /jobs/{{var.job_id}}
      until: data.status                #   dotted path of the state field
      done: [completed]
      failed: [failed, error]
      interval_seconds: 0.5
      max_polls: 240
    standalone:                         # how to run it on its own (single-user runs)
      set: {id: "{{pick:records}}"}
      before: [{case: OTHER-ID}]        #   unmeasured (AUX) unless `measure: true`
      after: [{case: X, measure: true}] #   measured under X's own id
    single_user: true                   # false: leave out of `cases: all/read` lists
```

Steps in `standalone.before/after` are either `{case: ID, measure: bool}`, an
inline request (`{method, path, query, body, extract}`, always AUX), or
`{set: {...}}`.

### Templates

Placeholders are `{{...}}`. When a placeholder is the entire string, the value
keeps its type (number, boolean, object). Otherwise it is inserted as text.

| Placeholder | Value |
|---|---|
| `{{var.name}}` | Variable from `set`/`extract`. Reset at the start of each journey iteration and each standalone execution |
| `{{ctx.name}}` | Run variable (`vars` + the dataset's `vars`) |
| `{{user.field}}` | Account field (sign-in requests only) |
| `{{pick:pool}}` | Random element of a pool (`records`, `own_records`, or `pools:` entries) |
| `{{int:a:b}}` | Random integer a…b |
| `{{intpad:a:b:w}}` | Random integer a…b, zero-padded to w digits (text) |
| `{{float:a:b:dp}}` | Random number a…b, rounded to dp decimals (default 2) |
| `{{bool:p}}` | true with probability p |
| `{{digits:n}}` / `{{alnum:n}}` | n random digits / letters and digits |
| `{{uuid}}` | Random UUID v4 |
| `{{seq}}` | Unique within the run: `v<vu>-<iteration>-<n>` |
| `{{vu}}` / `{{iter}}` | Virtual-user number / iteration number |
| `{{today}}` / `{{date:+N}}` / `{{now}}` / `{{epoch}}` | Date (YYYY-MM-DD), date N days from today, ISO timestamp, milliseconds |

All randomness comes from a seeded generator (`random_seed` and the VU number),
so a rerun makes the same choices.

## journey

```yaml
journey:
  steps:
    - cases: [A, B]          # run in order, then think
    - case: C                # `case` and `cases` can be combined; case runs first
      repeat: 3              # up to 3 times
      while: cursor          # ...while this variable is set
      think: false           # no think time after this step
    - probability: 0.2       # run this step in 20% of iterations
      set: {id: "{{pick:own_records}}"}
      cases: [D, E]
```

## monitoring

```yaml
monitoring:
  interval_seconds: 5
  docker:
    containers: {api: app_api, db: app_db}   # label: container name
    ssh: user@host                           # optional; key-based login only
  database:                                  # PostgreSQL, optional
    host: 127.0.0.1                          # or dsn_env: PERF_DB_DSN
    port: 5432
    name: appdb
    user_env: PERF_DB_USER                   # or user: literal
    password_env: PERF_DB_PASSWORD
    sslmode: prefer
    top_sql: off                             # off | read (cumulative) | reset (cleared before each run)
    tables: [orders]                         # size and row counts after each run
    allow_writes: false                      # true lets hooks open ctx.db(read_only=False)
```

Monitoring connections are read-only (`default_transaction_read_only=on`).
`top_sql: reset` calls `pg_stat_statements_reset()`, which clears statistics
other people may rely on, so use it only on a dedicated database.

## slo (optional)

```yaml
slo:
  p95_ms: 800
  error_rate: 0.01
  cases:
    RPT-01: {p95_ms: 5000}        # per-case override
```

With `slo:` defined, Results by case gains "SLO p95" and "SLO" (met / NOT met)
columns, and the summary counts results that met their SLO. Without it, reports
carry no verdicts.

## report

| Key | Default | Meaning |
|---|---|---|
| `organisation` | — | Cover line |
| `prepared_for` | — | Cover line |
| `classification` | `Internal` | Cover and footer |
| `doc_prefix` | `PT` | Report ids and file names: `<prefix>-<TEST>-<run-id>` |
| `accent` | `#2A78D6` | Brand colour (cover band, headings, first chart series) |
| `logo` | — | File next to `project.yaml`. PNG for Excel; PNG/SVG/JPG for HTML |
| `formats` | `[xlsx, html]` | What `run`/`report` write by default |

## tests

See [TEST_TYPES.md](TEST_TYPES.md) for every type's keys and defaults.
