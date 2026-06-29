"""Static guardrails for tenant-scoped SQL."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "eos"

TENANT_ID_TABLES = {
    "api_tokens",
    "appointment_holds",
    "appointments",
    "brand_kits",
    "clients",
    "contracts",
    "email_sequence_runs",
    "email_sequences",
    "emails_log",
    "galleries",
    "inquiries",
    "invoices",
    "listing_marketing_kit",
    "listing_media",
    "listings",
    "promo_codes",
    "proposals",
    "questionnaires",
    "service_addons",
    "service_packages",
    "studio_profiles",
    "users",
    "webhook_deliveries",
    "webhook_subscriptions",
}

LISTING_SCOPED_TABLES = {
    "appointments",
    "contracts",
    "email_sequence_runs",
    "emails_log",
    "galleries",
    "inquiries",
    "invoices",
    "listing_marketing_kit",
    "listing_media",
    "listing_shots",
    "listing_tasks",
    "mls_push_log",
    "proposals",
    "questionnaires",
}

SKIP_PATHS = {
    "eos/dogfood.py",  # demo seed/reset code intentionally manipulates default fixture rows.
}

UNSCOPED_ID_ALLOWLIST = {
    # Owner resolution before binding tenant context.
    ("eos/delivery_notify.py", "galleries"),
    ("eos/jobs.py", "galleries"),
    ("eos/jobs.py", "listings"),
    ("eos/stripe_webhooks.py", "invoices"),
    # Session/auth resolution before a tenant is known.
    ("eos/platform_admin.py", "users"),
    ("eos/rbac.py", "users"),
    ("eos/security.py", "users"),
    ("eos/tenant.py", "users"),
}

TABLE_RE = re.compile(r"\b(?:from|update|delete\s+from)\s+([a-z_][a-z0-9_]*)", re.I)
ID_PREDICATE_RE = re.compile(r"\bid\s*=\s*\?", re.I)
LISTING_PREDICATE_RE = re.compile(r"\blisting_id\s*=\s*\?", re.I)
SQL_RE = re.compile(r"\b(select|update|delete)\b", re.I)
STUDIO_RE = re.compile(r"\bstudio_id\b", re.I)


@dataclass(frozen=True)
class SqlLiteral:
    relpath: str
    line: int
    sql: str


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


def _joined_str(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            parts.append("{}")
    return "".join(parts)


def _sql_literals() -> list[SqlLiteral]:
    literals: list[SqlLiteral] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relpath = path.relative_to(ROOT).as_posix()
        if relpath in SKIP_PATHS:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            sql: str | None = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = node.value
            elif isinstance(node, ast.JoinedStr):
                sql = _joined_str(node)
            if not sql:
                continue
            compact = _normalize(sql)
            if SQL_RE.search(compact):
                literals.append(SqlLiteral(relpath, getattr(node, "lineno", 0), compact))
    return literals


def _tables(sql: str) -> set[str]:
    return {match.group(1).lower() for match in TABLE_RE.finditer(sql)}


def _where_clause(sql: str) -> str:
    _before, marker, after = sql.lower().partition(" where ")
    return after if marker else ""


def _has_studio_predicate(sql: str) -> bool:
    return bool(STUDIO_RE.search(_where_clause(sql)))


def test_tenant_owned_id_sql_has_studio_scope_or_documented_owner_lookup():
    violations: list[str] = []
    for literal in _sql_literals():
        if not ID_PREDICATE_RE.search(literal.sql):
            continue
        for table in sorted(_tables(literal.sql) & TENANT_ID_TABLES):
            if _has_studio_predicate(literal.sql):
                continue
            if (literal.relpath, table) in UNSCOPED_ID_ALLOWLIST:
                continue
            violations.append(f"{literal.relpath}:{literal.line} {table}: {literal.sql}")

    assert not violations, "Tenant-owned id SQL must include a studio_id predicate:\n" + "\n".join(
        violations
    )


def test_listing_scoped_tenant_sql_has_studio_scope():
    violations: list[str] = []
    for literal in _sql_literals():
        if not LISTING_PREDICATE_RE.search(literal.sql):
            continue
        for table in sorted(_tables(literal.sql) & LISTING_SCOPED_TABLES):
            if _has_studio_predicate(literal.sql):
                continue
            violations.append(f"{literal.relpath}:{literal.line} {table}: {literal.sql}")

    assert not violations, "Listing-scoped SQL must include a studio_id predicate:\n" + "\n".join(
        violations
    )
