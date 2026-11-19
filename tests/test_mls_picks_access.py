"""The gallery UI and the MLS archive must agree about download access."""

import eos.db as db
import eos.jobs as jobs
import eos.media_paths as media_paths
from httpx import ASGITransport, AsyncClient
from PIL import Image


async def test_mls_picks_link_and_archive_follow_gallery_access(app_env_http):
    listing = db.run("INSERT INTO listings (studio_id, title) VALUES ('default', 'Sample')")
    gallery = db.run(
        """INSERT INTO galleries (studio_id, listing_id, slug, title, pin, delivery_token, published)
           VALUES ('default', ?, 'listing-picks', 'Sample gallery', '1234', 'sample-picks', 1)""",
        (listing,),
    )
    original = media_paths.gallery_subdir(gallery, "original", studio_id="default") / "kitchen.jpg"
    Image.new("RGB", (60, 40), "green").save(original)
    photo = db.run(
        "INSERT INTO assets (gallery_id, filename, stored, kind, status) VALUES (?, 'kitchen.jpg', 'kitchen.jpg', 'photo', 'ready')",
        (gallery,),
    )
    video = db.run(
        "INSERT INTO assets (gallery_id, filename, kind, status) VALUES (?, 'tour.mp4', 'video', 'ready')",
        (gallery,),
    )
    url = "/g/listing-picks"
    download = f"{url}/download/mls-picks"
    async with AsyncClient(
        transport=ASGITransport(app=app_env_http), base_url="https://testserver"
    ) as client:
        assert (await client.get(download)).status_code == 403
        assert (await client.post(f"{url}/pin", data={"pin": "1234"})).status_code == 303
        page = await client.get(url)
        assert page.status_code == 200
        assert f'{url}/favorite/{photo}"' in page.text
        assert f'{url}/favorite/{video}"' not in page.text
        assert download not in page.text

        # Visitors can still clear a video star saved by an earlier version.
        db.run("UPDATE assets SET agent_favorite=1 WHERE id=?", (video,))
        page = await client.get(url)
        assert 'aria-label="Unstar tour.mp4"' in page.text
        assert download not in page.text
        assert (await client.post(f"{url}/favorite/{video}")).status_code == 303
        assert f'{url}/favorite/{video}"' not in (await client.get(url)).text
        assert (await client.post(f"{url}/favorite/{photo}")).status_code == 303
        page = await client.get(url)
        assert f'href="{download}">Download MLS picks</a>' in page.text
        assert "Your MLS picks (1)" in page.text

        invoice = db.run(
            """INSERT INTO invoices (studio_id, listing_id, slug, title, amount_cents, status)
               VALUES ('default', ?, 'sample-invoice', 'Sample invoice', 10000, 'sent')""",
            (listing,),
        )
        assert (await client.get(download)).status_code == 402
        assert download not in (await client.get(url)).text
        db.run("UPDATE invoices SET status='paid' WHERE id=?", (invoice,))
        waiting = await client.get(download)
        assert waiting.status_code == 202
        assert f'href="{download}"' in waiting.text

        job = db.one("SELECT id FROM jobs WHERE studio_id='default' AND kind='mls_picks'")
        jobs._execute(job["id"])
        ready = await client.get(download)
        assert ready.status_code == 200
        assert ready.headers["Content-Disposition"] == 'attachment; filename="mls-picks.zip"'
        assert ready.headers["Cache-Control"] == "private, no-store"
        db.run("UPDATE invoices SET status='sent' WHERE id=?", (invoice,))
        assert (await client.get(download)).status_code == 402
        db.run("UPDATE invoices SET status='paid' WHERE id=?", (invoice,))

        db.run("UPDATE galleries SET published=0 WHERE id=?", (gallery,))
        assert (await client.get(download)).status_code == 404
        db.run("UPDATE galleries SET published=1, expires_at='2000-01-01' WHERE id=?", (gallery,))
        assert (await client.get(download)).status_code == 410

        db.run("INSERT INTO studio (id, name, slug) VALUES ('neighbor', 'Neighbor', 'neighbor')")
        db.run(
            """INSERT INTO galleries (studio_id, slug, title, pin, delivery_token, published)
               VALUES ('neighbor', 'their-picks', 'Other gallery', '1234', 'neighbor-picks', 1)"""
        )
        assert (await client.get("/g/their-picks/download/mls-picks")).status_code == 404
