"""Optional domain hooks. Delete this file if the project needs none.

Every function is optional: define only what your tests need. Each receives
`ctx` (see docs/HOOKS.md): ctx.cfg, ctx.state / ctx.update_state(), ctx.admin()
(API client signed in as the admin), ctx.api(token), ctx.secret(name),
ctx.pacer(per_minute), ctx.db(), ctx.seed, ctx.log().

Needed by:
  volume tests, and any test with `records:`   -> seed_to, record_count
  cases that use {{pick:records}}               -> record_ids
  tests that create data and must stay stable   -> after_run
"""

# def setup(ctx, recreate=False):
#     """Create the test objects once (a project, a tenant, a catalogue ...). Store their ids:
#     ctx.update_state(project_id=...). Reference them in project.yaml as $state.project_id."""
#
# def users(ctx):
#     """Create or list the test accounts. Return [{"email": ..., "password": ...}, ...]."""
#
# def login(ctx, user):
#     """Replace auth.login when sign-in needs more than one request. Return the token."""
#
# def record_count(ctx, dataset):
#     """How many records the dataset holds now."""
#
# def seed_to(ctx, target, dataset):
#     """Grow the dataset to `target` records (through the API, so seeding is realistic).
#     Return a dict of facts for the report, e.g. {"start_count": 0, "added": 5000}."""
#
# def record_ids(ctx, dataset, limit):
#     """Ids the journey may open and edit ({{pick:records}}, {{pick:own_records}})."""
#
# def after_run(ctx, dataset):
#     """Remove what a run created, so the next run starts at the same size. Return the count."""
#
# def cleanup(ctx):
#     """Remove everything setup and the runs created. Called by `perfkit cleanup --yes`."""
#
# def describe(ctx):
#     """Test objects to list in the report (ids, field lists ...)."""
#
# def environment(ctx):
#     """Extra environment facts for the report (versions, instance sizes ...)."""
#
# def check(ctx):
#     """Extra pre-flight checks: return [(ok: bool | None, name, detail), ...]."""
