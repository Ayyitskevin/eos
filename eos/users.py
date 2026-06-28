"""Studio operator accounts — scrypt passwords, owner/operator roles."""

import hashlib
import logging
import secrets

from fastapi import HTTPException

from . import db
from .vocab import STUDIO_ID

log = logging.getLogger("eos.users")

_SCRYPT_N = 2**14


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=_SCRYPT_N, r=8, p=1)
    return f"scrypt${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, hexdigest = stored.split("$", 2)
        digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=_SCRYPT_N, r=8, p=1)
        return secrets.compare_digest(digest.hex(), hexdigest)
    except (ValueError, TypeError):
        return False


def get_user(user_id: int):
    row = db.one(
        "SELECT * FROM users WHERE id=? AND studio_id=? AND active=1", (user_id, STUDIO_ID)
    )
    if not row:
        raise HTTPException(status_code=404)
    return row


def get_by_email(email: str, *, studio_id: str | None = None):
    email = email.strip().lower()
    if studio_id:
        return db.one(
            "SELECT * FROM users WHERE email=? AND studio_id=? AND active=1",
            (email, studio_id),
        )
    return db.one(
        "SELECT * FROM users WHERE email=? AND active=1 ORDER BY id LIMIT 1",
        (email,),
    )


def list_users():
    return db.all_(
        "SELECT id, email, name, role, created_at FROM users WHERE studio_id=? ORDER BY email",
        (str(STUDIO_ID),),
    )


def create_user(
    email: str,
    password: str,
    *,
    name: str = "",
    role: str = "operator",
    studio_id: str | None = None,
) -> int:
    email = email.strip().lower()
    if not email or not password:
        raise HTTPException(status_code=400, detail="email and password required")
    if role not in ("owner", "operator", "scheduler", "editor", "accountant"):
        raise HTTPException(status_code=400, detail="invalid role")
    sid = studio_id or str(STUDIO_ID)
    uid = db.run(
        """INSERT INTO users (studio_id, email, password_hash, name, role)
           VALUES (?,?,?,?,?)""",
        (sid, email, hash_password(password), name.strip(), role),
    )
    db.audit("admin", "user.create", f"id={uid} email={email}")
    return uid


def authenticate(email: str, password: str, *, studio_id: str | None = None):
    user = get_by_email(email, studio_id=studio_id)
    if not user or not verify_password(password, user["password_hash"]):
        return None
    return user


def bootstrap_owner(email: str, password: str) -> None:
    if get_by_email(email):
        return
    create_user(email, password, name="Owner", role="owner")
    log.info("bootstrapped owner user %s", email)
