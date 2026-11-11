# hooks.py

`project.yaml` describes requests. `hooks.py` holds the domain work that YAML
can't express: creating a tenant, seeding 100,000 realistic records, removing
what a run created. Every hook is optional. Define only the ones your tests
need; `python perfkit.py show <project>` lists the hooks the kit found.

The file is loaded from the project folder, and the folder is on `sys.path`, so
hooks can import sibling modules (`from datagen import order_values`).

## The `ctx` object

| Member | What it is |
|---|---|
| `ctx.cfg` | The merged configuration for the selected target (dict) |
| `ctx.target` | Target name |
| `ctx.state` | Contents of `.state/<target>/state.json` (re-read on each access) |
| `ctx.update_state(**kv)` | Merge keys into `state.json`. Store the ids of objects you create here, and reference them in YAML as `$state.<key>` |
| `ctx.state_dir` | `Path` of `.state/<target>/` |
| `ctx.admin()` | API client signed in as `auth.admin` (cached) |
| `ctx.api(token=None)` | API client with an optional token |
| `ctx.secret(name)` | A generated password, created once per target and kept in `secrets.json` |
| `ctx.pacer(per_minute=None)` | `.wait()` before each call keeps a loop under the rate. Default rate: `pacing.admin_writes_per_minute` |
| `ctx.seed` | `random_seed`. Use it for deterministic data (`random.Random(ctx.seed + n)`) |
| `ctx.has_db()` / `ctx.db(read_only=True)` | The monitoring database (psycopg2 connection). Close it when done |
| `ctx.dataset_vars(dataset)` | The resolved `{{ctx.*}}` variables of a dataset |
| `ctx.is_local()` | Whether the target is this machine |
| `ctx.log(msg)` | Timestamped progress line |

API client methods: `get/post/put/patch/delete(path, body?, params=?, timeout=?)`
return the parsed JSON, unwrapped by `response.unwrap`, and raise
`HarnessError` on a non-2xx status. `call(method, path, ..., raw=True)` returns
the `requests.Response` so you can handle the status yourself.

Raise `perfkit.core.HarnessError("message")` for an expected failure. The kit
prints the message and stops without a traceback.

## Hooks

| Hook | Called by | Return |
|---|---|---|
| `setup(ctx, recreate=False)` | `setup`, first | — (store ids with `ctx.update_state`) |
| `users(ctx)` | `setup`, after `setup` | `[{"email"/"username": ..., "password": ..., ...}]`. Saved to `users.json` |
| `login(ctx, user)` | each sign-in, replacing `auth.login` | token (str) |
| `after_login(ctx, user, token, response_json)` | after each `auth.login` sign-in | a replacement token, or None |
| `admin_credentials(ctx)` | admin sign-in, replacing `auth.admin` | `{"email": ..., "password": ...}` |
| `vars(ctx, dataset)` | before each run | dict merged into `{{ctx.*}}` |
| `record_count(ctx, dataset)` | before/after each run and each seed | int |
| `seed_to(ctx, target, dataset)` | `records:` and every volume stage | dict of facts for the report (`start_count`, `added`, ...) |
| `record_ids(ctx, dataset, limit)` | before each run | list of ids → `{{pick:records}}`, `own_records` |
| `after_run(ctx, dataset)` | after each k6 run | number of records removed |
| `cleanup(ctx)` | `cleanup --yes` | dict summarising what was removed (saved to `.state`) |
| `describe(ctx)` | start of each test | dict → `raw/test_objects.json` |
| `environment(ctx)` | start of a run id | dict → Environment section |
| `check(ctx)` | `check` | `[(ok: bool or None, name, detail), ...]` |

### Rules that keep results valid

- **Seed through the API**, as a user or an import would. Rows inserted directly
  with SQL skip validation, search indexing, audit and caches, so the system
  you measure is not the system users run. The seeding rate is reported too.
- **Make data deterministic.** Record *n* should always get the same values
  (`random.Random(ctx.seed * 1_000_003 + n)`), so stages and reruns are
  comparable.
- **Make `after_run` remove exactly what the journey creates.** Give created
  records a marker in the case body (`"ref": "LT-{{seq}}"`) and delete by that
  marker.
- **Make every hook safe to run twice.** Check `ctx.state` before creating
  anything; `setup` may run more than once.
- **Never delete what you did not create.** Cleanup should use the ids in
  `ctx.state` and `users.json`.

## Minimal example

```python
import random

def order(n, seed):
    r = random.Random(seed * 1_000_003 + n)
    return {"ref": f"SEED-{n:07d}", "amount": round(r.uniform(5, 5000), 2)}

def record_count(ctx, dataset):
    return ctx.admin().get("/orders/count")["count"]

def seed_to(ctx, target, dataset):
    api, start = ctx.admin(), record_count(ctx, dataset)
    n = start
    while n < target:
        size = min(1000, target - n)
        api.post("/orders/bulk", {"items": [order(n + i + 1, ctx.seed) for i in range(size)]})
        n += size
    return {"start_count": start, "added": n - start}

def record_ids(ctx, dataset, limit):
    return [o["id"] for o in ctx.admin().get("/orders", params={"limit": limit})["items"]]

def after_run(ctx, dataset):
    return ctx.admin().post("/orders/purge", {"ref_prefix": "LT-"})["deleted"]
```

A fuller example is `projects/demo/hooks.py`: seeding, record ids, trimming
and cleanup for the demo orders API.

### Example: accounts that must change their password at first sign-in

```python
from perfkit.core import HarnessError

def users(ctx):
    """Create member accounts with a temporary password; sign-in changes it once."""
    api, temp, final = ctx.admin(), ctx.secret("user_temp_password"), ctx.secret("user_password")
    pacer, out = ctx.pacer(), []
    for n in range(1, ctx.cfg["users"]["count"] + 1):
        email = ctx.cfg["users"]["email"].format(n=n)
        pacer.wait()
        resp = api.call("POST", "/users", body={"email": email, "temporary_password": temp}, raw=True)
        if resp.status_code not in (200, 201, 409):          # 409: exists from an earlier setup
            raise HarnessError(f"Create {email}: HTTP {resp.status_code}")
        out.append({"email": email, "password": final, "initial_password": temp})
    return out

def login(ctx, user):
    for pw in (user["password"], user.get("initial_password")):
        resp = ctx.api().call("POST", "/auth/login", body={"email": user["email"], "password": pw}, raw=True)
        if resp.status_code == 200:
            data = resp.json()["data"]
            if data.get("must_change_password"):
                ctx.api(data["token"]).post("/auth/change-password",
                                            {"current_password": pw, "new_password": user["password"]})
            return data["token"]
    raise HarnessError(f"Could not sign in {user['email']}")
```
