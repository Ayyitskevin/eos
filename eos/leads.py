"""Property-site buyer leads — capture, durable notification, studio inbox.

Leads live in the existing ``inquiries`` table with status 'inquiry' and a
listing link; booking orders keep their own statuses. Studio/agent
notifications are durable intents persisted before any provider I/O, claimed
with attempt fencing exactly like ``delivery_notify`` and ``analytics``
digests. Submitter IPs are stored only as truncated SHA-256 hashes.
"""

import csv
import hashlib
import io
import logging

from fastapi import HTTPException

from . import db, emails, mailer, security, tenant
from .vocab import STUDIO_ID

log = logging.getLogger("eos.leads")

LEAD_STATUS = "inquiry"
_CLAIM_TIMEOUT = "-15 minutes"
_UNKNOWN_OUTCOME = "Delivery outcome unknown; verify the provider before retrying."


def _ip_hash(ip: str) -> str:
    return hashlib.sha256(f"lead|{ip}".encode()).hexdigest()[:32]


def _recipient(studio_id: str, listing_id: int) -> str:
    """Notify the listing's agent when present, else the studio contact."""
    row = db.one(
        """SELECT c.email AS agent_email, s.contact_email AS studio_email
           FROM listings l
           LEFT JOIN clients c ON c.id=l.client_id AND c.studio_id=l.studio_id
           JOIN studio s ON s.id=l.studio_id
           WHERE l.id=? AND l.studio_id=?""",
        (listing_id, studio_id),
    )
    if not row:
        return ""
    return (row["agent_email"] or "").strip() or (row["studio_email"] or "").strip()


def capture_lead(
    *,
    listing,
    name: str,
    email: str,
    phone: str,
    message: str,
    ip: str,
    address: str,
) -> int:
    """Store one microsite lead and its notification intent atomically."""
    studio_id = str(STUDIO_ID)
    with db.tx(immediate=True):
        inquiry_id = db.run(
            """INSERT INTO inquiries
               (studio_id, name, email, phone, message, property_address, status,
                listing_id, ip_hash)
               VALUES (?,?,?,?,?,?,'inquiry',?,?)""",
            (
                studio_id,
                name.strip(),
                email.strip().lower(),
                phone.strip(),
                message.strip(),
                address,
                listing["id"],
                _ip_hash(ip),
            ),
        )
        to_email = _recipient(studio_id, listing["id"])
        if to_email:
            db.run(
                """INSERT OR IGNORE INTO lead_notify_intents
                   (studio_id, inquiry_id, to_email, subject, updated_at)
                   VALUES (?,?,?,?,datetime('now'))""",
                (studio_id, inquiry_id, to_email, f"New property lead — {address}"),
            )
    db.audit("public", "site.lead", f"listing={listing['id']}")
    return inquiry_id


def _fail_stale_claims() -> int:
    with db.tx(immediate=True) as con:
        cur = con.execute(
            f"""UPDATE lead_notify_intents
                SET status='failed', error=?, claimed_at=NULL, attempt_token=NULL,
                    updated_at=datetime('now')
                WHERE status='pending'
                  AND claimed_at < datetime('now','{_CLAIM_TIMEOUT}')""",
            (_UNKNOWN_OUTCOME,),
        )
        return cur.rowcount


def _claim_intent(intent_id: int, studio_id: str):
    attempt_token = security.new_token()
    con = db.connect()
    try:
        cur = con.execute(
            """UPDATE lead_notify_intents
                SET claimed_at=datetime('now'), attempts=attempts+1,
                    attempt_token=?, updated_at=datetime('now')
                WHERE id=? AND studio_id=? AND status='pending'
                  AND claimed_at IS NULL""",
            (attempt_token, intent_id, studio_id),
        )
        if cur.rowcount != 1:
            con.commit()
            return None
        row = con.execute(
            "SELECT * FROM lead_notify_intents WHERE id=? AND studio_id=?",
            (intent_id, studio_id),
        ).fetchone()
        con.commit()
        return row
    finally:
        con.close()


def process_intent(intent_id: int, *, studio_id: str | None = None) -> bool:
    """Claim and send one lead notification; persist-first ordering is set at capture."""
    if not mailer.configured():
        return False
    claim_studio = studio_id or tenant.get_studio_id()
    intent = _claim_intent(intent_id, claim_studio)
    if not intent:
        return False
    previous_studio = tenant.get_studio_id()
    studio_id = intent["studio_id"]
    attempt_token = intent["attempt_token"]
    tenant.set_studio(studio_id)
    provider_accepted = False
    try:
        already_sent = db.one(
            """SELECT 1 AS x FROM emails_log
               WHERE studio_id=? AND doc_kind='lead_notify' AND doc_id=? LIMIT 1""",
            (studio_id, intent_id),
        )
        if already_sent:
            with db.tx(immediate=True) as con:
                con.execute(
                    """UPDATE lead_notify_intents
                       SET status='sent', error=NULL, claimed_at=NULL, attempt_token=NULL,
                           updated_at=datetime('now')
                       WHERE id=? AND studio_id=? AND status='pending'
                         AND attempt_token=?""",
                    (intent_id, studio_id, attempt_token),
                )
            return False
        lead = db.one(
            """SELECT q.*, l.site_slug
               FROM inquiries q
               LEFT JOIN listings l ON l.id=q.listing_id AND l.studio_id=q.studio_id
               WHERE q.id=? AND q.studio_id=?""",
            (intent["inquiry_id"], studio_id),
        )
        if not lead:
            raise RuntimeError("lead is missing")
        site_url = (
            f"{tenant.get_base_url()}/l/{lead['site_slug']}"
            if lead["site_slug"]
            else tenant.get_base_url()
        )
        subject, body = emails.lead_notification(
            name=lead["name"],
            email=lead["email"],
            phone=lead["phone"],
            message=lead["message"],
            address=lead["property_address"],
            site_url=site_url,
        )
        mailer.send_for_studio(intent["to_email"], subject, body)
        provider_accepted = True
        with db.tx(immediate=True) as con:
            updated = con.execute(
                """UPDATE lead_notify_intents
                   SET status='sent', sent_at=datetime('now'), error=NULL,
                       claimed_at=NULL, attempt_token=NULL, updated_at=datetime('now')
                   WHERE id=? AND studio_id=? AND status='pending'
                     AND attempt_token=?""",
                (intent_id, studio_id, attempt_token),
            )
            if updated.rowcount != 1:
                raise RuntimeError("lead notification claim was lost after provider acceptance")
            con.execute(
                """INSERT INTO emails_log
                   (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
                   VALUES (?,?,?,?,?,?)""",
                (
                    studio_id,
                    lead["listing_id"],
                    "lead_notify",
                    intent_id,
                    intent["to_email"],
                    subject,
                ),
            )
        log.info("sent lead notification %s to %s", intent_id, intent["to_email"])
        return True
    except Exception as exc:
        error = str(exc)[:500]
        if provider_accepted:
            error = f"{_UNKNOWN_OUTCOME} {error}"[:500]
        with db.tx(immediate=True) as con:
            con.execute(
                """UPDATE lead_notify_intents
                   SET status='failed', error=?, claimed_at=NULL, attempt_token=NULL,
                       updated_at=datetime('now')
                   WHERE id=? AND studio_id=? AND status='pending'
                     AND attempt_token=?""",
                (error, intent_id, studio_id, attempt_token),
            )
        log.error("lead notification %s failed: %s", intent_id, exc)
        return False
    finally:
        tenant.set_studio(previous_studio)


def process_pending(limit: int = 20) -> int:
    _fail_stale_claims()
    if not mailer.configured():
        return 0
    pending = db.all_(
        """SELECT id, studio_id FROM lead_notify_intents
            WHERE status='pending' AND claimed_at IS NULL
            ORDER BY created_at, id LIMIT ?""",
        (limit,),
    )
    return sum(1 for row in pending if process_intent(row["id"], studio_id=row["studio_id"]))


def list_leads(listing_id: int | None = None, limit: int = 200):
    sql = """SELECT q.*, l.title AS listing_title, l.address_line1
             FROM inquiries q
             LEFT JOIN listings l ON l.id=q.listing_id AND l.studio_id=q.studio_id
             WHERE q.studio_id=? AND q.status='inquiry'"""
    params: list = [str(STUDIO_ID)]
    if listing_id is not None:
        sql += " AND q.listing_id=?"
        params.append(listing_id)
    sql += " ORDER BY q.created_at DESC, q.id DESC LIMIT ?"
    params.append(limit)
    return db.all_(sql, tuple(params))


def listing_lead_count(listing_id: int) -> int:
    row = db.one(
        """SELECT COUNT(*) AS n FROM inquiries
           WHERE studio_id=? AND listing_id=? AND status='inquiry'""",
        (STUDIO_ID, listing_id),
    )
    return int(row["n"] if row else 0)


def set_contacted(inquiry_id: int, contacted: bool) -> None:
    with db.tx() as con:
        cur = con.execute(
            """UPDATE inquiries SET contacted=?
               WHERE id=? AND studio_id=? AND status='inquiry'""",
            (1 if contacted else 0, inquiry_id, str(STUDIO_ID)),
        )
        if cur.rowcount != 1:
            raise HTTPException(status_code=404)
    db.audit("admin", "lead.contacted", f"inquiry={inquiry_id} contacted={1 if contacted else 0}")


def leads_csv() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["created_at", "listing_id", "listing", "name", "email", "phone", "message", "contacted"]
    )
    for row in list_leads(limit=5000):
        writer.writerow(
            [
                row["created_at"],
                row["listing_id"] or "",
                row["address_line1"] or row["listing_title"] or "",
                row["name"],
                row["email"],
                row["phone"],
                row["message"],
                "yes" if row["contacted"] else "no",
            ]
        )
    return buf.getvalue()
