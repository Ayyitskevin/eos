-- Phase 6: smart booking, add-ons, promo codes, deposit checkout.

ALTER TABLE studio_profiles ADD COLUMN booking_enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE studio_profiles ADD COLUMN min_notice_hours INTEGER NOT NULL DEFAULT 24;
ALTER TABLE studio_profiles ADD COLUMN buffer_minutes INTEGER NOT NULL DEFAULT 30;
ALTER TABLE studio_profiles ADD COLUMN slot_minutes INTEGER NOT NULL DEFAULT 90;
ALTER TABLE studio_profiles ADD COLUMN day_start_min INTEGER NOT NULL DEFAULT 480;
ALTER TABLE studio_profiles ADD COLUMN day_end_min INTEGER NOT NULL DEFAULT 1080;
ALTER TABLE studio_profiles ADD COLUMN book_weekdays TEXT NOT NULL DEFAULT '0,1,2,3,4,5';

ALTER TABLE service_packages ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 90;
ALTER TABLE service_packages ADD COLUMN max_sqft INTEGER;

UPDATE service_packages SET deposit_cents=5000 WHERE name='Standard Listing' AND deposit_cents=0;
UPDATE service_packages SET deposit_cents=7500 WHERE name='Premium Listing' AND deposit_cents=0;
UPDATE service_packages SET deposit_cents=10000 WHERE name='Luxury Estate' AND deposit_cents=0;

CREATE TABLE IF NOT EXISTS service_addons (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    slug            TEXT NOT NULL,
    name            TEXT NOT NULL,
    price_cents     INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    position        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, slug)
);

INSERT OR IGNORE INTO service_addons (studio_id, slug, name, price_cents, position)
VALUES
    ('default', 'drone', 'Aerial / drone photos', 10000, 10),
    ('default', 'twilight', 'Twilight shoot', 15000, 20),
    ('default', 'matterport', 'Matterport 3D tour', 12500, 30),
    ('default', 'rush', 'Rush delivery (same day)', 7500, 40);

CREATE TABLE IF NOT EXISTS promo_codes (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    code            TEXT NOT NULL,
    discount_cents  INTEGER NOT NULL DEFAULT 0,
    discount_pct    INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, code)
);

ALTER TABLE inquiries ADD COLUMN status TEXT NOT NULL DEFAULT 'inquiry'
    CHECK (status IN ('inquiry','pending_payment','confirmed','canceled'));
ALTER TABLE inquiries ADD COLUMN package_id INTEGER REFERENCES service_packages(id);
ALTER TABLE inquiries ADD COLUMN addon_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE inquiries ADD COLUMN sqft INTEGER;
ALTER TABLE inquiries ADD COLUMN scheduled_at TEXT;
ALTER TABLE inquiries ADD COLUMN listing_id INTEGER REFERENCES listings(id);
ALTER TABLE inquiries ADD COLUMN client_id INTEGER REFERENCES clients(id);
ALTER TABLE inquiries ADD COLUMN appointment_id INTEGER REFERENCES appointments(id);
ALTER TABLE inquiries ADD COLUMN invoice_id INTEGER REFERENCES invoices(id);
ALTER TABLE inquiries ADD COLUMN order_token TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_inquiries_order_token ON inquiries(order_token) WHERE order_token IS NOT NULL;
ALTER TABLE inquiries ADD COLUMN signer_name TEXT NOT NULL DEFAULT '';
ALTER TABLE inquiries ADD COLUMN promo_code TEXT NOT NULL DEFAULT '';
ALTER TABLE inquiries ADD COLUMN total_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE inquiries ADD COLUMN deposit_cents INTEGER NOT NULL DEFAULT 0;

ALTER TABLE appointments ADD COLUMN inquiry_id INTEGER REFERENCES inquiries(id);
ALTER TABLE appointments ADD COLUMN ends_at TEXT;

ALTER TABLE invoices ADD COLUMN invoice_kind TEXT NOT NULL DEFAULT 'full'
    CHECK (invoice_kind IN ('full','deposit','balance'));
ALTER TABLE invoices ADD COLUMN inquiry_id INTEGER REFERENCES inquiries(id);