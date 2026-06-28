"""Plan-tier feature limits — starter vs pro gating."""

from fastapi import HTTPException

from . import db
from .vocab import STUDIO_ID

LIMITS: dict[str, dict] = {
    "solo": {
        "listings_month": None,
        "api_tokens": 5,
        "webhooks": 5,
        "custom_domain": True,
        "label": "Solo",
    },
    "trial": {
        "listings_month": 15,
        "api_tokens": 1,
        "webhooks": 1,
        "custom_domain": False,
        "label": "Trial",
    },
    "starter": {
        "listings_month": 30,
        "api_tokens": 2,
        "webhooks": 2,
        "custom_domain": False,
        "label": "Starter",
    },
    "pro": {
        "listings_month": None,
        "api_tokens": 10,
        "webhooks": 10,
        "custom_domain": True,
        "label": "Pro",
    },
}


def current_tier() -> str:
    row = db.one("SELECT plan_tier FROM studio WHERE id=?", (STUDIO_ID,))
    tier = (row["plan_tier"] if row else "solo") or "solo"
    return tier if tier in LIMITS else "solo"


def limits_for(tier: str | None = None) -> dict:
    return LIMITS.get(tier or current_tier(), LIMITS["solo"])


def _upgrade_message(feature: str) -> str:
    tier = current_tier()
    if tier in ("trial", "starter"):
        return f"{feature} limit reached on your {limits_for()['label']} plan. Upgrade to Pro in Billing."
    return f"{feature} limit reached."


def check_listing_create(*, current_month_count: int) -> None:
    cap = limits_for()["listings_month"]
    if cap is not None and current_month_count >= cap:
        raise HTTPException(status_code=403, detail=_upgrade_message("Listing"))


def check_api_token(*, current_count: int) -> None:
    cap = limits_for()["api_tokens"]
    if current_count >= cap:
        raise HTTPException(status_code=403, detail=_upgrade_message("API token"))


def check_webhook(*, current_count: int) -> None:
    cap = limits_for()["webhooks"]
    if current_count >= cap:
        raise HTTPException(status_code=403, detail=_upgrade_message("Webhook"))


def check_custom_domain() -> None:
    if not limits_for()["custom_domain"]:
        raise HTTPException(
            status_code=403,
            detail="Custom domains are available on Pro. Upgrade in Billing.",
        )
