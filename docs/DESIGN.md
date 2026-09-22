# HUMINT Platform — design notes

> This is the long-form document: what everything does and, more importantly,
> **why it works the way it does**. It is the reference, not the starting
> point. For installing and using the platform, start at the
> [README](../README.md) and the [SOPs](sop/).

A self-hosted intelligence platform for civilian/personal use: create
entities (people, organizations, locations, events, sources, communications,
vehicles), write reports and link them to the entities they discuss, attach
documents and images as evidence, and optionally let a local Ollama model do
the tedious first pass of pulling entities and relationships out of uploaded
files and flagging likely-duplicate records for you to review.

OpenCTI was the inspiration for it, not the template. This is a much smaller
thing, built around HUMINT case work rather than cyber threat intelligence —
no STIX bundles or observables, just people, places, organizations, and the
reports and relationships that tie them together.

The shape of the deployment is deliberately unremarkable: Docker Compose,
Postgres, an optional local Ollama container, and a single-page vanilla-JS
frontend with a phosphor-green terminal theme.

## What's actually here

- **Dashboard** — the landing page, as arrangeable panels: what needs
  attention, hotspots, reporting tempo, rising names, a clock (local time +
  UTC, since cross-referencing timestamps across sources usually means
  thinking in Zulu time), at-a-glance counts, and the most recent reports
  and entities. Every panel can be hidden or reordered, and
  every figure on it is computed from your own records with plain SQL — no
  model required. See "The dashboard" below.
- **Global search** — a search box in the top bar, available from every
  view, that searches entities and reports together and jumps straight to
  whichever result you pick. It's a quick jump-to, not a dedicated search
  page — for filtering a long list in place, the Entities and Reports views
  still have their own search boxes.
- **Map** — every Location entity that has coordinates plotted on an
  OpenStreetMap view. Click a marker to see what's tied to that location —
  Events (a relationship to a formal Event entity) and Reports (any report
  linked to this location, which in practice is how most "something
  happened here" records actually get made — an analyst writing up a report
  rarely also creates a separate Event entity for it) are both called out
  separately from everything else, specifically so multiple things that
  happened at the same place become obvious at a glance instead of
  something you have to notice by reading through separate reports one at a
  time. Click empty space on the map to drop a new Location entity exactly
  where you clicked.
- **Entities** — six fixed types (Person, Organization, Location, Event,
  Source, Communication), each with its own detail fields (a Person has
  aliases/DOB/occupation/Faction; a Source has an Admiralty reliability
  rating; etc.). A Person's Faction (Friendly/Neutral/Unknown/Hostile/Family)
  is a fixed dropdown, not free text — it's the analyst's own read on how
  this person relates to the case, so the extraction worker never proposes
  it the way it might propose an occupation or physical description from a
  document. Entities are archived rather than deleted in ordinary use, since reports and
  relationships may point at them. The Entities view is a **collapsible tree**
  beside a **relationship network** — see "The Entities page" below.
- **Bulk CSV import** — **Import CSV** on the Entities view. Pick a type,
  download that type's template (a header row matching its exact fields,
  plus a self-documenting example row), fill in one row per entity, upload
  it. Each row succeeds or fails independently — a typo in row 12 doesn't
  block rows 1–11 or 13 onward — and failed rows come back with the row
  number and reason so you can fix just those and re-upload them. See
  "Bulk CSV import" below for the file format and behavior in detail.
- **Address geocoding + Maidenhead grid** — a Location entity only needs an
  address; leave latitude/longitude blank and the worker looks up
  coordinates in the background (public OpenStreetMap Nominatim by
  default), so it shows up on the Map view without you ever having to find
  its coordinates yourself. Any Location with coordinates also gets a
  computed Maidenhead grid locator (e.g. `FM18lv`), the reference system
  amateur radio operators use for a station's location. See "Address
  geocoding + Maidenhead grid" below.
- **Relationships** — typed, directional edges between any two entities
  (`employed_by`, `associate_of`, `located_at`, `parent_of`, `spouse_of`, ...),
  worded correctly from whichever end you're reading — one stored edge shows
  as "child of" on one page and "parent of" on the other. Each carries a
  confidence level (confirmed / probable / possible), an optional discovery
  date (when you learned of it — not a validity date range, since HUMINT
  reporting usually can't actually support "this started on X and ended on
  Y" precision), and notes. Visualized as a graph on each entity's detail
  page.
- **Reports** — markdown case narratives, linked to whichever entities they
  discuss, with a status (draft/confirmed) and an Admiralty information
  credibility rating (1–6). Reports have no delete button on purpose — see
  "Deliberate limitations" below. **Export PDF** on any report produces a
  formatted intel package — the report plus a dossier for every entity it
  references and the photos attached to them — see "Exporting a report as a
  PDF" below.
  - Writing one is a full-page editor with a live list of everyone it names,
    and typing `@` anywhere in the prose finds an entity or creates one on
    the spot — so the cast list gets built while you write instead of before
    you start.
  - **Start a debrief** on the Reports view runs the same thing as a guided
    interview: source, timing, place, who was involved, the account, your
    assessment — creating entities at each step and filing a structured
    report at the end. See "Writing reports" below.
- **Documents** — the inbox, and usually where the job starts: drop in a PDF,
  a photographed page or a Word file (or paste raw text straight in) *before*
  you know what is in it, and the worker OCRs it, extracts the text and
  proposes the entities it found. Opening one shows the text beside those
  proposals so you can check a proposed name against the sentence it came from,
  and accept or dismiss it there. A document can stay in the inbox, be filed
  onto a report or entity later, or be archived. See "Documents" below.
- **Attachments** — documents and images, uploaded either to a report or
  directly to an entity (e.g. a mugshot on a Person, a scanned document on
  a Source). Images show as thumbnails and open in a preview window with a
  Download button, as do PDFs, so checking whether a file is the right one
  doesn't mean downloading it first. The worker OCRs/extracts text from
  anything it can (images, PDFs — including scanned ones, DOCX including
  its tables, TXT/RTF).
- **Review** — one tab holding both automated-suggestion queues as sub-tabs,
  with a combined badge counting everything waiting on you:
  - *Extraction* — proposals from three places, filterable by source, none
    of which writes to your case graph. If Ollama is enabled, the worker
    reads each attachment's extracted text and proposes entities and
    relationships it found. A **signal pass** (off by default; needs no model
    at all) looks over records you have already entered for pairs that look
    connected but have no edge — two people recorded as children of the same
    person, two people sharing a phone number, people named together in
    several reports. And you can **ask the assistant** to propose links in
    your own words: *"link the Smiths"*. Every proposal carries the reason it
    was made and sits here until an analyst accepts or dismisses it. See
    "Suggested links" below.
  - *Correlation* — if an embedding model is configured, the worker also
    flags pairs of entities (or reports) whose text is suspiciously similar
    — likely duplicates or related records — for an analyst to confirm or
    dismiss. Confirming a match does **not** merge the two records; see
    below. This also includes a cross-type pairing between Reports and
    ingested Events — see "RSS ingestion" below.
- **RSS ingestion** — an admin adds RSS/Atom feeds (no defaults ship — see
  "RSS ingestion" below); the worker polls each one in the background and
  turns new items directly into Event entities. Managed from the Admin page.
- **Accounts** — real login, not an "everything's public" model. This app
  stores real names, photos, and reports on real people, so it needed real
  access control. The first account created becomes an admin; every
  account after that is created by an admin from inside the app.
- **Admin settings page** — reached from the account menu in the top right,
  and only shown to admins. User management (change a user's role,
  activate/deactivate an account — the app always keeps at least one active
  admin), RSS feed management, Ollama Settings (pick a model from what's
  actually installed on the host, pull a new one with live progress, and
  change the host/timeout without a rebuild — see "Which model, and how it's
  configured" below), and Backup & Restore all live here. The audit log is
  reached from here too, but has its own page rather than being a section of
  this one.
- **Audit trail** — every change is recorded, along with the reads that move
  data out of the app (attachment downloads, PDF exports, backup downloads,
  AI Assistant queries): who did it, when, from what address, and to which
  record. Browsable and filterable on its own page (account menu → Audit
  log, admin-only), exportable as CSV, and
  optionally forwarded to a syslog collector on another machine — which is
  what makes it a record a local admin can't quietly rewrite. See "Audit
  trail" below.
- **Backup & restore** — Admin page → Backup & Restore. One button downloads
  a `.zip` holding every record in the app plus every uploaded attachment;
  uploading that zip on another instance reproduces it exactly, accounts and
  passwords included. Built for moving a deployment to a different machine
  and for getting back to a known state without re-typing a case by hand.
  See "Backup and restore" below — restore replaces everything and can't be
  undone.
- **AI Assistant** — if Ollama is enabled, an expandable side panel reachable
  from every view (not just the Dashboard) where you can ask plain-language
  questions about what's already in your case file — "what do we know about
  John Smith," "any reports mentioning the warehouse." It answers only from
  case data actually retrieved for that question, cites which entities/
  reports it used (click one to jump straight to that record), and says
  plainly when nothing relevant was found rather than guessing. See "AI
  Assistant" below.

## Getting started

1. Copy `.env.example` to `.env` and fill in a real Postgres password.
   Nothing else in `.env` needs to change to get the app running.
2. `docker compose up -d --build`
3. Open `http://localhost:8080` (or whatever `API_PORT` you set). The very
   first screen you'll see asks you to create the admin account — there
   are no seeded default credentials.
4. (Optional, for extraction/correlation) Start the bundled Ollama
   container and pull a model:
   ```
   docker compose --profile local-llm up -d
   ```
   Then log in, go to **Admin settings → Ollama Settings**, and use **Pull a
   model** to download `llama3.1:8b` — no terminal needed, and the model
   picker fills itself in once it lands. (The old way,
   `docker compose exec ollama ollama pull llama3.1:8b`, still works.) Or
   point `OLLAMA_BASE_URL` in `.env` at an Ollama instance you already run
   elsewhere and skip the bundled container entirely.
5. (Optional, for correlation specifically) Pull `nomic-embed-text` the same
   way and select it as the Embed model. Extraction works without this;
   correlation does not.
6. (Optional) Add RSS feeds from the Admin page (admin login required) to
   start ingesting Events in the background — see "RSS ingestion" below.
   Nothing is added for you; the feed list starts empty.

Everything above assumes Docker is already installed. If you're running
this on the same machine you're reading this on, `docker compose up -d
--build` from this project's folder is the whole install.

Note what still reaches outside the machine. Map **tiles** come from whatever
source is selected, drawn by the browser rather than proxied through this
app — but an admin can download areas as offline packs and then the tiles
come from here instead (see "Offline maps" below). Address **auto-geocoding**
needs the worker to reach a Nominatim instance, and is switched off with
`GEOCODE_ENABLED=false`. Everything else works fully offline/LAN-only.
Leaflet itself is served by this app, not a CDN, so the map always loads even
when there is nothing under it, and coordinates typed straight into a
Location record place without either service.

## Upgrading an existing deployment

New code alone won't touch your database — `db/init.sql` only runs once,
the first time the `db` container starts with an empty data volume.
Anything that changes the schema after you've already got real data in
there needs a one-time manual migration. As of this build, that's:

**Records** widen the entity type check and add one table. Without them,
saving a document as a Record fails, and so does every other part of the app
that reads `record_details`:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE entities DROP CONSTRAINT IF EXISTS entities_entity_type_check;
ALTER TABLE entities ADD CONSTRAINT entities_entity_type_check CHECK (entity_type IN
    ('person', 'organization', 'location', 'event', 'source', 'communication',
     'vehicle', 'record'));
CREATE TABLE IF NOT EXISTS record_details (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    record_kind TEXT,
    record_date DATE,
    issued_by TEXT,
    body TEXT,
    source_attachment_id INTEGER REFERENCES attachments(id) ON DELETE SET NULL
);
"
```

Both statements are safe to run twice.

**RSS lookback limits** add two nullable columns. Without them feeds keep
working exactly as before, with no limit:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE rss_feeds ADD COLUMN IF NOT EXISTS max_age_days       INTEGER;
ALTER TABLE rss_feeds ADD COLUMN IF NOT EXISTS max_items_per_poll INTEGER;
"
```

Existing feeds stay unlimited until you edit them — the defaults apply to
feeds added after the upgrade. Set them on anything already in the list.

**Map zones** add two tables and nothing else. Without them the Map view works
exactly as before; the zone controls on it will error.

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -f /dev/stdin < db/init.sql
```

Re-running the whole file is safe (every table is `CREATE TABLE IF NOT EXISTS`)
and is also how to pick up anything earlier you skipped.

**Alignment** renames one column and adds five. Run it before the new build
serves traffic — the API selects `alignment` and will error on every entity
read without it:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
DO \$\$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'person_details' AND column_name = 'faction')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'person_details' AND column_name = 'alignment')
    THEN
        ALTER TABLE person_details RENAME COLUMN faction TO alignment;
    END IF;
END \$\$;

ALTER TABLE person_details       ADD COLUMN IF NOT EXISTS alignment   TEXT;
ALTER TABLE organization_details ADD COLUMN IF NOT EXISTS alignment   TEXT;
ALTER TABLE source_details       ADD COLUMN IF NOT EXISTS alignment   TEXT;
ALTER TABLE vehicle_details      ADD COLUMN IF NOT EXISTS alignment   TEXT;
ALTER TABLE location_details     ADD COLUMN IF NOT EXISTS environment TEXT;
"
```

The rename is wrapped in a conditional so the whole block is safe to run
twice, and no value is touched: a person recorded as `Hostile` under the old
name is `Hostile` under the new one. Backups taken before this migration
restore without any manual step — see "Renamed columns" below.

**Offline maps** add two tables and seed one row. Without the migration the
Map view still works exactly as it did — it falls back to OpenStreetMap —
but the Maps section of the Admin page will error:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -f /dev/stdin < db/init.sql
```

`db/init.sql` is written to be safe to re-run: every table is
`CREATE TABLE IF NOT EXISTS` and the seeded OpenStreetMap source is an
`ON CONFLICT DO NOTHING` insert. Running the whole file is the least
error-prone way to pick up `map_sources` and `map_packs`, and it is also how
to catch up on any earlier migration you skipped.

**Dragging the network and the click-for-a-summary card need no migration at
all.** Both are frontend only; the card reads an endpoint the record page was
already calling.

**The right-click menu needs no migration at all.** It reads columns that
already exist and writes only rows the app already wrote.

**The tree, the network and relationship editing need no migration at all.**
They read columns that already exist. `docker compose up -d --build` is the
whole upgrade for those three. Everything below still applies if you are
coming from an older build than the one that introduced it.

**Recording records by hand from a document** needs a one-line migration too,
widening the values `extraction_suggestions.source` accepts. Without it,
recording something from a document fails with a constraint violation:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE extraction_suggestions DROP CONSTRAINT IF EXISTS extraction_suggestions_source_check;
ALTER TABLE extraction_suggestions ADD CONSTRAINT extraction_suggestions_source_check
    CHECK (source IN ('extraction', 'signal', 'assistant', 'manual'));
"
```

Admin deletion needs no migration at all — it only removes rows.

Vehicles, added before those, **do** need one — a new record type means a new
detail table and a wider CHECK constraint on `entities.entity_type`. Without
it, Vehicle appears in every dropdown and saving one fails. Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE entities DROP CONSTRAINT IF EXISTS entities_entity_type_check;
ALTER TABLE entities ADD CONSTRAINT entities_entity_type_check
    CHECK (entity_type IN ('person', 'organization', 'location', 'event',
                           'source', 'communication', 'vehicle'));

CREATE TABLE IF NOT EXISTS vehicle_details (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    make TEXT,
    model TEXT,
    color TEXT,
    license_plate TEXT,
    plate_region TEXT,
    style TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_vehicle_plate
    ON vehicle_details (upper(regexp_replace(license_plate, '[^A-Za-z0-9]', '', 'g')))
    WHERE license_plate IS NOT NULL;
"
```

Dropping and re-adding the CHECK widens what the column accepts and rejects
nothing that is already in there, so no existing row can fail it. Safe to run
twice.

Relationships changed from a start/end date range to a single discovery
date (see "Relationships" above for why). If you created any relationships
before this change, run this once against your running database to carry
existing values forward instead of losing them — a relationship's old
`start_date` becomes its `discovery_date` (an imperfect but reasonable
stand-in; `end_date` is simply dropped, since discovery date has no
"end"):

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE relationships ADD COLUMN IF NOT EXISTS discovery_date DATE;
UPDATE relationships SET discovery_date = start_date WHERE discovery_date IS NULL AND start_date IS NOT NULL;
ALTER TABLE relationships DROP COLUMN IF EXISTS start_date;
ALTER TABLE relationships DROP COLUMN IF EXISTS end_date;
"
```

Then rebuild as usual (`docker compose up -d --build`). Skip this entirely
on a brand new install — `init.sql` already creates the table with
`discovery_date` from the start.

A Person's free-text Nationality field became a fixed Faction dropdown
(Friendly/Neutral/Unknown/Hostile/Family — see "Entities" above). This is a
plain column rename, and it's non-destructive: any existing free-text value
(e.g. "American") stays in the column untouched — it just won't match any
of the five new dropdown options, so that field will show as blank next
time you open that person for editing until you pick one of the five
values. Nothing else about that entity is affected, and you can leave it
blank indefinitely if you don't need to classify it. Run once, in the same
session as the migration above if you're catching up on both at once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE person_details RENAME COLUMN nationality TO faction;
"
```

Then rebuild (`docker compose up -d --build`) — one rebuild covers both
migrations if you're running them together. Skip this on a brand new
install — `init.sql` already creates the column as `faction`.

RSS ingestion added two new tables (`rss_feeds`, `rss_items`) and a new
cross-type value (`report_event`) to `correlation_suggestions`' existing
`subject_type` check constraint, for the Report↔Event correlation described
under "RSS ingestion" below. Run once against your running database:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE correlation_suggestions DROP CONSTRAINT IF EXISTS correlation_suggestions_subject_type_check;
ALTER TABLE correlation_suggestions ADD CONSTRAINT correlation_suggestions_subject_type_check
    CHECK (subject_type IN ('entity', 'report', 'report_event'));

CREATE TABLE IF NOT EXISTS rss_feeds (
    id SERIAL PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_polled_at TIMESTAMPTZ,
    last_error TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rss_items (
    id SERIAL PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES rss_feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    entity_id TEXT REFERENCES entities(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (feed_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_rss_items_feed ON rss_items (feed_id);
"
```

This is purely additive — existing rows in `correlation_suggestions` are
untouched, and the two new tables start empty (nothing is ingested until
you add a feed). Then rebuild (`docker compose up -d --build`) — again, one
rebuild covers every pending migration if you run them all together. Skip
this on a brand new install — `init.sql` already includes all of it.

Communication's free-text `medium` field became a fixed dropdown, and a
new `medium_detail` column was added alongside it (see "Communication's
medium + detail field" below). Same non-destructive story as the Faction
migration above: this just adds a column, so there's nothing to lose, and
any existing free-text `medium` value (e.g. "phone call") stays in the
column untouched — it just won't match any of the fixed options, so it'll
show blank in the edit form until you pick one of the new values. Run
once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE communication_details ADD COLUMN IF NOT EXISTS medium_detail TEXT;
"
```

Then rebuild (`docker compose up -d --build`). Skip this on a brand new
install — `init.sql` already creates the column.

Location entities gained automatic address→coordinates geocoding and a
computed Maidenhead grid locator (see "Address geocoding + Maidenhead grid"
below) — four new columns on `location_details`. Non-destructive: existing
rows keep whatever `lat`/`lng` they already had, and nothing is
auto-geocoded retroactively (a legacy location with coordinates just shows
no grid square until you next open and save it — see that section for why
this is a deliberate non-issue rather than something to fix). Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE location_details ADD COLUMN IF NOT EXISTS maidenhead_grid TEXT;
ALTER TABLE location_details ADD COLUMN IF NOT EXISTS geocode_status TEXT CHECK (geocode_status IN ('pending', 'processing', 'done', 'failed'));
ALTER TABLE location_details ADD COLUMN IF NOT EXISTS geocode_error TEXT;
ALTER TABLE location_details ADD COLUMN IF NOT EXISTS geocoded_at TIMESTAMPTZ;
"
```

Then rebuild (`docker compose up -d --build`). Before your first real use of
this, set `GEOCODE_USER_AGENT` in `.env` to something that actually
identifies you/your install (see that section for why) and re-run
`docker compose up -d --build` to pick it up. Skip all of this on a brand
new install — `init.sql` already creates the columns.

Admin-editable Ollama settings (see "Ollama Settings" under the Admin page,
below) added one new table, `app_settings`. Purely additive — nothing
existing changes, and the table starts with no row at all, which is
identical in effect to every setting being unoverridden. Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
CREATE TABLE IF NOT EXISTS app_settings (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    ollama_base_url TEXT,
    ollama_model TEXT,
    ollama_embed_model TEXT,
    ollama_enabled BOOLEAN,
    ollama_timeout_seconds INTEGER,
    updated_at TIMESTAMPTZ,
    updated_by INTEGER REFERENCES users(id)
);
"
```

Then rebuild (`docker compose up -d --build`). Skip this on a brand new
install — `init.sql` already creates the table.

The AI Assistant (see "AI Assistant" below) added one new table,
`assistant_messages` — each user's own private conversation log with the
assistant. Purely additive and empty until someone actually uses the panel.
Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
CREATE TABLE IF NOT EXISTS assistant_messages (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    citations JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_assistant_messages_user ON assistant_messages (user_id, created_at);
"
```

Then rebuild (`docker compose up -d --build`). Skip this on a brand new
install — `init.sql` already creates the table. Until this migration is run,
opening the AI Assistant panel will error — every other part of the app is
completely unaffected either way.

The audit trail (see "Audit trail" below) added one new table, `audit_log`.
Purely additive, and it starts empty — the trail begins the moment you run
this, with no history for anything that happened before it. Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor_username TEXT,
    actor_kind TEXT NOT NULL DEFAULT 'user' CHECK (actor_kind IN ('user', 'system', 'anonymous')),
    action TEXT NOT NULL,
    object_type TEXT,
    object_id TEXT,
    object_label TEXT,
    outcome TEXT NOT NULL DEFAULT 'success' CHECK (outcome IN ('success', 'failure', 'denied')),
    ip_address TEXT,
    user_agent TEXT,
    detail JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_log_occurred ON audit_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log (actor_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log (action, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_object ON audit_log (object_type, object_id, occurred_at DESC);
"
```

Then rebuild (`docker compose up -d --build`). Skip this on a brand new
install — `init.sql` already creates it. Unlike the other migrations, running
the new code *without* this one is safe: auditing fails quietly (with the
reason in the api container's logs) rather than breaking anything, precisely
so a forgotten migration can't take the app down. You just get no audit trail
until you run it.

Person status fields and contact points (see "A Person's two status fields"
and "Contact details" below) added two columns to `person_details` and one new
table, `contact_points`. Purely additive: every existing Person keeps its
fields and gets a blank Status and Disposition, which is the correct starting
state — blank means nobody has assessed it. Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE person_details ADD COLUMN IF NOT EXISTS life_status TEXT;
ALTER TABLE person_details ADD COLUMN IF NOT EXISTS disposition TEXT;

CREATE TABLE IF NOT EXISTS contact_points (
    id SERIAL PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    label TEXT,
    value TEXT NOT NULL,
    notes TEXT,
    is_preferred BOOLEAN NOT NULL DEFAULT FALSE,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contact_points_entity ON contact_points (entity_id);
"
```

Then rebuild (`docker compose up -d --build`). Skip this on a brand new
install — `init.sql` already creates both. The new kinship relationship types
(`spouse_of`, `parent_of`, `child_of`, `significant_of`) need no migration at
all: relationship types have always been free text validated in the API layer
rather than a database enum, which is exactly the case this design was for.

Event expiry and report criticality (see "Event expiry" and "Report
criticality" below) added one column each. Both are additive and both start
NULL, which is the correct state: no Event has an expiry until someone sets
one, and NULL criticality means unrated, not Routine. Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE event_details ADD COLUMN IF NOT EXISTS expires_at DATE;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS criticality TEXT;
"
```

Then rebuild. Light/Dark needs no migration — it is entirely frontend and
stored in the browser. (The four *themes* added later do need one; see
below.)

Per-user dashboard layouts added one column to `users`. Additive, defaults to
an empty document, and nothing breaks without it beyond layouts failing to
save. Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE users ADD COLUMN IF NOT EXISTS preferences JSONB NOT NULL DEFAULT '{}'::jsonb;
"
```

Themes and branding added three columns to `app_settings`. All additive, all
nullable, and all meaning "use the .env default" when NULL — an instance that
skips this migration keeps the original look and name, and the only thing that
breaks is an admin trying to save branding. Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS instance_name TEXT;
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS brand_accent TEXT;
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS default_palette TEXT;
"
```

The per-user theme choice needs no migration of its own — it goes in the
`preferences` document added above, beside the dashboard layout.

Model activity monitoring added one table. Nothing reads it until Ollama is
actually used, and nothing breaks without it beyond the new Admin panel being
empty. Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
CREATE TABLE IF NOT EXISTS ollama_calls (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source TEXT NOT NULL CHECK (source IN ('api', 'worker')),
    operation TEXT NOT NULL CHECK (operation IN ('extract', 'embed', 'chat')),
    model TEXT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    prompt_tokens INTEGER,
    eval_tokens INTEGER,
    total_duration_ms INTEGER,
    load_duration_ms INTEGER,
    eval_duration_ms INTEGER,
    outcome TEXT NOT NULL DEFAULT 'success'
        CHECK (outcome IN ('success', 'failure', 'timeout', 'disabled')),
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ollama_calls_occurred ON ollama_calls (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_ollama_calls_outcome ON ollama_calls (outcome, occurred_at DESC);
"
```

A separate extraction model, and the link-signal and assistant proposals,
added one column to `app_settings` and five to `extraction_suggestions`, plus
two indexes. All additive. The one change that is not a pure addition is
dropping `NOT NULL` from `extraction_suggestions.attachment_id` — proposals
that did not come from a document have no attachment — and that only widens
what the column accepts, so existing rows are untouched. Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS ollama_extract_model TEXT;

ALTER TABLE extraction_suggestions ALTER COLUMN attachment_id DROP NOT NULL;
ALTER TABLE extraction_suggestions ADD COLUMN IF NOT EXISTS source TEXT NOT NULL
    DEFAULT 'extraction' CHECK (source IN ('extraction', 'signal', 'assistant'));
ALTER TABLE extraction_suggestions ADD COLUMN IF NOT EXISTS suggested_from_entity_id TEXT
    REFERENCES entities(id) ON DELETE CASCADE;
ALTER TABLE extraction_suggestions ADD COLUMN IF NOT EXISTS suggested_to_entity_id TEXT
    REFERENCES entities(id) ON DELETE CASCADE;
ALTER TABLE extraction_suggestions ADD COLUMN IF NOT EXISTS evidence JSONB;

CREATE UNIQUE INDEX IF NOT EXISTS idx_suggestions_pending_pair
    ON extraction_suggestions (source, suggestion_type, suggested_relationship_type,
                               suggested_from_entity_id, suggested_to_entity_id)
    WHERE status = 'pending' AND suggested_from_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_suggestions_source_status
    ON extraction_suggestions (source, status, created_at DESC);
"
```

Skipping this one leaves the app working exactly as it did: the queue shows
document-extracted suggestions only, the signal pass logs a failure and gives
up (it cannot write a row it has no columns for), and Admin → Ollama Settings
loses the extraction-model field. Nothing you already have is at risk.

### If an upgrade seems to have half-arrived

Symptom: after unpacking a new build, a new button is there but does nothing,
or a new panel never appears. That is a stale script in the browser, not a
broken build. `frontend/` is bind-mounted into the api container, so unpacking
a build changes the page immediately — but a browser that has been using the
app for a while may keep serving itself the old `app.js` from its own cache
without asking the server, and the page you get is new markup driven by old
code. A hard reload (Ctrl-Shift-R, or Cmd-Shift-R on a Mac) clears it.

Builds from September 2026 onward stop this happening: the API stamps the
asset URLs in the page with a hash of each file's contents, so an upgraded
script has a URL no cache has ever seen, and the assets themselves are served
with `Cache-Control: no-cache` — "always ask", not "never cache", so unchanged
files still answer with a cheap 304. Nothing to configure. Note that this part
lives in `api/`, which is baked into the image rather than mounted, so it only
takes effect once you have run `docker compose up -d --build`.

Merging duplicates added one column to `entities` and an index. Additive and
nullable; without it the Merge buttons appear and every merge fails. Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE entities ADD COLUMN IF NOT EXISTS merged_into TEXT
    REFERENCES entities(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_entities_merged_into
    ON entities (merged_into) WHERE merged_into IS NOT NULL;
"
```

The Documents inbox added four columns to `attachments` and, more importantly,
**dropped the constraint requiring every attachment to belong to a report or an
entity** — that constraint is the only reason there was nowhere to put a
document you had not filed yet. Dropping it only widens what the table accepts;
every existing row still has its parent. Run once:

```
docker compose exec db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "
ALTER TABLE attachments DROP CONSTRAINT IF EXISTS attachments_check;
ALTER TABLE attachments ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE attachments ADD COLUMN IF NOT EXISTS source_note TEXT;
ALTER TABLE attachments ADD COLUMN IF NOT EXISTS is_pasted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE attachments ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_attachments_unfiled
    ON attachments (uploaded_at DESC)
    WHERE report_id IS NULL AND entity_id IS NULL AND archived_at IS NULL;
"
```

(`attachments_check` is the name Postgres gives that unnamed table constraint.
If your deployment named it something else, `\d attachments` shows it.) Without
this migration the Documents tab loads and then fails on every upload, since
the database still refuses a parentless attachment. Backups need no attention
either way — the backup reads its column list out of the live schema, so a
backup taken after the migration carries the new fields automatically.

Then rebuild. Anyone who had already arranged their dashboard keeps that
arrangement: the first time they log in after the upgrade, the layout sitting
in their browser is adopted onto their account rather than thrown away. The
link-signal pass stays off until you set `LINK_SIGNALS_ENABLED=true` — see
"Suggested links" below before you do, because switching it on for an
established case file produces a burst of proposals all at once.

## Themes and branding

### Four themes

The app ships with four looks. Each has a light and a dark mode, so the choice
of theme and the choice of light-or-dark are two separate controls in the
account menu rather than one list of eight.

| Theme | What it is |
|---|---|
| **Terminal** | The phosphor-green CRT this tool started as. Monospace throughout, which is most of why it reads the way it does. Still the default. |
| **Slate** | Neutral blue-grey with a proper UI sans. The one nobody has to defend in a procurement meeting. |
| **Graphite** | Warm charcoal, bronze accent, tighter corners. An operations room rather than a SaaS product. Nothing glows. |
| **Archive** | Warm paper, serif headings, oxblood accent. For a team whose day is reading and writing reports rather than watching a dashboard. |

On every theme except Terminal the interface is set in a sans face, and
monospace is kept for the things that are genuinely *data*: record IDs,
coordinates, Maidenhead squares, frequencies and channel numbers, phone
numbers, and code. The test is whether anyone ever compares two of them
character by character or reads one aloud. A name or a description fails that
test and reads better in the UI face; a grid square passes it.

**Every font is a system font.** No webfonts, no CDN. This app is expected to
run on a Pi on a network that may have no route to the internet at all, and a
theme that silently falls back to Times because a font host is unreachable is
worse than one that never tried.

**Every theme was contrast-checked in both modes**, not just the two anyone
remembers to look at. The theme test suite walks all eight combinations
reading the *live computed* values out of the browser and fails on anything
under WCAG AA. That pass caught two genuine problems in the original green
theme that had been there all along: hint text at 2.94:1, and input borders at
1.43:1 against the panel behind them. Both are fixed, which is why there is now
a `--border-strong` token used for the edge of a control as distinct from the
hairline between two cards.

### Which choice lives where

- **Theme** is saved to your account, like the dashboard layout. It follows
  you to whichever machine you log in from — the Chromebooks-kept-at-each-site
  case.
- **Light / Dark** stays per-browser. It is a fact about the screen and the
  room, not about the person.

An admin sets the instance default in Admin settings → Branding. That is what
anyone sees until they choose for themselves, and changing it still moves
everybody who has never expressed a preference — a user's choice is only
written to their account when they actually make one.

It is a **default, not a lock**. An organisation gets to say what its instance
looks like; it does not get to decide that the analyst standing in direct
sunlight has to keep squinting at the corporate palette.

### Naming the instance

Admin settings → Branding sets:

- **Instance name** — replaces "HUMINT Platform" in the top bar, the browser
  tab, the login screen, and the running header of every exported PDF. A team
  standing this up for themselves will call it something of their own, and a
  deployment that cannot be named reads as somebody else's software they are
  borrowing.
- **Brand colour** — an optional `#rrggbb` that overrides the accent of
  whichever theme is in use.
- **Default theme** — as above.

All three start from `.env` (`INSTANCE_NAME`, `BRAND_ACCENT`,
`DEFAULT_PALETTE`) and are editable in the app afterwards without a rebuild,
the same arrangement the Ollama settings use. `PDF_HEADER_LABEL` still wins
outright if you set it — an organisation that has typed a specific running
header wants that header, not one assembled from something else.

You supply **one** brand colour, because that is what a brand guide contains.
The fill behind a primary button, its hover state, and — importantly — the
label colour on that fill are all derived from it. The label is chosen by
measuring rather than assuming: a brand colour can be anything from navy to
lime, and guessing white gets one of those badly wrong. If the colour is too
light to carry white text the fill is darkened until the pair clears AA.

The link colour is deliberately **not** adjusted. That is the organisation's
actual colour, and quietly darkening it would be presumptuous. Instead the
branding form measures it against both the panel and the page background and
says plainly when it will be hard to read — with the number, not just
"low contrast" — while noting that buttons are still fine, so the warning is
not read as worse than it is.

Renaming an instance is audited; one analyst preferring Graphite is not. The
difference is that a rename changes what every exported document says it came
from, which is a claim about provenance, and a theme is not.

The read side of branding (`GET /api/branding`) is the one deliberately
unauthenticated endpoint in the app, because the login screen has to show the
instance name before anyone has logged in. It returns exactly those three
fields and nothing else — an instance name is already printed on every PDF
that leaves the building and shown to everyone who can reach the login page.

## Adding people to the platform

Only admins can create new accounts, and only from inside the app (there is
no open signup) — log in as the admin, click **+ User** in the top bar, and
give them a username, password, and role (analyst or admin).

## The dashboard

Panels, not a fixed layout. Each one can be hidden or dragged into a different
order, and the arrangement is **stored on your account**, not in the browser.

That distinction matters for how these deployments actually work. A team with
a server at one site and Chromebooks kept at each of the others has people
moving between machines constantly: a layout held in browser storage would
never travel with the analyst, and — worse on a shared device — the next
person to log in would inherit it, including which panels a colleague had
chosen to hide. On the account, it follows the person and stays out of
everyone else's way.

A copy is still kept in the browser, but only as a cache: it lets the
dashboard paint immediately instead of waiting on a round trip, it keeps the
panels arrangeable if the save fails (with the toolbar saying so rather than
pretending it saved), and it is cleared on logout. The cache is keyed by
username, so even before logout a shared machine never shows one analyst
another's arrangement.

The **Appearance** switch (Light/Dark) deliberately stays per-browser. It is a
property of the screen you are sitting at — a bright tablet outdoors and a
monitor in a dim room want different answers from the same person — where the
dashboard layout is a property of how you work.

The **Theme** picker above it goes the other way, and is saved to the account
for the same reason the layout is. See "Themes and branding" below.

A window control at the top (7 / 14 / 30 / 90 days) drives everything that
says "recently". A fortnight is right for a fast-moving situation and far too
short for a case that unfolds over months, so it is a choice rather than a
constant.

**Everything here is plain SQL over the case file.** No Ollama, no embedding
model, nothing that degrades if no model is installed — that is deliberate.
The extraction and correlation queues are the only model-dependent parts of
this app, and a team without the budget or the hardware to run one should
still get pattern-spotting out of their own records. The panel header says so
on the page, so nobody has to wonder whether these numbers came out of an LLM.

That constraint also makes every panel **explainable**. A hotspot lists the
exact records that made it hot; a rising name shows both counts it was derived
from. You can disagree with any figure here by clicking through to the rows
behind it, which is not true of a similarity score.

### Needs attention

Four lists, kept separate rather than merged into one ranked feed, because
they call for different actions:

- **Urgent** — everything filed Flash or Immediate. Drafts included: a Flash
  report still sitting unfinished is *more* interesting, not less.
- **Forgotten drafts** — drafts nobody has touched in 14 days.
- **Ageing out** — Events lapsing within 7 days, plus anything that lapsed in
  the last week. Showing only the future half would hide an Event on exactly
  the day it went stale.
- **People** — anyone recorded as Captured, Detained, Missing or Evading.

### Hotspots

Locations that several records point at inside the window. The case this was
built for: a filling station that turns up in four separate convoy write-ups
over ten days. Nothing in any one of those reports says "this place matters" —
the pattern only exists across them, and it is exactly the sort of thing
noticed weeks late or not at all.

A Location's count is Events related to it, plus Reports linked to it, plus
**Reports linked to those Events** — that last route is the one the convoy
case actually travels, since a sighting is usually written up as a report
about the *event* rather than about the place. Three records is the threshold:
two is a coincidence, three is worth a look.

Each row shows the split (events vs reports), how many separate days saw
activity — six records from one incident is a different thing from four
records on four days — and lists the contributing records, each a link.

### Reporting tempo

Reports filed per week for the last twelve weeks. Weeks rather than days,
because at this scale a daily count is mostly zeroes with noise on top and the
thing worth seeing is a fortnight that produced three times the usual traffic.

The current week is compared against the **median** of the preceding weeks,
not the mean: one exceptional week would drag a mean upward and hide the next
one.

### Rising names

Entities named in more reports this window than in the window before it, with
both counts shown. "Rising" rather than "most mentioned" on purpose — a name
that is always busy is not news, and a name that went from nothing to four
reports in a fortnight is. Two mentions is the floor, so a single passing
reference cannot put someone on a panel headed "rising".

## The Entities page: a tree and a network

Two panes over one selection. The search box, the type dropdown and the two
tick boxes at the top drive both.

### The tree

One collapsible branch per type, records listed alphabetically inside, one
line each. This replaced a grid of cards, for a reason worth stating: every
card cost the same vertical space whether it was the organization at the
centre of the case or a phone number somebody typed in once and never used.
A page of equal boxes has no shape to read, and at three hundred records it
is a wall. A row per record fits roughly ten times as many on screen and
leaves the rest of the page for the picture.

Each row carries the record's name, its status badges if it has any, and the
number of relationships attached to it — the same number the network grades
its dots by, so the two panes never disagree. A branch you fold stays folded
next time you open the page; that is kept in the browser, not the account,
because which types you care about depends on what you are working on rather
than who you are. Rows answer to Enter and the spacebar as well as the mouse.

**Select to merge** still works here: it puts a tick box on every row, and
everything in "Merging duplicates" below applies unchanged.

### The network

Every record matching the current filters, drawn as a dot; every
relationship between two of them, as a line. **Dot size and colour both
grade by how many relationships the record has** — pale and small at the
bottom, the accent colour and large at the top. That grading is the point of
the pane. A list tells you what is in the file; this tells you what the file
is *about*, and the organization forty things point at is obvious the moment
the layout settles, without anybody having counted anything.

- **Hover** a dot to name it and see its count; its neighbours stay lit and
  everything else dims, which is how you read one record's immediate
  surroundings out of a busy picture. Hovering a row in the tree does the
  same thing to its dot.
- **Click** a dot to open that record.
- **Hide unconnected** is on by default. A record with no relationships has
  nothing to sit next to, and a rim of orphans around the edge makes the
  part that matters smaller. Untick it to see them.
- **Refit** re-runs the layout — worth a click after you have changed the
  filters a few times.

The count on a dot is the record's *real* relationship count, across the
whole database, not a count within whatever you have filtered to. Filter down
to Organizations and a company still shows the twelve people attached to it,
even though none of them is drawn. The alternative would make the same record
change colour depending on what you happened to be looking at.

Above 400 records the network draws the 400 most-connected and says so in the
summary line ("400 most-connected of 912"). The cap is by relationship count
on purpose: if something has to be left out it should be the isolated
records, not the hubs.

### No CDN

Both this network and the smaller one on each record's own page are drawn by
the app itself — a force-directed layout on a canvas, no library. The detail
page used to load vis-network from a CDN and show "graph library unavailable"
when it could not reach it. A machine in a cupboard with no route to the
internet is a normal way to run this app, and a headline feature that only
works online is not a feature. (Leaflet is still loaded from a CDN for the
Map tab, which needs online tiles to be useful anyway, as are `marked` and
`DOMPurify` for report markdown.)

## Relationships are editable

Everything extraction and the signal pass produce arrives as **possible**.
That is the honest default for something a machine proposed — and the whole
point of reviewing a case file is that some of those become probable and a
few become confirmed as you corroborate them.

Every relationship on a record's page has an **Edit** control that opens in
place: type, confidence, discovery date and notes, saved to the existing
relationship. Previously the only way to change a confidence was to delete
the relationship and type it again, which threw away its discovery date and
notes along with it, and left an entry in the audit trail saying a link had
been removed when nothing of the sort had happened.

Notes are shown under the relationship on the record's page rather than
hidden behind the editor — a note explaining *why* a link is only possible
is the thing you need to see when deciding whether it still is.

The type field accepts what you type and tidies it: "Works For" is stored as
`works_for`. A blank discovery date clears the one that was there.

## The entity model

Seven fixed types, rather than the fully custom object schema a platform
like OpenCTI offers. That's a deliberate trade: a fixed set of types with
real per-type fields (a Person's date of birth, a Source's reliability
rating) is far more useful day-to-day than a generic "anything goes"
object with a free-form key/value bag, and it keeps the extraction
pipeline's job well-defined — the model always knows exactly which kinds
of thing it's allowed to propose.

| Type | Distinguishing fields |
|---|---|
| Person | aliases, date of birth, **Alignment** (Friendly/Neutral/Unknown/Hostile), occupation, physical description, **Status** (Living/Deceased/Unknown), **Disposition** (At liberty/Captured/Detained/Evading/Missing) |
| Organization | org type, founded date, website, **Alignment** |
| Location | address, **Environment** (Permissive/Semi-permissive/Non-permissive/Denied/Unknown), latitude, longitude (auto-geocoded from address if left blank — see below), Maidenhead grid locator (computed) |
| Event | event type (also doubles as provenance, e.g. `RSS: Tulsa World`, for ingested Events — see "RSS ingestion"), start/end time, **relevant until** (see below) |
| Source | source type, **reliability rating (A–F)**, handling notes, **Alignment** |
| Communication | medium (fixed dropdown — see below) + a medium-specific detail field, occurred-at time, participants note |
| Vehicle | make, model, colour, licence plate, plate region, **Alignment**, **style** (fixed dropdown), notes |

### The correlation queue is bounded now, and it was not

Every newly embedded record is compared against every other record of its
kind, and every pair above `CORRELATION_SIMILARITY_THRESHOLD` became a
suggestion. That is quadratic in disguise. It is invisible at twenty records
and crippling at two hundred: a first poll of a busy news feed produced 200
Events, news items about the same city read alike by their nature, and roughly
a tenth of the ~19,900 possible pairs cleared the bar. Two thousand
suggestions, and a queue nobody could work through.

Three changes, and it is worth being clear about what each one does, because
only together do they fix it:

**`CORRELATION_MAX_MATCHES_PER_RECORD` (default 10)** makes the queue grow
linearly rather than quadratically. Each record proposes only its strongest
matches. This costs nothing real — candidates come back sorted by similarity,
and the 140th best match for a news item was never going to be accepted.
*On its own it would not have saved that feed*: 200 records at 10 each is
still up to 2,000 pairs. What it buys is that a thousand records is ten
thousand suggestions rather than half a million.

**RSS lookback limits** are what actually stop the flood, because they stop
200 items landing at once. A feed's first poll is the dangerous one — it sees
the publisher's whole current window. `max_age_days` and `max_items_per_poll`
default to 7 and 50 for new feeds. Both exist because neither is sufficient:
age is what people mean, but a feed with no timestamps would be filtered to
nothing by age alone, and a back-dated archive dump defeats it entirely.

**Bulk review** clears what is already there. Ticked rows, "select all shown",
or the whole filtered set with its count stated twice before anything happens.
A similarity band on the queue makes "the weak tail" a selectable thing, which
is where a bulk dismiss is safe and where it is least safe at the top.

Two details in the bulk endpoint worth keeping:

- **`expected_count` is not belt-and-braces.** The queue is written to by a
  background worker, so between the dialog saying "2,014" and somebody
  pressing the button, the worker may have added more. Acting on a larger set
  than the one that was agreed to is exactly the surprise a bulk action must
  not spring, so a mismatch is a 409 telling them to refresh.
- **Already-decided suggestions are skipped and counted, not re-decided.** A
  bulk action that quietly overwrote a colleague's review would be worse than
  no bulk action.

One audit entry per batch, not per row: two thousand identical entries would
bury the log this app expects people to actually read.

### Deleting a feed can take its records with it

Deleting a feed used to stop polling and leave every Event it had created,
which is right for a feed that ran for a month and wrong for one added by
mistake an hour ago. The dialog now counts what the feed created first —
including how many are cited by confirmed reports — and offers to leave,
archive or delete them.

**Records cited by a confirmed report are archived rather than deleted,
whichever option is chosen.** A confirmed report pointing at a record that no
longer exists is a hole in the reporting, and a feed cleanup is not the place
to punch one. The response says how many were protected that way.

### Map zones: ground that is different for a while

A NOTAM, for a case file. Somewhere is not normal, for a period, and anybody
opening the map should see that before reading a report. A protest, a cordon, a
road nobody should be on tonight.

**A zone is not a Location.** A Location is a place that exists; a zone is a
judgement about ground over a period, and the same ground can carry several at
once — a protest inside a district that is separately non-permissive. Putting
the assessment on the Location record would mean editing the town to say its
square was briefly dangerous, and then editing it back.

It uses the same vocabulary a Location's `environment` does, and is drawn in
the colour that vocabulary implies, read from the theme rather than hardcoded
so it survives a palette change.

#### The environment scale has its own colour tokens

`--zone-permissive` / `--zone-semi` / `--zone-non` / `--zone-denied` /
`--zone-unknown`, defined per palette in both themes, and used by zones *and*
by Location pins.

They are dedicated tokens because the first version was not. It pointed at
`--accent`, `--amber` and `--vehicle`, which are chosen for other jobs:
`--accent` is whatever the palette's personality colour happens to be, and in
graphite that is a tan while `--amber` is nearly the same tan. Permissive and
semi-permissive came out indistinguishable, and non-permissive — borrowing the
Vehicle pill's orange — did not read as a warning at all. **A scale that has to
be read at a glance cannot be assembled from colours picked for something
else.**

The scale is the traffic light everyone already knows: green, yellow, red.
Denied sits beyond non-permissive as a darker red *and* is drawn with a heavier
stroke and a denser fill, because two reds a shade apart is a thin way to carry
"difficult and dangerous" against "not an option" — particularly for a reader
who does not separate those two reds easily. Unknown is a plain grey that reads
as "nobody has said" rather than as a step on the scale, and a Location nobody
has assessed is drawn in that same grey: not-assessed and assessed-unknown both
mean do not assume it is fine.

Light-mode values are darker and more saturated than the dark-mode ones,
because the same hue over a pale basemap washes out entirely.

Colours are read from the CSS custom properties at draw time rather than baked
in, so switching palette or theme recolours what is already on the map.

#### Location pins are circles, not Leaflet's pin image

Leaflet's default marker is a PNG, and a PNG cannot be recoloured — which
rules it out the moment the colour is the point. Pins are `L.circleMarker`s,
which also matches the dot language the relationship network already uses, so
a dot means "a record" consistently across the app.

#### Expiry dims; it never deletes

The clock is the point, and so is what happens when it runs out. An expired
zone is drawn dashed and faint, stays exactly where it was, and keeps its link
to its Event and therefore to every report written while it was running.
"Where was the cordon that night" is asked weeks later, and an app that tidied
the answer away would be useless for the one question the feature exists to
answer. The map offers a *Show expired* toggle rather than a purge.

#### The Event link is optional, and that is deliberate

A protest is an Event; a permanently denied area is not. Drawing a zone offers
to create the Event and ties the two together, taking the Event's dates from
the zone's own clock — so "relevant until" on the dashboard and the zone's
expiry are one fact rather than two that can drift. Leaving it blank is a
supported answer, because forcing standing ground to invent an Event that never
ends would be the model telling a lie to keep its own shape.

`ON DELETE SET NULL`, not `CASCADE`: deleting the Event should not silently
take the ground assessment with it.

#### Changing the assessment is reporting, not correcting

A protest going semi-permissive to non-permissive at 18:40 **is** the finding.
Every change writes a row on the zone's own timeline — the new value, the one
it replaced, who, when, and a note saying why — and the first row is written at
creation so the history is complete rather than starting at the first edit.

That history is case material, kept on the zone, not only in the audit log. The
audit log records it too, but it is a log: an analyst should not have to ask an
admin to read their own timeline, and "police line moved onto the bridge" is a
line that belongs in a report.

Editing anything else, or re-saving the same environment, writes nothing.

#### Geometry without PostGIS

GeoJSON in a JSONB column, with denormalised bounds alongside it in indexed
columns. The operations this needs are draw, redraw, and "what of mine is
inside" — point-in-polygon is twenty lines of Python (`api/zones.py`), and an
extension would put a migration between a user and `docker compose up` for
arithmetic the app can do itself.

Containment is a cheap indexed rectangle test first, then the exact test only
on what survives it. Three details that are easy to get wrong and are handled
explicitly:

- **A point exactly on the boundary counts as inside.** Ray casting gives an
  arbitrary answer there, and "arbitrary" is not something to hand an analyst
  asking whether an address is inside a cordon. It happens for real: a zone
  corner dropped on a junction, and later a Location recorded at the same one.
- **Circles pad their bounding box by latitude.** Degrees of longitude shrink
  with the cosine of latitude, so one figure for both would make a northern
  circle's box too narrow and lose zones that genuinely contain a point.
- **GeoJSON is [lon, lat] and closes its rings; Leaflet is [lat, lon] and does
  not.** Both are correct and neither is negotiable, so the conversion happens
  in exactly one function.

Nothing is written to the records inside a zone. The list on the zone panel is
computed live, which means an expiring zone leaves no stale assessment behind
on a record that outlives it — and a Location's own page asks the question in
reverse, listing the zones that cover it, expired ones included.

#### Drawing

Polygon, rectangle and circle, hand-rolled rather than pulling in Leaflet.draw:
three shapes, one interaction each, against a standing rule about new runtime
dependencies. Two collisions with the existing map had to be handled and are
the sort of thing that looks like a bug report later:

- The map's own click handler creates a Location. Drawing a polygon clicks the
  map repeatedly, so it is suppressed while a draw is in progress.
- Leaflet synthesises a map `click` from the mousedown/mouseup pair that ends a
  rectangle or circle, arriving *after* the draw is over — which would open a
  New Location form on top of the zone form. A short suppression window after a
  completed shape handles it.
- Existing zones stop accepting clicks while drawing, so a cordon can be drawn
  inside a district that is already marked.

### Alignment, and why a faction is a record rather than a field

Person used to carry `faction`, a dropdown of
Friendly/Neutral/Unknown/Hostile/**Family**. It is now `alignment`, it has
lost Family, and it is on Organizations, Sources and Vehicles as well.

The rename is the point rather than cosmetic. **A faction is a thing.** "Iron
Horse Militia" has members, a place, a history and reporting about it — it is
an Organization with its own record and its own alignment, and belonging to it
is a relationship. Asking a Person to carry a faction in a dropdown was making
a text field do an entity's job, and it could only ever hold one. Alignment
asks the narrower question that dropdown was really being used for: **whose
side is this one subject on.** The two now compose — a Person aligned Friendly
can be `member_of` an Organization aligned Hostile, which is a real situation
the old model could not express at all.

It is on Sources deliberately, and deliberately separate from the A–F
reliability rating. **A source can be Hostile and grade A.** Whose side they
are on and how well their reporting has held up are different questions, and
collapsing them is how a reliable adversary source quietly gets discounted.

Vehicles carry it because a vehicle is often identified long before its owner
is, and "the van the hostile group uses" is the fact in hand.

**Family is gone because kinship is a relationship.** `family_of` already
exists, works between any two records, and can say *how* they are related.
Sitting in the same dropdown as Friendly/Hostile, Family also made a record
unable to say both that someone is a relative and that they are hostile —
which is exactly the case where you would most want to say both.

Records that already hold Family keep it. The API refuses to SET it on
anything that does not already have it, and the form puts it back into the
list, marked `(retired)`, for the records that do. That last part is not
politeness: without it, opening one of those records to change an occupation
would re-save alignment as blank, and the app would be rewriting an analyst's
own assessment as a side effect of an unrelated edit. Blanking them in the
migration was the other option and would have been worse — blank means "nobody
has assessed this", which about those people is untrue.

### A hostile record's name is marked wherever it appears

Alignment is not only a field on a form. A record assessed **Hostile** has its
name shown in red in the entity tree, on its own page, in the relationship list
of anything connected to it, in global search, and on the relationship network.

Three decisions inside that:

- **Hostile only.** Friendly and Neutral are not coloured. A list where every
  name is coloured is a list where no colour means anything.
- **Bold as well as red.** Colour alone fails a colour-blind reader and any
  greyscale printout, and this is the fact you least want quietly missed. The
  name also carries a title attribute.
- **The network colours the label, not the dot.** Dots there are graded by
  relationship count, which is the entire thing that picture says; recolouring
  them by alignment would destroy it.

Serving this meant putting `alignment` into three payloads that did not carry
it — the entity list, the graph nodes, and the other end of each relationship.
`entities.fetch_alignments()` answers all three with one UNION over the four
detail tables that hold one, keyed by id, rather than four round trips or a
join baked into `ENTITY_CORE_COLS` (which would put NULLs on every row in the
app to save a query on a page that is already paginated).

### Locations get an environment, not an alignment

Ground does not take a side. It is more or less workable, which is a different
question with a different vocabulary: Permissive / Semi-permissive /
Non-permissive / **Denied** / Unknown.

Denied is distinct from Non-permissive on purpose. Non-permissive means
working there would be difficult and dangerous; denied means it is not an
option at all. An analyst who can only say "non-permissive" about both has
lost the distinction that decides whether a plan exists.

Semi-permissive is in the list because it is where most places actually are,
and a three-value scale without it forces everything to round up or down.

### Unknown detail fields are now an error

Adding a field to four kinds and not the fifth made an old wart worth fixing.
Pydantic ignores unknown keys by default, so `POST /api/entities` with an
`alignment` on a Location used to create the record, return 201, and keep none
of it — indistinguishable from success for whoever typed it. `_parsed_details`
now rejects unknown keys and says which kinds *do* have that field.

The extraction pipeline is the deliberate exception and still filters through
`extraction._lenient_details` first: a model's proposals are best-effort, and
one bad field should not block accepting an otherwise-good suggestion. A
person filling in a form is owed the opposite.

### Renamed columns, and old backups

A backup zip carries its own column list, and the restore checks that list
against the live schema before touching a row — so a rename is
indistinguishable from a column that does not exist, and every archive taken
before this change would have been rejected with "upgrade this deployment
first", advice that cannot work because the deployment is already newer.

`RENAMED_COLUMNS` in `api/backup.py` maps `person_details.faction` to
`alignment` on the way in, and the restore reports what it adapted. Entries
belong there permanently, not for one release: an archive is only worth having
if it can still be read years later, which is the whole reason the backup
format is JSON with the column names travelling alongside the data.

### Vehicles

A vehicle is a thing people are seen in, arrive in and move things with, and
it identifies them the way a phone number does. "The white panel van with the
cracked bumper" is a lead; a plate is a lead you can act on. Before this type
existed those facts went into a Location's notes or a Person's physical
description, where nothing could search them and nothing could relate them.

**Style is a dropdown; make, model and colour are not.** Filtering "show me
the panel vans" only works if everyone spells it the same way, and free text
gives you "panel van", "Panel Van" and "van (panel)". Make and model stay free
text because constraining them would mean shipping and maintaining a list of
every manufacturer on earth. The list is deliberately wider than cars — a boat
and a light aircraft answer the same question ("what did they arrive in"), and
giving them their own types would split one question into three.

| | |
|---|---|
| Road | Sedan, Coupe, Hatchback, SUV, Pickup truck, Van, Panel van, Box truck, Semi-tractor, Bus, Motorcycle |
| Off-road | ATV/UTV, Trailer |
| Other | Boat, Aircraft, Other |

**The plate is stored exactly as it was written.** Not upper-cased, not
stripped of punctuation, not validated against any format. How a source wrote
a plate down is itself information, and normalising it destroys that. Lookup
does not suffer for it: there is a functional index on the plate with case and
punctuation removed, so "ABC 123", "abc-123" and "ABC123" all find each other.

**A plate is not a key.** Plates are not unique between issuing authorities —
which is what `plate_region` is for — and not unique over time either, since a
plate moves between vehicles. Nothing in the app treats a plate as an
identifier, and two records with the same plate are not automatically the same
vehicle.

**Two relationship types come with it**: `drives` (reading as "driven by" from
the vehicle's side) and `seen_in` ("carried"). Who was behind the wheel is a
different fact from who owns it, and in this kind of work it is usually the
more useful one — `owns` was already there for the ownership question.

Extraction proposes vehicles like any other record: "a white Ford panel van,
plate ABC-123" in a statement becomes a proposed Vehicle in the review queue,
with the style constrained to the list above so the model cannot invent one.
As with everything else, nothing is written until you accept it.

### A Person's two status fields

They answer different questions and are deliberately not one dropdown.
**Status** is Living / Deceased / Unknown. **Disposition** is At liberty /
Captured / Detained / Evading / Missing. A person can be Deceased with a
disposition that is simply no longer relevant, and recording both in one
field would force a choice between the two facts.

Three things about the values are on purpose:

- **Blank is not "Unknown".** Blank means nobody has assessed it; Unknown
  means somebody tried and could not resolve it. Both are real states and the
  app keeps them apart everywhere — a record with a blank Status is not
  asserting anything, and an app that collapsed the two would be quietly
  turning "no news" into a finding.
- **Detained is not Captured.** Held by authorities and taken by a party to
  the case are different facts with different consequences, and a file that
  calls both "captured" loses the distinction the moment it matters.
- **Missing is not Evading.** Evading asserts that someone is choosing to
  avoid being found. For most people whose whereabouts are unknown the
  reporting supports no such thing, and offering only Evading would push
  analysts into asserting an intent they cannot actually see.

In the Entities list, a Person shows a badge only when their state is worth
interrupting a scan for — Living and At liberty render nothing at all, so a
column of forty people shows the two that need attention rather than forty
pills. These two are also the only detail fields the list view carries: they
are the ones that change what you do on seeing a name, and finding out someone
is deceased by opening their record is a worse experience than a grey label
saying so.

### Event expiry

An Event can carry a **relevant until** date: the point past which it stops
being current intelligence. A protest is stale in a fortnight. An occupation,
a disaster or a standing threat stays relevant for a year. Blank means no
expiry has been set, and never means expired.

It is deliberately **not** the same field as "ended at", and the form says so,
because the two get confused constantly: a raid that ended in ten minutes can
stay relevant for months, and a strike that is still running can already be old
news. One is when the thing stopped happening; the other is when it stopped
mattering.

An explicit date, not a shelf-life preset that computes one. The analyst says
when it stops being relevant and the app does no arithmetic on their behalf.

**Nothing is hidden, archived or deleted when the date passes.** The Event gets
an `expired` badge on its record and in the Entities list, and the Entities
toolbar has a **Hide expired events** tick-box which is off by default. That is
the whole of it. Retiring records on a date would be the app making a judgement
the analyst did not make, and an Event going quiet on its own is exactly the
kind of thing that gets noticed six weeks too late.

The expired state is computed on read, never stored — "expired" is a fact about
today, and a stored flag would be wrong the morning after it was written. An
Event marked relevant until the 14th is still relevant *on* the 14th.

### Contact details

A Person, Organization or Location can hold a list of **contact points**:
a type (Phone / Mobile / Email / Address / Radio / Messaging / Social /
Website / Other), an optional label ("work", "switchboard", "after hours"),
the value, an optional note, and a **preferred** flag.

A repeatable list rather than phone/email/address columns, because one
organisation has a switchboard *and* a press office *and* an after-hours
number, and flattening that into one column each loses whichever one you
didn't pick.

**Preferred rows, and only those, go into an exported PDF.** The export is
what leaves the building; a dossier carrying every number ever recorded
against someone would make sharing a report a bigger decision than it should
be. Mark the one way you'd want a recipient to make contact, and the rest
stays in the app.

**A contact point is not a Communication entity**, and the difference matters
for keeping the case graph readable:

- A **Communication entity** is a channel that is itself an object of
  interest — a radio net being monitored, a number that keeps appearing in
  reporting. It belongs in the entity list, the graph and the correlation
  queue.
- A **contact point** is directory information. The office switchboard is not
  a lead, it is how you phone the office. Recording switchboards as entities
  would bury the channels that actually matter under a phone book.

Only those three types take contact points. An Event or a Communication has
nobody to contact; a Source's handling arrangements are a more sensitive
thing than a directory entry and stay in that type's handling notes, which
is a deliberate refusal rather than an oversight.

The audit trail records that a contact point was added, edited or removed,
its type and its label — never its value. An audit log holding every phone
number ever typed into the app would be a second, less protected copy of the
directory, which is the same reason entity edits record field names and not
field values.

### Communication's medium + detail field

A Communication entity's **Medium** is a fixed dropdown, not free text —
Cellphone, Landline, Text/SMS, Satellite Phone, Email, HF Radio, VHF Radio,
UHF Radio, FM Radio, GMRS, FRS, CB Radio, In-Person, Mail/Courier, Other.
It's built around a personal/family emergency-communications plan: the mix
of phone/email and licensed-or-unlicensed radio services a household
actually falls back to when normal infrastructure is down, not a generic
"how did you talk to them" list.

Next to it, **Detail** is a second field whose meaning changes with
whatever medium is selected — the form relabels it automatically:

| Medium | Detail means |
|---|---|
| Cellphone, Landline, Text/SMS, Satellite Phone | Phone number |
| Email | Email address |
| HF Radio, VHF Radio, UHF Radio, FM Radio | Frequency (MHz) |
| GMRS, FRS, CB Radio | Channel |
| In-Person | Location |
| Mail/Courier | Address |
| Other | Details |

Detail is plain text at the database level regardless of medium — nothing
validates that a "Frequency" value actually looks like a frequency. That's
a deliberate simplification: a phone number, an email address, a
frequency, and a channel number don't share a format worth enforcing, and
this app has no need to query "every communication on 146.520 MHz" in a
structured way. If the worker's extraction pipeline is enabled, it now
also proposes `medium` (constrained to the same list) and `medium_detail`
from document text — e.g. "reachable at 555-0100" on a document mentioning
a phone call.

Relationships connect any two entities regardless of type, with a
free-text (but lowercase-snake-case) relationship type — `employed_by`,
`member_of`, `located_at`, whatever the case calls for. The UI suggests a
starter list, but nothing stops you from typing your own; the field isn't
constrained to an enum at the database level.

### Kinship, and reading an edge from both ends

The suggested list includes `spouse_of`, `significant_of`, `parent_of` and
`child_of` alongside the general `family_of`, so an identity picture can carry
who is actually related to whom rather than a flat "family" edge.

A relationship is stored **once**, as one directed row, and appears on both
entities' pages. That creates a problem the app has to solve rather than
ignore: an edge recorded as "Anna `child_of` Boris" would, on Boris's page,
still be labelled "child of Anna" — precisely backwards, and exactly the kind
of error a family tree makes silently.

So every relationship carries a `reads_as` alongside its stored type: the
wording for the end you're looking at. The one stored row shows as **child of
Boris** on Anna's page and **parent of Anna** on Boris's. Nothing is
duplicated, and the direction you originally entered is preserved.

This applies to the older types too, which had the same problem: an employer's
page now reads "employs" rather than "employed by", a landlord's "location of"
rather than "located at". Genuinely symmetric types (`spouse_of`,
`significant_of`, `family_of`, `associate_of`, `communicated_with`,
`in_conflict_with`) read the same from both ends, which is correct, and any
type you invent that the app has never seen also reads the same both ways
rather than breaking.

`parent_of` and `child_of` are both offered even though each is the other's
inverse — you record whichever one you're actually thinking in at the time,
and the other end reads correctly regardless. The relationship graph still
draws the arrow in its stored direction, since the arrow and the label there
have to agree.

### Address geocoding + Maidenhead grid

A Location only needs an address — latitude and longitude are optional.
Leave them blank and the worker looks the address up in the background (via
the public [OpenStreetMap Nominatim](https://nominatim.openstreetmap.org)
API by default) and fills them in on its own, usually within a few seconds.
Enter coordinates yourself instead and geocoding never runs at all — typed
or already-geocoded coordinates always win, and nothing here ever silently
overwrites them.

While a lookup is in progress the location's detail page shows "Looking up
coordinates for this address…". If it can't be resolved (a typo, an
address the geocoder doesn't recognize, no network route to it — this
matters on an offline/LAN-only Pi deployment) it shows the failure reason
with a **Retry** button, and nothing is retried automatically beyond that —
an unfixable address would otherwise burn through the rate-limited request
budget forever for no benefit. Fix the address and save (which re-queues
it), or hit Retry if the address was fine and it just failed transiently
(a network blip, the service being briefly unreachable).

Once a Location has both coordinates, it also gets a six-character
[Maidenhead grid locator](https://en.wikipedia.org/wiki/Maidenhead_Locator_System)
(e.g. `FM18lv`) computed and shown automatically — the grid-square system
amateur radio operators use for a station's location, a natural companion
to Communication's HF/VHF/UHF/FM radio mediums. This is pure math from
whatever coordinates the location has, however they got there; it isn't a
separate lookup and never fails.

To force a location to re-geocode from its address — after fixing a typo,
say — clear both Latitude and Longitude in the edit form and save; that's
the one thing blanking those two fields specifically does (every other
field in this app treats "leave it blank" as "don't change it," but
lat/lng need to be genuinely clearable for this to be possible at all).

**Privacy note**: the default provider sends the address text you type over
the internet to OpenStreetMap's public servers to be resolved. If that's
not acceptable for how you're using this (this is a HUMINT/OSINT tool,
after all — the addresses in it may be exactly the kind of thing you don't
want leaving the machine), point `NOMINATIM_BASE_URL` in `.env` at a
Nominatim-compatible geocoder you run yourself on your own network instead,
or set `GEOCODE_ENABLED=false` to turn the whole feature off and enter
coordinates by hand. Either way, Nominatim's usage policy asks automated
clients to identify themselves — set `GEOCODE_USER_AGENT` in `.env` to
something that actually identifies you (an email address is fine) rather
than leaving the default placeholder, which every self-hosted copy of this
app would otherwise share.

### Offline maps: sources, packs, and why the licence lives in the schema

The Map view and the debrief's place-picker are only useful if there is
something under the pins, and this app is meant to keep working with no route
out. Three separate things used to need the internet, and they failed
differently:

| | Was | Now |
|---|---|---|
| Leaflet itself | loaded from unpkg | served from `frontend/vendor/leaflet/` |
| Tiles | hardcoded to OpenStreetMap | any registered source, or a downloaded pack |
| Address → coordinates | Nominatim | unchanged; off with `GEOCODE_ENABLED=false` |

Leaflet coming from a CDN was the worst of the three, because losing it took
out the whole view rather than degrading it — and the view is what lets a
source point at a place they cannot name. It is vendored now.

**A source is an XYZ URL template**, which is the same shape ATAK's map source
XML uses, so importing one of those is a parse rather than a translation
(`{$z}` becomes `{z}`, `<serverParts>a b c</serverParts>` becomes `abc`).
Collections of those files are published online; the Admin page links to one.

**A pack is an area of one source downloaded into an MBTiles file** — plain
SQLite with tile blobs in it. That is the whole "map server": no tile-server
container, no new dependency, no build step, about sixty lines to serve one.
It is also the format ATAK, QGIS and every other offline map tool reads, so a
pack built here works elsewhere and a pack built elsewhere drops in here.
Finished packs are checkpointed out of WAL mode deliberately, so the artifact
is one file somebody can copy rather than three.

#### `allow_download` is a separate column from `is_active`, and that is the point

Viewing someone's tile server and copying it wholesale are different
permissions, and most operators grant only the first. OpenStreetMap's own
tile usage policy prohibits bulk download outright. So the shipped OSM source
has `allow_download = FALSE` and the app **refuses to let an admin tick it
on** — the reason it is off is a licence term, not a preference. Adding your
own entry for a server you are entitled to cache takes one click.

This is in the schema rather than in the documentation because documentation
is not where somebody is standing when they press the button. Imported
sources default to not-downloadable for the same reason: the importer cannot
know what a given server's terms say, and the honest default is no.

What this project will not do is ship a list of other people's tile servers
and imply that caching them is fine.

#### Tile counts quadruple per zoom level

This is the single fact most likely to turn a coffee break into a fortnight,
so the download dialog computes the count per zoom and shows it before the
button does anything. A box that looks reasonable at z12 is a quarter of a
million tiles at z16. `MAP_PACK_MAX_TILES` refuses the truly silly ones with a
number rather than accepting them and running for weeks.

The downloader runs on **its own thread** in the worker rather than as another
step in the poll loop. Every other job there finishes in seconds; a pack is
hours of small HTTP requests, and in the loop it would mean an analyst's
uploaded document waiting behind a basemap. Rate limiting is global rather
than per-pack, because two packs hitting one host at once is exactly what gets
a deployment blocked.

A 404 from a tile server is recorded as "missing", not as a failure: that is
how most sources answer for tiles outside their coverage, and a rectangle
dragged over a coastline is full of them. Real failures are counted, and
twenty-five in a row abandons the pack — at that point the host is refusing
us and another six hours of trying helps nobody.

Tiles already in the file are skipped, so a cancelled or crashed job resumes
rather than re-fetching several hundred thousand tiles somebody already paid
for once. Stop is a flag the worker checks between tiles, not a kill: whatever
has been written stays usable.

### Two different reliability ratings, on purpose

This app uses the NATO/Admiralty System in two different places for two
different things, and they are deliberately not the same rating:

- **Source reliability (A–F)**, on a `source` entity — how trustworthy is
  *this source in general*, independent of any one thing they've told you.
- **Report credibility (1–6)**, on a report — how credible is *this specific
  piece of information*, independent of who's saying it.

A reliable source (A) can still pass along one dubious tip (6), and an
unproven source (F) can occasionally be dead right (1). Collapsing these
into one rating would lose exactly the distinction Admiralty grading exists
to capture.

### Report criticality — a third axis, not a fourth rating

Reports also carry a **criticality**: message precedence, one of **Flash**,
**Immediate**, **Priority**, **Routine**, or unset.

This is a different question again from either Admiralty rating. Credibility is
how much you believe it. Criticality is **how fast somebody needs to act on
it**. The two are independent and a report can be any combination:

- A rock-solid, well-corroborated write-up of who organised a protest:
  credibility **1**, criticality **Routine**.
- A thin, single-source indication that a hostage is about to be moved:
  credibility **4**, criticality **Flash**.

Both are worth writing up. Only one is worth waking someone for, and an app
with just one number cannot say which.

Precedence rather than Low/Medium/High/Critical because it describes required
speed of response instead of vague importance, and because anyone with a
service background already reads it correctly.

Unset renders as nothing at all — no badge, no default. A report nobody has
rated is not a report someone judged to be Routine, and showing one as the
other would be the app inventing an assessment. For the same reason, sorting
the Reports list by "Most urgent first" puts unrated reports **last**: a plain
sort on a nullable column would otherwise claim they were the most urgent
things in the case file.

Criticality shows as a badge in the Reports list, on the report, on the
dashboard, and leads the subtitle of an exported PDF — `FLASH · Confirmed
report · Information credibility: 4 — Doubtful` — because it is the first thing
a recipient needs off the front page. In the PDF it carries the same colour it
has on screen: Flash white-on-red, Immediate red, Priority amber, Routine grey.
The Reports view can filter by it and sort by it.



## Writing reports: mentions, and the guided debrief

Two ways in, both on the Reports view. They produce the same kind of report
and you can move between them — a debrief files an ordinary report that you
then edit in the editor like any other.

### The editor, and @-mentions

**+ New Report** opens a full-page editor rather than a modal: prose on the
left, the list of entities this report names on the right.

Typing `@` followed by a name searches your entities as you type and offers
to **create** one of any type with that name if it isn't on file yet.
Picking either inserts the name into the text and adds it to the side panel.
A new entity created this way is marked `new` and exists from that moment —
you can fill in its details later, from its own page.

The point is that you never have to leave the report to record that someone
new came up. Building three People, a Location and an Event in another tab
before you can start writing is the thing this replaces.

Mechanically, a mention is an ordinary markdown link to `#/entities/<id>`,
stored in the report body like any other link. That has consequences worth
knowing:

- **Mentioning someone is what links them.** The server re-reads the body on
  every save and links everyone named in it, whether or not the client sent
  them in the entity list. Anything you attach with the side panel's
  search box is added on top of that — the two are unioned, never
  substituted, so no client can accidentally unlink someone by sending a
  short list.
- **To unlink someone named in the text, delete the mention.** The side
  panel refuses to remove them and says why, rather than removing them from
  the list and letting them silently reappear on the next save.
- **A mention pointing at an entity that no longer exists is ignored**, not
  an error. A report that can't be saved because of a stale link in its own
  prose would be a much worse failure than a link that quietly does nothing.
- In the report, mentions render as clickable links straight to that
  entity's page. In the **PDF export** they become bold names — a printed
  page has nothing to click, and the entity's full dossier is in the same
  document anyway.

The editor keeps a local draft of an unsaved report in the browser and
offers it back if you get interrupted; once the report exists on the server,
autosave takes over and the local copy is dropped, so there's never a second
competing copy of the same report.

### The guided debrief

**Start a debrief** is for the other case: you're sitting across from a
source and the report doesn't exist yet. Five steps, one question each:

1. **Source** — who you're debriefing (find them or create them here), their
   Admiralty reliability, and when this conversation is happening. If they're
   a Source entity, the rating can be written back onto their own record —
   there's a checkbox, and it only appears for entity types that have
   somewhere to put it.
2. **When and where** — when the reported events happened (not when you're
   talking), the location, and optionally an Event to tie this to. A location
   created here takes an address, which is also what puts it on the Map; an
   event created here is dated from the time you entered above.
3. **Who was involved** — every other person, organisation or means of
   communication that came up, each with a short note on their part in it.
   That note goes into the report, and for anyone newly created it becomes
   the first line of their record.
4. **What happened** — the narrative, in the source's own account. `@` works
   here exactly as in the editor. A list of standing interview questions sits
   beside it; clicking one drops it in as a heading to answer underneath.
5. **Assessment** — your read on it, kept under its own heading because what
   the source said and what you make of it are different things, plus the
   information credibility rating and the report title.

The last step shows everything gathered before you file it, including which
entities were created along the way. Filing produces one structured report
with `Source` / `When and where` / `Who was involved` / `Narrative` /
`Assessment` sections, linked to everything the debrief touched, which then
exports as a PDF intel package like any other report.

Two things to know:

- **Entities are created as you name them, not when you file.** That's what
  makes the workflow work — the mention needs a real id to point at. It also
  means abandoning a debrief halfway leaves the entities you created behind.
  They can be archived from their own pages if you don't want them.
- **The debrief is saved in your browser as you go.** A refresh, a closed
  laptop, or wandering off to another tab mid-interview doesn't lose it —
  the next time you start a debrief it offers to pick that one back up, and
  declining throws it away.

There is no `debrief` table. A debrief produces a report and some entities;
inventing a third record type to own them would have meant a schema
migration on every existing deployment in exchange for nothing an analyst
would ever see.

## Exports

### A report as a PDF

**Export PDF** on any report's page produces a formatted intel package —
one document containing everything a reader needs to make sense of that
report without access to this app:

1. A title block: status, Admiralty credibility with the grade spelled out
   (`2 — Probably true`, not a bare `2`, since the recipient won't have the
   legend), record id, created/updated times, and who exported it when.
2. The report body, with its markdown rendered — headings, lists,
   emphasis, quotes, code blocks and links all come across.
3. A dossier for every entity the report links to: its type and description,
   its detail fields, its relationships (including ones that don't involve
   this report), and any photographs attached to it, embedded full-size.
4. The report's own attachments — images embedded, everything else listed
   in a table with its type and extraction status.

Every page carries a header, a footer note, and `Page X of Y`. Any
logged-in analyst can export; the package contains nothing they couldn't
already read on screen.

Two things it is **not**: it isn't a classified-marking system — the header
says "Case Material" and the footer says to handle it per your own policy,
because this app has no classification model and inventing one that looks
official would be worse than having none. And it isn't a redaction tool:
everything on the linked entities goes into the document, so check what a
report is linked to before sending the export outside your own circle.

Three `.env` values adjust it, none of them required:
`PDF_PAGE_SIZE` (`letter` by default, or `a4`), `PDF_HEADER_LABEL`, and
`PDF_FOOTER_NOTE` — the latter two let you put your own organisation's
wording in the running header and footer.

The PDF is generated with ReportLab, which is pure Python: no Chromium, no
pango/cairo, nothing to apt-install, which is what keeps the api image
buildable on a Raspberry Pi. Non-Latin text (Cyrillic, Greek, accented
names) renders properly via DejaVu, installed in the api image; if those
fonts are ever missing the export still works, falling back to a built-in
font and transliterating what it can't draw rather than printing rows of
black boxes.

**Message precedence prints in colour.** A Flash report exports with the same
white-on-red badge it wears in the app, Immediate in red, Priority in amber,
Routine in grey — in the subtitle, in the header table, and in every list of
reports in every export below. It used to print in the same grey as everything
else, which meant the one thing a recipient needs off the front page was the
one thing the page didn't show them. An unrated report still prints nothing:
"unset" is a deliberate and common state here, and giving it a colour would
make it look like a fourth level. The colours are the light-theme palette, not
the dark one — a PDF is printed on white, and they were contrast-checked
against exactly that background.

### Three more packages

Beyond the per-report export there are three others, all producing the same
kind of document — same header, footer, page numbering and styling, so a team
ends up with one document format rather than four.

**A dossier**, from the **Export** button on any record's page. One button,
three shapes, and the shape you want leads the menu for the kind of record
you're on:

| Shape | What it is |
|---|---|
| Organisation report | Structure, membership and holdings — who belongs to it, what it controls, who it is in conflict with |
| Target package | Everything on file for one record, grouped by status, affiliations, family, places and contacts |
| Plain dossier | The record and one flat list of what it connects to |

All three collect the **same** material — the record, everything one step away
from it, every report that mentions it, and its attachments. They differ only
in how that is arranged, because what separates an "org report" from a "target
package" is the question the reader is holding, not the data. Choosing a shape
never changes how much of the file you get.

One step, not two, is deliberate. A two-hop package from a well-connected
organisation runs past sixty pages, and a package nobody reads to the end is
not a better package. Records connected only through an intermediary are named
in the dossier but not reproduced in it.

**A hotspot package**, from the `PDF` button on any hotspot on the dashboard,
or from the Export menu on a Location. This is the dashboard's "you can click
a number and see the rows behind it" promise, as a document: the location, the
arithmetic, and *every* contributing record — not the capped sample the panel
shows. It states the threshold, explains the three routes by which a record
counts toward a location, and lists the full chronology.

It counts using the same SQL the dashboard panel uses. Written twice, the two
would agree on the day they were written and drift apart the first time either
was tuned — silently, with the package listing records that add up to a
different number than the panel that sent you to it.

A location *below* the threshold exports too, and says on its face that it is
below it. "Quieter than you thought" is also an answer, and an export that
404'd instead would make you go and count by hand to find that out.

**An executive summary**, from the dashboard toolbar, over a period you pick
(7 to 90 days, separate from the panel window — a team lead asking about last
month wants a month, whatever an analyst left selected). It contains:

- Headline counts — reports, records, relationships, attachments, drafts,
  urgent reporting — each with the **previous period beside it**, because a
  number on its own cannot be read. 41 reports against 12 last month is a
  different conversation from 41 against 58.
- Two weekly charts, stacked by analyst: reports filed, and records created.
  Weeks with no work stay empty rather than closing up, so a quiet fortnight
  reads as a gap instead of disappearing.
- A per-analyst totals table.
- What was added, by record type and by precedence.
- Hotspots for the period, and what is still outstanding at the end of it.

It states no conclusion. There is no sentence anywhere in it saying activity is
escalating or the team is performing, because a quiet month during a holiday
and a quiet month because a source went dark produce the same bar chart, and a
summary that answered that question from the chart would be inventing a
judgement. It also says, on its first page, that it attributes work to named
people — worth knowing before circulating it, and the audit entry records which
names were in it.

The per-analyst table counts work produced, not its value. A single
well-sourced report can be worth a dozen routine notes and the table cannot
tell them apart; the document says so in as many words, next to the table.

Every export is audited, and the entry names what left with it — the
neighbours and reports inside a dossier, the records behind a hotspot, the
analysts named in a summary. Any logged-in analyst can produce any of them:
each one contains only records that person can already read on screen, so an
export permission would be theatre. What is *not* theatre is the record that
fifty of those records were carried out of the system as a file, which is the
whole reason the audit trail exists.

The charts are drawn with ReportLab's own graphics, not matplotlib — same
reason as the rest of the PDF stack: matplotlib is a ~60MB install with a
compiled numpy underneath it, which is a bad trade for two bar charts a month
on a machine that deploys off a memory card.

## Bulk CSV import

For adding entities faster than the one-at-a-time **+ New Entity** form
allows — a list of people from another document, a batch of known
locations, whatever you already have sitting in a spreadsheet.

1. Entities view → **Import CSV** → pick the entity type → **Download
   template**. Each type gets its own template with exactly that type's
   columns — `name` and `description` (every type has these), plus that
   type's own detail fields in the same order they appear in the entity
   form (e.g. Person's template has `aliases, date_of_birth, alignment,
   occupation, physical_description` after `name, description`).
2. Row 2 of the template is a live example showing the expected format for
   every column (dates as `YYYY-MM-DD`, Faction as one of the five fixed
   values, `aliases` as semicolon-separated since it's the one list field —
   `Johnny; JS`), not just a written explanation. It's safe to leave in
   place — every cell in it starts with `#`, and any row whose `name`
   column starts with `#` is skipped automatically rather than imported as
   a literal entity named "# EXAMPLE...". Delete it or leave it; either way
   it doesn't count as a data row.
3. Add one row per entity below that. `name` is the only required column;
   leave any other cell blank to leave that field unset, exactly like
   leaving it blank in the entity form. One exception: for Location rows,
   leaving `lat`/`lng` blank while `address` is filled in queues that row
   for automatic geocoding, same as creating it through the form with
   coordinates left blank — see "Address geocoding + Maidenhead grid"
   above. Fill in `lat`/`lng` yourself in the row to skip that entirely.
4. Upload the filled-in file. Every row is validated and inserted
   independently — through the exact same rules as creating that entity
   one at a time by hand, so a row that fails does so for a reason you'd
   recognize from the regular form (e.g. `alignment` not one of the four
   allowed values, or a badly formatted date). A typo in one row never
   blocks the others: you get back a count of what succeeded and, for
   anything that didn't, the row number and the reason. Fix and re-upload
   just those rows — **not** the whole file, since there's no duplicate
   detection and re-uploading a row that already succeeded creates a
   second, separate entity.
5. Up to 5,000 data rows per upload (a safety limit, not a realistic
   expectation — split a bigger batch into multiple files if you ever hit
   it).

Any logged-in analyst can use this, same as the regular entity form — it's
not admin-gated.

## Extraction and correlation: review queues, not autopilot

### Which model, and how it's configured

Out of the box (the settings in `.env.example`), the worker talks to
`OLLAMA_MODEL=llama3.1:8b` at `OLLAMA_BASE_URL=http://ollama:11434` — the
bundled `ollama` container, started with `docker compose --profile
local-llm up -d`. A model still has to be downloaded before any of this
produces anything, but you no longer need a terminal for that: **Admin
settings → Ollama Settings → Pull a model** does it from inside the app.
(`docker compose exec ollama ollama pull llama3.1:8b` still works if you
prefer.)

**Admin settings → Ollama Settings** lets an admin change the base URL, the
three models, the enabled flag, and the per-call timeout from inside the app,
without editing `.env` or rebuilding. A saved change takes effect within one
worker poll cycle (`WORKER_POLL_INTERVAL_SECONDS`, 20 seconds by default) —
no `docker compose restart worker` needed. Leaving a field blank/using the
"Reset to .env default" button falls back to whatever `.env` has for that
setting; `.env` is still what a brand new deployment starts from, and still
the only way to set these before the app has ever been logged into.

### Three models, three jobs, all at once

The three model fields are not alternatives. Ollama keeps more than one model
loaded, so all three can be different and all three can be in use in the same
minute — there is no switching, and nothing to turn off to use something else.

| Setting | Field in Admin | What uses it |
| --- | --- | --- |
| `OLLAMA_MODEL` | Assistant model | The assistant you talk to, and the model that writes link proposals |
| `OLLAMA_EXTRACT_MODEL` | Extraction model | Reading uploaded documents into proposed entities |
| `OLLAMA_EMBED_MODEL` | Embed model | Turning text into vectors for correlation |

They want genuinely different things. Extraction runs unattended on every
upload, nobody reads its prose, and the only thing that matters is whether it
returns usable JSON quickly — a small instruct model is often *better* here
than a large one, and much cheaper to run. The assistant is the opposite: it
is read by a person, one request at a time, and being a good conversationalist
is the whole job. Embedding is not a chat model at all; a chat model cannot
produce embeddings and a `nomic-embed-text` cannot hold a conversation.

So the arrangement the split exists for:

```
OLLAMA_MODEL=mistral
OLLAMA_EXTRACT_MODEL=llama3.2:3b
OLLAMA_EMBED_MODEL=nomic-embed-text
```

Leave **Extraction model** blank and extraction uses the assistant's model,
which is exactly what happened before this setting existed — so an instance
upgrading into this build behaves identically until someone chooses otherwise.

Each model is attributed separately in **Admin → Model Activity**, so "which
of these is actually costing me time" is a question with an answer rather than
a guess. If you run all three, pull all three: a model named here but not
installed is flagged on the Admin page, and the operation that needs it fails
quietly otherwise.

### Choosing a model without guessing

The Model and Embed model fields aren't free text you have to get exactly
right. The app asks the Ollama host what it actually has installed and turns
both fields into pickers, showing each model's parameter size and quantization
so they're distinguishable at a glance. Above them, a status line says whether
the host is reachable and how many models it found.

Three details that matter in practice:

- **The picker follows the URL you've typed, not the one you've saved.** Edit
  Base URL, hit **Check connection & refresh models**, and you're looking at
  the new host's models before committing to it.
- **You can still type a name.** Choose "Type a model name…" to enter
  something that isn't installed yet — useful when you're about to pull it, or
  when the host is temporarily down. If the host can't be reached at all, both
  fields quietly fall back to plain text boxes, exactly as they behaved before
  this existed.
- **A model that isn't installed is called out.** Saving a model the host
  doesn't have is allowed (you may be about to pull it), but the Admin page
  shows a warning, and so does the Review tab — where the symptom would
  otherwise just be a queue that never fills up. That warning is visible to
  analysts too, not only admins, since the person wondering why nothing has
  come through usually isn't the person who configured it.

### Pulling a model from the app

**Pull a model** on the same page downloads a model onto the Ollama host,
with live progress. Common starting points are one click away: `llama3.1:8b`
for extraction and the assistant, `nomic-embed-text` for correlation.

The download runs on the server, not in your browser, so it keeps going if
you navigate away or close the tab — come back to the Admin page and the
progress is still there. One pull runs at a time; asking for a second while
one is in flight is refused with a message naming the one already running.
When a pull finishes, the model appears in the pickers immediately without a
manual refresh.

Two limits worth knowing. Progress lives in the api container's memory, so
restarting that container loses the progress display — the download itself is
resumable (Ollama keeps the layers it already fetched), so re-running the pull
picks up roughly where it left off. And this assumes a single api process,
which is what the shipped `docker-compose.yml` starts; running multiple api
replicas would give each its own copy of that progress state.

Both of Ollama's jobs in this app write to a queue an analyst has to act
on — never straight into your case graph. That's not a corner cut for lack
of time; it's the actual design. An LLM extracting "this document mentions
a person named J. Smith who works at Acme" is a reasonable first pass, but
it is not the same thing as an analyst deciding that claim belongs in the
record. Two consequences of that:

- **Accepting an extracted relationship never defaults to "confirmed."**
  It lands as "possible," the most conservative confidence level, even if
  the model reported high confidence in its own wording. The model's
  confidence score measures how sure it is about what the text said, not
  whether an analyst has judged the claim itself to be true — those aren't
  the same scale, and mapping one onto the other would quietly launder an
  LLM guess into something that looks analyst-verified.
- **Confirming a correlation match still does not merge the two records** —
  but merging is now a separate button beside it, so that is a choice rather
  than a limitation. "These are the same subject" and "fold these into one
  record" are genuinely different answers: two records can describe the same
  person and still be worth keeping apart. See "Merging duplicates" below.

### Working a long queue

Eight uploaded documents produce one long list, so the queue filters four ways
and shows a count on each option — the useful question at the top of fifty
proposals is not "can I filter to people" but "how many people are waiting",
and a filter that turns out empty after you pick it is a wasted click.

- **Status** — pending, accepted, rejected.
- **Source** — document extraction, the signal pass, or the assistant. These
  fail in different ways, so looking at one at a time matters.
- **Entities or relationships**, and when you are on entities, **which kind of
  record**.
- **Which document** it came from, by the name you gave it. One document at a
  time is how the material arrived and how it reads.

**Entities come first in the queue by default**, and that is not cosmetic: a
relationship can only be accepted once the records at both ends exist, so a
list that interleaves them asks you to decide about a link before deciding
about its endpoints. Working straight down in this order means each
relationship arrives with its ends already matched — see the next section for
what that looks like.

### When a proposed name is not a name

Models reading correspondence propose email addresses as people. `From: Miles
Trombley <m.trombley@example>` is one person and one address, and the address
is the half that tends to come back as a Person record.

Two things address it. The extraction prompt now says explicitly that an email,
phone number, handle or domain is something a person *has* rather than what one
is *called*, and that an address with no name attached should be left out
rather than turned into an invented person. And because a prompt is a request
rather than a guarantee, the queue checks: an entity suggestion whose name
parses as an address, a phone number or a URL is flagged on its card with what
it actually looks like.

The flag advises, it does not block — you can still accept it, and sometimes
you should, because you know whose address it is and can rename it on the way
through. What it prevents is accepting one at speed while working down a long
list, which is exactly when it happens.

(If you are wondering whether the model does this because the person does not
exist yet: it cannot be that. Extraction is given the system prompt and the
document text and nothing else — it has no view of your case file at all, so it
cannot know what exists.)

### Accepting a suggestion that names records you don't have yet

A model reading a CV produces "Fenwick — employed_by → Kestrel Logistics". It
saw text, so it has two *names* and no records, and the relationship cannot be
stored until both ends exist. That used to mean leaving the queue, creating
Kestrel Logistics by hand on the Entities page, coming back, and searching for
it again — for a name the app was already showing you.

Each end of a relationship suggestion now settles itself as far as it can:

- **Already in the case file** — the record is matched for you and shown with
  its type. Matching is exact and case-insensitive, never fuzzy: a near-match
  silently attaching a relationship to the wrong person is far worse than one
  extra search.
- **Several records share the name** — you pick from them, right on the card.
- **Not in the case file** — the card offers to create it, with the type the
  relationship implies already selected (`employed_by` → organization,
  `located_at` → location, `child_of` → person). One click accepts the
  suggestion and makes the record, in a single transaction — if either half
  fails, neither happens.

Any end can be overridden with a search box, in case the record exists under a
different spelling.

Two consequences worth knowing. If the same document also proposed that name as
its own entity suggestion, creating it from the relationship card **closes that
suggestion out** and points it at the record just made, so you never decide
about the same record twice or end up with a duplicate. And accepting an entity
suggestion makes every relationship suggestion mentioning that name match it
automatically — so working an extracted document top-down, entities first, is
now the path of least resistance rather than a chore.

**Entity suggestions can have their type corrected before you accept.** A model
reading a CV routinely calls an employer a person. The type is a dropdown on
the card; changing it drops the details the model inferred for the old type,
since an "occupation" means nothing on an organization.

The extraction prompt was also tightened: every name a model uses in a
relationship must now also appear in its entities list, so the "named but never
proposed" case should be rare. The controls above exist because *should be
rare* is not the same as *cannot happen*.

## Documents: the inbox

Everywhere else in this app asks you to know the answer first. Creating an
entity asks who. Writing a report asks what happened. Attaching a file asks
which record it belongs to. But the job usually starts the other way round: a
PDF arrives, or a photographed noticeboard, or a forwarded message, and the
whole question is *who is in this*.

**Documents** is the tab for that. Drop a file in — or paste text straight in
if there is no file — and the worker OCRs it, extracts its text, and proposes
the entities it found. Nothing is filed anywhere and nothing is written to your
case graph; you get a document with its text and a list of proposals.

### Dropping something in

Drag files onto the drop zone, or click it to pick them. PDFs (including
scanned ones, which get OCR'd page by page), photographs of documents, Word
files, plain text and RTF. Multiple files at once are uploaded one after
another rather than in parallel, which on a Pi is the difference between
twenty documents and twenty timeouts, and each one's outcome is reported
separately so a single bad file does not read as "the whole drop failed".

The paste box beside it is for the case with no file at all: an intercepted
transmission read out over a radio, the body of a forwarded message, a
transcript someone dictated to you. Pasted text is written to disk as a `.txt`
and then treated identically — same queue, same extraction, same proposals, and
it downloads and backs up like anything else.

Both take an optional name and a note on where it came from. Neither is
required, and both can be set later: you often cannot usefully title a document
until you have read it. A name is worth adding eventually, because
`IMG_2291.png` is not findable in six weeks and "Intercept, 0340Z, channel 4"
is.

### Reading one

Opening a document shows **the extracted text on one side and everything
proposed from it on the other**. That layout is the point of the feature. The
question a reviewer actually has about a proposed name is "where does it say
that", and an answer that needs a second screen is an answer nobody checks.
Every proposed entity has a **Find in text** button that highlights it in the
extracted text — and if the name is nowhere in the document, that tells you
something important, so the app says so rather than doing nothing.

Accept a proposed entity and the record is created, right there — and its type
is a dropdown, so a model that called an employer a person is corrected before
the record exists rather than after. Dismiss it and it is gone.

Proposed *relationships* are accepted here too. They name records by name
rather than by id, since the model only ever saw text, so each end of the card
either matches a record you already have or offers to create one with the type
the relationship implies. See "Accepting a suggestion that names records you
don't have yet" above — the same controls, on the page that shows the document
those names came from.

Search covers titles, filenames, source notes **and the extracted text**,
because the thing you remember about a document six weeks later is a phrase
that was in it.

### What happens to it afterwards

Nothing, unless you want something to. A document can sit in the inbox
indefinitely.

**File under…** attaches it to a report or an entity after the fact. This is a
pointer change, not a copy: the file, its extracted text and every proposal
already made from it stay exactly where they are, and the document simply stops
appearing in the inbox and starts appearing in that record's attachments.
Nothing is re-read and nothing is stored twice.

**Archive** takes it out of the inbox and keeps it. A document whose proposals
are all reviewed has served its purpose, but it is still the evidence behind
whatever was accepted from it — so archiving hides it rather than deleting it,
and archived documents are one dropdown away. Deleting a file outright is still
possible for the genuinely-wrong-file case, from the attachment endpoint.

**Read again** re-runs extraction over a document, at any status. The case
that needs it most does not look like a failure at all: if Ollama is off or its
model is missing when a document is read, the text still extracts, the document
is marked **Read**, and it simply produces nothing — indistinguishable from a
document with nothing in it. Restricting this to failed documents left exactly
the ones worth re-running unreachable.

Undecided proposals from the previous pass are cleared first, so a re-read
replaces rather than duplicates. Anything already accepted or dismissed is
kept: those were decisions, and re-running is not a reason to ask again.

**Read empty ones again**, on the toolbar, does the batch — every document that
was read and produced nothing. That is precisely the shape an Ollama outage
leaves behind, and clicking through thirty of them one at a time is the kind of
chore that does not get done. Documents that *failed* are left out by default,
since an unreadable file will only fail again; a genuinely empty document will
be offered every time, because the app cannot tell "produced nothing because
the model was down" from "produced nothing because there is nothing in it".

### When nothing comes out

Two different outcomes that look alike and are not:

- **"Read" with nothing proposed** means the text was extracted and the model
  found nothing in it worth proposing — or there is no model. The banner at
  the top of the tab says which, in the same words as the Review tab, because
  Ollama switched off, unreachable, and pointed at an un-pulled extraction
  model all otherwise look identical from here: an empty list.
- **"Failed"** means extraction itself broke, and the reason is shown on the
  document — an unsupported file type, a corrupt PDF, a missing file on disk.

A photograph with no legible writing in it reads as "Read" with empty text,
which is the honest answer: OCR worked and found nothing.

## Merging duplicates

Extraction produces duplicates. The same person is named four ways across six
documents — "Miles Trombley", "M. Trombley", "Trombley, Miles" — and each one
becomes its own record. A single address arrives as a street, a town and a
postcode, because the model read them as three places.

**Merge** folds them into one. One record survives, everything that pointed at
the others now points at it, and the others are archived with a note saying
where they went.

### Two ways in

- **Review → Correlation.** A proposed match now offers **Merge into one**
  beside **Same, but keep both**. Those are different answers and both are
  worth having: two records can describe the same subject and still be worth
  keeping apart.
- **Entities → Select to merge.** Tick boxes appear on the cards; pick as many
  copies as you have and merge them in one pass. This is the one you want for
  five duplicates, since the correlation queue only pairs what it happened to
  notice, and five copies are ten pairs.

Selecting is off until you ask for it — a tick box on every card all the time
turns a browsing view into a management view.

### What happens

You choose which record survives; the dialog defaults to whichever is carrying
the most, since that is the one whose content is preserved outright. Then:

- **The survivor wins every field it has filled in.** Anything it left blank is
  filled from the others. It will never overwrite something the survivor
  already says, and anything it gets wrong is editable afterwards like any
  record.
- **Aliases are unioned rather than replaced**, and the names the duplicates
  were filed under become aliases too — "M. Trombley" and "Trombley, Miles" are
  exactly the alternative spellings you want to keep.
- **Relationships, report links, attachments and contacts all move.** A
  relationship *between* two records being merged is dropped, because a record
  cannot be related to itself; two records related to the same third party the
  same way collapse to one edge, keeping whichever carried more (a confirmed
  edge with notes beats a bare possible one).
- **The survivor can be renamed in the same step.** This is there for the split
  address: fold the three fragments together and call the result "1140 Rennard
  Way, Kettleburn 74101" without a second trip to the edit form.

- **Suggestions about the merged records collapse too.** Duplicates rarely come
  in pairs: three copies of one company get flagged against each other, so
  after the merge several queued suggestions all say the same thing. The pair
  that prompted the merge is marked confirmed — somebody dealt with it — and
  any other suggestion that has become a duplicate of one already naming the
  survivor is marked reviewed, leaving one live copy. "A works for Halvard"
  and "A works for Halvard Industrial Svcs" become one row, not two identical
  ones or a failed merge.

The dialog previews all of this before you commit — how many relationships and
report links will move, which blank fields will be filled, and how many edges
between the records will be dropped.

### Merging records of different kinds

Normally the kinds have to match, and for good reason: the survivor's kind is
what the merged record *becomes*, and quietly changing a Person into an
Organization is not something an interface should do behind your back.

But it happens. Extraction files the same company as both a Person and an
Organization, reading a signature block one way and a letterhead the other, and
then one of those two records is simply wrong. Refusing outright left you
rebuilding one of them by hand.

So the dialog opens either way, and crossing kinds is something you turn on:

1. **A checkbox** says how many kinds are in the selection and which, and until
   you tick it the merge is unavailable and no survivor is offered — the first
   question is which kind this is going to be, and offering a survivor before
   that is asking the second question first.
2. **A kind picker** appears when you do. Choosing a kind chooses the survivor,
   because those are the same choice: the survivor is what decides the merged
   record's kind.
3. **The preview names what will be lost**, field by field: *"Halvard
   Industrial Services (person) loses date of birth, occupation, physical
   description — an organization has nowhere to keep them"*. Every kind's
   fields live in its own table, so there is nowhere in `organization_details`
   to put a date of birth. Nothing else is affected: the name, relationships,
   report links, attachments, contacts and description all move as they always
   do, and the records of the same kind as the survivor still fill its blanks
   normally.

"Three fields will be lost" is not a decision anybody can make. Naming them is
the whole point of the warning.

The API keeps the old behaviour by default — a merge across kinds is refused
unless the request explicitly passes `allow_type_change`, so nothing can cross
kinds by accident — and the audit entry records which kind the record ended up
as and exactly which fields were discarded.

### What it does not do

**There is no one-click undo.** The records merged away are archived rather than
deleted and each carries a pointer to where it went, so nothing is destroyed and
the history that referenced them still resolves — but putting them back is a
manual job. Read the preview.

The merge is one transaction: if any part of it fails, none of it happened.

### Why the addresses split in the first place

The extraction prompt now says a place is one entity — that "1140 Rennard Way,
Kettleburn 74101" is a single location whose address is the whole string, not a
street, a town and a postcode as three — and that a bare postcode or a lone
town mentioned only as part of somebody's address is not a place worth its own
record. Merge is the cure for what is already in your file; that is the
prevention.

## Deleting for good

Everything else in this app archives. This does not, and it took a real
problem to justify it.

**The problem.** Extraction proposes an entity called "ATTACHMENT A" or
"Page 2 of 4", somebody accepts it at four in the morning, and it is in the
file forever — cluttering the tree, drawn in the network, proposed as a
correlation match against other junk. Archiving hides it from one list and
leaves every row in the database. Enough of those and the case file is
mostly debris, and the debris is what the correlation pass spends its time
matching against.

The archive-don't-delete rule was written for records that were *real* and
are now finished with. It was never a good answer for records that should
never have existed.

### How it is kept narrow

- **Admins only.** The buttons do not render for an analyst, and the API
  refuses them with a 403 regardless of what the page shows.
- **Previewed, always.** The dialog counts what goes — relationships, contact
  details, attachments and their files, queued and historical suggestions —
  and names the records and reports involved before anything happens.
- **Typed to confirm.** You type DELETE. Lower case does not count.
- **Refused when a confirmed report cites the record.** A confirmed report is
  a statement somebody stands behind, and quietly removing something it names
  breaks that. The dialog says which report, and suggests archiving or
  unlinking instead. A *draft* is only a warning: the draft keeps its text and
  loses the link.
- **All or nothing.** A batch containing one blocked record deletes none of
  it, rather than doing most of the job and reporting a problem.
- **Audited.** One entry per thing destroyed, recording what it was called,
  what went with it, and which reports had cited it. The audit log stores ids
  as text precisely so that history outlives the thing it describes — it is
  the only trace left.

### What actually had to be cleaned up

This is the part that makes it more than one `DELETE`, and the part that was
causing the mess in the first place. Most references cascade. Two do not:

- **`extraction_suggestions.resolved_entity_id`** is a plain foreign key with
  no `ON DELETE` clause. A naive delete does not orphan it — it fails
  outright with a foreign-key violation.
- **`correlation_suggestions.subject_a_id` and `subject_b_id`** are TEXT with
  no foreign key at all, because a subject can be an entity or a report.
  Nothing cascades, nothing complains, and the rows sit there forever pointing
  at something that no longer exists. **These are the orphans.**

Both are handled explicitly, and the test suite asserts that after a delete
nothing anywhere in the schema still points at the record.

One more edge: a record that others were **merged into**. `merged_into` is
`ON DELETE SET NULL`, which would quietly turn "merged away into X" into a
plain archived record with no explanation. Instead the merged-away records
keep their archived status and gain a line in their description saying the
record they went into has since been deleted, so the trail still reads.

### What it will not do

Deleting a **document** does not delete the records already accepted out of
it. Those are part of the case file now, and where they came from is not a
reason to destroy them — they lose the provenance row and nothing else.

There is no undo. Only a restore from a backup brings anything back, which is
the other reason [SOP 03](sop/03-admin.md) asks you to test your backups.

## Recording what the model missed

The model does not propose everything worth having. A reporter's byline, a
photographer's credit, a company named once in passing, a plate written into
the middle of a sentence. You notice these while **reading** the document —
and until now, acting on it meant leaving the page, opening the New Entity
form, typing the name from memory and navigating back. That is how details
get skipped.

So the document page can create records two ways:

- **Select a name in the text.** A button appears under the selection with the
  name in it. Pressing it opens the normal entity form with the name already
  filled in, and the sentence it was selected from is kept as evidence.
- **Press "+ Record something".** The same form, empty, for what the document
  implies but never spells out.

Either way you stay on the document afterwards, because there is usually more
than one, and the record appears immediately in a group on the right headed
"recorded by hand from this document".

### Provenance for a record a person made

A record made this way is an ordinary entity in every respect. What makes it
more than a shortcut to the New Entity form is that it carries an
`extraction_suggestions` row with source **`manual`**, already accepted,
pointing at both the document and the new record.

That matters because this app's one non-negotiable question is *where did
this come from*. Before, that question had an answer for everything a model
proposed and no answer at all for anything typed by hand. Now it has one for
both, the document lists what a person found in it alongside what the model
found, and **Review → From: Recorded by hand** finds every record made this
way.

The detail fields are validated strictly, exactly as the New Entity form
validates them — unlike accepting a model's proposal, where a bad field is
dropped rather than blocking an otherwise-good suggestion. A person typing a
bad date should be told, not have it silently discarded.

## Suggested links: "these two might be connected"

You enter your family as five separate Person records. Nothing in the app
knows they are a family — you told it five facts, not one. Two features close
that gap, and neither of them writes anything.

Both land in the same place: **Review → Extraction**, alongside the
suggestions pulled out of documents. There is one queue because there is one
question — *something proposed this; do you agree?* — and a **source** filter
to look at one kind at a time.

### The signal pass (no model required)

A periodic pass over records you have already entered, looking for pairs that
look connected but have no edge between them. Five rules, each with a reason
you can check:

| Rule | Proposes | Confidence | Fires when |
| --- | --- | --- | --- |
| Same recorded parent | `sibling_of` | 0.80 | Two people are both recorded `child_of` the same person |
| Shared contact detail | `associate_of` | 0.70 | Two to five records share a normalised phone number, email, address, messaging handle or social account |
| Same radio/web channel | `associate_of` | 0.35 | The same, but for a Radio, Website or Other contact — a channel anyone can tune to, not a number you have to be given |
| Named together in reports | `associate_of` | 0.50 | Two people are mentioned in three or more of the same reports |
| Shared surname | `family_of` | 0.45 | Two to six people share an uncommon-in-this-file surname and differ in given name |
| Recorded at the same place | `associate_of` | 0.40 | Two people are both `located_at` the same Location record |

This is SQL over your own data. **It needs no model at all** and works with
Ollama switched off entirely — which also means it is the half of this feature
that still works on a Raspberry Pi with nothing installed.

Four things keep it from becoming noise:

- **One card per pair, not one per rule.** Three rules firing on the same two
  people is a stronger reason to look, not three things to click. The pair
  takes the relationship type of its strongest rule and carries every rule's
  evidence, so nothing is lost by merging.
- **Pairs already connected are skipped**, in either direction.
- **Pairs you dismissed are not raised again.**
- **It stops at `LINK_SIGNALS_MAX_PENDING`** (60) waiting suggestions and
  picks up as you clear them.

Two rules earn their low scores by being the ones that fire on coincidence.
**Shared surname** is capped at six people, requires the given names to differ,
and requires a full name rather than a single word — and will still
occasionally propose that two unrelated Nguyens are family. That is the correct
behaviour for a 0.45: it is a question, and the answer is often no.

**A shared radio frequency is not a shared phone number**, which is why it is
scored below even that. Two people reachable on the same mobile share a thing
they had to be given; two people listening on 146.520 share a national calling
frequency, which in a case file full of standard service channels is nearly
everyone. Both are still raised — "these two work the same net" is worth
knowing — but the channel kind sits at the bottom of the queue and says so on
the card. Websites and unclassified "Other" contacts are treated the same way.

The same instinct is why **co-mention is people-only**. Unscoped, that rule
measures how reports are written rather than anything about the world: a report
names the place it happened and the groups involved, so in a busy file every
location co-occurs with every organization operating there. Worse, two
organizations fighting each other appear together constantly, and the rule
would read open conflict as association. On the contested-metro sample,
scoping it to people took the queue from 60 proposals to 22 and removed every
"this highway interchange is an associate of this militia".

**It is off by default.** Set `LINK_SIGNALS_ENABLED=true` to turn it on.
Turning it on against an established case file produces a burst of proposals
about records entered months ago, all at once; that should be a decision you
make on a quiet afternoon, not a surprise after an update.

### Asking the assistant

**Review → Extraction → "Ask the assistant to propose links"** takes an
instruction in your own words — *"link the Smiths"*, *"connect everyone at the
rail yard"* — retrieves the records it is about, and asks the model what it
thinks connects them.

What it does with the answer is the important part:

- **It writes proposals, never relationships.** The model has no path to the
  case graph. Accepting a proposal runs the same validation your own typing
  goes through.
- **Ids it invents are discarded.** The model is given a list of records and
  told to copy ids from it; anything referring to a record that was not on
  that list is dropped before it reaches the queue. A model that hallucinates
  a sixth family member produces nothing rather than a proposal about a person
  who does not exist.
- **It only proposes relationships** — never new entities, never edits to
  existing ones. A model inventing a Person from a chat instruction is a much
  worse failure than it guessing wrong about a connection between two records
  you created yourself.
- **A pair already in the queue gains its reasoning rather than a second
  card.** When the signal pass and the assistant reach the same two people,
  that is one decision, and you see it once with both sets of evidence.
- **Every proposal keeps the instruction that produced it**, the model's own
  stated reason, and which model wrote it.

Retrieval handles the two phrasings people actually use. A plural family name
(*"the Smiths"*) matches the singular records, and naming one person pulls in
their household — everyone sharing that person's surname or a contact detail
with them — so *"connect Jane Smith's household"* has something to work with
rather than one record and nothing to connect it to.

If it finds nothing it says so, and adds nothing. "The model didn't find
anything it could support" is a real answer, not a failure.

The cost of proposing rather than applying is real: asking it to link forty
people produces forty things to click. It is still the right trade. You can
audit what you accepted; you cannot easily audit what a model quietly wrote
last Tuesday.

### Reviewing them

A suggestion from either source already knows which two records it means, so
its card is a decision rather than a form — no re-identifying records the
engine had already identified. Each card shows:

- **Where it came from**, as a label. "A rule matched two records you entered"
  and "a language model read a document" deserve different amounts of trust.
- **Why, in one line** — *"both recorded as a child of the same person"* — with
  a **Why** disclosure carrying the particulars: which phone number, which
  parent, how many shared reports, what you asked the assistant.
- **Both names, linked** to the records themselves.
- **The relationship type, editable.** A rule's guess at the *type* is the
  weakest thing about it — "these two are connected" is a much safer claim
  than "she is his sister" — so the type is a dropdown, not a label. Accept
  writes the type you chose.

**Dismiss** records the decision and stops that pair coming back.

## Audit trail

Account menu → **Audit log**, admin-only, on a page of its own — it's a long
paginated list, and living inside Admin settings it pushed everything below it
off the screen. Admin settings keeps a link across to it. Every entry records
who did it, what they did it to, when, from which IP, and with what user
agent.

### What is recorded, and what isn't

**Every change**: logins and failed logins, logouts, account creation and role
changes, entity creates/edits/archives, relationships, bulk imports, report
creates/edits/confirmations, attachment uploads and deletions, extraction and
correlation decisions, RSS feed changes, Ollama settings changes, model pulls,
and restores.

**Plus the reads that move data out of the app** — the ones where a copy
leaves and you can no longer control it:

| Event | Why it's in here |
|---|---|
| Attachment download or preview | A photo or scanned document left the system. Previews count: the same bytes reach the same person either way. |
| Report PDF export | A report *and* every linked entity's dossier left as one file. The entities included are listed in the entry. |
| Dossier export | A record, everything one step from it, and every report mentioning it. The entry names the shape taken and every neighbour and report inside. |
| Hotspot export | A location and every record behind its hotspot label. The entry records the window and what was included. |
| Executive summary export | A period's work, attributed to named analysts. The entry lists which names were in it. |
| Backup download | Every record, every attachment, and every password hash, in one file. |
| Audit log CSV export | The trail itself leaving. |
| AI Assistant query | Case data read on the user's behalf. The entry lists which records were retrieved; the question text is not copied here — it's already in that user's own conversation. |

Ollama calls are *not* in the audit log. They are recorded separately in
`ollama_calls` (see "Model activity") because they answer a different question:
the audit trail is about what people did to the case file, and a model call is
about what a machine spent doing it. The assistant query that *caused* the call
is audited, which is the part a person did.

**Ordinary browsing is not recorded.** Opening an entity, running a search,
scrolling the Reports list — none of that lands in the log. That's a
deliberate trade: logging every view on a tool used all day produces a table
that grows fast and buries the events that matter under noise. It also means
this log **cannot** answer "who has looked at this person's file" — only "who
took a copy of something." If you need the former, say so and it's a change to
make, not a limitation to work around.

Edits record **which fields changed, not their new values**. An audit trail
that captured values would quietly become a second copy of the case data, with
none of the access control the real records have. The exceptions are
deliberate and non-case-data: role changes and Ollama settings record what
they were set to, because that value *is* the security-relevant fact.

Failed logins record the attempted username. No password is recorded in any
form, ever.

### Reading it

The log is filterable by user, action, object type and outcome, with free-text
search over record names and ids, and paginates newest-first. The filter bar
stays put as the page scrolls. Two kinds of row are colour-marked so they're
findable at a glance: **egress** events (amber) and **failures/denials** (red).
**Export CSV** hands the current filtered view to someone outside the app — and
records that it did.

Arriving at the page always shows the newest events: your filters are kept, but
the page offset is reset, so opening **Audit log** never drops you onto page 5
of a listing you were reading half an hour ago.

### Forwarding to syslog

This is the part that matters if the trail needs to mean anything to an
outside party.

The audit table lives in the same database as the case data, on the same
machine, owned by the same Postgres role the app connects as. An admin with
shell access can edit it. Restoring an old backup replaces it. Neither of
those can be engineered away from inside a self-hosted app — so the answer
isn't to pretend otherwise, it's to get a copy off the box as events happen:

```
AUDIT_SYSLOG_ENABLED=true
AUDIT_SYSLOG_HOST=192.168.1.20
AUDIT_SYSLOG_PORT=514
AUDIT_SYSLOG_PROTOCOL=udp      # or tcp
AUDIT_SYSLOG_FACILITY=local0
AUDIT_SYSLOG_APP_NAME=humint-platform
```

Each event goes out as one syslog message whose body is a single line of JSON,
so a collector can parse it without a custom grok pattern. The audit page
shows whether forwarding is actually running and where to, so "is this on?" is
answerable without reading container logs.

**Forwarding can never break or slow down the app.** Messages go onto a queue
drained by a background thread, so a TCP collector that accepts a connection
and then stalls doesn't turn into a hung request. If the collector can't keep
up, events are dropped rather than queued without limit — and the count of
drops is shown on the audit page, because a silent gap in an audit trail is
worse than a visible one. Everything dropped is still in the database.

The same principle applies to the database write: if it fails (most likely
cause — running against a database that hasn't had the migration applied), the
reason goes to the api container's logs and the user's actual request carries
on. An audit trail that can take the application down with it is one that gets
switched off.

### Retention

`AUDIT_RETENTION_DAYS` defaults to `0`, meaning keep everything forever. Set a
number of days only if you have a policy that requires one; the worker then
prunes older entries at most once an hour. That prune is the only thing
anywhere in the app that deletes from this table, and it can only remove
entries by age — there is no endpoint, anywhere, to delete or edit a specific
entry.

### Two honest gaps

1. **A restore replaces the audit trail with the backup's.** Restoring is
   "make this instance identical to that backup", and the trail is part of the
   instance. The restore itself is recorded immediately afterward, so the log
   shows the seam — but recent entries that postdate the backup are gone.
   Syslog forwarding is what preserves them.
2. **An admin with database access can alter the table directly.** True of any
   self-hosted application, and again the reason forwarding exists. If you need
   a trail that survives a hostile local admin, forward it somewhere that admin
   doesn't control.

### Behind a proxy

The recorded IP is the connecting socket's address. If this app sits behind a
reverse proxy or tunnel, that's the proxy's address — set
`TRUST_PROXY_HEADERS=true` to use `X-Forwarded-For` instead. Leave it off
otherwise: that header is trivially forged by anyone able to reach the app
directly, and trusting it without a proxy in front would let a caller write
whatever address they liked into your audit log.

## Backup and restore

**Admin page → Backup & Restore.** Admin-only, both directions.

**Download backup** produces a single `.zip` — `humint-backup-<date>-<time>.zip`
— containing every record in the app plus every uploaded attachment. Inside
it: `data.json` (all the data, human-readable if you ever want to look),
`manifest.json` (what's in it, when it was taken, by whom), and `uploads/`
(the actual document and image files). Everything is covered: entities and
all their per-type details, relationships, reports and their entity links,
attachments, both review queues, RSS feeds and their dedup history, Ollama
settings, AI Assistant conversations, and user accounts.

The table list isn't hardcoded — it's read from the database at backup time,
so a table added by a future feature is included automatically rather than
being silently left out of everyone's backups until someone notices.

**What isn't in it:** active login sessions, and nothing else. Those are
deliberately excluded in both directions — a restored `users` table
underneath live session tokens would leave people holding valid cookies for
user ids that now belong to someone else entirely. So everyone gets logged
out by a restore, including whoever performed it.

### Restoring

Pick a backup file, click **Restore from this file…**, and type
`REPLACE ALL DATA` to confirm.

**Restore replaces everything.** It is not a merge and not an import — the
instance ends up holding exactly what the backup holds, and whatever was
there before is gone, including attachment files that nothing in the backup
references (leaving another case's scanned documents sitting in the uploads
volume after a "full replacement" would be the wrong default for a tool that
holds photos and documents about real people). There is no undo. If there's
any chance you want what's currently in there, take a backup first — that's
the whole point of the button right above it.

What makes that safe to offer at all is that the database half runs in a
single transaction: everything is wiped, restored, and committed as one
operation, so **a restore either fully succeeds or changes nothing at all.**
A corrupt zip, a backup from a newer version of the app, a row the schema
rejects, even the connection dropping partway — all of them leave the
instance exactly as it was, and say so. There is no half-restored state to
dig yourself out of.

**A backup that has been re-compressed still restores.** A backup this app
writes has `data.json` and `manifest.json` at the top level of the zip, with
attachments under `uploads/`. Download it on a desktop that expands archives
automatically, zip the resulting folder back up, and every one of those names
gains a wrapper directory — same bytes, same backup, and the old check
("is there a `data.json` at the top level?") refused it. Restore now looks for
the one `data.json`, at the top level or one directory down, and reads
`uploads/` relative to wherever it found it. A zip holding *two* backups is
still refused: there is no way to guess which one was meant.

**And a zip that isn't a backup says what it is.** "No data.json inside that
zip" sent an operator looking for a fault in a file that was fine, when the
answer was nearly always that the wrong zip got picked. The message now lists
what the zip actually contains, which usually identifies it on sight.

The backup is validated before anything is touched: it has to be a real zip,
from this app, in a format version this build understands, containing only
tables and columns this database actually has. That last check is what stops
a backup from a *newer* build restoring here and silently dropping the
columns this schema doesn't know about yet — upgrade first, then restore. The
reverse is fine and supported: an *older* backup restores cleanly, and any
table it predates simply ends up empty (the confirmation summary tells you
which).

### Moving to another machine

1. On the old instance: **Download backup**.
2. On the new machine: install as in "Getting started" (including a `.env`
   and any migrations listed under "Upgrading an existing deployment"), then
   create the first admin account when prompted.
3. Log in as that admin, go to Admin → Backup & Restore, and restore the
   file.
4. Log back in with an account from the old instance — the bootstrap admin
   you just made in step 2 is replaced along with everything else, and
   everyone's existing passwords work, since password hashes come across in
   the backup.

`MAX_RESTORE_MB` in `.env` (2048 by default) caps the size of a backup being
uploaded. It's deliberately separate from `MAX_UPLOAD_MB`, which caps a single
attached document — raise it if a legitimate backup is rejected for size.

### Treat the file like the database itself

A backup contains everything the app knows, including bcrypt password hashes
for every account, every report, and every attached photo and document. It is
exactly as sensitive as the Postgres volume it came from, and unlike that
volume it's a single file that's easy to leave in a Downloads folder or a
cloud drive. Store it accordingly, and prefer somewhere encrypted.

## Sample data

Three sample case files ship alongside the platform as ordinary backup zips.
Each one restores through **Admin → Backup & Restore** the same way any backup
does, and each **replaces everything currently in the instance** — so restore a
sample into a scratch deployment, never into a live one.

| File | Scenario |
|---|---|
| `humint-sample-kestrel-bay.zip` | Local-area threat monitoring in an invented coastal town. |
| `humint-sample-insider-threat.zip` | A corporate insider-threat investigation. |
| `humint-sample-contested-metro.zip` | A contested metropolitan area with no functioning national government. |

All three log in with `demo` (administrator) and `analyst` (not an
administrator), password `humint-demo-2026`. Log in as each — the role boundary
is one of the things the samples are there to show.

Everything in all three is invented: no real people, no real organisations, no
real incidents. The contested-metro file uses real public buildings in Oklahoma
City as its geography — a capitol, a courthouse, an airport, hospitals, a
fairground, a water plant — because that is the civic infrastructure any
continuity exercise turns on, and it contains no targeting, no methods and no
assessment of who is right.

Each file opens with a **"Read me first"** report explaining what to look at.
They are built to exercise the awkward cases rather than the tidy ones: a
hotspot that only appears when five unremarkable movement notes are taken
together, an incident with three reports where the second one is wrong and the
third withdraws it, people whose status fields are deliberately left blank, and
drafts nobody ever finished.

All three also arrive with something in the **Documents** inbox, since a tab
that is empty on arrival teaches the wrong thing about what it is for. Each has
four: one read with proposals waiting, one pasted in from a transcript rather
than uploaded, one still queued to be read, and one that failed with the reason
shown — so every state that tab can display is visible without you having to
produce a broken file yourself. If you want to see what a panel does when the data
is realistic rather than convenient, these are the files for it.

Restoring an older sample into a newer build is fine — the samples predate
several schema changes and restore cleanly regardless, because a missing column
falls back to its default.

## AI Assistant

An expandable side panel — the tab on the right edge of the screen, labeled
**AI Assistant** — reachable from every view, not just the Dashboard. Ask it
things like "what do we know about John Smith," "any reports mentioning the
warehouse," or "how is Acme Corp connected to anyone in this case," and it
answers from your own case data rather than general knowledge.

**How it decides what's relevant.** Every question triggers a fresh, small
retrieval step before it ever reaches Ollama:

1. If an embedding model is configured (`OLLAMA_EMBED_MODEL` — see "Which
   model, and how it's configured" above), the question itself is embedded
   and compared against every entity/report the correlation feature has
   already embedded, by similarity. This is the good path: it can surface a
   relevant record even when your question doesn't share exact words with
   it.
2. Otherwise (or if that turns up nothing), it falls back to matching the
   meaningful words in your question against entity names/descriptions and
   report titles/bodies directly. Cruder, but it means the assistant still
   works with only a chat model configured and no separate embedding model
   ever pulled — a very common setup, since embeddings are an optional
   second model pull extraction doesn't need.

Whichever entities/reports come out of that (up to 5 entities and 4 reports
per question), it builds a short dossier for each — the same
details/relationships/linked-reports you'd see by opening that record
yourself — hands that to Ollama as context alongside your question and the
last few turns of conversation, and that's the *only* information the model
is allowed to answer from. It's explicitly instructed to say "I don't see
that in the case file" rather than guess when nothing relevant turns up, and
every reply that did use case data shows small citation chips underneath it
— click one to jump straight to that entity or report.

**This is a private research aid, not a shared case log.** Each analyst has
their own conversation with the assistant; nobody else, including other
admins, can see it. **Clear** at the top of the panel wipes your own history
permanently — there's no undo, and no admin override to recover it.

**Nothing here writes to your case graph.** The assistant only ever reads
entities/reports/relationships to answer a question and writes to its own
conversation log — never to entities, relationships, or reports. Treat any
answer the same way you'd treat a colleague's off-the-cuff recollection of
the case file: a useful starting point, not a substitute for opening the
actual record.

**If it's unavailable**, the panel shows the reason inline instead of
inventing an answer: Ollama disabled entirely (Admin → Ollama Settings), or
unreachable/the configured model isn't pulled. Neither failure saves
anything to your conversation, so retrying a question never leaves a broken,
unanswered entry behind.

## Model activity

**Admin settings → Model activity** shows what Ollama has actually been doing.
One row is recorded per call, from both the api and the worker.

**This is not a bill.** A self-hosted model costs nothing per token. What it
costs is time on one machine that everything else queues behind, so the
headline figure on this panel is the share of calls that *failed*, not a token
count. Tokens appear mostly as the denominator that turns a duration into a
rate you can compare between two models.

Three questions, in the order people actually ask them:

**Is it working at all?** The failure rate, split into failures and timeouts,
with the last ten problems listed in full including the error text. This is the
one worth having: a model that has quietly started timing out on every
extraction looks *exactly* like an empty queue from the outside, and without
this there is nothing to tell those apart.

**What is it being spent on?** Volume by feature — document extraction,
correlation embeddings, assistant chat — and by account. Contention on one GPU
is real, and "the extraction queue hasn't moved all afternoon" and "someone has
been chatting to the assistant all afternoon" are frequently the same fact.
Only the assistant runs on behalf of a named person; extraction and correlation
show as *(background work)* rather than being attributed to whoever happened to
upload the document, since that person didn't ask for them.

**Is this box keeping up?** Median and 95th-percentile call time, tokens per
second, and cold starts counted separately. That last one matters on small
hardware: a slow call because the model had to be loaded from disk and a slow
call because the model is too big for this machine are different problems with
different fixes, and Ollama reports them separately so this does too.

Everything is a **median**, not a mean. One cold start with a forty-second
model load drags a mean into uselessness, and the question is "what does this
normally do".

Where a number was never reported it shows as a dash, not a zero. Ollama's
embeddings endpoint returns no token counts at all, and a row of zeroes there
would read as "this was free" rather than "this was never measured".

**What is not recorded:** no prompt text, no reply text, no document content.
The table holds timings and outcomes. A copy of every question anyone asked the
assistant would be a second, unaudited copy of the case file sitting in a
diagnostics table — the assistant's own conversation rows already hold that
text, scoped to the person who wrote it.

Records are kept for `OLLAMA_USAGE_RETENTION_DAYS` (30 by default) and then
deleted by the worker. Unlike the audit log, which defaults to keeping
everything, this has a real default: these are diagnostics that accrue a row on
every extraction, and a year of them buys nothing a month does not.

Recording a call can never break the call. The recorder swallows its own
errors, and so does the client that calls it — an extraction that succeeded and
then failed to write its telemetry row has still succeeded, and the right
outcome is to lose the row.

The panel is admin-only, because it names accounts and because "should we be
running a smaller model on this hardware" is a question for whoever chooses the
model.

## RSS ingestion: Events are ingested, Reports are analyst-generated

This app draws a hard line between the two: **an Event is information that
came in from a feed you're monitoring; a Report is something an analyst
wrote.** RSS ingestion only ever produces Events, never Reports — the two
should stay easy to tell apart at a glance in a case file.

- **No feeds ship built in.** The feed list starts completely empty. Add
  the ones that matter for your own casework — local news for wherever
  you're working a case, feeds for other regions where you're tracking
  something remotely, whatever RSS/Atom URL is relevant. Admin page → RSS
  Feeds → **+ Feed**, a URL and a label.
- **Every active feed is polled on its own clock**, independent of the
  extraction/correlation polling described above, and independent of
  whether Ollama is configured at all — there's no LLM step in taking a
  feed item at face value. Default is every 15 minutes per feed; change it
  with `RSS_POLL_INTERVAL_SECONDS` in `.env`.
- **A new item becomes a plain Event entity directly** — title, a cleaned
  version of the feed's summary/description (plus the item's link) as the
  description, and its published time as the Event's start time. It is
  **not** routed through the extraction review queue. That queue exists to
  gate an LLM's guess at what a scanned document might say; an RSS item is
  deterministic, already-published text from a feed you chose and
  configured yourself — you already vetted the source when you added the
  feed. The Event this creates is completely ordinary afterward: edit it,
  archive it, or link it into relationships and reports exactly like any
  Event you'd have entered by hand. Its Event Type field is set to `RSS:
  <feed label>` so you can tell at a glance where it came from without a
  separate "source" field.
- **Re-polling never creates duplicate Events.** Each feed item's own guid
  (or its link, if a feed omits one) is tracked so the same article is
  never ingested twice, even across restarts.
- **The two stated use cases this was built for:**
  1. *Event first, then a Report.* An Event gets ingested from a feed; an
     analyst later writes a Report and links it to that Event (or to the
     entities the Event concerns) to add supplemental information the feed
     item didn't have. This already works with the ordinary report-linking
     UI — nothing RSS-specific was needed for it.
  2. *Report first, then a matching Event.* An analyst writes a Report
     about something before it's ever publicly reported. Days later, an
     RSS feed ingests a news item covering the same thing as an Event. If
     an embedding model is configured (see "Getting started" above),
     correlation now compares Reports against Event entities specifically
     — not just against other Reports — and flags a likely match into the
     Correlation Queue for the analyst to notice and link, in either time
     order. This is the one genuinely new correlation capability added for
     RSS ingestion; every other comparison in this app still only ever
     compares same-type records against each other (see "Extraction and
     correlation" above).
- **A feed that fails to fetch or parse doesn't stop the others.** Its most
  recent error shows next to it on the Admin page and clears on the next
  successful poll. Pause a feed instead of deleting it if you want polling
  to stop temporarily without losing its ingestion history; deleting a feed
  stops polling and removes its dedup tracking, but leaves any Events it
  already created untouched.

## Security posture

- Sessions are opaque random tokens in Postgres, not JWTs — logging
  someone out (or an admin disabling an account) is just deleting/expiring
  a row, no token-blocklist bookkeeping needed. `SESSION_LIFETIME_HOURS`
  (12 by default) controls how long one lasts without activity. The
  frontend notices a session going invalid — expiring, being revoked, the
  account getting deactivated — the next time it makes any API call after
  that happens (at most every 30 seconds, via the same background check
  that refreshes the queue-count badges) and sends you back to the login
  screen with an explicit "Your session has expired" message, rather than
  leaving already-rendered views looking normal while newer actions quietly
  fail.
- Passwords are hashed with bcrypt. Five wrong attempts locks the account
  for 15 minutes.
- The session cookie defaults to **not** requiring HTTPS
  (`SESSION_COOKIE_SECURE=false`), because a LAN-only deployment over plain
  HTTP is the common case for something like this, and a `Secure` cookie
  would just silently never get sent back by the browser. If you put this
  behind a reverse proxy or something like Tailscale with real TLS, set
  `SESSION_COOKIE_SECURE=true` in `.env`.
- This app holds real names, photos, and reports on real people — treat it
  accordingly. Don't expose it to the open internet without putting real
  authentication/TLS in front of it beyond what's built in here, and back up
  regularly. Admin → Backup & Restore does that from inside the app,
  attachments included (see "Backup and restore"), but it's manual — for
  something that runs on a schedule, a cron'd `docker compose exec db pg_dump
  -U <user> <db> > backup.sql` alongside a copy of the `uploads` volume is
  still the right tool. The `.gitignore` already excludes a `backups/` folder
  if you want to keep dumps next to the project without committing them.
- **The audit trail is only as trustworthy as where it's stored.** Locally
  it's a table an admin with database access can edit, and a backup restore
  replaces it. Turn on syslog forwarding (see "Audit trail") if it needs to
  mean anything to someone who doesn't already trust whoever runs the server.
  Note also that it records changes and data-egress reads, not ordinary
  viewing — it can tell you who exported a report, not who read one.
- **A backup file is as sensitive as the database.** It contains every
  report, every attached photo and document, and every account's bcrypt
  password hash. That's what makes restoring onto a new machine work without
  re-issuing everyone's credentials, and it's also why a downloaded backup
  shouldn't sit around in a Downloads folder or sync to a cloud drive
  unencrypted.

## Arranging the network by hand

The layout used to run 220 cooling ticks and stop dead. That is the right
default — a graph that jiggles forever costs a CPU core for nothing — but it
left an analyst with whatever arrangement the simulation happened to find, and
no way to pull two records apart to see what connects them.

Nodes can now be dragged, and the picture keeps a little life after it settles.

**Three gears.** The loop runs the cooling simulation until it settles, then a
short burst of real simulation (`WAKE_FRAMES`) after any drag so the
neighbours make room, then an idle drift: one pass over the nodes moving each
a pixel or so around where it came to rest, at about 18fps. The idle pass is
O(n); the repulsion pass it replaces is O(n²), which is why leaving the real
simulation running was never an option.

**The drift knows when to stop.** It pauses when the tab is hidden, when the
canvas scrolls out of view (`IntersectionObserver`), above 400 nodes, and for
anyone whose system asks for reduced motion. Parked means the loop exits
entirely rather than spinning on a no-op.

**The fit is frozen once it settles.** `transform()` used to run every frame,
so the whole picture crept as nodes moved and lurched when a dragged node
changed the bounding box. It is computed once on settling and held until a
refit, a resize or new data. A drag is clamped to the canvas for the same
reason: the fit is not going to grow to find a node dropped outside it.

**A dragged node is pinned.** It keeps pushing and pulling on everything else
and stops moving itself, drawn with a dashed amber ring so it is obvious which
parts of the picture are being held. Double-click releases one; **Refit**
releases all of them and re-runs the layout.

**Click, drag and double-click on one dot.** A press that moves more than 3px
is a drag and never opens anything. A press that does not move waits 260ms
before acting, because the first half of a double-click is indistinguishable
from a single one, and double-click is how a node is released.

**One instance, many canvases.** The detail page replaces its canvas on every
record, so `wire()` runs repeatedly against one instance: the
`visibilitychange` listener is attached once and the previous
`IntersectionObserver` is disconnected before a new one is made.

## A click answers "who is this?" without leaving the page

A click on a dot used to open that record. Reading a network is a sequence of
"who is that one?" questions, and answering each of them by navigating away
threw out the arrangement the analyst had just made by hand and cost two page
loads to get back to it.

A click now opens a small card beside the dot: type, name, alignment, the two
or three fields that identify that kind of record — aliases and occupation for
a person, kind and date for a Record, plate for a vehicle — how many links,
reports and documents it has, and up to four of its connections with the
confidence on each. **Open record** is still one click away and says so.

**Which fields.** `summaryFacts()` holds one short list per entity type. It is
deliberately not the full detail table: a card that repeats the record page is
a slower record page. Confirmed relationships sort first, because a card has
room for four and those are the four worth showing.

**No new endpoint.** `GET /api/entities/{id}` already returns details,
relationships with the other end's name and alignment, contacts, reports and
attachments. The card is a second view of a response the record page was
already using.

**Closing it.** Escape, a click anywhere outside it, a scroll, or a resize. It
is `position: fixed` and placed against the viewport, clamped 8px inside every
edge, so at phone width it lands on screen rather than half off it.

**A connection on the card opens that record's card**, which is how a chain of
"and who is that?" is followed without ever leaving the picture. That button
stops its own click from propagating: without it the click reaches the
document handler *after* the new card has replaced the old one, and closes the
card it just opened.

**Both networks use it** — the Entities tab and the one on a record's own
page — with the record you are already looking at excluded on the latter.

## Closing a dialog without losing what was typed

A form that closes when you click beside it is fine until the form is twenty
fields of an entity somebody is halfway through. Two gestures were throwing
that work away:

- **One stray click outside the box.** The old handler closed on any click
  whose target was the backdrop.
- **Selecting text.** Drag from inside the box and release outside it, and the
  browser fires `click` on the nearest common ancestor of the press and the
  release — the backdrop. Highlighting a name to retype it closed the form.

The rule now is that the press and the release both have to land on the
backdrop, and if anything has been typed the dialog asks before closing.
Dirtiness is one flag set by the first `input` or `change` event inside the
dialog and cleared when one opens or closes, so an untouched dialog still
closes on a single click outside — which is what that gesture is for. Escape
goes through the same check rather than closing outright.

## Right-click actions on a record

Everything the app could do about one record used to live somewhere else: the
assistant panel on the right, the Review queue on another tab, the merge picker
behind a mode switch. Right-clicking a record's name — in the tree, on a card,
on a node in the network, in a map popup, in a report's linked list — now opens
a menu of the things worth doing to it, and the record page carries the same
menu behind an **Actions** button for anyone who does not think to try a
right-click.

Four items need a model. They are shown greyed out with the reason on them when
Ollama is off, rather than hidden, because a menu that changes shape depending
on server configuration teaches people the feature is unreliable.

| Item | What it does |
|---|---|
| **Look for links** | `POST /api/suggestions/propose` with the new `focus_entity_id`. Proposals land in Review; nothing is written to the graph. |
| **Ask the assistant about this** | Opens the panel with "What do we know about X?" already sent. |
| **What is missing on this record?** | The same panel, asking what the file does not answer and what to check next. |
| **Find similar records** | `GET /api/entities/{id}/similar` — cosine distance over the embeddings the worker already stores. |
| Add a relationship, Write a report about this, Select to merge, Export a dossier, Show on the map, Copy name / record ID | No model involved. |

### Decisions worth recording

- **`focus_entity_id` changes what retrieval runs on.** Without it the propose
  endpoint embeds the analyst's sentence; from the menu, the sentence is
  boilerplate wrapped around a name, so retrieval runs on the record's own text
  and the record is pinned into the candidate set. Otherwise "what is this
  connected to?" could return a candidate list that does not contain the record
  it was asked about.
- **Similar is not duplicate.** The floor is 0.55, well below the worker's 0.88
  for flagging a duplicate. This list answers "what reads like this", which is
  the question an analyst has when they suspect an alias or a second file on
  the same person. The score is shown so they can judge it.
- **A pair already ruled on is marked, not hidden.** The list says "in the
  review queue" or "dismissed before" against a pair rather than silently
  dropping it, so the absence of a Flag button has a visible reason.
- **Flagging a duplicate writes a suggestion, never a merge.** It inserts the
  same `correlation_suggestions` row the worker's pass writes, stored with the
  pair in sorted order so the unique index catches the same pair flagged from
  either side. Merging stays behind the existing confirm-and-preview dialog.
- **A record with no embedding still works.** If the worker has not reached it
  yet, one embedding is computed for the question and thrown away. The stored
  copy remains the worker's job.
- **Copying works without the clipboard API.** A LAN deployment on plain HTTP
  is not a secure context, so `navigator.clipboard` is missing there; the
  fallback is a hidden textarea and `execCommand`.

## Records: a document as an entity

Some documents are facts in their own right. The Magnolia Mutual letter in the
Blue Levee sample is the case in point: what matters isn't only the people it
names, but that the letter exists, when it was sent, and who it went to. As an
attachment it was only reachable from the inbox or from whichever single entity
it was filed under.

A **Record** is an eighth entity type for that. It holds a kind, the date on
the document, who issued it, and the full text, and it takes part in
relationships like anything else, so it appears on the network and on the page
of everyone it's linked to.

**Save as Record** on a document's page (`POST /api/documents/{id}/to-record`)
does the whole conversion in one transaction:

- creates the Record, with the document's extracted text as its body unless
  the form sends an edited body;
- links every ticked entity with `mentioned_in` (inverse `mentions`). The form
  pre-ticks what came out of the document: accepted entity suggestions, both
  ends of accepted relationship suggestions, and entities added by hand;
- files the original under the Record by default, which takes it out of the
  inbox. A document already filed elsewhere stays where it is;
- writes an accepted `manual` provenance row, the same one that entities added
  by hand from a document get.

A few decisions behind it:

- **The body is a copy, not a pointer.** Reading the document again (a better
  OCR pass, a new model) must not rewrite a Record someone has already relied
  on, and correcting an OCR slip in the Record must not change the evidence.
  `source_attachment_id` keeps the way back to the original.
- **`source_attachment_id` can't be edited.** `RecordDetails` leaves it out,
  and the strict unknown-field check rejects a PATCH that tries to set it.
- **One Record per document.** A second attempt gets a 409 naming the existing
  Record. Two Records with the same text would only produce a pair of
  duplicates for the correlation queue.
- **The model never proposes Records.** The extraction prompt's type list
  doesn't include them. Deciding that a document is worth keeping as a
  document is the analyst's call.
- **In PDF exports the body prints as paragraphs, not a table cell.** A
  ReportLab table cell can't split across pages, and a statement can run to
  several.

"Record" used to be the app's word for any entity ("Search records", "Use a
different record"). That text now says "entity" everywhere, so the word only
means this type.

## The Entities workspace is a two-cell grid, and only two

The tree and the network sit in a two-column grid: a sidebar of names beside
everything that is left. Both panes were auto-placed, which meant the grid
handed out its cells in document order — so adding a one-line hint *inside*
that div gave the hint the tree's cell, pushed the tree into the network's
cell, and wrapped the network onto a second row in the narrow column. The
list and the network swapped sides and the network came out the width of a
sidebar. Nothing about the CSS looked wrong; the markup had simply gained a
third child.

Both panes now name their cell (`grid-column` / `grid-row`), so a stray child
can only land underneath them, and the hint lives outside the grid. The
stacked layout below 900px resets both to one column, in document order, tree
first. `ui_entity_layout.py` asserts the shape at six widths — including that
the grid has exactly two children — because this is the kind of break that a
functional test walks straight past.

## Fitting the screen it is actually on

Two problems, one cause, and they surfaced together.

**Admin had become one long scroll.** Users, then feeds, then maps, then
branding, then the model settings, then the audit link, then backup and
restore — seven unrelated jobs in one column, each one pushing the next
further down. Nothing was broken; it just read as a pile.

It is now a navigation rail down the left with seven sections, the same shape
the audit trail took when it moved out of Admin onto its own page. Three
details are worth stating because they are easy to get wrong:

- **Each section loads on open, not on arrival.** `ADMIN_LOADERS` maps a
  section key to the loader that fills it, and `showAdminSection()` calls it
  when the section is shown. Opening Admin used to fire every one of those
  requests whether or not you cared about feeds.
- **The last section is remembered** in `localStorage` under
  `humint.admin.section`. An admin who is working through user accounts comes
  back to user accounts.
- **Below 860px the rail turns into a row of tabs** above the panel. A rail
  and a panel side by side needs roughly 150px plus a readable column; below
  that the rail was eating the panel.

**The relationship network did not fit a 13in laptop.** It fitted a large
desktop monitor, which is where it was built. The cause was pixel heights
that could not shrink: the graph pane had `min-height:420px`, the canvas
`height:320px`, the map `height:560px`. On a 900px-tall window with a toolbar
and a tab bar above them, they simply did not have the room they were
claiming.

Those are now `clamp()`d against viewport height — a floor so the pane is
never useless, a `vh` term so it tracks the window, a ceiling so it does not
become absurd on a 4K monitor. The tree and the graph pane share one
`--pane-h` custom property so they cannot drift apart. `dvh` is used where
supported, because on a phone `vh` is the address bar's idea of the viewport
rather than the one you can see.

A canvas needs more than CSS, though: its backing store is a fixed pixel
buffer, so a canvas whose box changes without a redraw draws stretched and
soft. `createNetwork()` now carries a `ResizeObserver` on its container,
debounced through `requestAnimationFrame` and skipping zero-size boxes (a
hidden pane reports 0×0, and refitting to that throws the layout away). It
fires on window resize, on the breakpoint stacking, on the admin rail
collapsing, and on browser zoom — the last one being the case no `resize`
listener would have caught.

### Long words

Separately, and found by putting an 88-character name with no spaces in it
into the test data: a browser will not break inside a word, so one such name
in a narrow column does not wrap. It spills out of its box and gives the
whole document a horizontal scrollbar — on the detail header, in a
`.field-row` whose `140px 1fr` columns both refuse to shrink below their
content, and in the audit list.

The fix is `overflow-wrap:anywhere` on the text surfaces (not on chrome —
buttons and tabs keep their words intact) and `minmax(0, …)` on those grid
columns. `anywhere` rather than `break-word` on purpose: only `anywhere`
lowers an element's min-content width, and min-content is exactly what a
grid or flex item refuses to shrink past.

Real case data is full of these: transliterated patronymics, URLs, file
names, record ids. The test suite now keeps one in the fixture and asserts
that `documentElement.scrollWidth <= clientWidth` on every view at 1920,
1440, 1280, 1366, 810 and 390 CSS pixels wide, with the record panel, the
debrief wizard, every admin section and the audit table open.

## Deliberate limitations (read before relying on this for anything important)

- **No delete on reports.** A bad report gets fixed by editing it or
  leaving it in draft, not removed — case write-ups shouldn't be able to
  silently vanish. If one genuinely needs to be gone, that's a manual
  `psql` operation, not a button in the UI.
- **The link-signal rules only know what is typed into the case file.** They
  cannot tell a family from four colleagues who share an office address, or a
  married couple from two people who happen to share a landline. That is why
  every rule proposes rather than writes, and why the confidence numbers are
  ordering hints rather than probabilities — the app is not claiming to know,
  it is claiming the pair is worth a look. A queue of proposals is not a
  finding, and accepting one is an analyst's judgement, recorded as theirs.
- **The assistant's proposals are only as good as retrieval.** It can only
  propose links between records it was shown, and it is shown the records that
  matched your instruction — so an instruction that retrieves the wrong
  twenty-five people produces proposals about the wrong twenty-five people.
  It says how many records it considered for exactly this reason. There is no
  way for it to propose a link involving a record it never saw, which is the
  guard that matters, but it is not the same as it having read your whole file.
- **A worker crash mid-extraction leaves that attachment stuck in
  "processing" forever.** There's no started-at timestamp and no
  auto-requeue logic. If this happens (check the worker's logs), the fix
  is a manual `UPDATE attachments SET extraction_status = 'pending' WHERE
  id = <id>;` via `psql` against the `db` container.
- **Individual Ollama call failures are still silent.** The app now warns
  loudly about the *predictable* causes — Ollama off, host unreachable, model
  not installed (see "Choosing a model without guessing") — but a call that
  fails for some other reason mid-run, like a model that's installed and
  simply times out on a large document, still just produces no suggestions
  for that attachment, with the reason only in the worker's logs. There's no
  per-attachment "extraction failed and here's why" surfaced in the UI.
- **Entities and reports are embedded once, not re-embedded after edits.**
  Correlation runs off whatever text existed the first time something was
  embedded (`embedding IS NULL` is the trigger). Edit a report heavily
  after the fact and correlation won't reconsider it against anything new.
- **No real entity-merge**, as covered above under Extraction and
  correlation.
- **Bulk CSV import has no duplicate detection.** Re-uploading a file that
  includes rows from a previous successful import creates a second set of
  entities, not an update to the first. This is on purpose scoped down: it
  keeps every row's outcome fully independent (see "Bulk CSV import"
  above), rather than adding "does this look like an existing entity"
  matching logic that would just be a narrower, worse version of what the
  correlation review queue already does after the fact.
- **One theme.** The phosphor-green CRT look is fixed, not configurable
  per-user.
- **Single-worker assumptions, mostly harmless if violated.** The worker
  uses `FOR UPDATE SKIP LOCKED` when claiming jobs, so running more than
  one worker replica wouldn't double-process anything — but nothing in
  this build actually needs more than one at personal/lab scale, so it's
  never been exercised under real concurrent load.
- **The map only shows Locations that have coordinates.** A Location
  entity created with just a free-text address (no lat/lon) is fully usable
  everywhere else in the app — reports, relationships, attachments all work
  on it — it just won't have a marker until coordinates exist. With
  geocoding enabled (the default) that normally happens on its own within a
  few seconds of saving the address; if it fails (see "Address geocoding +
  Maidenhead grid" above) or geocoding is disabled, set coordinates by hand
  via the entity edit form. There's no drag-to-reposition on an existing
  marker, on purpose, so a marker's position never changes from an
  accidental drag.
- **Geocoding accuracy is whatever the provider returns, unverified.** The
  app takes the first result Nominatim (or whatever you've pointed
  `NOMINATIM_BASE_URL` at) returns for an address and uses it as-is — there's
  no confidence score shown, no "did you mean," and no cross-check against a
  second provider. A vague, ambiguous, or informally-written address (common
  in HUMINT reporting — "the farmhouse past the second bridge") may resolve
  to the wrong place, or to nothing at all, without anything flagging that
  the result should be double-checked. Treat an auto-geocoded pin as a
  starting point to verify, not a survey-grade coordinate.
- **RSS-ingested Events are trusted at face value.** Adding a feed is
  itself the trust decision — there's no separate credibility rating on an
  ingested Event the way a Report has one, and no re-verification step. A
  feed that turns out to be unreliable is a feed to pause or delete, not
  something the app will flag for you.
- **Map markers don't draw connections between locations.** Two events
  sharing a location becomes obvious because they both show up in that
  location's own marker popup — the map doesn't additionally draw lines
  between related locations. If that turns out to matter in practice, it's
  a reasonable follow-up, not something this build takes on now.
- **No export has redaction or per-section control.** You get
  the whole package or nothing: every linked entity, every one of their
  photos. There's no "export without the source dossiers" switch and no way
  to omit a field. It also renders a deliberately small markdown subset — a
  table typed into a report body comes out as plain lines rather than a
  formatted table.
- **A Word document's tables are read, and were not always.** Until recently
  extraction walked only a `.docx`'s top-level paragraphs, which silently
  dropped every table in it — and a leaked ledger, roster, badge log or payment
  schedule *is* a table, so the documents where the substance lives were the
  ones that lost it while reporting success. Tables now come through in document
  order, one line per row, so a payee stays next to who approved it. If you have
  a `.docx` that was uploaded before this build, re-run it from Documents →
  Try again.
- **Attachment preview covers images and PDFs only.** DOCX, RTF and
  everything else still downloads to open, since rendering those in a
  browser would mean shipping a document viewer. A PDF preview also depends
  on the browser's own built-in viewer; where that's disabled, the Download
  button in the preview is the fallback.
- **Backup/restore is all-or-nothing, and manual.** There's no scheduled or
  automatic backup — nothing happens unless someone clicks the button, so
  "back up before you do something risky" is a habit, not a feature. Restore
  has no partial mode either: you can't pull just one entity, one report, or
  one table out of a backup through the UI. `data.json` inside the zip is
  plain readable JSON, so recovering one record by hand is possible, but the
  app won't do it for you. Two smaller consequences: a backup is built in
  memory before being written, so an enormous case is limited by the api
  container's RAM rather than its disk; and the format is versioned, so a
  future schema change may need a newer build to read older backups (it will
  say so plainly rather than restoring something half-understood).
- **The AI Assistant's retrieval is a best-effort shortlist, not a
  guarantee.** It hands Ollama the top few entities/reports it thinks are
  relevant (by embedding similarity, or keyword overlap without an embedding
  model — see "AI Assistant" above) — not your whole case file. A record
  that's relevant but doesn't share vocabulary with your question, and
  doesn't score high enough on similarity either, can simply be missed,
  producing a confidently-worded "not in the case file" that's actually a
  retrieval miss rather than a true absence. Treat it as a fast first look,
  not a substitute for searching the Entities/Reports views yourself when it
  really matters. It's also one continuous log per user, not multiple
  named/switchable conversations — clearing it is the only "start over."
  Replies aren't streamed token-by-token either — it's a single wait, then
  the full answer, same as extraction/correlation's request pattern.

## Architecture

```
docker-compose.yml
├── db        Postgres 16 — entities, relationships, reports, attachments,
│             users/sessions, both review queues. Backed up and restored
│             wholesale from the Admin page (see "Backup and restore"),
│             which reads the table list out of the live schema rather than
│             a hardcoded one.
├── api       FastAPI — REST API + serves the frontend's static files.
│             No separate frontend container. Also calls Ollama directly
│             (chat + embeddings) for the AI Assistant panel, on the
│             request path — the one deliberate exception to "an LLM call
│             never blocks a user-facing request," justified because a
│             chat reply that's slow to arrive is expected UX for a chat
│             panel, unlike an entity/report save.
├── worker    Polls for pending attachments (OCR/text-extraction — this is
│             the same loop that reads an unfiled Document, since a Document
│             is just an attachment with no parent record yet),
│             un-embedded entities/reports (Ollama extraction + correlation),
│             due RSS feeds (ingests new items as Event entities),
│             Locations awaiting geocoding (address -> coordinates, via
│             OpenStreetMap Nominatim by default), and — when switched on —
│             runs the link-signal pass over records already in the file
│             (see "Suggested links"; that one needs no model at all).
│             Never sits on the request path — uploads return immediately.
└── ollama    Optional (--profile local-llm). Skip it and point
              OLLAMA_BASE_URL at an existing Ollama instance instead.
```

The frontend (`frontend/`) is a single vanilla-JS single-page app — no
build step, no framework — served directly by the `api` container via
`StaticFiles`. Two things come from a CDN — `Leaflet` + OpenStreetMap tiles
for the Map view, and `marked` + `DOMPurify` for rendering (and sanitizing)
report markdown — both optional in the sense that the app still works, minus
that one feature, if a given CDN is unreachable. The relationship networks
have no dependency at all: the force-directed layout and the canvas drawing
are a few hundred lines in `app.js`, so the picture works on a machine with
no route to the internet.

Navigation is six tabs (Dashboard, Entities, Reports, Documents, Map, Review) plus an
account menu in the top right holding the appearance switch, Admin settings,
the Audit log and Log out. Anything about the case is a tab; anything about
*you* — or about running the instance — is in the menu.

**Light and dark.** The account menu has a System / Light / Dark switch. Dark
is the phosphor-green palette this tool has always used; light exists because
green-on-black is unreadable in daylight, and an analyst working outdoors is a
real use of this app rather than an edge case — so the light theme is built for
glare (near-black on near-white, visible borders, a dark green accent instead
of the neon one) rather than being a washed-out inversion. System follows the
operating system and keeps following it if the OS flips while the app is open.

The choice lives in the browser, not the account: it is a property of the
screen you are sitting at, and the same analyst on a bright tablet outdoors and
a monitor indoors wants different answers. It is resolved before the first
paint, so switching pages never flashes the wrong theme. Every colour in the
app is a token defined once per theme; the one place that cannot read them —
the relationship networks, which are painted onto a canvas — reads them live
from the stylesheet at draw time and repaints when the theme changes.

**Wide screens.** Most views are capped at a comfortable reading width, because
a 3440px-wide paragraph is unreadable. The Entities view deliberately is not:
the tree keeps its column and every extra pixel goes to the network, which is
the one thing in the app that genuinely gets more useful the more room it has.
Below 900px the two panes stack, tree first — finding a record by name is
something you do on a phone; reading a force-directed graph is not. The
topbar is
sticky and deliberately layered above the AI Assistant panel, so opening the
assistant never puts the nav or the account menu out of reach.

## Testing this yourself

`GET /health` reports API + database connectivity and needs no
authentication — point any uptime check at it. `docker compose logs -f
worker` is the place to watch OCR/extraction/correlation activity as it
happens.

To see the link-signal pass work without waiting a quarter of an hour for its
interval, run it once by hand against the running stack:

```
docker compose exec -e LINK_SIGNALS_ENABLED=true worker \
  python -c "import link_signals; print(link_signals.run_link_signals(force=True))"
```

It prints how many proposals it added and returns `False` when it found
nothing new — which, run twice in a row, is the quickest confirmation that it
is not going to re-propose what you already dismissed.
