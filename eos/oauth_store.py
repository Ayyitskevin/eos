"""Per-studio OAuth credential storage."""

from __future__ import annotations

import datetime as dt
import logging

import httpx

from . import db, secret_store
from .vocab import STUDIO_ID

log = logging.getLogger("eos.oauth")


def get_connection(provider: str):
    return db.one(
        "SELECT * FROM studio_oauth WHERE studio_id=? AND provider=?",
        (STUDIO_ID, provider),
    )


def save_tokens(
    provider: str,
    *,
    access_token: str,
    refresh_token: str | None = None,
    expires_in: int | None = None,
    scopes: str = "",
    account_label: str = "",
) -> None:
    expires_at = None
    if expires_in:
        expires_at = (dt.datetime.utcnow() + dt.timedelta(seconds=expires_in)).strftime("%Y-%m-%d %H:%M:%S")
    enc_access = secret_store.encrypt(access_token)
    enc_refresh = secret_store.encrypt(refresh_token) if refresh_token else None
    existing = get_connection(provider)
    if existing:
        db.run(
            """UPDATE studio_oauth SET access_token=?, refresh_token=COALESCE(?, refresh_token),
               expires_at=?, scopes=?, account_label=?, updated_at=datetime('now')
               WHERE studio_id=? AND provider=?""",
            (enc_access, enc_refresh, expires_at, scopes, account_label, STUDIO_ID, provider),
        )
    else:
        db.run(
            """INSERT INTO studio_oauth
               (studio_id, provider, access_token, refresh_token, expires_at, scopes, account_label)
               VALUES (?,?,?,?,?,?,?)""",
            (STUDIO_ID, provider, enc_access, enc_refresh, expires_at, scopes, account_label),
        )


def delete_connection(provider: str) -> None:
    db.run("DELETE FROM studio_oauth WHERE studio_id=? AND provider=?", (STUDIO_ID, provider))


def access_token(provider: str) -> str | None:
    row = get_connection(provider)
    if not row:
        return None
    return secret_store.decrypt(row["access_token"])


def refresh_token(provider: str) -> str | None:
    row = get_connection(provider)
    if not row or not row["refresh_token"]:
        return None
    return secret_store.decrypt(row["refresh_token"])


def set_sync_token(provider: str, sync_token: str | None) -> None:
    db.run(
        "UPDATE studio_oauth SET sync_token=?, updated_at=datetime('now') WHERE studio_id=? AND provider=?",
        (sync_token, STUDIO_ID, provider),
    )


def exchange_code(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    resp = httpx.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access(
    provider: str,
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
) -> str | None:
    rt = refresh_token(provider)
    if not rt:
        return access_token(provider)
    try:
        resp = httpx.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": rt,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.exception("oauth refresh failed for %s/%s", STUDIO_ID, provider)
        return None
    save_tokens(
        provider,
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_in=data.get("expires_in"),
    )
    return data["access_token"]