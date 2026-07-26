"""Manual agent rebooking outreach with audit-log cooldown."""

from __future__ import annotations

import datetime as dt
import re
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException

from . import churn, clients, db, mailer, tenant
from .vocab import STUDIO_ID

COOLDOWN_DAYS = 14
FOLLOW_UP_DAYS = 7
ACTION_SENT = "rebooking.email.sent"
ACTION_DRAFT = "rebooking.email.draft"
ACTION_FAILED = "rebooking.email.failed"
ACTION_UNKNOWN = "rebooking.email.unknown"
_CLIENT_RE = re.compile(r"\bclient_id=(\d+)\b")
_CLAIM_STALE_MINUTES = 10


def _first(name: str) -> str:
    return (name or "there").split()[0]


def _detail(client_id: int, email: str, subject: str = "") -> str:
    return f"client_id={client_id}; email={email}; subject={subject[:120]}"


def _money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${dollars:,.0f}"
    return f"${dollars:,.2f}"


def _days_since_timestamp(value: str | None) -> int | None:
    if not value:
        return None
    try:
        seen = dt.datetime.fromisoformat(value[:19].replace(" ", "T"))
    except ValueError:
        return None
    return max(0, (dt.datetime.now() - seen).days)


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


def unresolved_intent(client_id: int) -> dict[str, Any] | None:
    row = db.one(
        """SELECT id, status, to_email, subject, claimed_at, error,
                  CASE
                    WHEN status='unknown'
                      OR claimed_at <= datetime('now', ?)
                    THEN 1 ELSE 0
                  END AS reconcilable
           FROM rebooking_email_intents
           WHERE studio_id=? AND client_id=? AND status IN ('claimed','unknown')
           ORDER BY id DESC LIMIT 1""",
        (f"-{_CLAIM_STALE_MINUTES} minutes", STUDIO_ID, client_id),
    )
    return dict(row) if row else None


@db.transactional(immediate=True)
def _claim_email_intent(
    draft: dict[str, str | int | None], *, cooldown_days: int
) -> dict[str, Any]:
    client_id = int(draft["client_id"] or 0)
    recent = recent_sent_at(client_id, days=cooldown_days)
    if recent:
        return {"status": "cooldown", "last_sent_at": recent}

    unresolved = unresolved_intent(client_id)
    if unresolved:
        return {"status": "review", "intent": unresolved}

    latest_sent = db.one(
        """SELECT id FROM rebooking_email_intents
           WHERE studio_id=? AND client_id=? AND status='sent'
           ORDER BY id DESC LIMIT 1""",
        (STUDIO_ID, client_id),
    )
    predecessor = str(latest_sent["id"]) if latest_sent else "first"
    event_key = f"rebooking:{client_id}:after:{predecessor}"
    claim_token = secrets.token_urlsafe(24)
    existing = db.one(
        """SELECT id, status, sent_at FROM rebooking_email_intents
           WHERE studio_id=? AND event_key=?""",
        (STUDIO_ID, event_key),
    )
    if existing and existing["status"] == "sent":
        return {"status": "cooldown", "last_sent_at": existing["sent_at"]}
    if existing:
        db.run(
            """UPDATE rebooking_email_intents
               SET status='claimed', attempts=attempts+1,
                   claimed_at=datetime('now'), error=NULL, claim_token=?,
                   updated_at=datetime('now')
               WHERE id=? AND studio_id=? AND status='failed'""",
            (claim_token, existing["id"], STUDIO_ID),
        )
        return {
            "status": "claimed",
            "intent_id": existing["id"],
            "claim_token": claim_token,
        }

    intent_id = db.run(
        """INSERT INTO rebooking_email_intents
           (studio_id, client_id, event_key, to_email, subject, claim_token)
           VALUES (?,?,?,?,?,?)""",
        (
            STUDIO_ID,
            client_id,
            event_key,
            str(draft["to"]),
            str(draft["subject"]),
            claim_token,
        ),
    )
    return {"status": "claimed", "intent_id": intent_id, "claim_token": claim_token}


@db.transactional(immediate=True)
def _mark_intent_failed(
    intent_id: int,
    claim_token: str,
    exc: Exception,
    *,
    outcome_unknown: bool,
) -> None:
    row = db.one(
        """SELECT client_id, to_email, subject FROM rebooking_email_intents
           WHERE id=? AND studio_id=? AND status='claimed' AND claim_token=?""",
        (intent_id, STUDIO_ID, claim_token),
    )
    if not row:
        return
    status = "unknown" if outcome_unknown else "failed"
    action = ACTION_UNKNOWN if outcome_unknown else ACTION_FAILED
    message = str(exc)[:300]
    db.run(
        """UPDATE rebooking_email_intents
           SET status=?, error=?, claim_token='', updated_at=datetime('now')
           WHERE id=? AND studio_id=? AND status='claimed' AND claim_token=?""",
        (status, message, intent_id, STUDIO_ID, claim_token),
    )
    db.audit(
        "admin",
        action,
        f"{_detail(row['client_id'], row['to_email'], row['subject'])}; intent_id={intent_id}; error={message[:120]}",
    )


@db.transactional(immediate=True)
def _mark_intent_sent(intent_id: int, claim_token: str) -> None:
    row = db.one(
        """SELECT client_id, to_email, subject FROM rebooking_email_intents
           WHERE id=? AND studio_id=? AND status='claimed' AND claim_token=?""",
        (intent_id, STUDIO_ID, claim_token),
    )
    if not row:
        raise RuntimeError("rebooking delivery claim is no longer active")
    db.run(
        """UPDATE rebooking_email_intents
           SET status='sent', sent_at=datetime('now'), error=NULL, claim_token='',
               updated_at=datetime('now')
           WHERE id=? AND studio_id=? AND status='claimed' AND claim_token=?""",
        (intent_id, STUDIO_ID, claim_token),
    )
    db.run(
        """INSERT INTO emails_log (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
           VALUES (?,?,?,?,?,?)""",
        (STUDIO_ID, None, "rebooking", row["client_id"], row["to_email"], row["subject"]),
    )
    db.audit(
        "admin",
        ACTION_SENT,
        f"{_detail(row['client_id'], row['to_email'], row['subject'])}; intent_id={intent_id}",
    )


@db.transactional(immediate=True)
def reconcile_intent(intent_id: int, *, client_id: int, delivered: bool) -> None:
    row = db.one(
        """SELECT * FROM rebooking_email_intents
           WHERE id=? AND studio_id=? AND client_id=?
             AND status IN ('claimed','unknown')""",
        (intent_id, STUDIO_ID, client_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="rebooking delivery intent not found")
    stale_claim = row["claimed_at"] <= (
        dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(minutes=_CLAIM_STALE_MINUTES)
    ).strftime("%Y-%m-%d %H:%M:%S")
    if row["status"] == "claimed" and not stale_claim:
        raise HTTPException(status_code=409, detail="delivery is still in progress")

    if not delivered:
        db.run(
            """UPDATE rebooking_email_intents
               SET status='failed', error='Operator confirmed provider did not deliver',
                   claim_token='', updated_at=datetime('now')
               WHERE id=? AND studio_id=?""",
            (intent_id, STUDIO_ID),
        )
        db.audit(
            "admin",
            ACTION_FAILED,
            f"{_detail(client_id, row['to_email'], row['subject'])}; intent_id={intent_id}; reconciled=not-delivered",
        )
        return

    db.run(
        """UPDATE rebooking_email_intents
           SET status='sent', sent_at=COALESCE(sent_at, datetime('now')),
               error=NULL, claim_token='', updated_at=datetime('now')
           WHERE id=? AND studio_id=?""",
        (intent_id, STUDIO_ID),
    )
    db.run(
        """INSERT INTO emails_log (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
           SELECT ?, NULL, 'rebooking', ?, ?, ?
           WHERE NOT EXISTS (
               SELECT 1 FROM audit_log
               WHERE studio_id=? AND action=? AND detail LIKE ?
           )""",
        (
            STUDIO_ID,
            client_id,
            row["to_email"],
            row["subject"],
            STUDIO_ID,
            ACTION_SENT,
            f"%; intent_id={intent_id};%",
        ),
    )
    db.audit(
        "admin",
        ACTION_SENT,
        f"{_detail(client_id, row['to_email'], row['subject'])}; intent_id={intent_id}; reconciled=delivered",
    )


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


def _latest_sent_rows(*, days: int = 90) -> list[Any]:
    rows = db.all_(
        """SELECT action, detail, created_at
           FROM audit_log
           WHERE studio_id=? AND action=?
             AND created_at >= datetime('now', ?)
           ORDER BY created_at DESC""",
        (STUDIO_ID, ACTION_SENT, f"-{days} days"),
    )
    latest: dict[int, Any] = {}
    for row in rows:
        client_id = _client_id_from_detail(row["detail"])
        if client_id is None or client_id in latest:
            continue
        latest[client_id] = row
    return list(latest.values())


def follow_up_queue(
    *, follow_up_days: int = FOLLOW_UP_DAYS, lookback_days: int = 90, limit: int = 8
) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for row in _latest_sent_rows(days=lookback_days):
        days_waiting = _days_since_timestamp(row["created_at"])
        if days_waiting is None or days_waiting < follow_up_days:
            continue
        if _conversion_for_outreach(row):
            continue
        client_id = _client_id_from_detail(row["detail"])
        if client_id is None:
            continue
        client = db.one(
            "SELECT id, name, company, email FROM clients WHERE id=? AND studio_id=? AND client_type='agent'",
            (client_id, STUDIO_ID),
        )
        if not client:
            continue
        opportunity = churn.rebooking_for_client(client_id)
        can_send_again = not recent_sent_at(client_id)
        queue.append(
            {
                "id": client_id,
                "name": client["name"],
                "company": client["company"],
                "email": client["email"],
                "sent_at": row["created_at"],
                "days_waiting": days_waiting,
                "can_send_again": can_send_again,
                "cooldown_active": not can_send_again,
                "client_href": f"/admin/clients/{client_id}",
                "book_href": f"/admin/listings/new?client_id={client_id}",
                "next_action": "Send second touch" if can_send_again else "Manual check-in",
                "reason_line": f"nudged {days_waiting} days ago · no repeat listing yet",
                "priority": opportunity["priority"] if opportunity else "Warm",
                "priority_key": opportunity["priority_key"] if opportunity else "priority-warm",
            }
        )
        if len(queue) >= limit:
            break
    return queue


def performance_snapshot(
    opportunities: list[dict[str, Any]] | None = None,
    *,
    days: int = 30,
    followups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if opportunities is None:
        opportunities = decorate_opportunities(churn.rebooking_opportunities(limit=50))
    if followups is None:
        followups = follow_up_queue()
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
    follow_up_count = len(followups)
    if follow_up_count:
        next_action = (
            f"Follow up with {follow_up_count} nudged agent{'s' if follow_up_count != 1 else ''}."
        )
    elif ready_count:
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
        "follow_up_count": follow_up_count,
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
    days_since_sent = _days_since_timestamp(latest_sent)
    if latest_sent:
        conversion = db.one(
            """SELECT id, title, created_at FROM listings
               WHERE studio_id=? AND client_id=? AND created_at >= ?
               ORDER BY created_at ASC LIMIT 1""",
            (STUDIO_ID, client_id, latest_sent),
        )
    return {
        "events": rows,
        "delivery_review": unresolved_intent(client_id),
        "latest_sent_at": latest_sent,
        "days_since_sent": days_since_sent,
        "draft_count": sum(1 for row in rows if row["action"] == ACTION_DRAFT),
        "failed_count": sum(1 for row in rows if row["action"] == ACTION_FAILED),
        "follow_up_due": bool(
            latest_sent
            and not conversion
            and days_since_sent is not None
            and days_since_sent >= FOLLOW_UP_DAYS
        ),
        "can_send_again": bool(latest_sent and not recent_sent_at(client_id)),
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
    if not mailer.configured():
        db.audit("admin", ACTION_DRAFT, _detail(client_id, str(draft["to"]), str(draft["subject"])))
        return {"status": "draft", "draft": draft}

    claim = _claim_email_intent(draft, cooldown_days=cooldown_days)
    if claim["status"] != "claimed":
        return {**claim, "draft": draft}
    intent_id = int(claim["intent_id"])
    claim_token = str(claim["claim_token"])

    try:
        mailer.send_for_studio(str(draft["to"]), str(draft["subject"]), str(draft["body"]))
    except mailer.DeliveryOutcomeUnknown as exc:
        _mark_intent_failed(intent_id, claim_token, exc, outcome_unknown=True)
        raise HTTPException(
            status_code=502,
            detail="rebooking email outcome is unknown; verify the provider before retrying",
        ) from exc
    except Exception as exc:
        _mark_intent_failed(intent_id, claim_token, exc, outcome_unknown=False)
        raise HTTPException(status_code=502, detail="rebooking email failed") from exc

    try:
        _mark_intent_sent(intent_id, claim_token)
    except Exception as exc:
        try:
            _mark_intent_failed(intent_id, claim_token, exc, outcome_unknown=True)
        except Exception:
            # The durable claim remains closed to replay if persistence itself is unavailable.
            pass
        raise HTTPException(
            status_code=502,
            detail="rebooking email was accepted but local confirmation failed; verify the provider",
        ) from exc
    return {"status": "sent", "draft": draft}
