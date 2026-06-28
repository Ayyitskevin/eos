"""Granular RBAC — owner, operator, scheduler, editor, accountant."""

from fastapi import HTTPException, Request

from . import security, users

ROLES = ("owner", "operator", "scheduler", "editor", "accountant")

# Permission sets per role (owner always has all).
ROLE_PERMS: dict[str, frozenset[str]] = {
    "owner": frozenset({"*"}),
    "operator": frozenset({
        "*",
    }),
    "scheduler": frozenset({
        "calendar", "listings.read", "listings.write", "clients.read", "today",
    }),
    "editor": frozenset({
        "listings.read", "listings.write", "galleries", "deliveries", "today",
    }),
    "accountant": frozenset({
        "reports", "invoices", "clients.read", "listings.read",
    }),
}

# Routes blocked unless owner/operator.
OWNER_ONLY_PREFIXES = (
    "/admin/studio",
    "/admin/billing",
    "/admin/sequences",
    "/admin/onboarding",
)


def role_for_request(request: Request) -> str:
    from . import db, tenant

    uid = security.current_user_id(request)
    if not uid:
        return "owner"
    row = db.one("SELECT role, studio_id FROM users WHERE id=? AND active=1", (uid,))
    if not row or row["studio_id"] != tenant.get_studio_id():
        return "owner"
    return row["role"]


def has_perm(role: str, perm: str) -> bool:
    perms = ROLE_PERMS.get(role, frozenset())
    return "*" in perms or perm in perms


def check_route(request: Request) -> None:
    """Called from middleware — block disallowed admin paths by role."""
    path = request.url.path
    if not path.startswith("/admin"):
        return
    if path in ("/admin/login", "/admin/logout", ""):
        return
    role = role_for_request(request)
    if role in ("owner", "operator"):
        return
    for prefix in OWNER_ONLY_PREFIXES:
        if path.startswith(prefix):
            raise HTTPException(status_code=403, detail="This area requires owner access.")
    if path.startswith("/admin/calendar") or path.startswith("/admin/appointments"):
        if not has_perm(role, "calendar"):
            raise HTTPException(status_code=403, detail="Scheduler access required.")
    elif path.startswith("/admin/galleries") or path.startswith("/admin/galleries"):
        if not has_perm(role, "galleries"):
            raise HTTPException(status_code=403, detail="Editor access required.")
    elif path.startswith("/admin/reports"):
        if not has_perm(role, "reports"):
            raise HTTPException(status_code=403, detail="Accountant access required.")
    elif path.startswith("/admin/invoices"):
        if not has_perm(role, "invoices"):
            raise HTTPException(status_code=403, detail="Accountant access required.")


def require_perm(perm: str):
    def _dep(request: Request) -> None:
        security.require_admin(request)
        role = role_for_request(request)
        if not has_perm(role, perm):
            raise HTTPException(status_code=403, detail="Insufficient permissions.")
    return _dep