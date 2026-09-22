"""Proposing relationships from what is already in the case file.

No model. No embeddings. Plain SQL over records an analyst has already typed,
looking for the handful of patterns that genuinely imply a connection, and
writing each one into the review queue with the exact evidence that produced
it.

WHY THIS EXISTS WITHOUT A MODEL

The case that prompted it: someone enters their family as Person records and
nothing in the app notices they are obviously related. A model could be asked,
but it does not need to be — four people sharing a surname and an address is
not a subtle inference, it is a join. And this app is built for teams with no
budget for the hardware to run a model, so the feature that answers "who might
be connected" should be the one that works on an empty Raspberry Pi.

WHAT A SIGNAL IS AND IS NOT

Every rule here proposes a *question*, never a fact. Shared surname means "these
two might be family", not "these two are family" — plenty of unrelated people
share a surname, and plenty of families do not. So:

  * Nothing here writes to `relationships`. It writes proposals that a person
    accepts or dismisses, through the same review queue and the same validation
    an analyst's own typing goes through.
  * Every proposal carries the values it matched on, so a reviewer can see
    "surname Smith" or "both recorded as child_of Jane Smith" and judge it
    without leaving the queue.
  * A pair that already has a relationship of any kind is skipped. The point is
    to surface what is missing, not to nag about what is recorded.

CONFIDENCE

The numbers attached here are ordering hints, not probabilities. A shared
unusual contact value is a stronger signal than a shared common surname, and
that is all the ordering is claiming. They are deliberately never shown as
percentages in the UI for that reason.
"""

import os

from db import db_cursor

# Off by default. An instance that has never asked for this should not suddenly
# find its review queue full of proposals about records it entered months ago.
ENABLED = os.environ.get("LINK_SIGNALS_ENABLED", "false").lower() == "true"

# How often the pass runs. There is nothing to gain from running it every poll
# cycle: it looks at the whole case file, and the case file does not change
# that fast.
INTERVAL_SECONDS = int(os.environ.get("LINK_SIGNALS_INTERVAL_SECONDS", "900"))

# Stop proposing once this many are already waiting. A reviewer facing four
# hundred pending suggestions reviews none of them, and a queue that grows
# faster than it is cleared is worse than no queue.
MAX_PENDING = int(os.environ.get("LINK_SIGNALS_MAX_PENDING", "60"))

# Surnames this common carry no signal at all. A shared "Smith" across a large
# case file is noise; the threshold is on how many people in THIS file share it,
# not on any external frequency list, because what is common depends on the
# case.
MAX_SHARED_SURNAME_GROUP = 6

# How many reports two records must both appear in before co-mention is worth
# raising. One shared report is a coincidence of subject matter.
CO_MENTION_MIN_REPORTS = 3

_last_run_at = 0.0


# ---------------------------------------------------------------------------
# The rules
#
# Each returns rows of:
#   (from_id, to_id, relationship_type, confidence, signal_name, evidence_dict)
#
# Ordered pairs are normalised to (lesser id, greater id) in SQL so the same
# pair is never proposed twice in mirror image.
# ---------------------------------------------------------------------------

SHARED_SURNAME_SQL = """
WITH people AS (
    SELECT e.id, e.name,
           -- Last whitespace-separated token, lower-cased. Crude, and
           -- deliberately so: this is a hint for a human, and a full name
           -- parser would be a large amount of machinery to make a guess
           -- slightly better at guessing.
           lower(split_part(trim(e.name), ' ', array_length(string_to_array(trim(e.name), ' '), 1))) AS surname
    FROM entities e
    WHERE e.entity_type = 'person' AND e.is_active
),
named AS (
    SELECT id, name, surname,
           lower(split_part(trim(name), ' ', 1)) AS given
    FROM people
    -- A single-token name has no surname to share. "Ahmed" alone matching
    -- every other "Ahmed" is not a family signal, it is a collision.
    WHERE surname <> '' AND length(surname) > 1
      AND array_length(string_to_array(trim(name), ' '), 1) > 1
),
groups AS (
    SELECT surname FROM named GROUP BY surname
    HAVING count(*) BETWEEN 2 AND %(max_group)s
)
SELECT a.id, b.id, 'family_of', 0.45, 'shared_surname',
       json_build_object('rule', 'shared surname',
                         'surname', a.surname,
                         'matched', json_build_array(a.name, b.name))
FROM named a
JOIN named b ON b.surname = a.surname AND a.id < b.id
JOIN groups g ON g.surname = a.surname
-- Two records with the SAME full name are a suspected duplicate, not a
-- family. The correlation queue already exists for that, and proposing
-- "family_of" between a record and its own double is worse than proposing
-- nothing: it is wrong, and accepting it would write a self-referential
-- edge into the case file.
WHERE a.name <> b.name
  -- Same given name and same surname with different spacing or a middle
  -- initial is the same person again by another route.
  AND a.given <> b.given
"""

# Contact kinds you have to be GIVEN, as opposed to ones anyone can tune to or
# type in. Two people reachable on the same mobile number share a thing; two
# people listening to the same radio frequency share a public channel, and in a
# case file full of standard calling and service frequencies that is nearly
# everyone. The distinction is the difference between a real signal and a queue
# full of "these two both own a radio", so the rule keeps both but scores them
# very differently — see SHARED_CONTACT_SQL.
PRIVATE_CONTACT_KINDS = ("Phone", "Mobile", "Email", "Address", "Messaging", "Social")

SHARED_CONTACT_SQL = """
-- Two records reachable at the same contact value. Much stronger than a shared
-- name: a shared contact value is a shared thing, not a shared word — as long
-- as the thing is actually shared rather than public. A radio frequency, a
-- website or an unclassified "Other" is a channel, not a possession, so it is
-- scored below even the shared-surname rule and worded so a reviewer can see
-- at a glance that it is the weak kind of match.
WITH normalised AS (
    SELECT c.entity_id, c.kind,
           -- Punctuation and spacing stripped so "+1 555 0140" and
           -- "+15550140" are the same value.
           regexp_replace(lower(trim(c.value)), '[^a-z0-9@.]', '', 'g') AS val,
           c.value AS raw,
           (c.kind = ANY(%(private_kinds)s)) AS is_private
    FROM contact_points c
    JOIN entities e ON e.id = c.entity_id AND e.is_active
    WHERE c.value IS NOT NULL AND length(trim(c.value)) > 4
),
shared AS (
    SELECT val FROM normalised GROUP BY val
    -- A value shared by half the case file is a switchboard, not a household.
    HAVING count(DISTINCT entity_id) BETWEEN 2 AND 5
)
SELECT a.entity_id, b.entity_id, 'associate_of',
       CASE WHEN a.is_private AND b.is_private THEN 0.7 ELSE 0.35 END,
       'shared_contact',
       json_build_object('rule',
                         CASE WHEN a.is_private AND b.is_private
                              THEN 'same contact details on file'
                              ELSE 'same ' || lower(a.kind) || ' channel — a shared channel, not a shared number' END,
                         'kind', a.kind,
                         'value', a.raw)
FROM normalised a
JOIN normalised b ON b.val = a.val AND a.entity_id < b.entity_id
JOIN shared s ON s.val = a.val
"""

IMPLIED_SIBLING_SQL = """
-- Two people both recorded as a child of the same person. This one is not a
-- guess about the world, it is a fact already in the file that nobody has
-- written down as an edge.
SELECT c1.from_entity_id, c2.from_entity_id, 'sibling_of', 0.8, 'implied_sibling',
       json_build_object('rule', 'both recorded as a child of the same person',
                         'parent', p.name)
FROM relationships c1
JOIN relationships c2
  ON c2.to_entity_id = c1.to_entity_id
 AND c2.relationship_type = 'child_of'
 AND c1.from_entity_id < c2.from_entity_id
JOIN entities p ON p.id = c1.to_entity_id
JOIN entities a ON a.id = c1.from_entity_id AND a.is_active
JOIN entities b ON b.id = c2.from_entity_id AND b.is_active
WHERE c1.relationship_type = 'child_of'
"""

SHARED_HOUSEHOLD_SQL = """
-- Two people both related to the same Location by located_at. Weaker than a
-- shared contact value — an office is a location too — so it proposes the
-- neutral "associate_of" rather than anything about family.
SELECT r1.from_entity_id, r2.from_entity_id, 'associate_of', 0.4, 'shared_location',
       json_build_object('rule', 'both recorded at the same place',
                         'place', l.name)
FROM relationships r1
JOIN relationships r2
  ON r2.to_entity_id = r1.to_entity_id
 AND r2.relationship_type = 'located_at'
 AND r1.from_entity_id < r2.from_entity_id
JOIN entities l ON l.id = r1.to_entity_id AND l.entity_type = 'location'
JOIN entities a ON a.id = r1.from_entity_id AND a.entity_type = 'person' AND a.is_active
JOIN entities b ON b.id = r2.from_entity_id AND b.entity_type = 'person' AND b.is_active
WHERE r1.relationship_type = 'located_at'
"""

CO_MENTION_SQL = """
-- Two PEOPLE who keep turning up in the same reports. Says nothing about what
-- the connection IS, which is why it proposes associate_of and leans on the
-- reviewer to say more.
--
-- People only, deliberately. Unscoped, this rule measures how reports are
-- written rather than anything about the world: a report names the place it
-- happened and the groups involved, so every location in a busy case file
-- co-occurs with every organization operating there, and the rule proposes
-- "associate_of" between a highway interchange and a militia. Worse, two
-- organizations fighting each other appear together constantly — co-mention
-- would read open conflict as association. Person-to-person co-occurrence is
-- the version of this that means something: two names that keep appearing in
-- the same reporting are two people worth looking at together.
WITH pairs AS (
    SELECT re1.entity_id AS a_id, re2.entity_id AS b_id,
           count(*) AS shared_reports,
           json_agg(r.title ORDER BY r.created_at DESC) AS titles
    FROM report_entities re1
    JOIN report_entities re2
      ON re2.report_id = re1.report_id AND re1.entity_id < re2.entity_id
    JOIN reports r ON r.id = re1.report_id
    JOIN entities a ON a.id = re1.entity_id AND a.is_active AND a.entity_type = 'person'
    JOIN entities b ON b.id = re2.entity_id AND b.is_active AND b.entity_type = 'person'
    GROUP BY re1.entity_id, re2.entity_id
    HAVING count(*) >= %(min_reports)s
)
SELECT a_id, b_id, 'associate_of', 0.5, 'co_mention',
       json_build_object('rule', 'named together in several reports',
                         'reports', shared_reports,
                         'examples', titles)
FROM pairs
"""

RULES = (
    ("implied_sibling", IMPLIED_SIBLING_SQL, {}),
    ("shared_contact", SHARED_CONTACT_SQL, {"private_kinds": list(PRIVATE_CONTACT_KINDS)}),
    ("co_mention", CO_MENTION_SQL, {"min_reports": CO_MENTION_MIN_REPORTS}),
    ("shared_surname", SHARED_SURNAME_SQL, {"max_group": MAX_SHARED_SURNAME_GROUP}),
    ("shared_location", SHARED_HOUSEHOLD_SQL, {}),
)


def _pending_count(cur) -> int:
    cur.execute("SELECT count(*) FROM extraction_suggestions "
                "WHERE status = 'pending' AND source = 'signal'")
    return cur.fetchone()[0]


def _propose(cur, entry) -> bool:
    """Insert one merged proposal for a pair, unless that pair is already
    connected, already proposed, or has been dismissed before.

    Returns True if a row was actually written.
    """
    from_id = entry["from_id"]
    to_id = entry["to_id"]
    rel_type = entry["relationship_type"]
    confidence = entry["confidence"]
    signals = entry["signals"]

    # Already connected in either direction, by any relationship type. The
    # queue is for what is missing; a pair an analyst has already described
    # does not need a machine's opinion about it.
    cur.execute(
        """
        SELECT 1 FROM relationships
        WHERE (from_entity_id = %s AND to_entity_id = %s)
           OR (from_entity_id = %s AND to_entity_id = %s)
        LIMIT 1
        """,
        (from_id, to_id, to_id, from_id),
    )
    if cur.fetchone():
        return False

    # Dismissed before. Re-raising something a person has already said no to is
    # how a review queue teaches people to ignore it — and since a pair is now
    # proposed once with all its evidence, "no" means no about the pair rather
    # than about one rule.
    cur.execute(
        """
        SELECT 1 FROM extraction_suggestions
        WHERE source = 'signal' AND status = 'rejected'
          AND suggested_from_entity_id = %s AND suggested_to_entity_id = %s
        LIMIT 1
        """,
        (from_id, to_id),
    )
    if cur.fetchone():
        return False

    cur.execute(
        """
        INSERT INTO extraction_suggestions
            (source, suggestion_type, suggested_relationship_type,
             suggested_from_entity_id, suggested_to_entity_id,
             suggested_from_name, suggested_to_name,
             confidence, evidence)
        SELECT 'signal', 'relationship', %(rel)s, %(from_id)s, %(to_id)s,
               a.name, b.name, %(confidence)s, %(evidence)s::jsonb
        FROM entities a, entities b
        WHERE a.id = %(from_id)s AND b.id = %(to_id)s
        ON CONFLICT DO NOTHING
        """,
        {
            "rel": rel_type, "from_id": from_id, "to_id": to_id,
            "confidence": confidence,
            "evidence": _evidence_json(entry),
        },
    )
    return cur.rowcount > 0


def _evidence_json(entry: dict) -> str:
    """Every rule that fired on this pair, in one document.

    `signal` and `rule` name the strongest one so the queue has a short label,
    and `all` carries the lot — a reviewer looking at two people who share a
    surname AND an address AND four reports should be able to see all three
    without opening anything."""
    import json
    reasons = entry["evidence"]
    strongest = reasons[0] if reasons else {}
    # One rule can fire twice on the same pair — two people who share both an
    # address and a phone number match shared_contact once per contact. Both
    # belong in `all` (they are two different pieces of evidence) but the
    # short label list should read "shared_contact, shared_surname", not
    # "shared_contact, shared_contact, shared_surname".
    names, seen = [], set()
    for name in entry["signals"]:
        if name not in seen:
            seen.add(name)
            names.append(name)
    return json.dumps({
        "signal": strongest.get("signal"),
        "rule": strongest.get("rule"),
        "signals": names,
        "all": reasons,
    })


def run_link_signals(force: bool = False) -> bool:
    """One pass over every rule. Returns True if anything was proposed.

    Follows the same convention as the other poll-loop jobs: cheap to call
    often, does its own rate limiting, and never raises — a bad rule must not
    take down the worker that does the real work.
    """
    global _last_run_at
    if not ENABLED and not force:
        return False

    import time
    now = time.monotonic()
    if not force and _last_run_at and (now - _last_run_at) < INTERVAL_SECONDS:
        return False
    _last_run_at = now

    proposed = 0
    try:
        with db_cursor(commit=True) as cur:
            if _pending_count(cur) >= MAX_PENDING:
                # Deliberately silent after the first time: a worker that logs
                # this every fifteen minutes is a worker nobody reads the logs
                # of.
                return False

            # Collect every rule's candidates first, then merge by pair. Three
            # rules firing on the same two people is a stronger reason to look,
            # not three things to review — and a queue that shows the same
            # couple three times is one a person stops reading. The pair keeps
            # the relationship type of its highest-confidence rule and carries
            # every rule's evidence, so nothing is lost by merging.
            merged: dict = {}
            for name, sql, params in RULES:
                cur.execute(sql, params)
                for from_id, to_id, rel_type, confidence, signal, evidence in cur.fetchall():
                    key = (from_id, to_id)
                    entry = merged.get(key)
                    if entry is None:
                        merged[key] = {
                            "from_id": from_id, "to_id": to_id,
                            "relationship_type": rel_type, "confidence": confidence,
                            "signals": [signal], "evidence": [dict(evidence, signal=signal)],
                        }
                        continue
                    entry["signals"].append(signal)
                    entry["evidence"].append(dict(evidence, signal=signal))
                    if confidence > entry["confidence"]:
                        entry["relationship_type"] = rel_type
                        entry["confidence"] = confidence

            for entry in sorted(merged.values(), key=lambda e: -e["confidence"]):
                if _pending_count(cur) >= MAX_PENDING:
                    break
                if _propose(cur, entry):
                    proposed += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[worker] link signals failed: {exc}", flush=True)
        return False

    if proposed:
        print(f"[worker] proposed {proposed} possible link(s) for review", flush=True)
    return bool(proposed)
