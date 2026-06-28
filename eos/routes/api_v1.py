from fastapi import APIRouter, Depends, Request

from .. import api_tokens, inquiries, listings, tenant
from ..vocab import STUDIO_ID

router = APIRouter(prefix="/api/v1")


async def _api_tenant(request: Request):
    studio_id = api_tokens.authenticate_request(request)
    tenant.set_studio(studio_id)
    return studio_id


@router.get("/listings")
async def api_listings(_: str = Depends(_api_tenant)):
    rows = listings.list_listings()
    return [
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


@router.get("/bookings")
async def api_bookings(_: str = Depends(_api_tenant)):
    rows = inquiries.list_confirmed()
    return [
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


@router.get("/health")
async def api_health():
    return {"ok": True, "studio_id": str(STUDIO_ID)}