"""Dogfood seed — one listing through the pipeline."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import eos.config as config
import eos.db as db
import eos.dogfood as dogfood
import eos.tenant as tenant
import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("EOS_BOOTSTRAP_EMAIL", "owner@test.com")
    for mod in (config, db, tenant, dogfood):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    return tmp_path


def test_dogfood_seed_idempotent(app_env):
    tenant.set_studio("default")
    first = dogfood.seed()
    second = dogfood.seed()
    assert first["listing_id"] == second["listing_id"]
    assert first["gallery_pin"] == dogfood.GALLERY_PIN
    assert first["site_url"].endswith("/l/1420-maple-dr")
    assert first["referral_code"] == dogfood.REFERRAL_CODE
    assert first["referral_booking_url"].endswith(f"/book?ref={dogfood.REFERRAL_CODE}")
    assert first["referral_short_url"].endswith(f"/r/{dogfood.REFERRAL_CODE}")

    listing = db.one(
        "SELECT status, site_published FROM listings WHERE id=?", (first["listing_id"],)
    )
    assert listing["status"] == "delivered"
    assert listing["site_published"] == 1

    assets = db.one(
        "SELECT COUNT(*) AS n FROM assets WHERE gallery_id IN (SELECT id FROM galleries WHERE listing_id=?)",
        (first["listing_id"],),
    )
    assert assets["n"] == 6

    code = db.one(
        """SELECT r.code, c.email AS referrer_email
           FROM referral_codes r
           JOIN clients c
             ON c.id=r.referrer_client_id
            AND c.studio_id=r.studio_id
          WHERE r.studio_id='default' AND r.code=?""",
        (dogfood.REFERRAL_CODE,),
    )
    assert code["referrer_email"] == "sarah.chen@kw.com"


def test_migrate_script_bootstraps_fresh_database(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "EOS_ENV_FILE": str(tmp_path / "missing.env"),
            "EOS_DATA_DIR": str(tmp_path / "data"),
            "EOS_SECRET_KEY": "test-secret-key-32chars-minimum!!",
            "EOS_ADMIN_PASSWORD": "test-admin-pass",
        }
    )
    result = subprocess.run(
        [sys.executable, "scripts/migrate.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "schema up to date" not in result.stdout
    assert "applied:" in result.stdout
    assert (tmp_path / "data" / "eos.db").exists()
