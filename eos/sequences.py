"""Email drip sequences — schedule on listing events, send via scheduler."""

import datetime as dt
import logging
import re

from . import db, mailer, tenant
from .tenant import get_base_url, get_site_name
from .vocab import STUDIO_ID

log = logging.getLogger("eos.sequences")

_VAR_RE = re.compile(r"\{(\w+)\}")
TRIGGER_EVENTS = ("listing.booked", "listing.delivered", "proposal.sent")


def _client_for_listing(listing_id: int):
    row = db.one(
        """SELECT c.id, c.name, c.email, c.company, l.title, l.address_line1, l.city
           FROM listings l
           LEFT JOIN clients c ON c.id=l.client_id AND c.studio_id=l.studio_id
           WHERE l.id=? AND l.studio_id=?""",
        (listing_id, STUDIO_ID),
    )
    return row


def _gallery_for_listing(listing_id: int):
    return db.one(
        """SELECT slug, pin FROM galleries
           WHERE listing_id=? AND studio_id=? AND published=1
           ORDER BY created_at DESC LIMIT 1""",
        (listing_id, STUDIO_ID),
    )


def _proposal_for_listing(listing_id: int):
    return db.one(
        """SELECT slug FROM proposals
           WHERE listing_id=? AND studio_id=? AND status='sent'
           ORDER BY sent_at DESC LIMIT 1""",
        (listing_id, STUDIO_ID),
    )


def _intake_for_listing(listing_id: int):
    return db.one(
        """SELECT token FROM questionnaires
           WHERE listing_id=? AND studio_id=? AND status='pending'
           ORDER BY created_at DESC LIMIT 1""",
        (listing_id, STUDIO_ID),
    )


def build_context(listing_id: int, extra: dict | None = None) -> dict:
    row = _client_for_listing(listing_id)
    gallery = _gallery_for_listing(listing_id)
    proposal = _proposal_for_listing(listing_id)
    intake = _intake_for_listing(listing_id)
    client_name = row["name"] if row and row["name"] else "there"
    ctx = {
        "site_name": get_site_name(),
        "client_name": client_name,
        "client_first": client_name.split()[0] if client_name else "there",
        "client_email": row["email"] if row else "",
        "listing_title": row["title"] if row else "",
        "listing_address": ", ".join(p for p in (row["address_line1"], row["city"]) if row and p)
        if row
        else "",
        "gallery_link": f"{get_base_url()}/g/{gallery['slug']}" if gallery else "",
        "gallery_pin": gallery["pin"] if gallery else "",
        "proposal_link": f"{get_base_url()}/p/{proposal['slug']}" if proposal else "",
        "intake_link": f"{get_base_url()}/q/{intake['token']}" if intake else "",
    }
    if extra:
        ctx.update(extra)
    return ctx


def render_template(template: str, ctx: dict) -> str:
    def repl(m):
        return str(ctx.get(m.group(1), ""))

    return _VAR_RE.sub(repl, template)


def trigger(event: str, listing_id: int) -> int:
    """Schedule all active sequences matching event. Returns count scheduled."""
    if not mailer.configured():
        return 0
    ctx = build_context(listing_id)
    to_email = ctx.get("client_email", "").strip()
    if not to_email:
        log.debug("sequence trigger %s skipped listing %s (no client email)", event, listing_id)
        return 0
    seqs = db.all_(
        """SELECT * FROM email_sequences
           WHERE studio_id=? AND trigger_event=? AND active=1 ORDER BY position""",
        (STUDIO_ID, event),
    )
    scheduled = 0
    now = dt.datetime.now()
    client_id = db.one(
        "SELECT client_id FROM listings WHERE id=? AND studio_id=?",
        (listing_id, STUDIO_ID),
    )
    cid = client_id["client_id"] if client_id else None
    for seq in seqs:
        due = now + dt.timedelta(hours=seq["delay_hours"])
        db.run(
            """INSERT INTO email_sequence_runs
               (studio_id, sequence_id, listing_id, client_id, to_email, scheduled_at)
               VALUES (?,?,?,?,?,?)""",
            (STUDIO_ID, seq["id"], listing_id, cid, to_email, due.strftime("%Y-%m-%d %H:%M:%S")),
        )
        scheduled += 1
    if scheduled:
        log.info("scheduled %d sequence runs for %s listing %s", scheduled, event, listing_id)
    return scheduled


def process_due() -> int:
    """Send due scheduled runs. Returns count sent."""
    if not mailer.configured():
        return 0
    due = db.all_(
        """SELECT r.*, s.subject, s.body_template, s.channel
           FROM email_sequence_runs r
           JOIN email_sequences s ON s.id=r.sequence_id AND s.studio_id=r.studio_id
           WHERE r.status='scheduled' AND r.scheduled_at <= datetime('now')
           ORDER BY r.scheduled_at LIMIT 20""",
    )
    sent = 0
    original_studio = tenant.get_studio_id()
    try:
        for run in due:
            tenant.set_studio(run["studio_id"])
            try:
                if run["listing_id"] is not None and not db.one(
                    "SELECT id FROM listings WHERE id=? AND studio_id=?",
                    (run["listing_id"], STUDIO_ID),
                ):
                    raise RuntimeError("sequence listing is not in this studio")
                ctx = build_context(run["listing_id"])
                subject = render_template(run["subject"], ctx)
                body = render_template(run["body_template"], ctx)
                channel = run["channel"] or "email"
                if channel == "sms":
                    from . import sms

                    phone_row = db.one(
                        "SELECT phone FROM clients WHERE id=? AND studio_id=?",
                        (run["client_id"], STUDIO_ID),
                    )
                    phone = phone_row["phone"] if phone_row else ""
                    if not phone or not sms.send(to_phone=phone, body=body[:500]):
                        raise RuntimeError("SMS delivery failed or no phone")
                else:
                    mailer.send_for_studio(run["to_email"], subject, body)
                db.run(
                    """UPDATE email_sequence_runs
                       SET status='sent', sent_at=datetime('now'), error=NULL
                       WHERE id=? AND studio_id=?""",
                    (run["id"], STUDIO_ID),
                )
                db.run(
                    """INSERT INTO emails_log (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        STUDIO_ID,
                        run["listing_id"],
                        "sequence",
                        run["sequence_id"],
                        run["to_email"],
                        subject,
                    ),
                )
                sent += 1
            except Exception as e:
                db.run(
                    "UPDATE email_sequence_runs SET status='failed', error=? WHERE id=? AND studio_id=?",
                    (str(e)[:500], run["id"], STUDIO_ID),
                )
                log.error("sequence run %s failed: %s", run["id"], e)
    finally:
        tenant.set_studio(original_studio)
    return sent


def list_sequences():
    return db.all_(
        "SELECT * FROM email_sequences WHERE studio_id=? ORDER BY position",
        (STUDIO_ID,),
    )


def list_pending_runs(limit: int = 30):
    return db.all_(
        """SELECT r.*, s.name AS sequence_name, l.title AS listing_title
           FROM email_sequence_runs r
           JOIN email_sequences s ON s.id=r.sequence_id AND s.studio_id=r.studio_id
           LEFT JOIN listings l ON l.id=r.listing_id AND l.studio_id=r.studio_id
           WHERE r.studio_id=? AND r.status IN ('scheduled','failed')
           ORDER BY r.scheduled_at LIMIT ?""",
        (STUDIO_ID, limit),
    )


def get_run(run_id: int):
    from fastapi import HTTPException

    row = db.one(
        "SELECT * FROM email_sequence_runs WHERE id=? AND studio_id=?",
        (run_id, STUDIO_ID),
    )
    if not row:
        raise HTTPException(status_code=404)
    return row


def cancel_run(run_id: int) -> None:
    get_run(run_id)
    db.run(
        """UPDATE email_sequence_runs SET status='canceled'
           WHERE id=? AND studio_id=? AND status='scheduled'""",
        (run_id, STUDIO_ID),
    )


def toggle_sequence(seq_id: int, active: bool) -> None:
    get_sequence(seq_id)
    db.run(
        "UPDATE email_sequences SET active=? WHERE id=? AND studio_id=?",
        (1 if active else 0, seq_id, STUDIO_ID),
    )


def get_sequence(seq_id: int):
    from fastapi import HTTPException

    row = db.one(
        "SELECT * FROM email_sequences WHERE id=? AND studio_id=?",
        (seq_id, STUDIO_ID),
    )
    if not row:
        raise HTTPException(status_code=404)
    return row


def update_sequence(
    seq_id: int,
    *,
    name: str,
    subject: str,
    body_template: str,
    delay_hours: int,
    trigger_event: str,
) -> None:
    get_sequence(seq_id)
    db.run(
        """UPDATE email_sequences SET name=?, subject=?, body_template=?, delay_hours=?, trigger_event=?
           WHERE id=? AND studio_id=?""",
        (
            name.strip(),
            subject.strip(),
            body_template,
            delay_hours,
            trigger_event.strip(),
            seq_id,
            STUDIO_ID,
        ),
    )
    db.audit("admin", "sequence.update", f"id={seq_id}")
