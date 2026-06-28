"""Studio-namespaced media paths with legacy fallback."""

from pathlib import Path

from . import config
from .vocab import STUDIO_ID


def gallery_dir(gallery_id: int, *, studio_id: str | None = None) -> Path:
    sid = studio_id or str(STUDIO_ID)
    namespaced = config.MEDIA_DIR / sid / str(gallery_id)
    legacy = config.MEDIA_DIR / str(gallery_id)
    if legacy.exists() and not namespaced.exists():
        return legacy
    return namespaced


def gallery_subdir(gallery_id: int, sub: str, *, studio_id: str | None = None) -> Path:
    path = gallery_dir(gallery_id, studio_id=studio_id) / sub
    path.mkdir(parents=True, exist_ok=True)
    return path


def exports_dir(gallery_id: int) -> Path:
    path = gallery_dir(gallery_id) / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path
