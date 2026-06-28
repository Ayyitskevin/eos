"""Production monitoring — optional Sentry, structured health checks."""

import logging
import shutil

from . import config, db, jobs

log = logging.getLogger("eos.monitoring")


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
    ok = free_gb >= config.MIN_FREE_GB
    details = {
        "ok": ok,
        "version": config.APP_VERSION,
        "disk_free_gb": round(free_gb, 2),
        "jobs_pending": pending,
    }
    try:
        db.one("SELECT 1 AS x")
        details["db"] = "ok"
    except Exception as exc:
        details["db"] = "error"
        details["ok"] = False
        details["db_error"] = str(exc)
    return details