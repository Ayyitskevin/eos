"""Auto-rendered slideshow / reel videos from gallery photos (ffmpeg)."""

import logging
import shutil
import subprocess
from pathlib import Path

from fastapi import HTTPException

from . import config, db
from .vocab import STUDIO_ID

log = logging.getLogger("eos.video_render")

FORMATS: dict[str, tuple[int, int]] = {
    "slideshow": (1920, 1080),  # 16:9 landscape
    "reel": (1080, 1920),  # 9:16 vertical for Instagram/TikTok
}
PHOTO_SECONDS = 3.0
XFADE_SECONDS = 0.5
FPS = 30
CRF = "20"
RENDER_TIMEOUT_SECONDS = 600


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def enabled() -> bool:
    return config.VIDEO_RENDER_ENABLED and ffmpeg_path() is not None


def get_render(gallery_id: int, fmt: str):
    if fmt not in FORMATS:
        return None
    return db.one(
        "SELECT * FROM gallery_video_renders WHERE gallery_id=? AND format=? AND studio_id=?",
        (gallery_id, fmt, STUDIO_ID),
    )


def list_for_gallery(gallery_id: int) -> dict:
    rows = db.all_(
        "SELECT * FROM gallery_video_renders WHERE gallery_id=? AND studio_id=?",
        (gallery_id, STUDIO_ID),
    )
    return {row["format"]: dict(row) for row in rows}


def ready_formats(gallery_id: int) -> set[str]:
    rows = db.all_(
        """SELECT format FROM gallery_video_renders
           WHERE gallery_id=? AND studio_id=? AND status='ready'""",
        (gallery_id, STUDIO_ID),
    )
    return {row["format"] for row in rows}


def galleries_with_ready_videos(gallery_ids: list[int]) -> set[int]:
    ids = [int(gid) for gid in dict.fromkeys(gallery_ids) if gid]
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    rows = db.all_(
        f"""SELECT DISTINCT gallery_id FROM gallery_video_renders
            WHERE studio_id=? AND status='ready' AND gallery_id IN ({placeholders})""",
        (STUDIO_ID, *ids),
    )
    return {row["gallery_id"] for row in rows}


def render_path(gallery_id: int, fmt: str) -> Path:
    from . import media_paths

    return media_paths.gallery_subdir(gallery_id, "renders") / f"{fmt}.mp4"


def ready_file(gallery_id: int, fmt: str) -> Path | None:
    row = get_render(gallery_id, fmt)
    if not row or row["status"] != "ready":
        return None
    path = Path(row["file_path"]) if row["file_path"] else render_path(gallery_id, fmt)
    return path if path.is_file() else None


def request_render(gallery_id: int, fmt: str) -> bool:
    """Enqueue a gallery video render. Returns False when one is already active."""
    from . import galleries, jobs

    if fmt not in FORMATS:
        raise HTTPException(status_code=400, detail="invalid video format")
    galleries.get_gallery(gallery_id)
    if not enabled():
        raise HTTPException(
            status_code=409,
            detail="video rendering is unavailable (ffmpeg not found on PATH)",
        )
    row = db.one(
        """SELECT COUNT(*) AS n FROM assets
           WHERE gallery_id=? AND kind='photo' AND status='ready'""",
        (gallery_id,),
    )
    if not row or not row["n"]:
        raise HTTPException(status_code=409, detail="no ready photos to render")
    with db.tx(immediate=True) as con:
        existing = con.execute(
            """SELECT status FROM gallery_video_renders
               WHERE gallery_id=? AND format=? AND studio_id=?""",
            (gallery_id, fmt, str(STUDIO_ID)),
        ).fetchone()
        if existing and existing["status"] in ("queued", "rendering"):
            return False
        con.execute(
            """INSERT INTO gallery_video_renders
               (studio_id, gallery_id, format, status, updated_at)
               VALUES (?,?,?,?,datetime('now'))
               ON CONFLICT(gallery_id, format) DO UPDATE SET
                 status='queued', job_id=NULL, error='', updated_at=datetime('now')""",
            (str(STUDIO_ID), gallery_id, fmt, "queued"),
        )
    job_id = jobs.enqueue("gallery_video_render", {"gallery_id": gallery_id, "format": fmt})
    db.run(
        """UPDATE gallery_video_renders SET job_id=?, updated_at=datetime('now')
           WHERE gallery_id=? AND format=? AND studio_id=?""",
        (job_id, gallery_id, fmt, str(STUDIO_ID)),
    )
    db.audit("admin", "gallery.video_render", f"gallery={gallery_id} format={fmt}")
    return True


def build_ffmpeg_command(ffmpeg: str, inputs: list[Path], out: Path, fmt: str) -> list[str]:
    """Per-image loop + xfade crossfades, scale/crop to fill, H.264 + faststart."""
    w, h = FORMATS[fmt]
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for src in inputs:
        cmd += ["-loop", "1", "-t", f"{PHOTO_SECONDS:g}", "-i", str(src)]
    filters = []
    for i in range(len(inputs)):
        filters.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},fps={FPS},format=yuv420p,setsar=1[v{i}]"
        )
    if len(inputs) == 1:
        last = "v0"
    else:
        step = PHOTO_SECONDS - XFADE_SECONDS
        prev = "v0"
        for i in range(1, len(inputs)):
            label = f"x{i}"
            filters.append(
                f"[{prev}][v{i}]xfade=transition=fade:"
                f"duration={XFADE_SECONDS:g}:offset={step * i:g}[{label}]"
            )
            prev = label
        last = prev
    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        f"[{last}]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        CRF,
        "-movflags",
        "+faststart",
        "-an",
        str(out),
    ]
    return cmd


def _run_ffmpeg(cmd: list[str]) -> None:
    """Isolated for tests — the only place ffmpeg is invoked."""
    subprocess.run(cmd, check=True, capture_output=True, timeout=RENDER_TIMEOUT_SECONDS)


def _mark(gallery_id: int, fmt: str, status: str, *, file_path: str = "", error: str = "") -> None:
    db.run(
        """UPDATE gallery_video_renders
           SET status=?, file_path=CASE WHEN ?='' THEN file_path ELSE ? END,
               error=?, updated_at=datetime('now')
           WHERE gallery_id=? AND format=? AND studio_id=?""",
        (status, file_path, file_path, error[:500], gallery_id, fmt, str(STUDIO_ID)),
    )


def mark_failed(gallery_id: int, fmt: str, error: str) -> None:
    _mark(gallery_id, fmt, "failed", error=error)


def render_gallery_video(gallery_id: int, fmt: str) -> None:
    """Job handler body — runs with the tenant already bound by jobs._execute."""
    if fmt not in FORMATS:
        raise RuntimeError(f"unknown video format {fmt!r}")
    if not get_render(gallery_id, fmt):
        raise RuntimeError("render record missing")
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    _mark(gallery_id, fmt, "rendering")
    assets = db.all_(
        """SELECT stored FROM assets
           WHERE gallery_id=? AND kind='photo' AND status='ready'
           ORDER BY section_id, position, id LIMIT ?""",
        (gallery_id, config.VIDEO_MAX_PHOTOS),
    )
    from . import media_paths

    base = media_paths.gallery_dir(gallery_id)
    inputs: list[Path] = []
    for asset in assets:
        # Web derivatives are already orientation-baked and sRGB-normalized.
        web = base / "web" / f"{Path(asset['stored']).stem}.jpg"
        src = web if web.is_file() else base / "original" / asset["stored"]
        if src.is_file():
            inputs.append(src)
    if not inputs:
        raise RuntimeError("no ready photo files on disk")
    final = render_path(gallery_id, fmt)
    tmp = final.with_name(f"{final.stem}.part.mp4")
    try:
        _run_ffmpeg(build_ffmpeg_command(ffmpeg, inputs, tmp, fmt))
        if not tmp.is_file():
            raise RuntimeError("ffmpeg produced no output")
        tmp.rename(final)
    finally:
        tmp.unlink(missing_ok=True)
    _mark(gallery_id, fmt, "ready", file_path=str(final))
    from . import object_store, tenant

    object_store.sync_gallery_file(
        final, studio_id=tenant.get_studio_id(), gallery_id=gallery_id, sub="renders"
    )
