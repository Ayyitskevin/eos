"""Manual agent rebooking outreach with audit-log cooldown."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException

from . import churn, clients, db, mailer, tenant
from .vocab import STUDIO_ID

COOLDOWN_DAYS = 14
ACTION_SENT = "rebooking.email.sent"
ACTION_DRAFT = "rebooking.email.draft"
ACTION_FAILED = "rebooking.email.failed"


def _first(name: str) -> str:
    return (name or "there").split()[0]


def _detail(client_id: int, email: str, subject: str = "") -> str:
    return f"client_id={client_id}; email={email}; subject={subject[:120]}"


def recent_sent_at(client_id: int, *, days: int = COOLDOWN_DAYS) -> str | None:
    row = db.one(
        """SELECT created_at FROM audit_log
           WHERE studio_id=? AND action=? AND detail LIKE ?
             AND created_at >= datetime('now', ?)
           ORDER BY created_at DESC LIMIT 1""",
        (STUDIO_ID, ACTION_SENT, f"client_id={client_id};%", f"-{days} days"),
    )
    return row["created_at"] if row else None


def decorate_opportunity(opportunity: dict[str, Any]) -> dict[str, Any]:
    recent = recent_sent_at(opportunity["id"])
    enriched = dict(opportunity)
    enriched["last_rebooking_email_at"] = recent
    enriched["cooldown_active"] = bool(recent)
    enriched["can_email_rebooking"] = bool(opportunity.get("email")) and not recent
    return enriched


def decorate_opportunities(opportunities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [decorate_opportunity(o) for o in opportunities]


def build_email(client_id: int) -> dict[str, str | int | None]:
    client = clients.get_client(client_id)
    if client["client_type"] != "agent":
        raise HTTPException(status_code=400, detail="rebooking outreach is only for agents")
    if not client["email"]:
        raise HTTPException(status_code=400, detail="agent email required")
    opportunity = churn.rebooking_for_client(client_id)
    if not opportunity:
        raise HTTPException(status_code=400, detail="agent is not a rebooking opportunity")

    book_link = f"{tenant.get_base_url()}/book"
    if opportunity["days_idle"] is None:
        history_line = "I would love to help with your next listing when you have one ready."
    else:
        history_line = (
            f"It has been about {opportunity['days_idle']} days since the last listing "
            "we photographed together."
        )
    subject = "Ready for your next listing?"
    body = f"""Hi {_first(client["name"])},

I wanted to make it easy to get your next listing on the calendar.
{history_line}

Book a new listing here:
{book_link}

If you already have an address ready, reply with the property details and ideal timing.

Thanks,
{tenant.get_site_name()}"""
    return {
        "client_id": client_id,
        "to": client["email"].strip(),
        "subject": subject,
        "body": body,
        "mailto_href": f"mailto:{client['email'].strip()}?{urlencode({'subject': subject, 'body': body})}",
    }


def send_email(client_id: int, *, cooldown_days: int = COOLDOWN_DAYS) -> dict[str, Any]:
    draft = build_email(client_id)
    recent = recent_sent_at(client_id, days=cooldown_days)
    if recent:
        return {"status": "cooldown", "last_sent_at": recent, "draft": draft}

    if not mailer.configured():
        db.audit("admin", ACTION_DRAFT, _detail(client_id, str(draft["to"]), str(draft["subject"])))
        return {"status": "draft", "draft": draft}

    try:
        mailer.send_for_studio(str(draft["to"]), str(draft["subject"]), str(draft["body"]))
    except Exception as exc:
        db.audit(
            "admin",
            ACTION_FAILED,
            f"{_detail(client_id, str(draft['to']), str(draft['subject']))}; error={str(exc)[:120]}",
        )
        raise HTTPException(status_code=502, detail="rebooking email failed")

    db.run(
        """INSERT INTO emails_log (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
           VALUES (?,?,?,?,?,?)""",
        (STUDIO_ID, None, "rebooking", client_id, draft["to"], draft["subject"]),
    )
    db.audit("admin", ACTION_SENT, _detail(client_id, str(draft["to"]), str(draft["subject"])))
    return {"status": "sent", "draft": draft}
