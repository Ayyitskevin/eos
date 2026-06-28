-- Phase 13: calendar depth, agent favorites, plan limits, upsell checkout, revisions.

ALTER TABLE assets ADD COLUMN agent_favorite INTEGER NOT NULL DEFAULT 0;

ALTER TABLE listings ADD COLUMN revision_round INTEGER NOT NULL DEFAULT 0;
ALTER TABLE listings ADD COLUMN revision_notes TEXT;

ALTER TABLE clients ADD COLUMN credit_cents INTEGER NOT NULL DEFAULT 0;

ALTER TABLE studio ADD COLUMN custom_domain_verified INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS studio_usage (
    studio_id           TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    period              TEXT NOT NULL,
    listings_created    INTEGER NOT NULL DEFAULT 0,
    storage_bytes       INTEGER NOT NULL DEFAULT 0,
    api_calls           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (studio_id, period)
);

CREATE TABLE IF NOT EXISTS listing_upsell_orders (
    id                  INTEGER PRIMARY KEY,
    studio_id           TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    listing_id          INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    client_id           INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    addon_ids           TEXT NOT NULL DEFAULT '[]',
    amount_cents        INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','paid','canceled')),
    stripe_session_id   TEXT,
    invoice_id          INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    token               TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_upsell_orders_listing ON listing_upsell_orders(listing_id);