"""Unified Stripe Checkout for invoices — Connect destination charges or legacy solo key."""

from __future__ import annotations

import logging

import stripe
from fastapi import HTTPException

from . import config, stripe_connect

log = logging.getLogger("eos.stripe_checkout")


def _line_items(*, title: str, amount_cents: int) -> list:
    return [
        {
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": amount_cents,
                "product_data": {"name": title},
            },
        }
    ]


def create_payment_session(
    *,
    amount_cents: int,
    title: str,
    success_url: str,
    cancel_url: str,
    customer_email: str | None,
    metadata: dict[str, str],
    existing_session_id: str | None = None,
) -> stripe.checkout.Session:
    if existing_session_id:
        api_key = _api_key_for_retrieve()
        if api_key:
            try:
                session = stripe.checkout.Session.retrieve(
                    existing_session_id,
                    api_key=api_key,
                )
                if session.url and session.status == "open":
                    return session
            except Exception:
                pass

    conn = stripe_connect.studio_connect()
    if conn and conn["charges_enabled"] and stripe_connect.is_configured():
        fee = stripe_connect.application_fee_cents(amount_cents)
        pi_data: dict = {"transfer_data": {"destination": conn["account_id"]}}
        if fee:
            pi_data["application_fee_amount"] = fee
        session = stripe.checkout.Session.create(
            api_key=stripe_connect.platform_api_key(),
            mode="payment",
            payment_method_types=["card"],
            line_items=_line_items(title=title, amount_cents=amount_cents),
            customer_email=customer_email,
            metadata=metadata,
            success_url=success_url,
            cancel_url=cancel_url,
            payment_intent_data=pi_data,
        )
        log.info("connect checkout %s amount=%s fee=%s", session.id, amount_cents, fee)
        return session

    if config.STRIPE_SECRET_KEY:
        session = stripe.checkout.Session.create(
            api_key=config.STRIPE_SECRET_KEY,
            mode="payment",
            payment_method_types=["card"],
            line_items=_line_items(title=title, amount_cents=amount_cents),
            customer_email=customer_email,
            metadata=metadata,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        log.info("legacy checkout %s amount=%s", session.id, amount_cents)
        return session

    raise HTTPException(status_code=503, detail="online payment is not configured")


def _api_key_for_retrieve() -> str:
    if stripe_connect.payments_ready():
        return stripe_connect.platform_api_key()
    return config.STRIPE_SECRET_KEY


def payments_configured() -> bool:
    return stripe_connect.payments_ready() or bool(config.STRIPE_SECRET_KEY)