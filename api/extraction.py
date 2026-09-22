"""Extraction review queue: accept/reject the entity + relationship
suggestions the worker generates from uploaded attachments (see
worker/main.py and worker/ollama_client.py).

Nothing in this module writes to entities/relationships on its own — only
accepting a suggestion does that, and it always goes through the same
validation entities.py uses for a hand-created entity/relationship (this
module calls those helpers directly rather than reimplementing them), so an
LLM-proposed entity can never end up less validated than a manually typed
one.
"""

import re
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import audit
import auth
import entities as entities_module
from db import db_cursor
from idgen import generate_id

router = APIRouter(prefix="/api", tags=["extraction"])

SUGGESTION_STATUSES = ("pending", "accepted", "rejected")

_SUGGESTION_COLS = [
    "id", "attachment_id", "source", "suggestion_type", "suggested_entity_type", "suggested_name",
    "suggested_relationship_type", "suggested_from_name", "suggested_to_name",
    "suggested_from_entity_id", "suggested_to_entity_id", "evidence",
    "details", "confidence", "status", "resolved_entity_id", "reviewed_by", "reviewed_at", "created_at",
]

# Where a suggestion came from. See the `source` column comment in db/init.sql.
# "manual" is a record a person made while reading a document — see
# POST /api/documents/{id}/record. It is never pending: it exists only as
# the accepted provenance row linking a hand-made record to its source.
SUGGESTION_SOURCES = ("extraction", "signal", "assistant", "manual")


class AcceptEntitySuggestion(BaseModel):
    entity_type: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    details: Optional[dict] = None


class CreateEndpoint(BaseModel):
    """Make the record this relationship needs, as part of accepting it.

    A document says "Fenwick worked at Kestrel Logistics". The relationship is
    useless until Kestrel Logistics exists as a record, and before this the
    reviewer had to leave the queue, create it by hand on the Entities page,
    come back, and find it again in a picker — for something the app already
    knew the name of. This is that round trip removed.

    It is still an explicit act: the reviewer clicks a button that names what
    will be created and picks its type. Nothing is created by merely accepting.
    """
    entity_type: str
    name: Optional[str] = None      # defaults to the name on the suggestion
    description: Optional[str] = None


class AcceptRelationshipSuggestion(BaseModel):
    # Optional, because a signal- or assistant-derived suggestion already knows
    # which two records it means and stored their ids. Only a suggestion pulled
    # out of a document needs the reviewer to say who the names refer to, since
    # there the model only ever saw text.
    from_entity_id: Optional[str] = None
    to_entity_id: Optional[str] = None
    # Used only when the corresponding *_entity_id is absent: create that end
    # of the relationship, then use it.
    create_from: Optional[CreateEndpoint] = None
    create_to: Optional[CreateEndpoint] = None
    relationship_type: Optional[str] = None
    confidence: Optional[str] = None
    notes: Optional[str] = None
    discovery_date: Optional[date] = None


def _lenient_details(entity_type: str, raw_details: dict) -> dict:
    """LLM-proposed detail fields are best-effort: an unknown key is dropped
    silently, and a key that fails validation on its own (a malformed date,
    an out-of-range reliability rating, etc.) is dropped by itself rather
    than blocking accept of an otherwise-good suggestion over one bad field."""
    model = entities_module.DETAIL_MODELS[entity_type]
    allowed = set(model.model_fields.keys())
    cleaned = {}
    for key, value in (raw_details or {}).items():
        if key not in allowed:
            continue
        try:
            validated = model(**{key: value}).model_dump(include={key})
            cleaned[key] = validated[key]
        except Exception:
            continue
    return cleaned


def _create_entity(cur, user: dict, entity_type: str, name: str,
                   description=None, raw_details=None) -> str:
    """One place that turns a proposed entity into a real record.

    Shared by accepting an entity suggestion and by creating a missing end of a
    relationship inline, so the two cannot drift into validating differently —
    which would make "how the record got here" change what the record is
    allowed to be."""
    entities_module._validate_entity_type(entity_type)
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Entity needs a name")
    details = _lenient_details(entity_type, raw_details or {})
    full_details = entities_module._parsed_details(entity_type, details)
    entity_id = generate_id(entity_type, name)
    cur.execute(
        "INSERT INTO entities (id, entity_type, name, description, created_by) "
        "VALUES (%s, %s, %s, %s, %s)",
        (entity_id, entity_type, name, description, user["id"]),
    )
    entities_module._insert_details(cur, entity_type, entity_id, full_details)
    return entity_id


def _resolve_pending_entity_suggestion(cur, attachment_id, name, entity_id, user) -> None:
    """A name created from a relationship card usually ALSO has its own entity
    suggestion waiting from the same document. Leaving that pending would ask
    the reviewer to decide twice about a record that now exists, and accepting
    it a second time would create a duplicate — so it is closed out and pointed
    at the record that was just made."""
    if attachment_id is None or not name:
        return
    cur.execute(
        "UPDATE extraction_suggestions SET status = 'accepted', resolved_entity_id = %s, "
        "reviewed_by = %s, reviewed_at = now() "
        "WHERE attachment_id = %s AND suggestion_type = 'entity' AND status = 'pending' "
        "  AND lower(trim(suggested_name)) = lower(trim(%s))",
        (entity_id, user["id"], attachment_id, name),
    )


# A name that is really a contact detail. Models reading correspondence emit
# these constantly — "Miles Trombley <m.trombley@example>" is one person and an
# address, and the address is the part that gets proposed as a Person. The
# extraction prompt now says so explicitly, but a prompt is a request and this
# is the check, so the ones that get through are visible rather than accepted
# by a reviewer working quickly down a long queue.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^(https?://|www\.)\S+$|^[\w-]+\.(com|org|net|gov|edu|io|example)\S*$",
                     re.I)
_PHONE_RE = re.compile(r"^[+(]?[\d][\d\s().+-]{5,}$")

# Only for the types where it is definitely wrong. A Communication entity is
# legitimately named after a channel, and nobody proposes a Location called
# "555-0142".
_NAME_SHOULD_BE_A_NAME = ("person", "organization")


def looks_like_contact_detail(entity_type: str, name: str) -> str:
    """Returns what the name actually looks like, or "" if it looks like a name."""
    if entity_type not in _NAME_SHOULD_BE_A_NAME:
        return ""
    value = (name or "").strip()
    if not value:
        return ""
    if _EMAIL_RE.match(value):
        return "an email address"
    if _URL_RE.match(value):
        return "a web address"
    if _PHONE_RE.match(value) and sum(c.isdigit() for c in value) >= 6:
        return "a phone number"
    return ""


def _name_candidates(cur, names: list[str]) -> dict:
    """Existing records each proposed name might refer to.

    A relationship pulled out of a document carries two names and no ids. Most
    of the time one of those names is already a record — the reviewer entered
    it last week, or accepted it from this very document two clicks ago — and
    making them search for it again in a picker is asking them to do a lookup
    the database can do. Exact, case-insensitive, and capped: a fuzzy match
    here would silently attach a relationship to the wrong record, which is
    much worse than one extra search."""
    wanted = sorted({n.strip().lower() for n in names if n and n.strip()})
    if not wanted:
        return {}
    cur.execute(
        "SELECT id, name, entity_type FROM entities "
        "WHERE is_active AND lower(trim(name)) = ANY(%s) ORDER BY name, id",
        (wanted,),
    )
    out: dict = {}
    for entity_id, name, entity_type in cur.fetchall():
        out.setdefault(name.strip().lower(), []).append(
            {"id": entity_id, "name": name, "entity_type": entity_type})
    return out


# What a relationship implies about the record on its far end, used only to
# preselect the type in the "create this record" control. A guess the reviewer
# can see and change — never applied on its own.
ENDPOINT_TYPE_HINTS = {
    "employed_by": ("person", "organization"),
    "member_of": ("person", "organization"),
    "affiliated_with": (None, "organization"),
    "owns": (None, None),
    "located_at": (None, "location"),
    "present_at": ("person", None),
    "communicated_with": (None, None),
    "family_of": ("person", "person"),
    "spouse_of": ("person", "person"),
    "parent_of": ("person", "person"),
    "child_of": ("person", "person"),
    "sibling_of": ("person", "person"),
    "significant_of": ("person", "person"),
    "reported_by": (None, "source"),
    "drives": ("person", "vehicle"),
    "seen_in": ("person", "vehicle"),
}


def _endpoint_hints(relationship_type: str) -> dict:
    from_hint, to_hint = ENDPOINT_TYPE_HINTS.get(
        (relationship_type or "").strip().lower(), (None, None))
    return {"from_entity_type_hint": from_hint, "to_entity_type_hint": to_hint}


def _suggestion_row(cur, suggestion_id: int) -> dict:
    cur.execute(
        f"SELECT {', '.join(_SUGGESTION_COLS)} FROM extraction_suggestions WHERE id = %s",
        (suggestion_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return dict(zip(_SUGGESTION_COLS, row))


@router.get("/extraction-suggestions")
def list_extraction_suggestions(
    status: Optional[str] = Query(default="pending"),
    attachment_id: Optional[int] = Query(default=None),
    source: Optional[str] = Query(default=None,
                                  description=f"One of {SUGGESTION_SOURCES}"),
    suggestion_type: Optional[str] = Query(default=None,
                                           description="entity | relationship"),
    entity_type: Optional[str] = Query(default=None,
                                       description="Narrow entity suggestions to one type"),
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(auth.require_user),
):
    if status is not None and status not in SUGGESTION_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {SUGGESTION_STATUSES}")

    where = []
    params: list = []
    if status:
        where.append("status = %s")
        params.append(status)
    if attachment_id is not None:
        where.append("attachment_id = %s")
        params.append(attachment_id)
    if source is not None:
        if source not in SUGGESTION_SOURCES:
            raise HTTPException(status_code=400,
                                detail=f"source must be one of {SUGGESTION_SOURCES}")
        where.append("source = %s")
        params.append(source)
    if suggestion_type is not None:
        if suggestion_type not in ("entity", "relationship"):
            raise HTTPException(status_code=400,
                                detail="suggestion_type must be 'entity' or 'relationship'")
        where.append("suggestion_type = %s")
        params.append(suggestion_type)
    if entity_type is not None:
        entities_module._validate_entity_type(entity_type)
        where.append("suggested_entity_type = %s")
        params.append(entity_type)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM extraction_suggestions {where_clause}", params)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT {', '.join(_SUGGESTION_COLS)} FROM extraction_suggestions {where_clause} "
            # Entities before relationships, and that is not cosmetic: a
            # relationship can only be accepted once the records at both ends
            # exist, so a queue that interleaves them asks the reviewer to
            # decide about a link before deciding about its endpoints. Working
            # straight down the list in this order means each relationship
            # arrives with its ends already matched.
            #
            # `id` is not decoration either. The signal pass inserts a whole
            # batch in one transaction, and Postgres' now() is
            # transaction-scoped, so every row in that batch carries an
            # identical created_at. With only created_at to sort on the order
            # is whatever the heap happens to give back — which changes the
            # moment any row is updated, so the reviewer's queue silently
            # reshuffles under them mid-review.
            "ORDER BY CASE suggestion_type WHEN 'entity' THEN 0 ELSE 1 END, "
            "         created_at, id LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        items = [dict(zip(_SUGGESTION_COLS, r)) for r in cur.fetchall()]
        annotate_relationship_candidates(cur, items)
        annotate_contact_shaped_names(items)

    return {"items": items, "total": total, "limit": limit, "offset": offset}


def annotate_contact_shaped_names(items: list[dict]) -> None:
    """Mark entity suggestions whose "name" is really a contact detail."""
    for s in items:
        if s.get("suggestion_type") != "entity":
            continue
        looks = looks_like_contact_detail(s.get("suggested_entity_type"),
                                          s.get("suggested_name"))
        if looks:
            s["looks_like"] = looks


def annotate_relationship_candidates(cur, items: list[dict]) -> None:
    """Attach, to each relationship suggestion that names records only by name,
    the existing records those names might be — and a type hint for creating
    the ones that do not exist yet.

    One query for the whole page rather than one per card: a fifty-row queue
    would otherwise be a hundred lookups to answer a question the reviewer has
    about every single card."""
    named = [s for s in items
             if s.get("suggestion_type") == "relationship"
             and not (s.get("suggested_from_entity_id") and s.get("suggested_to_entity_id"))]
    if not named:
        return
    names = []
    for s in named:
        names.extend([s.get("suggested_from_name"), s.get("suggested_to_name")])
    candidates = _name_candidates(cur, names)
    for s in named:
        s["from_candidates"] = candidates.get((s.get("suggested_from_name") or "").strip().lower(), [])
        s["to_candidates"] = candidates.get((s.get("suggested_to_name") or "").strip().lower(), [])
        s.update(_endpoint_hints(s.get("suggested_relationship_type")))


@router.get("/extraction-suggestions/facets")
def suggestion_facets(
    status: Optional[str] = Query(default="pending"),
    user: dict = Depends(auth.require_user),
):
    """What is in the queue, broken down the ways you would filter it.

    Counts rather than a bare list of options, because the useful question at
    the top of a fifty-item queue is not "can I filter by person" but "how many
    people are waiting" — and a filter that turns out to be empty after you
    pick it is a wasted click. Also names the documents, so the queue can be
    worked one document at a time, which is how the material actually arrived.
    """
    if status is not None and status not in SUGGESTION_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {SUGGESTION_STATUSES}")
    where, params = ("WHERE status = %s", [status]) if status else ("", [])

    with db_cursor() as cur:
        cur.execute(f"SELECT suggestion_type, count(*) FROM extraction_suggestions "
                    f"{where} GROUP BY 1", params or None)
        by_type = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute(f"SELECT suggested_entity_type, count(*) FROM extraction_suggestions "
                    f"{where}{' AND' if where else 'WHERE'} suggestion_type = 'entity' "
                    "AND suggested_entity_type IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
                    params or None)
        by_entity_type = [{"entity_type": row[0], "count": row[1]} for row in cur.fetchall()]

        cur.execute(f"SELECT source, count(*) FROM extraction_suggestions {where} GROUP BY 1",
                    params or None)
        by_source = {row[0]: row[1] for row in cur.fetchall()}

        # Left join, because a suggestion from the signal pass or the assistant
        # has no attachment and still has to be reachable from this filter.
        cur.execute(
            f"SELECT s.attachment_id, COALESCE(a.title, a.filename), count(*) "
            "FROM extraction_suggestions s "
            "LEFT JOIN attachments a ON a.id = s.attachment_id "
            f"{where} GROUP BY 1, 2 ORDER BY 3 DESC, 2",
            params or None)
        by_document = [
            {"attachment_id": row[0],
             "label": row[1] or ("Not from a document" if row[0] is None else f"#{row[0]}"),
             "count": row[2]}
            for row in cur.fetchall()
        ]

    return {"by_type": by_type, "by_entity_type": by_entity_type,
            "by_source": by_source, "by_document": by_document,
            "total": sum(by_type.values())}


@router.post("/extraction-suggestions/{suggestion_id}/reject")
def reject_extraction_suggestion(suggestion_id: int, user: dict = Depends(auth.require_user)):
    with db_cursor(commit=True) as cur:
        suggestion = _suggestion_row(cur, suggestion_id)
        if suggestion["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"Suggestion already {suggestion['status']}")
        cur.execute(
            "UPDATE extraction_suggestions SET status = 'rejected', reviewed_by = %s, reviewed_at = now() "
            "WHERE id = %s",
            (user["id"], suggestion_id),
        )
    audit.record("extraction.reject", user=user, object_type="extraction_suggestion",
                 object_id=suggestion_id, object_label=suggestion.get("suggested_name"))
    return {"id": suggestion_id, "status": "rejected"}


@router.post("/extraction-suggestions/{suggestion_id}/accept")
def accept_extraction_suggestion(
    suggestion_id: int,
    payload: dict = {},
    user: dict = Depends(auth.require_user),
):
    """Body shape depends on the suggestion's own type — validated below
    against whichever of AcceptEntitySuggestion/AcceptRelationshipSuggestion
    applies. Every field is an optional override; omitted fields fall back
    to what the model originally suggested."""
    with db_cursor(commit=True) as cur:
        suggestion = _suggestion_row(cur, suggestion_id)
        if suggestion["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"Suggestion already {suggestion['status']}")

        if suggestion["suggestion_type"] == "entity":
            try:
                overrides = AcceptEntitySuggestion(**payload)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=str(exc))

            entity_type = overrides.entity_type or suggestion["suggested_entity_type"]
            raw_details = overrides.details if overrides.details is not None else (suggestion["details"] or {})
            entity_id = _create_entity(
                cur, user, entity_type,
                overrides.name or suggestion["suggested_name"] or "",
                description=overrides.description, raw_details=raw_details)

            cur.execute(
                "UPDATE extraction_suggestions SET status = 'accepted', resolved_entity_id = %s, "
                "reviewed_by = %s, reviewed_at = now() WHERE id = %s",
                (entity_id, user["id"], suggestion_id),
            )
            audit.record("extraction.accept", user=user, object_type="extraction_suggestion",
                         object_id=suggestion_id, object_label=suggestion.get("suggested_name"),
                         detail={"accepted_as": "entity", "entity_id": entity_id})
            return {"id": suggestion_id, "status": "accepted", "resolved_entity_id": entity_id}

        if suggestion["suggestion_type"] == "relationship":
            try:
                overrides = AcceptRelationshipSuggestion(**payload)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=str(exc))

            # Fall back to the ids the proposer recorded. An accept with an
            # empty body is the normal case for a signal suggestion: the
            # reviewer is agreeing to exactly what was put in front of them.
            if overrides.from_entity_id is None:
                overrides.from_entity_id = suggestion.get("suggested_from_entity_id")
            if overrides.to_entity_id is None:
                overrides.to_entity_id = suggestion.get("suggested_to_entity_id")

            # Create either end the reviewer asked for. This is the difference
            # between "a document says Fenwick worked at Kestrel Logistics, now
            # go and make Kestrel Logistics by hand somewhere else" and one
            # button. It happens inside the same transaction as the
            # relationship, so a failure leaves neither behind.
            created = []
            for side, create in (("from", overrides.create_from), ("to", overrides.create_to)):
                current = getattr(overrides, f"{side}_entity_id")
                if current or create is None:
                    continue
                fallback_name = suggestion.get(f"suggested_{side}_name")
                new_id = _create_entity(cur, user, create.entity_type,
                                        create.name or fallback_name or "",
                                        description=create.description)
                setattr(overrides, f"{side}_entity_id", new_id)
                created.append({"side": side, "entity_id": new_id,
                                "entity_type": create.entity_type,
                                "name": create.name or fallback_name})
                _resolve_pending_entity_suggestion(
                    cur, suggestion.get("attachment_id"),
                    create.name or fallback_name, new_id, user)

            if not overrides.from_entity_id or not overrides.to_entity_id:
                raise HTTPException(
                    status_code=400,
                    detail="Say which entities both ends refer to, or create them, before accepting.",
                )

            relationship_type = (
                overrides.relationship_type or suggestion["suggested_relationship_type"] or ""
            ).strip().lower()
            if not entities_module.RELATIONSHIP_TYPE_RE.match(relationship_type):
                raise HTTPException(
                    status_code=400,
                    detail="relationship_type must be lowercase snake_case, e.g. 'employed_by'",
                )
            # Deliberately NOT mapping the LLM's 0-1 confidence score onto
            # confirmed/probable/possible — those measure different things
            # (the model's certainty about the wording vs. an analyst's
            # tradecraft judgment about the claim itself). Auto-promoting an
            # unreviewed suggestion straight to 'confirmed' would undercut
            # the whole point of a review queue, so this defaults to the
            # most conservative option unless the analyst explicitly
            # overrides it in the accept call.
            confidence = overrides.confidence or "possible"
            if confidence not in entities_module.CONFIDENCE_LEVELS:
                raise HTTPException(
                    status_code=400,
                    detail=f"confidence must be one of {entities_module.CONFIDENCE_LEVELS}",
                )
            if overrides.from_entity_id == overrides.to_entity_id:
                raise HTTPException(status_code=400, detail="An entity cannot have a relationship with itself")
            if not entities_module._entity_exists(cur, overrides.from_entity_id):
                raise HTTPException(status_code=404, detail=f"from_entity_id '{overrides.from_entity_id}' not found")
            if not entities_module._entity_exists(cur, overrides.to_entity_id):
                raise HTTPException(status_code=404, detail=f"to_entity_id '{overrides.to_entity_id}' not found")

            cur.execute(
                """
                INSERT INTO relationships
                    (from_entity_id, to_entity_id, relationship_type, confidence, discovery_date, notes, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    overrides.from_entity_id, overrides.to_entity_id, relationship_type, confidence,
                    overrides.discovery_date, overrides.notes, user["id"],
                ),
            )
            relationship_id = cur.fetchone()[0]

            cur.execute(
                "UPDATE extraction_suggestions SET status = 'accepted', reviewed_by = %s, reviewed_at = now() "
                "WHERE id = %s",
                (user["id"], suggestion_id),
            )
            audit.record("extraction.accept", user=user, object_type="extraction_suggestion",
                         object_id=suggestion_id,
                         detail={"accepted_as": "relationship", "relationship_id": relationship_id,
                                 # Records created on the way through are named
                                 # here so the audit log shows that accepting
                                 # one suggestion made two things.
                                 "created_entities": created or None})
            return {"id": suggestion_id, "status": "accepted",
                    "relationship_id": relationship_id,
                    "created_entities": created}

        raise HTTPException(
            status_code=500, detail=f"Unknown suggestion_type '{suggestion['suggestion_type']}'"
        )
