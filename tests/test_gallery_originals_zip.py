"""Download and extract a full gallery whose filenames collide with a generated suffix."""

import io
import unicodedata
import zipfile

import eos.config as config
import eos.db as db
import eos.jobs as jobs
import eos.media_paths as media_paths
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image


def _gallery():
    return db.run(
        """INSERT INTO galleries (studio_id, slug, title, pin, delivery_token, published, content_rev)
           VALUES ('default', 'interiors', 'Interiors', '1234', 'synthetic-interiors', 1, 7)"""
    )


def _photo(gid, asset_id, filename, color):
    stored = f"photo-{asset_id}.jpg"
    path = media_paths.gallery_subdir(gid, "original", studio_id="default") / stored
    Image.new("RGB", (32, 24), color).save(path)
    db.run(
        """INSERT INTO assets (id, gallery_id, position, filename, stored, status)
           VALUES (?, ?, ?, ?, ?, 'ready')""",
        (asset_id, gid, asset_id, filename, stored),
    )
    return path.read_bytes()


@pytest.mark.parametrize("legacy_cache", [False, True], ids=["fresh", "previously-cached"])
async def test_download_all_extracts_every_original(app_env_http, tmp_path, legacy_cache):
    gid = _gallery()
    originals = [
        _photo(gid, 1, "Kitchen.jpg", "red"),
        _photo(gid, 2, "Kitchen_3.jpg", "green"),
        _photo(gid, 3, "Kitchen.jpg", "blue"),
    ]

    if legacy_cache:
        # Earlier versions gave the third photo the second photo's original filename.
        with zipfile.ZipFile(config.ZIP_DIR / f"g{gid}-r7.zip", "w") as archive:
            archive.writestr("Kitchen.jpg", originals[0])
            archive.writestr("Kitchen_3.jpg", originals[1])
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("Kitchen_3.jpg", originals[2])

    gallery = "/g/interiors"
    download = f"{gallery}/download"
    async with AsyncClient(
        transport=ASGITransport(app=app_env_http), base_url="https://testserver"
    ) as client:
        unlocked = await client.post(f"{gallery}/pin", data={"pin": "1234"})
        assert unlocked.status_code == 303
        page = await client.get(unlocked.headers["location"])
        assert page.status_code == 200
        assert f'href="{download}"' in page.text

        waiting = await client.get(download)
        assert waiting.status_code == 200
        assert f'href="{download}"' in waiting.text
        job = db.one(
            "SELECT id FROM jobs WHERE studio_id='default' AND kind='zip_build' AND status='queued'"
        )
        assert job is not None
        jobs._execute(job["id"])
        landing = await client.get(download)
        assert landing.status_code == 303
        assert landing.headers["location"] == f"{download}/zip"
        response = await client.get(landing.headers["location"])
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/zip"

    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.infolist()) == 3
        archive.extractall(extracted)
    files = {path.name: path.read_bytes() for path in extracted.iterdir()}
    assert len(files) == 3, list(files)
    assert set(files.values()) == set(originals)
    assert files["Kitchen.jpg"] == originals[0]
    assert files["Kitchen_3.jpg"] == originals[1]


def test_zip_names_survive_case_unicode_and_byte_limits_on_rebuild(app_env_http):
    gid = _gallery()
    long_name = "é" * 125 + "x.jpg"  # Exactly 255 UTF-8 bytes before adding a suffix.
    first = _photo(gid, 11, "Café.jpg", "white")
    variant = _photo(gid, 12, "CAFE\u0301.JPG", "black")
    reserved = _photo(gid, 13, "Café_12.jpg", "gray")
    long_first = _photo(gid, 14, long_name, "yellow")
    long_copy = _photo(gid, 15, long_name, "purple")

    def build(rev):
        jobs._h_zip({"gallery_id": gid, "rev": rev})
        with zipfile.ZipFile(jobs.zip_path(gid, rev)) as archive:
            return {archive.read(info): info.filename for info in archive.infolist()}

    names = build(7)
    assert set(names) == {first, variant, reserved, long_first, long_copy}
    assert names[first] == "Café.jpg"
    assert names[reserved] == "Café_12.jpg"
    assert names[long_first] == long_name
    assert len({unicodedata.normalize("NFC", name).casefold() for name in names.values()}) == 5
    assert all(
        name.lower().endswith(".jpg") and len(name.encode()) <= 255 for name in names.values()
    )

    db.run("UPDATE assets SET position=-position WHERE gallery_id=?", (gid,))
    assert build(8) == names
