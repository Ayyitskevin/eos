"""Static guardrails for tenant-scoped SQL."""

from __future__ import annotations

import ast
import importlib
import re
from dataclasses import dataclass
from pathlib import Path

import eos.clients as clients
import eos.config as config
import eos.db as db
import eos.invoices as invoices
import eos.listings as listings
import eos.studio as studio
import eos.tenant as tenant
import eos.usage as usage
import eos.vocab as vocab
import pytest

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
    "referral_codes",
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


# ---------------------------------------------------------------------------
# Behavioral guardrails for the dynamic-SQL allowlist sites (bandit B608 skip).
# Each site builds SET clauses via f-strings; non-allowlisted keys must never
# reach the SQL string. The strongest probes are real column names that are
# NOT in the allowlist — if the allowlist broke, these would move data.
# ---------------------------------------------------------------------------


@pytest.fixture()
def sql_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_BASE_URL", "http://testserver")
    for module in (config, db, tenant, vocab, listings, clients, studio, invoices, usage):
        importlib.reload(module)
    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")
    yield
    tenant.set_studio("default")


def _seed_listing() -> int:
    return db.run("INSERT INTO listings (studio_id, title) VALUES ('default', '1 Main St')")


def _seed_client() -> int:
    return db.run(
        "INSERT INTO clients (studio_id, name) VALUES ('default', 'Agent A')",
    )


def _seed_invoice(listing_id: int) -> int:
    return db.run(
        """INSERT INTO invoices (studio_id, listing_id, slug, title, amount_cents, status,
                                 line_items, invoice_kind, currency)
           VALUES ('default', ?, 'inv-guard', 'Shoot fee', 10000, 'sent', '[]', 'full', 'usd')""",
        (listing_id,),
    )


def test_update_listing_rejects_non_allowlisted_columns(sql_env):
    listing_id = _seed_listing()
    before = db.one("SELECT * FROM listings WHERE id=?", (listing_id,))

    listings.update_listing(
        listing_id,
        title="2 Oak Ave",
        studio_id="other-studio",
        id=99999,
        created_at="1999-01-01 00:00:00",
        delivered_at="1999-01-01 00:00:00",
        **{"status=?; DROP TABLE listings--": "x"},
    )

    row = db.one("SELECT * FROM listings WHERE id=?", (listing_id,))
    assert row["title"] == "2 Oak Ave"
    for column in ("studio_id", "id", "created_at", "delivered_at"):
        assert row[column] == before[column]
    assert db.one("SELECT COUNT(*) AS n FROM listings")["n"] == 1


def test_update_client_rejects_non_allowlisted_columns(sql_env):
    client_id = _seed_client()
    before = db.one("SELECT * FROM clients WHERE id=?", (client_id,))

    clients.update_client(
        client_id,
        company="Brokerage Co",
        studio_id="other-studio",
        id=4242,
        created_at="1999-01-01 00:00:00",
        **{"portal_token": "forged-token"},
    )

    row = db.one("SELECT * FROM clients WHERE id=?", (client_id,))
    assert row["company"] == "Brokerage Co"
    assert row["studio_id"] == before["studio_id"]
    assert row["id"] == client_id
    assert row["created_at"] == before["created_at"]
    assert row["portal_token"] == before["portal_token"]


def test_update_studio_rejects_non_allowlisted_columns(sql_env):
    before = db.one("SELECT * FROM studio WHERE id='default'")

    studio.update_studio(
        name="Renamed Studio",
        slug="hijacked-slug",
        id="other-studio",
        billing_status="active",
        plan_tier="pro",
    )

    row = db.one("SELECT * FROM studio WHERE id='default'")
    assert row["name"] == "Renamed Studio"
    assert row["slug"] == before["slug"]
    assert row["billing_status"] == before["billing_status"]
    assert row["plan_tier"] == before["plan_tier"]


def test_update_profile_rejects_non_allowlisted_columns(sql_env):
    studio.update_profile(headline="New headline", studio_id="other-studio", id=7)

    row = db.one("SELECT * FROM studio_profiles WHERE studio_id='default'")
    assert row["headline"] == "New headline"
    assert db.one("SELECT COUNT(*) AS n FROM studio_profiles")["n"] == 1


def test_update_invoice_rejects_non_allowlisted_columns(sql_env):
    invoice_id = _seed_invoice(_seed_listing())
    before = db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))

    invoices.update_invoice(
        invoice_id,
        notes="updated note",
        slug="hijacked-slug",
        studio_id="other-studio",
        stripe_session_id="cs_forged",
        paid_at="1999-01-01 00:00:00",
    )

    row = db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))
    assert row["notes"] == "updated note"
    for column in ("slug", "studio_id", "stripe_session_id", "paid_at"):
        assert row[column] == before[column]


def test_usage_bump_ignores_non_allowlisted_fields(sql_env):
    usage.bump("listings_created", 2, period="2026-07")
    usage.bump("api_calls", 3, period="2026-07")
    usage.bump("storage_bytes=999999999, api_calls", 1, period="2026-07")
    usage.bump("1=1; DROP TABLE studio_usage--", 1, period="2026-07")

    row = db.one(
        "SELECT * FROM studio_usage WHERE studio_id='default' AND period='2026-07'",
    )
    assert row["listings_created"] == 2
    assert row["api_calls"] == 3
    assert row["storage_bytes"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM studio_usage")["n"] == 1
