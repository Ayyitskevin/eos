"""Optional S3/R2 object storage for multi-tenant media at scale."""

from __future__ import annotations

import logging
from pathlib import Path

from . import config

log = logging.getLogger("eos.object_store")

_s3 = None


def enabled() -> bool:
    return bool(config.S3_BUCKET)


def reset_client() -> None:
    global _s3
    _s3 = None


def get_client():
    global _s3
    if _s3 is not None:
        return _s3
    if not enabled():
        return None
    import boto3

    kwargs: dict = {}
    if config.S3_ENDPOINT:
        kwargs["endpoint_url"] = config.S3_ENDPOINT
    if config.S3_ACCESS_KEY and config.S3_SECRET_KEY:
        kwargs["aws_access_key_id"] = config.S3_ACCESS_KEY
        kwargs["aws_secret_access_key"] = config.S3_SECRET_KEY
    if config.S3_REGION:
        kwargs["region_name"] = config.S3_REGION
    _s3 = boto3.client("s3", **kwargs)
    return _s3


def media_key(*, studio_id: str, gallery_id: int, sub: str, filename: str) -> str:
    prefix = config.S3_PREFIX.strip("/")
    parts = [p for p in (prefix, "media", studio_id, str(gallery_id), sub, filename) if p]
    return "/".join(parts)


def upload_file(local_path: Path, key: str) -> bool:
    if not enabled() or not local_path.is_file():
        return False
    try:
        get_client().upload_file(str(local_path), config.S3_BUCKET, key)
        log.debug("s3 upload %s -> %s", local_path, key)
        return True
    except Exception as exc:
        log.warning("s3 upload failed %s: %s", key, exc)
        return False


def sync_gallery_file(
    local_path: Path,
    *,
    studio_id: str,
    gallery_id: int,
    sub: str,
) -> bool:
    if not enabled():
        return False
    return upload_file(
        local_path,
        media_key(
            studio_id=studio_id,
            gallery_id=gallery_id,
            sub=sub,
            filename=local_path.name,
        ),
    )


def studio_storage_bytes(*, studio_id: str) -> int:
    if not enabled():
        return 0
    prefix = config.S3_PREFIX.strip("/")
    base = "/".join(p for p in (prefix, "media", studio_id) if p) + "/"
    total = 0
    try:
        paginator = get_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=config.S3_BUCKET, Prefix=base):
            for obj in page.get("Contents") or []:
                total += int(obj.get("Size") or 0)
    except Exception as exc:
        log.warning("s3 list failed studio=%s: %s", studio_id, exc)
    return total
