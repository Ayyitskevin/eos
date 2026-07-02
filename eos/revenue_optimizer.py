"""Package and upsell revenue reporting for real-estate studio operators."""

import csv
import io
import json
from collections import defaultdict
from typing import Any

from . import db
from .vocab import PROPERTY_TYPE_LABELS, STUDIO_ID


def _money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${dollars:,.0f}"
    return f"${dollars:,.2f}"


def _safe_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()


def _service_packages() -> tuple[dict[int, dict], dict[str, int]]:
    rows = db.all_(
        """SELECT id, name, price_cents, active, position
           FROM service_packages
           WHERE studio_id=?
           ORDER BY position, id""",
        (STUDIO_ID,),
    )
    packages = {
        row["id"]: {
            "id": row["id"],
            "name": row["name"],
            "price_cents": int(row["price_cents"] or 0),
            "active": bool(row["active"]),
            "position": int(row["position"] or 0),
        }
        for row in rows
    }
    by_label = {_normalized(row["name"]): row["id"] for row in rows}
    return packages, by_label


def _service_addons() -> tuple[dict[int, dict], dict[str, int], list[dict]]:
    rows = db.all_(
        """SELECT id, name, slug, price_cents, active, position
           FROM service_addons
           WHERE studio_id=?
           ORDER BY position, id""",
        (STUDIO_ID,),
    )
    addons = {
        row["id"]: {
            "id": row["id"],
            "name": row["name"],
            "slug": row["slug"],
            "price_cents": int(row["price_cents"] or 0),
            "active": bool(row["active"]),
            "position": int(row["position"] or 0),
        }
        for row in rows
    }
    by_label = {_normalized(row["name"]): row["id"] for row in rows}
    active = [addon for addon in addons.values() if addon["active"]]
    return addons, by_label, active


def _invoice_labels_by_listing() -> dict[int, set[str]]:
    labels: dict[int, set[str]] = defaultdict(set)
    rows = db.all_(
        """SELECT listing_id, line_items
           FROM invoices
           WHERE studio_id=? AND listing_id IS NOT NULL AND status IN ('sent','paid')""",
        (STUDIO_ID,),
    )
    for row in rows:
        for item in _safe_json_list(row["line_items"]):
            if isinstance(item, dict):
                label = _normalized(item.get("label"))
                if label:
                    labels[row["listing_id"]].add(label)
    return labels


def _listing_rows() -> list[dict]:
    packages, package_by_label = _service_packages()
    addons, addon_by_label, _active_addons = _service_addons()
    labels_by_listing = _invoice_labels_by_listing()
    rows = db.all_(
        """SELECT l.id, l.title, l.property_type, l.status, l.created_at, l.shoot_date,
                  c.name AS client_name,
                  q.package_id, q.addon_ids, q.total_cents, q.created_at AS booking_created_at,
                  sp.name AS package_name,
                  COALESCE((SELECT SUM(i.amount_cents)
                     FROM invoices i
                    WHERE i.studio_id=l.studio_id AND i.listing_id=l.id AND i.status='paid'), 0) AS paid_cents,
                  COALESCE((SELECT SUM(i.amount_cents)
                     FROM invoices i
                    WHERE i.studio_id=l.studio_id AND i.listing_id=l.id AND i.status='sent'), 0) AS open_cents,
                  (SELECT COUNT(*)
                     FROM invoices i
                    WHERE i.studio_id=l.studio_id AND i.listing_id=l.id AND i.status='paid') AS n_paid_invoices
           FROM listings l
           LEFT JOIN clients c
             ON c.id=l.client_id AND c.studio_id=l.studio_id
           LEFT JOIN inquiries q
             ON q.id=(
                  SELECT q2.id
                    FROM inquiries q2
                   WHERE q2.studio_id=l.studio_id AND q2.listing_id=l.id
                   ORDER BY q2.created_at DESC, q2.id DESC
                   LIMIT 1
                )
           LEFT JOIN service_packages sp
             ON sp.id=q.package_id AND sp.studio_id=q.studio_id
           WHERE l.studio_id=?
           ORDER BY COALESCE(l.shoot_date, l.created_at) DESC, l.id DESC""",
        (STUDIO_ID,),
    )
    out: list[dict] = []
    for row in rows:
        package_id = row["package_id"]
        package_name = row["package_name"]
        inferred = False
        if not package_id:
            for label in labels_by_listing.get(row["id"], set()):
                if label in package_by_label:
                    package_id = package_by_label[label]
                    package_name = packages[package_id]["name"]
                    inferred = True
                    break
        addon_ids = {int(a) for a in _safe_json_list(row["addon_ids"]) if str(a).isdigit()}
        for label in labels_by_listing.get(row["id"], set()):
            addon_id = addon_by_label.get(label)
            if addon_id:
                addon_ids.add(addon_id)
        addon_names = [addons[addon_id]["name"] for addon_id in addon_ids if addon_id in addons]
        paid_cents = int(row["paid_cents"] or 0)
        open_cents = int(row["open_cents"] or 0)
        out.append(
            {
                "id": row["id"],
                "title": row["title"],
                "property_type": row["property_type"] or "other",
                "property_type_label": PROPERTY_TYPE_LABELS.get(row["property_type"], "Other"),
                "status": row["status"],
                "created_at": row["created_at"],
                "shoot_date": row["shoot_date"],
                "client_name": row["client_name"],
                "package_id": package_id,
                "package_name": package_name or "Unattributed",
                "package_inferred": inferred,
                "addon_ids": sorted(addon_ids),
                "addon_names": addon_names,
                "has_addons": bool(addon_ids),
                "booking_total_cents": int(row["total_cents"] or 0),
                "booking_created_at": row["booking_created_at"],
                "paid_cents": paid_cents,
                "open_cents": open_cents,
                "n_paid_invoices": int(row["n_paid_invoices"] or 0),
                "paid_display": _money(paid_cents),
                "open_display": _money(open_cents),
                "listing_href": f"/admin/listings/{row['id']}",
            }
        )
    return out


def _empty_bucket(name: str, *, package_id: int | None = None) -> dict:
    return {
        "id": package_id,
        "name": name,
        "n_listings": 0,
        "n_paid_listings": 0,
        "n_addon_listings": 0,
        "missed_upsell_count": 0,
        "booked_cents": 0,
        "paid_cents": 0,
        "open_cents": 0,
        "last_listing_at": None,
    }


def _finish_bucket(row: dict) -> dict:
    paid_listings = row["n_paid_listings"]
    listings = row["n_listings"]
    addon_attach_rate = round((row["n_addon_listings"] / listings) * 100) if listings else 0
    avg_paid_cents = row["paid_cents"] // paid_listings if paid_listings else 0
    row.update(
        {
            "paid_display": _money(row["paid_cents"]),
            "open_display": _money(row["open_cents"]),
            "booked_display": _money(row["booked_cents"]),
            "avg_paid_cents": avg_paid_cents,
            "avg_paid_display": _money(avg_paid_cents),
            "addon_attach_rate": addon_attach_rate,
            "missed_upsell_display": _money(row["paid_cents"])
            if row["missed_upsell_count"] == row["n_paid_listings"] and row["paid_cents"]
            else f"{row['missed_upsell_count']} listings",
        }
    )
    return row


def package_performance(limit: int = 12, rows: list[dict] | None = None) -> list[dict]:
    rows = rows if rows is not None else _listing_rows()
    buckets: dict[str, dict] = {}
    for listing in rows:
        key = str(listing["package_id"] or "unattributed")
        bucket = buckets.setdefault(
            key,
            _empty_bucket(listing["package_name"], package_id=listing["package_id"]),
        )
        bucket["n_listings"] += 1
        bucket["booked_cents"] += listing["booking_total_cents"]
        bucket["paid_cents"] += listing["paid_cents"]
        bucket["open_cents"] += listing["open_cents"]
        if listing["paid_cents"]:
            bucket["n_paid_listings"] += 1
        if listing["has_addons"]:
            bucket["n_addon_listings"] += 1
        elif listing["paid_cents"]:
            bucket["missed_upsell_count"] += 1
        activity_at = listing["shoot_date"] or listing["created_at"]
        if activity_at and (
            not bucket["last_listing_at"] or activity_at > bucket["last_listing_at"]
        ):
            bucket["last_listing_at"] = activity_at
    finished = [_finish_bucket(row) for row in buckets.values()]
    finished.sort(
        key=lambda row: (row["paid_cents"], row["open_cents"], row["n_listings"]), reverse=True
    )
    return finished[:limit]


def property_type_performance(rows: list[dict] | None = None) -> list[dict]:
    rows = rows if rows is not None else _listing_rows()
    buckets: dict[str, dict] = {}
    for listing in rows:
        bucket = buckets.setdefault(
            listing["property_type"],
            _empty_bucket(listing["property_type_label"]),
        )
        bucket["property_type"] = listing["property_type"]
        bucket["n_listings"] += 1
        bucket["booked_cents"] += listing["booking_total_cents"]
        bucket["paid_cents"] += listing["paid_cents"]
        bucket["open_cents"] += listing["open_cents"]
        if listing["paid_cents"]:
            bucket["n_paid_listings"] += 1
        if listing["has_addons"]:
            bucket["n_addon_listings"] += 1
        elif listing["paid_cents"]:
            bucket["missed_upsell_count"] += 1
        activity_at = listing["shoot_date"] or listing["created_at"]
        if activity_at and (
            not bucket["last_listing_at"] or activity_at > bucket["last_listing_at"]
        ):
            bucket["last_listing_at"] = activity_at
    finished = [_finish_bucket(row) for row in buckets.values()]
    finished.sort(
        key=lambda row: (row["paid_cents"], row["open_cents"], row["n_listings"]), reverse=True
    )
    return finished


def upsell_opportunities(limit: int = 10, rows: list[dict] | None = None) -> list[dict]:
    rows = rows if rows is not None else _listing_rows()
    _addons, _addon_by_label, active_addons = _service_addons()
    suggested = active_addons[:2]
    suggested_cents = sum(addon["price_cents"] for addon in suggested)
    candidates = [
        row
        for row in rows
        if row["paid_cents"] > 0 and not row["has_addons"] and row["status"] != "archived"
    ]
    candidates.sort(
        key=lambda row: (row["paid_cents"], row["shoot_date"] or row["created_at"] or ""),
        reverse=True,
    )
    out = []
    for row in candidates[:limit]:
        out.append(
            {
                **row,
                "recommended_addons": suggested,
                "recommended_names": ", ".join(addon["name"] for addon in suggested)
                or "Add-on bundle",
                "suggested_cents": suggested_cents,
                "suggested_display": _money(suggested_cents),
            }
        )
    return out


def summary(rows: list[dict] | None = None) -> dict:
    rows = rows if rows is not None else _listing_rows()
    paid_cents = sum(row["paid_cents"] for row in rows)
    open_cents = sum(row["open_cents"] for row in rows)
    paid_listings = sum(1 for row in rows if row["paid_cents"])
    addon_listings = sum(1 for row in rows if row["has_addons"])
    opportunities = upsell_opportunities(rows=rows)
    suggested_cents = sum(row["suggested_cents"] for row in opportunities)
    addon_attach_rate = round((addon_listings / len(rows)) * 100) if rows else 0
    avg_paid_cents = paid_cents // paid_listings if paid_listings else 0
    return {
        "n_listings": len(rows),
        "n_paid_listings": paid_listings,
        "paid_cents": paid_cents,
        "open_cents": open_cents,
        "paid_display": _money(paid_cents),
        "open_display": _money(open_cents),
        "avg_paid_cents": avg_paid_cents,
        "avg_paid_display": _money(avg_paid_cents),
        "addon_attach_rate": addon_attach_rate,
        "n_opportunities": len(opportunities),
        "suggested_cents": suggested_cents,
        "suggested_display": _money(suggested_cents),
    }


def dashboard() -> dict:
    rows = _listing_rows()
    return {
        "summary": summary(rows),
        "packages": package_performance(rows=rows),
        "property_types": property_type_performance(rows=rows),
        "opportunities": upsell_opportunities(rows=rows),
    }


def optimizer_csv() -> str:
    data = dashboard()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Package performance"])
    writer.writerow(
        [
            "package",
            "listings",
            "paid_listings",
            "paid_cents",
            "open_cents",
            "avg_paid_cents",
            "addon_attach_rate",
            "missed_upsell_count",
            "last_listing_at",
        ]
    )
    for row in data["packages"]:
        writer.writerow(
            [
                row["name"],
                row["n_listings"],
                row["n_paid_listings"],
                row["paid_cents"],
                row["open_cents"],
                row["avg_paid_cents"],
                row["addon_attach_rate"],
                row["missed_upsell_count"],
                (row["last_listing_at"] or "")[:10],
            ]
        )
    writer.writerow([])
    writer.writerow(["Property type performance"])
    writer.writerow(
        [
            "property_type",
            "listings",
            "paid_cents",
            "open_cents",
            "avg_paid_cents",
            "addon_attach_rate",
            "missed_upsell_count",
        ]
    )
    for row in data["property_types"]:
        writer.writerow(
            [
                row["name"],
                row["n_listings"],
                row["paid_cents"],
                row["open_cents"],
                row["avg_paid_cents"],
                row["addon_attach_rate"],
                row["missed_upsell_count"],
            ]
        )
    writer.writerow([])
    writer.writerow(["Upsell opportunities"])
    writer.writerow(
        ["listing", "agent", "package", "property_type", "paid_cents", "recommended_addons"]
    )
    for row in data["opportunities"]:
        writer.writerow(
            [
                row["title"],
                row["client_name"] or "",
                row["package_name"],
                row["property_type_label"],
                row["paid_cents"],
                row["recommended_names"],
            ]
        )
    return buf.getvalue()
