from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import api_tokens, db, inquiries, listings, tenant
from ..vocab import STUDIO_ID

router = APIRouter(prefix="/api/v1")


async def _api_tenant(request: Request):
    studio_id = api_tokens.authenticate_request(request)
    tenant.set_studio(studio_id)
    return studio_id


def _paginate(rows: list, *, limit: int, offset: int) -> dict:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    page = rows[offset: offset + limit]
    return {"items": page, "limit": limit, "offset": offset, "total": len(rows)}


@router.get("/listings")
async def api_listings(
    _: str = Depends(_api_tenant),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = None,
):
    rows = listings.list_listings(status=status)
    payload = [
        {
            "id": r["id"],
            "title": r["title"],
            "status": r["status"],
            "client_name": r["client_name"],
            "due_at": r["due_at"],
            "address_line1": r["address_line1"],
        }
        for r in rows
    ]
    return _paginate(payload, limit=limit, offset=offset)


@router.get("/bookings")
async def api_bookings(
    _: str = Depends(_api_tenant),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows = inquiries.list_confirmed(limit=500)
    payload = [
        {
            "id": r["id"],
            "name": r["name"],
            "email": r["email"],
            "property_address": r["property_address"],
            "scheduled_at": r["scheduled_at"],
            "status": r["status"],
            "listing_id": r["listing_id"],
        }
        for r in rows
    ]
    return _paginate(payload, limit=limit, offset=offset)


@router.get("/bookings/{inquiry_id}")
async def api_booking_detail(inquiry_id: int, _: str = Depends(_api_tenant)):
    row = db.one(
        "SELECT * FROM inquiries WHERE id=? AND studio_id=?",
        (inquiry_id, str(STUDIO_ID)),
    )
    if not row:
        raise HTTPException(status_code=404)
    return dict(row)


@router.get("/health")
async def api_health():
    return {"ok": True}


@router.get("/me")
async def api_me(_: str = Depends(_api_tenant)):
    return {"ok": True, "studio_id": str(STUDIO_ID)}