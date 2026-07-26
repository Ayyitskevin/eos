"""Stripe Connect — each studio collects client payments on their own account."""

from __future__ import annotations

import logging

import stripe
from fastapi import HTTPException

from . import config, db
from .vocab import STUDIO_ID

log = logging.getLogger("eos.stripe_connect")


def is_configured() -> bool:
    return bool(config.STRIPE_PLATFORM_SECRET_KEY)


def platform_api_key() -> str:
    return config.STRIPE_PLATFORM_SECRET_KEY


def studio_connect(*, studio_id: str | None = None) -> dict | None:
    sid = studio_id or str(STUDIO_ID)
    row = db.one(
        """SELECT stripe_connect_account_id, stripe_connect_charges_enabled
           FROM studio WHERE id=?""",
        (sid,),
    )
    if not row or not row["stripe_connect_account_id"]:
        return None
    return {
        "account_id": row["stripe_connect_account_id"],
        "charges_enabled": bool(row["stripe_connect_charges_enabled"]),
    }


def payments_ready(*, studio_id: str | None = None) -> bool:
    conn = studio_connect(studio_id=studio_id)
    return bool(conn and conn["charges_enabled"])


def ensure_account(*, email: str | None = None, country: str = "US") -> str:
    if not is_configured():
        raise HTTPException(status_code=503, detail="Stripe Connect is not configured")
    studio_row = db.one("SELECT * FROM studio WHERE id=?", (STUDIO_ID,))
    if not studio_row:
        raise HTTPException(status_code=404, detail="Studio not found")
    if studio_row["stripe_connect_account_id"]:
        return studio_row["stripe_connect_account_id"]
    acct = stripe.Account.create(
        api_key=platform_api_key(),
        type="express",
        country=country,
        email=email or studio_row["contact_email"] or None,
        capabilities={"card_payments": {"requested": True}, "transfers": {"requested": True}},
        metadata={"studio_id": str(STUDIO_ID), "slug": studio_row["slug"]},
        business_profile={"name": studio_row["name"]},
        idempotency_key=f"eos-connect-account:{STUDIO_ID}",
    )
    with db.tx(immediate=True) as con:
        updated = con.execute(
            """UPDATE studio SET stripe_connect_account_id=?
               WHERE id=? AND COALESCE(stripe_connect_account_id, ?) = ?""",
            (acct.id, STUDIO_ID, "", ""),
        )
        winner = (
            acct.id
            if updated.rowcount == 1
            else con.execute(
                "SELECT stripe_connect_account_id FROM studio WHERE id=?", (STUDIO_ID,)
            ).fetchone()["stripe_connect_account_id"]
        )
    if not winner:
        raise RuntimeError("Stripe Connect account persistence failed")
    if winner != acct.id:
        log.warning("studio %s already bound to Connect account %s", STUDIO_ID, winner)
    else:
        log.info("connect account %s for studio %s", acct.id, STUDIO_ID)
    return winner


def onboarding_url(*, refresh_url: str, return_url: str) -> str:
    account_id = ensure_account()
    link = stripe.AccountLink.create(
        api_key=platform_api_key(),
        account=account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )
    return link.url


def dashboard_url() -> str:
    conn = studio_connect()
    if not conn:
        raise HTTPException(status_code=400, detail="Connect Stripe first")
    link = stripe.Account.create_login_link(
        conn["account_id"],
        api_key=platform_api_key(),
    )
    return link.url


def refresh_account_status(*, studio_id: str | None = None) -> bool:
    sid = studio_id or str(STUDIO_ID)
    row = db.one(
        "SELECT stripe_connect_account_id FROM studio WHERE id=?",
        (sid,),
    )
    if not row or not row["stripe_connect_account_id"]:
        return False
    acct = stripe.Account.retrieve(row["stripe_connect_account_id"], api_key=platform_api_key())
    enabled = bool(acct.charges_enabled and acct.payouts_enabled)
    db.run(
        "UPDATE studio SET stripe_connect_charges_enabled=? WHERE id=?",
        (1 if enabled else 0, sid),
    )
    return enabled


def disconnect() -> None:
    db.run(
        """UPDATE studio SET stripe_connect_account_id='', stripe_connect_charges_enabled=0
           WHERE id=?""",
        (STUDIO_ID,),
    )
    db.audit("admin", "stripe_connect.disconnect", f"studio={STUDIO_ID}")


def handle_account_updated(account: dict) -> None:
    acct_id = account.get("id")
    if not acct_id:
        return
    row = db.one("SELECT id FROM studio WHERE stripe_connect_account_id=?", (acct_id,))
    if not row:
        return
    enabled = bool(account.get("charges_enabled") and account.get("payouts_enabled"))
    db.run(
        "UPDATE studio SET stripe_connect_charges_enabled=? WHERE id=?",
        (1 if enabled else 0, row["id"]),
    )
    log.info("connect account %s charges_enabled=%s studio=%s", acct_id, enabled, row["id"])


def application_fee_cents(amount_cents: int) -> int:
    pct = config.STRIPE_APPLICATION_FEE_PERCENT
    if pct <= 0:
        return 0
    return max(0, int(amount_cents * pct / 100))
