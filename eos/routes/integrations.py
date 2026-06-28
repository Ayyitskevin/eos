"""OAuth connect/disconnect for Google Calendar and Dropbox."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from .. import security, studio
from ..integrations import dropbox, google_calendar

router = APIRouter()


@router.get("/admin/integrations/google/connect")
async def google_connect(_: None = Depends(security.require_admin)):
    if not google_calendar.is_configured():
        raise HTTPException(status_code=503, detail="Google Calendar is not configured on this server")
    return RedirectResponse(google_calendar.connect_url(), status_code=303)


@router.get("/oauth/google/callback")
async def google_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse("/admin/studio?oauth_error=google", status_code=303)
    try:
        google_calendar.handle_callback(code=code, state=state)
    except ValueError:
        return RedirectResponse("/admin/studio?oauth_error=google", status_code=303)
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/admin/integrations/google/disconnect")
async def google_disconnect(_: None = Depends(security.require_admin)):
    google_calendar.disconnect()
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.get("/admin/integrations/dropbox/connect")
async def dropbox_connect(_: None = Depends(security.require_admin)):
    if not dropbox.is_configured():
        raise HTTPException(status_code=503, detail="Dropbox is not configured on this server")
    return RedirectResponse(dropbox.connect_url(), status_code=303)


@router.get("/oauth/dropbox/callback")
async def dropbox_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse("/admin/studio?oauth_error=dropbox", status_code=303)
    try:
        dropbox.handle_callback(code=code, state=state)
    except ValueError:
        return RedirectResponse("/admin/studio?oauth_error=dropbox", status_code=303)
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/admin/integrations/dropbox/disconnect")
async def dropbox_disconnect(_: None = Depends(security.require_admin)):
    dropbox.disconnect()
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/admin/integrations/dropbox/settings")
async def dropbox_settings(
    _: None = Depends(security.require_admin),
    dropbox_watch_path: str = "",
    dropbox_default_listing_id: str = "",
):
    lid = int(dropbox_default_listing_id) if dropbox_default_listing_id.strip().isdigit() else None
    studio.update_profile(
        dropbox_watch_path=dropbox_watch_path.strip() or "/Eos Ingest",
        dropbox_default_listing_id=lid,
    )
    return RedirectResponse("/admin/studio#integrations", status_code=303)


@router.post("/admin/integrations/google/settings")
async def google_settings(
    _: None = Depends(security.require_admin),
    google_calendar_id: str = "primary",
):
    studio.update_profile(google_calendar_id=google_calendar_id.strip() or "primary")
    return RedirectResponse("/admin/studio#integrations", status_code=303)