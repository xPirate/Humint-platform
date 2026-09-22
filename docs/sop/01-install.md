# SOP 01 — Install and first run

**Purpose.** Take a machine with nothing on it to a working HUMINT Platform
with an admin account.

**Time.** Fifteen minutes without a model. An hour with one, most of it
waiting for a download.

**You will need.** A machine you control, Docker, and the ability to reach it
on a port. Nothing else — no account anywhere, no API key.

---

## 1. Decide where it runs

| | Without a model | With a local model |
|---|---|---|
| CPU | 2 cores | 4+ cores |
| RAM | 2 GB | 8 GB for a 7–8B model, more for larger |
| Disk | 5 GB + attachments | 5 GB + the model (4–40 GB) |

A Raspberry Pi 4 with 8 GB runs the platform comfortably. It will *technically*
run a small model too, slowly enough that you will not enjoy it — if you want
extraction on a Pi, point it at an Ollama running on a desktop elsewhere on
your network (step 6).

The machine should be on a network you control. This platform has no
multi-tenancy and no per-record access control: **anyone who can log in sees
the whole case file.** Do not put it on the open internet.

## 2. Install Docker

Skip if you already have it. On Debian, Ubuntu or Raspberry Pi OS:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
```

Log out and back in, then check both parts are there:

```bash
docker --version
docker compose version
```

If `docker compose version` fails but `docker-compose --version` works, you
have the old standalone tool. Every command below uses the modern spelling;
substitute accordingly, or install the plugin.

## 3. Get the code

```bash
git clone https://github.com/xPirate/Humintelligence.git
cd Humintelligence
```

## 4. Write your `.env`

```bash
cp .env.example .env
```

Open `.env` in an editor. **One line must change:**

```
POSTGRES_PASSWORD=change-me
```

Set it to something real. This is the database password; it is never typed
into the app and never displayed, so make it long. Generate one if you like:

```bash
openssl rand -base64 24
```

Everything else has a working default. The four you are most likely to want:

| Setting | Default | Change it when |
|---|---|---|
| `API_PORT` | `8080` | Something already uses 8080 on this machine. |
| `INSTANCE_NAME` | `HUMINT Platform` | You want your own name in the title bar and on exported PDFs. |
| `MAX_UPLOAD_MB` | `25` | You will attach scans bigger than 25 MB. |
| `OLLAMA_ENABLED` | `true` | You are running without a model — set it to `false` (see step 6). |

`.env` is in `.gitignore` and must stay there. It holds your database
password.

## 5. Start it

```bash
docker compose up -d --build
```

The first build takes a few minutes. Then check all three services are up:

```bash
docker compose ps
```

You want `db`, `api` and `worker` all running. Then ask the API itself:

```bash
curl http://localhost:8080/health
```

A healthy instance answers `{"status":"ok","db":true}`. If it says
`"db":false`, the API is up but cannot reach Postgres — go to
[Troubleshooting](#troubleshooting).

## 6. The model (optional)

Extraction, duplicate detection and the assistant need a model. Everything
else — records, relationships, reports, exports, the audit trail, the signal
pass that proposes links using plain SQL — works without one.

**Option A: no model.** Set `OLLAMA_ENABLED=false` in `.env` and
`docker compose up -d`. The app will say plainly, where a model would have
been used, that none is configured. This is a perfectly reasonable way to run
it.

**Option B: the bundled container.** On a machine with the memory for it:

```bash
docker compose --profile local-llm up -d
docker compose exec ollama ollama pull llama3.1:8b
```

The pull is several gigabytes. The defaults in `.env` already point at this
container, so there is nothing else to set.

**Option C: an Ollama you already run.** Point `.env` at it and leave the
bundled container alone:

```
OLLAMA_BASE_URL=http://192.168.1.50:11434
OLLAMA_MODEL=llama3.1:8b
```

Use the machine's address, not `localhost` — `localhost` inside the container
means the container. Then `docker compose up -d`.

Whichever you choose, confirm the app can see it: **account menu → Admin
settings → Ollama**. It reports whether the model answered and how long it
took. That page is the authority; a model that works from your shell but not
from the container is almost always the `localhost` mistake above.

## 7. Create the admin account

Open `http://<the machine>:8080`.

The first screen asks you to create an account, because there are no default
credentials and no seeded users. **The first account created becomes the
administrator.** Use a real password — at least 10 characters, and not one
you use elsewhere.

There is no password reset. If you lose the admin password, see SOP 03.

## 8. Check it works

Five minutes that will save you an evening later.

1. **Make a record.** Entities → **+ New Entity** → Person → a name → Save.
   It appears in the tree under PERSON. (The other kinds are Organization,
   Location, Event, Source, Communication and Vehicle.)
2. **Make a second and link them.** Open the first, **+ Add relationship**,
   pick the second, type `associate_of`, save. Both now show it, and the
   network beside the tree draws a line.
3. **Write a report.** Reports → **+ New Report** → give it a title and a
   sentence → Save. Link it to a record, then press **Export PDF** on the
   report's own page. A PDF should download.
4. **Upload a document.** Documents → drop a PDF or image in. Within a minute
   its status moves from *Waiting* to *Reading* to *Read*. With a model
   configured, proposals appear in **Review → Extraction**. Without one, the
   text is extracted and searchable and nothing is proposed — which is
   correct, not a fault.
5. **Take a backup.** Account menu → Admin settings → Backup & Restore →
   **Download backup**. You now know the backup works before you need it.

If any of the first three fail, stop and check the logs (below) rather than
carrying on.

## 9. Before real data goes in

- **Read [SOP 03](03-admin.md) and set up backups.** There is no other copy
  of your case file.
- **Put TLS in front of it** if anything but this machine will reach it, and
  set `SESSION_COOKIE_SECURE=true` and `TRUST_PROXY_HEADERS=true` in `.env`
  once you have. A reverse proxy (Caddy, nginx, a Tailscale funnel) is the
  usual way.
- **Download your maps while you have a connection**, if this machine will
  ever be offline. Admin settings → Maps: register a tile source you are
  entitled to cache, then download the areas you need. Nothing else in the app
  requires the internet, but tiles have to be fetched from somewhere and the
  time to do it is before you need them. See [SOP 03](03-admin.md).
- **Try the exercise first.** `docs/exercise/` has eight invented documents
  that take an empty instance to a finished assessment. It is the fastest way
  to learn what the review queues want from you, and it costs you nothing to
  get wrong. [SOP 02](02-daily-use.md) walks through it.
- **Or restore a sample.** `samples/` holds three complete case files as
  backup zips. Restoring one **replaces everything in the instance**, so do
  it before your own data goes in, or in a second deployment. They log in
  with `demo` / `humint-demo-2026`.

---

## Troubleshooting

**`docker compose ps` shows `db` restarting.**
Almost always `POSTGRES_PASSWORD` being empty or containing a character your
shell ate. Check `docker compose logs db`. If you started once with a broken
password, the data volume was initialised with it — on a fresh install with
nothing to lose, `docker compose down -v` (this **deletes the database**) and
start again.

**`/health` says `"db":false`.**
The API is running and Postgres is not reachable. `docker compose logs db`.
On a Pi, this is often the database still starting up on first run — wait
thirty seconds and ask again.

**The page loads but nothing works, or a button does nothing.**
Hard-reload the browser (Ctrl-Shift-R, or Cmd-Shift-R on a Mac). If that
fixes it, you were on a cached script from an earlier build. Builds from
September 2026 onward stamp their assets to prevent this.

**Uploads fail with a size error.**
`MAX_UPLOAD_MB` in `.env`, then `docker compose up -d`.

**Documents sit at *Waiting* forever.**
The worker is not running or cannot see the database:
`docker compose logs --tail 50 worker`.

**Everything is slow on a Pi.**
Check whether the model is the reason: Admin settings → Ollama shows response
times. If a local model on the Pi is the bottleneck, move it to another
machine (step 6, Option C) or turn it off.

**Where the logs are.**

```bash
docker compose logs -f api       # requests, errors, the web layer
docker compose logs -f worker    # OCR, extraction, correlation, geocoding
docker compose logs -f db        # Postgres itself
```
