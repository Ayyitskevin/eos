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

from . import billing_gate, bootstrap, config, db, jobs, scheduler, security, tenant
from .render import ROOT, templates
from .routes import (
    activity, appointments, auth, booking, brand_kits, clients, contracts_admin, dashboard,
    portal as portal_routes,
    microsites as microsite_routes,
    reports as reports_routes, kanban as kanban_routes,
    signup as signup_routes, api_v1, integrations, platform_billing as platform_billing_routes,
    delivery, docs, downloads, emails, galleries_admin, invoices_admin, listings,
    media, pay, proposals_admin, questionnaires, sequences_admin, site, studio_admin,
    today, uploads,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("eos.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.migrate()
    bootstrap.maybe_bootstrap()
    jobs.start()
    scheduler.start()
    log.info("Eos up on :%s · data=%s · saas=%s", config.PORT, config.DATA_DIR, config.SAAS_MODE)
    yield
    scheduler.stop()
    jobs.stop()


app = FastAPI(
    title="Eos", version="1.2.0", lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

_ERROR_MESSAGES = {
    403: "You need to unlock this page first — use the link and PIN from your email.",
    404: "That link doesn't go anywhere — double-check it, or contact your photographer.",
    410: "This gallery has expired. Get in touch to have it re-opened.",
}


@app.middleware("http")
async def request_id(request: Request, call_next):
    import uuid
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = rid
    resp = await call_next(request)
    resp.headers["X-Request-Id"] = rid
    return resp


@app.middleware("http")
async def tenant_context(request: Request, call_next):
    tenant.bind_request(request)
    blocked = billing_gate.check_access(request)
    if blocked:
        return blocked
    security.validate_csrf(request)
    return await call_next(request)


@app.middleware("http")
async def common_headers(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/admin"):
        security.set_csrf_cookie(resp)
    p = request.url.path
    if not (p in site.INDEXABLE or p.startswith(("/static/", "/q/", "/l/", "/api/", "/oauth/"))):
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
        "version": "1.2.0",
        "jobs_pending": jobs.pending_count(),
    }


for r in (
    auth.router, dashboard.router, clients.router, listings.router,
    galleries_admin.router, uploads.router, media.router,
    delivery.router, downloads.router, brand_kits.router,
    invoices_admin.router, pay.router, appointments.router,
    proposals_admin.router, contracts_admin.router, docs.router, emails.router,
    questionnaires.admin, questionnaires.router, studio_admin.router, today.router,
    activity.router, sequences_admin.router, booking.router, portal_routes.router,
    microsite_routes.router, reports_routes.router, kanban_routes.router,
    signup_routes.router, api_v1.router, integrations.router,
    platform_billing_routes.router, site.router,
):
    app.include_router(r)