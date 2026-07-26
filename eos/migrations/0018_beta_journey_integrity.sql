-- Beta journey integrity: activation, booking replay, delivery recovery, Stripe replay.

ALTER TABLE studio ADD COLUMN provisioning_status TEXT NOT NULL DEFAULT 'ready'
    CHECK (provisioning_status IN ('provisioning','ready','degraded'));
ALTER TABLE studio ADD COLUMN provisioning_error TEXT;
ALTER TABLE studio ADD COLUMN signup_verify_issued_at TEXT;
ALTER TABLE studio ADD COLUMN platform_checkout_session_id TEXT NOT NULL DEFAULT '';
ALTER TABLE studio ADD COLUMN platform_checkout_plan TEXT NOT NULL DEFAULT '';
ALTER TABLE studio ADD COLUMN platform_checkout_url TEXT NOT NULL DEFAULT '';

ALTER TABLE inquiries ADD COLUMN request_key TEXT;
ALTER TABLE inquiries ADD COLUMN referral_id INTEGER REFERENCES referral_codes(id) ON DELETE SET NULL;
ALTER TABLE inquiries ADD COLUMN payment_expires_at TEXT;
ALTER TABLE inquiries ADD COLUMN payment_expired_at TEXT;
ALTER TABLE inquiries ADD COLUMN credit_applied_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE inquiries ADD COLUMN payment_reconcile_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE inquiries ADD COLUMN payment_reconcile_error TEXT;
ALTER TABLE inquiries ADD COLUMN payment_reconciled_at TEXT;
UPDATE inquiries
SET payment_expires_at=datetime(created_at, '+60 minutes')
WHERE status='pending_payment' AND payment_expires_at IS NULL;

-- Recover legacy manual deposit payments that predate atomic booking confirmation.
UPDATE inquiries
SET status='confirmed', payment_expires_at=NULL, payment_reconcile_error=NULL,
    payment_reconciled_at=COALESCE(payment_reconciled_at, datetime('now'))
WHERE status='pending_payment'
  AND EXISTS (
      SELECT 1 FROM invoices i
      WHERE i.id=inquiries.invoice_id
        AND i.studio_id=inquiries.studio_id
        AND i.invoice_kind='deposit'
        AND i.status='paid'
  );
UPDATE appointments
SET status='confirmed'
WHERE status='proposed'
  AND EXISTS (
      SELECT 1 FROM inquiries q
      WHERE q.appointment_id=appointments.id
        AND q.studio_id=appointments.studio_id
        AND q.status='confirmed'
  );
UPDATE listings
SET status='booked', updated_at=datetime('now')
WHERE status='lead'
  AND EXISTS (
      SELECT 1 FROM inquiries q
      WHERE q.listing_id=listings.id
        AND q.studio_id=listings.studio_id
        AND q.status='confirmed'
  );
UPDATE proposals
SET status='sent', sent_at=COALESCE(sent_at, datetime('now'))
WHERE status='draft'
  AND EXISTS (
      SELECT 1 FROM inquiries q
      WHERE q.listing_id=proposals.listing_id
        AND q.studio_id=proposals.studio_id
        AND q.status='confirmed'
  );
CREATE UNIQUE INDEX IF NOT EXISTS idx_inquiries_request_key
    ON inquiries(studio_id, request_key) WHERE request_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS referral_redemptions (
    id                  INTEGER PRIMARY KEY,
    studio_id           TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    referral_id         INTEGER NOT NULL REFERENCES referral_codes(id) ON DELETE RESTRICT,
    referrer_client_id  INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    referral_code       TEXT NOT NULL,
    inquiry_id          INTEGER NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    referred_client_id  INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    credit_cents        INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'confirmed'
                        CHECK (status IN ('reserved','confirmed','released')),
    expires_at          TEXT,
    finalized_at        TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, inquiry_id)
);
CREATE INDEX IF NOT EXISTS idx_referral_redemptions_referral
    ON referral_redemptions(studio_id, referral_id);

ALTER TABLE listings ADD COLUMN delivered_at TEXT;

ALTER TABLE email_sequence_runs ADD COLUMN event_key TEXT;
ALTER TABLE email_sequence_runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE email_sequence_runs ADD COLUMN claimed_at TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_sequence_run_event
    ON email_sequence_runs(sequence_id, event_key) WHERE event_key IS NOT NULL;

ALTER TABLE webhook_deliveries ADD COLUMN event_key TEXT;
ALTER TABLE webhook_deliveries ADD COLUMN payload TEXT NOT NULL DEFAULT '{}';
ALTER TABLE webhook_deliveries ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE webhook_deliveries ADD COLUMN claimed_at TEXT;
ALTER TABLE webhook_deliveries ADD COLUMN response_status INTEGER;
ALTER TABLE webhook_deliveries ADD COLUMN updated_at TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_delivery_event
    ON webhook_deliveries(subscription_id, event_key) WHERE event_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_webhook_delivery_recovery
    ON webhook_deliveries(studio_id, status, created_at);

CREATE TABLE IF NOT EXISTS delivery_notifications (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    gallery_id      INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','sent','failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    claimed_at      TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, gallery_id)
);
CREATE INDEX IF NOT EXISTS idx_delivery_notifications_recovery
    ON delivery_notifications(studio_id, status, created_at);

ALTER TABLE jobs ADD COLUMN studio_id TEXT REFERENCES studio(id) ON DELETE CASCADE;
ALTER TABLE jobs ADD COLUMN idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
    ON jobs(studio_id, kind, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_recovery
    ON jobs(studio_id, status, created_at);

ALTER TABLE listing_upsell_orders ADD COLUMN request_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_upsell_order_request
    ON listing_upsell_orders(studio_id, listing_id, request_key)
    WHERE request_key IS NOT NULL;

ALTER TABLE invoices ADD COLUMN currency TEXT NOT NULL DEFAULT 'usd';
ALTER TABLE invoices ADD COLUMN stripe_destination_account TEXT;
ALTER TABLE invoices ADD COLUMN stripe_checkout_claimed_at TEXT;

CREATE TABLE IF NOT EXISTS stripe_event_receipts (
    source          TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'processing'
                    CHECK (status IN ('processing','processed','rejected','failed')),
    attempts        INTEGER NOT NULL DEFAULT 1,
    studio_id       TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, event_id)
);
CREATE INDEX IF NOT EXISTS idx_stripe_event_recovery
    ON stripe_event_receipts(status, updated_at);

-- Existing delivered records predate an explicit delivery timestamp.
UPDATE listings SET delivered_at=updated_at
WHERE status='delivered' AND delivered_at IS NULL;
