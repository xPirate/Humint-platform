"""RSS/Atom feed ingestion.

Feeds are entirely user-managed (see api/rss.py) — nothing ships built in.
Each active feed is polled at most once every RSS_POLL_INTERVAL_SECONDS,
tracked per-feed via rss_feeds.last_polled_at rather than a second sleep
timer in main.py: process_one_feed() claims one due feed per call with the
same "FOR UPDATE SKIP LOCKED" pattern main.py already uses for attachments,
so it slots into the existing poll loop without needing its own clock.

Events = ingested information, Reports = analyst-generated: a new feed item
becomes a plain Event entity directly, not routed through
extraction_suggestions. That review queue exists to gate an LLM's guess at
what a scanned document might say; an RSS item is deterministic,
already-published text from a feed the analyst chose and configured
themselves, so the analyst already vetted the *source*. The Event this
creates is a completely ordinary entity afterward — edit or archive it like
any other Event if a feed turns out to be noisy. It also embeds and
cross-correlates against Reports exactly like any other Event, once
correlate.py picks it up on a later pass (see process_one_entity_embedding's
event-type branch there) — that's what lets an analyst's earlier Report get
flagged against a later-ingested Event for review.
"""

import re
from datetime import datetime, timedelta, timezone
from time import mktime

import feedparser

import audit
from db import db_cursor
from idgen import generate_id

MAX_TITLE_LENGTH = 256
MAX_DESCRIPTION_LENGTH = 4000


def _clean_text(html_or_text) -> str | None:
    """A light strip of markup some feeds embed in their summary/description
    field — not a full HTML sanitizer, and it doesn't need to be one: nothing
    from here ever reaches innerHTML unescaped on the frontend (see
    escapeHtml usage throughout app.js), so anything left over is cosmetic
    at worst, not a security boundary."""
    if not html_or_text:
        return None
    text = re.sub(r"<[^>]+>", " ", html_or_text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_DESCRIPTION_LENGTH] if text else None


def _item_guid(entry) -> str | None:
    """Prefer the feed's own item id/guid; fall back to the link for feeds
    that omit one entirely. Returns None only for a genuinely unusable entry
    (no stable identifier at all), which is skipped rather than guessed at —
    inventing one risks silently re-ingesting the same item as a "new" one
    every poll."""
    return entry.get("id") or entry.get("guid") or entry.get("link") or None


def _item_published(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)
    except (ValueError, OverflowError):
        return None


def _claim_due_feed(cur, poll_interval_seconds: int):
    cur.execute(
        """
        SELECT id, url, label, max_age_days, max_items_per_poll FROM rss_feeds
        WHERE is_active = TRUE
          AND (last_polled_at IS NULL OR last_polled_at < now() - (%s || ' seconds')::interval)
        ORDER BY last_polled_at NULLS FIRST
        LIMIT 1 FOR UPDATE SKIP LOCKED
        """,
        (poll_interval_seconds,),
    )
    return cur.fetchone()


def _apply_limits(entries, max_age_days, max_items_per_poll):
    """Narrow a feed's entries to what this feed is allowed to take.

    Returns (entries, skipped_as_too_old, trimmed_by_the_cap).

    Age first, then the count, and the count keeps the NEWEST -- taking the
    first N in feed order would be arbitrary, since feeds are not reliably
    sorted, and on an archive dump it would take the oldest items rather than
    the ones somebody actually wanted.

    An entry with no usable date is kept when a max age is set. A feed that
    publishes no timestamps would otherwise be silently ingested as nothing at
    all, which looks exactly like a broken feed; the item cap is the guard for
    that case, and it is why both limits exist.
    """
    skipped_old = 0
    if max_age_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        kept = []
        for entry in entries:
            published = _item_published(entry)
            if published is not None and published < cutoff:
                skipped_old += 1
                continue
            kept.append(entry)
        entries = kept

    trimmed = 0
    if max_items_per_poll and len(entries) > max_items_per_poll:
        # Undated items sort last, so a dated feed keeps its newest and an
        # undated one keeps its feed order -- neither is surprising.
        entries = sorted(
            entries,
            key=lambda e: _item_published(e) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True)
        trimmed = len(entries) - max_items_per_poll
        entries = entries[:max_items_per_poll]

    return entries, skipped_old, trimmed


def _ingest_entry(cur, feed_id: int, feed_label: str, entry) -> bool:
    """Returns True if this entry resulted in a new Event (or was recorded
    as skipped), False if it was already-seen and nothing needed doing."""
    guid = _item_guid(entry)
    if not guid:
        return False

    cur.execute("SELECT 1 FROM rss_items WHERE feed_id = %s AND guid = %s", (feed_id, guid))
    if cur.fetchone() is not None:
        return False

    title = (entry.get("title") or "Untitled item").strip()[:MAX_TITLE_LENGTH]
    description = _clean_text(entry.get("summary") or entry.get("description"))
    link = entry.get("link")
    published_at = _item_published(entry)

    entity_id = generate_id("event", title)
    full_description = description or ""
    if link:
        full_description = f"{full_description}\n\nSource: {link}".strip()
    full_description = full_description[:MAX_DESCRIPTION_LENGTH] or None

    cur.execute(
        "INSERT INTO entities (id, entity_type, name, description) VALUES (%s, 'event', %s, %s)",
        (entity_id, title, full_description),
    )
    # event_type doubles as provenance here ("RSS: <feed label>") so an
    # analyst can see at a glance, right in the existing Event details UI,
    # that this Event came from a feed rather than being hand-entered —
    # no separate "source" field/UI needed for that.
    cur.execute(
        "INSERT INTO event_details (entity_id, event_type, started_at) VALUES (%s, %s, %s)",
        (entity_id, f"RSS: {feed_label}", published_at),
    )
    cur.execute(
        "INSERT INTO rss_items (feed_id, guid, entity_id) VALUES (%s, %s, %s)",
        (feed_id, guid, entity_id),
    )
    # Recorded with actor_kind='system': an Event that appears in the case
    # with no audit entry at all would look, to anyone reading the trail,
    # like a record that arrived from nowhere.
    audit.record("entity.create", actor_kind="system", object_type="entity",
                 object_id=entity_id, object_label=title,
                 detail={"entity_type": "event", "source": "rss", "feed": feed_label})
    return True


def process_one_feed(poll_interval_seconds: int) -> bool:
    """Returns True if it did work (a feed was due, whether or not that
    feed's fetch succeeded or it had any new items), so main() knows not to
    sleep while there's a backlog of due feeds."""
    with db_cursor(commit=True) as cur:
        row = _claim_due_feed(cur, poll_interval_seconds)
        if row is None:
            return False
        feed_id, url, label, max_age_days, max_items_per_poll = row
        # Stamp last_polled_at immediately, before the network fetch — a
        # slow or hanging feed shouldn't leave this row looking perpetually
        # "due" to every loop iteration in the meantime.
        cur.execute("UPDATE rss_feeds SET last_polled_at = now() WHERE id = %s", (feed_id,))

    try:
        parsed = feedparser.parse(url)
        # feedparser's `bozo` flag covers malformed XML, not HTTP-level
        # failures — a 404 or 500 parses "successfully" into an empty feed
        # with bozo left False, so that needs its own check via `status`
        # (only present for http(s) URLs, never for a local file/string).
        # Entries surviving a bozo parse are still usable in practice (real
        # feeds are often a little malformed), so only treat it as a
        # failure when nothing came through either way.
        status = parsed.get("status")
        if status is not None and status >= 400:
            raise ValueError(f"HTTP {status} fetching feed")
        if parsed.get("bozo") and not parsed.entries:
            raise ValueError(str(parsed.get("bozo_exception") or "feed could not be parsed"))
        entries = parsed.entries
    except Exception as exc:
        with db_cursor(commit=True) as cur:
            cur.execute("UPDATE rss_feeds SET last_error = %s WHERE id = %s", (str(exc)[:2000], feed_id))
        print(f"[worker] rss feed {feed_id} ({label}): poll failed: {exc}", flush=True)
        return True

    entries, skipped_old, trimmed = _apply_limits(entries, max_age_days, max_items_per_poll)

    created = 0
    with db_cursor(commit=True) as cur:
        cur.execute("UPDATE rss_feeds SET last_error = NULL WHERE id = %s", (feed_id,))
        for entry in entries:
            if _ingest_entry(cur, feed_id, label, entry):
                created += 1

    if created or skipped_old or trimmed:
        note = f"[worker] rss feed {feed_id} ({label}): ingested {created} new item(s) as Events"
        if skipped_old:
            note += f", skipped {skipped_old} older than {max_age_days}d"
        if trimmed:
            note += f", left {trimmed} over the {max_items_per_poll}-item cap"
        print(note, flush=True)
    return True
