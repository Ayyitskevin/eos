-- Gallery-view analytics + scheduled agent digest emails.
--
-- Privacy-preserving public traffic events: no raw IPs at rest (visitor_key is
-- a truncated SHA-256 of IP + user-agent) and only the referrer domain is kept
-- (query strings stripped). Weekly per-agent digests go out through durable,
-- replay-safe intents persisted before any provider I/O.

CREATE TABLE view_events (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL CHECK (event_type IN ('gallery_view','microsite_view')),
    listing_id      INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    gallery_id      INTEGER REFERENCES galleries(id) ON DELETE SET NULL,
    visitor_key     TEXT NOT NULL DEFAULT '',
    referrer_domain TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_view_events_studio_time
    ON view_events(studio_id, created_at);
CREATE INDEX idx_view_events_listing
    ON view_events(studio_id, listing_id, created_at);
CREATE INDEX idx_view_events_gallery
    ON view_events(studio_id, gallery_id, created_at);

CREATE TABLE analytics_digest_intents (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    event_key       TEXT NOT NULL,
    to_email        TEXT NOT NULL,
    subject         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','sent','failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    claimed_at      TEXT,
    attempt_token   TEXT,
    sent_at         TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, event_key)
);
CREATE INDEX idx_analytics_digest_recovery
    ON analytics_digest_intents(studio_id, status, created_at);

ALTER TABLE studio_profiles ADD COLUMN analytics_digest_enabled INTEGER NOT NULL DEFAULT 1;
