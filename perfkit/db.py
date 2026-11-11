"""Optional PostgreSQL access for monitoring (connections, top SQL, table sizes).

monitoring.database:
  host / port / name           or  dsn_env: NAME_OF_ENV_VAR_WITH_DSN
  user_env / password_env      environment variables holding the login
  sslmode                      default prefer
  top_sql: off | read | reset  read pg_stat_statements; `reset` also clears it before each run
  tables: [orders, ...]        tables whose size and row counts are recorded per run

Monitoring connections are opened read-only (default_transaction_read_only=on).
Nothing in the kit writes through this connection; a hook may ask for a
writable one with ctx.db(read_only=False) on a local target.
"""

from __future__ import annotations

import os

from .core import HarnessError
from .project import assert_allowed


def configured(cfg: dict) -> bool:
    d = (cfg.get("monitoring") or {}).get("database")
    if not d or d.get("enabled") is False:
        return False
    if d.get("dsn_env"):
        return bool(os.environ.get(d["dsn_env"]))
    has_user = d.get("user") or os.environ.get(d.get("user_env") or "")
    has_password = d.get("password") or not d.get("password_env") or os.environ.get(d["password_env"])
    return bool(d.get("host") and has_user and has_password)


def connect(cfg: dict, read_only: bool = True):
    import psycopg2

    d = (cfg.get("monitoring") or {}).get("database") or {}
    if not configured(cfg):
        raise HarnessError("No database configured (monitoring.database)")
    if not read_only and not d.get("allow_writes"):
        raise HarnessError("A writable database connection needs monitoring.database.allow_writes: true")
    options = "-c default_transaction_read_only=on" if read_only else ""
    if d.get("dsn_env"):
        conn = psycopg2.connect(os.environ[d["dsn_env"]], options=options, application_name="perfkit")
    else:
        assert_allowed(d["host"], cfg, "allowed_db_hosts")
        conn = psycopg2.connect(
            host=d["host"], port=d.get("port", 5432), dbname=d.get("name"),
            user=d.get("user") or os.environ.get(d.get("user_env") or ""),
            password=d.get("password") or os.environ.get(d.get("password_env") or "", ""),
            sslmode=d.get("sslmode", "prefer"), options=options, application_name="perfkit",
        )
    conn.autocommit = True
    return conn


def rows(conn, query: str, params=None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def connection_counts(conn) -> dict:
    data = rows(conn, """
        select coalesce(state, 'other') as state, count(*) as n
        from pg_stat_activity where datname = current_database() group by 1
    """)
    c = {r["state"]: int(r["n"]) for r in data}
    return {"total": sum(c.values()), "active": c.get("active", 0), "idle": c.get("idle", 0),
            "idle_in_tx": c.get("idle in transaction", 0) + c.get("idle in transaction (aborted)", 0)}


def reset_statements(cfg: dict) -> str:
    if not configured(cfg):
        return "not collected (no database configured)"
    mode = ((cfg.get("monitoring") or {}).get("database") or {}).get("top_sql", "off")
    if mode == "off":
        return "not collected (top_sql: off)"
    if mode == "read":
        return "collected (cumulative: pg_stat_statements is not reset per run)"
    try:
        conn = connect(cfg)
        try:
            with conn.cursor() as cur:
                cur.execute("select pg_stat_statements_reset()")
        finally:
            conn.close()
        return "collected"
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return f"not collected ({type(exc).__name__}: {str(exc)[:120]})"


def snapshot(cfg: dict, top_sql_status: str) -> dict:
    """Database size, the configured tables' sizes and row estimates, and top SQL."""
    if not configured(cfg):
        return {"status": "not collected (no database configured)", "tables": [], "top_sql": []}
    d = cfg["monitoring"]["database"]
    try:
        conn = connect(cfg)
    except Exception as exc:  # noqa: BLE001
        return {"status": f"not collected ({type(exc).__name__})", "tables": [], "top_sql": []}
    try:
        out = {"status": "collected", "database_bytes": rows(conn, "select pg_database_size(current_database()) as b")[0]["b"]}
        tables = []
        for t in d.get("tables") or []:
            found = rows(conn, """
                select c.relname as table, pg_total_relation_size(c.oid) as total_bytes,
                       pg_relation_size(c.oid) as heap_bytes, pg_indexes_size(c.oid) as index_bytes,
                       s.n_live_tup as live_rows, s.n_dead_tup as dead_rows
                from pg_class c left join pg_stat_user_tables s on s.relid = c.oid
                where c.relname = %s and c.relkind = 'r' limit 1
            """, (t,))
            tables.extend(found)
        out["tables"] = tables
        out["top_sql"] = []
        out["top_sql_status"] = top_sql_status
        if top_sql_status.startswith("collected"):
            out["top_sql"] = rows(conn, """
                select left(regexp_replace(s.query, '\\s+', ' ', 'g'), 300) as query, s.calls,
                       round(s.total_exec_time::numeric, 1) as total_ms, round(s.mean_exec_time::numeric, 2) as mean_ms,
                       round(s.max_exec_time::numeric, 2) as max_ms, s.rows
                from pg_stat_statements s join pg_database db on db.oid = s.dbid
                where db.datname = current_database() and s.query not ilike '%%pg_stat_%%'
                order by s.total_exec_time desc limit 10
            """)
        return out
    except Exception as exc:  # noqa: BLE001
        return {"status": f"partly collected ({type(exc).__name__}: {str(exc)[:120]})", "tables": [], "top_sql": []}
    finally:
        conn.close()
