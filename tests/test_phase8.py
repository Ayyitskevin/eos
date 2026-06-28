"""Phase 8 — property microsites and marketing kit."""

import importlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

import eos.bundles as bundles
import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.main as main
import eos.marketing_kit as marketing_kit


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EOS_SECRET_KEY", "test-secret-key-32chars-minimum!!")
    monkeypatch.setenv("EOS_ADMIN_PASSWORD", "test-admin-pass")
    for mod in (config, db, jobs, bundles, marketing_kit, main):
        importlib.reload(mod)
    config.ensure_dirs()
    db.migrate()
    jobs.start()
    yield main.app
    jobs.stop()


def _seed_gallery_with_photo(listing_id: int, slug: str = "sitegal") -> int:
    gid = db.run(
        """INSERT INTO galleries (studio_id, listing_id, slug, title, pin, delivery_token, published)
           VALUES ('default', ?, ?, 'Site Test', '4321', 'dt-site', 1)""",
        (listing_id, slug),
    )
    asset_id = db.run(
        """INSERT INTO assets (gallery_id, filename, stored, status, width, height)
           VALUES (?, 'hero.jpg', 'hero.jpg', 'ready', 800, 600)""",
        (gid,),
    )
    media = config.MEDIA_DIR / str(gid)
    for sub in ("original", "web", "thumb"):
        (media / sub).mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (800, 600), color=(70, 110, 150))
    img.save(media / "original" / "hero.jpg", "JPEG")
    img.save(media / "web" / "hero.jpg", "JPEG")
    img.save(media / "thumb" / "hero.jpg", "JPEG")
    db.run("UPDATE galleries SET cover_asset_id=? WHERE id=?", (asset_id, gid))
    return gid


@pytest.mark.asyncio
async def test_microsite_renders_specs_and_gallery(app_env):
    lid = db.run(
        """INSERT INTO listings
           (studio_id, title, site_slug, site_published, address_line1, city, state, zip, beds, baths, sqft, mls_id)
           VALUES ('default', 'Lake House', 'lake-house-8', 1, '123 Lake Rd', 'Austin', 'TX', '78701', 4, 3.5, 2800, 'MLS-99')""",
    )
    _seed_gallery_with_photo(lid)

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/l/lake-house-8")
        assert r.status_code == 200
        assert "123 Lake Rd" in r.text
        assert "2,800" in r.text
        assert "MLS-99" in r.text
        assert "/l/lake-house-8/photo/" in r.text
        assert "og:title" in r.text


@pytest.mark.asyncio
async def test_mls_bundle_download(app_env):
    lid = db.run(
        """INSERT INTO listings (studio_id, title, site_slug, site_published, status)
           VALUES ('default', 'Bundle Test', 'bundle-8', 1, 'delivered')""",
    )
    gid = _seed_gallery_with_photo(lid, "bundlegal")
    exports = config.MEDIA_DIR / str(gid) / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2048, 1365), color=(100, 100, 100)).save(exports / "hero_mls-3x2.jpg", "JPEG")
    path = bundles.build_bundle(lid, "mls")
    assert path and path.is_file()

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/l/bundle-8/download/mls", follow_redirects=False)
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("application/zip")


@pytest.mark.asyncio
async def test_marketing_kit_build(app_env):
    lid = db.run(
        """INSERT INTO listings
           (studio_id, title, site_slug, site_published, address_line1, city, state)
           VALUES ('default', 'Kit Test', 'kit-8', 1, '9 Oak St', 'Dallas', 'TX')""",
    )
    _seed_gallery_with_photo(lid, "kitgal")

    marketing_kit.build_kit(lid)
    status = marketing_kit.get_status(lid)
    assert status["status"] == "ready"
    assert Path(status["ig_square"]).is_file()
    assert Path(status["flyer"]).is_file()

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/l/kit-8/marketing/ig_square")
        assert r.status_code == 200
        assert "image/jpeg" in r.headers.get("content-type", "")