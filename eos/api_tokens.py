"""Studio API bearer tokens for Zapier / integrations."""

import hashlib
import logging
import secrets

from fastapi import HTTPException, Request

from . import db
from .vocab import STUDIO_ID

log = logging.getLogger("eos.api_tokens")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(*, label: str = "API") -> tuple[int, str]:
    from . import plan_limits

    plan_limits.check_api_token(current_count=len(list_tokens()))
    raw = f"eos_{secrets.token_urlsafe(32)}"
    prefix = raw[:12]
    tid = db.run(
        """INSERT INTO api_tokens (studio_id, label, token_prefix, token_hash)
           VALUES (?,?,?,?)""",
        (str(STUDIO_ID), label.strip() or "API", prefix, _hash(raw)),
    )
    db.audit("admin", "api_token.create", f"id={tid}")
    return tid, raw


def list_tokens():
    return db.all_(
        "SELECT id, label, token_prefix, created_at, last_used_at FROM api_tokens WHERE studio_id=? ORDER BY id DESC",
        (str(STUDIO_ID),),
    )


def revoke_token(token_id: int) -> None:
    db.run("DELETE FROM api_tokens WHERE id=? AND studio_id=?", (token_id, str(STUDIO_ID)))


def authenticate_request(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    raw = auth[7:].strip()
    if not raw:
        raise HTTPException(status_code=401, detail="missing bearer token")
    row = db.one("SELECT studio_id FROM api_tokens WHERE token_hash=?", (_hash(raw),))
    if not row:
        raise HTTPException(status_code=401, detail="invalid token")
    db.run(
        "UPDATE api_tokens SET last_used_at=datetime('now') WHERE token_hash=?",
        (_hash(raw),),
    )
    from . import usage

    usage.bump("api_calls")
    return row["studio_id"]
