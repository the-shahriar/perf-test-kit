"""perfkit command line.

    python perfkit.py list
    python perfkit.py init <name>                         # new project from the template
    python perfkit.py show <project>                      # cases, journey and tests as configured
    python perfkit.py check <project> [--target T]
    python perfkit.py setup <project> [--target T] [--recreate]
    python perfkit.py run <project> <test> [<test> ...] --run-id ID [--quick] [--target T]
    python perfkit.py report <project> --run-id ID [--format xlsx|html|both]
    python perfkit.py cleanup <project> --yes [--target T]

Any target that is not this machine needs --yes on setup, run and cleanup.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from .core import PROJECTS, HarnessError, die, log, write_json


def _ctx(args):
    from .project import Context, Project

    return Context(Project(args.project, getattr(args, "target", None)))


def _confirm_remote(ctx, args, action: str) -> None:
    if not ctx.is_local() and not getattr(args, "yes", False):
        die(f"Target '{ctx.target}' ({ctx.cfg['base_url']}) is not this machine. {action} writes to it. "
            "Re-run with --yes once the test window is approved.")


def cmd_list(args) -> None:
    from .project import Project

    for d in sorted(PROJECTS.iterdir()):
        if not (d / "project.yaml").exists() or d.name.startswith("_"):
            continue
        try:
            p = Project(str(d))
            print(f"{d.name:<24} {p.name}   targets: {', '.join(p.targets)}")
            print(f"{'':<24} tests: {', '.join(p.cfg['tests'])}")
        except HarnessError as exc:
            print(f"{d.name:<24} (invalid: {exc})")


def cmd_init(args) -> None:
    src = PROJECTS / (args.template or "_template")
    dest = Path(args.dir) if args.dir else PROJECTS / args.name
    if dest.exists():
        die(f"{dest} already exists")
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".state", "results", "__pycache__"))
    yml = dest / "project.yaml"
    yml.write_text(yml.read_text(encoding="utf-8").replace("My Project", args.name), encoding="utf-8")
    log(f"Created {dest}. Next: edit project.yaml (targets, auth, cases, journey), then `perfkit check {args.name}`")


def cmd_show(args) -> None:
    ctx = _ctx(args)
    cfg = ctx.cfg
    print(f"\n{ctx.project.name}  (target {ctx.target}: {cfg['base_url']}{cfg.get('api_prefix') or ''})\n")
    print("Cases:")
    for cid, c in cfg["cases"].items():
        print(f"  {cid:<8} {c['kind']:<6} {c['method'].upper():<6} {c['path']:<50} {c['label']}")
    steps = (cfg.get("journey") or {}).get("steps") or []
    if steps:
        print("\nJourney:")
        for i, st in enumerate(steps, 1):
            ids = ([st["case"]] if st.get("case") else []) + list(st.get("cases") or [])
            extra = []
            if "probability" in st:
                extra.append(f"p={st['probability']}")
            if st.get("repeat"):
                extra.append(f"x{st['repeat']}")
            print(f"  {i:>2}. {', '.join(ids)}  {' '.join(extra)}")
    print("\nTests:")
    for name, t in cfg["tests"].items():
        print(f"  {name:<16} type={t['type']}")
    hooks = [h for h in ("users", "login", "setup", "seed_to", "record_count", "record_ids", "after_run", "cleanup",
                         "describe", "environment", "check", "vars") if ctx.hook(h)]
    print(f"\nHooks: {', '.join(hooks) if hooks else 'none'}\n")


def cmd_check(args) -> None:
    from .check import run_checks

    if not run_checks(_ctx(args)):
        die("One or more checks failed (see above).")


def cmd_setup(args) -> None:
    from . import auth

    ctx = _ctx(args)
    _confirm_remote(ctx, args, "Setup")
    if args.recreate:
        for name in ("state.json", "tokens.json", "users.json"):
            (ctx.state_dir / name).unlink(missing_ok=True)
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    if ctx.hook("setup"):
        log("Running the project's setup hook")
        ctx.call("setup", recreate=args.recreate)
    auth.provision_users(ctx)
    tokens = auth.sign_in_all(ctx)
    log(f"Setup complete: {len(tokens)} session(s) ready in {ctx.state_dir}")


def cmd_run(args) -> None:
    from .tests import run_test

    ctx = _ctx(args)
    _confirm_remote(ctx, args, "A run")
    for name in args.tests:
        log(f"=== {name} ({ctx.cfg['tests'].get(name, {}).get('type', '?')}) — run {args.run_id} ===")
        run_test(ctx, args.run_id, name, args.quick)
    if not args.no_report:
        cmd_report(args)


def cmd_report(args) -> None:
    from .report.build import build_all

    ctx = _ctx(args)
    run_dir = ctx.project.results_dir / args.run_id
    fmt = getattr(args, "format", None)
    formats = None if fmt in (None, "auto") else (["xlsx", "html"] if fmt == "both" else [fmt])
    for path in build_all(run_dir, ctx.project.dir, formats):
        log(f"Wrote {path}")


def cmd_cleanup(args) -> None:
    ctx = _ctx(args)
    if not args.yes:
        die("cleanup removes what setup and the runs created on the target. Re-run with --yes.")
    result = ctx.call("cleanup", default=None)
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    write_json(ctx.state_dir / f"cleanup_{stamp}.json", {"result": result})
    for name in ("state.json", "tokens.json"):
        p = ctx.state_dir / name
        if p.exists():
            p.rename(ctx.state_dir / f"{p.stem}.cleaned-{stamp}.json")
    log("Cleanup done" + ("" if ctx.hook("cleanup") else " (no cleanup hook: only local state was reset)"))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="perfkit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="projects and their tests")
    i = sub.add_parser("init", help="create a project from the template")
    i.add_argument("name")
    i.add_argument("--template", help="copy another project instead of _template (e.g. demo)")
    i.add_argument("--dir", help="create the project folder here instead of projects/<name>")

    def project_cmd(name, help_text):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("project", help="name under projects/ or a path to a project folder")
        s.add_argument("--target", help="target from project.yaml (default: default_target)")
        return s

    project_cmd("show", "print the cases, journey, tests and hooks")
    project_cmd("check", "verify target, k6, sign-in, database and monitoring")
    s = project_cmd("setup", "create test data and accounts, sign them in")
    s.add_argument("--recreate", action="store_true", help="forget earlier test objects and sessions")
    s.add_argument("--yes", action="store_true")
    r = project_cmd("run", "run one or more tests")
    r.add_argument("tests", nargs="+")
    r.add_argument("--run-id", required=True)
    r.add_argument("--quick", action="store_true", help="scale the test down to minutes (pipeline check)")
    r.add_argument("--no-report", action="store_true")
    r.add_argument("--format", choices=["auto", "xlsx", "html", "both"], default="auto")
    r.add_argument("--yes", action="store_true")
    rp = project_cmd("report", "build the reports of a run")
    rp.add_argument("--run-id", required=True)
    rp.add_argument("--format", choices=["auto", "xlsx", "html", "both"], default="auto")
    c = project_cmd("cleanup", "remove test objects (cleanup hook) and reset local state")
    c.add_argument("--yes", action="store_true")

    args = p.parse_args(argv)
    handlers = {"list": cmd_list, "init": cmd_init, "show": cmd_show, "check": cmd_check, "setup": cmd_setup,
                "run": cmd_run, "report": cmd_report, "cleanup": cmd_cleanup}
    try:
        handlers[args.command](args)
    except HarnessError as exc:
        die(str(exc))
    except KeyboardInterrupt:
        die("Interrupted. Re-run the same command with the same --run-id to resume.")
