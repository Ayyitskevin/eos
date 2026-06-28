"""Eos configuration — env-driven, .env loaded if present."""

import os
from pathlib import Path

_ENV_FILE = os.environ.get("EOS_ENV_FILE", "")


def _load_env_file(path: str) -> None:
    p = Path(path)
    if not p.is_file():
        return
    for raw_line in p.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


if _ENV_FILE:
    _load_env_file(_ENV_FILE)
else:
    for candidate in (Path("/opt/eos/.env"), Path(__file__).resolve().parent.parent / ".env"):
        _load_env_file(str(candidate))


def _b(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


APP_VERSION = "1.5.0"

HOST = os.environ.get("EOS_HOST", "127.0.0.1")
PORT = int(os.environ.get("EOS_PORT", "8410"))
BASE_URL = os.environ.get("EOS_BASE_URL", f"http://localhost:{PORT}")

_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("EOS_DATA_DIR", str(_ROOT / "data")))
DB_PATH = DATA_DIR / "eos.db"
MEDIA_DIR = DATA_DIR / "media"
ZIP_DIR = DATA_DIR / "zips"
BRAND_DIR = DATA_DIR / "brand"

SECRET_KEY = os.environ.get("EOS_SECRET_KEY", "")
ADMIN_PASSWORD = os.environ.get("EOS_ADMIN_PASSWORD", "")

SITE_NAME = os.environ.get("EOS_SITE_NAME", "Eos Photography")
CONTACT_EMAIL = os.environ.get("EOS_CONTACT_EMAIL", "")
TIMEZONE = os.environ.get("EOS_TIMEZONE", "America/New_York")

STRIPE_SECRET_KEY = os.environ.get("EOS_STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("EOS_STRIPE_WEBHOOK_SECRET", "")

GMAIL_USER = os.environ.get("EOS_GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("EOS_GMAIL_APP_PASSWORD", "")
CONTACT_REPLY_TO = os.environ.get("EOS_CONTACT_REPLY_TO", CONTACT_EMAIL)

DEFAULT_TURNAROUND_HOURS = int(os.environ.get("EOS_DEFAULT_TURNAROUND_HOURS", "24"))
PIN_MAX_FAILS = int(os.environ.get("EOS_PIN_MAX_FAILS", "5"))
PIN_LOCKOUT_MIN = int(os.environ.get("EOS_PIN_LOCKOUT_MIN", "15"))
SESSION_MAX_AGE = int(os.environ.get("EOS_SESSION_MAX_AGE", str(60 * 60 * 24 * 90)))
COOKIE_SECURE = _b("EOS_COOKIE_SECURE", "false")

WEB_MAX_PX = int(os.environ.get("EOS_WEB_MAX_PX", "2400"))
THUMB_MAX_PX = int(os.environ.get("EOS_THUMB_MAX_PX", "480"))
JPEG_QUALITY = int(os.environ.get("EOS_JPEG_QUALITY", "88"))
JOB_WORKERS = int(os.environ.get("EOS_JOB_WORKERS", "2"))
MIN_FREE_GB = float(os.environ.get("EOS_MIN_FREE_GB", "2"))

SAAS_MODE = _b("EOS_SAAS_MODE", "false")
SIGNUP_ENABLED = _b("EOS_SIGNUP_ENABLED", "false") or SAAS_MODE
BASE_DOMAIN = os.environ.get("EOS_BASE_DOMAIN", "")
BOOTSTRAP_EMAIL = os.environ.get("EOS_BOOTSTRAP_EMAIL", "")
SEQUENCE_TICK_SECONDS = int(os.environ.get("EOS_SEQUENCE_TICK_SECONDS", "60"))
INTEGRATION_TICK_SECONDS = int(os.environ.get("EOS_INTEGRATION_TICK_SECONDS", "120"))

GOOGLE_CLIENT_ID = os.environ.get("EOS_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("EOS_GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("EOS_GOOGLE_REDIRECT_URI", "")
GOOGLE_ADMIN_REDIRECT_URI = os.environ.get(
    "EOS_GOOGLE_ADMIN_REDIRECT_URI",
    f"http://localhost:{PORT}/oauth/google/admin/callback",
)

SENTRY_DSN = os.environ.get("EOS_SENTRY_DSN", "")
SENTRY_ENVIRONMENT = os.environ.get("EOS_SENTRY_ENVIRONMENT", "production")
PLATFORM_ADMIN_EMAILS = os.environ.get("EOS_PLATFORM_ADMIN_EMAILS", "")
DEMO_ENABLED = _b("EOS_DEMO_ENABLED", "true")

DROPBOX_APP_KEY = os.environ.get("EOS_DROPBOX_APP_KEY", "")
DROPBOX_APP_SECRET = os.environ.get("EOS_DROPBOX_APP_SECRET", "")
DROPBOX_REDIRECT_URI = os.environ.get("EOS_DROPBOX_REDIRECT_URI", "")

STRIPE_PLATFORM_SECRET_KEY = os.environ.get("EOS_STRIPE_PLATFORM_SECRET_KEY", "")
STRIPE_PLATFORM_WEBHOOK_SECRET = os.environ.get("EOS_STRIPE_PLATFORM_WEBHOOK_SECRET", "")
STRIPE_PRICE_STARTER = os.environ.get("EOS_STRIPE_PRICE_STARTER", "")
STRIPE_PRICE_PRO = os.environ.get("EOS_STRIPE_PRICE_PRO", "")

TOKEN_ENCRYPTION_KEY = os.environ.get("EOS_TOKEN_ENCRYPTION_KEY", "") or SECRET_KEY

BILLING_ENFORCE = _b("EOS_BILLING_ENFORCE", "false") or SAAS_MODE
SIGNUP_RATE_LIMIT = int(os.environ.get("EOS_SIGNUP_RATE_LIMIT", "5"))
SIGNUP_RATE_WINDOW_SEC = int(os.environ.get("EOS_SIGNUP_RATE_WINDOW_SEC", "3600"))

TWILIO_ACCOUNT_SID = os.environ.get("EOS_TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("EOS_TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("EOS_TWILIO_FROM_NUMBER", "")


MARKETING_DIR = DATA_DIR / "marketing"


def ensure_dirs() -> None:
    for d in (DATA_DIR, MEDIA_DIR, ZIP_DIR, BRAND_DIR, MARKETING_DIR):
        d.mkdir(parents=True, exist_ok=True)