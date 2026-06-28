"""Pay-to-download gate — lock originals until listing invoices are paid."""

from . import db, studio
from .vocab import STUDIO_ID


def payment_required(listing_id: int | None) -> bool:
    if not listing_id:
        return False
    profile = studio.get_profile()
    if not profile["pay_to_download"]:
        return False
    row = db.one(
        """SELECT 1 AS x FROM invoices
           WHERE listing_id=? AND studio_id=? AND status='sent' LIMIT 1""",
        (listing_id, STUDIO_ID),
    )
    return bool(row)


def unpaid_invoice_slug(listing_id: int | None) -> str | None:
    if not listing_id:
        return None
    row = db.one(
        """SELECT slug FROM invoices
           WHERE listing_id=? AND studio_id=? AND status='sent'
           ORDER BY CASE invoice_kind WHEN 'full' THEN 0 WHEN 'balance' THEN 1 ELSE 2 END, id
           LIMIT 1""",
        (listing_id, STUDIO_ID),
    )
    return row["slug"] if row else None


def watermark_previews() -> bool:
    return bool(studio.get_profile()["watermark_until_paid"])
