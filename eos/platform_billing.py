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
_STATUS_RESTRICTIVENESS = {
    "active": 0,
    "trialing": 1,
    "none": 2,
    "past_due": 3,
    "canceled": 4,
}
_TIER_RESTRICTIVENESS = {
    "pro": 0,
    "starter": 1,
    # ``solo`` is only emitted with terminal canceled status. It is an
    # unmetered self-hosted tier, never a valid active Stripe subscription tier.
    "solo": 0,
}


def _normalized_tier(billing_status: str, plan_tier: str | None) -> str:
    if billing_status == "canceled":
        return "solo"
    candidate = (plan_tier or "").strip().lower()
    return candidate if candidate in PLANS else "starter"


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
    if not studio_row:
        raise RuntimeError("studio not found")
    if studio_row["stripe_customer_id"]:
        return studio_row["stripe_customer_id"]
    cust = stripe.Customer.create(
        api_key=_api_key(),
        email=email or studio_row["contact_email"] or None,
        name=name or studio_row["name"],
        metadata={"studio_id": str(STUDIO_ID), "slug": studio_row["slug"]},
        idempotency_key=f"eos-platform-customer:{STUDIO_ID}",
    )
    with db.tx(immediate=True) as con:
        updated = con.execute(
            """UPDATE studio SET stripe_customer_id=?
               WHERE id=? AND COALESCE(stripe_customer_id, ?) = ?""",
            (cust.id, STUDIO_ID, "", ""),
        )
        winner = (
            cust.id
            if updated.rowcount == 1
            else con.execute(
                "SELECT stripe_customer_id FROM studio WHERE id=?", (STUDIO_ID,)
            ).fetchone()["stripe_customer_id"]
        )
    if not winner:
        raise RuntimeError("platform customer persistence failed")
    if winner != cust.id:
        log.warning("studio %s already bound to platform customer %s", STUDIO_ID, winner)
    else:
        log.info("platform customer %s for studio %s", cust.id, STUDIO_ID)
    return winner


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
    if not studio_row:
        raise RuntimeError("studio not found")
    if studio_row["billing_status"] in ("active", "past_due"):
        return create_portal()
    if studio_row["stripe_subscription_id"] and studio_row["billing_status"] != "canceled":
        return create_portal()

    previous_session_id = studio_row["platform_checkout_session_id"] or ""
    if previous_session_id:
        try:
            previous = stripe.checkout.Session.retrieve(previous_session_id, api_key=_api_key())
        except Exception as exc:
            raise RuntimeError(
                "Unable to verify the existing subscription checkout; retry shortly."
            ) from exc
        metadata = dict(previous.get("metadata") or {})
        bound = (
            previous.get("mode") == "subscription"
            and previous.get("customer") == studio_row["stripe_customer_id"]
            and metadata.get("studio_id") == str(STUDIO_ID)
            and metadata.get("plan_tier") == studio_row["platform_checkout_plan"]
        )
        if not bound:
            raise RuntimeError("stored subscription checkout binding mismatch")
        if previous.get("status") == "open" and previous.get("url"):
            return previous.url
        if previous.get("status") == "complete":
            raise RuntimeError("subscription checkout is still processing")
        if previous.get("status") != "expired":
            raise RuntimeError("subscription checkout has an unexpected state")

    customer_id = ensure_customer(email=studio_row["contact_email"], name=studio_row["name"])
    attempt = previous_session_id or "initial"
    try:
        session = stripe.checkout.Session.create(
            api_key=_api_key(),
            idempotency_key=f"eos-platform-subscription:{STUDIO_ID}:{attempt}",
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{tenant.get_base_url()}/admin/billing?thanks=1",
            cancel_url=f"{tenant.get_base_url()}/admin/billing",
            metadata={"studio_id": str(STUDIO_ID), "plan_tier": spec["tier"]},
            subscription_data={
                "metadata": {"studio_id": str(STUDIO_ID), "plan_tier": spec["tier"]}
            },
        )
    except Exception as exc:
        raise RuntimeError("Unable to create subscription checkout; retry shortly.") from exc
    with db.tx(immediate=True) as con:
        updated = con.execute(
            """UPDATE studio
               SET platform_checkout_session_id=?, platform_checkout_plan=?,
                   platform_checkout_url=?
               WHERE id=? AND billing_status NOT IN ('active','past_due')
                 AND COALESCE(platform_checkout_session_id, ?) = ?""",
            (
                session.id,
                spec["tier"],
                session.url,
                STUDIO_ID,
                "",
                previous_session_id,
            ),
        )
        winner = con.execute(
            """SELECT billing_status, stripe_subscription_id,
                      platform_checkout_session_id, platform_checkout_url
               FROM studio WHERE id=?""",
            (STUDIO_ID,),
        ).fetchone()
    if updated.rowcount == 1:
        return session.url
    if winner and (
        winner["billing_status"] in ("active", "past_due") or winner["stripe_subscription_id"]
    ):
        return create_portal()
    if winner and winner["platform_checkout_session_id"] and winner["platform_checkout_url"]:
        return winner["platform_checkout_url"]
    raise RuntimeError("subscription checkout persistence failed")


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
    event_created: int,
    event_id: str,
    plan_tier: str | None = None,
) -> bool:
    previous_studio = tenant.get_studio_id()
    tenant.set_studio(studio_id)
    try:
        billing_status = _STATUS_MAP.get(status, "none")
        tier = _normalized_tier(billing_status, plan_tier)
        if billing_status != "canceled" and (plan_tier or "").strip().lower() not in PLANS:
            log.warning(
                "subscription event has no recognized paid tier; defaulting studio=%s to starter",
                studio_id,
            )
        with db.tx(immediate=True) as con:
            row = con.execute("SELECT * FROM studio WHERE id=?", (studio_id,)).fetchone()
            if not row:
                return False
            last_created = int(row["platform_subscription_event_created"] or 0)
            last_event_id = str(row["platform_subscription_event_id"] or "")
            same_subscription = row["stripe_subscription_id"] == subscription_id
            duplicate = event_created == last_created and event_id == last_event_id
            incoming_restrictiveness = (
                _STATUS_RESTRICTIVENESS.get(billing_status, 3),
                _TIER_RESTRICTIVENESS[tier],
            )
            current_tier = _normalized_tier(row["billing_status"], row["plan_tier"])
            current_restrictiveness = (
                _STATUS_RESTRICTIVENESS.get(row["billing_status"], 3),
                _TIER_RESTRICTIVENESS[current_tier],
            )
            ambiguous_relaxation = (
                event_created == last_created
                and same_subscription
                and incoming_restrictiveness <= current_restrictiveness
            )
            if event_created < last_created or duplicate or ambiguous_relaxation:
                log.warning(
                    "stale subscription event ignored studio=%s event=%s", studio_id, event_id
                )
                return False
            if (
                row["stripe_subscription_id"] == subscription_id
                and row["billing_status"] == "canceled"
                and billing_status != "canceled"
            ):
                log.warning("terminal canceled subscription update ignored studio=%s", studio_id)
                return False
            con.execute(
                """UPDATE studio
                   SET stripe_subscription_id=?, billing_status=?, plan_tier=?,
                       platform_checkout_session_id='', platform_checkout_plan='',
                       platform_checkout_url='',
                       platform_subscription_event_created=?,
                       platform_subscription_event_id=?
                   WHERE id=?""",
                (
                    subscription_id,
                    billing_status,
                    tier,
                    event_created,
                    event_id,
                    studio_id,
                ),
            )
        log.info("studio %s billing %s tier %s", studio_id, billing_status, tier)
        return True
    finally:
        tenant.set_studio(previous_studio)


def handle_webhook_event(event: dict) -> None:
    from . import stripe_webhooks

    etype = event["type"]
    obj = event["data"]["object"]
    event_id = str(event.get("id") or "")
    try:
        event_created = int(event.get("created") or 0)
    except (TypeError, ValueError):
        event_created = 0
    subscription_event = (
        etype == "checkout.session.completed" and obj.get("mode") == "subscription"
    ) or etype in ("customer.subscription.updated", "customer.subscription.deleted")
    if subscription_event and (not event_id or event_created <= 0):
        log.warning("subscription event without a stable id/timestamp ignored")
        return
    if etype == "checkout.session.completed" and obj.get("mode") == "payment":
        if stripe_webhooks.handle_invoice_checkout_event(event, source="stripe-platform"):
            return
    if etype == "checkout.session.completed" and obj.get("mode") == "subscription":
        metadata = dict(obj.get("metadata") or {})
        studio_id = str(metadata.get("studio_id") or "")
        sub_id = str(obj.get("subscription") or "")
        plan_tier = str(metadata.get("plan_tier") or "")
        row = db.one("SELECT * FROM studio WHERE id=?", (studio_id,)) if studio_id else None
        if not row or not sub_id:
            log.warning("unbound subscription checkout completion ignored")
            return
        bound = (
            str(obj.get("id") or "") == row["platform_checkout_session_id"]
            and str(obj.get("customer") or "") == row["stripe_customer_id"]
            and plan_tier == row["platform_checkout_plan"]
            and plan_tier in PLANS
        )
        if not bound or (
            row["stripe_subscription_id"]
            and row["stripe_subscription_id"] != sub_id
            and row["billing_status"] != "canceled"
        ):
            log.warning(
                "stale or mismatched subscription checkout ignored studio=%s session=%s",
                studio_id,
                obj.get("id"),
            )
            return
        apply_subscription(
            studio_id=studio_id,
            subscription_id=sub_id,
            status="active",
            plan_tier=plan_tier,
            event_created=event_created,
            event_id=event_id,
        )
    elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        metadata = dict(obj.get("metadata") or {})
        studio_id = str(metadata.get("studio_id") or "")
        customer_id = str(obj.get("customer") or "")
        if studio_id:
            row = db.one("SELECT * FROM studio WHERE id=?", (studio_id,))
        else:
            row = db.one("SELECT * FROM studio WHERE stripe_customer_id=?", (customer_id,))
            studio_id = row["id"] if row else ""
        subscription_id = str(obj.get("id") or "")
        plan_tier = str(metadata.get("plan_tier") or "")
        if not row or not subscription_id or customer_id != row["stripe_customer_id"]:
            log.warning("unbound subscription update ignored")
            return
        current = row["stripe_subscription_id"]
        if current and current != subscription_id:
            log.warning("stale subscription update ignored studio=%s", studio_id)
            return
        if not current and (
            not row["platform_checkout_session_id"] or plan_tier != row["platform_checkout_plan"]
        ):
            log.warning("subscription update lacks a pending checkout binding")
            return
        apply_subscription(
            studio_id=studio_id,
            subscription_id=subscription_id,
            status=obj["status"],
            plan_tier=plan_tier,
            event_created=event_created,
            event_id=event_id,
        )


def provision_new_studio(studio_id: str, *, email: str, name: str) -> None:
    from . import tenant

    tenant.set_studio(studio_id)
    start_trial()
    if is_configured():
        ensure_customer(email=email, name=name)
