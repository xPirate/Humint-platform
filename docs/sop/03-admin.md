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
left with eleven sections, in this order:

| Section | What lives there |
|---|---|
| **Users** | Accounts, roles, password resets. |
| **RSS feeds** | Feed subscriptions, lookback limits, how long unread items are kept, deleting a feed and its records. |
| **Maps** | Tile sources, offline map packs, ATAK/MOBAC imports. |
| **Appearance** | Instance name, logo, colour palette. |
| **Model** | The Ollama endpoint and model, plus recent model activity. |
| **Boards** | Switch on the Roster, BOLO and Priorities pages, and name them. |
| **Field devices** | Enroll and revoke the phones that can send field reports. |
| **Link signals** | Turn the no-model link-finding pass on, off, or on for a set time. |
| **Retention** | How long a record may sit untouched before it is archived. |
| **Audit log** | The way through to the audit trail. |
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
cp -r humint-platform humint-scratch && cd humint-scratch
cp ../humint-platform/.env .
sed -i 's/^API_PORT=.*/API_PORT=8081/' .env
docker compose -p humint-scratch up -d --build
```

`-p` is what keeps its database volume separate from the real one. Tear it
down with `docker compose -p humint-scratch down -v` when you are finished.

## Upgrading

```bash
cd humint-platform
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
3. **Run any migration** you need. They ship as files in `db/`, named for
   the version that introduced them, and are fed to `psql` on standard
   input:

   ```bash
   docker compose exec -T db psql \
       -U "$POSTGRES_USER" -d "$POSTGRES_DB" < db/migrate-v1.4-field.sql
   ```

   The two variables come from your `.env`; `set -a; . ./.env; set +a` puts
   them in the shell. Run the migrations **in order** if you are skipping
   several versions
   — v1.4 before v1.5 before v1.6 — and note the `-T`, without which
   compose eats the redirected file. A fresh install needs none of them.
4. **Check `/health`** and open the app.
5. **Hard-reload the browser** once (Ctrl-Shift-R / Cmd-Shift-R) if anything
   looks half-applied. Builds from September 2026 onward stamp their assets so
   this should not be necessary; older ones cached scripts.
6. **Rebuild the companion app** if you use one and the release notes
   mention field templates. The forms are compiled into the APK, so the
   console learns about a new one on `git pull` and the handsets do not. A
   mismatch is not a failure — a report written against an older set still
   uploads and still renders — but the new form will not be there to pick
   until somebody installs a new build. See `android/README.md`.

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

## Link signals: on when you want it

Admin settings → **Link signals**. The no-model pass that proposes
relationships from what is already in the file — shared surnames, shared
addresses, shared contact details. Proposals go to **Review**; nothing reaches
the case file without somebody accepting it.

You no longer need to edit `.env` and rebuild to try it.

- **Switch on / Switch off** — stays that way until you change it.
- **Run it for 10 min / 30 min / 1 hour / 4 hours / 8 hours** — switches itself
  off at the end. This is the one to reach for: turn it on, look at what comes
  out, and it tidies up after itself.
- **Run one pass now** — does a single pass immediately and tells you how many
  it proposed. Worth knowing: the pass has its own fifteen-minute timer, so a
  ten-minute window can come and go without one happening. If you want to see
  results now, press this.

The state line at the top says whether it is running, what is deciding that,
how long is left on a window, and when the last pass was. With no override
saved it follows `LINK_SIGNALS_ENABLED` in `.env`.

Turning it on against an established file produces a burst of proposals about
records entered months ago, all at once. Do it on a quiet afternoon.

## Feeds

Admin settings → **RSS feeds** to add one: a URL and a label.

Feed items no longer become Event records. They appear on the **Feeds** page,
where anyone can read them and send the useful ones to Documents; everything
else is dismissed. Events that earlier versions created from feeds are
untouched.

Three limits per feed, all on the add form and changeable afterwards:

- **Only items from the last N days** and **at most N per poll** — these stop a
  busy publisher dumping its whole current window on the page the day you add
  it. Seven days and fifty items is a normal week of a normal feed.
- **Keep unread items for N days** — the clean-up. Editable straight from the
  feed list, because the reason to change it is always "this feed turned out
  noisier than I thought", which you notice while looking at the list. Blank
  keeps them forever. Anything already sent to Documents is a document and is
  never pruned.

Set the third one by how fast that publisher goes stale: days for a newsroom, a
year for a monthly bulletin.

**Article fetching.** Sending an item pulls the linked page so the document
holds the article rather than a one-line summary. Links that resolve to private
or loopback addresses are refused by default — a third-party feed should not be
able to make this server read things on your internal network. If your feeds
genuinely are internal, set `FEED_FETCH_ALLOW_PRIVATE=true` in `.env`, knowing
that any feed you add can then make the server fetch anything it can reach.

## Boards: only the ones this team uses

Admin settings → **Boards**. Three optional pages, each switched on separately.
All three are off until you turn one on, and switching one off takes the tab
away without throwing anything away.

| Board | What it is | Who it suits |
|---|---|---|
| **Roster** | Cards for the team — portrait, name, callsign, role, how to reach them. Each opens the record. | A standing team with people who need to recognise each other. |
| **BOLO** | Anything to recognise on sight, of any type, with what to do about it and how urgent it is. | A case with a vehicle, a person or a place in circulation. |
| **Priorities** | The standing questions, ranked, with what would count as an answer. | Anyone with more to look at than time — a fox hunt, a collection plan. |

Turn on only what the team will use. Two pages nobody opens cost more than
they look: every new person asks what they are for.

**Name them what you call them.** "Roster" becomes Watch bill or Who's Who;
"BOLO" becomes Lookouts. The nav tab and the page heading both follow, and the
one-line blurb underneath is yours to write.

**Who posts.** You decide which boards exist; any analyst can post to one and
close entries out. Raising a lookout is operational and should not wait for an
admin. Everything is in the audit log under `board.*` and `priority.*`.

## Field devices

Admin settings → **Field devices**. A device is a phone or tablet that can
**send** a field report into the intake queue and do nothing else. Its token is
refused by every read endpoint in the app: a device that is lost, lent or
seized leaks whatever is still sitting unsent on it, and nothing from the case
file.

**Enrolling.** Give it a name you will recognise in a list, pick whose device
it is, and check the address — it is pre-filled with whatever this browser is
using to reach the server, which on a LAN is usually right, but a phone cannot
reach `localhost`. Use the address the phone can actually reach.

**The token appears once.** Scan the QR with the companion app, or type the
token in by hand. It is stored hashed; nobody, including you, can read it back.
If it is lost, revoke that device and enroll a new one — it takes ten seconds.

**Bind it to the right person.** Submissions are attributed to that account,
and deactivating the account stops every device enrolled to it. That is
deliberate: the process you already have for somebody leaving should also turn
off their phone, without a second list to remember.

**Revoke** stops a device immediately — the next thing it sends is refused.
What it already sent stays. **Remove** takes it off this list; its submissions
keep saying which device they came from.

The list shows how much each device has sent and when it last checked in.
A device that has never checked in has not been set up yet. Everything is in
the audit log under `field.device.*` and `field.submission.*`, and a
submission is recorded as coming from a device rather than from a person.

**The limits** are in `.env` under `FIELD_*`: how large a photo may be, how
many per report, and how many writes a minute one device gets. The last of
these is a guard against a phone stuck in a retry loop, not a usage budget —
30 a minute is generous for a person typing.

**Who works the queue.** Any analyst, under Review → *From the field*. Only
admins enroll devices.

### The companion app, and why you will be asked for a QR

The Android app (`android/` in the repository, with its own README) does not
keep the console's address or its token. They are scanned off the enrollment
QR at the moment of upload and forgotten again as soon as it finishes.

That is a deliberate trade and it has a cost you should plan for: **somebody
has to be able to show an analyst that code when they come back in range.**

**Show a code** next to any live device does exactly that. Two things about
it are worth understanding before you use it.

*It issues a new code rather than showing you the old one.* The console never
kept the token — only a SHA-256 of it — precisely so that somebody who gets
into the console does not walk away with a set of working device
credentials. There is nothing to retrieve. So the button mints a fresh token
for the same device: same row, same label, same account, same history of
everything it has sent.

*That costs nothing on the handset.* The app does not store a code. It scans
one at the moment it uploads and forgets it immediately, so a token only has
to be valid for the minute it is being scanned. Rotating it between uploads
invalidates nothing on any phone.

The one thing it does invalidate is a code you already **printed**. Which
brings us to:

**Print a card.** On the screen that shows a code — whether from enrolling or
from Show a code — there is a Print a card button. It prints one page with
the device name, the account, the address the app should reach, the QR and
the token, and a line saying what the code can and cannot do. That is the
artefact to keep: put it wherever the team keeps keys, and an analyst coming
back in range can be handed it without anybody logging in.

It prints rather than downloading, on purpose. A file with a live token in it
ends up in a Downloads folder, gets synced somewhere, and outlives its
usefulness. Paper does not sync.

If you print a card and later press Show a code for that device, the printed
card stops working. Print the new one.

What it buys: a handset that is lost, lent or seized gives up the reports on
it that had not been sent yet — encrypted — and nothing else. Not a token, not
an address, not even the knowledge that there is a console to look for.

If a device is lost, revoke it here. The queue that was on it was never
readable without the phone's own hardware key, and nothing on it pointed
anywhere.

## Retention: keeping the file usable over years

A case that closes never needs this. A standing file does: records entered
years ago and never looked at since crowd every list, every search and every
network, and the instance gets quietly less useful each year.

Admin settings → **Retention**. It is off, with no windows set, until you set
them.

**Set a window per type.** Days with no activity. Blank means that type never
ages out. The types age differently — an intercept is stale in months, a
building is not — so there is no single right number.

**Activity is not editing.** The clock resets when the record is edited, when
a relationship is added at either end, when a report that names it is written,
or when a document is attached to it. Somebody who has not been edited in two
years but appears in last week's reporting is not idle.

**Preview before you save.** It runs your numbers against this instance and
changes nothing: how many records each window catches, and how many each
exemption spares. Do this first — the "anything still linked to an active
record" exemption can spare almost everything in a well-linked file, and the
only way to know is to look.

**It warns before it acts.** A record is flagged with a visible date and
archived only if the grace period passes with nothing happening to it. Touch
it and the flag goes.

**It archives; it never deletes.** An archived record keeps its relationships,
reports and documents, stays searchable under **Show archived**, and comes
back with one click. Its page says the policy archived it rather than a
colleague.

**Anyone can hold a record.** **Keep indefinitely** on the record, or
right-click it anywhere it appears. That record then never ages out, whatever
the policy says. Analysts do not need to be admins to do this, on purpose.

**Run now** runs the saved policy immediately instead of waiting for the
hourly sweep. Everything it does is in the audit log under `retention.*`.

### A reasonable starting point

Windows are a matter of local judgement, but if you want somewhere to begin:
generous numbers, both exemptions on, a long grace period, and Preview until
the "due" column is a number you would be comfortable reading through. Then
switch it on and look at the flagged list after the first sweep. Tighten
later; a policy that archives too little is a nuisance, one that archives too
much is a morning spent clicking Reactivate.

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

**"violates foreign key constraint attachments_entity_id_fkey"** means the
deployment is missing the portrait-pointer migration (see "The portrait
pointer makes the schema's only cycle" in DESIGN.md). Re-running `db/init.sql`
does *not* fix it — run the `ALTER TABLE ... DROP CONSTRAINT` / `ADD
CONSTRAINT ... DEFERRABLE INITIALLY DEFERRED` pair from that section, then
restore again. Your backup files are fine; nothing needs re-taking.

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
