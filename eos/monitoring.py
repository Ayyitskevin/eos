"""Production monitoring — optional Sentry, structured health checks."""

import logging
import os
import shutil

from . import config, db, jobs

log = logging.getLogger("eos.monitoring")


def healthcheck_url() -> str:
    """healthchecks.io-style ping URL (kept here to avoid config.py churn)."""
    return os.environ.get("EOS_HEALTHCHECK_URL", "").strip()


def ping_healthcheck(*, ok: bool, note: str = "") -> None:
    """POST to EOS_HEALTHCHECK_URL (or its /fail suffix) — no-op when unset."""
    url = healthcheck_url()
    if not url:
        return
    target = url if ok else f"{url.rstrip('/')}/fail"
    try:
        import httpx

        httpx.post(target, content=note or ("ok" if ok else "failed"), timeout=10)
    except Exception as exc:
        log.warning("healthcheck ping failed: %s", exc)


def report_health() -> None:
    """Periodic dead-man ping — alerts when jobs are dead-lettered or the app dies."""
    failed = jobs.failed_count()
    ping_healthcheck(ok=failed == 0, note=f"jobs_failed={failed}")


def init() -> None:
    dsn = config.SENTRY_DSN.strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=config.SENTRY_ENVIRONMENT,
            release=f"eos@{config.APP_VERSION}",
            traces_sample_rate=0.1,
            integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        )
        log.info("Sentry initialized (%s)", config.SENTRY_ENVIRONMENT)
    except ImportError:
        log.warning("EOS_SENTRY_DSN set but sentry-sdk not installed")


def health_details() -> dict:
    free_gb = shutil.disk_usage(config.DATA_DIR).free / 1e9
    pending = jobs.pending_count()
    failed = jobs.failed_count()
    ok = free_gb >= config.MIN_FREE_GB and failed == 0
    details = {
        "ok": ok,
        "version": config.APP_VERSION,
        "disk_free_gb": round(free_gb, 2),
        "jobs_pending": pending,
        "jobs_failed": failed,
    }
    try:
        db.one("SELECT 1 AS x")
        details["db"] = "ok"
    except Exception as exc:
        details["db"] = "error"
        details["ok"] = False
        details["db_error"] = str(exc)
    return details
