"""Payment readiness — Stripe Connect per tenant or legacy global key."""

from . import stripe_checkout, stripe_connect


def configured() -> bool:
    return stripe_checkout.payments_configured()


def connect_status() -> dict:
    conn = stripe_connect.studio_connect()
    return {
        "connect_configured": stripe_connect.is_configured(),
        "connected": bool(conn),
        "charges_enabled": bool(conn and conn["charges_enabled"]),
        "account_id": conn["account_id"] if conn else "",
    }
