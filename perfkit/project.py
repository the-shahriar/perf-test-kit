"""Loads a project definition and builds the context hooks and runners use.

A project is a folder with project.yaml (required) and hooks.py (optional):

    projects/<name>/project.yaml   what to test: targets, auth, users, cases, journey, tests
    projects/<name>/hooks.py       how to prepare data for this domain (optional)

The folder can live anywhere; pass its path instead of a name.
"""

from __future__ import annotations

import importlib.util
import os
import secrets as pysecrets
import string
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .core import (
    DEFAULTS_FILE, LOCAL_HOSTS, PROJECTS, HarnessError, Pacer, deep_merge, log, read_json, write_json,
)

TEST_TYPES = {"smoke", "performance", "load", "stress", "spike", "soak", "volume"}
CASE_KINDS = {"read", "write", "auth", "other"}


def resolve_project_dir(ref: str) -> Path:
    path = Path(ref)
    if (path / "project.yaml").exists():
        return path.resolve()
    named = PROJECTS / ref
    if (named / "project.yaml").exists():
        return named.resolve()
    raise HarnessError(f"No project '{ref}': expected {named / 'project.yaml'} or a folder with project.yaml")


def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise HarnessError(f"{path} must contain a mapping at the top level")
    return data


class Project:
    def __init__(self, ref: str, target: str | None = None):
        self.dir = resolve_project_dir(ref)
        raw = _load_yaml(self.dir / "project.yaml")
        defaults = _load_yaml(DEFAULTS_FILE)
        # Test entries inherit their type's defaults, so merge tests separately.
        base_tests = defaults.pop("tests")
        project_tests = raw.pop("tests", {}) or {}
        targets = raw.pop("targets", None) or {}
        if not targets:
            raise HarnessError("project.yaml needs at least one entry under `targets:`")
        self.name = (raw.get("project") or {}).get("name") or self.dir.name
        self.slug = self.dir.name
        self.targets = list(targets)
        self.target = target or raw.get("default_target") or next(iter(targets))
        if self.target not in targets:
            raise HarnessError(f"Unknown target '{self.target}'. Defined: {', '.join(targets)}")
        cfg = deep_merge(defaults, raw)
        target_cfg = dict(targets[self.target] or {})
        target_tests = target_cfg.pop("tests", {}) or {}
        cfg = deep_merge(cfg, target_cfg)
        cfg["target"] = self.target
        cfg["tests"] = self._merge_tests(base_tests, deep_merge(project_tests, target_tests))
        self.cfg = cfg
        self.state_dir = self.dir / ".state" / self.target
        self.results_dir = self.dir / "results"
        self.hooks = self._load_hooks()
        self.validate()

    # ── configuration ──
    @staticmethod
    def _merge_tests(base: dict, project: dict) -> dict:
        out = {}
        for name in list(base) + [n for n in project if n not in base]:
            entry = dict(project.get(name) or {})
            ttype = entry.get("type") or (name if name in base else None)
            if ttype not in TEST_TYPES:
                raise HarnessError(f"Test '{name}' needs `type:` (one of {', '.join(sorted(TEST_TYPES))})")
            out[name] = deep_merge(base[ttype], entry)
            out[name]["type"] = ttype
        return out

    def validate(self) -> None:
        cfg = self.cfg
        if not cfg.get("base_url"):
            raise HarnessError(f"Target '{self.target}' has no base_url")
        cases = cfg.get("cases") or {}
        if not cases:
            raise HarnessError("project.yaml defines no `cases:`")
        for cid, case in cases.items():
            if not isinstance(case, dict) or not case.get("path") or not case.get("method"):
                raise HarnessError(f"Case {cid} needs at least `method` and `path`")
            case.setdefault("label", cid)
            case.setdefault("kind", "read" if case["method"].upper() == "GET" else "write")
            if case["kind"] not in CASE_KINDS:
                raise HarnessError(f"Case {cid}: kind must be one of {sorted(CASE_KINDS)}")
            for step in (case.get("standalone") or {}).get("before", []) + (case.get("standalone") or {}).get("after", []):
                if "case" in step and step["case"] not in cases:
                    raise HarnessError(f"Case {cid} standalone step refers to unknown case {step['case']}")
        for i, step in enumerate((cfg.get("journey") or {}).get("steps", []), 1):
            for c in step_cases(step):
                if c not in cases:
                    raise HarnessError(f"Journey step {i} refers to unknown case {c}")
        a = cfg["auth"]
        if a["type"] not in ("none", "login", "token"):
            raise HarnessError("auth.type must be none, login or token")
        if a["type"] == "login" and not (a.get("login") or self.has_hook("login")):
            raise HarnessError("auth.type is login but auth.login is not configured (and no login hook)")

    def is_local(self) -> bool:
        return urlparse(self.cfg["base_url"]).hostname in LOCAL_HOSTS

    # ── hooks ──
    def _load_hooks(self):
        path = self.dir / "hooks.py"
        if not path.exists():
            return None
        if str(self.dir) not in sys.path:
            sys.path.insert(0, str(self.dir))   # hooks may import sibling modules (datagen.py ...)
        spec = importlib.util.spec_from_file_location(f"perfkit_hooks_{self.slug.replace('-', '_')}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def has_hook(self, name: str) -> bool:
        return self.hooks is not None and callable(getattr(self.hooks, name, None))


def step_cases(step: dict) -> list[str]:
    out = list(step.get("cases") or [])
    if step.get("case"):
        out.insert(0, step["case"])
    return out


def assert_allowed(url_or_host: str, cfg: dict, key: str = "allowed_hosts") -> None:
    """This machine always; any other host only when the target lists it by name."""
    host = urlparse(url_or_host).hostname if "://" in url_or_host else url_or_host
    if host in LOCAL_HOSTS or host in (cfg.get(key) or []):
        return
    raise HarnessError(
        f"Refusing to contact '{host}': it is not listed in `{key}` of target '{cfg.get('target')}'. "
        "Add it there once the test window is approved."
    )


class Context:
    """What hooks receive: the merged config, state, API clients, secrets and logging."""

    def __init__(self, project: Project):
        self.project = project
        self.cfg = project.cfg
        self.target = project.target
        self.state_dir = project.state_dir
        self.log = log
        self._admin = None

    # state files (gitignored, per target)
    @property
    def state(self) -> dict:
        return read_json(self.state_dir / "state.json")

    def update_state(self, **changes) -> dict:
        data = self.state
        data.update(changes)
        write_json(self.state_dir / "state.json", data)
        return data

    def ledger(self, dataset: str = "default") -> int:
        return int((self.state.get("ledger") or {}).get(dataset, 0))

    def set_ledger(self, dataset: str, count: int) -> None:
        led = dict(self.state.get("ledger") or {})
        led[dataset] = int(count)
        self.update_state(ledger=led)

    def secret(self, name: str, length: int = 20) -> str:
        """A generated secret, created once per target and kept in .state/<target>/secrets.json."""
        path = self.state_dir / "secrets.json"
        sec = read_json(path)
        if not sec.get(name):
            alphabet = string.ascii_letters + string.digits
            sec[name] = "Pk" + "".join(pysecrets.choice(alphabet) for _ in range(length)) + "9!"
            write_json(path, sec)
        return sec[name]

    @property
    def seed(self) -> int:
        return int(self.cfg["random_seed"])

    def is_local(self) -> bool:
        return self.project.is_local()

    def pacer(self, per_minute: float | None = None) -> Pacer:
        return Pacer(per_minute if per_minute is not None else self.cfg["pacing"].get("admin_writes_per_minute"))

    # HTTP
    def api(self, token: str | None = None):
        from .http import Api

        return Api(self.cfg, token)

    def admin(self):
        """An API client signed in as the admin account (see auth.admin)."""
        from .auth import admin_api

        if self._admin is None:
            self._admin = admin_api(self)
        return self._admin

    # database (optional, see monitoring.database)
    def db(self, read_only: bool = True):
        from .db import connect

        return connect(self.cfg, read_only=read_only)

    def has_db(self) -> bool:
        from .db import configured

        return configured(self.cfg)

    # hooks
    def hook(self, name: str):
        return getattr(self.project.hooks, name) if self.project.has_hook(name) else None

    def call(self, name: str, *args, default=None, **kwargs):
        fn = self.hook(name)
        return fn(self, *args, **kwargs) if fn else default

    def dataset_vars(self, dataset: str = "default") -> dict:
        """Run variables for k6 ({{ctx.x}}): `vars`, then the dataset's `vars`, then the
        vars hook. A string value "$state.key" is replaced by that key from state.json."""
        ds = (self.cfg.get("datasets") or {}).get(dataset)
        if ds is None:
            raise HarnessError(f"Unknown dataset '{dataset}'. Defined: {', '.join(self.cfg.get('datasets') or {})}")
        merged = {**(self.cfg.get("vars") or {}), **(ds.get("vars") or {})}
        st = self.state
        out = {}
        for k, v in merged.items():
            if isinstance(v, str) and v.startswith("$state."):
                v = st.get(v[len("$state."):])
            elif isinstance(v, str) and v.startswith("$env."):
                v = os.environ.get(v[len("$env."):], "")
            out[k] = v
        out.update(self.call("vars", dataset, default={}) or {})
        out["dataset"] = dataset
        return out
