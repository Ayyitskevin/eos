"""Explicit, fail-closed RBAC for every supported admin route family."""

from __future__ import annotations

import re

from fastapi import HTTPException, Request

from . import security

ROLES = ("owner", "operator", "scheduler", "editor", "accountant")
_READ_METHODS = frozenset({"GET", "HEAD"})

# Owner bypasses route permissions. Every other role receives only these grants.
ROLE_PERMS: dict[str, frozenset[str]] = {
    "owner": frozenset({"*"}),
    "operator": frozenset(
        {
            "dashboard.read",
            "today.read",
            "activity.read",
            "clients.read",
            "clients.write",
            "listings.read",
            "listings.write",
            "calendar.read",
            "calendar.write",
            "galleries.read",
            "galleries.write",
            "reports.read",
            "invoices.read",
            "invoices.write",
        }
    ),
    "scheduler": frozenset(
        {
            "dashboard.read",
            "today.read",
            "calendar.read",
            "calendar.write",
            "listings.read",
            "listings.write",
            "clients.read",
        }
    ),
    "editor": frozenset(
        {
            "dashboard.read",
            "today.read",
            "listings.read",
            "listings.write",
            "galleries.read",
            "galleries.write",
        }
    ),
    "accountant": frozenset(
        {
            "dashboard.read",
            "reports.read",
            "invoices.read",
            "invoices.write",
            "clients.read",
            "listings.read",
        }
    ),
}

# Compatibility aliases for callers that predate read/write route separation.
_PERMISSION_ALIASES = {
    "calendar": "calendar.read",
    "galleries": "galleries.read",
    "reports": "reports.read",
    "invoices": "invoices.read",
    "today": "today.read",
}

_PUBLIC_ADMIN_PATHS = frozenset(
    {
        "/admin/login",
        "/admin/logout",
    }
)

# These areas contain tenant configuration, billing, credentials, integrations,
# outbound messaging, or platform controls. Authenticated non-owners never enter.
_OWNER_ONLY_PREFIXES = (
    "/admin/studio",
    "/admin/billing",
    "/admin/onboarding",
    "/admin/verify-pending",
    "/admin/sequences",
    "/admin/stripe",
    "/admin/integrations",
    "/admin/platform",
    "/admin/email",
    "/admin/sent",
    "/admin/rebooking",
)

# Nested routes whose permission belongs to a different family than their prefix.
_SPECIAL_ROUTES: tuple[tuple[re.Pattern[str], str | None], ...] = (
    (re.compile(r"/admin/listings/[^/]+/invoice"), "invoices.write"),
    (re.compile(r"/admin/listings/[^/]+/gallery"), "galleries.write"),
    (re.compile(r"/admin/listings/[^/]+/(?:proposals|contracts)"), None),
    (re.compile(r"/admin/clients/[^/]+/credit"), None),
    (re.compile(r"/admin/galleries/[^/]+/email"), None),
)

# Prefix, read permission, mutation permission. A missing mutation permission is
# deliberately owner-only. Unknown admin routes are also owner-only.
_ROUTE_FAMILIES: tuple[tuple[tuple[str, ...], str, str | None], ...] = (
    (("/admin/clients",), "clients.read", "clients.write"),
    (("/admin/listings", "/admin/kanban"), "listings.read", "listings.write"),
    (("/admin/proposals", "/admin/contracts"), "listings.read", None),
    (("/admin/calendar", "/admin/appointments"), "calendar.read", "calendar.write"),
    (("/admin/galleries",), "galleries.read", "galleries.write"),
    (("/admin/reports", "/admin/brokerages"), "reports.read", None),
    (("/admin/invoices",), "invoices.read", "invoices.write"),
    (("/admin/today",), "today.read", None),
    (("/admin/activity",), "activity.read", None),
)


def _in_family(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _required_permission(method: str, path: str) -> str | None:
    for pattern, permission in _SPECIAL_ROUTES:
        if pattern.fullmatch(path):
            return permission
    read = method.upper() in _READ_METHODS
    for prefixes, read_permission, write_permission in _ROUTE_FAMILIES:
        if any(_in_family(path, prefix) for prefix in prefixes):
            return read_permission if read else write_permission
    if path == "/admin" and read:
        return "dashboard.read"
    return None


def role_for_request(request: Request) -> str:
    from . import db, tenant

    uid = security.current_user_id(request)
    if not uid:
        # The password-only legacy admin session is the single-studio owner.
        return "owner"
    row = db.one("SELECT role, studio_id FROM users WHERE id=? AND active=1", (uid,))
    if not row or row["studio_id"] != tenant.get_studio_id() or row["role"] not in ROLES:
        return "invalid"
    return row["role"]


def has_perm(role: str, perm: str) -> bool:
    resolved = _PERMISSION_ALIASES.get(perm, perm)
    perms = ROLE_PERMS.get(role, frozenset())
    return "*" in perms or resolved in perms


def check_route(request: Request) -> None:
    """Block every undeclared admin route for authenticated non-owner roles."""
    path = request.url.path.rstrip("/") or "/"
    if not _in_family(path, "/admin"):
        return
    if path in _PUBLIC_ADMIN_PATHS:
        return

    role = role_for_request(request)
    if role == "owner":
        return
    if any(_in_family(path, prefix) for prefix in _OWNER_ONLY_PREFIXES):
        raise HTTPException(status_code=403, detail="This area requires owner access.")

    permission = _required_permission(request.method, path)
    if permission is None or not has_perm(role, permission):
        raise HTTPException(status_code=403, detail="Insufficient permissions for this route.")


def require_perm(perm: str):
    def _dep(request: Request) -> None:
        security.require_admin(request)
        role = role_for_request(request)
        if not has_perm(role, perm):
            raise HTTPException(status_code=403, detail="Insufficient permissions.")

    return _dep


def require_owner(request: Request) -> None:
    """Require an active owner user whose session belongs to this tenant."""
    from . import db, tenant

    security.require_admin(request)
    uid = security.current_user_id(request)
    if uid is None:
        raise HTTPException(status_code=403, detail="This area requires owner access.")
    row = db.one(
        """SELECT u.role, u.studio_id
           FROM users u
           JOIN studio s ON s.id=u.studio_id AND s.active=1
           WHERE u.id=? AND u.active=1""",
        (uid,),
    )
    if not row or row["studio_id"] != tenant.get_studio_id() or row["role"] != "owner":
        raise HTTPException(status_code=403, detail="This area requires owner access.")
