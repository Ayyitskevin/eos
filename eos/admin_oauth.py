"""Durable, tenant-bound Google OAuth login for studio operators."""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass

import httpx

from . import config, db, tenant

log = logging.getLogger("eos.admin_oauth")

PROVIDER = "google_admin"
PURPOSE = "admin_login"
NONCE_COOKIE = "__Host-eos_google_admin_nonce"
NONCE_MAX_AGE = 10 * 60
_SCOPES = "openid email profile"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_STATE_MAX_AGE = NONCE_MAX_AGE
_COMPLETION_MAX_AGE = 2 * 60


@dataclass(frozen=True)
class LoginStart:
    url: str
    browser_nonce: str


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


def is_configured() -> bool:
    return bool(
        config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET and config.GOOGLE_ADMIN_REDIRECT_URI
    )


def can_start_login(*, studio_id: str, host: str, scheme: str) -> bool:
    redirect = urllib.parse.urlsplit(config.GOOGLE_ADMIN_REDIRECT_URI)
    return bool(
        is_configured()
        and config.COOKIE_SECURE
        and scheme == "https"
        and redirect.scheme == "https"
        and callback_host_is_apex(redirect.netloc)
        and valid_login_host(studio_id, host)
    )


def valid_login_host(studio_id: str, raw_host: str | None) -> bool:
    """Require the studio's live canonical subdomain or verified custom domain."""
    host = tenant.normalize_host(raw_host)
    if not host:
        return False
    studio = db.one("SELECT id, slug, active FROM studio WHERE id=?", (studio_id,))
    if not studio or not studio["active"]:
        return False
    custom_studio = tenant.studio_id_for_custom_domain(host)
    if custom_studio:
        return secrets.compare_digest(custom_studio, studio_id)
    subdomain = tenant.subdomain_from_host(host)
    if not subdomain or not secrets.compare_digest(subdomain, studio["slug"]):
        return False
    resolved = tenant.studio_id_for_slug(subdomain)
    return bool(resolved) and secrets.compare_digest(resolved, studio_id)


def callback_host_is_apex(raw_host: str | None) -> bool:
    """The shared callback is accepted only on the configured platform apex."""
    host = tenant.normalize_host(raw_host)
    redirect_host = tenant.normalize_host(
        urllib.parse.urlsplit(config.GOOGLE_ADMIN_REDIRECT_URI).netloc
    )
    apex_host = tenant.configured_base_host()
    return bool(host and redirect_host and apex_host and host == redirect_host == apex_host)


def _prune_flows(con, now: int) -> None:
    con.execute(
        """DELETE FROM google_admin_login_flows
           WHERE (status='initiated' AND state_expires_at<?)
              OR (status='claimed' AND claimed_at<?)
              OR (status='completed' AND completion_expires_at<?)
              OR (status IN ('consumed','failed')
                  AND updated_at<datetime('now', '-7 days'))""",
        (now, now - _STATE_MAX_AGE, now),
    )


def begin_login(*, studio_id: str, host: str, scheme: str = "https") -> LoginStart:
    if not can_start_login(studio_id=studio_id, host=host, scheme=scheme):
        raise ValueError("Google login is unavailable for this host")
    normalized_host = tenant.normalize_host(host)
    if not normalized_host:
        raise ValueError("invalid login host")
    state = secrets.token_urlsafe(32)
    browser_nonce = secrets.token_urlsafe(32)
    now = _now()
    with db.tx(immediate=True) as con:
        _prune_flows(con, now)
        con.execute(
            """INSERT INTO google_admin_login_flows
               (studio_id, purpose, return_host, state_hash, browser_nonce_hash,
                state_expires_at)
               VALUES (?,?,?,?,?,?)""",
            (
                studio_id,
                PURPOSE,
                normalized_host,
                _fingerprint(state),
                _fingerprint(browser_nonce),
                now + _STATE_MAX_AGE,
            ),
        )
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.GOOGLE_ADMIN_REDIRECT_URI,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    }
    return LoginStart(
        url=f"{_AUTH_URL}?{urllib.parse.urlencode(params)}",
        browser_nonce=browser_nonce,
    )


def _claim_state(state: str):
    if not state:
        return None
    now = _now()
    with db.tx(immediate=True) as con:
        row = con.execute(
            """SELECT * FROM google_admin_login_flows
               WHERE state_hash=? AND purpose=?""",
            (_fingerprint(state), PURPOSE),
        ).fetchone()
        if not row:
            return None
        claimed = con.execute(
            """UPDATE google_admin_login_flows
               SET status='claimed', claimed_at=?, updated_at=datetime('now')
               WHERE id=? AND status='initiated' AND state_expires_at>=?""",
            (now, row["id"], now),
        )
        if claimed.rowcount != 1:
            return None
        return dict(row)


def _error_redirect(flow) -> str:
    if flow and valid_login_host(flow["studio_id"], flow["return_host"]):
        return f"https://{flow['return_host']}/admin/login?oauth_error=google"
    return "/admin/login?oauth_error=google"


def _fail_claim(flow_id: int, reason: str) -> None:
    db.run(
        """UPDATE google_admin_login_flows
           SET status='failed', error=?, updated_at=datetime('now')
           WHERE id=? AND status='claimed'""",
        (reason[:500], flow_id),
    )


def _provider_profile(code: str) -> dict:
    with httpx.Client(timeout=15.0) as client:
        token_response = client.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": config.GOOGLE_CLIENT_ID,
                "client_secret": config.GOOGLE_CLIENT_SECRET,
                "redirect_uri": config.GOOGLE_ADMIN_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]
        profile_response = client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        profile_response.raise_for_status()
        return profile_response.json()


def _complete_claim(flow: dict, email: str) -> tuple[int, str] | None:
    completion_token = secrets.token_urlsafe(32)
    now = _now()
    with db.tx(immediate=True) as con:
        live = con.execute(
            """SELECT f.status, f.studio_id, f.return_host, f.purpose,
                      s.active AS studio_active, u.id AS user_id, u.active AS user_active
               FROM google_admin_login_flows f
               JOIN studio s ON s.id=f.studio_id
               LEFT JOIN users u
                 ON u.studio_id=f.studio_id AND lower(u.email)=lower(?)
               WHERE f.id=?""",
            (email, flow["id"]),
        ).fetchone()
        if (
            not live
            or live["status"] != "claimed"
            or live["purpose"] != PURPOSE
            or not live["studio_active"]
            or not live["user_id"]
            or not live["user_active"]
            or not valid_login_host(live["studio_id"], live["return_host"])
        ):
            return None
        completed = con.execute(
            """UPDATE google_admin_login_flows
               SET user_id=?, completion_token_hash=?, completion_expires_at=?,
                   status='completed', completed_at=?, error=NULL,
                   updated_at=datetime('now')
               WHERE id=? AND status='claimed' AND purpose=?""",
            (
                live["user_id"],
                _fingerprint(completion_token),
                now + _COMPLETION_MAX_AGE,
                now,
                flow["id"],
                PURPOSE,
            ),
        )
        if completed.rowcount != 1:
            return None
        return int(live["user_id"]), completion_token


def handle_callback(
    code: str,
    state: str,
    *,
    provider_error: str = "",
) -> dict | None:
    """Claim state, verify Google identity, and mint a tenant-only handoff."""
    flow = _claim_state(state)
    if not flow:
        return None
    redirect_on_error = _error_redirect(flow)
    if provider_error or not code:
        _fail_claim(flow["id"], "provider rejected login")
        return {"ok": False, "redirect_url": redirect_on_error}
    try:
        profile = _provider_profile(code)
        email = (profile.get("email") or "").strip().lower()
        email_verified = profile.get("email_verified") is True
    except Exception:
        log.warning("Google admin login provider exchange failed", exc_info=True)
        _fail_claim(flow["id"], "provider exchange failed")
        return {"ok": False, "redirect_url": redirect_on_error}
    if not email or not email_verified:
        _fail_claim(flow["id"], "provider identity was not verified")
        return {"ok": False, "redirect_url": redirect_on_error}
    completion = _complete_claim(flow, email)
    if not completion:
        _fail_claim(flow["id"], "active tenant operator was not found")
        return {"ok": False, "redirect_url": redirect_on_error}
    user_id, token = completion
    return {
        "ok": True,
        "user_id": user_id,
        "studio_id": flow["studio_id"],
        "redirect_url": (
            f"https://{flow['return_host']}/oauth/google/admin/complete"
            f"#handoff={urllib.parse.quote(token, safe='')}"
        ),
    }


def consume_completion(
    token: str,
    *,
    browser_nonce: str,
    studio_id: str,
    host: str,
) -> int | None:
    """Atomically exchange a tenant/host/nonce-bound completion for one user ID."""
    normalized_host = tenant.normalize_host(host)
    if (
        not token
        or not browser_nonce
        or not normalized_host
        or not valid_login_host(studio_id, normalized_host)
    ):
        return None
    now = _now()
    with db.tx(immediate=True) as con:
        flow = con.execute(
            """SELECT f.*, u.active AS user_active, u.studio_id AS user_studio_id,
                      s.active AS studio_active
               FROM google_admin_login_flows f
               JOIN users u ON u.id=f.user_id
               JOIN studio s ON s.id=f.studio_id
               WHERE f.completion_token_hash=? AND f.purpose=?""",
            (_fingerprint(token), PURPOSE),
        ).fetchone()
        if (
            not flow
            or flow["status"] != "completed"
            or flow["completion_expires_at"] is None
            or flow["completion_expires_at"] < now
            or not flow["user_active"]
            or not flow["studio_active"]
            or flow["studio_id"] != studio_id
            or flow["user_studio_id"] != studio_id
            or flow["return_host"] != normalized_host
            or not secrets.compare_digest(
                flow["browser_nonce_hash"],
                _fingerprint(browser_nonce),
            )
        ):
            return None
        consumed = con.execute(
            """UPDATE google_admin_login_flows
               SET status='consumed', consumed_at=?, updated_at=datetime('now')
               WHERE id=? AND status='completed' AND purpose=?
                 AND completion_expires_at>=?""",
            (now, flow["id"], PURPOSE, now),
        )
        if consumed.rowcount != 1:
            return None
        return int(flow["user_id"])
