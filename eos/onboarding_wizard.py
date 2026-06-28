"""Post-signup onboarding checklist."""

from . import db, studio
from .vocab import STUDIO_ID

STEPS = (
    ("profile", "Studio profile & service area"),
    ("packages", "Review service packages"),
    ("calendar", "Connect Google Calendar"),
    ("billing", "Set up platform billing"),
)


def status() -> dict:
    profile = studio.get_profile()
    done = bool(profile["onboarding_done"])
    step = int(profile["onboarding_step"] or 0)
    from . import platform_billing
    from .integrations import google_calendar

    checks = {
        "profile": bool(profile["service_area"] and profile["headline"]),
        "packages": bool(studio.list_packages(active_only=True)),
        "calendar": google_calendar.is_connected(),
        "billing": bool(platform_billing.studio_billing().get("stripe_customer_id")),
    }
    return {
        "done": done,
        "step": step,
        "steps": [{"id": s[0], "label": s[1], "complete": checks.get(s[0], False)} for s in STEPS],
        "progress_pct": int(100 * sum(1 for s in STEPS if checks.get(s[0])) / len(STEPS)),
    }


def should_redirect() -> bool:
    if status()["done"]:
        return False
    from . import config

    return config.SIGNUP_ENABLED or config.SAAS_MODE


def advance(*, step: int | None = None, mark_done: bool = False) -> None:
    if mark_done:
        db.run(
            "UPDATE studio_profiles SET onboarding_done=1, onboarding_step=? WHERE studio_id=?",
            (len(STEPS), STUDIO_ID),
        )
        return
    if step is not None:
        db.run(
            "UPDATE studio_profiles SET onboarding_step=? WHERE studio_id=?",
            (step, STUDIO_ID),
        )


def skip() -> None:
    advance(mark_done=True)
