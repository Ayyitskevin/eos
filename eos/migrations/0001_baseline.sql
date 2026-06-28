-- 0001_baseline — Eos RE photography OS (solo-operator foundation, SaaS-ready schema).
-- Inspired by Mise (gallery delivery + studio CRM) and Hestia (packages, questionnaires,
-- appointments). Vertical-locked to real estate: listings are first-class, not generic projects.

-- Solo studio row (single operator today; studio_id on every table for a future SaaS tier).
CREATE TABLE IF NOT EXISTS studio (
    id              TEXT PRIMARY KEY DEFAULT 'default',
    name            TEXT NOT NULL DEFAULT 'Eos Studio',
    slug            TEXT NOT NULL UNIQUE DEFAULT 'studio',
    timezone        TEXT NOT NULL DEFAULT 'America/New_York',
    contact_email   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO studio (id, name, slug) VALUES ('default', 'Eos Studio', 'studio');

CREATE TABLE IF NOT EXISTS studio_profiles (
    studio_id       TEXT PRIMARY KEY REFERENCES studio(id) ON DELETE CASCADE,
    headline        TEXT NOT NULL DEFAULT '',
    about           TEXT NOT NULL DEFAULT '',
    service_area    TEXT NOT NULL DEFAULT '',
    published       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Agents, brokerages, homeowners — parent_id models broker → agent (Mise client hierarchy).
CREATE TABLE IF NOT EXISTS clients (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    parent_id       INTEGER REFERENCES clients(id) ON DELETE RESTRICT
                    CHECK (parent_id IS NULL OR parent_id <> id),
    client_type     TEXT NOT NULL DEFAULT 'agent'
                    CHECK (client_type IN ('agent','brokerage','homeowner','vendor')),
    name            TEXT NOT NULL,
    company         TEXT NOT NULL DEFAULT '',
    email           TEXT NOT NULL DEFAULT '',
    phone           TEXT NOT NULL DEFAULT '',
    license_number  TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_clients_studio ON clients(studio_id);
CREATE INDEX IF NOT EXISTS idx_clients_parent ON clients(parent_id);

-- A listing is the RE spine (address + MLS + turnaround), not a generic "project".
CREATE TABLE IF NOT EXISTS listings (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    client_id       INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'lead'
                    CHECK (status IN ('lead','booked','shooting','editing','delivered','archived')),
    property_type   TEXT NOT NULL DEFAULT 'residential'
                    CHECK (property_type IN ('residential','commercial','land','multi_family','other')),
    address_line1   TEXT NOT NULL DEFAULT '',
    address_line2   TEXT NOT NULL DEFAULT '',
    city            TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL DEFAULT '',
    zip             TEXT NOT NULL DEFAULT '',
    mls_id          TEXT NOT NULL DEFAULT '',
    beds            REAL,
    baths           REAL,
    sqft            INTEGER,
    shoot_date      TEXT,
    due_at          TEXT,
    access_notes    TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_listings_studio ON listings(studio_id, status);
CREATE INDEX IF NOT EXISTS idx_listings_client ON listings(client_id);
CREATE INDEX IF NOT EXISTS idx_listings_due ON listings(due_at);

-- Room-driven shot list (RE vocab — not F&B dish categories).
CREATE TABLE IF NOT EXISTS listing_shots (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    listing_id      INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    room            TEXT NOT NULL DEFAULT 'other',
    title           TEXT NOT NULL,
    priority        TEXT NOT NULL DEFAULT 'must'
                    CHECK (priority IN ('must','want','if_time')),
    done            INTEGER NOT NULL DEFAULT 0,
    position        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_listing_shots_listing ON listing_shots(listing_id, position);

-- Per-listing checklist (Hestia project_tasks pattern).
CREATE TABLE IF NOT EXISTS listing_tasks (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    listing_id      INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    label           TEXT NOT NULL,
    done            INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_listing_tasks_listing ON listing_tasks(listing_id);

-- Galleries: Mise-style delivery with room sections.
CREATE TABLE IF NOT EXISTS galleries (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    listing_id      INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    slug            TEXT NOT NULL,
    title           TEXT NOT NULL,
    client_name     TEXT,
    pin             TEXT NOT NULL,
    delivery_token  TEXT NOT NULL UNIQUE,
    expires_at      TEXT,
    published       INTEGER NOT NULL DEFAULT 0,
    content_rev     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_galleries_listing ON galleries(listing_id);

CREATE TABLE IF NOT EXISTS sections (
    id              INTEGER PRIMARY KEY,
    gallery_id      INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    position        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS assets (
    id              INTEGER PRIMARY KEY,
    gallery_id      INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    section_id      INTEGER REFERENCES sections(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL DEFAULT 'photo' CHECK (kind IN ('photo','video')),
    filename        TEXT NOT NULL,
    stored          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','ready','failed')),
    width           INTEGER,
    height          INTEGER,
    bytes           INTEGER,
    position        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_assets_gallery ON assets(gallery_id, section_id, position);

-- MLS / portal export presets (Mise crop_presets pattern).
CREATE TABLE IF NOT EXISTS crop_presets (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    slug            TEXT NOT NULL,
    name            TEXT NOT NULL,
    ratio_label     TEXT NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    target_channel  TEXT,
    brand_overlay   INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    sort            INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, slug)
);

INSERT OR IGNORE INTO crop_presets (studio_id, slug, name, ratio_label, width, height, target_channel, sort)
VALUES
    ('default', 'mls-3x2', 'MLS Standard (3:2)', '3:2', 2048, 1365, 'mls', 10),
    ('default', 'zillow-16x9', 'Zillow Hero (16:9)', '16:9', 1920, 1080, 'zillow', 20),
    ('default', 'ig-4x5', 'Instagram (4:5)', '4:5', 1080, 1350, 'instagram', 30);

-- Agent watermark kits (Mise brand_kits).
CREATE TABLE IF NOT EXISTS brand_kits (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    label           TEXT NOT NULL DEFAULT 'Logo',
    stored          TEXT NOT NULL DEFAULT '',
    position        TEXT NOT NULL DEFAULT 'br'
                    CHECK (position IN ('tl','tc','tr','ml','c','mr','bl','bc','br')),
    opacity         INTEGER NOT NULL DEFAULT 100 CHECK (opacity BETWEEN 0 AND 100),
    scale_pct       INTEGER NOT NULL DEFAULT 18 CHECK (scale_pct BETWEEN 1 AND 100),
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Service menu (Hestia service_packages).
CREATE TABLE IF NOT EXISTS service_packages (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    price_cents     INTEGER NOT NULL DEFAULT 0,
    deposit_cents   INTEGER NOT NULL DEFAULT 0,
    turnaround_hours INTEGER NOT NULL DEFAULT 24,
    active          INTEGER NOT NULL DEFAULT 1,
    position        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO service_packages (studio_id, name, description, price_cents, turnaround_hours, position)
VALUES
    ('default', 'Standard Listing', 'Up to 25 photos · 24hr turnaround', 17500, 24, 10),
    ('default', 'Premium Listing', 'Up to 40 photos · twilight add-on · 24hr turnaround', 27500, 24, 20),
    ('default', 'Luxury Estate', 'Full coverage · drone-ready naming · 12hr rush available', 45000, 12, 30);

-- Studio documents (foundation — proposals/contracts/invoices expand in phase 2).
CREATE TABLE IF NOT EXISTS proposals (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    listing_id      INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    line_items      TEXT NOT NULL DEFAULT '[]',
    total_cents     INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','sent','accepted','declined')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    listing_id      INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    client_id       INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','sent','paid','void')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    paid_at         TEXT
);

-- Appointments (Hestia scheduler — simplified for solo RE shoots).
CREATE TABLE IF NOT EXISTS appointments (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    listing_id      INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    client_id       INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'shoot'
                    CHECK (kind IN ('consultation','shoot','twilight','other')),
    status          TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed','confirmed','canceled','completed')),
    starts_at       TEXT,
    location        TEXT NOT NULL DEFAULT '',
    token           TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT,
    actor           TEXT NOT NULL DEFAULT 'admin',
    action          TEXT NOT NULL,
    detail          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','done','failed')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pin_attempts (
    ip              TEXT NOT NULL,
    gallery_id      INTEGER NOT NULL,
    ts              REAL NOT NULL
);