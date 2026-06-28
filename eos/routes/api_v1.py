from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import api_tokens, commerce, db, inquiries, listings, scheduling, tenant
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


class ListingCreate(BaseModel):
    title: str
    address_line1: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = Field("", alias="zip")
    mls_id: str = ""
    client_id: int | None = None
    property_type: str = "residential"

    model_config = {"populate_by_name": True}


class ListingPatch(BaseModel):
    title: str | None = None
    status: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    mls_id: str | None = None
    due_at: str | None = None


class BookingCreate(BaseModel):
    name: str
    email: str
    phone: str = ""
    property_address: str
    package_id: int
    scheduled_at: str
    addon_ids: list[int] = Field(default_factory=list)
    signer_name: str = ""
    message: str = ""


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


@router.post("/listings", status_code=201)
async def api_create_listing(body: ListingCreate, _: str = Depends(_api_tenant)):
    lid = listings.create_listing(
        body.title,
        client_id=body.client_id,
        property_type=body.property_type,
        address_line1=body.address_line1,
        city=body.city,
        state=body.state,
        zip_code=body.zip_code,
        mls_id=body.mls_id,
    )
    row = listings.get_listing(lid)
    return {"id": lid, "title": row["title"], "status": row["status"]}


@router.patch("/listings/{listing_id}")
async def api_patch_listing(
    listing_id: int,
    body: ListingPatch,
    _: str = Depends(_api_tenant),
):
    listings.get_listing(listing_id)
    fields = body.model_dump(exclude_unset=True)
    if fields:
        listings.update_listing(listing_id, **fields)
    row = listings.get_listing(listing_id)
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "due_at": row["due_at"],
    }


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


@router.post("/bookings", status_code=201)
async def api_create_booking(body: BookingCreate, _: str = Depends(_api_tenant)):
    if not scheduling.slot_is_open(body.scheduled_at):
        raise HTTPException(status_code=409, detail="slot not available")
    signer = body.signer_name.strip() or body.name.strip()
    result = commerce.create_booking(
        name=body.name,
        email=body.email,
        phone=body.phone,
        property_address=body.property_address,
        package_id=body.package_id,
        scheduled_at=body.scheduled_at,
        addon_ids=body.addon_ids,
        message=body.message,
        signer_name=signer,
    )
    return result


@router.get("/bookings/{inquiry_id}")
async def api_booking_detail(inquiry_id: int, _: str = Depends(_api_tenant)):
    row = db.one(
        "SELECT * FROM inquiries WHERE id=? AND studio_id=?",
        (inquiry_id, str(STUDIO_ID)),
    )
    if not row:
        raise HTTPException(status_code=404)
    return dict(row)


@router.post("/inbound/{event_name}")
async def api_inbound_event(event_name: str, request: Request, _: str = Depends(_api_tenant)):
    """Inbound automation hook — e.g. POST /api/v1/inbound/listing.delivered with listing_id."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")
    if event_name == "listing.delivered":
        lid = payload.get("listing_id")
        if not lid:
            raise HTTPException(status_code=400, detail="listing_id required")
        listings.update_listing(int(lid), status="delivered")
        return {"ok": True, "listing_id": int(lid), "status": "delivered"}
    if event_name == "listing.booked":
        lid = payload.get("listing_id")
        if not lid:
            raise HTTPException(status_code=400, detail="listing_id required")
        listings.update_listing(int(lid), status="booked")
        return {"ok": True, "listing_id": int(lid), "status": "booked"}
    raise HTTPException(status_code=404, detail="unknown inbound event")


@router.post("/mls/export-ready")
async def api_mls_export_ready(request: Request, _: str = Depends(_api_tenant)):
    """MLS push webhook stub — logs payload for partner integration."""
    import json
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    lid = payload.get("listing_id")
    db.run(
        "INSERT INTO mls_push_log (studio_id, listing_id, payload, status) VALUES (?,?,?,?)",
        (str(STUDIO_ID), int(lid) if lid else None, json.dumps(payload), "received"),
    )
    return {"ok": True, "message": "MLS export logged — configure partner connector for auto-upload"}


@router.get("/health")
async def api_health():
    return {"ok": True}


@router.get("/me")
async def api_me(_: str = Depends(_api_tenant)):
    from .. import plan_limits, usage
    return {
        "ok": True,
        "studio_id": str(STUDIO_ID),
        "plan_tier": plan_limits.current_tier(),
        "usage": usage.snapshot(),
    }