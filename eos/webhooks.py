"""Outbound webhooks for booking, delivery, and payment events."""

import hashlib
import hmac
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import urllib.request

from . import db, security
from .vocab import STUDIO_ID

log = logging.getLogger("eos.webhooks")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="eos-hook")

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


def _post(url: str, secret: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Eos-Event": payload.get("event", ""),
            "X-Eos-Signature": _sign(secret, body),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 400:
                log.warning("webhook %s returned %s", url, resp.status)
    except Exception as e:
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
        _pool.submit(_post, sub["url"], sub["secret"], body)