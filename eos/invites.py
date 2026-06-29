"""Invite-only signup codes for beta rollout."""

import re
import secrets

from fastapi import HTTPException

from . import config, db

_CODE_RE = re.compile(r"^[A-Za-z0-9-]{4,32}$")


def invite_required() -> bool:
    return config.SIGNUP_INVITE_ONLY


def normalize(code: str) -> str:
    return code.strip().upper()


def validate(code: str) -> None:
    if not invite_required():
        return
    raw = normalize(code)
    if not raw:
        raise HTTPException(status_code=400, detail="Invite code is required.")
    row = db.one(
        """SELECT * FROM invite_codes
           WHERE upper(code)=? AND active=1""",
        (raw,),
    )
    if not row:
        raise HTTPException(status_code=400, detail="Invalid invite code.")
    if row["max_uses"] is not None and row["uses"] >= row["max_uses"]:
        raise HTTPException(status_code=400, detail="This invite code has been fully used.")


def redeem(code: str) -> None:
    if not invite_required():
        return
    raw = normalize(code)
    validate(raw)
    db.run(
        "UPDATE invite_codes SET uses=uses+1 WHERE upper(code)=?",
        (raw,),
    )


def create_code(*, code: str, label: str = "", max_uses: int | None = None) -> str:
    raw = normalize(code) if code.strip() else secrets.token_hex(4).upper()
    if not _CODE_RE.match(raw):
        raise HTTPException(status_code=400, detail="Code must be 4–32 alphanumeric characters.")
    if db.one("SELECT 1 AS x FROM invite_codes WHERE upper(code)=?", (raw,)):
        raise HTTPException(status_code=409, detail="Invite code already exists.")
    db.run(
        "INSERT INTO invite_codes (code, label, max_uses) VALUES (?,?,?)",
        (raw, label.strip(), max_uses),
    )
    return raw


def list_codes() -> list:
    return db.all_(
        """SELECT id, code, label, max_uses, uses, active, created_at
           FROM invite_codes ORDER BY created_at DESC""",
    )


def deactivate(code_id: int) -> None:
    db.run("UPDATE invite_codes SET active=0 WHERE id=?", (code_id,))
