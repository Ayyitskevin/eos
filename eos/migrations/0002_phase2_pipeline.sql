-- Phase 2: imaging pipeline, export presets, Stripe invoices, job retries.

ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN error TEXT;
ALTER TABLE jobs ADD COLUMN updated_at TEXT NOT NULL DEFAULT (datetime('now'));

ALTER TABLE crop_presets ADD COLUMN centering_x REAL NOT NULL DEFAULT 0.5;
ALTER TABLE crop_presets ADD COLUMN centering_y REAL NOT NULL DEFAULT 0.5;

ALTER TABLE brand_kits ADD COLUMN margin_pct INTEGER NOT NULL DEFAULT 2
    CHECK (margin_pct BETWEEN 0 AND 25);

ALTER TABLE invoices ADD COLUMN stripe_session_id TEXT;
ALTER TABLE invoices ADD COLUMN line_items TEXT NOT NULL DEFAULT '[]';
ALTER TABLE invoices ADD COLUMN notes TEXT NOT NULL DEFAULT '';

-- MLS/Zillow exports get agent watermark when a brand kit exists.
UPDATE crop_presets SET brand_overlay=1
WHERE studio_id='default' AND slug IN ('mls-3x2', 'zillow-16x9');