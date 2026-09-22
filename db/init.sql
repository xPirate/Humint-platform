-- Runs automatically the first time the postgres container initializes its
-- data directory (docker-entrypoint-initdb.d convention). It will NOT
-- re-run on subsequent restarts, only on a fresh volume.

-- ============================================================================
-- Users & sessions
-- ============================================================================
-- No default/seeded account on purpose — shipping a real deployment with a
-- known default username+password is exactly the kind of thing that ends up
-- unchanged on a public instance. Instead the API treats "zero rows in
-- users" as a bootstrap state: the very first registration succeeds and is
-- made an admin automatically, and registration is closed after that (see
-- "Accounts" in README.md). Every later account is created by an admin.

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'analyst' CHECK (role IN ('admin', 'analyst')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,   -- disable an account without deleting it (preserves created_by/author_id history)
    -- Simple brute-force lockout: failed_login_attempts resets to 0 on any
    -- successful login; locked_until is set a short cooldown into the
    -- future once the count crosses a threshold (see api's auth module).
    -- Not a substitute for network-level protection, just a cheap backstop
    -- since this app is reachable to anyone on the LAN, not just the owner.
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ,
    -- Per-person UI state that has to follow the person rather than the
    -- machine: currently the dashboard's panel layout. A team that shares
    -- Chromebooks kept at each site — one analyst working from three
    -- different devices in a week, three analysts sharing one device —
    -- gets the wrong answer from browser storage in both directions: your
    -- arrangement doesn't travel with you, and you inherit whoever used
    -- that Chromebook last.
    --
    -- One JSONB document rather than a table of key/value rows or a column
    -- per setting: these are opaque blobs the server never queries into,
    -- only stores and hands back, and a column per preference would mean a
    -- migration every time the UI gains a toggle. Shape is owned by the
    -- frontend; the API validates only size and that it is an object.
    --
    -- Theme is deliberately NOT in here — see the appearance switch in
    -- README.md for why that one is a property of the screen you are sitting
    -- at rather than of you.
    preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Server-side sessions rather than a signed JWT: a row here can be revoked
-- immediately (delete it, or flip users.is_active) without needing a
-- token-blocklist. Cheap at this scale — a handful of users, not a
-- high-traffic public API.
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,            -- opaque random token; the cookie value
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at);

-- ============================================================================
-- Entities
-- ============================================================================
-- Fixed core types (Person, Organization, Location, Event, Source,
-- Communication, Vehicle) rather than a fully custom/user-defined schema — see the
-- scoping discussion in README.md. A single polymorphic `entities` table
-- carries the fields every type shares (id, type, name, audit columns) so
-- relationships/reports/attachments can reference "an entity" generically
-- with one FK column regardless of type; each type's distinguishing fields
-- live in its own 1:1 detail table rather than one giant sparse table or a
-- schemaless JSONB blob.
--
-- Entity ids are human-readable slugs with a random suffix, generated in
-- application code — e.g. "person-john-smith-a1b2c3" — greppable and
-- debuggable in logs and URLs, collision-proofed by the suffix.

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN
        ('person', 'organization', 'location', 'event', 'source', 'communication',
         'vehicle', 'record')),
    name TEXT NOT NULL,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,  -- archive instead of hard-delete — relationships/reports/attachments may still reference this entity and that history should stay intact. Permanent deletion exists too, for noise that should never have been recorded, but it is an admin-only deliberate act: see api/destroy.py
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- JSON-encoded embedding vector over name+description (and linked report
    -- text, computed by the worker), used only for the correlation review
    -- queue below — stored as TEXT rather than pulling in the pgvector
    -- extension, which is the right trade at personal/lab scale. NULL until
    -- OLLAMA_EMBED_MODEL is configured and the worker has processed this
    -- entity at least once.
    embedding TEXT,
    -- Set when this record was merged into another and archived. The row stays
    -- because history points at it — an old report link, an audit entry, a PDF
    -- someone exported last month — and following this pointer is how the app
    -- answers "where did that record go?" instead of showing a dead end.
    --
    -- Deliberately NOT ON DELETE CASCADE: if a survivor is ever hard-deleted,
    -- the records merged into it should be orphaned rather than destroyed.
    merged_into TEXT REFERENCES entities(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities (entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities (name);

CREATE TABLE IF NOT EXISTS person_details (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    aliases TEXT[],
    date_of_birth DATE,
    -- One of Friendly/Neutral/Unknown/Hostile — an analyst's own operational
    -- read on where this person stands relative to the case, not a fact
    -- extracted from a document (see worker/ollama_client.py, which
    -- deliberately never proposes it). Constrained at the API layer (see
    -- PersonDetails in api/entities.py), same pattern as Source's
    -- reliability_rating, not with a DB CHECK constraint.
    --
    -- Was called `faction` until named groups became entities in their own
    -- right. A faction is an Organization now -- "Iron Horse Militia" is a
    -- record with its own alignment, and a person's membership of it is a
    -- relationship. This column answers the narrower question the name
    -- `faction` was being stretched to cover: whose side is this one person
    -- on. The legacy value 'Family' still validates so that records already
    -- carrying it keep it, but it is no longer offered for new ones; see
    -- ALIGNMENT_VALUES / LEGACY_ALIGNMENT_VALUES in api/entities.py.
    alignment TEXT,
    occupation TEXT,
    physical_description TEXT,
    -- Living / Deceased / Unknown. Blank means nobody has assessed it, which
    -- is different from Unknown — Unknown is a judgement someone made and
    -- could not resolve, blank is a field nobody filled in. Both are useful
    -- and the app keeps them distinct everywhere.
    life_status TEXT,
    -- At liberty / Captured / Detained / Evading / Missing. Separate from
    -- life_status because they answer different questions and a person can be
    -- Deceased with a disposition that is simply no longer relevant.
    --
    -- Detained is deliberately distinct from Captured (held by authorities vs
    -- taken by a party to the case), and Missing from Evading: recording a
    -- person whose whereabouts are unknown as Evading asserts an intent to
    -- avoid that the reporting usually cannot support. Constrained at the API
    -- layer, same as alignment above.
    disposition TEXT
);

-- Contact points: how you would actually reach a Person, Organization or
-- Location, for a team that has a reason to.
--
-- A repeatable list rather than phone/email/address columns, because one
-- organisation has a switchboard and a press office and an after-hours
-- number, and flattening that into one column each loses whichever one you
-- didn't pick. Deliberately NOT the same thing as a Communication entity: a
-- Communication is a channel that is itself an object of interest (a radio
-- net being monitored, a number that appears in reporting), whereas a contact
-- point is directory information hanging off a record. Recording an office
-- switchboard as a Communication entity would put it in the entity list, the
-- graph and the correlation queue, which is not what a phone number in a
-- directory is for.
CREATE TABLE IF NOT EXISTS contact_points (
    id SERIAL PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    -- Free text validated against a vocabulary in the API layer (see
    -- CONTACT_KINDS in api/contacts.py), same pattern as relationship_type.
    kind TEXT NOT NULL,
    -- What this one is for: "work", "switchboard", "after hours", "home".
    label TEXT,
    value TEXT NOT NULL,
    notes TEXT,
    -- Marked rows are the ones that go into an exported PDF dossier, so an
    -- export carries the way to reach someone without carrying every number
    -- ever recorded against them.
    is_preferred BOOLEAN NOT NULL DEFAULT FALSE,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_contact_points_entity ON contact_points (entity_id);

CREATE TABLE IF NOT EXISTS organization_details (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    org_type TEXT,          -- freeform: company, NGO, agency, informal group, etc.
    founded_date DATE,
    website TEXT,
    -- The same scale as person_details.alignment, and the reason a person no
    -- longer needs a free-text "faction": a named group is an Organization
    -- with its own alignment, and belonging to it is a relationship. Setting
    -- it here says something about the group; setting it on a member says
    -- something about the member, and the two are allowed to disagree.
    alignment TEXT
);

CREATE TABLE IF NOT EXISTS location_details (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    address TEXT,
    -- Permissive / Semi-permissive / Non-permissive / Denied / Unknown: how
    -- freely someone could operate here. A place does not take a side the way
    -- a person does, so it gets its own vocabulary rather than the alignment
    -- scale -- "Hostile ground" and "a hostile person" are not the same claim.
    -- Denied is distinct from Non-permissive: non-permissive means it would
    -- be difficult and dangerous, denied means it is not an option.
    -- Constrained at the API layer, same as alignment.
    environment TEXT,
    lat DOUBLE PRECISION,
    lng DOUBLE PRECISION,
    -- Six-character Maidenhead grid locator (e.g. "IO91xl"), computed
    -- server-side from lat/lng whenever both are present (see api/geo.py /
    -- worker/geo.py) -- never user-settable directly, and NULL until
    -- coordinates exist.
    maidenhead_grid TEXT,
    -- Address -> coordinates automation (see worker/geocode.py). Only ever
    -- kicks in when a Location has an address but BOTH lat and lng are
    -- blank -- entering coordinates by hand (or already having them from a
    -- prior geocode) always wins and leaves this NULL. NULL = not
    -- applicable (no address, or coordinates already present); 'pending' =
    -- queued for the worker; 'processing' = claimed, request in flight;
    -- 'done' / 'failed' = terminal, see geocode_error for the latter.
    geocode_status TEXT CHECK (geocode_status IN ('pending', 'processing', 'done', 'failed')),
    geocode_error TEXT,
    geocoded_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS event_details (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    event_type TEXT,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    -- "Relevant until": the date past which this Event stops being current
    -- intelligence. A protest is stale in a fortnight; an occupation or a
    -- disaster stays relevant for a year. Distinct from ended_at, which is
    -- when the thing itself stopped happening — a raid that ended in ten
    -- minutes can stay relevant for months, and a strike that is still
    -- running can already be old news.
    --
    -- A date, not a duration or a shelf-life category: the analyst says when
    -- it stops mattering and the app does no arithmetic on their behalf.
    -- NULL means no expiry has been set, which is the default and never
    -- means "expired".
    --
    -- Nothing is deleted, archived or hidden when this passes. It drives a
    -- badge and an optional list filter, and that is all — an app that
    -- quietly retired records on a date would be making a judgement the
    -- analyst did not make.
    expires_at DATE
);

-- Source reliability uses the NATO/Admiralty System (STANAG 2511): a
-- letter grade A (completely reliable) through F (reliability cannot be
-- judged) for the SOURCE itself, independent of how credible any single
-- piece of reporting from that source is — that's reports.credibility_rating
-- further down. A normally-reliable source can still pass along something
-- implausible; an unreliable one can happen to be right. Keeping these as
-- two separate ratings on two different objects is deliberate, not
-- redundant.
CREATE TABLE IF NOT EXISTS source_details (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    source_type TEXT,        -- e.g. human, document, open-source, signal
    reliability_rating TEXT CHECK (reliability_rating IN ('A', 'B', 'C', 'D', 'E', 'F')),
    handling_notes TEXT,     -- caveats/protection requirements for handling this source — not a substitute for real operational security around a sensitive source's true identity
    -- Deliberately a different axis from reliability_rating. A source can be
    -- Hostile and grade A: whose side they are on and how well their
    -- reporting has held up are separate questions, and collapsing them is
    -- how a reliable adversary source gets discounted.
    alignment TEXT
);

-- A vehicle is a thing people are seen in, arrive in and move things with, and
-- it identifies them the way a phone number does: "the white panel van with
-- the cracked bumper" is a lead, and a plate is a lead you can act on. Before
-- this table those facts went into a Location's notes or a Person's physical
-- description, where nothing could search them and nothing could relate them.
CREATE TABLE IF NOT EXISTS vehicle_details (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    make TEXT,               -- Ford, Toyota, ...
    model TEXT,
    color TEXT,
    -- Stored as typed. Plates are not unique across issuing authorities, which
    -- is what plate_region is for, and they are not unique over time either —
    -- a plate moves between vehicles. Deliberately not a key of any kind.
    license_plate TEXT,
    plate_region TEXT,       -- issuing state, province or country
    -- Usually inherited in spirit from whoever owns it, but recorded here
    -- because a vehicle is often identified long before its owner is.
    alignment TEXT,
    -- One of a fixed list (Sedan, Coupe, SUV, Panel van, ...) — constrained at
    -- the API layer, see VEHICLE_STYLE_VALUES in api/entities.py, same pattern
    -- as person_details.alignment rather than a DB CHECK constraint, so adding a
    -- style is a code change and not a migration.
    style TEXT,
    notes TEXT               -- the specifics: damage, markings, equipment, who drives it
);

-- Plate lookups are the point of recording a plate at all, and they are
-- case- and space-insensitive in practice: "ABC 123" and "abc-123" are the
-- same plate written by two people.
CREATE INDEX IF NOT EXISTS idx_vehicle_plate
    ON vehicle_details (upper(regexp_replace(license_plate, '[^A-Za-z0-9]', '', 'g')))
    WHERE license_plate IS NOT NULL;

CREATE TABLE IF NOT EXISTS communication_details (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    -- One of a fixed list of emergency-comms-plan channels (Cellphone,
    -- HF/VHF/UHF/FM Radio, GMRS/FRS/CB, Email, In-Person, ...) — constrained
    -- at the API layer (see COMMUNICATION_MEDIUM_VALUES in api/entities.py),
    -- same pattern as person_details.alignment, not a DB CHECK constraint.
    medium TEXT,
    -- Free text whose meaning depends on medium — a phone number, a
    -- frequency, a channel number, an email address, etc. The frontend
    -- relabels this field to match whichever medium is selected.
    medium_detail TEXT,
    occurred_at TIMESTAMPTZ,
    participants_note TEXT   -- free text; the actual structured participants are relationships pointing at this entity, not this field
);

-- ============================================================================
-- Relationships
-- ============================================================================
-- A typed, directed edge between any two entities regardless of type —
-- Person "employed_by" Organization, Person "present_at" Event, Person
-- "communicated_with" Communication, Organization "located_at" Location,
-- and so on. relationship_type is free text validated against a known
-- vocabulary in the API layer rather than a DB enum, so adding a new
-- relationship type is a code change, not a migration.

CREATE TABLE IF NOT EXISTS relationships (
    id SERIAL PRIMARY KEY,
    from_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'possible' CHECK (confidence IN ('confirmed', 'probable', 'possible')),
    -- When the analyst learned of this relationship, not when it was
    -- allegedly active — a validity date range implies a precision about
    -- when a relationship started/ended that HUMINT reporting usually can't
    -- actually support, whereas "when did we find this out" is always a
    -- fact the analyst genuinely knows.
    discovery_date DATE,
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_entity_id <> to_entity_id)
);

CREATE INDEX IF NOT EXISTS idx_relationships_from ON relationships (from_entity_id);
CREATE INDEX IF NOT EXISTS idx_relationships_to ON relationships (to_entity_id);

-- ============================================================================
-- Reports
-- ============================================================================

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,               -- e.g. "report-a1b2c3", same id convention as entities
    title TEXT NOT NULL,
    body_markdown TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed')),
    -- NATO/Admiralty information credibility (1 = confirmed by other
    -- sources, through 6 = truth cannot be judged) for THIS report's
    -- content specifically — see the source_details comment above for why
    -- this is deliberately separate from source reliability.
    credibility_rating TEXT CHECK (credibility_rating IN ('1', '2', '3', '4', '5', '6')),
    -- Message precedence: Routine / Priority / Immediate / Flash. A SECOND
    -- axis, deliberately not merged with credibility_rating above — those two
    -- answer different questions and a report can be any combination of them.
    -- Credibility is how much you believe it. Criticality is how fast someone
    -- needs to act on it. A rock-solid write-up of who organised a protest can
    -- be credibility 1 and Routine; a thin, single-source indication that a
    -- hostage is about to be moved can be credibility 4 and Flash, and
    -- collapsing those into one number would lose the distinction that makes
    -- the second one worth waking someone for.
    --
    -- Precedence rather than Low/Medium/High/Critical because it describes
    -- required speed of response instead of vague importance, and because a
    -- reader with any service background already knows what it means. Stored
    -- as text and validated at the API layer, same as every other vocabulary
    -- in this app.
    criticality TEXT,
    author_id INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding TEXT                     -- same purpose/format as entities.embedding above
);

CREATE TABLE IF NOT EXISTS report_entities (
    report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (report_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_report_entities_entity ON report_entities (entity_id);

-- "What was merged into this record?" — asked on every entity page, so it is
-- indexed. Partial, because merged_into is NULL for all but a handful of rows.
CREATE INDEX IF NOT EXISTS idx_entities_merged_into
    ON entities (merged_into) WHERE merged_into IS NOT NULL;

-- ============================================================================
-- Attachments
-- ============================================================================
-- A document or image, attached to a report and/or directly to an entity
-- (a mugshot on a Person, a site photo on a Location). storage_path is
-- relative to UPLOAD_DIR, shared between the api (writes on upload) and
-- worker (reads to OCR/extract) containers via the `uploads` named volume.

-- An attachment with NEITHER report_id NOR entity_id is a DOCUMENT: something
-- dropped in before anyone knows what is in it. That is the normal first step
-- of the actual job — a PDF arrives, you want to know who is named in it — and
-- until this table allowed it, every upload had to be filed against a record
-- the analyst had not created yet, which is backwards.
--
-- Documents are the same rows, in the same table, running the same worker
-- pipeline (OCR → text → model-proposed entities → review queue). They are not
-- a separate concept with a parallel implementation; they are this concept with
-- the parent left off. `documents.py` is only the view over them.
CREATE TABLE IF NOT EXISTS attachments (
    id SERIAL PRIMARY KEY,
    report_id TEXT REFERENCES reports(id) ON DELETE CASCADE,
    entity_id TEXT REFERENCES entities(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    -- What a person calls it. "Intercept, 0340Z, channel 4" is findable in six
    -- months; "IMG_2291.png" is not, and a scanner or a phone picks the latter.
    -- NULL means "no one has named it", and the filename is shown instead.
    title TEXT,
    -- Where it came from, in the analyst's words: handed over by a source,
    -- pulled off a noticeboard, forwarded by a colleague. Provenance is half of
    -- what makes a document worth anything, and nothing else here records it.
    source_note TEXT,
    -- TRUE when the content was typed/pasted rather than uploaded as a file. It
    -- is still written to disk as a .txt so backup, extraction and download all
    -- work identically — this flag exists so the UI can say "pasted text"
    -- instead of offering a download named after a file nobody chose.
    is_pasted BOOLEAN NOT NULL DEFAULT FALSE,
    -- Documents are archived, not deleted, for the same reason entities are:
    -- the suggestions in the review queue point at them, and a proposal whose
    -- evidence has vanished is not reviewable. DELETE still exists for the
    -- "wrong file, get it off the server" case.
    archived_at TIMESTAMPTZ,
    storage_path TEXT NOT NULL,
    mime_type TEXT,
    file_size_bytes BIGINT,
    uploaded_by INTEGER REFERENCES users(id),
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    extracted_text TEXT,               -- OCR (images/scanned PDFs) or direct text extraction (text PDFs, .docx) output, filled in by the worker
    extraction_status TEXT NOT NULL DEFAULT 'pending' CHECK (extraction_status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
    extraction_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_attachments_report ON attachments (report_id);
CREATE INDEX IF NOT EXISTS idx_attachments_entity ON attachments (entity_id);
CREATE INDEX IF NOT EXISTS idx_attachments_extraction_status ON attachments (extraction_status);

-- The Documents view's only query: unfiled, unarchived, newest first. Partial,
-- because filed attachments are the overwhelming majority in an established
-- case file and none of them belong in this list.
CREATE INDEX IF NOT EXISTS idx_attachments_unfiled
    ON attachments (uploaded_at DESC)
    WHERE report_id IS NULL AND entity_id IS NULL AND archived_at IS NULL;

-- A Record is a document kept in the case file as an entity in its own right:
-- an insurance letter, a bank statement, a registration printout. It has a
-- name, the full text, and relationships like any other entity, so it shows up
-- on the network and on the pages of everyone it mentions.
--
-- body holds the text as it was when the Record was made. It is a copy, not a
-- pointer, on purpose: re-reading the source file later (a better OCR pass,
-- say) must not rewrite a Record someone has already relied on.
-- source_attachment_id points back at the original file when there is one.
CREATE TABLE IF NOT EXISTS record_details (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    record_kind TEXT,          -- free text: letter, statement, registration, ...
    record_date DATE,          -- the date ON the document, not when it was filed
    issued_by TEXT,            -- who produced it: an insurer, a bank, an agency
    body TEXT,
    source_attachment_id INTEGER REFERENCES attachments(id) ON DELETE SET NULL
);

-- ============================================================================
-- Extraction review queue
-- ============================================================================
-- Ollama-proposed entities/relationships read off an attachment's
-- extracted text. Nothing here ever writes to entities/relationships
-- directly — an analyst accepting a suggestion through the API is what
-- actually inserts it into the graph. This is the main safeguard against a
-- bad OCR read or a model hallucination silently corrupting a case.
-- suggested_from_name/suggested_to_name are plain names, not entity ids: at
-- suggestion time the model may be describing a person who doesn't have an
-- entity row yet, so relationship suggestions get resolved against
-- already-accepted entity suggestions (or existing entities) at review time.

CREATE TABLE IF NOT EXISTS extraction_suggestions (
    id SERIAL PRIMARY KEY,
    -- NULL for anything that did not come from a document. The table started
    -- as "things a model found in an attachment" and now holds proposals from
    -- three places (see `source`), only one of which has an attachment.
    attachment_id INTEGER REFERENCES attachments(id) ON DELETE CASCADE,
    -- Where the proposal came from. It changes how a reviewer should read it,
    -- so it is stored rather than inferred:
    --   extraction  a model read an uploaded document
    --   signal      plain SQL over records already in the case file — shared
    --               surname, shared address, co-mention. No model involved,
    --               and `evidence` names exactly what was matched.
    --   assistant   an analyst asked the assistant to propose links. A model
    --               wrote it, on request, and `evidence` carries the
    --               instruction it was given.
    --   manual      a person reading a document recorded something the model
    --               did not propose. The row exists so that "where did this
    --               record come from" still has an answer: it is the only
    --               link between a hand-made record and the document that
    --               prompted it.
    source TEXT NOT NULL DEFAULT 'extraction'
        CHECK (source IN ('extraction', 'signal', 'assistant', 'manual')),
    suggestion_type TEXT NOT NULL CHECK (suggestion_type IN ('entity', 'relationship')),
    suggested_entity_type TEXT,        -- set when suggestion_type = 'entity'
    suggested_name TEXT,
    suggested_relationship_type TEXT,  -- set when suggestion_type = 'relationship'
    suggested_from_name TEXT,
    suggested_to_name TEXT,
    -- Set when the proposer already knows WHICH records it means, which is
    -- true for every signal- and assistant-derived suggestion and false for
    -- extraction, where the model only ever saw names in a document. Without
    -- these a reviewer would have to re-identify two records the engine had
    -- already identified, which is busywork and an invitation to pick wrong.
    suggested_from_entity_id TEXT REFERENCES entities(id) ON DELETE CASCADE,
    suggested_to_entity_id TEXT REFERENCES entities(id) ON DELETE CASCADE,
    details JSONB,                     -- extra proposed fields (aliases, dates, notes) — freeform because what the model can extract varies per document
    -- Why this was proposed, in a form a person can argue with: for a signal,
    -- the rule that fired and the values it matched on; for the assistant, the
    -- instruction and its stated reasoning. A suggestion whose only
    -- justification is a number is a suggestion nobody can check.
    evidence JSONB,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    resolved_entity_id TEXT REFERENCES entities(id),  -- set once accepted and materialized into a real entity
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One live proposal per (pair, type, source). The signal pass re-runs on every
-- poll cycle and would otherwise propose the same link again every twenty
-- seconds; this makes re-running it free. Partial, so that dismissing a
-- suggestion does not block a later one from being raised again if the
-- evidence changes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_suggestions_pending_pair
    ON extraction_suggestions (source, suggestion_type, suggested_relationship_type,
                               suggested_from_entity_id, suggested_to_entity_id)
    WHERE status = 'pending' AND suggested_from_entity_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_suggestions_source_status
    ON extraction_suggestions (source, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_extraction_suggestions_status ON extraction_suggestions (status);
CREATE INDEX IF NOT EXISTS idx_extraction_suggestions_attachment ON extraction_suggestions (attachment_id);

-- ============================================================================
-- Correlation review queue
-- ============================================================================
-- Flags likely-duplicate or likely-linked entities/reports based on
-- embedding similarity (see entities.embedding / reports.embedding above).
-- Like the extraction queue, this never merges or links anything on its
-- own — an analyst confirms or dismisses each flag through the API.

CREATE TABLE IF NOT EXISTS correlation_suggestions (
    id SERIAL PRIMARY KEY,
    -- 'entity' and 'report' are same-type pairs (subject_a/b interchangeable,
    -- normalized by sorting — see worker/correlate.py's _normalize_pair).
    -- 'report_event' is a fixed-role cross-type pair added for RSS-ingested
    -- Events: subject_a_id is ALWAYS a report id and subject_b_id is ALWAYS
    -- an Event entity id, never sorted, since the two sides are never
    -- interchangeable the way two same-type ids are.
    subject_type TEXT NOT NULL CHECK (subject_type IN ('entity', 'report', 'report_event')),
    subject_a_id TEXT NOT NULL,        -- entity id or report id, per subject_type — no FK, since the two subjects can be of a type this table doesn't otherwise know how to join generically
    subject_b_id TEXT NOT NULL,
    similarity_score REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'dismissed')),
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (subject_a_id <> subject_b_id)
);

CREATE INDEX IF NOT EXISTS idx_correlation_suggestions_status ON correlation_suggestions (status);

-- (subject_a_id, subject_b_id) and (subject_b_id, subject_a_id) describe the
-- same pair; the worker normalizes pair order (e.g. sorts the two ids)
-- before inserting so this unique index actually catches re-flagging the
-- same pair on every poll, rather than duplicating that rule as a second
-- DB constraint.
CREATE UNIQUE INDEX IF NOT EXISTS idx_correlation_suggestions_pair
    ON correlation_suggestions (subject_type, subject_a_id, subject_b_id);

-- ============================================================================
-- RSS feed ingestion
-- ============================================================================
-- User-managed feeds only — nothing ships built in (see api/rss.py). An
-- analyst adds the feeds relevant to their own casework (e.g. local news
-- for a specific region) and the worker polls each active one on an
-- interval (see worker/rss_ingest.py). Events = ingested information,
-- Reports = analyst-generated: a new item from a feed becomes a plain Event
-- entity directly, NOT routed through extraction_suggestions above — that
-- queue exists to gate an LLM's guess at what a scanned document says,
-- whereas an RSS item is deterministic, already-published text from a feed
-- the analyst chose and configured themselves. The resulting Event is an
-- ordinary entity afterward; edit or archive it like any other if a feed
-- turns out to be noisy.
CREATE TABLE IF NOT EXISTS rss_feeds (
    id SERIAL PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    -- How far back this feed may reach, in days. NULL means no limit.
    --
    -- A feed's first poll is the dangerous one: it sees the publisher's whole
    -- current window at once, which for a busy newsroom is hundreds of items.
    -- Those land as Events, every Event is embedded and compared against every
    -- other Event, and news items about the same city read alike -- so a
    -- single careless feed can bury the correlation queue in pairs that were
    -- never going to be accepted.
    max_age_days INTEGER CHECK (max_age_days IS NULL OR max_age_days > 0),
    -- A ceiling on one poll regardless of dates, for the feed that publishes
    -- no timestamps at all and for the sudden back-dated dump. The newest
    -- items win; see worker/rss_ingest.py.
    max_items_per_poll INTEGER CHECK (max_items_per_poll IS NULL OR max_items_per_poll > 0),
    last_polled_at TIMESTAMPTZ,
    last_error TEXT,            -- most recent fetch/parse failure, cleared on the next successful poll
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per feed item the worker has already turned into (or considered
-- and skipped) an Event, keyed by the feed's own item guid so a later poll
-- never re-ingests the same article as a duplicate Event. entity_id is
-- nullable with ON DELETE SET NULL rather than CASCADE: if an analyst later
-- archives/deletes the Event that came from an item, the dedup record
-- itself should still survive so that item stays skipped on the next poll.
CREATE TABLE IF NOT EXISTS rss_items (
    id SERIAL PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES rss_feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    entity_id TEXT REFERENCES entities(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (feed_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_rss_items_feed ON rss_items (feed_id);

-- Runtime-editable overrides for settings that would otherwise only be
-- changeable via .env + a container rebuild (see "Ollama Settings" in the
-- Admin page). A single row (id is pinned to 1 via the CHECK) rather than a
-- generic key/value table: the handful of fields here are known in advance
-- and typed columns beat parsing bools/ints back out of TEXT on every read.
-- Every column is nullable and NULL specifically means "not overridden, use
-- the .env value" -- this table starts (and can return to) completely empty
-- of overrides without meaning "nothing configured," so a brand new install
-- with no admin having touched these settings yet behaves identically to
-- one where init.sql never ran this migration at all.
CREATE TABLE IF NOT EXISTS app_settings (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    ollama_base_url TEXT,
    -- The model that answers the AI assistant, and the default for anything
    -- else that needs generation.
    ollama_model TEXT,
    -- The model that reads uploaded documents. Separate from the assistant's
    -- because the two jobs want different things: extraction runs unattended
    -- on every upload and wants a small fast model that reliably emits JSON,
    -- while the assistant runs when somebody is waiting and wants a better
    -- conversationalist. NULL means "use ollama_model", which is what every
    -- deployment did before this column existed.
    ollama_extract_model TEXT,
    -- Embeddings for the correlation pass. Already separate, and a different
    -- kind of model again — an embedder emits vectors, not text.
    ollama_embed_model TEXT,
    ollama_enabled BOOLEAN,
    ollama_timeout_seconds INTEGER,
    -- Branding. Same NULL-means-"use the .env default" convention as the
    -- Ollama columns above.
    --
    -- instance_name replaces "HUMINT Platform" in the topbar badge, the
    -- browser tab, the login screen and the running header of every exported
    -- PDF. A team standing this up for themselves will call it something of
    -- their own, and a deployment that cannot be named reads as somebody
    -- else's software that they are borrowing.
    --
    -- brand_accent is an optional #rrggbb that overrides whichever palette is
    -- in use. Stored as the single colour an organisation actually has in its
    -- brand guide; the related tokens (hover, fill, and the label colour that
    -- has to stay readable on it) are derived in the frontend, so nobody has
    -- to supply five values or get the contrast right by hand.
    --
    -- default_palette is what a user sees before they have chosen one of their
    -- own. It is a default and not a lock: an analyst who cannot read the
    -- company palette in direct sunlight can still switch, which is the whole
    -- reason the light/dark axis exists separately from this one.
    instance_name TEXT,
    brand_accent TEXT,
    default_palette TEXT,
    updated_at TIMESTAMPTZ,
    updated_by INTEGER REFERENCES users(id)
);

-- ============================================================================
-- AI assistant (case-data-aware chat side panel)
-- ============================================================================
-- One continuous, private conversation log per user, not a multi-conversation
-- "threads" model — this is a personal research aid over the case ("what do
-- we know about John Smith"), not a shared case discussion log, so there's no
-- need for the complexity of naming/switching between separate chats. An
-- analyst can clear their own history (DELETE /api/assistant/messages) to
-- start fresh; nothing here is visible to other users, including admins —
-- see api/assistant.py.
--
-- citations is a JSON array of {"kind": "entity"|"report", "id": ..., "label": ...}
-- for whichever entities/reports were actually fed to Ollama as context for
-- that specific answer -- only ever set on 'assistant' rows -- so the panel
-- can render clickable "sources" under a reply without the frontend having
-- to re-derive what was used to ground it.
CREATE TABLE IF NOT EXISTS assistant_messages (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    citations JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_assistant_messages_user ON assistant_messages (user_id, created_at);

-- ============================================================================
-- Ollama call log
-- ============================================================================
-- One row per call to Ollama, from either the api or the worker.
--
-- This is NOT a billing ledger. A self-hosted model costs nothing per token;
-- what it costs is time on one machine that everything else is queued behind.
-- So the columns that matter here are the durations and the outcome, and the
-- token counts are mostly useful as the denominator that turns a duration
-- into a rate you can compare between models.
--
-- Three questions it exists to answer, in the order people ask them:
--
--   Is it working at all?      outcome + error. A model that has quietly
--                              started timing out on every extraction looks
--                              exactly like an empty queue from the outside,
--                              and that is the failure this table is most
--                              worth having for.
--   What is it spent on?       operation + user_id. Contention on a single
--                              GPU is real: one person's chat habit can be
--                              why the extraction queue is not moving.
--   Is this box keeping up?    eval_tokens / eval_duration_ms is tokens per
--                              second, and load_duration_ms separates "the
--                              model was cold" from "the model is slow",
--                              which are different problems with different
--                              fixes.
--
-- Durations are milliseconds. Ollama reports nanoseconds; they are converted
-- on the way in, because nobody reading this table by hand wants to count
-- nine zeroes.
--
-- Token counts are nullable on purpose. Ollama's older /api/embeddings
-- endpoint returns no counts at all, and a failed call has none either. NULL
-- means "not reported", which is a different fact from zero and the panel
-- says so rather than averaging a zero in.
CREATE TABLE IF NOT EXISTS ollama_calls (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Which side made the call. The worker's calls are batch work nobody is
    -- waiting on; the api's are someone sitting looking at a spinner, and a
    -- slow one matters more.
    source TEXT NOT NULL CHECK (source IN ('api', 'worker')),
    operation TEXT NOT NULL CHECK (operation IN ('extract', 'embed', 'chat')),
    model TEXT,
    -- NULL for anything the worker does: extraction and correlation are
    -- nobody's request in particular. Set for assistant chat, which is the
    -- only Ollama work done on behalf of a named person.
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    prompt_tokens INTEGER,
    eval_tokens INTEGER,
    total_duration_ms INTEGER,
    load_duration_ms INTEGER,
    eval_duration_ms INTEGER,
    outcome TEXT NOT NULL DEFAULT 'success'
        CHECK (outcome IN ('success', 'failure', 'timeout', 'disabled')),
    -- Truncated on the way in. Enough to tell a connection refused from a
    -- model-not-found without turning this into a second log file.
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_ollama_calls_occurred ON ollama_calls (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_ollama_calls_outcome ON ollama_calls (outcome, occurred_at DESC);

-- ============================================================================
-- Audit log
-- ============================================================================
-- Who did what, when. Append-only by convention: nothing in the app ever
-- updates or deletes a row here except the retention prune (see
-- AUDIT_RETENTION_DAYS and worker/audit_prune.py). That convention is the
-- honest limit of what a self-hosted app can promise — an admin with psql
-- access can edit this table, which is exactly why syslog forwarding exists:
-- a copy on another machine is the only version an on-box admin can't
-- quietly rewrite. See "Audit trail" in README.md.
--
-- Records every change, plus the reads that move data OUT of the system
-- (attachment downloads, PDF exports, backup downloads, assistant queries).
-- Ordinary browsing is deliberately not recorded — see the same README
-- section for why, and how to reason about that gap.
--
-- actor_id is ON DELETE SET NULL rather than CASCADE, and actor_username is
-- denormalised alongside it: deleting or renaming an account must never
-- erase or obscure the record of what that account did. The username stored
-- here is the one in force at the time of the action.
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor_username TEXT,               -- NULL for actions taken by the worker itself (see actor_kind)
    actor_kind TEXT NOT NULL DEFAULT 'user' CHECK (actor_kind IN ('user', 'system', 'anonymous')),
    action TEXT NOT NULL,              -- dotted object.verb, e.g. 'entity.create', 'report.export_pdf', 'auth.login_failed'
    object_type TEXT,                  -- 'entity' | 'report' | 'attachment' | 'user' | 'backup' | ...
    object_id TEXT,                    -- stored as TEXT because ids across this schema are a mix of slugs and serials
    object_label TEXT,                 -- human-readable name/title AS IT WAS, so the log still reads sensibly after a rename
    outcome TEXT NOT NULL DEFAULT 'success' CHECK (outcome IN ('success', 'failure', 'denied')),
    ip_address TEXT,
    user_agent TEXT,
    -- Action-specific extras. Deliberately holds field NAMES rather than
    -- values for edits: the audit trail should say what someone changed
    -- without becoming a second, unprotected copy of the case data itself.
    detail JSONB
);

CREATE INDEX IF NOT EXISTS idx_audit_log_occurred ON audit_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log (actor_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log (action, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_object ON audit_log (object_type, object_id, occurred_at DESC);


-- ---------------------------------------------------------------------------
-- Offline maps
--
-- The Map view and the debrief's place-picker are only useful if there is
-- something under the pins, and this app is meant to keep working with no
-- route out. So tiles come from here rather than from whatever the frontend
-- was hardcoded to: an admin registers tile SOURCES while online, downloads
-- the areas they care about as PACKS, and from then on the map is served out
-- of this machine.
--
-- A source is just an XYZ URL template, which is the same shape ATAK's map
-- source XML uses -- importing one of those is a parse, not a translation.

CREATE TABLE IF NOT EXISTS map_sources (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url_template TEXT NOT NULL,        -- XYZ template, e.g. https://{s}.tile.example.org/{z}/{x}/{y}.png
    -- Subdomains for the {s} placeholder, as the single string Leaflet wants
    -- ("abc" means a, b and c). NULL when the template has no {s}.
    subdomains TEXT,
    min_zoom INTEGER NOT NULL DEFAULT 0 CHECK (min_zoom >= 0 AND min_zoom <= 22),
    max_zoom INTEGER NOT NULL DEFAULT 19 CHECK (max_zoom >= 0 AND max_zoom <= 22),
    tile_format TEXT NOT NULL DEFAULT 'png' CHECK (tile_format IN ('png', 'jpg', 'webp')),
    -- Shown bottom-right on the map. Not decoration: most tile licences
    -- require it, and an analyst reading a screenshot months later needs to
    -- know which imagery they were looking at.
    attribution TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    -- Whether this source may be BULK DOWNLOADED into a pack. Viewing a
    -- source online and scraping it wholesale are different permissions and
    -- most tile operators grant only the first -- OpenStreetMap's own tile
    -- usage policy prohibits bulk download outright. The shipped OSM source
    -- therefore has this FALSE, and an admin adding a source has to say
    -- deliberately that they are entitled to cache it.
    allow_download BOOLEAN NOT NULL DEFAULT FALSE,
    -- Set on the one source that ships with the app, so the UI can explain
    -- why it cannot be downloaded and the upgrade path never duplicates it.
    is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order INTEGER NOT NULL DEFAULT 100,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (max_zoom >= min_zoom)
);

-- One downloaded area, backed by an MBTiles file on disk (plain SQLite, the
-- same format ATAK, QGIS and every offline map tool already read -- so a pack
-- built here is usable elsewhere, and a pack built elsewhere drops in here).
-- The row is the job as well as the artifact: the worker claims it at
-- 'pending' and reports progress against it, which keeps "what is downloading
-- right now" answerable from the same place as "what do we have".
CREATE TABLE IF NOT EXISTS map_packs (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES map_sources(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    min_lat DOUBLE PRECISION NOT NULL,
    min_lon DOUBLE PRECISION NOT NULL,
    max_lat DOUBLE PRECISION NOT NULL,
    max_lon DOUBLE PRECISION NOT NULL,
    min_zoom INTEGER NOT NULL,
    max_zoom INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'downloading', 'done', 'failed', 'cancelled')),
    -- Counted up front from the bbox and zoom range, so the dialog can say
    -- "412,000 tiles, about 6 GB" before anybody commits to an overnight job.
    tiles_total BIGINT NOT NULL DEFAULT 0,
    tiles_done BIGINT NOT NULL DEFAULT 0,
    tiles_failed BIGINT NOT NULL DEFAULT 0,
    bytes_total BIGINT NOT NULL DEFAULT 0,
    -- Set by an admin pressing Stop. The worker checks it between tiles
    -- rather than being killed, so a cancelled pack keeps the tiles it
    -- already has and can be resumed by queueing the same area again.
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    last_error TEXT,
    file_path TEXT,                    -- absolute path to the .mbtiles, set when the worker starts
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK (max_zoom >= min_zoom),
    CHECK (max_lat >= min_lat AND max_lon >= min_lon)
);

CREATE INDEX IF NOT EXISTS idx_map_packs_source ON map_packs (source_id);
-- The worker's claim query and the tile server's "which packs cover this
-- source" lookup are the only two hot paths here.
CREATE INDEX IF NOT EXISTS idx_map_packs_status ON map_packs (status, id);

-- The one source that ships. Everything else is imported by an admin -- see
-- the ATAK map source collections linked from the Admin page. Deliberately
-- NOT downloadable: the OSM tile usage policy forbids bulk download, and
-- shipping it any other way would be handing users a footgun.
INSERT INTO map_sources (name, url_template, subdomains, min_zoom, max_zoom,
                         tile_format, attribution, allow_download, is_builtin, sort_order)
VALUES ('OpenStreetMap', 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', 'abc',
        0, 19, 'png', '&copy; OpenStreetMap contributors', FALSE, TRUE, 0)
ON CONFLICT (name) DO NOTHING;


-- ---------------------------------------------------------------------------
-- Map zones
--
-- An area with an assessment attached and a clock on it -- a protest, a
-- cordon, a stretch of road nobody should be on tonight. The NOTAM idea:
-- somewhere is different from normal, for a while, and everyone looking at the
-- map should see it without reading a report first.
--
-- A zone is NOT a Location. A Location is a place that exists; a zone is a
-- judgement about ground over a period, and the same ground can carry several
-- at once (a protest inside a district that is separately non-permissive).
-- Conflating them would mean editing the town's record to say the square was
-- briefly dangerous.
--
-- Geometry is GeoJSON in JSONB rather than PostGIS. The operations this needs
-- are draw, draw again, and "what of mine is inside" -- point-in-polygon is
-- twenty lines of Python (api/zones.py) and does not put an extension between
-- a user and `docker compose up`. The bounding box columns alongside are what
-- keeps that honest: they are indexed, so containment questions start with a
-- cheap rectangle test and only the survivors are checked properly.

CREATE TABLE IF NOT EXISTS map_zones (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    -- Same vocabulary as location_details.environment, and for the same
    -- reason: ground does not take a side, it is more or less workable.
    -- Constrained at the API layer (see ENVIRONMENT_VALUES in
    -- api/entities.py), same pattern as alignment.
    environment TEXT NOT NULL,
    -- 'polygon' | 'rectangle' | 'circle'. Rectangle is stored as a polygon
    -- and kept as a separate shape only so re-editing one offers the handles
    -- it was drawn with. A circle stores its centre and radius_m instead,
    -- because a circle flattened to a polygon can never be dragged back into
    -- a circle.
    shape TEXT NOT NULL CHECK (shape IN ('polygon', 'rectangle', 'circle')),
    -- GeoJSON geometry: a Polygon for polygon/rectangle, a Point for circle.
    geometry JSONB NOT NULL,
    radius_m DOUBLE PRECISION,          -- circles only
    -- Denormalised bounds, written by the API whenever geometry changes.
    -- Indexed, so "which zones could contain this point" is an index scan
    -- rather than a JSONB parse of every zone ever drawn.
    min_lat DOUBLE PRECISION NOT NULL,
    min_lon DOUBLE PRECISION NOT NULL,
    max_lat DOUBLE PRECISION NOT NULL,
    max_lon DOUBLE PRECISION NOT NULL,
    -- The Event this zone is the footprint of, when there is one. Optional on
    -- purpose: a protest is an Event, a permanently denied area is not, and
    -- forcing the second to invent an Event that never ends would be the
    -- model telling a lie to keep its own shape.
    --
    -- ON DELETE SET NULL, not CASCADE: deleting the Event should not silently
    -- take the ground assessment with it.
    event_id TEXT REFERENCES entities(id) ON DELETE SET NULL,
    valid_from TIMESTAMPTZ,
    -- When this stops being current. NULL means open-ended.
    --
    -- **Expiry does not delete anything.** An expired zone stays, stops being
    -- drawn as live, and remains attached to its Event along with every report
    -- that was written while it was running -- which is the whole point of
    -- having it. "Where was the cordon that night" is a question asked weeks
    -- later, and an app that tidied the answer away would be useless for it.
    valid_until TIMESTAMPTZ,
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (max_lat >= min_lat AND max_lon >= min_lon),
    CHECK ((shape = 'circle') = (radius_m IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_map_zones_bounds ON map_zones (min_lat, max_lat, min_lon, max_lon);
CREATE INDEX IF NOT EXISTS idx_map_zones_event ON map_zones (event_id);
CREATE INDEX IF NOT EXISTS idx_map_zones_valid ON map_zones (valid_until);

-- Every change of assessment, kept as case material rather than only as an
-- audit entry. "It went non-permissive at 18:40 when the police line moved"
-- is a finding somebody will want in a report; the audit log is a log, and
-- an analyst should not have to ask an admin to read their own timeline.
--
-- The first row is written when the zone is created, so the history is
-- complete rather than starting at the first edit.
CREATE TABLE IF NOT EXISTS map_zone_changes (
    id SERIAL PRIMARY KEY,
    zone_id INTEGER NOT NULL REFERENCES map_zones(id) ON DELETE CASCADE,
    environment TEXT NOT NULL,
    -- NULL on the row that records the zone being created.
    previous_environment TEXT,
    note TEXT,
    changed_by INTEGER REFERENCES users(id),
    -- Tie-broken by id everywhere it is ordered: now() is transaction-scoped,
    -- so two changes in one request would otherwise sort arbitrarily.
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_map_zone_changes_zone ON map_zone_changes (zone_id, changed_at DESC, id DESC);
