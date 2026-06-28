-- Phase 7: agent portal, pay-to-download, listing media embeds.

ALTER TABLE clients ADD COLUMN portal_token TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_portal_token ON clients(portal_token) WHERE portal_token IS NOT NULL;

ALTER TABLE studio_profiles ADD COLUMN pay_to_download INTEGER NOT NULL DEFAULT 1;
ALTER TABLE studio_profiles ADD COLUMN watermark_until_paid INTEGER NOT NULL DEFAULT 1;
ALTER TABLE studio_profiles ADD COLUMN auto_deliver_email INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS listing_media (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    listing_id      INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL DEFAULT 'url'
                    CHECK (kind IN ('matterport','youtube','vimeo','iguide','url')),
    label           TEXT NOT NULL DEFAULT '',
    embed_url       TEXT NOT NULL,
    position        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_listing_media_listing ON listing_media(listing_id);