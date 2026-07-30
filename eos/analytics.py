"""Gallery + microsite view analytics and weekly agent digest emails.

Tracking is privacy-preserving: visitor keys are truncated SHA-256 hashes of
IP + user-agent (never raw IPs at rest) and only the referrer domain is kept.
Digests are durable intents — persisted before any provider I/O, claimed with
attempt fencing, and reconciled through ``emails_log`` exactly like
``delivery_notify``.
"""

import csv
import datetime as dt
import hashlib
import io
import logging
from urllib.parse import urlparse

from . import db, emails, mailer, security, studio, tenant
from .vocab import STUDIO_ID

log = logging.getLogger("eos.analytics")

EVENT_GALLERY = "gallery_view"
EVENT_MICROSITE = "microsite_view"
WINDOW_DAYS = (7, 30, 90)
DIGEST_WINDOW_DAYS = 7
_CLAIM_TIMEOUT = "-15 minutes"
_UNKNOWN_OUTCOME = "Delivery outcome unknown; verify the provider before retrying."


def _visitor_key(request) -> str:
    ip = security.client_ip(request)
    ua = request.headers.get("user-agent", "")[:120]
    return hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:32]


def _referrer_domain(request) -> str:
    raw = request.headers.get("referer", "")
    if not raw:
        return ""
    host = urlparse(raw).hostname or ""
    return host.lower()[:200]


def track_view(
    request,
    *,
    event_type: str,
    listing_id: int | None = None,
    gallery_id: int | None = None,
) -> None:
    """Record one public view; never block or break the public path."""
    try:
        if request.headers.get("dnt") == "1":
            return
        db.run(
            """INSERT INTO view_events
               (studio_id, event_type, listing_id, gallery_id, visitor_key, referrer_domain)
               VALUES (?,?,?,?,?,?)""",
            (
                STUDIO_ID,
                event_type,
                listing_id,
                gallery_id,
                _visitor_key(request),
                _referrer_domain(request),
            ),
        )
    except Exception:
        log.exception("view tracking failed for %s", event_type)


def _trend(views: int, prior_views: int) -> int | None:
    if prior_views == 0:
        return None
    return round((views - prior_views) / prior_views * 100)


def _period_totals(days: int) -> dict:
    row = db.one(
        """SELECT COUNT(*) AS views, COUNT(DISTINCT visitor_key) AS unique_visitors
           FROM view_events
           WHERE studio_id=? AND created_at >= datetime('now', ?)""",
        (STUDIO_ID, f"-{days} days"),
    )
    prior = db.one(
        """SELECT COUNT(*) AS views
           FROM view_events
           WHERE studio_id=? AND created_at >= datetime('now', ?)
             AND created_at < datetime('now', ?)""",
        (STUDIO_ID, f"-{2 * days} days", f"-{days} days"),
    )
    views = int(row["views"] or 0)
    prior_views = int(prior["views"] or 0)
    return {
        "views": views,
        "unique_visitors": int(row["unique_visitors"] or 0),
        "prior_views": prior_views,
        "trend_pct": _trend(views, prior_views),
    }


def _listing_rows(days: int) -> list[dict]:
    rows = db.all_(
        """SELECT l.id AS listing_id, l.title, l.address_line1,
                  SUM(CASE WHEN v.event_type='gallery_view' THEN 1 ELSE 0 END) AS gallery_views,
                  SUM(CASE WHEN v.event_type='microsite_view' THEN 1 ELSE 0 END) AS microsite_views,
                  COUNT(*) AS views,
                  COUNT(DISTINCT v.visitor_key) AS unique_visitors
           FROM view_events v
           JOIN listings l ON l.id=v.listing_id AND l.studio_id=v.studio_id
           WHERE v.studio_id=? AND v.created_at >= datetime('now', ?)
           GROUP BY l.id
           ORDER BY views DESC, l.id""",
        (STUDIO_ID, f"-{days} days"),
    )
    out = []
    for row in rows:
        prior = db.one(
            """SELECT COUNT(*) AS views FROM view_events
               WHERE studio_id=? AND listing_id=? AND created_at >= datetime('now', ?)
                 AND created_at < datetime('now', ?)""",
            (STUDIO_ID, row["listing_id"], f"-{2 * days} days", f"-{days} days"),
        )
        views = int(row["views"] or 0)
        prior_views = int(prior["views"] or 0)
        out.append(
            {
                "listing_id": row["listing_id"],
                "title": row["title"],
                "address": row["address_line1"] or row["title"],
                "gallery_views": int(row["gallery_views"] or 0),
                "microsite_views": int(row["microsite_views"] or 0),
                "views": views,
                "unique_visitors": int(row["unique_visitors"] or 0),
                "prior_views": prior_views,
                "trend_pct": _trend(views, prior_views),
                "listing_href": f"/admin/listings/{row['listing_id']}",
            }
        )
    return out


def _gallery_rows(days: int) -> list[dict]:
    rows = db.all_(
        """SELECT g.id AS gallery_id, g.title, g.slug,
                  COUNT(*) AS views,
                  COUNT(DISTINCT v.visitor_key) AS unique_visitors
           FROM view_events v
           JOIN galleries g ON g.id=v.gallery_id AND g.studio_id=v.studio_id
           WHERE v.studio_id=? AND v.event_type='gallery_view'
             AND v.created_at >= datetime('now', ?)
           GROUP BY g.id
           ORDER BY views DESC, g.id""",
        (STUDIO_ID, f"-{days} days"),
    )
    return [
        {
            "gallery_id": row["gallery_id"],
            "title": row["title"],
            "views": int(row["views"] or 0),
            "unique_visitors": int(row["unique_visitors"] or 0),
            "gallery_href": f"/admin/galleries/{row['gallery_id']}",
        }
        for row in rows
    ]


def _top_referrers(days: int, *, limit: int = 8) -> list[dict]:
    rows = db.all_(
        """SELECT referrer_domain, COUNT(*) AS views
           FROM view_events
           WHERE studio_id=? AND referrer_domain != ''
             AND created_at >= datetime('now', ?)
           GROUP BY referrer_domain
           ORDER BY views DESC, referrer_domain LIMIT ?""",
        (STUDIO_ID, f"-{days} days", limit),
    )
    return [{"domain": row["referrer_domain"], "views": int(row["views"])} for row in rows]


def dashboard(days: int = 30) -> dict:
    if days not in WINDOW_DAYS:
        days = 30
    return {
        "days": days,
        "windows": WINDOW_DAYS,
        "summary": _period_totals(days),
        "listings": _listing_rows(days),
        "galleries": _gallery_rows(days),
        "referrers": _top_referrers(days),
    }


def analytics_csv(days: int = 30) -> str:
    data = dashboard(days)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"listing analytics — last {data['days']} days"])
    w.writerow(
        [
            "listing",
            "address",
            "gallery_views",
            "microsite_views",
            "views",
            "unique_visitors",
            "prior_period_views",
            "trend_pct",
        ]
    )
    for row in data["listings"]:
        w.writerow(
            [
                row["title"],
                row["address"],
                row["gallery_views"],
                row["microsite_views"],
                row["views"],
                row["unique_visitors"],
                row["prior_views"],
                "" if row["trend_pct"] is None else row["trend_pct"],
            ]
        )
    w.writerow([])
    w.writerow(["referrer_domain", "views"])
    for ref in data["referrers"]:
        w.writerow([ref["domain"], ref["views"]])
    return buf.getvalue()


def portal_counts(listing_ids: list[int], *, studio_id: str) -> dict[int, dict]:
    """Per-listing views + unique visitors for the agent portal."""
    counts: dict[int, dict] = {lid: {"views": 0, "unique_visitors": 0} for lid in listing_ids}
    if not listing_ids:
        return counts
    placeholders = ",".join("?" for _ in listing_ids)
    rows = db.all_(
        f"""SELECT listing_id, COUNT(*) AS views,
                   COUNT(DISTINCT visitor_key) AS unique_visitors
            FROM view_events
            WHERE studio_id=? AND listing_id IN ({placeholders})
            GROUP BY listing_id""",
        (studio_id, *listing_ids),
    )
    for row in rows:
        counts[row["listing_id"]] = {
            "views": int(row["views"] or 0),
            "unique_visitors": int(row["unique_visitors"] or 0),
        }
    return counts


def _iso_week_key(today: dt.date | None = None) -> str:
    iso = (today or dt.date.today()).isocalendar()
    return f"{iso.year}W{iso.week:02d}"


def _digest_rows(studio_id: str, client_id: int) -> list[dict]:
    rows = db.all_(
        """SELECT l.id AS listing_id, l.title, l.address_line1,
                  l.site_slug, l.site_published,
                  (SELECT g.slug FROM galleries g
                    WHERE g.listing_id=l.id AND g.studio_id=l.studio_id AND g.published=1
                    ORDER BY g.created_at DESC LIMIT 1) AS gallery_slug,
                  COUNT(*) AS views,
                  COUNT(DISTINCT v.visitor_key) AS unique_visitors
           FROM view_events v
           JOIN listings l ON l.id=v.listing_id AND l.studio_id=v.studio_id
           WHERE v.studio_id=? AND l.client_id=?
             AND v.created_at >= datetime('now', ?)
           GROUP BY l.id
           ORDER BY views DESC, l.id""",
        (studio_id, client_id, f"-{DIGEST_WINDOW_DAYS} days"),
    )
    base = tenant.get_base_url()
    out = []
    for row in rows:
        if row["site_published"] and row["site_slug"]:
            url = f"{base}/l/{row['site_slug']}"
        elif row["gallery_slug"]:
            url = f"{base}/g/{row['gallery_slug']}"
        else:
            url = base
        out.append(
            {
                "address": row["address_line1"] or row["title"],
                "views": int(row["views"] or 0),
                "unique_visitors": int(row["unique_visitors"] or 0),
                "url": url,
            }
        )
    return out


def _digest_subject(studio_id: str, client_id: int) -> tuple[str, str]:
    client = db.one(
        "SELECT name, email FROM clients WHERE id=? AND studio_id=?",
        (client_id, studio_id),
    )
    rows = _digest_rows(studio_id, client_id)
    total = sum(r["views"] for r in rows)
    subject, _body = emails.agent_view_digest(
        client_name=client["name"] if client else "there",
        rows=rows,
        total_views=total,
    )
    return subject, (client["email"] if client else "")


def enqueue_weekly_digests() -> int:
    """Persist one digest intent per active agent per ISO week, before any I/O."""
    candidates = db.all_(
        """SELECT DISTINCT v.studio_id, l.client_id
           FROM view_events v
           JOIN listings l ON l.id=v.listing_id AND l.studio_id=v.studio_id
           JOIN clients c ON c.id=l.client_id AND c.studio_id=l.studio_id
           WHERE v.created_at >= datetime('now', ?)
             AND l.client_id IS NOT NULL AND c.email != ''""",
        (f"-{DIGEST_WINDOW_DAYS} days",),
    )
    enqueued = 0
    previous_studio = tenant.get_studio_id()
    for row in candidates:
        studio_id = row["studio_id"]
        client_id = row["client_id"]
        tenant.set_studio(studio_id)
        try:
            if not studio.get_profile()["analytics_digest_enabled"]:
                continue
            subject, to_email = _digest_subject(studio_id, client_id)
            if not to_email:
                continue
            event_key = f"analytics-digest:{client_id}:{_iso_week_key()}"
            with db.tx() as con:
                cur = con.execute(
                    """INSERT OR IGNORE INTO analytics_digest_intents
                       (studio_id, client_id, event_key, to_email, subject, updated_at)
                       VALUES (?,?,?,?,?,datetime('now'))""",
                    (studio_id, client_id, event_key, to_email, subject),
                )
                enqueued += cur.rowcount
        except Exception:
            log.exception("digest enqueue failed for studio=%s client=%s", studio_id, client_id)
        finally:
            tenant.set_studio(previous_studio)
    return enqueued


def _fail_stale_claims() -> int:
    with db.tx(immediate=True) as con:
        cur = con.execute(
            f"""UPDATE analytics_digest_intents
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
            """UPDATE analytics_digest_intents
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
            "SELECT * FROM analytics_digest_intents WHERE id=? AND studio_id=?",
            (intent_id, studio_id),
        ).fetchone()
        con.commit()
        return row
    finally:
        con.close()


def process_intent(intent_id: int, *, studio_id: str | None = None) -> bool:
    """Claim and send one digest; persist-first ordering is set at enqueue."""
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
               WHERE studio_id=? AND doc_kind='analytics_digest' AND doc_id=? LIMIT 1""",
            (studio_id, intent_id),
        )
        if already_sent:
            with db.tx(immediate=True) as con:
                con.execute(
                    """UPDATE analytics_digest_intents
                       SET status='sent', error=NULL, claimed_at=NULL, attempt_token=NULL,
                           updated_at=datetime('now')
                       WHERE id=? AND studio_id=? AND status='pending'
                         AND attempt_token=?""",
                    (intent_id, studio_id, attempt_token),
                )
            return False
        client = db.one(
            "SELECT name, email FROM clients WHERE id=? AND studio_id=?",
            (intent["client_id"], studio_id),
        )
        if not client or not client["email"]:
            raise RuntimeError("digest recipient is missing")
        rows = _digest_rows(studio_id, intent["client_id"])
        if not rows:
            # Activity aged out before the send; close the intent without mailing.
            with db.tx(immediate=True) as con:
                con.execute(
                    """UPDATE analytics_digest_intents
                       SET status='sent', error='no recent activity at send time',
                           claimed_at=NULL, attempt_token=NULL, updated_at=datetime('now')
                       WHERE id=? AND studio_id=? AND status='pending'
                         AND attempt_token=?""",
                    (intent_id, studio_id, attempt_token),
                )
            return False
        subject, body = emails.agent_view_digest(
            client_name=client["name"],
            rows=rows,
            total_views=sum(r["views"] for r in rows),
        )
        mailer.send_for_studio(client["email"], subject, body)
        provider_accepted = True
        with db.tx(immediate=True) as con:
            updated = con.execute(
                """UPDATE analytics_digest_intents
                   SET status='sent', sent_at=datetime('now'), error=NULL,
                       claimed_at=NULL, attempt_token=NULL, updated_at=datetime('now')
                   WHERE id=? AND studio_id=? AND status='pending'
                     AND attempt_token=?""",
                (intent_id, studio_id, attempt_token),
            )
            if updated.rowcount != 1:
                raise RuntimeError("analytics digest claim was lost after provider acceptance")
            con.execute(
                """INSERT INTO emails_log
                   (studio_id, listing_id, doc_kind, doc_id, to_email, subject)
                   VALUES (?,?,?,?,?,?)""",
                (
                    studio_id,
                    None,
                    "analytics_digest",
                    intent_id,
                    client["email"],
                    subject,
                ),
            )
        log.info("sent analytics digest %s to %s", intent_id, client["email"])
        return True
    except Exception as exc:
        error = str(exc)[:500]
        if provider_accepted:
            error = f"{_UNKNOWN_OUTCOME} {error}"[:500]
        with db.tx(immediate=True) as con:
            con.execute(
                """UPDATE analytics_digest_intents
                   SET status='failed', error=?, claimed_at=NULL, attempt_token=NULL,
                       updated_at=datetime('now')
                   WHERE id=? AND studio_id=? AND status='pending'
                     AND attempt_token=?""",
                (error, intent_id, studio_id, attempt_token),
            )
        log.error("analytics digest %s failed: %s", intent_id, exc)
        return False
    finally:
        tenant.set_studio(previous_studio)


def process_pending(limit: int = 20) -> int:
    _fail_stale_claims()
    if not mailer.configured():
        return 0
    pending = db.all_(
        """SELECT id, studio_id FROM analytics_digest_intents
            WHERE status='pending' AND claimed_at IS NULL
            ORDER BY created_at, id LIMIT ?""",
        (limit,),
    )
    return sum(1 for row in pending if process_intent(row["id"], studio_id=row["studio_id"]))
