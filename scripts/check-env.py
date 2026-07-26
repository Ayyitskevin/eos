#!/usr/bin/env python3
"""Validate production-critical env before deploy or boot."""

from __future__ import annotations

import os
import sys

WEAK_SECRETS = {
    "",
    "change-me",
    "change-me-in-production",
    "change-me-strong-password",
    "dev",
    "dogfood-admin",
    "test",
}
WEAK_KEYS = {"", "change-me-in-production", "dev-secret-key-32chars-minimum!!"}


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _warn(msg: str) -> None:
    print(f"WARN: {msg}", file=sys.stderr)


def main() -> int:
    mode = os.environ.get("EOS_CHECK_MODE", "local").lower()
    strict = mode in ("prod", "production")

    secret = os.environ.get("EOS_SECRET_KEY", "")
    admin = os.environ.get("EOS_ADMIN_PASSWORD", "")
    base_url = os.environ.get("EOS_BASE_URL", "")
    saas = os.environ.get("EOS_SAAS_MODE", "").lower() in ("1", "true", "yes")
    signup = saas or os.environ.get("EOS_SIGNUP_ENABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )
    signup_auto_verify = os.environ.get("EOS_SIGNUP_AUTO_VERIFY_LOCAL", "").lower() in (
        "1",
        "true",
        "yes",
    )
    base_domain = os.environ.get("EOS_BASE_DOMAIN", "")
    cookie_secure = os.environ.get("EOS_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
    platform_stripe = os.environ.get("EOS_STRIPE_PLATFORM_SECRET_KEY", "")
    s3_bucket = os.environ.get("EOS_S3_BUCKET", "")

    if len(secret) < 32 or secret in WEAK_KEYS:
        msg = f"EOS_SECRET_KEY is missing or weak (len={len(secret)})"
        (_fail if strict else _warn)(msg)

    if admin in WEAK_SECRETS:
        (_fail if strict else _warn)("EOS_ADMIN_PASSWORD is missing or default")

    if saas and not base_domain:
        _fail("EOS_SAAS_MODE requires EOS_BASE_DOMAIN")

    if strict:
        if not base_url.startswith("https://"):
            _fail("EOS_BASE_URL must be https:// in production")
        if not cookie_secure:
            _fail("EOS_COOKIE_SECURE must be true in production")
        if saas and not os.environ.get("EOS_PLATFORM_ADMIN_EMAILS", "").strip():
            _warn("EOS_PLATFORM_ADMIN_EMAILS not set — no platform super-admin")
        if saas:
            platform_billing = {
                "EOS_STRIPE_PLATFORM_SECRET_KEY": platform_stripe,
                "EOS_STRIPE_PLATFORM_WEBHOOK_SECRET": os.environ.get(
                    "EOS_STRIPE_PLATFORM_WEBHOOK_SECRET", ""
                ),
                "EOS_STRIPE_PRICE_STARTER": os.environ.get("EOS_STRIPE_PRICE_STARTER", ""),
                "EOS_STRIPE_PRICE_PRO": os.environ.get("EOS_STRIPE_PRICE_PRO", ""),
            }
            missing_billing = [key for key, value in platform_billing.items() if not value.strip()]
            if missing_billing:
                _fail(f"hosted SaaS billing requires {', '.join(missing_billing)}")
        if saas and not s3_bucket:
            _warn(
                "EOS_S3_BUCKET not set — media stored on local disk only (not recommended at scale)"
            )
        if signup_auto_verify:
            _fail("EOS_SIGNUP_AUTO_VERIFY_LOCAL must be false in production")
        if signup:
            email_provider = os.environ.get("EOS_EMAIL_PROVIDER", "smtp").lower()
            if email_provider == "postmark":
                email_ready = bool(
                    os.environ.get("EOS_POSTMARK_API_KEY", "").strip()
                    and os.environ.get("EOS_POSTMARK_FROM_EMAIL", "").strip()
                )
                required = "EOS_POSTMARK_API_KEY and EOS_POSTMARK_FROM_EMAIL"
            elif email_provider == "smtp":
                email_ready = bool(
                    os.environ.get("EOS_GMAIL_USER", "").strip()
                    and os.environ.get("EOS_GMAIL_APP_PASSWORD", "").strip()
                )
                required = "EOS_GMAIL_USER and EOS_GMAIL_APP_PASSWORD"
            else:
                _fail("EOS_EMAIL_PROVIDER must be postmark or smtp")
            if not email_ready:
                _fail(f"hosted signup requires {required}")

    print("env check ok", f"(mode={mode}, saas={saas})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
