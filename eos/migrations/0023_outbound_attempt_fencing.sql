-- Fence stale outbound workers from finalizing a newer retry attempt.

ALTER TABLE delivery_notifications ADD COLUMN attempt_token TEXT;
ALTER TABLE email_sequence_runs ADD COLUMN attempt_token TEXT;
ALTER TABLE sms_reminder_intents ADD COLUMN attempt_token TEXT;
ALTER TABLE webhook_deliveries ADD COLUMN attempt_token TEXT;
