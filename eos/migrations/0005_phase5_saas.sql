-- Phase 5: operator accounts, email drip sequences, production SaaS flags.

ALTER TABLE studio ADD COLUMN plan TEXT NOT NULL DEFAULT 'solo';
ALTER TABLE studio ADD COLUMN saas_enabled INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    email           TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'owner'
                    CHECK (role IN ('owner','operator')),
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, email)
);

CREATE TABLE IF NOT EXISTS email_sequences (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    slug            TEXT NOT NULL,
    name            TEXT NOT NULL,
    trigger_event   TEXT NOT NULL,
    delay_hours     INTEGER NOT NULL DEFAULT 0,
    subject         TEXT NOT NULL,
    body_template   TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    position        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, slug)
);

CREATE TABLE IF NOT EXISTS email_sequence_runs (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL DEFAULT 'default' REFERENCES studio(id) ON DELETE CASCADE,
    sequence_id     INTEGER NOT NULL REFERENCES email_sequences(id) ON DELETE CASCADE,
    listing_id      INTEGER REFERENCES listings(id) ON DELETE CASCADE,
    client_id       INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    to_email        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled','sent','canceled','failed')),
    scheduled_at    TEXT NOT NULL,
    sent_at         TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_seq_runs_due ON email_sequence_runs(status, scheduled_at);

-- Default RE drip sequences (solo studio; disable in admin if undesired).
INSERT OR IGNORE INTO email_sequences
    (studio_id, slug, name, trigger_event, delay_hours, subject, body_template, position)
VALUES
    ('default', 'booking-confirm', 'Booking confirmation', 'listing.booked', 0,
     'You''re booked — {listing_title}',
     'Hi {client_first},

Your listing shoot is confirmed for {listing_title}.

Pre-shoot intake (please complete before we arrive):
{intake_link}

See you soon!
{site_name}', 10),
    ('default', 'delivery-followup', 'Post-delivery follow-up', 'listing.delivered', 24,
     'Photos delivered — {listing_title}',
     'Hi {client_first},

Your gallery for {listing_title} is ready:
{gallery_link}
PIN: {gallery_pin}

Let me know if you need any MLS sizing tweaks.

Thank you!
{site_name}', 20),
    ('default', 'proposal-nudge', 'Proposal reminder', 'proposal.sent', 48,
     'Following up — proposal for {listing_title}',
     'Hi {client_first},

Just checking in on the proposal for {listing_title}:

{proposal_link}

Happy to adjust the package if needed.

{site_name}', 30);