"""SQLite access — WAL mode, numbered migrations (Hestia pattern)."""

import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any

from . import config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_VERSION_RE = re.compile(r"^(\d+)_")
_TRANSACTION_SQL = {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}
_LEADING_SQL_COMMENTS_RE = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.DOTALL)
_FOREIGN_KEYS_PRAGMA_RE = re.compile(
    r"\APRAGMA\s+foreign_keys\s*=\s*(ON|OFF)\s*;?\s*\Z", re.IGNORECASE
)
_connection: ContextVar[sqlite3.Connection | None] = ContextVar("eos_db_connection", default=None)
_after_commit_callbacks: ContextVar[list[Callable[[], Any]] | None] = ContextVar(
    "eos_db_after_commit", default=None
)


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


def _migration_statements(script: str) -> list[str]:
    """Split a migration without sqlite3.executescript's implicit COMMIT."""
    statements: list[str] = []
    pending: list[str] = []
    for char in script:
        pending.append(char)
        if char == ";":
            candidate = "".join(pending)
            if sqlite3.complete_statement(candidate):
                statements.append(candidate)
                pending.clear()
    remainder = "".join(pending).strip()
    if remainder:
        statements.append(remainder)
    return statements


def _statement_body(statement: str) -> str:
    return _LEADING_SQL_COMMENTS_RE.sub("", statement, count=1).strip()


def _prepare_migration(script: str) -> tuple[list[str], bool]:
    """Validate transaction ownership and pull out connection-level FK pragmas."""
    executable: list[str] = []
    disables_foreign_keys = False
    for statement in _migration_statements(script):
        body = _statement_body(statement)
        if not body:
            continue
        keyword_match = re.match(r"[A-Za-z]+", body)
        keyword = keyword_match.group(0).upper() if keyword_match else ""
        if keyword in _TRANSACTION_SQL:
            raise ValueError(f"migration transaction control is not allowed: {keyword}")
        foreign_keys = _FOREIGN_KEYS_PRAGMA_RE.fullmatch(body)
        if foreign_keys:
            disables_foreign_keys |= foreign_keys.group(1).upper() == "OFF"
            continue
        executable.append(statement)
    return executable, disables_foreign_keys


def _ensure_migrations_table(con: sqlite3.Connection) -> None:
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
               version TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL DEFAULT (datetime('now')))"""
        )
        con.commit()
    except Exception:
        con.rollback()
        raise


def _apply_migration(
    con: sqlite3.Connection,
    version: str,
    name: str,
    statements: list[str],
    *,
    disables_foreign_keys: bool,
) -> None:
    foreign_keys_before = int(con.execute("PRAGMA foreign_keys").fetchone()[0])
    if disables_foreign_keys:
        con.execute("PRAGMA foreign_keys=OFF")
    try:
        con.execute("BEGIN IMMEDIATE")
        already_applied = con.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (version,),
        ).fetchone()
        if already_applied:
            con.commit()
            return
        for statement in statements:
            con.execute(statement)
        con.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (version, name),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_before else 'OFF'}")


def migrate() -> None:
    config.ensure_dirs()
    con = connect()
    try:
        _ensure_migrations_table(con)
        for version, name, path in _discover_migrations():
            statements, disables_foreign_keys = _prepare_migration(path.read_text())
            _apply_migration(
                con,
                version,
                name,
                statements,
                disables_foreign_keys=disables_foreign_keys,
            )
    finally:
        con.close()


def one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    active = _connection.get()
    if active is not None:
        return active.execute(sql, params).fetchone()
    con = connect()
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()


def all_(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    active = _connection.get()
    if active is not None:
        return active.execute(sql, params).fetchall()
    con = connect()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def run(sql: str, params: tuple = ()) -> int:
    active = _connection.get()
    if active is not None:
        return active.execute(sql, params).lastrowid
    con = connect()
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def in_transaction() -> bool:
    return _connection.get() is not None


def after_commit(callback: Callable[[], Any]) -> None:
    """Run callback after the outer transaction commits, or immediately when not in one."""
    callbacks = _after_commit_callbacks.get()
    if callbacks is None:
        callback()
        return
    callbacks.append(callback)


@contextmanager
def tx(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Join the active transaction or open one.

    ``BEGIN IMMEDIATE`` serializes workflows that re-check availability
    immediately before writing.
    """
    active = _connection.get()
    if active is not None:
        yield active
        return

    con = connect()
    con_token = _connection.set(con)
    callbacks: list[Callable[[], Any]] = []
    callbacks_token = _after_commit_callbacks.set(callbacks)
    try:
        if immediate:
            con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        _after_commit_callbacks.reset(callbacks_token)
        _connection.reset(con_token)
        con.close()
    for callback in callbacks:
        callback()


def audit(actor: str, action: str, detail: str | None = None) -> None:
    from .vocab import STUDIO_ID

    run(
        "INSERT INTO audit_log (studio_id, actor, action, detail) VALUES (?, ?, ?, ?)",
        (str(STUDIO_ID), actor, action, detail),
    )


def transactional(*, immediate: bool = False):
    """Decorator for a domain command that must commit as one unit."""

    def decorate(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            with tx(immediate=immediate):
                return func(*args, **kwargs)

        return wrapped

    return decorate
