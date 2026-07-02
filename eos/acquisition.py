"""Agent acquisition and referral tracking reports."""

import csv
import io
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException

from . import clients, db, mailer, tenant
from .vocab import STUDIO_ID

COOLDOWN_DAYS = 14
ACTION_SENT = "acquisition.intro.sent"
ACTION_DRAFT = "acquisition.intro.draft"
ACTION_FAILED = "acquisition.intro.failed"
QUEUE_FILTERS = {
    "all": "All",
    "needs_code": "Needs code",
    "needs_intro": "Needs intro",
    "ready": "Ready",
    "cooldown": "Cooldown",
}


def _money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${dollars:,.0f}"
    return f"${dollars:,.2f}"


def _first(name: str) -> str:
    return (name or "there").split()[0]


def _detail(client_id: int, email: str, subject: str = "") -> str:
    return f"client_id={client_id}; email={email}; subject={subject[:120]}"


def recent_intro_sent_at(client_id: int, *, days: int = COOLDOWN_DAYS) -> str | None:
    row = db.one(
        """SELECT created_at FROM audit_log
           WHERE studio_id=? AND action=? AND detail LIKE ?
             AND created_at >= datetime('now', ?)
           ORDER BY created_at DESC LIMIT 1""",
        (STUDIO_ID, ACTION_SENT, f"client_id={client_id};%", f"-{days} days"),
    )
    return row["created_at"] if row else None


def _active_referral_code(client_id: int) -> dict[str, Any] | None:
    row = db.one(
        """SELECT code, credit_cents
           FROM referral_codes
           WHERE studio_id=? AND referrer_client_id=? AND active=1
           ORDER BY created_at DESC LIMIT 1""",
        (STUDIO_ID, client_id),
    )
    return dict(row) if row else None


def _agent_value(client_id: int) -> dict:
    row = db.one(
        """SELECT c.id, c.name, c.company, c.email,
                  parent.name AS brokerage_name,
                  (SELECT COUNT(*)
                     FROM listings l
                    WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS n_listings,
                  (SELECT MAX(l.created_at)
                     FROM listings l
                    WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS last_listing_at,
                  COALESCE((SELECT SUM(i.amount_cents)
                     FROM invoices i
                     LEFT JOIN listings li
                       ON li.id=i.listing_id AND li.studio_id=i.studio_id
                    WHERE i.studio_id=c.studio_id
                      AND i.status='paid'
                      AND (
                           i.agent_client_id=c.id
                           OR (i.agent_client_id IS NULL AND i.client_id=c.id)
                           OR (i.agent_client_id IS NULL AND i.client_id IS NULL AND li.client_id=c.id)
                      )), 0) AS paid_cents,
                  COALESCE((SELECT SUM(i.amount_cents)
                     FROM invoices i
                     LEFT JOIN listings li
                       ON li.id=i.listing_id AND li.studio_id=i.studio_id
                    WHERE i.studio_id=c.studio_id
                      AND i.status='sent'
                      AND (
                           i.agent_client_id=c.id
                           OR (i.agent_client_id IS NULL AND i.client_id=c.id)
                           OR (i.agent_client_id IS NULL AND i.client_id IS NULL AND li.client_id=c.id)
                      )), 0) AS open_cents
           FROM clients c
           LEFT JOIN clients parent
             ON parent.id=c.parent_id
            AND parent.studio_id=c.studio_id
            AND parent.client_type='brokerage'
           WHERE c.studio_id=? AND c.id=? AND c.client_type='agent'""",
        (STUDIO_ID, client_id),
    )
    if not row:
        return {}
    paid_cents = int(row["paid_cents"] or 0)
    open_cents = int(row["open_cents"] or 0)
    n_listings = int(row["n_listings"] or 0)
    return {
        "id": row["id"],
        "name": row["name"],
        "company": row["company"],
        "email": row["email"],
        "brokerage_name": row["brokerage_name"],
        "n_listings": n_listings,
        "last_listing_at": row["last_listing_at"],
        "paid_cents": paid_cents,
        "open_cents": open_cents,
        "paid_display": _money(paid_cents),
        "open_display": _money(open_cents),
        "client_href": f"/admin/clients/{row['id']}",
    }


def referral_performance(limit: int = 50) -> list[dict]:
    rows = db.all_(
        """SELECT r.id, r.code, r.credit_cents, r.referrer_client_id, r.uses,
                  r.max_uses, r.active, r.created_at,
                  ref.name AS referrer_name,
                  ref.company AS referrer_company,
                  (SELECT COUNT(*)
                     FROM inquiries q
                    WHERE q.studio_id=r.studio_id
                      AND upper(q.promo_code)=upper(r.code)) AS n_inquiries,
                  (SELECT COUNT(DISTINCT q.listing_id)
                     FROM inquiries q
                    WHERE q.studio_id=r.studio_id
                      AND q.listing_id IS NOT NULL
                      AND upper(q.promo_code)=upper(r.code)) AS n_listings,
                  (SELECT MAX(q.created_at)
                     FROM inquiries q
                    WHERE q.studio_id=r.studio_id
                      AND upper(q.promo_code)=upper(r.code)) AS last_used_at,
                  COALESCE((SELECT SUM(i.amount_cents)
                     FROM invoices i
                    WHERE i.studio_id=r.studio_id
                      AND i.status='paid'
                      AND EXISTS (
                          SELECT 1
                            FROM inquiries q
                           WHERE q.studio_id=i.studio_id
                             AND q.listing_id=i.listing_id
                             AND upper(q.promo_code)=upper(r.code)
                      )), 0) AS referred_paid_cents,
                  COALESCE((SELECT SUM(i.amount_cents)
                     FROM invoices i
                    WHERE i.studio_id=r.studio_id
                      AND i.status='sent'
                      AND EXISTS (
                          SELECT 1
                            FROM inquiries q
                           WHERE q.studio_id=i.studio_id
                             AND q.listing_id=i.listing_id
                             AND upper(q.promo_code)=upper(r.code)
                      )), 0) AS referred_open_cents
           FROM referral_codes r
           LEFT JOIN clients ref
             ON ref.id=r.referrer_client_id
            AND ref.studio_id=r.studio_id
           WHERE r.studio_id=?
           ORDER BY referred_paid_cents DESC, r.uses DESC, r.created_at DESC
           LIMIT ?""",
        (STUDIO_ID, limit),
    )
    out = []
    for row in rows:
        paid_cents = int(row["referred_paid_cents"] or 0)
        open_cents = int(row["referred_open_cents"] or 0)
        referrer = _agent_value(row["referrer_client_id"]) if row["referrer_client_id"] else {}
        out.append(
            {
                "id": row["id"],
                "code": row["code"],
                "credit_cents": int(row["credit_cents"] or 0),
                "credit_display": _money(int(row["credit_cents"] or 0)),
                "referrer_client_id": row["referrer_client_id"],
                "referrer_name": row["referrer_name"],
                "referrer_company": row["referrer_company"],
                "referrer": referrer,
                "uses": int(row["uses"] or 0),
                "max_uses": row["max_uses"],
                "active": bool(row["active"]),
                "created_at": row["created_at"],
                "n_inquiries": int(row["n_inquiries"] or 0),
                "n_listings": int(row["n_listings"] or 0),
                "last_used_at": row["last_used_at"],
                "referred_paid_cents": paid_cents,
                "referred_open_cents": open_cents,
                "referred_paid_display": _money(paid_cents),
                "referred_open_display": _money(open_cents),
                "referrer_href": f"/admin/clients/{row['referrer_client_id']}"
                if row["referrer_client_id"]
                else "",
            }
        )
    return out


def intro_ask_queue(limit: int = 12) -> list[dict]:
    rows = db.all_(
        """SELECT * FROM (
             SELECT c.id, c.name, c.company, c.email,
                    parent.name AS brokerage_name,
                    (SELECT COUNT(*)
                       FROM listings l
                      WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS n_listings,
                    (SELECT MAX(l.created_at)
                       FROM listings l
                      WHERE l.studio_id=c.studio_id AND l.client_id=c.id) AS last_listing_at,
                    (SELECT COUNT(*)
                       FROM referral_codes r
                      WHERE r.studio_id=c.studio_id
                        AND r.referrer_client_id=c.id
                        AND r.active=1) AS n_active_codes,
                    (SELECT COALESCE(SUM(r.uses), 0)
                       FROM referral_codes r
                      WHERE r.studio_id=c.studio_id
                        AND r.referrer_client_id=c.id
                        AND r.active=1) AS referral_uses,
                    (SELECT r.code
                       FROM referral_codes r
                      WHERE r.studio_id=c.studio_id
                        AND r.referrer_client_id=c.id
                        AND r.active=1
                      ORDER BY r.created_at DESC LIMIT 1) AS active_referral_code,
                    COALESCE((SELECT SUM(i.amount_cents)
                       FROM invoices i
                       LEFT JOIN listings li
                         ON li.id=i.listing_id AND li.studio_id=i.studio_id
                      WHERE i.studio_id=c.studio_id
                        AND i.status='paid'
                        AND (
                             i.agent_client_id=c.id
                             OR (i.agent_client_id IS NULL AND i.client_id=c.id)
                             OR (i.agent_client_id IS NULL AND i.client_id IS NULL AND li.client_id=c.id)
                        )), 0) AS paid_cents
               FROM clients c
               LEFT JOIN clients parent
                 ON parent.id=c.parent_id
                AND parent.studio_id=c.studio_id
                AND parent.client_type='brokerage'
              WHERE c.studio_id=? AND c.client_type='agent'
           ) agents
           WHERE (n_listings >= 2 OR paid_cents >= 40000)
             AND (n_active_codes = 0 OR referral_uses = 0)
           ORDER BY n_active_codes ASC, paid_cents DESC, n_listings DESC, last_listing_at DESC
           LIMIT ?""",
        (STUDIO_ID, limit),
    )
    out = []
    for row in rows:
        paid_cents = int(row["paid_cents"] or 0)
        n_active_codes = int(row["n_active_codes"] or 0)
        referral_uses = int(row["referral_uses"] or 0)
        recent = recent_intro_sent_at(row["id"])
        if n_active_codes == 0:
            action = "Create referral code"
            reason = "High-value agent has no referral code."
        else:
            action = "Ask for an introduction"
            reason = "Referral code exists but has not converted yet."
        out.append(
            {
                "id": row["id"],
                "name": row["name"],
                "company": row["company"],
                "email": row["email"],
                "brokerage_name": row["brokerage_name"],
                "n_listings": int(row["n_listings"] or 0),
                "last_listing_at": row["last_listing_at"],
                "paid_cents": paid_cents,
                "paid_display": _money(paid_cents),
                "n_active_codes": n_active_codes,
                "referral_uses": referral_uses,
                "active_referral_code": row["active_referral_code"],
                "last_acquisition_email_at": recent,
                "cooldown_active": bool(recent),
                "can_email_intro": bool(row["email"]) and not recent,
                "action": action,
                "reason": reason,
                "suggested_code": _suggested_code(row["name"]),
                "client_href": f"/admin/clients/{row['id']}",
            }
        )
    return out


def normalize_queue_filter(queue_filter: str | None) -> str:
    value = (queue_filter or "all").strip().lower()
    return value if value in QUEUE_FILTERS else "all"


def filter_intro_asks(asks: list[dict], queue_filter: str | None) -> list[dict]:
    selected = normalize_queue_filter(queue_filter)
    if selected == "needs_code":
        return [row for row in asks if row["n_active_codes"] == 0]
    if selected == "needs_intro":
        return [row for row in asks if row["n_active_codes"] > 0 and row["referral_uses"] == 0]
    if selected == "ready":
        return [row for row in asks if row["can_email_intro"]]
    if selected == "cooldown":
        return [row for row in asks if row["cooldown_active"]]
    return asks


def queue_filter_options(asks: list[dict], selected: str | None) -> list[dict[str, Any]]:
    current = normalize_queue_filter(selected)
    return [
        {
            "key": key,
            "label": label,
            "count": len(filter_intro_asks(asks, key)),
            "active": key == current,
        }
        for key, label in QUEUE_FILTERS.items()
    ]


def _suggested_code(name: str) -> str:
    letters = "".join(ch for ch in name.upper() if ch.isalnum())
    return f"{letters[:8] or 'AGENT'}25"


def build_intro_email(client_id: int) -> dict[str, str | int | None]:
    client = clients.get_client(client_id)
    if client["client_type"] != "agent":
        raise HTTPException(status_code=400, detail="acquisition outreach is only for agents")
    if not client["email"]:
        raise HTTPException(status_code=400, detail="agent email required")

    value = _agent_value(client_id)
    if not value:
        raise HTTPException(status_code=404)
    code = _active_referral_code(client_id)
    code_text = code["code"] if code else _suggested_code(client["name"])
    credit_text = _money(int(code["credit_cents"])) if code else "$25"
    booking_link = f"{tenant.get_base_url()}/book"
    if value["n_listings"]:
        history_line = (
            f"We have photographed {value['n_listings']} listing"
            f"{'s' if value['n_listings'] != 1 else ''} together."
        )
    else:
        history_line = "I have enjoyed working with you and your listings."
    if code:
        code_line = (
            f"Your referral code is {code_text}; anyone who books with it gets "
            f"{credit_text} tracked back to the introduction."
        )
    else:
        code_line = (
            f"I can set up {code_text} as your referral code with a {credit_text} credit "
            "for the next agent you introduce."
        )

    subject = "Know another agent who needs listing photos?"
    body = f"""Hi {_first(client["name"])},

{history_line} If another agent in your office needs reliable listing photos, I would appreciate the introduction.

{code_line}

Booking link:
{booking_link}

You can forward this link or reply with the agent's name and email and I will take it from there.

Thanks,
{tenant.get_site_name()}"""
    email = client["email"].strip()
    return {
        "client_id": client_id,
        "to": email,
        "subject": subject,
        "body": body,
        "suggested_code": code_text,
        "active_referral_code": code["code"] if code else None,
        "mailto_href": f"mailto:{email}?{urlencode({'subject': subject, 'body': body})}",
    }


def send_intro_email(client_id: int, *, cooldown_days: int = COOLDOWN_DAYS) -> dict[str, Any]:
    draft = build_intro_email(client_id)
    recent = recent_intro_sent_at(client_id, days=cooldown_days)
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
        raise HTTPException(status_code=502, detail="acquisition email failed") from exc

    db.run(
        """INSERT INTO emails_log (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
           VALUES (?,?,?,?,?,?)""",
        (STUDIO_ID, None, "acquisition_intro", client_id, draft["to"], draft["subject"]),
    )
    db.audit("admin", ACTION_SENT, _detail(client_id, str(draft["to"]), str(draft["subject"])))
    return {"status": "sent", "draft": draft}


def bulk_send_intro_emails(*, queue_filter: str = "ready", limit: int = 50) -> dict[str, int | str]:
    selected = normalize_queue_filter(queue_filter)
    candidates = filter_intro_asks(intro_ask_queue(limit=limit), selected)
    result: dict[str, int | str] = {
        "queue_filter": selected,
        "candidates": len(candidates),
        "sent": 0,
        "draft": 0,
        "cooldown": 0,
        "failed": 0,
        "skipped": 0,
    }
    for row in candidates:
        if not row["email"]:
            result["skipped"] += 1
            continue
        try:
            status = send_intro_email(row["id"])["status"]
        except HTTPException:
            result["failed"] += 1
            continue
        if status in {"sent", "draft", "cooldown"}:
            result[status] += 1
        else:
            result["skipped"] += 1
    return result


def _intro_status(client_id: int, email: str | None) -> dict[str, Any]:
    recent = recent_intro_sent_at(client_id)
    if recent:
        return {
            "last_acquisition_email_at": recent,
            "can_email_intro": False,
            "intro_status": f"emailed {recent[:10]}",
        }
    if email:
        return {
            "last_acquisition_email_at": None,
            "can_email_intro": True,
            "intro_status": "ready",
        }
    return {
        "last_acquisition_email_at": None,
        "can_email_intro": False,
        "intro_status": "missing email",
    }


def agent_referral_summary(
    referrals: list[dict] | None = None, *, limit: int = 50
) -> list[dict[str, Any]]:
    referrals = referrals if referrals is not None else referral_performance(limit=200)
    grouped: dict[int, dict[str, Any]] = {}
    for row in referrals:
        client_id = row["referrer_client_id"]
        if not client_id:
            continue
        referrer = row["referrer"] or _agent_value(client_id)
        item = grouped.setdefault(
            client_id,
            {
                "id": client_id,
                "name": row["referrer_name"] or referrer.get("name") or "Unknown agent",
                "company": row["referrer_company"] or referrer.get("company") or "",
                "email": referrer.get("email") or "",
                "brokerage_name": referrer.get("brokerage_name") or "",
                "client_href": f"/admin/clients/{client_id}",
                "codes": [],
                "code_count": 0,
                "active_code_count": 0,
                "uses": 0,
                "n_inquiries": 0,
                "n_listings": 0,
                "referred_paid_cents": 0,
                "referred_open_cents": 0,
                "last_used_at": None,
            },
        )
        item["codes"].append(row["code"])
        item["code_count"] += 1
        item["active_code_count"] += 1 if row["active"] else 0
        item["uses"] += row["uses"]
        item["n_inquiries"] += row["n_inquiries"]
        item["n_listings"] += row["n_listings"]
        item["referred_paid_cents"] += row["referred_paid_cents"]
        item["referred_open_cents"] += row["referred_open_cents"]
        if row["last_used_at"] and (
            item["last_used_at"] is None or row["last_used_at"] > item["last_used_at"]
        ):
            item["last_used_at"] = row["last_used_at"]

    out = []
    for item in grouped.values():
        item["code_list"] = ", ".join(item["codes"])
        item["referred_paid_display"] = _money(item["referred_paid_cents"])
        item["referred_open_display"] = _money(item["referred_open_cents"])
        item.update(_intro_status(item["id"], item["email"]))
        out.append(item)
    out.sort(
        key=lambda row: (
            row["referred_paid_cents"],
            row["uses"],
            row["n_listings"],
            row["last_used_at"] or "",
        ),
        reverse=True,
    )
    return out[:limit]


def summary(referrals: list[dict] | None = None, asks: list[dict] | None = None) -> dict:
    referrals = referrals if referrals is not None else referral_performance()
    asks = asks if asks is not None else intro_ask_queue()
    paid_cents = sum(row["referred_paid_cents"] for row in referrals)
    open_cents = sum(row["referred_open_cents"] for row in referrals)
    return {
        "n_codes": len(referrals),
        "n_active_codes": sum(1 for row in referrals if row["active"]),
        "n_referred_inquiries": sum(row["n_inquiries"] for row in referrals),
        "n_referred_listings": sum(row["n_listings"] for row in referrals),
        "n_intro_asks": len(asks),
        "n_intro_ready": sum(1 for row in asks if row["can_email_intro"]),
        "n_intro_cooldown": sum(1 for row in asks if row["cooldown_active"]),
        "referred_paid_cents": paid_cents,
        "referred_open_cents": open_cents,
        "referred_paid_display": _money(paid_cents),
        "referred_open_display": _money(open_cents),
    }


def dashboard(*, queue_filter: str = "all") -> dict:
    referrals = referral_performance()
    all_asks = intro_ask_queue()
    selected = normalize_queue_filter(queue_filter)
    asks = filter_intro_asks(all_asks, selected)
    return {
        "summary": summary(referrals, all_asks),
        "referrals": referrals,
        "intro_asks": asks,
        "all_intro_asks": all_asks,
        "queue_filter": selected,
        "queue_filters": queue_filter_options(all_asks, selected),
        "agent_referrals": agent_referral_summary(referrals),
    }


def acquisition_csv() -> str:
    data = dashboard()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Referral codes"])
    writer.writerow(
        [
            "code",
            "referrer",
            "referrer_company",
            "referrer_email",
            "uses",
            "max_uses",
            "credit_cents",
            "referred_inquiries",
            "referred_listings",
            "referred_paid_cents",
            "referred_open_cents",
            "last_used_at",
            "intro_status",
            "last_intro_sent_at",
        ]
    )
    for row in data["referrals"]:
        referrer = row["referrer"] or {}
        intro = (
            _intro_status(row["referrer_client_id"], referrer.get("email"))
            if row["referrer_client_id"]
            else {}
        )
        writer.writerow(
            [
                row["code"],
                row["referrer_name"] or "",
                row["referrer_company"] or "",
                referrer.get("email") or "",
                row["uses"],
                row["max_uses"] or "",
                row["credit_cents"],
                row["n_inquiries"],
                row["n_listings"],
                row["referred_paid_cents"],
                row["referred_open_cents"],
                (row["last_used_at"] or "")[:10],
                intro.get("intro_status", ""),
                (intro.get("last_acquisition_email_at") or "")[:10],
            ]
        )
    writer.writerow([])
    writer.writerow(["Agent referral summary"])
    writer.writerow(
        [
            "agent",
            "company",
            "email",
            "brokerage",
            "codes",
            "active_codes",
            "uses",
            "referred_inquiries",
            "referred_listings",
            "referred_paid_cents",
            "referred_open_cents",
            "last_used_at",
            "intro_status",
            "last_intro_sent_at",
        ]
    )
    for row in data["agent_referrals"]:
        writer.writerow(
            [
                row["name"],
                row["company"] or "",
                row["email"] or "",
                row["brokerage_name"] or "",
                row["code_list"],
                row["active_code_count"],
                row["uses"],
                row["n_inquiries"],
                row["n_listings"],
                row["referred_paid_cents"],
                row["referred_open_cents"],
                (row["last_used_at"] or "")[:10],
                row["intro_status"],
                (row["last_acquisition_email_at"] or "")[:10],
            ]
        )
    writer.writerow([])
    writer.writerow(["Intro ask queue"])
    writer.writerow(
        [
            "agent",
            "company",
            "brokerage",
            "listings",
            "paid_cents",
            "active_codes",
            "referral_uses",
            "suggested_code",
            "action",
        ]
    )
    for row in data["intro_asks"]:
        writer.writerow(
            [
                row["name"],
                row["company"] or "",
                row["brokerage_name"] or "",
                row["n_listings"],
                row["paid_cents"],
                row["n_active_codes"],
                row["referral_uses"],
                row["suggested_code"],
                row["action"],
            ]
        )
    return buf.getvalue()
