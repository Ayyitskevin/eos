"""Agent client portal — magic-link access to deliveries and invoices."""

from fastapi import HTTPException

from . import db, security, tenant
from .vocab import STUDIO_ID


def ensure_token(client_id: int) -> str:
    row = db.one(
        "SELECT portal_token FROM clients WHERE id=? AND studio_id=?", (client_id, STUDIO_ID)
    )
    if not row:
        raise HTTPException(status_code=404)
    if row["portal_token"]:
        return row["portal_token"]
    token = security.new_token()
    db.run(
        "UPDATE clients SET portal_token=? WHERE id=? AND studio_id=?",
        (token, client_id, STUDIO_ID),
    )
    return token


def get_client_by_token(token: str):
    row = db.one(
        "SELECT * FROM clients WHERE portal_token=? AND studio_id=?",
        (token, STUDIO_ID),
    )
    if not row:
        raise HTTPException(status_code=404)
    return row


def deliveries(client_id: int) -> list:
    return db.all_(
        """SELECT l.id AS listing_id, l.title, l.status, l.address_line1, l.created_at,
                  l.site_slug, l.site_published,
                  g.id AS gallery_id, g.slug AS gallery_slug, g.title AS gallery_title,
                  g.published AS gallery_published, g.pin AS gallery_pin,
                  (SELECT slug FROM invoices i WHERE i.listing_id=l.id AND i.status='sent'
                   ORDER BY i.id LIMIT 1) AS unpaid_invoice_slug,
                  (SELECT status FROM invoices i WHERE i.listing_id=l.id AND i.status='paid'
                   ORDER BY i.paid_at DESC LIMIT 1) AS has_paid
           FROM listings l
           LEFT JOIN galleries g ON g.listing_id=l.id AND g.published=1
           WHERE l.client_id=? AND l.studio_id=?
           ORDER BY l.created_at DESC""",
        (client_id, STUDIO_ID),
    )


def portal_url(client_id: int) -> str:
    return f"{tenant.get_base_url()}/portal/{ensure_token(client_id)}"


def brokerage_portal_url(client_id: int) -> str:
    return f"{tenant.get_base_url()}/portal/brokerage/{ensure_token(client_id)}"


def repeat_links(client_id: int, *, portal_token: str | None = None) -> dict:
    from urllib.parse import quote

    from . import referrals

    token = portal_token or ensure_token(client_id)
    referral = referrals.code_for_client(client_id)
    base = tenant.get_base_url()
    return {
        "rebook_url": f"{base}/book?returning={quote(token)}",
        "referral_url": f"{base}/r/{quote(referral['code'])}" if referral else None,
    }
