"""Per-studio Stripe platform billing — subscriptions for SaaS tenants."""

from __future__ import annotations

import datetime as dt
import logging

import stripe

from . import config, db, tenant
from .vocab import STUDIO_ID

log = logging.getLogger("eos.platform_billing")

PLANS = {
    "starter": {
        "tier": "starter",
        "price_id": lambda: config.STRIPE_PRICE_STARTER,
        "label": "Starter",
    },
    "pro": {"tier": "pro", "price_id": lambda: config.STRIPE_PRICE_PRO, "label": "Pro"},
}

_STATUS_MAP = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "canceled": "canceled",
    "unpaid": "past_due",
    "incomplete": "none",
    "incomplete_expired": "canceled",
}


def is_configured() -> bool:
    return bool(config.STRIPE_PLATFORM_SECRET_KEY)


def _api_key() -> str:
    return config.STRIPE_PLATFORM_SECRET_KEY


def studio_billing() -> dict:
    row = db.one("SELECT * FROM studio WHERE id=?", (STUDIO_ID,))
    return {
        "plan_tier": row["plan_tier"],
        "billing_status": row["billing_status"],
        "stripe_customer_id": row["stripe_customer_id"],
        "stripe_subscription_id": row["stripe_subscription_id"],
        "trial_ends_at": row["trial_ends_at"] if row and "trial_ends_at" in row.keys() else None,
    }


def ensure_customer(*, email: str | None = None, name: str | None = None) -> str:
    if not is_configured():
        return ""
    studio_row = db.one("SELECT * FROM studio WHERE id=?", (STUDIO_ID,))
    if studio_row["stripe_customer_id"]:
        return studio_row["stripe_customer_id"]
    cust = stripe.Customer.create(
        api_key=_api_key(),
        email=email or studio_row["contact_email"] or None,
        name=name or studio_row["name"],
        metadata={"studio_id": STUDIO_ID, "slug": studio_row["slug"]},
    )
    db.run("UPDATE studio SET stripe_customer_id=? WHERE id=?", (cust.id, STUDIO_ID))
    log.info("platform customer %s for studio %s", cust.id, STUDIO_ID)
    return cust.id


def start_trial(*, days: int = 14) -> None:
    ends = (dt.datetime.now(dt.UTC) + dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    db.run(
        "UPDATE studio SET plan_tier='trial', billing_status='trialing', trial_ends_at=? WHERE id=?",
        (ends, STUDIO_ID),
    )


def create_checkout(plan: str) -> str:
    if not is_configured():
        raise RuntimeError("platform billing is not configured")
    spec = PLANS.get(plan)
    if not spec:
        raise ValueError("unknown plan")
    price_id = spec["price_id"]()
    if not price_id:
        raise RuntimeError(f"price not configured for {plan}")
    studio_row = db.one("SELECT * FROM studio WHERE id=?", (STUDIO_ID,))
    customer_id = ensure_customer(email=studio_row["contact_email"], name=studio_row["name"])
    session = stripe.checkout.Session.create(
        api_key=_api_key(),
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{tenant.get_base_url()}/admin/billing?thanks=1",
        cancel_url=f"{tenant.get_base_url()}/admin/billing",
        metadata={"studio_id": STUDIO_ID, "plan_tier": spec["tier"]},
        subscription_data={"metadata": {"studio_id": STUDIO_ID, "plan_tier": spec["tier"]}},
    )
    return session.url


def create_portal() -> str:
    if not is_configured():
        raise RuntimeError("platform billing is not configured")
    studio_row = db.one("SELECT * FROM studio WHERE id=?", (STUDIO_ID,))
    if not studio_row["stripe_customer_id"]:
        raise RuntimeError("no billing account yet")
    session = stripe.billing_portal.Session.create(
        api_key=_api_key(),
        customer=studio_row["stripe_customer_id"],
        return_url=f"{tenant.get_base_url()}/admin/billing",
    )
    return session.url


def apply_subscription(
    *,
    studio_id: str,
    subscription_id: str,
    status: str,
    plan_tier: str | None = None,
) -> None:
    previous_studio = tenant.get_studio_id()
    tenant.set_studio(studio_id)
    try:
        billing_status = _STATUS_MAP.get(status, "none")
        tier = plan_tier or "starter"
        if billing_status == "canceled":
            tier = "solo"
        db.run(
            """UPDATE studio SET stripe_subscription_id=?, billing_status=?, plan_tier=?
               WHERE id=?""",
            (subscription_id, billing_status, tier, studio_id),
        )
        log.info("studio %s billing %s tier %s", studio_id, billing_status, tier)
    finally:
        tenant.set_studio(previous_studio)


def handle_webhook_event(event: dict) -> None:
    from . import stripe_webhooks

    etype = event["type"]
    obj = event["data"]["object"]
    if etype == "checkout.session.completed" and obj.get("mode") == "payment":
        if stripe_webhooks.handle_invoice_checkout_completed(obj):
            return
    if etype == "checkout.session.completed" and obj.get("mode") == "subscription":
        studio_id = obj.get("metadata", {}).get("studio_id")
        sub_id = obj.get("subscription")
        plan_tier = obj.get("metadata", {}).get("plan_tier")
        if studio_id and sub_id:
            apply_subscription(
                studio_id=studio_id,
                subscription_id=sub_id,
                status="active",
                plan_tier=plan_tier,
            )
    elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        studio_id = obj.get("metadata", {}).get("studio_id")
        if not studio_id:
            cust = obj.get("customer")
            row = db.one("SELECT id FROM studio WHERE stripe_customer_id=?", (cust,))
            studio_id = row["id"] if row else None
        if studio_id:
            plan_tier = obj.get("metadata", {}).get("plan_tier")
            apply_subscription(
                studio_id=studio_id,
                subscription_id=obj["id"],
                status=obj["status"],
                plan_tier=plan_tier,
            )


def provision_new_studio(studio_id: str, *, email: str, name: str) -> None:
    from . import tenant

    tenant.set_studio(studio_id)
    start_trial()
    if is_configured():
        ensure_customer(email=email, name=name)
