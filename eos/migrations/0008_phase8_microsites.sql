-- Phase 8: property microsites and marketing kit.

ALTER TABLE listings ADD COLUMN site_slug TEXT;
ALTER TABLE listings ADD COLUMN site_published INTEGER NOT NULL DEFAULT 0;
ALTER TABLE listings ADD COLUMN site_description TEXT NOT NULL DEFAULT '';
ALTER TABLE listings ADD COLUMN site_lead_capture INTEGER NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_site_slug ON listings(studio_id, site_slug) WHERE site_slug IS NOT NULL;

ALTER TABLE studio_profiles ADD COLUMN auto_publish_site INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS listing_marketing_kit (
    listing_id      INTEGER PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','building','ready','failed')),
    ig_square       TEXT NOT NULL DEFAULT '',
    ig_story        TEXT NOT NULL DEFAULT '',
    flyer           TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);