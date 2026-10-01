"""Folding duplicate records into one.

WHY THIS EXISTS

Extraction produces duplicates. The same person is named four ways across six
documents; a single address arrives as a street, a town and a postcode because
the model read them as three places. The correlation queue has always been able
to *say* two records look like the same thing, but confirming that only
recorded an opinion — the analyst still had to reassign every relationship,
report link and attachment by hand, which nobody does, so the file fills up
with near-duplicates instead.

WHAT A MERGE IS

One survivor, one or more records folded into it, and everything that pointed
at the losers now points at the survivor. The losers are archived, not deleted,
and each carries a pointer to where it went — history references them (an old
report link, last month's exported PDF, an audit entry) and a dead end is worse
than a redirect.

THE FIELD RULE

The survivor wins. Any field it left blank is filled from the losers, in the
order they were given. This is deliberately not a field-by-field negotiation:
with five copies of the same record that is a lot of clicking to reach an
obvious answer, and anything the merge gets wrong is editable afterwards like
any other record. What it will never do is overwrite something the survivor
already says.

WHAT MAKES THIS FIDDLY

Not the reassignment — that is a handful of UPDATEs. It is the edges:

  * A relationship between two records being merged becomes a self-edge, which
    is meaningless and which the schema forbids. It is dropped.
  * Two records that both relate to a third the same way produce a duplicate
    edge. Deduplicated, keeping the one with the most information on it.
  * report_entities and contact_points can collide on their own keys.
  * The correlation table stores ids as plain text with no foreign key, so
    nothing cascades and its rows have to be rewritten by hand — including the
    pair that prompted the merge, which resolves itself.
  * The survivor's embedding is now stale, because its text has changed. It is
    cleared so the worker recomputes it; leaving it would keep proposing the
    old duplicates.

CROSSING KINDS

Normally a merge refuses records of different kinds, because the survivor's
kind decides what the merged record IS and quietly changing that is not
something an interface should do behind your back. But it does happen that
extraction files one company as both a Person and an Organization, and then
one of those two records is simply wrong.

So it is allowed, on two conditions: the caller passes `allow_type_change`
explicitly, and the interface says first what it costs. The cost is specific
— each kind's fields live in its own table, so a Person folded into an
Organization loses its date of birth, its aliases and its physical
description, because organization_details has nowhere to put them. Everything
that is not kind-specific — the name, relationships, reports, attachments,
contacts, the description — moves across as it always does.
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import audit
import auth
import entities as entities_module
from db import db_cursor

router = APIRouter(prefix="/api", tags=["merge"])

# One merge, one transaction, and a person has to be able to read the summary
# afterwards and understand what happened. Beyond this it is a bulk operation
# wearing a merge's clothes.
MAX_MERGE_SOURCES = 20


class MergeRequest(BaseModel):
    survivor_id: str = Field(min_length=1)
    merge_ids: list[str] = Field(min_length=1, max_length=MAX_MERGE_SOURCES)
    # Rename the survivor as part of the merge. The reason this exists is the
    # split-address case: a model reads "1140 Rennard Way, Kettleburn 74101" as
    # three Locations, and folding them together leaves a record called
    # "Rennard Way" that should be called the whole address. Making that a
    # second trip to the edit form for something you knew while merging is a
    # small cruelty.
    survivor_name: Optional[str] = Field(default=None, max_length=300)
    # Merging a Person into an Organization is occasionally the right answer —
    # extraction files the same company as both, and one of those two records
    # is simply wrong. It is never the right DEFAULT, though: the survivor's
    # kind decides what the merged record is, and the losers' type-specific
    # fields have nowhere to go, because they live in a different table with
    # different columns. So it is off unless the caller says otherwise, and
    # the preview says exactly which fields would be lost.
    allow_type_change: bool = False


def _entity(cur, entity_id: str) -> dict:
    cur.execute("SELECT id, entity_type, name, description, is_active, merged_into "
                "FROM entities WHERE id = %s", (entity_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Entity '{entity_id}' not found")
    return dict(zip(("id", "entity_type", "name", "description", "is_active", "merged_into"), row))


@router.get("/entities/{entity_id}/merge-preview")
def merge_preview(entity_id: str, into: str, allow_type_change: bool = False,
                  user: dict = Depends(auth.require_user)):
    """What a merge would move, before anyone commits to it.

    Counts rather than a diff: the question at this point is "am I about to
    lose something" and a number answers it. The detail fields are shown
    individually because those are the only place the survivor's own content
    can be added to."""
    with db_cursor() as cur:
        loser = _entity(cur, entity_id)
        survivor = _entity(cur, into)
        if loser["id"] == survivor["id"]:
            raise HTTPException(status_code=400, detail="An entity cannot be merged into itself")
        type_change = loser["entity_type"] != survivor["entity_type"]
        if type_change and not allow_type_change:
            raise HTTPException(
                status_code=400,
                detail=f"These are different entity types — a {loser['entity_type']} "
                       f"cannot be merged into a {survivor['entity_type']}.")

        counts = {}
        for label, sql, params in (
            ("relationships",
             "SELECT count(*) FROM relationships WHERE from_entity_id = %s OR to_entity_id = %s",
             (entity_id, entity_id)),
            ("reports", "SELECT count(*) FROM report_entities WHERE entity_id = %s", (entity_id,)),
            ("attachments", "SELECT count(*) FROM attachments WHERE entity_id = %s", (entity_id,)),
            ("contacts", "SELECT count(*) FROM contact_points WHERE entity_id = %s", (entity_id,)),
        ):
            cur.execute(sql, params)
            counts[label] = cur.fetchone()[0]

        # The edge between the two of them, if there is one, disappears rather
        # than moving — worth saying, since it is the one thing a merge loses.
        cur.execute(
            "SELECT count(*) FROM relationships "
            "WHERE (from_entity_id = %s AND to_entity_id = %s) "
            "   OR (from_entity_id = %s AND to_entity_id = %s)",
            (entity_id, into, into, entity_id))
        counts["self_edges_dropped"] = cur.fetchone()[0]

        # A loser of a different kind cannot fill anything: its details live in
        # another table entirely. It is asked about separately, so the dialog
        # can name what is about to be thrown away.
        same_type_ids = [entity_id] if not type_change else []
        fills = _detail_fills(cur, survivor["entity_type"], into, same_type_ids)
        dropped = _dropped_details(cur, loser) if type_change else {}

    return {"survivor": survivor, "loser": loser, "moves": counts,
            "fields_filled": fills,
            "type_change": type_change,
            "dropped_details": dropped,
            "description_appended": bool(loser["description"] and not survivor["description"])}


def _dropped_details(cur, loser: dict) -> dict:
    """What a cross-kind merge cannot carry across.

    Each kind's fields live in its own table — a Person's date of birth is a
    column in person_details and there is nowhere in organization_details to
    put it. Relationships, reports, attachments, contacts and the name all
    move regardless of kind; this is only about the type-specific fields, and
    only the ones that actually have something in them. Naming them is the
    whole point: "you will lose these three values" is a decision somebody can
    make, and "some fields may be lost" is not."""
    table, cols = entities_module.DETAIL_TABLES[loser["entity_type"]]
    readable = [c for c in cols if c not in
                ("maidenhead_grid", "geocode_status", "geocode_error", "geocoded_at")]
    if not readable:
        return {}
    cur.execute(f"SELECT {', '.join(readable)} FROM {table} WHERE entity_id = %s",
                (loser["id"],))
    row = cur.fetchone()
    if row is None:
        return {}
    return {col: value for col, value in zip(readable, row) if _has_value(value)}


def _detail_fills(cur, entity_type: str, survivor_id: str, loser_ids: list[str]) -> dict:
    """Which blank fields on the survivor the losers can fill, and with what.

    Read-only: the merge itself recomputes this inside its own transaction.
    Returned by the preview so nobody has to guess what "blanks filled" means
    for the records actually in front of them."""
    table, cols = entities_module.DETAIL_TABLES[entity_type]
    # Derived columns are the app's to maintain, never copied between records —
    # a Maidenhead grid belongs to a coordinate pair, not to a name.
    writable = [c for c in cols if c not in
                ("maidenhead_grid", "geocode_status", "geocode_error", "geocoded_at")]
    if not writable:
        return {}

    cur.execute(f"SELECT {', '.join(writable)} FROM {table} WHERE entity_id = %s", (survivor_id,))
    row = cur.fetchone()
    current = dict(zip(writable, row)) if row else {c: None for c in writable}

    fills: dict = {}
    for loser_id in loser_ids:
        cur.execute(f"SELECT {', '.join(writable)} FROM {table} WHERE entity_id = %s", (loser_id,))
        row = cur.fetchone()
        if row is None:
            continue
        for col, value in zip(writable, row):
            if col in fills or _has_value(current.get(col)):
                continue
            if _has_value(value):
                fills[col] = value
    return fills


def _has_value(value) -> bool:
    """Blank means nobody filled it in. An empty string and an empty array are
    blank; zero and False are not, because on a numeric or boolean field those
    are answers."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return len(value) > 0
    return True


@router.post("/entities/merge")
def merge_entities(payload: MergeRequest, user: dict = Depends(auth.require_user)):
    survivor_id = payload.survivor_id
    loser_ids = [i for i in dict.fromkeys(payload.merge_ids) if i != survivor_id]
    if not loser_ids:
        raise HTTPException(status_code=400,
                            detail="Nothing to merge — only the kept entity was given.")

    moved = {"relationships": 0, "relationships_dropped": 0, "reports": 0,
             "attachments": 0, "contacts": 0, "suggestions": 0, "rss_items": 0}

    with db_cursor(commit=True) as cur:
        survivor = _entity(cur, survivor_id)
        if survivor["merged_into"]:
            raise HTTPException(
                status_code=409,
                detail="That entity has already been merged into another. Merge into that one instead.")

        losers = []
        dropped_details: dict = {}
        for loser_id in loser_ids:
            loser = _entity(cur, loser_id)
            if loser["entity_type"] != survivor["entity_type"]:
                if not payload.allow_type_change:
                    raise HTTPException(
                        status_code=400,
                        detail=f"'{loser['name']}' is a {loser['entity_type']} and '{survivor['name']}' is a {survivor['entity_type']}. Confirm the type change to merge them.")
                # Asked for explicitly. Record what it costs while the row is
                # still there to read, so the audit entry can say precisely
                # what was given up rather than that something was.
                lost = _dropped_details(cur, loser)
                if lost:
                    dropped_details[loser_id] = {
                        "name": loser["name"],
                        "entity_type": loser["entity_type"],
                        "fields": sorted(lost),
                    }
            if loser["merged_into"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"'{loser['name']}' has already been merged into another entity.")
            losers.append(loser)

        # Fill the survivor's blanks before anything moves, so the values come
        # from the losers as they are now rather than from a half-merged state.
        # Only same-kind losers can fill anything — a Person's detail row has
        # no columns in common with an Organization's.
        same_type_ids = [l["id"] for l in losers
                         if l["entity_type"] == survivor["entity_type"]]
        fills = _detail_fills(cur, survivor["entity_type"], survivor_id, same_type_ids)
        if fills:
            _ensure_detail_row(cur, survivor["entity_type"], survivor_id)
            entities_module._update_details(cur, survivor["entity_type"], survivor_id, fills)

        _merge_aliases(cur, survivor, losers)
        _fill_description(cur, survivor, losers)

        # After the aliases are gathered, so the survivor's ORIGINAL name is
        # kept as one of them — renaming should not lose what it used to be
        # called.
        new_name = (payload.survivor_name or "").strip()
        if new_name and new_name != survivor["name"]:
            cur.execute("UPDATE entities SET name = %s, updated_at = now() WHERE id = %s",
                        (new_name, survivor_id))
            survivor["renamed_from"] = survivor["name"]
            survivor["name"] = new_name

        for loser in losers:
            _move_relationships(cur, loser["id"], survivor_id, moved)
            _move_report_links(cur, loser["id"], survivor_id, moved)
            _move_simple(cur, loser["id"], survivor_id, moved)
            _move_suggestions(cur, loser["id"], survivor_id, moved)
            _rewrite_correlations(cur, loser["id"], survivor_id)

            cur.execute(
                "UPDATE entities SET is_active = FALSE, merged_into = %s, updated_at = now() "
                "WHERE id = %s", (survivor_id, loser["id"]))

        # The survivor's text has changed, so its embedding no longer describes
        # it. Left alone it would keep matching the records just folded in.
        cur.execute("UPDATE entities SET embedding = NULL, updated_at = now() WHERE id = %s",
                    (survivor_id,))

    audit.record("entity.merge", user=user, object_type="entity", object_id=survivor_id,
                 object_label=survivor["name"],
                 detail={"merged": [{"id": l["id"], "name": l["name"],
                                     "entity_type": l["entity_type"]} for l in losers],
                         "moved": moved, "fields_filled": sorted(fills),
                         "survivor_type": survivor["entity_type"],
                         # Only present when kinds were crossed. An audit entry
                         # that does not say what was discarded is not much of
                         # an audit entry.
                         "dropped_details": dropped_details or None,
                         "renamed_from": survivor.get("renamed_from")})

    return {"survivor_id": survivor_id, "survivor_name": survivor["name"],
            "survivor_type": survivor["entity_type"],
            "renamed_from": survivor.get("renamed_from"),
            "merged": [{"id": l["id"], "name": l["name"],
                        "entity_type": l["entity_type"]} for l in losers],
            "moved": moved, "fields_filled": sorted(fills),
            "dropped_details": dropped_details}


def _ensure_detail_row(cur, entity_type: str, entity_id: str) -> None:
    """A record can exist with no detail row at all. _update_details is an
    UPDATE, which would silently do nothing."""
    table, _ = entities_module.DETAIL_TABLES[entity_type]
    cur.execute(f"INSERT INTO {table} (entity_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (entity_id,))


def _merge_aliases(cur, survivor: dict, losers: list[dict]) -> None:
    """A Person's aliases are a set, so this is the one field where the losers
    ADD to the survivor rather than only filling a blank — the other names a
    duplicate was filed under are exactly what you want to keep, and the names
    of the merged records themselves are the most useful of all."""
    if survivor["entity_type"] != "person":
        return
    _ensure_detail_row(cur, "person", survivor["id"])
    cur.execute("SELECT aliases FROM person_details WHERE entity_id = %s", (survivor["id"],))
    row = cur.fetchone()
    known = list(row[0] or []) if row else []
    lowered = {a.strip().lower() for a in known if a and a.strip()}
    lowered.add(survivor["name"].strip().lower())

    for loser in losers:
        cur.execute("SELECT aliases FROM person_details WHERE entity_id = %s", (loser["id"],))
        row = cur.fetchone()
        candidates = list(row[0] or []) if row else []
        candidates.append(loser["name"])
        for alias in candidates:
            if alias and alias.strip() and alias.strip().lower() not in lowered:
                lowered.add(alias.strip().lower())
                known.append(alias.strip())

    cur.execute("UPDATE person_details SET aliases = %s WHERE entity_id = %s",
                (known or None, survivor["id"]))


def _fill_description(cur, survivor: dict, losers: list[dict]) -> None:
    """Only if the survivor has none. Concatenating descriptions produces a
    record that reads like three records, which is what a merge is supposed to
    stop."""
    if survivor["description"] and survivor["description"].strip():
        return
    for loser in losers:
        if loser["description"] and loser["description"].strip():
            cur.execute("UPDATE entities SET description = %s WHERE id = %s",
                        (loser["description"], survivor["id"]))
            return


def _move_relationships(cur, loser_id: str, survivor_id: str, moved: dict) -> None:
    """Reassign both ends, then clean up what that creates.

    Order matters: the self-edges have to go before the reassignment, or the
    UPDATE writes a row the table's own CHECK forbids."""
    cur.execute(
        "DELETE FROM relationships "
        "WHERE (from_entity_id = %s AND to_entity_id = %s) "
        "   OR (from_entity_id = %s AND to_entity_id = %s)",
        (loser_id, survivor_id, survivor_id, loser_id))
    moved["relationships_dropped"] += cur.rowcount

    cur.execute("UPDATE relationships SET from_entity_id = %s WHERE from_entity_id = %s",
                (survivor_id, loser_id))
    moved["relationships"] += cur.rowcount
    cur.execute("UPDATE relationships SET to_entity_id = %s WHERE to_entity_id = %s",
                (survivor_id, loser_id))
    moved["relationships"] += cur.rowcount

    # Two records related to the same third party the same way are now one
    # relationship stated twice. Keep the row carrying the most — a confirmed
    # edge with notes beats a bare possible one — and the lowest id to break a
    # tie deterministically.
    cur.execute(
        """
        DELETE FROM relationships r
        USING relationships keep
        WHERE r.from_entity_id = keep.from_entity_id
          AND r.to_entity_id = keep.to_entity_id
          AND r.relationship_type = keep.relationship_type
          AND r.id <> keep.id
          AND (
            (CASE keep.confidence WHEN 'confirmed' THEN 2 WHEN 'probable' THEN 1 ELSE 0 END,
             (keep.notes IS NOT NULL)::int, (keep.discovery_date IS NOT NULL)::int, -keep.id)
            >
            (CASE r.confidence WHEN 'confirmed' THEN 2 WHEN 'probable' THEN 1 ELSE 0 END,
             (r.notes IS NOT NULL)::int, (r.discovery_date IS NOT NULL)::int, -r.id)
          )
          AND (r.from_entity_id = %s OR r.to_entity_id = %s)
        """,
        (survivor_id, survivor_id))
    moved["relationships_dropped"] += cur.rowcount


def _move_report_links(cur, loser_id: str, survivor_id: str, moved: dict) -> None:
    """(report_id, entity_id) is the primary key, so a report that mentioned
    both records has to lose one of the two rows rather than collide."""
    cur.execute(
        "DELETE FROM report_entities WHERE entity_id = %s AND report_id IN "
        "(SELECT report_id FROM report_entities WHERE entity_id = %s)",
        (loser_id, survivor_id))
    cur.execute("UPDATE report_entities SET entity_id = %s WHERE entity_id = %s",
                (survivor_id, loser_id))
    moved["reports"] += cur.rowcount


def _move_simple(cur, loser_id: str, survivor_id: str, moved: dict) -> None:
    cur.execute("UPDATE attachments SET entity_id = %s WHERE entity_id = %s",
                (survivor_id, loser_id))
    moved["attachments"] += cur.rowcount

    # The same phone number on both copies is one phone number.
    cur.execute(
        "DELETE FROM contact_points c USING contact_points keep "
        "WHERE c.entity_id = %s AND keep.entity_id = %s "
        "  AND lower(trim(c.value)) = lower(trim(keep.value)) AND c.kind = keep.kind",
        (loser_id, survivor_id))
    cur.execute("UPDATE contact_points SET entity_id = %s WHERE entity_id = %s",
                (survivor_id, loser_id))
    moved["contacts"] += cur.rowcount

    cur.execute("UPDATE rss_items SET entity_id = %s WHERE entity_id = %s",
                (survivor_id, loser_id))
    moved["rss_items"] += cur.rowcount


def _move_suggestions(cur, loser_id: str, survivor_id: str, moved: dict) -> None:
    """Three columns, all of which can point at a merged-away record.

    A pending relationship suggestion whose two ends are now the same record
    can never be accepted, so it is dismissed rather than left to fail when
    somebody clicks Accept."""
    # Same shape of problem as the correlation table: one live proposal per
    # (source, type, relationship, from, to) is enforced by a unique index,
    # and moving a loser's suggestions onto the survivor can land two pending
    # proposals on the same key -- "A knows B" and "A knows C" become the same
    # sentence the moment B and C turn out to be one record. Settle it before
    # the rewrite rather than letting the UPDATE fail. The oldest proposal
    # stays pending; the rest are marked reviewed, because they were, by this
    # merge. NULL relationship types are left alone: the index treats NULLs as
    # distinct, so those rows never collide in the first place.
    cur.execute(
        """
        WITH rewritten AS (
            SELECT id, source, suggestion_type, suggested_relationship_type,
                   CASE WHEN suggested_from_entity_id = %(loser)s THEN %(survivor)s
                        ELSE suggested_from_entity_id END AS from_id,
                   CASE WHEN suggested_to_entity_id = %(loser)s THEN %(survivor)s
                        ELSE suggested_to_entity_id END AS to_id
            FROM extraction_suggestions
            WHERE status = 'pending'
              AND suggested_from_entity_id IS NOT NULL
              AND suggested_relationship_type IS NOT NULL
        ),
        ranked AS (
            SELECT id, row_number() OVER (
                       PARTITION BY source, suggestion_type,
                                    suggested_relationship_type, from_id, to_id
                       ORDER BY id) AS rn
            FROM rewritten
        )
        UPDATE extraction_suggestions s
           SET status = 'rejected', reviewed_at = now()
          FROM ranked
         WHERE s.id = ranked.id AND ranked.rn > 1
        """,
        {"loser": loser_id, "survivor": survivor_id})

    for column in ("resolved_entity_id", "suggested_from_entity_id", "suggested_to_entity_id"):
        cur.execute(f"UPDATE extraction_suggestions SET {column} = %s WHERE {column} = %s",
                    (survivor_id, loser_id))
        moved["suggestions"] += cur.rowcount

    cur.execute(
        "UPDATE extraction_suggestions SET status = 'rejected', reviewed_at = now() "
        "WHERE status = 'pending' AND suggested_from_entity_id = suggested_to_entity_id")


def _rewrite_correlations(cur, loser_id: str, survivor_id: str) -> None:
    """The correlation table stores ids as plain text with no foreign key, so
    nothing here cascades and nothing here is cleaned up for us.

    The pair that prompted the merge becomes a record matched against itself.
    That is not a suggestion any more, it is a completed decision, so it is
    marked confirmed rather than deleted — the queue should show that somebody
    dealt with it."""
    cur.execute(
        "UPDATE correlation_suggestions SET status = 'confirmed', reviewed_at = now() "
        "WHERE subject_type = 'entity' AND status = 'pending' "
        "  AND ((subject_a_id = %s AND subject_b_id = %s) "
        "    OR (subject_a_id = %s AND subject_b_id = %s))",
        (loser_id, survivor_id, survivor_id, loser_id))

    # The pair BETWEEN these two records keeps its original ids. Rewriting it
    # would produce a row pairing the survivor with itself, which the table's
    # own CHECK forbids — and it should keep them anyway: it is the historical
    # record that somebody judged these two to be the same thing, which is
    # exactly what just happened. Every OTHER row moves onto the survivor.
    for column, other in (("subject_a_id", "subject_b_id"),
                          ("subject_b_id", "subject_a_id")):
        # Collapse before rewriting, not after. (subject_type, a, b) is
        # UNIQUE, so if the survivor is ALREADY paired with whatever this
        # loser row points at -- which is common the moment three or more
        # copies of one record have been flagged against each other -- the
        # UPDATE below is what violates the index, and no amount of tidying
        # afterwards gets a chance to run. The pair is about to become the
        # same pair either way, so the loser's row is redundant: the row
        # already naming the survivor is the one to keep.
        cur.execute(
            f"""
            DELETE FROM correlation_suggestions c
            WHERE c.{column} = %(loser)s
              AND c.{other} <> %(survivor)s
              AND c.subject_type IN ('entity', 'report_event')
              AND EXISTS (
                    SELECT 1 FROM correlation_suggestions k
                    WHERE k.id <> c.id
                      AND k.subject_type = c.subject_type
                      AND ((k.{column} = %(survivor)s AND k.{other} = c.{other})
                        -- The same pair the other way round. Only entity
                        -- pairs are symmetric; a report_event row's two
                        -- sides are a report and an event, so swapping
                        -- them would be a different row, not a duplicate.
                        OR (c.subject_type = 'entity'
                            AND k.{other} = %(survivor)s AND k.{column} = c.{other})))
            """,
            {"loser": loser_id, "survivor": survivor_id})
        cur.execute(
            f"UPDATE correlation_suggestions SET {column} = %s "
            f"WHERE {column} = %s AND {other} <> %s "
            "  AND subject_type IN ('entity', 'report_event')",
            (survivor_id, loser_id, survivor_id))

    cur.execute(
        """
        DELETE FROM correlation_suggestions c
        USING correlation_suggestions keep
        WHERE c.subject_type = keep.subject_type
          AND c.subject_a_id = keep.subject_a_id
          AND c.subject_b_id = keep.subject_b_id
          AND c.id > keep.id
        """)

    # Rewriting can also produce (survivor, X) when (X, survivor) already
    # exists — the same pair in mirror image, which the correlation pass
    # normalises but this rewrite cannot. Keep the older row.
    cur.execute(
        """
        DELETE FROM correlation_suggestions c
        USING correlation_suggestions keep
        WHERE c.subject_type = 'entity' AND keep.subject_type = 'entity'
          AND c.subject_a_id = keep.subject_b_id
          AND c.subject_b_id = keep.subject_a_id
          AND c.id > keep.id
        """)


@router.get("/entities/{entity_id}/merged-from")
def merged_from(entity_id: str, user: dict = Depends(auth.require_user)):
    """The records folded into this one. Shown on the entity page so a merge is
    visible after the fact rather than something that quietly happened."""
    with db_cursor() as cur:
        cur.execute("SELECT id, name, entity_type, updated_at FROM entities "
                    "WHERE merged_into = %s ORDER BY updated_at DESC, name", (entity_id,))
        return {"items": [{"id": r[0], "name": r[1], "entity_type": r[2], "merged_at": r[3]}
                          for r in cur.fetchall()]}
