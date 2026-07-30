"""Auto-rendered gallery slideshow / reel videos (Wave 2, item 6)."""

import shutil
from pathlib import Path

import eos.config as config
import eos.db as db
import eos.galleries as galleries
import eos.jobs as jobs
import eos.media_paths as media_paths
import eos.rbac as rbac
import eos.security as security
import eos.tenant as tenant
import eos.users as users
import eos.video_render as video_render
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from PIL import Image
from starlette.requests import Request


@pytest.fixture(autouse=True)
def _reset_tenant_binding():
    yield
    tenant.set_studio("default")


def _seed_studio(studio_id: str) -> None:
    db.run(
        "INSERT INTO studio (id, name, slug) VALUES (?,?,?)",
        (studio_id, studio_id.title(), studio_id),
    )
    db.run("INSERT INTO studio_profiles (studio_id) VALUES (?)", (studio_id,))


def _seed_gallery(studio: str = "default", n_photos: int = 3, published: bool = False) -> int:
    tenant.set_studio(studio)
    gid = galleries.create_gallery(f"Gallery {studio}")
    orig = media_paths.gallery_subdir(gid, "original")
    web = media_paths.gallery_subdir(gid, "web")
    for i in range(n_photos):
        stored = f"photo{i}.jpg"
        img = Image.new("RGB", (640, 480), (30 + i * 40, 90, 160))
        img.save(orig / stored, "JPEG")
        img.save(web / f"photo{i}.jpg", "JPEG")
        db.run(
            """INSERT INTO assets (gallery_id, kind, filename, stored, status, position)
               VALUES (?, 'photo', ?, ?, 'ready', ?)""",
            (gid, stored, stored, i),
        )
    if published:
        db.run("UPDATE galleries SET published=1 WHERE id=?", (gid,))
    return gid


def _fake_ffmpeg(monkeypatch, captured: list | None = None) -> None:
    """Run the full render path without needing ffmpeg installed."""
    monkeypatch.setattr(video_render, "enabled", lambda: True)
    monkeypatch.setattr(video_render, "ffmpeg_path", lambda: "/fake/ffmpeg")

    def fake_run(cmd: list[str]) -> None:
        if captured is not None:
            captured.append(cmd)
        Path(cmd[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42fake-video")

    monkeypatch.setattr(video_render, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(jobs, "_submit", lambda job_id: None)


def _only_video_job():
    return db.one("SELECT * FROM jobs WHERE kind='gallery_video_render'")


# --- ffmpeg command graph (pure, no ffmpeg needed) ----------------------------


def test_build_ffmpeg_command_shapes():
    out = Path("out.mp4")
    cmd = video_render.build_ffmpeg_command(
        "ffmpeg", [Path("a.jpg"), Path("b.jpg"), Path("c.jpg")], out, "slideshow"
    )
    assert "-movflags" in cmd and "+faststart" in cmd
    assert "-an" in cmd  # no audio
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080" in graph
    assert "xfade=transition=fade:duration=0.5:offset=2.5" in graph
    assert "offset=5" in graph
    assert cmd[cmd.index("-map") + 1] == "[x2]"

    single = video_render.build_ffmpeg_command("ffmpeg", [Path("a.jpg")], out, "reel")
    single_graph = single[single.index("-filter_complex") + 1]
    assert "xfade" not in single_graph
    assert "scale=1080:1920" in single_graph
    assert single[single.index("-map") + 1] == "[v0]"


# --- Job lifecycle -------------------------------------------------------------


def test_render_job_success_writes_record_and_studio_namespaced_file(app_env, monkeypatch):
    captured: list = []
    _fake_ffmpeg(monkeypatch, captured)
    gid = _seed_gallery()

    assert video_render.request_render(gid, "slideshow") is True
    row = video_render.get_render(gid, "slideshow")
    assert row["status"] == "queued"
    assert row["studio_id"] == "default"

    # Replay-safe: a second request does not stack another job.
    assert video_render.request_render(gid, "slideshow") is False
    jobs_rows = db.all_("SELECT * FROM jobs WHERE kind='gallery_video_render'")
    assert len(jobs_rows) == 1

    jobs._execute(jobs_rows[0]["id"])
    row = video_render.get_render(gid, "slideshow")
    assert row["status"] == "ready"
    path = Path(row["file_path"])
    assert path.is_file()
    assert path.parent.name == "renders"
    assert path == media_paths.gallery_dir(gid) / "renders" / "slideshow.mp4"
    assert str(config.MEDIA_DIR / "default") in str(path)
    assert db.one("SELECT status FROM jobs WHERE id=?", (jobs_rows[0]["id"],))["status"] == "done"

    cmd = captured[0]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.count("xfade") == 2  # three photos -> two crossfades
    assert "-movflags" in cmd and "-an" in cmd

    # After success a new request re-renders (row goes back to queued).
    assert video_render.request_render(gid, "slideshow") is True
    assert video_render.get_render(gid, "slideshow")["status"] == "queued"


def test_render_failure_recorded_durably(app_env, monkeypatch):
    monkeypatch.setattr(video_render, "enabled", lambda: True)
    monkeypatch.setattr(video_render, "ffmpeg_path", lambda: "/fake/ffmpeg")

    def boom(cmd: list[str]) -> None:
        raise RuntimeError("ffmpeg boom")

    monkeypatch.setattr(video_render, "_run_ffmpeg", boom)
    monkeypatch.setattr(jobs, "_submit", lambda job_id: None)
    gid = _seed_gallery()

    assert video_render.request_render(gid, "reel") is True
    job = _only_video_job()
    jobs._execute(job["id"])
    row = video_render.get_render(gid, "reel")
    assert row["status"] == "failed"
    assert "ffmpeg boom" in row["error"]
    job_row = db.one("SELECT status, error FROM jobs WHERE id=?", (job["id"],))
    assert job_row["status"] == "queued"  # first failure schedules a retry
    assert "ffmpeg boom" in job_row["error"]

    # Exhaust attempts: the job itself lands in durable failure too.
    jobs._execute(job["id"])
    jobs._execute(job["id"])
    assert db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] == "failed"


def test_request_render_validates_format_enabled_and_photos(app_env, monkeypatch):
    gid = _seed_gallery()
    monkeypatch.setattr(video_render, "enabled", lambda: True)
    with pytest.raises(HTTPException) as exc_info:
        video_render.request_render(gid, "vr360")
    assert exc_info.value.status_code == 400

    monkeypatch.setattr(video_render, "enabled", lambda: False)
    with pytest.raises(HTTPException) as exc_info:
        video_render.request_render(gid, "slideshow")
    assert exc_info.value.status_code == 409

    monkeypatch.setattr(video_render, "enabled", lambda: True)
    empty_gid = galleries.create_gallery("Empty")
    with pytest.raises(HTTPException) as exc_info:
        video_render.request_render(empty_gid, "slideshow")
    assert exc_info.value.status_code == 409


def test_renders_are_scoped_per_studio(app_env, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    _seed_studio("beta")
    gid_default = _seed_gallery("default")
    gid_beta = _seed_gallery("beta")

    tenant.set_studio("default")
    with pytest.raises(HTTPException) as exc_info:
        video_render.request_render(gid_beta, "slideshow")
    assert exc_info.value.status_code == 404

    assert video_render.request_render(gid_default, "slideshow") is True
    assert video_render.get_render(gid_default, "slideshow") is not None

    tenant.set_studio("beta")
    assert video_render.get_render(gid_default, "slideshow") is None
    assert video_render.list_for_gallery(gid_default) == {}
    assert video_render.galleries_with_ready_videos([gid_default]) == set()


# --- Admin routes + RBAC -------------------------------------------------------


async def _admin_client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    login = await client.post(
        "/admin/login", data={"password": "test-admin-pass"}, follow_redirects=False
    )
    assert login.status_code == 303
    return client


@pytest.mark.asyncio
async def test_admin_render_action_status_page_and_download(app_env, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    gid = _seed_gallery()
    client = await _admin_client(app_env)
    try:
        csrf = client.cookies.get(security.CSRF_COOKIE)
        page = await client.get(f"/admin/galleries/{gid}")
        assert page.status_code == 200
        assert "Video renders" in page.text

        r = await client.post(
            f"/admin/galleries/{gid}/video",
            data={"format": "slideshow"},
            headers={"x-eos-csrf": csrf},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert video_render.get_render(gid, "slideshow")["status"] == "queued"

        # Duplicate enqueue prevented at the route level too.
        dup = await client.post(
            f"/admin/galleries/{gid}/video",
            data={"format": "slideshow"},
            headers={"x-eos-csrf": csrf},
            follow_redirects=False,
        )
        assert dup.status_code == 303
        assert len(db.all_("SELECT * FROM jobs WHERE kind='gallery_video_render'")) == 1

        bad = await client.post(
            f"/admin/galleries/{gid}/video",
            data={"format": "vr360"},
            headers={"x-eos-csrf": csrf},
        )
        assert bad.status_code == 400

        before = await client.get(f"/admin/galleries/{gid}/video/slideshow")
        assert before.status_code == 404

        jobs._execute(_only_video_job()["id"])
        after = await client.get(f"/admin/galleries/{gid}/video/slideshow")
        assert after.status_code == 200
        assert after.headers["content-type"] == "video/mp4"

        page = await client.get(f"/admin/galleries/{gid}")
        assert "<video" in page.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_admin_render_action_requires_login(app_env):
    gid = _seed_gallery()
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post(
            f"/admin/galleries/{gid}/video",
            data={"format": "slideshow"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "/admin/login" in r.headers["location"]


def _role_request(user_id: int, method: str, path: str) -> Request:
    name, value = security.set_session_cookie(user_id)
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"cookie", f"{name}={value}".encode())],
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 12345),
        }
    )


def test_render_action_rbac_fail_closed(app_env):
    tenant.set_studio("default")
    editor_id = users.create_user("ed@example.com", "pass12345", role="editor")
    scheduler_id = users.create_user("sched@example.com", "pass12345", role="scheduler")
    rbac.check_route(_role_request(editor_id, "POST", "/admin/galleries/1/video"))
    with pytest.raises(HTTPException) as exc_info:
        rbac.check_route(_role_request(scheduler_id, "POST", "/admin/galleries/1/video"))
    assert exc_info.value.status_code == 403


# --- Public delivery gates ------------------------------------------------------


@pytest.mark.asyncio
async def test_public_video_respects_publication_and_pin_gates(app_env, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    gid = _seed_gallery(published=False)
    g = db.one("SELECT slug, pin FROM galleries WHERE id=?", (gid,))
    slug = g["slug"]
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        unpublished = await client.get(f"/g/{slug}/video/slideshow")
        assert unpublished.status_code == 404

        db.run("UPDATE galleries SET published=1 WHERE id=?", (gid,))
        locked = await client.get(f"/g/{slug}/video/slideshow")
        assert locked.status_code == 403

        pin = await client.post(f"/g/{slug}/pin", data={"pin": g["pin"]}, follow_redirects=False)
        assert pin.status_code == 303
        not_ready = await client.get(f"/g/{slug}/video/slideshow")
        assert not_ready.status_code == 404

        tenant.set_studio("default")
        assert video_render.request_render(gid, "slideshow") is True
        jobs._execute(_only_video_job()["id"])

        ready = await client.get(f"/g/{slug}/video/slideshow")
        assert ready.status_code == 200
        assert ready.headers["content-type"] == "video/mp4"

        page = await client.get(f"/g/{slug}")
        assert page.status_code == 200
        assert "Slideshow" in page.text
        assert f"/g/{slug}/video/slideshow" in page.text

        bad_format = await client.get(f"/g/{slug}/video/vr360")
        assert bad_format.status_code == 404


@pytest.mark.asyncio
async def test_public_video_fails_closed_when_payment_locked(app_env, monkeypatch):
    _fake_ffmpeg(monkeypatch)
    tenant.set_studio("default")
    listing_id = db.run(
        "INSERT INTO listings (studio_id, title, status) VALUES ('default', '1 Paid St', 'delivered')"
    )
    gid = _seed_gallery(published=True)
    db.run("UPDATE galleries SET listing_id=? WHERE id=?", (listing_id, gid))
    db.run(
        """INSERT INTO invoices (studio_id, listing_id, slug, title, amount_cents, status)
           VALUES ('default', ?, 'inv-video-1', 'Shoot', 10000, 'sent')""",
        (listing_id,),
    )
    g = db.one("SELECT slug, pin FROM galleries WHERE id=?", (gid,))
    slug = g["slug"]

    assert video_render.request_render(gid, "slideshow") is True
    jobs._execute(_only_video_job()["id"])

    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post(f"/g/{slug}/pin", data={"pin": g["pin"]}, follow_redirects=False)
        blocked = await client.get(f"/g/{slug}/video/slideshow")
        assert blocked.status_code == 402
        page = await client.get(f"/g/{slug}")
        assert "Slideshow" not in page.text

        # Microsite video is paywalled the same way.
        db.run(
            "UPDATE listings SET site_slug='site-video-paid', site_published=1 WHERE id=?",
            (listing_id,),
        )
        site = await client.get("/l/site-video-paid/video")
        assert site.status_code == 402
        site_page = await client.get("/l/site-video-paid")
        assert "<video" not in site_page.text

    # Paying the invoice opens both surfaces.
    db.run("UPDATE invoices SET status='paid', paid_at=datetime('now') WHERE slug='inv-video-1'")
    transport = ASGITransport(app=app_env)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post(f"/g/{slug}/pin", data={"pin": g["pin"]}, follow_redirects=False)
        opened = await client.get(f"/g/{slug}/video/slideshow")
        assert opened.status_code == 200
        site = await client.get("/l/site-video-paid/video")
        assert site.status_code == 200
        site_page = await client.get("/l/site-video-paid")
        assert "<video" in site_page.text


# --- Real ffmpeg integration ----------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_renders_playable_mp4(app_env, monkeypatch):
    monkeypatch.setattr(jobs, "_submit", lambda job_id: None)
    gid = _seed_gallery(n_photos=2)
    assert video_render.enabled()
    assert video_render.request_render(gid, "slideshow") is True
    jobs._execute(_only_video_job()["id"])

    row = video_render.get_render(gid, "slideshow")
    assert row["status"] == "ready", row["error"]
    data = Path(row["file_path"]).read_bytes()
    assert b"ftyp" in data[:32]
    assert len(data) > 1000
