"""RE photography contracts — merge-field template, SHA-256 lock at send, typed e-sign."""

import hashlib
from datetime import date

from fastapi import HTTPException

from . import config, db, listings, security
from .vocab import STUDIO_ID

DEFAULT_TEMPLATE = """\
REAL ESTATE PHOTOGRAPHY SERVICES AGREEMENT

This agreement is between {site_name} ("Photographer") and {client_name}{company_clause} ("Client"), dated {date}, for the listing "{listing_title}"{address_clause}.

1. SCOPE — Photographer will provide real estate photography services described in the accepted proposal{total_clause}. Deliverables include edited digital images in MLS-ready sizes and a private online gallery.

2. PAYMENT — Per the associated invoice. Payment is due on delivery unless a deposit was agreed in writing to reserve the shoot date.

3. TURNAROUND — Edited images delivered within the turnaround stated in the proposal, measured from the shoot date.

4. MLS & MARKETING USAGE — Client receives a non-exclusive, perpetual license to use delivered images on MLS, Zillow, Realtor.com, print collateral, and social media for this listing. Photographer retains copyright and may use images for portfolio unless Client opts out in writing.

5. ACCESS & CANCELLATION — Client will provide property access at the scheduled time. Cancellations within 24 hours of the shoot may incur a rescheduling fee.

6. LIABILITY — Photographer's total liability is limited to fees paid under this agreement.

7. E-SIGNATURE — Both parties agree that a typed name submitted through the signing page constitutes a legal signature under the U.S. ESIGN Act.
"""


def get_contract(contract_id: int):
    row = db.one("SELECT * FROM contracts WHERE id=? AND studio_id=?", (contract_id, STUDIO_ID))
    if not row:
        raise HTTPException(status_code=404)
    listings.get_listing(row["listing_id"])
    return row


def get_contract_by_slug(slug: str):
    row = db.one("SELECT * FROM contracts WHERE slug=? AND studio_id=?", (slug, STUDIO_ID))
    if not row or row["status"] == "draft":
        raise HTTPException(status_code=404)
    listings.get_listing(row["listing_id"])
    return row


def list_for_listing(listing_id: int):
    listings.get_listing(listing_id)
    return db.all_(
        """SELECT * FROM contracts
           WHERE listing_id=? AND studio_id=? ORDER BY created_at DESC""",
        (listing_id, STUDIO_ID),
    )


def render_template(listing_id: int) -> str:
    listing = listings.get_listing(listing_id)
    client_name = "Client"
    company_clause = ""
    if listing["client_id"]:
        c = db.one(
            "SELECT name, company FROM clients WHERE id=? AND studio_id=?",
            (listing["client_id"], STUDIO_ID),
        )
        if c:
            client_name = c["name"]
            company_clause = f" of {c['company']}" if c["company"] else ""
    accepted = db.one(
        """SELECT total_cents FROM proposals
           WHERE listing_id=? AND studio_id=? AND status='accepted'
           ORDER BY accepted_at DESC LIMIT 1""",
        (listing_id, STUDIO_ID),
    )
    total_clause = f" for a total of ${accepted['total_cents'] / 100:.2f}" if accepted else ""
    addr = listings.format_address(listing)
    address_clause = f" at {addr}" if addr else ""
    return DEFAULT_TEMPLATE.format(
        site_name=config.SITE_NAME,
        client_name=client_name,
        company_clause=company_clause,
        date=date.today().isoformat(),
        listing_title=listing["title"],
        address_clause=address_clause,
        total_clause=total_clause,
    )


def create_contract(listing_id: int) -> int:
    listing = listings.get_listing(listing_id)
    cid = db.run(
        """INSERT INTO contracts (studio_id, listing_id, slug, title, body)
           VALUES (?,?,?,?,?)""",
        (
            STUDIO_ID,
            listing_id,
            security.new_slug(),
            f"Services Agreement — {listing['title']}",
            render_template(listing_id),
        ),
    )
    db.audit("admin", "contract.create", f"id={cid} listing_id={listing_id}")
    return cid


def update_contract(contract_id: int, *, title: str, body: str) -> None:
    d = get_contract(contract_id)
    if d["status"] != "draft":
        raise HTTPException(status_code=400, detail="sent contracts are locked")
    if not body.strip():
        raise HTTPException(status_code=400, detail="body required")
    db.run(
        "UPDATE contracts SET title=?, body=? WHERE id=? AND studio_id=?",
        (title.strip() or d["title"], body, contract_id, STUDIO_ID),
    )


def mark_sent(contract_id: int) -> None:
    d = get_contract(contract_id)
    if d["status"] != "draft":
        raise HTTPException(status_code=400, detail="already sent")
    sha = hashlib.sha256(d["body"].encode()).hexdigest()
    db.run(
        """UPDATE contracts SET status='sent', body_sha256=?, sent_at=datetime('now')
           WHERE id=? AND studio_id=?""",
        (sha, contract_id, STUDIO_ID),
    )


def mark_viewed(contract_id: int) -> None:
    get_contract(contract_id)
    db.run(
        """UPDATE contracts SET status='viewed', viewed_at=datetime('now')
           WHERE id=? AND studio_id=? AND status='sent'""",
        (contract_id, STUDIO_ID),
    )


def sign_by_slug(slug: str, signer_name: str, signer_ip: str) -> None:
    d = get_contract_by_slug(slug)
    if d["status"] not in ("sent", "viewed"):
        raise HTTPException(status_code=400, detail="contract is not open for signing")
    if not signer_name.strip():
        raise HTTPException(status_code=400, detail="typed name required")
    if hashlib.sha256(d["body"].encode()).hexdigest() != d["body_sha256"]:
        raise HTTPException(status_code=409, detail="contract integrity check failed")
    db.run(
        """UPDATE contracts SET status='signed', signer_name=?, signer_ip=?,
           signed_at=datetime('now') WHERE id=? AND studio_id=?""",
        (signer_name.strip(), signer_ip, d["id"], STUDIO_ID),
    )
