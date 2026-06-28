"""Public property microsites — /l/{slug}."""

import re
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import bundles, config, db, marketing_kit, microsites, paywall, security, stripe_checkout, studio
from ..render import templates
from ..vocab import STUDIO_ID

router = APIRouter()
INDEXABLE_PREFIX = "/l/"
_EMAIL = re.compile(r"^[^@\s]+\.[^@\s]+\.[^@\s]+$")


def _pay_context(listing_id: int) -> dict:
    locked = paywall.payment_required(listing_id)
    inv_slug = paywall.unpaid_invoice_slug(listing_id) if locked else None
    return {
        "payment_locked": locked,
        "pay_url": f"/i/{inv_slug}" if inv_slug else None,
        "payments_on": stripe_checkout.payments_configured(),
    }


@router.get("/l/{slug}", response_class=HTMLResponse)
async def listing_site(request: Request, slug: str):
    listing = microsites.get_published_by_slug(slug)
    ctx = microsites.site_context(listing)
    kit = marketing_kit.get_status(listing["id"])
    upsell = studio.delivery_upsell() if listing["status"] == "delivered" else None
    return templates.TemplateResponse(
        request,
        "public/listing_site.html",
        {
            **ctx,
            "kit": kit,
            "upsell": upsell,
            "lead_capture": bool(listing["site_lead_capture"]),
            "thanks": request.query_params.get("thanks") == "1",
            "error": None,
            **_pay_context(listing["id"]),
        },
    )


@router.post("/l/{slug}/inquire")
async def listing_inquire(
    request: Request,
    slug: str,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    message: str = Form(""),
):
    listing = microsites.get_published_by_slug(slug)
    if not listing["site_lead_capture"]:
        raise HTTPException(status_code=404)
    ip = security.client_ip(request)
    if security.inquiry_throttled(ip, security.INQUIRY_BUCKET_SITE):
        raise HTTPException(status_code=429, detail="too many requests")
    email = email.strip().lower()
    if not _EMAIL.match(email):
        return templates.TemplateResponse(
            request,
            "public/listing_site.html",
            {
                **microsites.site_context(listing),
                "kit": marketing_kit.get_status(listing["id"]),
                "lead_capture": True,
                "thanks": False,
                "error": "Invalid email.",
                **_pay_context(listing["id"]),
            },
            status_code=400,
        )
    security.inquiry_record(ip, security.INQUIRY_BUCKET_SITE)
    address = microsites.site_context(listing)["address"]
    db.run(
        """INSERT INTO inquiries (studio_id, name, email, phone, message, property_address)
           VALUES (?,?,?,?,?,?)""",
        (STUDIO_ID, name.strip(), email, phone.strip(), message.strip(), address),
    )
    db.audit("public", "site.lead", f"listing={listing['id']}")
    return RedirectResponse(f"/l/{slug}?thanks=1", status_code=303)


@router.get("/l/{slug}/hero.jpg")
async def listing_hero(slug: str):
    listing = microsites.get_published_by_slug(slug)
    gal = microsites.primary_gallery(listing["id"])
    if not gal:
        raise HTTPException(status_code=404)
    asset_id = gal["cover_asset_id"]
    if not asset_id:
        row = db.one(
            "SELECT id FROM assets WHERE gallery_id=? AND status='ready' ORDER BY position, id LIMIT 1",
            (gal["id"],),
        )
        asset_id = row["id"] if row else None
    if not asset_id:
        raise HTTPException(status_code=404)
    asset = db.one("SELECT * FROM assets WHERE id=? AND gallery_id=?", (asset_id, gal["id"]))
    if not asset:
        raise HTTPException(status_code=404)
    from .. import media_paths

    base = media_paths.gallery_dir(gal["id"])
    path = base / "web" / f"{Path(asset['stored']).stem}.jpg"
    if not path.is_file():
        path = base / "original" / asset["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"}
    )


@router.get("/l/{slug}/photo/{asset_id}")
async def listing_photo(slug: str, asset_id: int):
    listing = microsites.get_published_by_slug(slug)
    gal = microsites.primary_gallery(listing["id"])
    if not gal:
        raise HTTPException(status_code=404)
    asset = db.one(
        "SELECT * FROM assets WHERE id=? AND gallery_id=? AND status='ready'",
        (asset_id, gal["id"]),
    )
    if not asset:
        raise HTTPException(status_code=404)
    from .. import media_paths

    base = media_paths.gallery_dir(gal["id"])
    path = base / "web" / f"{Path(asset['stored']).stem}.jpg"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"}
    )


@router.get("/l/{slug}/download/{kind}")
async def listing_bundle(request: Request, slug: str, kind: str):
    if kind not in bundles.BUNDLE_KINDS:
        raise HTTPException(status_code=404)
    listing = microsites.get_published_by_slug(slug)
    if paywall.payment_required(listing["id"]):
        inv_slug = paywall.unpaid_invoice_slug(listing["id"])
        raise HTTPException(
            status_code=402,
            detail=f"payment required — pay invoice at /i/{inv_slug}"
            if inv_slug
            else "payment required",
        )
    path = bundles.get_ready_bundle(listing["id"], kind)
    if not path:
        gal = microsites.primary_gallery(listing["id"])
        if gal:
            bundles.ensure_exports(gal["id"])
        bundles.enqueue_bundle(listing["id"], kind)
        return templates.TemplateResponse(
            request,
            "public/zip_wait.html",
            {"g": {"title": listing["title"], "slug": slug}, "return_url": f"/l/{slug}"},
        )
    label = {"mls": "MLS", "zillow": "Zillow", "fullres": "Full-Res"}[kind]
    return FileResponse(
        path, filename=f"{listing['title']}-{label}.zip", media_type="application/zip"
    )


@router.get("/l/{slug}/marketing/{kind}")
async def listing_marketing_asset(slug: str, kind: str):
    listing = microsites.get_published_by_slug(slug)
    if paywall.payment_required(listing["id"]):
        inv_slug = paywall.unpaid_invoice_slug(listing["id"])
        raise HTTPException(
            status_code=402,
            detail=f"payment required — pay invoice at /i/{inv_slug}"
            if inv_slug
            else "payment required",
        )
    path = marketing_kit.asset_path(listing["id"], kind)
    if not path:
        marketing_kit.enqueue_build(listing["id"])
        raise HTTPException(status_code=404, detail="marketing kit building")
    media = "application/pdf" if kind == "flyer" else "image/jpeg"
    return FileResponse(path, media_type=media)
