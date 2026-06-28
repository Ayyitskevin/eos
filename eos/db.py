"""SQLite access — WAL mode, numbered migrations (Hestia pattern)."""

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_VERSION_RE = re.compile(r"^(\d+)_")


def connect() -> sqlite3.Connection:
    from .vocab import _StudioId

    sqlite3.register_adapter(_StudioId, str)
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _discover_migrations() -> list[tuple[str, str, Path]]:
    found: list[tuple[int, str, str, Path]] = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        m = _VERSION_RE.match(path.name)
        if not m:
            continue
        found.append((int(m.group(1)), m.group(1), path.stem, path))
    found.sort(key=lambda r: r[0])
    return [(ver, name, path) for _, ver, name, path in found]


def migrate() -> None:
    config.ensure_dirs()
    con = connect()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
               version TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL DEFAULT (datetime('now')))"""
        )
        applied = {r["version"] for r in con.execute("SELECT version FROM schema_migrations")}
        for version, name, path in _discover_migrations():
            if version in applied:
                continue
            con.executescript(path.read_text())
            con.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (version, name),
            )
            con.commit()
    finally:
        con.close()


def one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    con = connect()
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()


def all_(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    con = connect()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def run(sql: str, params: tuple = ()) -> int:
    con = connect()
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def audit(actor: str, action: str, detail: str | None = None) -> None:
    from .vocab import STUDIO_ID

    run(
        "INSERT INTO audit_log (studio_id, actor, action, detail) VALUES (?, ?, ?, ?)",
        (str(STUDIO_ID), actor, action, detail),
    )