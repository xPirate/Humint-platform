-- ---------------------------------------------------------------------------
-- v1.5 — structured field reports
--
-- Run this against an existing database ONCE, before starting the new API,
-- and after db/migrate-v1.4-field.sql:
--
--     docker compose exec -T db psql -U <POSTGRES_USER> -d <POSTGRES_DB> < db/migrate-v1.5-templates.sql
--
-- A fresh install does not need it — db/init.sql already has all of it. Every
-- statement checks first, so it is safe to run twice.
--
-- What it is for: a field report used to be a title and a paragraph. It can
-- now also say which of the seven shapes in api/field_templates.json it was
-- written as, and carry that shape's fields as data — so a vehicle sighting
-- arrives with a plate in a field called `plate` rather than buried in prose,
-- and accepting it can offer a Vehicle record with the plate already in it.
-- ---------------------------------------------------------------------------

BEGIN;

-- Nullable on purpose. A report posted by hand with curl names no template,
-- and one from an app a version ahead of the console names a template this
-- file has never heard of. Neither is an error: the console renders what it
-- can and flags the rest. A field report is never refused over its shape.
ALTER TABLE field_submissions ADD COLUMN IF NOT EXISTS template TEXT;
ALTER TABLE field_submissions ADD COLUMN IF NOT EXISTS template_version INTEGER;

-- JSONB rather than a column per field, because the set of fields is data and
-- not schema: adding a question to the vehicle form should be an edit to a
-- JSON file, not a migration against a table holding live reports.
ALTER TABLE field_submissions
    ADD COLUMN IF NOT EXISTS fields JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Audio and video only, and only because the phone already knows it. Reading
-- a duration out of a container server-side would mean ffprobe in the api
-- image — a lot of megabytes to answer "how long is this clip" on a card.
ALTER TABLE field_submission_files ADD COLUMN IF NOT EXISTS duration_ms INTEGER;

COMMIT;
