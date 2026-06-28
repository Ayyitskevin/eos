"""Dogfood seed — one listing through the pipeline."""

import importlib

import eos.config as config
import eos.db as db
import eos.dogfood as dogfood
import eos.tenant as tenant
import pytest


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
