"""Prepare the current gallery picks as one MLS archive on the durable worker."""

import hashlib
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO

from . import bundles, config, db, galleries, imaging, jobs, media_paths
from .vocab import STUDIO_ID


class PicksUnavailable(ValueError):
    """A public explanation of why the complete selection is unavailable."""


@dataclass
class Selection:
    members: list[tuple[Path, str]]
    preset: dict
    options: dict
    signature: str


def _version(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not info.st_size:
        raise PicksUnavailable("An image needed for the MLS picks is unavailable.")
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def selection(gallery) -> Selection:
    gid = gallery["id"]
    assets = galleries.ready_photo_picks(gid)
    if not assets:
        raise PicksUnavailable("Star at least one ready photo to download MLS picks.")
    preset = bundles.preset_for_kind("mls")
    if not preset:
        raise PicksUnavailable("The studio's MLS Standard export preset is unavailable.")
    preset = dict(preset)
    options = jobs.export_options(gallery["listing_id"])
    original = media_paths.gallery_dir(gid) / "original"
    members = []
    # Include the original and logo versions as well as the visible crop settings.
    # Replacing a damaged original must also release a previously failed preparation.
    inputs = [str(STUDIO_ID), gid, preset, config.JPEG_QUALITY, options]
    try:
        if preset["brand_overlay"] and options["overlay"]:
            inputs.append(_version(Path(options["overlay"]["path"])))
        for asset in assets:
            stored = asset["stored"]
            if Path(stored).name != stored or stored in {"", ".", ".."}:
                raise PicksUnavailable("A picked photo's original is unavailable.")
            source = original / stored
            stem = Path(asset["filename"].replace("\\", "/")).stem
            stem = re.sub(r"[^\w .-]", "_", stem).strip(" .") or "photo"
            prefix = f"{asset['id']}-"
            # Leave room for the ID and extension within a 255-byte filesystem name.
            stem_bytes = 255 - len(prefix) - len(".jpg")
            stem = stem.encode("utf-8")[:stem_bytes].decode("utf-8", errors="ignore")
            member = f"{prefix}{stem}.jpg"
            members.append((source, member))
            inputs.append((stored, member, _version(source)))
    except OSError:
        raise PicksUnavailable(
            "A source image is unavailable. Contact the studio for this download."
        )
    signature = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return Selection(members, preset, options, signature)


def _archive_path(gallery_id: int) -> Path:
    return config.ZIP_DIR / f"g{gallery_id}-mls-picks.zip"


def open_ready(gallery_id: int, signature: str) -> BinaryIO | None:
    """Pin the exact file we validate so a concurrent replacement cannot change the download."""
    try:
        stream = _archive_path(gallery_id).open("rb")
    except FileNotFoundError:
        return None
    try:
        with zipfile.ZipFile(stream) as archive:
            matches = archive.comment == signature.encode()
        if matches:
            stream.seek(0)
            return stream
    except zipfile.BadZipFile:
        pass  # A fresh job can replace a damaged archive.
    stream.close()
    return None


def request_build(gallery_id: int, signature: str) -> None:
    """Coalesce all visitors onto one job per gallery, including after a change of picks."""
    key = f"gallery:{gallery_id}:mls-picks"
    payload = {"gallery_id": gallery_id, "signature": signature}
    with db.tx(immediate=True) as con:
        job = db.one(
            """SELECT id, status, payload FROM jobs
               WHERE studio_id=? AND kind='mls_picks' AND idempotency_key=?""",
            (STUDIO_ID, key),
        )
        if job:
            if job["status"] in ("queued", "running"):
                return
            if job["status"] == "failed" and json.loads(job["payload"]) == payload:
                raise PicksUnavailable("The MLS picks could not be prepared. Contact the studio.")
            con.execute(
                """UPDATE jobs SET status='queued', attempts=0, error=NULL, payload=?,
                          updated_at=datetime('now') WHERE id=? AND studio_id=?""",
                (json.dumps(payload), job["id"], STUDIO_ID),
            )
        jobs.enqueue("mls_picks", payload, idempotency_key=key)


def build(gallery_id: int, signature: str) -> None:
    gallery = galleries.get_gallery(gallery_id)
    try:
        picks = selection(gallery)
    except PicksUnavailable:
        return  # No longer eligible. The next request explains the current state.
    if picks.signature != signature:
        return  # The requesting selection has already changed.
    with TemporaryDirectory(dir=config.ZIP_DIR) as tmp:
        scratch = Path(tmp)
        archive_path = scratch / "picks.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
            for source, member in picks.members:
                (crop_name,) = imaging.make_crops(
                    str(source),
                    scratch,
                    "pick",
                    config.JPEG_QUALITY,
                    [picks.preset],
                    **picks.options,
                )
                crop = scratch / crop_name
                archive.write(crop, arcname=member)
                crop.unlink()
            archive.comment = signature.encode()
        archive_path.chmod(0o600)
        archive_path.replace(_archive_path(gallery_id))
