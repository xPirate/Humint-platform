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
- **For the Android app**, the handset and Android version, and the crash or
  the log rather than the compose ones:

  ```bash
  adb logcat -d AndroidRuntime:E '*:S'                 # a crash
  adb logcat -d --pid="$(adb shell pidof org.humint.field)"   # everything it did
  ```

  The app writes no log lines of its own, in any build — deliberately,
  because a field handset should not keep a diary of what was observed — so
  logcat shows you framework and crash output and nothing else, and a report
  about an upload needs the console's `api` log too. "The upload failed" is
  three different bugs depending on which end refused it. If you are
  debugging one and need tracing, add it locally and take it out again.

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
  bigger conversation than a PR. (`android/` is the one exception and it is
  not really one: it is a separate Gradle project that nothing in the compose
  stack builds, imports or serves. The server does not know whether it
  exists.)
- **The field token is write-only, and the app holds nothing.** A device
  token is refused by every read endpoint; the handset never stores the
  console's address or its token, only scans them at the moment of upload.
  Both halves are the feature. A change that lets a device read anything, or
  that saves a credential on the phone "so the analyst does not have to scan
  every time", is a change to the threat model and belongs in an issue first.
- **The app has no Google Play Services.** QR decoding, location and the
  camera are ZXing, the AOSP `LocationManager` and CameraX respectively,
  because the handsets this is for are de-Googled. A dependency that pulls in
  `play-services-*` takes the app off those phones entirely.
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
- **Duplicated modules stay byte-identical.** `audit.py`, `db.py`, `geo.py`,
  `link_signals.py`, `ollama_client.py`, `ollama_usage.py`, `retention.py`
  and `tilemath.py` exist in both `api/` and `worker/` because the two
  containers are built separately. If you change one, copy it to the other
  and check with `cmp`. The whole list in one go:

  ```bash
  for f in audit db geo link_signals ollama_client ollama_usage retention tilemath; do
    cmp "api/$f.py" "worker/$f.py" || echo "DRIFTED: $f.py"
  done
  ```

  `tilemath.py` is the one where drift would be quietest: the API counts the
  tiles in an area and the worker fetches them, so a one-row disagreement
  means every pack finishes reporting a total it never reached.
  `idgen.py`, `main.py` and `settings.py` also appear in both and are
  *deliberately* different — do not "fix" those with a copy.
- **`api/field_templates.json` is the only definition of a field form.** The
  console renders submissions from it, the API validates against it, and the
  Android build copies it into the APK's assets at compile time — there is no
  second copy to keep in step. Bump its `version` on any change to a field
  key, a template key or an entity mapping, because a handset that has been
  off-network for a week is still queueing reports against the old one and
  the version is how the two ends notice. The rule that outranks all of this:
  **nothing in that file may ever cause a submission to be refused.** An
  unknown template, a renamed key and a value of the wrong type are all
  rendered rather than rejected, because a report that reached the console is
  a report somebody walked somewhere to write.
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

**The Android app has its own.** `./gradlew test` in `android/` runs JVM unit
tests with no emulator needed. The one that matters most is
`TitleParityTest`, which checks that the title the app shows for a report it
has just written is character-for-character the title the console will give
the same report after upload. Those are two implementations of the same rule
in two languages, which is exactly the kind of thing that drifts silently, so
the fixture is generated from the console's own code rather than written by
hand:

```bash
python3 tools/gen_title_cases.py        # after any change to compose_title
cd android && ./gradlew test
```

Regenerate it in the same PR as the change, and commit the result.
`android/README.md` has the rest, including the JDK version trap that will
otherwise cost you an afternoon.

## Pull requests

- One subject per PR.
- Say what it changes and why in the description; link the issue if there is one.
- Include the test output.
- If it changes the database schema, include the migration SQL and add it to
  the upgrade section of `docs/DESIGN.md`. Schema changes must be additive and
  safe to run twice (`IF NOT EXISTS`, `IF EXISTS`).
- If it changes something a user would notice, update the relevant
  [SOP](docs/sop/) too. A feature nobody can find is not finished.
- If it touches `api/field_templates.json`, bump its `version`, regenerate
  the title fixture, and run the Android unit tests. Three things read that
  file and only one of them is in Python.
- If it touches `android/`, say which handset or emulator image you ran it
  on and whether `./gradlew test` passed. The app is not built by CI and
  nobody else is going to catch it.

## Security

If you find something with real security consequences, do not open a public
issue. Contact the maintainer directly and give them a reasonable chance to
fix it first.

Be aware of what this project does and does not claim: it is built to run on
a network you control, with no multi-tenancy and no per-record access control.
"Any logged-in user can see the whole case file" is documented behaviour, not
a vulnerability. See
[Security posture](docs/DESIGN.md#security-posture).
