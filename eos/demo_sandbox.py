"""Read-only demo studio for prospects — /demo routes."""

import logging

from . import config, db, dogfood, studio_seed, tenant, users

log = logging.getLogger("eos.demo_sandbox")

DEMO_SLUG = "demo"
DEMO_STUDIO_ID = "demo"


def ensure_demo() -> str:
    """Create demo studio + seed listing if missing. Returns studio id."""
    row = db.one("SELECT id FROM studio WHERE slug=? AND active=1", (DEMO_SLUG,))
    if row:
        return row["id"]
    db.run(
        """INSERT INTO studio (id, slug, name, active, is_demo, read_only, plan_tier, billing_status)
           VALUES (?,?,?,?,?,?,?,?)""",
        (DEMO_STUDIO_ID, DEMO_SLUG, "Eos Demo Studio", 1, 1, 1, "pro", "active"),
    )
    studio_seed.seed_studio(DEMO_STUDIO_ID)
    tenant.set_studio(DEMO_STUDIO_ID)
    users.create_user(
        "demo@eos.app", "demo-view-only",
        name="Demo Viewer", role="owner", studio_id=DEMO_STUDIO_ID,
    )
    dogfood.seed()
    log.info("demo sandbox studio created")
    return DEMO_STUDIO_ID


def is_read_only(studio_id: str | None = None) -> bool:
    sid = studio_id or tenant.get_studio_id()
    row = db.one("SELECT read_only FROM studio WHERE id=?", (sid,))
    return bool(row and row["read_only"])


def demo_base_url() -> str | None:
    if not config.BASE_DOMAIN:
        return f"{config.BASE_URL.rstrip('/')}/demo"
    scheme = "https" if config.COOKIE_SECURE else "http"
    return f"{scheme}://{DEMO_SLUG}.{config.BASE_DOMAIN}"