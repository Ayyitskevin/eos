"""
Eos — real estate photography OS · FastAPI + HTMX · port 8410

  uvicorn eos.main:app --host 127.0.0.1 --port 8410
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, db, jobs
from .render import ROOT, templates
from .routes import (
    appointments, auth, brand_kits, clients, dashboard, delivery, downloads,
    galleries_admin, invoices_admin, listings, media, pay, site, uploads,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("eos.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.migrate()
    jobs.start()
    log.info("Eos up on :%s · data=%s", config.PORT, config.DATA_DIR)
    yield
    jobs.stop()


app = FastAPI(
    title="Eos", version="0.1.0", lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

_ERROR_MESSAGES = {
    403: "You need to unlock this page first — use the link and PIN from your email.",
    404: "That link doesn't go anywhere — double-check it, or contact your photographer.",
    410: "This gallery has expired. Get in touch to have it re-opened.",
}


@app.middleware("http")
async def common_headers(request: Request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if not (p in site.INDEXABLE or p.startswith("/static/")):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


@app.exception_handler(StarletteHTTPException)
async def branded_errors(request: Request, exc: StarletteHTTPException):
    if exc.status_code in _ERROR_MESSAGES and "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(
            request, "public/error.html",
            {"message": _ERROR_MESSAGES[exc.status_code]},
            status_code=exc.status_code,
        )
    return await http_exception_handler(request, exc)


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "service": "eos",
        "version": "0.2.0",
        "jobs_pending": jobs.pending_count(),
    }


for r in (
    auth.router, dashboard.router, clients.router, listings.router,
    galleries_admin.router, uploads.router, media.router,
    delivery.router, downloads.router, brand_kits.router,
    invoices_admin.router, pay.router, appointments.router,
    site.router,
):
    app.include_router(r)