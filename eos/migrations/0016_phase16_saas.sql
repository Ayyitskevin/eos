-- Phase 16 — multi-tenant SaaS pivot (Stripe Connect, domain verify, platform ops)

ALTER TABLE studio ADD COLUMN stripe_connect_account_id TEXT NOT NULL DEFAULT '';
ALTER TABLE studio ADD COLUMN stripe_connect_charges_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE studio ADD COLUMN domain_verify_token TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS platform_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id   INTEGER,
    action          TEXT NOT NULL,
    studio_id       TEXT NOT NULL DEFAULT '',
    detail          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_platform_audit_studio ON platform_audit(studio_id);