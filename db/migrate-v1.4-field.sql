-- ---------------------------------------------------------------------------
-- v1.4 — field devices and the intake queue
--
-- Run this against an existing database ONCE, before starting the new API:
--
--     docker compose exec -T db psql -U <POSTGRES_USER> -d <POSTGRES_DB> < db/migrate-v1.4-field.sql
--
-- A fresh install does not need it — db/init.sql already contains everything
-- below. It is safe to run twice; every statement checks first.
-- ---------------------------------------------------------------------------

BEGIN;

-- A device is a credential, not a user. It belongs to a user account, and
-- deactivating that account is what stops it — one place to look when someone
-- leaves, rather than a separate list of phones to remember.
CREATE TABLE IF NOT EXISTS field_devices (
    id SERIAL PRIMARY KEY,
    label TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- The token itself is never stored. The prefix is kept only so the admin
    -- page can tell two devices apart in a list.
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'submit' CHECK (scope IN ('submit')),
    revoked_at TIMESTAMPTZ,
    revoked_by INTEGER REFERENCES users(id),
    last_seen_at TIMESTAMPTZ,
    last_seen_ip TEXT,
    submission_count INTEGER NOT NULL DEFAULT 0,
    enrolled_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_field_devices_user ON field_devices (user_id);
-- Every device request resolves a token; the live ones are the only rows it
-- ever has to look at.
CREATE INDEX IF NOT EXISTS idx_field_devices_live
    ON field_devices (revoked_at) WHERE revoked_at IS NULL;

-- What a device sends lands here and nowhere else until a person accepts it.
-- device_label is denormalised on purpose: a submission has to keep saying
-- which phone sent it after that phone has been removed from the list.
CREATE TABLE IF NOT EXISTS field_submissions (
    id SERIAL PRIMARY KEY,
    device_id INTEGER REFERENCES field_devices(id) ON DELETE SET NULL,
    device_label TEXT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    body TEXT,
    criticality TEXT CHECK (criticality IN ('Routine','Priority','Immediate','Flash')),
    observed_at TIMESTAMPTZ,
    lat DOUBLE PRECISION,
    lng DOUBLE PRECISION,
    location_accuracy_m DOUBLE PRECISION,
    location_note TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','accepted','rejected')),
    report_id TEXT REFERENCES reports(id) ON DELETE SET NULL,
    handled_by INTEGER REFERENCES users(id),
    handled_at TIMESTAMPTZ,
    handled_note TEXT,
    client_ref TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A phone on a bad connection retries. The app sends its own reference with
-- each report, and this index is what makes a retry land as the same report
-- instead of a second one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_field_submissions_client_ref
    ON field_submissions (device_id, client_ref) WHERE client_ref IS NOT NULL;

-- The queue's own query: one status, newest first.
CREATE INDEX IF NOT EXISTS idx_field_submissions_new
    ON field_submissions (status, received_at DESC);

CREATE TABLE IF NOT EXISTS field_submission_files (
    id SERIAL PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES field_submissions(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    mime_type TEXT,
    file_size_bytes BIGINT,
    client_ref TEXT,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_field_files_submission
    ON field_submission_files (submission_id);
-- The same retry guard as above, for a photo that is re-sent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_field_files_client_ref
    ON field_submission_files (submission_id, client_ref) WHERE client_ref IS NOT NULL;

-- The audit log has to be able to say "a device did this", and its actor_kind
-- is CHECK-constrained. audit.record deliberately never raises, so without
-- this the device's audit rows would simply not appear — a silent gap in the
-- one record that is supposed to be complete.
DO $$ BEGIN
    ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_actor_kind_check;
    ALTER TABLE audit_log ADD CONSTRAINT audit_log_actor_kind_check
        CHECK (actor_kind IN ('user','system','anonymous','device'));
END $$;

COMMIT;
