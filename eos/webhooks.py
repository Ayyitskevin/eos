"""Outbound webhooks for booking, delivery, and payment events."""

import hashlib
import hmac
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import db, security
from .vocab import STUDIO_ID

log = logging.getLogger("eos.webhooks")
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="eos-hook")
_TIMEOUT = 10.0

EVENTS = ("booking.created", "listing.delivered", "invoice.paid")


def list_subscriptions():
    return db.all_(
        "SELECT * FROM webhook_subscriptions WHERE studio_id=? ORDER BY id DESC",
        (str(STUDIO_ID),),
    )


def create_subscription(*, label: str, url: str, events: list[str]) -> int:
    url = url.strip()
    if not url.startswith("https://"):
        raise ValueError("webhook URL must be https")
    ev = [e for e in events if e in EVENTS]
    if not ev:
        ev = list(EVENTS)
    secret = security.new_token()
    wid = db.run(
        """INSERT INTO webhook_subscriptions (studio_id, label, url, secret, events)
           VALUES (?,?,?,?,?)""",
        (str(STUDIO_ID), label.strip() or "Webhook", url, secret, json.dumps(ev)),
    )
    db.audit("admin", "webhook.create", f"id={wid}")
    return wid


def delete_subscription(sub_id: int) -> None:
    db.run("DELETE FROM webhook_subscriptions WHERE id=? AND studio_id=?", (sub_id, str(STUDIO_ID)))


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(sub_id: int, studio_id: str, url: str, secret: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    delivery_id = db.run(
        """INSERT INTO webhook_deliveries (subscription_id, studio_id, event, status)
           VALUES (?,?,?,'pending')""",
        (sub_id, studio_id, payload.get("event", "")),
    )
    try:
        resp = httpx.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Eos-Event": payload.get("event", ""),
                "X-Eos-Signature": _sign(secret, body),
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        db.run(
            "UPDATE webhook_deliveries SET status='ok' WHERE id=?",
            (delivery_id,),
        )
    except Exception as e:
        db.run(
            "UPDATE webhook_deliveries SET status='failed', error=? WHERE id=?",
            (str(e)[:500], delivery_id),
        )
        log.error("webhook %s failed: %s", url, e)


def dispatch(event: str, payload: dict, *, studio_id: str | None = None) -> None:
    sid = studio_id or str(STUDIO_ID)
    subs = db.all_(
        "SELECT * FROM webhook_subscriptions WHERE studio_id=? AND active=1",
        (sid,),
    )
    if not subs:
        return
    body = {"event": event, "studio_id": sid, "data": payload}
    for sub in subs:
        try:
            events = json.loads(sub["events"] or "[]")
        except json.JSONDecodeError:
            events = list(EVENTS)
        if event not in events:
            continue
        _pool.submit(_post, sub["id"], sid, sub["url"], sub["secret"], body)