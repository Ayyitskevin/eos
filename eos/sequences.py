"""Email drip sequences — schedule on listing events, send via scheduler."""

import datetime as dt
import logging
import re

from . import db, mailer, security, tenant
from .tenant import get_base_url, get_site_name
from .vocab import STUDIO_ID

log = logging.getLogger("eos.sequences")

_VAR_RE = re.compile(r"\{(\w+)\}")
TRIGGER_EVENTS = ("listing.booked", "listing.delivered", "proposal.sent")
_UNKNOWN_OUTCOME = "Delivery outcome unknown; verify the provider before retrying."
_NOT_DELIVERED = "Operator confirmed the sequence email was not delivered."


class SequenceRunNotFound(LookupError):
    """The requested run does not belong to the active studio."""


class SequenceRunReconciliationConflict(RuntimeError):
    """The run cannot be safely reconciled in its current state."""


def _is_unknown_outcome(error: str | None) -> bool:
    normalized = (error or "").strip().lower()
    return normalized.startswith(_UNKNOWN_OUTCOME.lower()) or normalized.startswith(
        "email outcome unknown after "
    )


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
    repeat = {"rebook_url": "", "referral_url": ""}
    if row and row["id"] is not None:
        from . import portal

        repeat = portal.repeat_links(row["id"])
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
        "rebook_link": repeat["rebook_url"],
        "referral_link": repeat["referral_url"] or "",
    }
    if extra:
        ctx.update(extra)
    return ctx


def render_template(template: str, ctx: dict) -> str:
    def repl(m):
        return str(ctx.get(m.group(1), ""))

    return _VAR_RE.sub(repl, template)


def _event_key(event: str, listing_id: int, explicit: str | None) -> str:
    key = (explicit or f"{event}:listing:{listing_id}").strip()
    if not key or len(key) > 200:
        raise ValueError("invalid sequence event key")
    return key


def trigger(event: str, listing_id: int, *, event_key: str | None = None) -> int:
    """Schedule active sequences once for a stable listing event."""
    ctx = build_context(listing_id)
    to_email = (ctx.get("client_email") or "").strip()
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
    stable_key = _event_key(event, listing_id, event_key)
    client_id = db.one(
        "SELECT client_id FROM listings WHERE id=? AND studio_id=?",
        (listing_id, STUDIO_ID),
    )
    cid = client_id["client_id"] if client_id else None
    with db.tx() as con:
        for seq in seqs:
            due = now + dt.timedelta(hours=seq["delay_hours"])
            cur = con.execute(
                """INSERT OR IGNORE INTO email_sequence_runs
                   (studio_id, sequence_id, listing_id, client_id, to_email, scheduled_at,
                    event_key)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    STUDIO_ID,
                    seq["id"],
                    listing_id,
                    cid,
                    to_email,
                    due.strftime("%Y-%m-%d %H:%M:%S"),
                    stable_key,
                ),
            )
            scheduled += cur.rowcount
    if scheduled:
        log.info("scheduled %d sequence runs for %s listing %s", scheduled, event, listing_id)
    return scheduled


def _fail_stale_claims() -> int:
    """Surface interrupted provider sends instead of automatically replaying them."""
    with db.tx(immediate=True) as con:
        cur = con.execute(
            """UPDATE email_sequence_runs
               SET status='failed', error=?, claimed_at=NULL, attempt_token=NULL
               WHERE status='scheduled'
                 AND claimed_at < datetime('now','-15 minutes')""",
            (_UNKNOWN_OUTCOME,),
        )
        return cur.rowcount


def _claim_run(run_id: int, studio_id: str):
    attempt_token = security.new_token()
    con = db.connect()
    try:
        cur = con.execute(
            """UPDATE email_sequence_runs
               SET claimed_at=datetime('now'), attempts=attempts+1, attempt_token=?
               WHERE id=? AND studio_id=? AND status='scheduled'
                 AND scheduled_at <= datetime('now') AND claimed_at IS NULL""",
            (attempt_token, run_id, studio_id),
        )
        if cur.rowcount != 1:
            con.commit()
            return None
        row = con.execute(
            """SELECT r.*, s.subject, s.body_template, s.channel
               FROM email_sequence_runs r
               JOIN email_sequences s
                 ON s.id=r.sequence_id AND s.studio_id=r.studio_id
               WHERE r.id=? AND r.studio_id=?""",
            (run_id, studio_id),
        ).fetchone()
        con.commit()
        return row
    finally:
        con.close()


def _release_unconfigured_claim(run_id: int, studio_id: str, attempt_token: str) -> None:
    db.run(
        """UPDATE email_sequence_runs SET claimed_at=NULL, attempt_token=NULL
           WHERE id=? AND studio_id=? AND status='scheduled' AND attempt_token=?""",
        (run_id, studio_id, attempt_token),
    )


def process_due(limit: int = 20) -> int:
    """Claim and send due sequence runs. Returns count sent."""
    _fail_stale_claims()
    due = db.all_(
        """SELECT id, studio_id FROM email_sequence_runs
           WHERE status='scheduled' AND scheduled_at <= datetime('now')
             AND claimed_at IS NULL
           ORDER BY scheduled_at LIMIT ?""",
        (limit,),
    )
    sent = 0
    original_studio = tenant.get_studio_id()
    try:
        for candidate in due:
            run = _claim_run(candidate["id"], candidate["studio_id"])
            if not run:
                continue
            tenant.set_studio(run["studio_id"])
            attempt_token = run["attempt_token"]
            provider_accepted = False
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

                    if not sms.configured():
                        _release_unconfigured_claim(run["id"], STUDIO_ID, attempt_token)
                        continue
                    phone_row = db.one(
                        "SELECT phone FROM clients WHERE id=? AND studio_id=?",
                        (run["client_id"], STUDIO_ID),
                    )
                    phone = phone_row["phone"] if phone_row else ""
                    if not phone or not sms.send(to_phone=phone, body=body[:500]):
                        raise RuntimeError("SMS delivery failed or no phone")
                    provider_accepted = True
                else:
                    if not mailer.configured():
                        _release_unconfigured_claim(run["id"], STUDIO_ID, attempt_token)
                        continue
                    mailer.send_for_studio(run["to_email"], subject, body)
                    provider_accepted = True
                with db.tx(immediate=True) as con:
                    updated = con.execute(
                        """UPDATE email_sequence_runs
                           SET status='sent', sent_at=datetime('now'),
                               error=NULL, claimed_at=NULL, attempt_token=NULL
                           WHERE id=? AND studio_id=? AND status='scheduled'
                             AND attempt_token=?""",
                        (run["id"], STUDIO_ID, attempt_token),
                    )
                    if updated.rowcount != 1:
                        raise RuntimeError("sequence claim was lost after provider acceptance")
                    con.execute(
                        """INSERT INTO emails_log
                           (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
                           VALUES (?,?,?,?,?,?)""",
                        (
                            STUDIO_ID,
                            run["listing_id"],
                            "sequence",
                            run["id"],
                            run["to_email"],
                            subject,
                        ),
                    )
                sent += 1
            except Exception as exc:
                error = str(exc)[:500]
                if provider_accepted:
                    error = (_UNKNOWN_OUTCOME + " " + error)[:500]
                with db.tx(immediate=True) as con:
                    con.execute(
                        """UPDATE email_sequence_runs
                           SET status='failed', error=?, claimed_at=NULL, attempt_token=NULL
                           WHERE id=? AND studio_id=? AND status='scheduled'
                             AND attempt_token=?""",
                        (error, run["id"], STUDIO_ID, attempt_token),
                    )
                log.error("sequence run %s failed: %s", run["id"], exc)
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
        """SELECT r.*, s.name AS sequence_name, s.channel, l.title AS listing_title,
                  CASE WHEN r.status='failed' AND r.claimed_at IS NULL
                         AND r.attempt_token IS NULL
                         AND (lower(COALESCE(r.error,'')) LIKE 'delivery outcome unknown;%'
                              OR lower(COALESCE(r.error,'')) LIKE
                                 'email outcome unknown after %')
                         AND COALESCE(s.channel,'email')='email'
                       THEN 1 ELSE 0 END AS reconcile_ready
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
        """UPDATE email_sequence_runs
           SET status='canceled', claimed_at=NULL, attempt_token=NULL
           WHERE id=? AND studio_id=? AND status='scheduled' AND claimed_at IS NULL""",
        (run_id, STUDIO_ID),
    )


def retry_run(run_id: int) -> bool:
    get_run(run_id)
    with db.tx() as con:
        cur = con.execute(
            """UPDATE email_sequence_runs
               SET status='scheduled', scheduled_at=datetime('now'), error=NULL,
                   claimed_at=NULL, attempt_token=NULL
               WHERE id=? AND studio_id=? AND status='failed'
                 AND claimed_at IS NULL AND attempt_token IS NULL
                 AND NOT (lower(COALESCE(error,'')) LIKE 'delivery outcome unknown;%'
                          OR lower(COALESCE(error,'')) LIKE
                             'email outcome unknown after %')""",
            (run_id, STUDIO_ID),
        )
        if cur.rowcount:
            db.audit("admin", "sequence.retry", f"run={run_id}")
        return cur.rowcount == 1


def reconcile_run(run_id: int, *, delivered: bool) -> None:
    """Record a provider-verified email outcome without sending again."""
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True) as con:
        row = con.execute(
            """SELECT r.*, s.subject, COALESCE(s.channel,'email') AS channel
               FROM email_sequence_runs r
               JOIN email_sequences s ON s.id=r.sequence_id AND s.studio_id=r.studio_id
               WHERE r.id=? AND r.studio_id=?""",
            (run_id, studio_id),
        ).fetchone()
        if not row:
            raise SequenceRunNotFound("sequence run not found")
        if row["claimed_at"] is not None or row["attempt_token"] is not None:
            raise SequenceRunReconciliationConflict("sequence run still has an active claim")
        if row["channel"] != "email":
            raise SequenceRunReconciliationConflict(
                "only email sequence outcomes can be reconciled here"
            )
        if row["status"] != "failed" or not _is_unknown_outcome(row["error"]):
            raise SequenceRunReconciliationConflict(
                "only unknown sequence email outcomes can be reconciled"
            )

        if delivered:
            cur = con.execute(
                """UPDATE email_sequence_runs
                   SET status='sent', sent_at=COALESCE(sent_at,datetime('now')),
                       error=NULL, claimed_at=NULL, attempt_token=NULL
                   WHERE id=? AND studio_id=? AND status='failed' AND error=?
                     AND claimed_at IS NULL AND attempt_token IS NULL""",
                (run_id, studio_id, row["error"]),
            )
            if cur.rowcount != 1:
                raise SequenceRunReconciliationConflict(
                    "sequence email reconciliation state changed"
                )
            con.execute(
                """INSERT INTO emails_log
                   (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
                   SELECT ?, ?, 'sequence', ?, ?, ?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM emails_log
                       WHERE studio_id=? AND doc_kind='sequence' AND doc_id=?
                   )""",
                (
                    studio_id,
                    row["listing_id"],
                    run_id,
                    row["to_email"],
                    row["subject"],
                    studio_id,
                    run_id,
                ),
            )
            action = "sequence.reconcile.delivered"
        else:
            cur = con.execute(
                """UPDATE email_sequence_runs
                   SET error=?, claimed_at=NULL, attempt_token=NULL
                   WHERE id=? AND studio_id=? AND status='failed' AND error=?
                     AND claimed_at IS NULL AND attempt_token IS NULL""",
                (_NOT_DELIVERED, run_id, studio_id, row["error"]),
            )
            if cur.rowcount != 1:
                raise SequenceRunReconciliationConflict(
                    "sequence email reconciliation state changed"
                )
            action = "sequence.reconcile.not_delivered"
        db.audit("admin", action, f"run={run_id}")


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
