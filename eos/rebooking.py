"""Manual agent rebooking outreach with audit-log cooldown."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException

from . import churn, clients, db, mailer, tenant
from .vocab import STUDIO_ID

COOLDOWN_DAYS = 14
ACTION_SENT = "rebooking.email.sent"
ACTION_DRAFT = "rebooking.email.draft"
ACTION_FAILED = "rebooking.email.failed"
_CLIENT_RE = re.compile(r"\bclient_id=(\d+)\b")


def _first(name: str) -> str:
    return (name or "there").split()[0]


def _detail(client_id: int, email: str, subject: str = "") -> str:
    return f"client_id={client_id}; email={email}; subject={subject[:120]}"


def _money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${dollars:,.0f}"
    return f"${dollars:,.2f}"


def _client_id_from_detail(detail: str | None) -> int | None:
    if not detail:
        return None
    match = _CLIENT_RE.search(detail)
    return int(match.group(1)) if match else None


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


def _recent_activity(days: int) -> list[Any]:
    return db.all_(
        """SELECT action, detail, created_at
           FROM audit_log
           WHERE studio_id=? AND action IN (?, ?, ?)
             AND created_at >= datetime('now', ?)
           ORDER BY created_at DESC""",
        (
            STUDIO_ID,
            ACTION_SENT,
            ACTION_DRAFT,
            ACTION_FAILED,
            f"-{days} days",
        ),
    )


def _conversion_for_outreach(row: Any) -> dict[str, Any] | None:
    client_id = _client_id_from_detail(row["detail"])
    if client_id is None:
        return None
    converted = db.one(
        """SELECT l.id, l.title, l.created_at, c.name AS client_name
           FROM listings l
           JOIN clients c ON c.id=l.client_id AND c.studio_id=l.studio_id
           WHERE l.studio_id=? AND l.client_id=? AND l.created_at >= ?
           ORDER BY l.created_at ASC LIMIT 1""",
        (STUDIO_ID, client_id, row["created_at"]),
    )
    if not converted:
        return None
    return {
        "client_id": client_id,
        "client_name": converted["client_name"],
        "listing_id": converted["id"],
        "listing_title": converted["title"],
        "listing_href": f"/admin/listings/{converted['id']}",
        "created_at": converted["created_at"],
    }


def performance_snapshot(
    opportunities: list[dict[str, Any]] | None = None, *, days: int = 30
) -> dict[str, Any]:
    if opportunities is None:
        opportunities = decorate_opportunities(churn.rebooking_opportunities(limit=50))
    activity = _recent_activity(days)
    sent = [row for row in activity if row["action"] == ACTION_SENT]
    drafts = [row for row in activity if row["action"] == ACTION_DRAFT]
    failures = [row for row in activity if row["action"] == ACTION_FAILED]

    converted_by_client: dict[int, dict[str, Any]] = {}
    for row in sent:
        converted = _conversion_for_outreach(row)
        if converted:
            converted_by_client.setdefault(converted["client_id"], converted)

    ready_count = sum(1 for o in opportunities if o.get("can_email_rebooking"))
    cooldown_count = sum(1 for o in opportunities if o.get("cooldown_active"))
    missing_email_count = sum(1 for o in opportunities if not o.get("email"))
    high_value_count = sum(1 for o in opportunities if o.get("priority") == "High value")
    at_risk_paid_cents = sum(int(o.get("paid_cents") or 0) for o in opportunities)
    sent_count = len(sent)
    converted_count = len(converted_by_client)
    if ready_count:
        next_action = (
            f"Nudge {ready_count} agent{'s' if ready_count != 1 else ''} ready for outreach."
        )
    elif missing_email_count:
        next_action = "Add missing agent emails to unlock outreach."
    elif cooldown_count:
        next_action = "Wait for replies or watch for repeat listings from recently nudged agents."
    else:
        next_action = "No stale agent outreach needs action right now."

    conversion_rate = round((converted_count / sent_count) * 100) if sent_count else 0
    return {
        "days": days,
        "opportunity_count": len(opportunities),
        "ready_count": ready_count,
        "cooldown_count": cooldown_count,
        "missing_email_count": missing_email_count,
        "high_value_count": high_value_count,
        "at_risk_paid_cents": at_risk_paid_cents,
        "at_risk_paid_display": _money(at_risk_paid_cents),
        "sent_recent": sent_count,
        "draft_recent": len(drafts),
        "failed_recent": len(failures),
        "converted_recent": converted_count,
        "conversion_rate_pct": conversion_rate,
        "converted_listings": list(converted_by_client.values()),
        "next_action": next_action,
    }


def client_history(client_id: int, *, days: int = 90) -> dict[str, Any]:
    clients.get_client(client_id)
    rows = db.all_(
        """SELECT action, detail, created_at
           FROM audit_log
           WHERE studio_id=? AND detail LIKE ?
             AND action IN (?, ?, ?)
             AND created_at >= datetime('now', ?)
           ORDER BY created_at DESC LIMIT 10""",
        (
            STUDIO_ID,
            f"client_id={client_id};%",
            ACTION_SENT,
            ACTION_DRAFT,
            ACTION_FAILED,
            f"-{days} days",
        ),
    )
    latest_sent = next((row["created_at"] for row in rows if row["action"] == ACTION_SENT), None)
    conversion = None
    if latest_sent:
        conversion = db.one(
            """SELECT id, title, created_at FROM listings
               WHERE studio_id=? AND client_id=? AND created_at >= ?
               ORDER BY created_at ASC LIMIT 1""",
            (STUDIO_ID, client_id, latest_sent),
        )
    return {
        "events": rows,
        "latest_sent_at": latest_sent,
        "draft_count": sum(1 for row in rows if row["action"] == ACTION_DRAFT),
        "failed_count": sum(1 for row in rows if row["action"] == ACTION_FAILED),
        "converted_listing": {
            "id": conversion["id"],
            "title": conversion["title"],
            "href": f"/admin/listings/{conversion['id']}",
            "created_at": conversion["created_at"],
        }
        if conversion
        else None,
    }


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
