-- Embeddable booking widget + property-site lead capture (Wave 1, items 4-5).
--
-- Studios can embed /book/embed in an iframe on their own site; the CSP
-- frame-ancestors allowlist lives on the studio profile ('*' = anywhere).
-- Buyer leads captured on property microsites stay in the inquiries table
-- (status 'inquiry') with a listing link and a hashed submitter IP — never a
-- raw IP at rest. Studio/agent notifications go out through durable intents
-- persisted before any provider I/O, same pattern as analytics digests.

ALTER TABLE studio_profiles ADD COLUMN embed_allowed_domains TEXT NOT NULL DEFAULT '*';
ALTER TABLE studio_profiles ADD COLUMN lead_capture_enabled INTEGER NOT NULL DEFAULT 1;

ALTER TABLE inquiries ADD COLUMN ip_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE inquiries ADD COLUMN contacted INTEGER NOT NULL DEFAULT 0;

CREATE TABLE lead_notify_intents (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    inquiry_id      INTEGER NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
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
    UNIQUE (studio_id, inquiry_id)
);
CREATE INDEX idx_lead_notify_recovery
    ON lead_notify_intents(studio_id, status, created_at);
