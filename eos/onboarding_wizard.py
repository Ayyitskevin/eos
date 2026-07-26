"""Post-signup onboarding checklist."""

from fastapi import HTTPException

from . import db, studio, tenant
from .vocab import STUDIO_ID

STEPS = (
    ("profile", "Studio profile & service area"),
    ("packages", "Review service packages"),
    ("stripe", "Online payments (optional in offline mode)"),
    ("publish", "Publish site & enable booking"),
    ("billing", "Subscribe to a platform plan"),
)


def status() -> dict:
    profile = studio.get_profile()
    step = int(profile["onboarding_step"] or 0)
    from . import platform_billing, stripe_connect

    studio_row = studio.get_studio()
    payments_ready = stripe_connect.payments_ready()
    public = tenant.public_booking_readiness()
    billing_ready = platform_billing.studio_billing().get("billing_status") in (
        "active",
        "trialing",
    )
    checks = {
        "profile": bool(profile["service_area"] and profile["headline"]),
        "packages": bool(studio.list_packages(active_only=True)),
        "stripe": True,
        "publish": bool(profile["published"] and profile["booking_enabled"]),
        "billing": billing_ready,
    }
    core_ready = bool(public["ready"] and checks["packages"] and billing_ready)
    base = tenant.get_base_url()
    steps = [
        {
            "id": key,
            "label": label,
            "complete": checks.get(key, False),
            "optional": key == "stripe",
        }
        for key, label in STEPS
    ]
    return {
        "done": core_ready,
        "step": step,
        "steps": steps,
        "progress_pct": int(100 * sum(1 for s in STEPS if checks.get(s[0])) / len(STEPS)),
        "booking_url": f"{base}/book" if core_ready else None,
        "payments_mode": "online" if payments_ready else "offline",
        "provisioning_status": studio_row["provisioning_status"],
        "provisioning_error": studio_row["provisioning_error"],
    }


def should_redirect() -> bool:
    if status()["done"]:
        return False
    from . import config

    return config.SIGNUP_ENABLED or config.SAAS_MODE


def advance(*, step: int | None = None, mark_done: bool = False) -> None:
    if mark_done:
        if not status()["done"]:
            raise HTTPException(status_code=409, detail="Complete the required setup checks first.")
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
    advance(step=len(STEPS))


def quick_launch(*, headline: str, service_area: str) -> None:
    headline = headline.strip()
    service_area = service_area.strip()
    if not headline or not service_area:
        raise HTTPException(status_code=400, detail="Headline and service area are required.")
    studio.update_profile(
        headline=headline,
        service_area=service_area,
        published=True,
        booking_enabled=True,
    )
