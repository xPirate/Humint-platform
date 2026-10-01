-- ---------------------------------------------------------------------------
-- v1.6 — showing a device's code again, and one spelling
--
-- Run against an existing database ONCE, after v1.5:
--
--     docker compose exec -T db psql -U <POSTGRES_USER> -d <POSTGRES_DB> < db/migrate-v1.6-device-codes.sql
--
-- A fresh install does not need it. Safe to run twice.
-- ---------------------------------------------------------------------------

BEGIN;

-- Where this device's app should connect. Typed by the admin at enrollment
-- and kept, so that asking the console to show a device's code again does
-- not mean recalling a LAN address from memory. It is not a secret: the
-- console knows its own address, and so does anybody already on this page.
ALTER TABLE field_devices ADD COLUMN IF NOT EXISTS base_url TEXT;

-- The action name changed from 'enrol' to 'enroll' along with the rest of
-- the spelling. This relabels the rows that already exist.
--
-- Rewriting anything in an audit log deserves a justification, so: this
-- changes the *name* of an action, not the record of what happened, who did
-- it, when, or to what. The alternative is a log where the same event is
-- filed under two spellings forever, and a filter on either one silently
-- shows half the history — which is a worse outcome for the one table that
-- has to be trustworthy. If you would rather keep the original strings
-- exactly as written, delete this statement; nothing else depends on it.
UPDATE audit_log SET action = 'field.device.enroll'
 WHERE action = 'field.device.enrol';

COMMIT;
