"""Google OAuth login for studio operators."""

import logging
import urllib.parse

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config, db, security, tenant, users

log = logging.getLogger("eos.admin_oauth")

PROVIDER = "google_admin"
_SCOPES = "openid email profile"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_STATE_MAX_AGE = 600


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="eos-google-admin-oauth")


def is_configured() -> bool:
    return bool(
        config.GOOGLE_CLIENT_ID
        and config.GOOGLE_CLIENT_SECRET
        and config.GOOGLE_ADMIN_REDIRECT_URI
    )


def login_url(*, studio_id: str) -> str:
    state = _signer().dumps({"studio_id": studio_id})
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.GOOGLE_ADMIN_REDIRECT_URI,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    }
    return f"{_AUTH_URL}?{urllib.parse.urlencode(params)}"


def handle_callback(code: str, state: str) -> dict | None:
    try:
        payload = _signer().loads(state, max_age=_STATE_MAX_AGE)
    except BadSignature:
        return None
    studio_id = payload.get("studio_id") or "default"
    tenant.set_studio(studio_id)
    with httpx.Client(timeout=15.0) as client:
        tok = client.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": config.GOOGLE_CLIENT_ID,
                "client_secret": config.GOOGLE_CLIENT_SECRET,
                "redirect_uri": config.GOOGLE_ADMIN_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        tok.raise_for_status()
        access = tok.json()["access_token"]
        info = client.get(_USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
        info.raise_for_status()
        profile = info.json()
    email = (profile.get("email") or "").strip().lower()
    if not email or not profile.get("email_verified"):
        return None
    user = users.get_by_email(email, studio_id=studio_id)
    if not user:
        return None
    return {"user_id": user["id"], "studio_id": studio_id, "email": email}