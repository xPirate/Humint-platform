"""HUMINT Platform — background worker.

Four jobs on one poll loop, run sequentially rather than as separate
processes/threads — all are read-mostly against small tables at personal/
lab scale, so there's no real concurrency benefit to splitting them, and one
loop is simpler to reason about:

  1. Extraction: OCR/text-extract newly uploaded attachments, then run
     Ollama entity/relationship extraction on the resulting text into
     extraction_suggestions for analyst review (see api/extraction.py for
     the accept/reject endpoints — nothing here ever writes to entities/
     relationships directly).
  2. RSS ingestion (see rss_ingest.py): polls each active, user-added feed
     at most once every RSS_POLL_INTERVAL_SECONDS and turns new items
     directly into Event entities — always runs, independent of Ollama,
     since there's no LLM step involved in taking a feed item at face value.
  3. Entity correlation and 4. Report correlation (see correlate.py): embeds
     entities/reports and flags likely duplicates/links — including the
     cross-type Report<->Event pairing RSS ingestion feeds into — into
     correlation_suggestions (see api/correlation.py for confirm/dismiss).
     Both are skipped entirely when OLLAMA_EMBED_MODEL isn't configured —
     see correlation_enabled below — rather than looping on embed calls
     that will only ever return None.
  6. Audit retention (see audit_prune.py): deletes audit_log rows older than
     AUDIT_RETENTION_DAYS, at most once an hour. Does nothing at all unless
     that's set to a positive number — the default keeps everything.
  5. Geocoding (see geocode.py): turns a Location's address into lat/lng
     (and the Maidenhead grid locator that falls out of any coordinates —
     see geo.py) via the public OpenStreetMap Nominatim API by default,
     self-throttled well under that service's ~1 request/second usage
     policy regardless of how many addresses are queued at once. Always
     runs, independent of Ollama — there's no LLM step involved, same
     reasoning as RSS ingestion above.

Ollama configuration (base URL, model, embed model, enabled, timeout) is
re-read from .env plus any Admin-page override (see settings.py,
app_settings in db/init.sql) at the top of every iteration below, not once
at startup — so changing the model from the Admin page takes effect within
one poll cycle, no `docker compose restart worker` needed.

Crash recovery note: if the worker dies mid-extraction (after claiming an
attachment as 'processing' but before finishing it), that attachment stays
stuck in 'processing' with no automatic timeout/requeue — there's no
processing-started-at column to judge staleness against. At personal/lab
scale with a single worker container this is rare enough that a manual
`UPDATE attachments SET extraction_status = 'pending' WHERE id = ...` is an
acceptable recovery path rather than building a staleness-tracking system
for it. Documented in README.md as well. The same "just re-run it" logic
applies to a feed claimed but never finished polling — it simply looks due
again once RSS_POLL_INTERVAL_SECONDS has passed.
"""

import json
import os
import time

from audit_prune import (RETENTION_DAYS as AUDIT_RETENTION_DAYS, prune_audit_log,
                         prune_ollama_calls)
from link_signals import run_link_signals
from map_download import start_download_thread
from correlate import process_one_entity_embedding, process_one_report_embedding
from db import db_cursor
from extract import extract_text
from geocode import GEOCODE_ENABLED, process_one_geocode
import ollama_usage
from ollama_client import OllamaClient
from rss_ingest import process_one_feed
from settings import get_effective_ollama_config

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")
POLL_INTERVAL_SECONDS = int(os.environ.get("WORKER_POLL_INTERVAL_SECONDS", "20"))
CORRELATION_SIMILARITY_THRESHOLD = float(os.environ.get("CORRELATION_SIMILARITY_THRESHOLD", "0.88"))
# How often each individual feed gets re-polled — tracked per-feed (see
# rss_ingest._claim_due_feed), not a separate sleep timer, so this is
# independent of POLL_INTERVAL_SECONDS above. 15 minutes is a reasonable
# default for news feeds; lower it for something closer to real-time, or
# raise it for a slow-moving/low-volume source.
RSS_POLL_INTERVAL_SECONDS = int(os.environ.get("RSS_POLL_INTERVAL_SECONDS", "900"))

# How many matches one newly-embedded record may propose. Everything above the
# similarity threshold used to be flagged, which is quadratic: 200 records that
# resemble each other -- a feed's worth of news items, say -- produced over two
# thousand pairs and a review queue nobody could work through.
#
# Ten is generous. These arrive sorted by similarity, so a real duplicate is
# near the top or it is not there at all; the hundredth-best match for a news
# item was never going to be accepted by anybody.
CORRELATION_MAX_MATCHES_PER_RECORD = int(
    os.environ.get("CORRELATION_MAX_MATCHES_PER_RECORD", "10"))

# Module-level so process_one_attachment() (below) can keep referencing a
# plain `ollama` global without threading it through as a parameter — but
# unlike everything else built at import time in this file, this gets
# *reassigned* at the top of every poll loop iteration in main(), rebuilt
# from get_effective_ollama_config() (.env, overridden by whatever's saved
# in app_settings via the Admin page). That's what makes an Admin-page
# change to the Ollama model/URL/etc. take effect within one poll cycle
# instead of needing `docker compose restart worker`. The construction here
# just gives it a valid value before the loop's first iteration.
def _record_ollama_call(**call):
    """Everything the worker asks of Ollama is batch work — an attachment in
    the queue, a correlation pass — so no user_id. Attributing it to whoever
    happened to upload the document would be wrong: they did not ask for the
    extraction and are not waiting on it."""
    ollama_usage.record("worker", **call)


ollama = OllamaClient(**get_effective_ollama_config(), on_call=_record_ollama_call)


def _claim_pending_attachment(cur):
    """Marks one pending attachment 'processing' and returns
    (id, storage_path, filename), or None if there's nothing to do.
    FOR UPDATE SKIP LOCKED means a second worker replica (never actually
    run at this scale, but cheap to get right) would skip past a row
    another worker already grabbed instead of blocking or double-processing it."""
    cur.execute(
        "SELECT id, storage_path, filename FROM attachments "
        "WHERE extraction_status = 'pending' "
        "ORDER BY uploaded_at LIMIT 1 FOR UPDATE SKIP LOCKED"
    )
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("UPDATE attachments SET extraction_status = 'processing' WHERE id = %s", (row[0],))
    return row


def process_one_attachment() -> bool:
    """Returns True if it did work, so main() can keep draining a backlog
    without sleeping between items and only pause once caught up."""
    with db_cursor(commit=True) as cur:
        row = _claim_pending_attachment(cur)
        if row is None:
            return False
        attachment_id, storage_path, filename = row

    abs_path = os.path.join(UPLOAD_DIR, storage_path)
    text, error = extract_text(abs_path, filename)

    if error:
        with db_cursor(commit=True) as cur:
            cur.execute(
                "UPDATE attachments SET extraction_status = 'failed', extraction_error = %s WHERE id = %s",
                (error[:2000], attachment_id),
            )
        print(f"[worker] attachment {attachment_id} extraction failed: {error}", flush=True)
        return True

    with db_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE attachments SET extraction_status = 'done', extracted_text = %s WHERE id = %s",
            (text, attachment_id),
        )

    if text.strip() and ollama.enabled:
        suggestions = ollama.extract_entities(text)
        if suggestions and (suggestions["entities"] or suggestions["relationships"]):
            _store_suggestions(attachment_id, suggestions)
            print(
                f"[worker] attachment {attachment_id}: "
                f"{len(suggestions['entities'])} entity suggestion(s), "
                f"{len(suggestions['relationships'])} relationship suggestion(s)",
                flush=True,
            )

    return True


def _store_suggestions(attachment_id: int, suggestions: dict) -> None:
    with db_cursor(commit=True) as cur:
        for e in suggestions["entities"]:
            cur.execute(
                """
                INSERT INTO extraction_suggestions
                    (attachment_id, suggestion_type, suggested_entity_type, suggested_name, details, confidence)
                VALUES (%s, 'entity', %s, %s, %s, %s)
                """,
                (attachment_id, e["entity_type"], e["name"], json.dumps(e["details"]), e["confidence"]),
            )
        for r in suggestions["relationships"]:
            cur.execute(
                """
                INSERT INTO extraction_suggestions
                    (attachment_id, suggestion_type, suggested_relationship_type,
                     suggested_from_name, suggested_to_name, confidence)
                VALUES (%s, 'relationship', %s, %s, %s, %s)
                """,
                (attachment_id, r["relationship_type"], r["from_name"], r["to_name"], r["confidence"]),
            )


def _refresh_ollama_client() -> bool:
    """Rebuilds the module-level `ollama` client from the current effective
    config (.env + any app_settings override) and returns whether
    correlation should run this iteration. Cheap enough to call every
    iteration: get_effective_ollama_config() is one indexed single-row
    lookup, and OllamaClient itself just holds a few attributes — there's no
    connection or session to tear down/recreate."""
    global ollama
    ollama = OllamaClient(**get_effective_ollama_config(), on_call=_record_ollama_call)
    return ollama.enabled and bool(ollama.embed_model)


def main():
    print("[worker] starting, poll interval =", POLL_INTERVAL_SECONDS, "s", flush=True)
    print("[worker] ollama enabled =", ollama.enabled, "model =", ollama.model, flush=True)
    print("[worker] rss poll interval =", RSS_POLL_INTERVAL_SECONDS, "s per feed", flush=True)
    # Map packs run on their own thread: every other job here finishes in
    # seconds, and a pack is hours of small HTTP requests. In the loop it
    # would mean an uploaded document waiting behind a basemap.
    start_download_thread()
    print("[worker] geocoding enabled =", GEOCODE_ENABLED, flush=True)
    print("[worker] audit retention =",
          f"{AUDIT_RETENTION_DAYS} days" if AUDIT_RETENTION_DAYS > 0 else "keep forever", flush=True)
    print(
        "[worker] Ollama settings (base URL/model/embed model/enabled/timeout) are "
        "re-read from .env + Admin-page overrides every poll cycle — changes there "
        "take effect without a restart",
        flush=True,
    )
    while True:
        try:
            correlation_enabled = _refresh_ollama_client()
            did_work = process_one_attachment()
            did_work = process_one_feed(RSS_POLL_INTERVAL_SECONDS) or did_work
            did_work = process_one_geocode() or did_work
            did_work = prune_audit_log() or did_work
            did_work = prune_ollama_calls() or did_work
            # Needs no model, so it runs whatever Ollama is doing.
            did_work = run_link_signals() or did_work
            if correlation_enabled:
                did_work = process_one_entity_embedding(ollama, CORRELATION_SIMILARITY_THRESHOLD,
                                                       CORRELATION_MAX_MATCHES_PER_RECORD) or did_work
                did_work = process_one_report_embedding(ollama, CORRELATION_SIMILARITY_THRESHOLD,
                                                       CORRELATION_MAX_MATCHES_PER_RECORD) or did_work
            if not did_work:
                time.sleep(POLL_INTERVAL_SECONDS)
        except Exception as exc:
            print(f"[worker] error in poll loop: {exc}", flush=True)
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
