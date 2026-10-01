"""Cross-entity and cross-report correlation.

Embeds entity/report text via Ollama (only when OLLAMA_EMBED_MODEL is
configured — see main.py's correlation_enabled gate) and flags pairs above
CORRELATION_SIMILARITY_THRESHOLD into correlation_suggestions for analyst
review. Like extraction, this never merges, links, or deletes anything on
its own — see api/correlation.py for the confirm/dismiss endpoints, and
note that "confirm" there only acknowledges the match; it does not merge
the two records. Building an actual entity-merge feature (reassigning
relationships/reports/attachments from one entity onto another) is real
work left for later — flagging it here rather than quietly half-building it.

Documented simplification: an entity/report is embedded once, when its
embedding column is still NULL. Editing it later does not trigger
re-embedding — there is no "embedded at revision N" tracking. Clear the
embedding column by hand if a specific record's embedding needs a refresh
after a substantial edit.

Entities are still only ever compared against other entities of the SAME
type — a Person and a Location being textually similar isn't a meaningful
signal the way two Person records both named "R. Okonjo" is — with one
deliberate exception: Event entities are ALSO compared against Reports.
That's the report_event pairing added for RSS ingestion (see
rss_ingest.py, which creates Events straight from feed items): the use case
is an analyst writing a Report about something before it's publicly
reported, and later an RSS feed ingests a news item covering the same
thing as an Event — surfacing that pair in the Correlation Queue is what
lets the analyst notice and link them, in either time order. See
_flag_report_event_pairs below.
"""

import json

from db import db_cursor

ENTITY_TEXT_FIELDS = ("name", "description")


def cosine_similarity(a: list, b: list) -> float:
    """Plain-Python cosine similarity — no numpy dependency, which is the
    right call here: vectors are at most a few thousand floats, compared
    against a modest number of same-type records at personal/lab scale."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def build_entity_text(name: str, description: str | None) -> str:
    return f"{name}. {description}" if description else name


def build_report_text(title: str, body_markdown: str | None) -> str:
    # Capped, not the full body — this is a similarity signal, not a
    # document store, and a very long report shouldn't dominate the
    # embedding call's cost/latency.
    text = title
    if body_markdown:
        text += ". " + body_markdown[:1500]
    return text


def _normalize_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _score_candidates(vec, candidates, threshold: float, limit: int = None):
    """The best matches for one record: (candidate_id, score), strongest first.

    Shared by both the same-type and cross-type flaggers below, so there is one
    place that decodes an embedding, compares it and filters.

    **`limit` is the thing that keeps the review queue finite.** This used to
    return everything above the threshold, which is quadratic in disguise:
    every record is compared against every other one, so N records can produce
    up to N*(N-1)/2 suggestions. That is invisible at twenty records and
    crippling at two hundred -- 200 news items from one feed, which are alike
    in wording by their nature, generated over two thousand pairs and made the
    queue unusable.

    Capping per record bounds the queue at roughly N * limit instead, and costs
    nothing real: these come back sorted by similarity, and the 140th best
    match for a news item was never going to be accepted. A genuine duplicate
    is at the top of this list or it is not in it.
    """
    scored = []
    for candidate_id, embedding_json in candidates:
        try:
            candidate_vec = json.loads(embedding_json)
        except (TypeError, ValueError):
            continue
        score = cosine_similarity(vec, candidate_vec)
        if score >= threshold:
            scored.append((candidate_id, score))
    # Tie-broken by id so a run over identical scores is stable rather than
    # returning a different ten each time the same record is re-embedded.
    scored.sort(key=lambda pair: (-pair[1], str(pair[0])))
    return scored[:limit] if limit else scored


def _flag_pairs(cur, subject_type: str, subject_id: str, candidates, vec,
                threshold: float, limit: int = None) -> int:
    flagged = 0
    for other_id, score in _score_candidates(vec, candidates, threshold, limit):
        a, b = _normalize_pair(subject_id, other_id)
        cur.execute(
            """
            INSERT INTO correlation_suggestions (subject_type, subject_a_id, subject_b_id, similarity_score)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (subject_type, subject_a_id, subject_b_id) DO NOTHING
            """,
            (subject_type, a, b, score),
        )
        flagged += 1
    return flagged


def _flag_report_event_pairs(cur, fixed_side: str, fixed_id: str, candidates, vec,
                             threshold: float, limit: int = None) -> int:
    """Cross-type version of _flag_pairs for the report<->Event pairing.
    Unlike same-type pairs, roles here are fixed rather than sorted:
    subject_a_id is always a report id, subject_b_id is always an Event
    entity id (see the correlation_suggestions comment in db/init.sql).
    `fixed_side` says which role `fixed_id` (the record just embedded) plays
    — 'report' or 'event' — since this is called from both directions:
    a newly-embedded Report comparing against existing Event entities, and a
    newly-embedded Event entity comparing against existing Reports."""
    flagged = 0
    for other_id, score in _score_candidates(vec, candidates, threshold, limit):
        report_id, event_id = (fixed_id, other_id) if fixed_side == "report" else (other_id, fixed_id)
        cur.execute(
            """
            INSERT INTO correlation_suggestions (subject_type, subject_a_id, subject_b_id, similarity_score)
            VALUES ('report_event', %s, %s, %s)
            ON CONFLICT (subject_type, subject_a_id, subject_b_id) DO NOTHING
            """,
            (report_id, event_id, score),
        )
        flagged += 1
    return flagged


def process_one_entity_embedding(ollama, threshold: float, limit: int = None) -> bool:
    """Returns True if it did work (embedded something, whether or not that
    led to any flags), so main() knows not to sleep while there's a backlog."""
    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT id, entity_type, name, description FROM entities "
            "WHERE embedding IS NULL AND is_active = TRUE "
            "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        row = cur.fetchone()
        if row is None:
            return False
        entity_id, entity_type, name, description = row

    vec = ollama.embed(build_entity_text(name, description))
    if vec is None:
        # Leave embedding NULL so this entity is retried on a later pass
        # instead of being marked done with nothing — a transient Ollama
        # outage shouldn't permanently exclude a record from correlation.
        return False

    with db_cursor(commit=True) as cur:
        cur.execute("UPDATE entities SET embedding = %s WHERE id = %s", (json.dumps(vec), entity_id))
        cur.execute(
            "SELECT id, embedding FROM entities WHERE entity_type = %s AND id != %s "
            "AND is_active = TRUE AND embedding IS NOT NULL",
            (entity_type, entity_id),
        )
        candidates = cur.fetchall()
        flagged = _flag_pairs(cur, "entity", entity_id, candidates, vec, threshold, limit)

        # Event is the one entity type that also gets compared across type,
        # against Reports — see the module docstring and rss_ingest.py.
        if entity_type == "event":
            cur.execute("SELECT id, embedding FROM reports WHERE embedding IS NOT NULL")
            report_candidates = cur.fetchall()
            flagged += _flag_report_event_pairs(cur, "event", entity_id, report_candidates,
                                                vec, threshold, limit)

    if flagged:
        print(f"[worker] entity {entity_id}: flagged {flagged} likely match(es) for review", flush=True)
    return True


def process_one_report_embedding(ollama, threshold: float, limit: int = None) -> bool:
    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT id, title, body_markdown FROM reports "
            "WHERE embedding IS NULL ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        row = cur.fetchone()
        if row is None:
            return False
        report_id, title, body_markdown = row

    vec = ollama.embed(build_report_text(title, body_markdown))
    if vec is None:
        return False

    with db_cursor(commit=True) as cur:
        cur.execute("UPDATE reports SET embedding = %s WHERE id = %s", (json.dumps(vec), report_id))
        cur.execute("SELECT id, embedding FROM reports WHERE id != %s AND embedding IS NOT NULL", (report_id,))
        candidates = cur.fetchall()
        flagged = _flag_pairs(cur, "report", report_id, candidates, vec, threshold, limit)

        # Also compare against every already-embedded Event entity — the
        # other direction of the report_event pairing (see module docstring):
        # this covers an analyst's Report written BEFORE the matching Event
        # was later ingested from a feed.
        cur.execute(
            "SELECT id, embedding FROM entities WHERE entity_type = 'event' "
            "AND is_active = TRUE AND embedding IS NOT NULL"
        )
        event_candidates = cur.fetchall()
        flagged += _flag_report_event_pairs(cur, "report", report_id, event_candidates,
                                            vec, threshold, limit)

    if flagged:
        print(f"[worker] report {report_id}: flagged {flagged} likely match(es) for review", flush=True)
    return True
