"""Public gallery selection and MLS JPEG download, using only synthetic originals."""

import io
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

import eos.db as db
import eos.jobs as jobs
import eos.media_paths as media_paths
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

RED = (210, 30, 35)
GREEN = (25, 185, 50)
BLUE = (40, 60, 220)
DOWNLOAD = "/g/interiors/download/mls-picks"


@pytest.fixture()
async def client(app_env_http):
    transport = ASGITransport(app=app_env_http)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client


def _gallery(slug="interiors", listing_id=None):
    return db.run(
        """INSERT INTO galleries (studio_id, listing_id, slug, title, pin, delivery_token, published)
           VALUES ('default', ?, ?, 'Synthetic listing', '1234', ?, 1)""",
        (listing_id, slug, f"token-{slug}"),
    )


def _photo(gid, name, size, color, *, right_color=None):
    stored = f"{uuid4().hex}.png"
    original = media_paths.gallery_subdir(gid, "original", studio_id="default") / stored
    image = Image.new("RGB", size, color)
    if right_color:
        image.paste(right_color, (image.width // 2, 0, image.width, image.height))
    image.save(original)
    return db.run(
        """INSERT INTO assets (gallery_id, filename, stored, status, width, height)
           VALUES (?, ?, ?, 'ready', ?, ?)""",
        (gid, f"{name}.png", stored, *size),
    )


async def _unlock(client, slug="interiors"):
    resp = await client.post(f"/g/{slug}/pin", data={"pin": "1234"})
    assert resp.status_code == 303


async def _set_favorite(client, asset_id, action, slug="interiors"):
    resp = await client.post(f"/g/{slug}/favorite/{asset_id}", data={"action": action})
    assert resp.status_code == 303


async def _download(client):
    """Drive any persisted preparation jobs to completion in the isolated test DB."""
    for _ in range(jobs.MAX_ATTEMPTS + 2):
        response = await client.get(DOWNLOAD)
        if response.status_code != 202:
            return response
        assert int(response.headers["Retry-After"]) > 0
        queued = db.all_("SELECT id FROM jobs WHERE studio_id='default' AND status='queued'")
        assert queued, "Preparation is pending without a queued job"
        for job in queued:
            jobs._execute(job["id"])
    pytest.fail("MLS download still pending after running its preparation jobs")


def _members(response, dimensions):
    assert response.status_code == 200, response.text[:200]
    assert response.headers["Content-Type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        colors = {}
        for member in archive.infolist():
            name = member.filename
            assert name not in colors
            assert Path(name).suffix.lower() in {".jpg", ".jpeg"}
            with Image.open(io.BytesIO(archive.read(member))) as image:
                assert image.format == "JPEG"
                assert image.size == dimensions
                colors[name] = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
        return colors


def _assert_photos(members, expected):
    assert len(members) == len(expected)
    for stem, color in expected.items():
        matches = [rgb for name, rgb in members.items() if stem in name.lower()]
        assert len(matches) == 1, list(members)
        assert matches[0] == pytest.approx(color, abs=6)


class _FavoriteForm(HTMLParser):
    """Capture the submitted controls from one asset's form on an already-loaded page."""

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.active = False
        self.fields = {}
        self.label = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.active = attrs.get("action") == self.url
            if self.active:
                assert attrs.get("method", "get").lower() == "post"
        elif self.active and tag in {"input", "button"}:
            if "name" in attrs:
                self.fields[attrs["name"]] = attrs.get("value", "")
            if tag == "button":
                self.label = attrs.get("aria-label")

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False


@pytest.mark.parametrize("sectioned", [False, True], ids=["unsectioned", "sectioned"])
async def test_two_open_pages_agree_on_mls_picks(client, app_env_http, sectioned):
    gid = _gallery()
    kitchen = _photo(gid, "kitchen", (80, 60), RED)
    porch = _photo(gid, "porch", (60, 80), GREEN)
    if sectioned:
        section = db.run("INSERT INTO sections (gallery_id, name) VALUES (?, 'Interior')", (gid,))
        db.run("UPDATE assets SET section_id=? WHERE id=?", (section, kitchen))
    await _unlock(client)
    await _set_favorite(client, porch, "star")

    async with AsyncClient(
        transport=ASGITransport(app=app_env_http), base_url="https://testserver"
    ) as other:
        await _unlock(other)
        for label, expected in [
            ("Star kitchen.png", {"kitchen": RED, "porch": GREEN}),
            ("Unstar kitchen.png", {"porch": GREEN}),
        ]:
            # Both visitors have already seen the same button before either submits it.
            forms = []
            for visitor in (client, other):
                page = await visitor.get("/g/interiors")
                assert page.status_code == 200
                form = _FavoriteForm(f"/g/interiors/favorite/{kitchen}")
                form.feed(page.text)
                assert form.label == label
                forms.append((visitor, form))

            for visitor, form in forms:
                response = await visitor.post(form.url, data=form.fields)
                assert response.status_code == 303
                _assert_photos(_members(await _download(visitor), (2048, 1365)), expected)


async def test_starred_photos_and_current_preset_round_trip(client):
    gid = _gallery()
    kitchen = _photo(gid, "kitchen", (320, 180), RED)
    porch = _photo(gid, "porch", (100, 225), GREEN)
    den = _photo(gid, "den", (160, 160), BLUE)
    await _unlock(client)
    await _set_favorite(client, kitchen, "star")
    await _set_favorite(client, porch, "star")
    result = await _download(client)
    _assert_photos(_members(result, (2048, 1365)), {"kitchen": RED, "porch": GREEN})

    await _set_favorite(client, porch, "unstar")
    await _set_favorite(client, den, "star")
    result = await _download(client)
    _assert_photos(_members(result, (2048, 1365)), {"kitchen": RED, "den": BLUE})

    await _set_favorite(client, den, "unstar")
    await _set_favorite(client, porch, "star")
    result = await _download(client)
    _assert_photos(_members(result, (2048, 1365)), {"kitchen": RED, "porch": GREEN})

    db.run(
        """UPDATE crop_presets SET width=222, height=148
           WHERE studio_id='default' AND slug='mls-3x2'"""
    )
    result = await _download(client)
    _assert_photos(_members(result, (222, 148)), {"kitchen": RED, "porch": GREEN})


async def test_crop_centering_change_refreshes_download(client):
    gid = _gallery()
    photo = _photo(gid, "panorama", (300, 100), RED, right_color=BLUE)
    await _unlock(client)
    await _set_favorite(client, photo, "star")
    db.run(
        """UPDATE crop_presets SET width=100, height=100, centering_x=0
           WHERE studio_id='default' AND slug='mls-3x2'"""
    )
    _assert_photos(_members(await _download(client), (100, 100)), {"panorama": RED})
    db.run("UPDATE crop_presets SET centering_x=1 WHERE studio_id='default' AND slug='mls-3x2'")
    _assert_photos(_members(await _download(client), (100, 100)), {"panorama": BLUE})


async def test_excludes_other_galleries_videos_and_unready_photos(client):
    listing_id = db.run("INSERT INTO listings (studio_id, title) VALUES ('default', 'Synthetic')")
    gid = _gallery(listing_id=listing_id)
    sibling = _gallery("exteriors", listing_id)
    kitchen = _photo(gid, "kitchen", (40, 20), RED)
    yard = _photo(sibling, "yard", (35, 45), GREEN)
    db.run(
        """INSERT INTO assets (gallery_id, filename, kind, status, agent_favorite)
           VALUES (?, 'walkthrough.mp4', 'video', 'ready', 1),
                  (?, 'foyer.png', 'photo', 'pending', 1),
                  (?, 'stairs.png', 'photo', 'failed', 1)""",
        (gid, gid, gid),
    )
    await _unlock(client, "exteriors")
    await _set_favorite(client, yard, "star", "exteriors")
    await _unlock(client)
    empty = await _download(client)
    assert empty.status_code == 409
    assert any(word in empty.text.lower() for word in ("star", "pick", "select"))

    await _set_favorite(client, kitchen, "star")
    _assert_photos(_members(await _download(client), (2048, 1365)), {"kitchen": RED})
    await _set_favorite(client, kitchen, "unstar")
    assert (await _download(client)).status_code == 409


async def test_favorite_action_requires_gallery_access_and_explicit_intent(client):
    gid = _gallery()
    photo = _photo(gid, "kitchen", (80, 60), RED)
    other_gallery = _gallery("exteriors")
    other_photo = _photo(other_gallery, "porch", (60, 80), GREEN)
    url = f"/g/interiors/favorite/{photo}"

    assert (await client.post(url, data={"action": "star"})).status_code == 403
    await _unlock(client)
    assert (await _download(client)).status_code == 409

    # A legacy page or unknown action cannot reverse an existing selection.
    await _set_favorite(client, photo, "star")
    for fields in [{}, {"action": "toggle"}]:
        response = await client.post(url, data=fields)
        assert 400 <= response.status_code < 500
        assert "refresh" in response.text.lower()
        _assert_photos(_members(await _download(client), (2048, 1365)), {"kitchen": RED})

    response = await client.post(f"/g/interiors/favorite/{other_photo}", data={"action": "star"})
    assert response.status_code == 404
    assert (
        db.one("SELECT agent_favorite FROM assets WHERE id=?", (other_photo,))["agent_favorite"]
        == 0
    )


async def test_duplicate_original_filenames_produce_two_distinct_jpegs(client):
    gid = _gallery()
    first = _photo(gid, "balcony", (38, 30), RED)
    second = _photo(gid, "balcony", (34, 25), BLUE)
    await _unlock(client)
    await _set_favorite(client, first, "star")
    await _set_favorite(client, second, "star")
    members = _members(await _download(client), (2048, 1365))
    assert len(members) == 2
    assert all("balcony" in name.lower() for name in members)
    blue, red = sorted(members.values())
    assert blue == pytest.approx(BLUE, abs=6)
    assert red == pytest.approx(RED, abs=6)


async def test_disabling_mls_preset_prevents_cached_download(client):
    gid = _gallery()
    photo = _photo(gid, "study", (47, 61), GREEN)
    await _unlock(client)
    await _set_favorite(client, photo, "star")
    _assert_photos(_members(await _download(client), (2048, 1365)), {"study": GREEN})
    db.run("UPDATE crop_presets SET active=0 WHERE studio_id='default' AND slug='mls-3x2'")
    result = await _download(client)
    assert result.status_code == 409
    assert "preset" in result.text.lower()


@pytest.mark.parametrize("corrupt", [False, True], ids=["missing", "unreadable"])
async def test_unavailable_source_cannot_produce_partial_download(client, monkeypatch, corrupt):
    gid = _gallery()
    ready = _photo(gid, "kitchen", (40, 30), RED)
    stored = "unavailable.png"
    if corrupt:
        originals = media_paths.gallery_subdir(gid, "original", studio_id="default")
        (originals / stored).write_bytes(b"synthetic bad image")
    unavailable = db.run(
        """INSERT INTO assets (gallery_id, filename, stored, status)
           VALUES (?, 'basement.png', ?, 'ready')""",
        (gid, stored),
    )
    await _unlock(client)
    await _set_favorite(client, ready, "star")
    await _set_favorite(client, unavailable, "star")
    # This test drains queued retries inline. Keep their timers out of later fixtures.
    monkeypatch.setattr(jobs, "_schedule_retry", lambda job_id, attempts: None)
    response = await _download(client)
    assert 400 <= response.status_code < 600
    assert not zipfile.is_zipfile(io.BytesIO(response.content))
    assert any(word in response.text.lower() for word in ("photo", "prepar", "source", "image"))


async def test_long_unicode_photo_name_can_be_extracted(client, tmp_path):
    gid = _gallery()
    stem = "é" * 125 + "a"  # 255 UTF-8 bytes, including the original .png extension.
    photo = _photo(gid, stem, (60, 40), BLUE)
    await _unlock(client)
    await _set_favorite(client, photo, "star")
    result = await _download(client)
    assert result.status_code == 200
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        (name,) = archive.namelist()
        assert "é" in name and name.endswith(".jpg")
        extracted = archive.extract(name, path=tmp_path)
    with Image.open(extracted) as image:
        assert image.size == (2048, 1365)
        assert image.getpixel((100, 100)) == pytest.approx(BLUE, abs=6)
