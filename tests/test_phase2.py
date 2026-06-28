"""Phase 2 — uploads, imaging jobs, invoices, appointments."""

import io
import time

import eos.db as db
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image


def _tiny_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color=(200, 180, 160)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_upload_and_derivatives(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]

        lid = db.run(
            "INSERT INTO listings (studio_id, title) VALUES ('default', 'Test')",
        )
        gid = db.run(
            """INSERT INTO galleries (studio_id, listing_id, slug, title, pin, delivery_token)
               VALUES ('default', ?, 'testslug123456', 'Test Gallery', '1234', 'tok')""",
            (lid,),
        )
        db.run(
            "INSERT INTO sections (gallery_id, name, position) VALUES (?, 'Exterior', 0)", (gid,)
        )

        r = await client.post(
            f"/admin/galleries/{gid}/upload",
            files=[("files", ("front.jpg", _tiny_jpeg(), "image/jpeg"))],
            headers={"cookie": cookie},
        )
        assert r.status_code == 200
        assert r.json()["accepted"] == 1

        for _ in range(40):
            asset = db.one("SELECT status FROM assets WHERE gallery_id=?", (gid,))
            if asset and asset["status"] == "ready":
                break
            time.sleep(0.1)
        assert asset["status"] == "ready"

        thumb = await client.get(
            f"/admin/galleries/{gid}/media/thumb/{asset['id'] if 'id' in asset else 1}"
        )
        # asset id is 1 in fresh db
        aid = db.one("SELECT id FROM assets WHERE gallery_id=?", (gid,))["id"]
        thumb = await client.get(
            f"/admin/galleries/{gid}/media/thumb/{aid}", headers={"cookie": cookie}
        )
        assert thumb.status_code == 200


@pytest.mark.asyncio
async def test_invoice_and_appointment(app_env):
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
        )
        cookie = login.headers["set-cookie"]

        lid = db.run(
            "INSERT INTO listings (studio_id, title) VALUES ('default', 'Invoice Test')",
        )

        inv = await client.post(
            f"/admin/listings/{lid}/invoice",
            data={"title": "Shoot fee", "amount_dollars": "199.00"},
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        assert inv.status_code == 303

        appt = await client.post(
            "/admin/appointments",
            data={
                "title": "Twilight shoot",
                "kind": "twilight",
                "starts_at": "2026-07-01T18:30",
                "listing_id": str(lid),
            },
            headers={"cookie": cookie},
            follow_redirects=False,
        )
        assert appt.status_code == 303

        cal = await client.get("/admin/calendar?date=2026-07-01", headers={"cookie": cookie})
        assert cal.status_code == 200
        assert "Twilight shoot" in cal.text
