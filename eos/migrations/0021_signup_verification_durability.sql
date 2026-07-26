-- Durable signup-verification delivery claims and fail-closed recovery.

CREATE TABLE signup_verification_intents (
    studio_id          TEXT PRIMARY KEY REFERENCES studio(id) ON DELETE CASCADE,
    token_fingerprint  TEXT NOT NULL,
    to_email           TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'claimed'
                       CHECK (status IN ('claimed','sent','failed','unknown','verified')),
    attempts           INTEGER NOT NULL DEFAULT 1,
    generation         INTEGER NOT NULL DEFAULT 1,
    claim_token        TEXT UNIQUE,
    claimed_at         TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at            TEXT,
    error              TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_signup_verification_recovery
    ON signup_verification_intents(status, claimed_at);

-- Legacy successful delivery has no provider receipt, while a recorded delivery
-- error is ambiguous. Preserve the known state and never replay an ambiguous send.
INSERT INTO signup_verification_intents
    (studio_id, token_fingerprint, to_email, status, attempts, claimed_at,
     sent_at, error, created_at, updated_at)
SELECT id, substr(signup_verify_token, -12), contact_email,
       CASE
         WHEN provisioning_error LIKE '%verification:%' THEN 'unknown'
         ELSE 'sent'
       END,
       1, signup_verify_issued_at,
       CASE
         WHEN provisioning_error LIKE '%verification:%' THEN NULL
         ELSE signup_verify_issued_at
       END,
       CASE
         WHEN provisioning_error LIKE '%verification:%'
         THEN 'Legacy verification delivery outcome requires provider review'
         ELSE NULL
       END,
       signup_verify_issued_at, signup_verify_issued_at
FROM studio
WHERE signup_verified=0
  AND signup_verify_token IS NOT NULL
  AND signup_verify_issued_at IS NOT NULL;
