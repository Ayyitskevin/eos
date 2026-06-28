-- Phase 10: multi-tenant SaaS platform layer.

ALTER TABLE studio ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE studio ADD COLUMN custom_domain TEXT;
ALTER TABLE studio ADD COLUMN plan_tier TEXT NOT NULL DEFAULT 'solo'
    CHECK (plan_tier IN ('solo','trial','starter','pro'));
ALTER TABLE studio ADD COLUMN stripe_customer_id TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS api_tokens (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    label           TEXT NOT NULL DEFAULT 'API',
    token_prefix    TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_studio ON api_tokens(studio_id);

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    label           TEXT NOT NULL DEFAULT 'Webhook',
    url             TEXT NOT NULL,
    secret          TEXT NOT NULL,
    events          TEXT NOT NULL DEFAULT '["booking.created","listing.delivered","invoice.paid"]',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_webhooks_studio ON webhook_subscriptions(studio_id);

CREATE TABLE IF NOT EXISTS referral_codes (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    code            TEXT NOT NULL,
    credit_cents    INTEGER NOT NULL DEFAULT 2500,
    referrer_client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    uses            INTEGER NOT NULL DEFAULT 0,
    max_uses        INTEGER,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, code)
);