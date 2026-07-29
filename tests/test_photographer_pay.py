"""Photographer pay integrity: exact cents, date windows, and tenant isolation."""

from __future__ import annotations

import importlib

import eos.config as config
import eos.db as db
import eos.photographer_pay as photographer_pay
import eos.tenant as tenant
import eos.vocab as vocab
import pytest


@pytest.fixture()
def pay_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_BASE_URL", "http://testserver")
    for module in (config, db, tenant, vocab, photographer_pay):
        importlib.reload(module)
    config.ensure_dirs()
    db.migrate()
    tenant.set_studio("default")
    yield
    tenant.set_studio("default")


def _seed_studio(studio_id: str) -> None:
    db.run(
        "INSERT INTO studio (id, name, slug) VALUES (?,?,?)",
        (studio_id, f"{studio_id} Studio", studio_id),
    )


def _seed_user(studio_id: str, email: str, name: str) -> int:
    return db.run(
        "INSERT INTO users (studio_id, email, password_hash, name, role) VALUES (?,?,?,?,?)",
        (studio_id, email, "hash", name, "operator"),
    )


def _seed_listing(
    studio_id: str,
    title: str,
    *,
    pay_cents: int | None,
    photographer_id: int | None = None,
    shoot_date: str | None = None,
    created_at: str | None = None,
) -> int:
    listing_id = db.run(
        """INSERT INTO listings
           (studio_id, title, shoot_date, photographer_pay_cents, assigned_user_id)
           VALUES (?,?,?,?,?)""",
        (studio_id, title, shoot_date, pay_cents, photographer_id),
    )
    if created_at is not None:
        db.run(
            "UPDATE listings SET created_at=? WHERE id=?",
            (created_at, listing_id),
        )
    return listing_id


def test_pay_report_returns_exact_cents_sorted_by_shoot_date(pay_env):
    _seed_studio("pay-studio")
    shooter = _seed_user("pay-studio", "amy@pay.test", "Amy Shooter")
    second = _seed_user("pay-studio", "bo@pay.test", "Bo Lens")
    older = _seed_listing(
        "pay-studio", "11 Old Ln", pay_cents=15000, photographer_id=second, shoot_date="2026-01-10"
    )
    newer = _seed_listing(
        "pay-studio", "22 New Rd", pay_cents=25050, photographer_id=shooter, shoot_date="2026-03-05"
    )
    tenant.set_studio("pay-studio")

    rows = photographer_pay.pay_report(days=90)

    assert [row["id"] for row in rows] == [newer, older]
    first, last = rows
    assert dict(first) == {
        "id": newer,
        "title": "22 New Rd",
        "shoot_date": "2026-03-05",
        "photographer_pay_cents": 25050,
        "status": "lead",
        "photographer_name": "Amy Shooter",
        "photographer_id": shooter,
    }
    assert last["photographer_pay_cents"] == 15000
    assert last["photographer_name"] == "Bo Lens"


def test_pay_report_excludes_zero_null_and_stale_pay(pay_env):
    _seed_studio("pay-studio")
    _seed_listing("pay-studio", "Null Pay", pay_cents=None)
    _seed_listing("pay-studio", "Zero Pay", pay_cents=0)
    _seed_listing("pay-studio", "Negative Pay", pay_cents=-500)
    _seed_listing(
        "pay-studio",
        "Stale Pay",
        pay_cents=9000,
        created_at="2020-01-01 00:00:00",
    )
    current = _seed_listing("pay-studio", "Current Pay", pay_cents=9000)
    tenant.set_studio("pay-studio")

    rows = photographer_pay.pay_report(days=90)

    assert [row["id"] for row in rows] == [current]


def test_pay_report_stale_rows_reappear_with_wider_window(pay_env):
    _seed_studio("pay-studio")
    stale = _seed_listing(
        "pay-studio",
        "Old Shoot",
        pay_cents=7000,
        created_at="2020-06-01 00:00:00",
    )
    tenant.set_studio("pay-studio")

    assert photographer_pay.pay_report(days=30) == []
    rows = photographer_pay.pay_report(days=365 * 10)
    assert [row["id"] for row in rows] == [stale]


def test_totals_by_photographer_aggregates_shoots_and_cents(pay_env):
    _seed_studio("pay-studio")
    amy = _seed_user("pay-studio", "amy@pay.test", "Amy Shooter")
    bo = _seed_user("pay-studio", "bo@pay.test", "Bo Lens")
    _seed_listing("pay-studio", "A1", pay_cents=10000, photographer_id=bo)
    _seed_listing("pay-studio", "A2", pay_cents=25000, photographer_id=amy)
    _seed_listing("pay-studio", "A3", pay_cents=7500, photographer_id=amy)
    _seed_listing("pay-studio", "Unpaid", pay_cents=None, photographer_id=amy)
    _seed_listing(
        "pay-studio",
        "Stale",
        pay_cents=99999,
        photographer_id=bo,
        created_at="2020-01-01 00:00:00",
    )
    tenant.set_studio("pay-studio")

    totals = photographer_pay.totals_by_photographer(days=90)

    assert [dict(row) for row in totals] == [
        {"id": amy, "name": "Amy Shooter", "n_shoots": 2, "total_cents": 32500},
        {"id": bo, "name": "Bo Lens", "n_shoots": 1, "total_cents": 10000},
    ]


def test_totals_by_photographer_skips_unassigned_listings(pay_env):
    _seed_studio("pay-studio")
    _seed_listing("pay-studio", "No Photographer", pay_cents=5000, photographer_id=None)
    tenant.set_studio("pay-studio")

    assert photographer_pay.totals_by_photographer(days=90) == []
    rows = photographer_pay.pay_report(days=90)
    assert len(rows) == 1
    assert rows[0]["photographer_name"] is None
    assert rows[0]["photographer_id"] is None


def test_pay_report_is_isolated_between_studios(pay_env):
    _seed_studio("studio-a")
    _seed_studio("studio-b")
    amy = _seed_user("studio-a", "amy@a.test", "Amy A")
    bo = _seed_user("studio-b", "bo@b.test", "Bo B")
    a_listing = _seed_listing("studio-a", "A Listing", pay_cents=12000, photographer_id=amy)
    _seed_listing("studio-b", "B Listing", pay_cents=34000, photographer_id=bo)

    tenant.set_studio("studio-a")
    rows_a = photographer_pay.pay_report(days=90)
    totals_a = photographer_pay.totals_by_photographer(days=90)

    assert [row["id"] for row in rows_a] == [a_listing]
    assert all(row["title"] != "B Listing" for row in rows_a)
    assert [dict(row) for row in totals_a] == [
        {"id": amy, "name": "Amy A", "n_shoots": 1, "total_cents": 12000},
    ]

    tenant.set_studio("studio-b")
    rows_b = photographer_pay.pay_report(days=90)
    totals_b = photographer_pay.totals_by_photographer(days=90)

    assert len(rows_b) == 1
    assert rows_b[0]["title"] == "B Listing"
    assert rows_b[0]["photographer_pay_cents"] == 34000
    assert [row["id"] for row in totals_b] == [bo]


def test_cross_tenant_user_reference_never_leaks_photographer_name(pay_env):
    """A listing bound to another studio's user id must not expose that user's name."""
    _seed_studio("studio-a")
    _seed_studio("studio-b")
    amy = _seed_user("studio-a", "amy@a.test", "Amy A")
    b_listing = _seed_listing("studio-b", "B Listing", pay_cents=8000, photographer_id=amy)

    tenant.set_studio("studio-b")
    rows = photographer_pay.pay_report(days=90)
    totals = photographer_pay.totals_by_photographer(days=90)

    assert [row["id"] for row in rows] == [b_listing]
    assert rows[0]["photographer_name"] is None
    assert rows[0]["photographer_id"] is None
    assert totals == []
