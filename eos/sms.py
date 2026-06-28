"""Twilio SMS — shoot-day reminders and sequence channel."""

import logging

import httpx

from . import config, db
from .vocab import STUDIO_ID

log = logging.getLogger("eos.sms")


def configured() -> bool:
    return bool(
        config.TWILIO_ACCOUNT_SID
        and config.TWILIO_AUTH_TOKEN
        and config.TWILIO_FROM_NUMBER
    )


def send(*, to_phone: str, body: str) -> bool:
    to_phone = "".join(c for c in to_phone if c.isdigit() or c == "+")
    if not to_phone or not body.strip():
        return False
    if not configured():
        log.info("sms stub to=%s body=%s", to_phone, body[:80])
        db.run(
            "INSERT INTO sms_log (studio_id, to_phone, body, status) VALUES (?,?,?,'stub')",
            (STUDIO_ID, to_phone, body.strip()[:500]),
        )
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        resp = httpx.post(
            url,
            auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN),
            data={"From": config.TWILIO_FROM_NUMBER, "To": to_phone, "Body": body.strip()[:1600]},
            timeout=30,
        )
        resp.raise_for_status()
        db.run(
            "INSERT INTO sms_log (studio_id, to_phone, body, status) VALUES (?,?,?,'sent')",
            (STUDIO_ID, to_phone, body.strip()[:500]),
        )
        return True
    except Exception as e:
        log.error("sms failed: %s", e)
        db.run(
            "INSERT INTO sms_log (studio_id, to_phone, body, status) VALUES (?,?,?,'failed')",
            (STUDIO_ID, to_phone, body.strip()[:500]),
        )
        return False


def shoot_day_reminders() -> int:
    """SMS agents with shoots starting today."""
    if not configured():
        return 0
    sent = 0
    rows = db.all_(
        """SELECT a.title, a.starts_at, c.phone, c.name
           FROM appointments a
           JOIN clients c ON c.id=a.client_id
           WHERE a.studio_id=? AND a.status='confirmed'
             AND date(a.starts_at)=date('now')
             AND c.phone IS NOT NULL AND c.phone != ''""",
        (STUDIO_ID,),
    )
    from .tenant import get_site_name
    site = get_site_name()
    for r in rows:
        when = r["starts_at"][11:16] if r["starts_at"] else "today"
        body = f"Reminder: {site} shoot at {when} — {r['title']}. Reply if you need to reschedule."
        if send(to_phone=r["phone"], body=body):
            sent += 1
    return sent