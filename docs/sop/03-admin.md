# SOP 03 — Administration and maintenance

**Purpose.** Keep an instance running, backed up and upgradable, and know what
to do when it misbehaves.

**Audience.** Whoever owns the machine. On a one-person deployment that is
also the analyst.

**Assumed.** A running instance ([SOP 01](01-install.md)) and shell access to
the host. Every command runs from the project directory.

---

## Finding your way around Admin settings

Account menu → **Admin settings**. The page has a navigation rail down the
left with seven sections:

| Section | What lives there |
|---|---|
| **Users** | Accounts, roles, password resets. |
| **RSS feeds** | Feed subscriptions, lookback limits, deleting a feed and its records. |
| **Maps** | Tile sources, offline map packs, ATAK/MOBAC imports. |
| **Appearance** | Instance name, logo, colour palette. |
| **Model** | The Ollama endpoint and model, plus recent model activity. |
| **Audit** | The way through to the audit trail. |
| **Backup & restore** | Download a backup, restore one. |

Each section loads when you open it, not before, so a slow feed list does not
hold up the users table. The section you were last in is remembered, so
coming back to Admin puts you where you left off.

On a narrow screen the rail becomes a row of tabs above the panel instead.

## Backups

**There is no other copy of your case file.** The Docker volume is not a
backup; a snapshot of the host might be, if somebody set one up.

A backup is a single `.zip` holding every record plus every uploaded
attachment — enough to stand the instance up somewhere else. It includes
**user accounts and their password hashes**, so treat the file as seriously as
the database.

### Taking one by hand

Account menu → **Admin settings** → **Backup & Restore** → **Download
backup**. Large cases with many attachments take a moment to build.

### Automating it

There is no scheduler in the app — deliberately, because the host already has
one and knows where the disk is. Log in with `curl`, download, keep a month:

```bash
#!/bin/bash
# /usr/local/bin/humint-backup.sh
set -euo pipefail
BASE="http://localhost:8080"
DEST="/var/backups/humint"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

mkdir -p "$DEST"
curl -sS -c "$JAR" -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$HUMINT_USER\",\"password\":\"$HUMINT_PASS\"}" \
  -o /dev/null
curl -sS -b "$JAR" "$BASE/api/admin/backup" \
  -o "$DEST/humint-$(date +%F).zip"
curl -sS -b "$JAR" -X POST "$BASE/api/auth/logout" -o /dev/null

find "$DEST" -name 'humint-*.zip' -mtime +31 -delete
```

Put the credentials in a root-only environment file, not in the script:

```bash
sudo install -m 600 /dev/stdin /etc/humint-backup.env <<'EOF'
HUMINT_USER=admin
HUMINT_PASS=your-admin-password
EOF
```

And a nightly timer via cron:

```
15 2 * * *  . /etc/humint-backup.env && /usr/local/bin/humint-backup.sh
```

**Then test it.** An untested backup is a rumour. Restore last night's into a
scratch deployment (below) once, now, and again after any upgrade that changes
the schema.

Keep at least one copy off the machine. A backup that only exists on the Pi
does not survive the Pi.

### Restoring

**Restore replaces everything** — records, reports, attachments, user
accounts. There is no undo, and everyone including you is logged out
afterwards and signs back in with an account from the restored file.

Admin settings → Backup & Restore → choose the file → **Restore from this
file…** → type the confirmation phrase it asks for.

Restoring an archive from an older build is safe: the restore checks the
backup against the current schema first and refuses with a specific reason if
it cannot fit, rather than half-applying. An older archive missing a column
this build added simply leaves that column empty.

### A scratch deployment for testing restores

Copy the project to a second directory, give it its own port and its own
volume namespace, and restore into that:

```bash
cp -r Humintelligence humint-scratch && cd humint-scratch
cp ../Humintelligence/.env .
sed -i 's/^API_PORT=.*/API_PORT=8081/' .env
docker compose -p humint-scratch up -d --build
```

`-p` is what keeps its database volume separate from the real one. Tear it
down with `docker compose -p humint-scratch down -v` when you are finished.

## Upgrading

```bash
cd Humintelligence
git pull
docker compose up -d --build
```

**`--build` matters.** The API and worker code is baked into their images; the
frontend is mounted from disk and updates the moment you pull. Without
`--build` you get new pages driven by old code.

Then:

1. **Read the release notes** for migrations. `docs/DESIGN.md` has an
   "Upgrading an existing deployment" section listing every schema change and
   the exact SQL. They are additive and safe to run twice.
2. **Take a backup first**, every time, before the migration.
3. **Run any migration** you need:

   ```bash
   docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
   ALTER TABLE ... ;
   "
   ```

   Substitute the values from your `.env`.
4. **Check `/health`** and open the app.
5. **Hard-reload the browser** once (Ctrl-Shift-R / Cmd-Shift-R) if anything
   looks half-applied. Builds from September 2026 onward stamp their assets so
   this should not be necessary; older ones cached scripts.

### Rolling back

```bash
git checkout <previous-tag>
docker compose up -d --build
```

A rollback does **not** undo a migration. Migrations here are additive — a
column the old code does not know about is ignored — so this is usually fine,
but it is the reason to take the backup before the migration rather than
after.

## Users

Account menu → **Admin settings** → **+ New User**. Two roles:

| Role | Can |
|---|---|
| **analyst** | Everything to do with the case file: records, reports, documents, review, merging, exports. |
| **admin** | All of that, plus users, backup and restore, the audit log, branding and model settings, and **deleting records permanently** (see below). |

Give people **analyst** unless they need to administer the instance. The role
boundary is one of the few safety rails here.

There is no per-record access control. Every account sees the whole case file.
If two pieces of work must not see each other, run two instances.

**To remove someone**, set their account inactive rather than deleting it —
the audit trail references them, and a dangling author is worse than an
inactive one. The app will refuse to remove or demote the last active admin,
which is the one thing that would lock everybody out.

### There is no password change and no reset

This is the honest statement of a real limitation. No self-service reset, no
email flow, no "change my password" screen. If somebody needs a new password,
an admin cannot do it from the app either.

Recovering an account means writing a new hash into the database directly.
It works, and it is deliberately not one command:

```bash
# 1. Generate a bcrypt hash for the new password.
docker compose exec api python3 -c \
  "import bcrypt,getpass; print(bcrypt.hashpw(getpass.getpass().encode(), bcrypt.gensalt()).decode())"

# 2. Write it in, and clear any lockout at the same time.
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "UPDATE users SET password_hash = '<the hash>', failed_login_attempts = 0,
   locked_until = NULL WHERE username = '<the user>';"
```

The same procedure recovers a lost admin password. Keep the admin password
somewhere you will still have it after losing the laptop.

### Lockouts

Five failed logins locks an account for fifteen minutes. It clears itself.
To clear it now, run just the `failed_login_attempts`/`locked_until` part of
the statement above.

## Deleting records for good

Analysts archive. Admins can also delete, and the two are not the same thing:
archiving hides a record and keeps every row, deleting removes them.

Use it for noise — records that should never have existed. "ATTACHMENT A",
"Page 2 of 4", a page header the model read as a person. Do not use it to
tidy away records that were real and are now finished with: that is what
archiving is for, and reports and old exports still point at them.

- **One record**: open it, press **Delete**.
- **Several**: Entities → **Select to merge** → tick them → **Delete
  selected**.
- **A report or a document**: the same button on its own page.

Every delete is previewed, counted and typed-to-confirm, refused when a
confirmed report cites the record, and written to the audit log with what was
destroyed and by whom. The audit entry is the only trace left.

**There is no undo.** A restore from a backup is the only way back, which is
the practical reason to keep the backups in this SOP working.

## RSS feeds

### Set the lookback before the first poll

A feed's **first** poll is the one that hurts: it sees the publisher's whole
current window, which for a busy newsroom is hundreds of items. Every one
becomes an Event, every Event is compared against every other Event, and news
items about the same city read alike — so one unlimited feed can put thousands
of suggestions in the correlation queue on the day you add it.

New feeds default to **7 days** and **50 items per poll**. Both are editable
per feed and both matter: age is what you actually mean, but a feed that
publishes no timestamps would be filtered to nothing by age alone, and a
back-dated archive dump defeats an age limit entirely.

**Feeds added before this upgrade have no limits.** Set them.

### Deleting a feed

The dialog counts what the feed put in the case file first, then offers three
things to do with those Events:

| | |
|---|---|
| **Leave them** | Only stops polling. Right for a feed that has been running and whose records you have built on. |
| **Archive them** | Hidden from the default lists, every row kept, reversible. The safe middle. |
| **Delete them** | Gone for good. Right for a feed added by mistake. |

Anything cited by a **confirmed** report is archived rather than deleted
whichever you choose, and the result tells you how many. A confirmed report
pointing at a record that no longer exists is a hole in the reporting.

## Maps

The map draws tiles from whatever **source** is selected. Out of the box that
is OpenStreetMap, which needs an internet connection. To keep the map working
offline, register a source you are allowed to cache and **download the areas
you care about while you still have a connection**.

Admin settings → **Maps**.

### Adding sources

**+ Source** takes an XYZ URL template (`https://…/{z}/{x}/{y}.png`).
**Import ATAK XML** takes an ATAK/MOBAC map source document and converts it —
`{$z}` placeholders and `<serverParts>` are handled for you, and every
`<customMapSource>` in the file is read. Collections of these are published
online; [joshuafuller/ATAK-Maps](https://github.com/joshuafuller/ATAK-Maps) is
a well-known one, and includes satellite imagery sources.

**Read the terms before you cache anything.** Those collections point at other
people's tile servers, and some of them are undocumented endpoints whose terms
of service do not permit this use. Importing a source here is not a judgement
that you may use it — that is why imported sources arrive marked
not-downloadable and you have to tick the box yourself. The bundled
OpenStreetMap source cannot be ticked at all: the OSM tile usage policy
forbids bulk download, and scraping it is how a deployment gets blocked.

Put a real contact address in `MAP_TILE_USER_AGENT`. Several operators require
one and serve errors to anything anonymous.

### Downloading an area

**+ Download an area**, pick the source, drag a box on the map, choose a zoom
range. The dialog shows the tile count and rough disk **before** you start.

**Tile counts quadruple with every zoom level.** Zoom 14 is roughly "streets
named"; 16 is "individual buildings". A town to z16 is a few hundred thousand
tiles and runs for hours at the polite default rate. Download the smallest
area and the shallowest zoom that does the job, then add more later — packs
stack, and a second download of the same ground resumes rather than repeats.

Downloads run in the background on the worker, so the app stays usable. Stop
keeps what has already been downloaded; queueing the same area again carries
on from there.

### Living with packs

- Packs land in the `mappacks` Docker volume, not in `uploads` — **they are
  not in your backups**, deliberately. A pack is re-downloadable; your case
  file is not, and a multi-gigabyte basemap inside a backup zip helps nobody.
- Deleting a source deletes its packs and their files.
- `docker system df` shows the volume. Watch it: this is the one feature in
  this app that can fill a disk.
- On the Map page, **Downloaded only** draws from packs alone. Outside a
  downloaded area the map is blank, which is the honest answer — that is what
  an analyst with no connection will see.
- Set `MAP_DOWNLOAD_ENABLED=false` on a machine that must never make an
  outbound connection.

## Watching it

```bash
curl http://localhost:8080/health          # API + database, unauthenticated
docker compose ps                          # are the containers up
docker compose logs -f --tail 100 api      # requests and errors
docker compose logs -f --tail 100 worker   # OCR, extraction, correlation, geocoding
docker compose logs -f --tail 100 db       # Postgres
docker system df                           # disk, including the volumes
```

Point any uptime check at `/health`. It reports `{"status":"ok","db":true}`
when both halves are working and `"degraded"` when the database is not
reachable, which is the failure worth being woken for.

**The audit log** (account menu → Audit log) records every write with who did
it and from where. It exports to CSV. If you want it somewhere central, set
`AUDIT_SYSLOG_ENABLED=true` and the `AUDIT_SYSLOG_*` variables in `.env`;
forwarding is best-effort and never blocks or fails a request.

By default the audit log is kept forever (`AUDIT_RETENTION_DAYS=0`). Set a
number of days if you would rather it did not grow without bound — but think
about what you are giving up before you do.

## When something misbehaves

### Documents stay at *Waiting*

The worker is not running or cannot reach the database.

```bash
docker compose ps worker
docker compose logs --tail 50 worker
docker compose restart worker
```

### Documents say *Read* but propose nothing

The model was unavailable when they were read. Check **Admin settings →
Ollama** — it says whether the model answers and how long it takes. Fix the
model, then use **Read empty ones again** on the Documents inbox, which
re-queues exactly the documents that came back empty and leaves your existing
decisions alone.

### Extraction is slow

Normal on modest hardware, and nothing in the app waits on it — extraction
runs in the worker, never on the request path. If it is too slow to be useful,
in order of effect: use a smaller model, set `OLLAMA_EXTRACT_MODEL` to
something small while leaving the assistant on a larger one, or move Ollama to
a machine with more to give ([SOP 01](01-install.md), step 6, Option C).

Admin settings → Ollama shows per-model response times, so you can tell
whether you are guessing.

### The app is up but pages look wrong

Hard-reload once. If that fixes it, the browser had a cached script from an
earlier build.

### Disk filling up

Attachments are the usual culprit, then Postgres, then old Docker images.

```bash
docker system df
docker image prune          # removes images nothing uses
```

Do **not** prune volumes. `docker volume prune` will take your database with
it if the stack is down.

### Restore refuses the file

Read the message — it says specifically what it could not fit.

- **"…it contains: …"** — the zip is not a backup, and the message lists what
  is actually inside it. Usually that names the file you picked by mistake.
- **A table or column it does not have** — the backup is from a newer build
  than the one you are restoring into. Upgrade the deployment first (including
  migrations), then restore.

A backup that has been unzipped and zipped back up — which some desktops do
automatically on download — restores fine; the wrapper folder is allowed. A
zip containing two backups is not, because there is no way to tell which one
you meant.

### Total loss of the machine

1. Install on the new machine ([SOP 01](01-install.md), steps 1–5).
2. Create an admin account when asked — it will be replaced by the restore.
3. Restore your most recent backup.
4. Log in with an account from the backup. The account you just made is gone.

Practise this once before you need it.

---

## A monthly ten minutes

- Confirm the backups exist and are not zero bytes.
- Restore last month's into a scratch deployment and log into it.
- `git pull` and read what changed.
- Skim the audit log for anything you do not recognise.
- Check the disk.
