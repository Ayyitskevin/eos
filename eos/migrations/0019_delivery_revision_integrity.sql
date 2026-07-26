-- Delivery revision identity, durable gallery grants, outbound claims, and billing ordering.

ALTER TABLE galleries ADD COLUMN delivered_at TEXT;
UPDATE galleries
SET delivered_at=COALESCE(
    (SELECT l.delivered_at FROM listings l
     WHERE l.id=galleries.listing_id AND l.studio_id=galleries.studio_id),
    created_at
)
WHERE listing_id IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM listings l
      WHERE l.id=galleries.listing_id AND l.studio_id=galleries.studio_id
        AND l.status='delivered'
  );

PRAGMA foreign_keys=OFF;
CREATE TABLE delivery_notifications_v2 (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    gallery_id      INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    event_key       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','sent','failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    claimed_at      TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, event_key)
);
INSERT INTO delivery_notifications_v2
    (id, studio_id, gallery_id, event_key, status, attempts, claimed_at, error,
     created_at, updated_at)
SELECT dn.id, dn.studio_id, dn.gallery_id,
       CASE
         WHEN g.listing_id IS NOT NULL
          AND dn.id = (
              SELECT MAX(dn2.id)
              FROM delivery_notifications dn2
              JOIN galleries g2
                ON g2.id=dn2.gallery_id AND g2.studio_id=dn2.studio_id
              LEFT JOIN listings l2
                ON l2.id=g2.listing_id AND l2.studio_id=g2.studio_id
              WHERE dn2.studio_id=dn.studio_id
                AND g2.listing_id=g.listing_id
                AND COALESCE(l2.revision_round, 0)=COALESCE(l.revision_round, 0)
          )
         THEN 'listing:' || g.listing_id || ':delivered:r' || COALESCE(l.revision_round, 0)
         WHEN g.listing_id IS NOT NULL
         THEN 'listing:' || g.listing_id || ':delivered:r' ||
              COALESCE(l.revision_round, 0) || ':legacy:' || dn.id
         ELSE 'gallery:' || dn.gallery_id || ':legacy:' || dn.id
       END,
       dn.status, dn.attempts, dn.claimed_at, dn.error, dn.created_at, dn.updated_at
FROM delivery_notifications dn
JOIN galleries g ON g.id=dn.gallery_id AND g.studio_id=dn.studio_id
LEFT JOIN listings l ON l.id=g.listing_id AND l.studio_id=g.studio_id
ORDER BY dn.id;
DROP TABLE delivery_notifications;
ALTER TABLE delivery_notifications_v2 RENAME TO delivery_notifications;
CREATE INDEX idx_delivery_notifications_recovery
    ON delivery_notifications(studio_id, status, created_at);
CREATE INDEX idx_delivery_notifications_gallery
    ON delivery_notifications(studio_id, gallery_id, created_at);

ALTER TABLE studio ADD COLUMN platform_subscription_event_created INTEGER NOT NULL DEFAULT 0;
ALTER TABLE studio ADD COLUMN platform_subscription_event_id TEXT NOT NULL DEFAULT '';

CREATE TABLE sms_reminder_intents (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    appointment_id  INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    reminder_date   TEXT NOT NULL,
    reminder_kind   TEXT NOT NULL DEFAULT 'shoot_day',
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','sent','failed','canceled')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    claimed_at      TEXT,
    error           TEXT,
    sent_at         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, appointment_id, reminder_date, reminder_kind)
);
CREATE INDEX idx_sms_reminder_recovery
    ON sms_reminder_intents(status, reminder_date, created_at);

CREATE TABLE rebooking_email_intents (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    event_key       TEXT NOT NULL,
    to_email        TEXT NOT NULL,
    subject         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'claimed'
                    CHECK (status IN ('claimed','sent','failed','unknown')),
    attempts        INTEGER NOT NULL DEFAULT 1,
    claim_token     TEXT NOT NULL,
    claimed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at         TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (studio_id, event_key)
);
CREATE UNIQUE INDEX idx_rebooking_email_active
    ON rebooking_email_intents(studio_id, client_id)
    WHERE status IN ('claimed','unknown');
CREATE INDEX idx_rebooking_email_recovery
    ON rebooking_email_intents(studio_id, status, claimed_at);
