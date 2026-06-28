"""Foundation smoke tests — migrate, listing spine, RE gallery sections."""

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_healthz(app_env_http):
    transport = ASGITransport(app=app_env_http)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["service"] == "eos"


@pytest.mark.asyncio
async def test_listing_and_gallery_flow(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login",
            data={"password": "test-admin-pass"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        cookie = login.headers["set-cookie"]

        listing = await client.post(
            "/admin/listings",
            data={
                "title": "123 Oak St",
                "address_line1": "123 Oak Street",
                "city": "Austin",
                "state": "TX",
                "zip_code": "78701",
                "mls_id": "MLS-999",
            },
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        assert listing.status_code == 303
        listing_id = int(listing.headers["location"].rstrip("/").split("/")[-1])

        page = await client.get(f"/admin/listings/{listing_id}", headers={"cookie": cookie})
        assert page.status_code == 200
        assert "123 Oak Street" in page.text
        assert "Front elevation" in page.text
        assert "Confirm lockbox" in page.text

        gallery = await client.post(
            f"/admin/listings/{listing_id}/gallery",
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        assert gallery.status_code == 303
        gallery_id = int(gallery.headers["location"].rstrip("/").split("/")[-1])

        gpage = await client.get(f"/admin/galleries/{gallery_id}", headers={"cookie": cookie})
        assert gpage.status_code == 200
        assert "Curb Appeal" in gpage.text
        assert "Kitchen" in gpage.text


@pytest.mark.asyncio
async def test_crop_presets_seeded(app_env):
    import eos.db as db

    db.migrate()
    rows = db.all_("SELECT slug FROM crop_presets WHERE studio_id='default'")
    slugs = {r["slug"] for r in rows}
    assert "mls-3x2" in slugs
    assert "zillow-16x9" in slugs
