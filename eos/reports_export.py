"""CSV export for ops reports."""

import csv
import io

from . import reports


def summary_csv() -> str:
    s = reports.summary()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["metric", "value"])
    w.writerow(["revenue_mtd_cents", s["revenue_mtd_cents"]])
    w.writerow(["revenue_ytd_cents", s["revenue_ytd_cents"]])
    w.writerow(["bookings_mtd", s["bookings_mtd"]])
    w.writerow(["aov_cents", s["aov_cents"]])
    w.writerow(["overdue_ar_cents", s["overdue_ar_cents"]])
    w.writerow(["open_ar_cents", s["open_ar_cents"]])
    return buf.getvalue()


def agents_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "company", "listings", "revenue_cents", "open_cents"])
    for a in reports.top_agents(limit=100):
        w.writerow(
            [a["name"], a["company"] or "", a["n_listings"], a["revenue_cents"], a["open_cents"]]
        )
    return buf.getvalue()


def full_csv() -> str:
    return summary_csv() + "\n\n" + agents_csv()


def quickbooks_csv() -> str:
    """QuickBooks-friendly expense/sales export — invoices paid in last 90 days."""
    from . import db
    from .vocab import STUDIO_ID

    rows = db.all_(
        """SELECT i.paid_at, i.title, i.amount_cents, c.name AS client_name, l.title AS listing_title
           FROM invoices i
           LEFT JOIN clients c ON c.id=i.client_id
           LEFT JOIN listings l ON l.id=i.listing_id
           WHERE i.studio_id=? AND i.status='paid' AND i.paid_at >= datetime('now', '-90 days')
           ORDER BY i.paid_at""",
        (STUDIO_ID,),
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Name", "Memo", "Amount"])
    for r in rows:
        memo = f"{r['listing_title'] or ''} — {r['title']}".strip(" —")
        w.writerow(
            [
                (r["paid_at"] or "")[:10],
                r["client_name"] or "Client",
                memo,
                f"{(r['amount_cents'] or 0) / 100:.2f}",
            ]
        )
    return buf.getvalue()
