# Contributing

Thanks for taking a look. This is a small project with a strong opinion about
a few things, so a note on what helps before you spend time on a change.

## Reporting a bug

Open an issue with:

- **What you did**, in enough detail to repeat it.
- **What happened**, including the exact error text where there is one.
- **What you expected instead.**
- **The relevant logs**: `docker compose logs --tail 100 api` and
  `docker compose logs --tail 100 worker`. A stack trace is worth more than a
  description of a stack trace.
- **Which build**, so the maintainer knows what code you are on.

**Scrub your case data first.** A log line from a real file contains real
names, and so does a screenshot. If a bug needs data to reproduce it, please
reproduce it with invented data — [`docs/exercise/`](docs/exercise/) exists
for exactly this, and is already fictional from end to end.

## Before writing code

Read [`docs/DESIGN.md`](docs/DESIGN.md) for the area you are touching. Most of
what looks like an odd decision in this codebase is a decision, and the
reasoning is written down. If you disagree with one, say so in an issue first
— that is a conversation worth having before either of us spends an evening
on it.

A few things that are settled and not up for a drive-by change:

- **Nothing a model proposes is written to the case file without a human
  accepting it.** Extraction, correlation and the assistant all produce
  suggestions in a queue. There is no autopilot mode and there will not be one.
- **Archiving is the default; deletion is an admin's deliberate act.**
  Ordinary work archives, because reports, audit entries and old exports
  reference records and a dead link is worse than a redirect. Permanent
  deletion exists for noise that should never have been recorded — it is
  admin-only, previewed, typed-to-confirm, refused when a confirmed report
  cites the record, and audited. Do not widen that: no delete for analysts,
  no delete without a preview, no silent cascade past what the preview said.
- **The frontend has no build step.** It is vanilla JS, HTML and CSS served
  straight from disk. A PR that introduces npm, a bundler or a framework is a
  bigger conversation than a PR.
- **No new runtime dependency on a CDN.** People run this on machines with no
  route to the internet. Leaflet is vendored in `frontend/vendor/` precisely
  because losing it took out the whole Map view rather than degrading it. What
  is still loaded externally (`marked` and `DOMPurify` for report markdown)
  degrades to a working app without it, and anything new has to do the same —
  or be vendored.
- **A tile source is not shipped just because it works.** Viewing a tile
  server and bulk-caching it are different permissions. `allow_download` is a
  separate column from `is_active` for that reason, the bundled OpenStreetMap
  source cannot be made downloadable at all, and this project does not ship a
  list of other people's tile servers. Users import their own.

## Code style

Match what is around it. Concretely:

- **Comments say why, not what.** `# cooling: big moves early` earns its
  place; `# loop over nodes` does not. Where a piece of code looks wrong at a
  glance and is correct, that is precisely where a comment belongs.
- **Python**: standard library and the handful of pinned dependencies. No new
  dependency without a reason that could not be met with fifty lines.
- **SQL**: parameterised, always. There is f-string SQL in this codebase and
  every instance interpolates only names from a fixed dict in the same file,
  never request data.
- **Duplicated modules stay byte-identical.** `geo.py`, `ollama_client.py`,
  `audit.py`, `ollama_usage.py` and `tilemath.py` exist in both `api/` and
  `worker/` because the two containers are built separately. If you change
  one, copy it to the other and check with `cmp`. `tilemath.py` is the one
  where drift would be quietest: the API counts the tiles in an area and the
  worker fetches them, so a one-row disagreement means every pack finishes
  reporting a total it never reached.
- **In-app text is instructions, not reasoning.** Hints, empty states and
  error messages say what to do in a sentence or two. The reason something
  works the way it does goes in a code comment or `docs/DESIGN.md`, not on
  the screen.
- **"Record" is an entity type.** When you mean any entity, write "entity".

## Tests

Every change to behaviour needs a test that would have failed before it.

The suites are end-to-end against a real Postgres and a real browser, not
mocks: an API suite drives the HTTP endpoints, a UI suite drives Chromium
through Playwright. They are deliberately written to read as sentences about
what the app promises — `check("an archived record is left out by default",
...)` — because a failing test should tell you what broke in the product, not
just which assertion tripped.

Test names describe behaviour. `test_merge_keeps_the_confirmed_edge` is
useful; `test_merge_2` is not.

## Pull requests

- One subject per PR.
- Say what it changes and why in the description; link the issue if there is one.
- Include the test output.
- If it changes the database schema, include the migration SQL and add it to
  the upgrade section of `docs/DESIGN.md`. Schema changes must be additive and
  safe to run twice (`IF NOT EXISTS`, `IF EXISTS`).
- If it changes something a user would notice, update the relevant
  [SOP](docs/sop/) too. A feature nobody can find is not finished.

## Security

If you find something with real security consequences, do not open a public
issue. Contact the maintainer directly and give them a reasonable chance to
fix it first.

Be aware of what this project does and does not claim: it is built to run on
a network you control, with no multi-tenancy and no per-record access control.
"Any logged-in user can see the whole case file" is documented behaviour, not
a vulnerability. See
[Security posture](docs/DESIGN.md#security-posture).
