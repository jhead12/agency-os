"""
One-time copy of a SQLite agency-os database (the pre-PostgreSQL format)
into PostgreSQL. Used by `agency-os import-sqlite`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.db import Database

# Parents before children, so foreign keys resolve as rows go in.
TABLES = [
    "prospects", "campaigns", "outreach", "email_log", "call_log",
    "product_events", "job_runs", "sync_cursors",
    "users", "roles", "role_permissions", "user_roles",
    "sessions", "audit_log", "invites",
]


def import_sqlite(sqlite_path: str | Path, db: Database) -> tuple[dict[str, int], dict[str, int]]:
    """Copy every table from sqlite_path into db, keeping ids.

    Returns (rows copied, orphaned rows skipped) per table. SQLite only enforced
    foreign keys when asked to, so old databases can hold rows pointing at
    deleted parents; PostgreSQL would reject those.

    All or nothing: runs in one transaction, and refuses to run if the
    target already has prospects or users, so it can't create duplicates.
    """
    path = Path(sqlite_path)
    if not path.exists():
        raise FileNotFoundError(f"No SQLite database at {path}")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    src_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    orphans: dict[str, set[int]] = {}
    for table, rowid, _parent, _fkid in src.execute("PRAGMA foreign_key_check"):
        orphans.setdefault(table, set()).add(rowid)

    copied, skipped = {}, {}
    with db.transaction() as c:
        for table in ("prospects", "users"):
            if c.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise RuntimeError(f"The target database already has {table}; import into an empty one.")

        # The schema seeds starter roles on first use; the source has its own.
        c.execute("DELETE FROM role_permissions")
        c.execute("DELETE FROM roles")

        for table in TABLES:
            if table not in src_tables:
                continue
            pg_types = {r["column_name"]: r["data_type"] for r in c.execute(
                """SELECT column_name, data_type FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = ?""",
                (table,),
            ).fetchall()}
            src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
            cols = [col for col in src_cols if col in pg_types]
            bad = orphans.get(table, set())
            rows = [
                tuple(_clean(row[col], pg_types[col]) for col in cols)
                for row in src.execute(f"SELECT rowid AS _rowid, {', '.join(cols)} FROM {table}")
                if row["_rowid"] not in bad
            ]
            c.executemany(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                rows,
            )
            if pg_types.get("id") == "integer":
                # Ids were copied as-is; move the id counter past them.
                c.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table}"
                )
            copied[table] = len(rows)
            if bad:
                skipped[table] = len(bad)
    src.close()
    return copied, skipped


def _clean(value, pg_type: str):
    """SQLite stores any value in any column; blank strings become NULL for typed columns."""
    if value == "" and pg_type not in ("text", "USER-DEFINED"):
        return None
    return value
