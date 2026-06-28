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
        w.writerow([a["name"], a["company"] or "", a["n_listings"], a["revenue_cents"], a["open_cents"]])
    return buf.getvalue()


def full_csv() -> str:
    return summary_csv() + "\n\n" + agents_csv()