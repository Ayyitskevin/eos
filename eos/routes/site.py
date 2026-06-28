import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, security, studio
from ..render import templates
from ..vocab import STUDIO_ID

router = APIRouter()
INDEXABLE = {"/", "/book"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    profile = studio.get_profile()
    packages = studio.list_packages()
    return templates.TemplateResponse(
        request, "site/home.html",
        {"profile": profile, "packages": packages},
    )


@router.get("/book", response_class=HTMLResponse)
async def book_form(request: Request):
    profile = studio.get_profile()
    return templates.TemplateResponse(request, "site/book.html", {"profile": profile, "error": None, "thanks": False})


@router.post("/book")
async def book_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    property_address: str = Form(""),
    message: str = Form(""),
):
    ip = security.client_ip(request)
    if security.inquiry_throttled(ip, security.INQUIRY_BUCKET_BOOK):
        raise HTTPException(status_code=429, detail="too many requests")
    email = email.strip().lower()
    if not _EMAIL.match(email):
        return templates.TemplateResponse(
            request, "site/book.html",
            {"profile": studio.get_profile(), "error": "Invalid email.", "thanks": False},
            status_code=400,
        )
    security.inquiry_record(ip, security.INQUIRY_BUCKET_BOOK)
    db.run(
        """INSERT INTO inquiries (studio_id, name, email, phone, message, property_address)
           VALUES (?,?,?,?,?,?)""",
        (STUDIO_ID, name.strip(), email, phone.strip(), message.strip(), property_address.strip()),
    )
    db.audit("public", "inquiry.book", email)
    return templates.TemplateResponse(
        request, "site/book.html",
        {"profile": studio.get_profile(), "error": None, "thanks": True},
    )