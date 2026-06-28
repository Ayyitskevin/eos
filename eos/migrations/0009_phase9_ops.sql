-- Phase 9: ops intelligence, team scale, brokerage billing.

ALTER TABLE listings ADD COLUMN assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE appointments ADD COLUMN assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE invoices ADD COLUMN bill_to_client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL;
ALTER TABLE invoices ADD COLUMN agent_client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL;

ALTER TABLE studio_profiles ADD COLUMN twilight_start_min INTEGER NOT NULL DEFAULT 1020;
ALTER TABLE studio_profiles ADD COLUMN twilight_end_min INTEGER NOT NULL DEFAULT 1140;
ALTER TABLE studio_profiles ADD COLUMN delivery_upsell_title TEXT NOT NULL DEFAULT '';
ALTER TABLE studio_profiles ADD COLUMN delivery_upsell_body TEXT NOT NULL DEFAULT '';
ALTER TABLE studio_profiles ADD COLUMN delivery_upsell_link TEXT NOT NULL DEFAULT '/book';

UPDATE invoices SET bill_to_client_id=client_id, agent_client_id=client_id
WHERE bill_to_client_id IS NULL AND client_id IS NOT NULL;