"""Entity + relationship CRUD.

Fixed core entity types (see db/init.sql for the reasoning): person,
organization, location, event, source, communication, vehicle, record. Each type's
distinguishing fields live in a 1:1 "detail" table; DETAIL_TABLES below is
the single source of truth mapping a type to its table and column list, so
every dynamic-SQL helper here reads from one place instead of duplicating
the mapping per endpoint.

Entities are archived (is_active = false), never hard-deleted — relationships,
reports, and attachments may reference them and that history should survive.
Relationships ARE hard-deleted: an edge is a much smaller, more mechanical
fact than an entity, and there is no cross-table history hanging off a
relationship row the way there is off an entity.
"""

import re
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import audit
import auth
import contacts
from db import db_cursor
from geo import maidenhead_locator
from idgen import generate_id

router = APIRouter(prefix="/api", tags=["entities"])

ENTITY_TYPES = ("person", "organization", "location", "event", "source", "communication",
                "vehicle", "record")

# type -> (detail table name, ordered column list). Column names here are
# never user input — they're read from this fixed dict to build parameterized
# SQL, not from request data — so the f-string SQL below doesn't open an
# injection path despite not looking like it at a glance.
DETAIL_TABLES: dict[str, tuple[str, list[str]]] = {
    "person": ("person_details", ["aliases", "date_of_birth", "alignment", "occupation",
                                 "physical_description", "life_status", "disposition"]),
    "organization": ("organization_details", ["org_type", "founded_date", "website", "alignment"]),
    # maidenhead_grid/geocode_status/geocode_error/geocoded_at are derived,
    # not user-settable: they're columns here (so GET returns them, via
    # _fetch_details) but deliberately absent from LocationDetails below, so
    # a PATCH that tries to set them directly 422s as an unknown field. The
    # only code path allowed to write them is _apply_location_derived_fields.
    "location": ("location_details", ["address", "environment", "lat", "lng", "maidenhead_grid", "geocode_status", "geocode_error", "geocoded_at"]),
    "event": ("event_details", ["event_type", "started_at", "ended_at", "expires_at"]),
    "source": ("source_details", ["source_type", "reliability_rating", "handling_notes", "alignment"]),
    "communication": ("communication_details", ["medium", "medium_detail", "occurred_at", "participants_note"]),
    "vehicle": ("vehicle_details", ["make", "model", "color", "license_plate",
                                    "plate_region", "alignment", "style", "notes"]),
    # source_attachment_id is set only by POST /documents/{id}/to-record and is
    # absent from RecordDetails, so it cannot be pointed at a different file.
    "record": ("record_details", ["record_kind", "record_date", "issued_by", "body",
                                  "source_attachment_id"]),
}

# Not DB-enforced (see the relationships table comment in init.sql) — this is
# just what the frontend offers by default via GET /api/relationship-types.
# Anything matching RELATIONSHIP_TYPE_RE is accepted so a real investigation
# isn't blocked waiting on a code change for a relationship type nobody
# thought of yet.
SUGGESTED_RELATIONSHIP_TYPES = (
    "affiliated_with", "employed_by", "member_of", "family_of", "associate_of",
    "spouse_of", "significant_of", "parent_of", "child_of", "sibling_of",
    "located_at", "present_at", "communicated_with", "reported_by",
    "owns", "controls", "financed_by", "in_conflict_with",
    # Who was behind the wheel is a different fact from who owns it, and in
    # this kind of work it is usually the more useful one.
    "drives", "seen_in",
    # Entity -> Record: this document names or concerns that entity.
    "mentioned_in",
)

# What a relationship means read from the OTHER end. Relationships are stored
# once, as one directed row, and shown on both entities' pages — so without
# this, a "child_of" edge from Anna to Boris appears on Boris's page still
# labelled "child of Anna", which is precisely backwards and is the kind of
# error a family tree makes silently.
#
# A type absent from this map is treated as reading the same both ways
# (family_of, associate_of, communicated_with, in_conflict_with, spouse_of and
# significant_of genuinely do), and gets its own label on both sides.
INVERSE_RELATIONSHIP_TYPES = {
    "parent_of": "child_of",
    "child_of": "parent_of",
    "employed_by": "employs",
    "member_of": "has_member",
    "owns": "owned_by",
    "controls": "controlled_by",
    "financed_by": "finances",
    "located_at": "location_of",
    "present_at": "had_present",
    "reported_by": "reported_on",
    "drives": "driven_by",
    "seen_in": "carried",
    "mentioned_in": "mentions",
}


def inverse_relationship_type(relationship_type: str) -> str:
    """How `relationship_type` reads from the far end of the edge."""
    return INVERSE_RELATIONSHIP_TYPES.get(relationship_type, relationship_type)
RELATIONSHIP_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
CONFIDENCE_LEVELS = ("confirmed", "probable", "possible")

# An analyst's own operational read on where someone or something stands
# relative to the case — not a fact to extract from text, so the extraction
# prompt never proposes it (see worker/ollama_client.py).
#
# This was `faction`, on Person only, and the rename is the point rather than
# cosmetic. A faction is a THING — "Iron Horse Militia" is an Organization
# with its own record, its own alignment and its own members — so asking a
# person to carry one in a dropdown was making a text field do an entity's
# job. Alignment asks the narrower question that dropdown was really for:
# whose side is this one subject on.
#
# It applies to People, Organizations, Sources and Vehicles. Not Locations:
# ground does not take a side, it is more or less permissive, which is a
# different vocabulary (see ENVIRONMENT_VALUES). Not Events or
# Communications, where the question does not arise.
ALIGNMENT_VALUES = ("Friendly", "Neutral", "Unknown", "Hostile")

# "Family" was in the old faction list and is not offered any more: kinship is
# what the `family_of` relationship is for, and mixing it into a
# friendly-to-hostile scale meant a record could not say both that someone is
# a relative and that they are hostile.
#
# Records already carrying it keep it, and keep displaying it. Nothing here
# rewrites an analyst's own classification to something they did not say, and
# blanking it would assert that nobody had ever assessed that person, which is
# untrue. It cannot be set on a record that does not already have it — see
# _check_legacy_alignment below.
LEGACY_ALIGNMENT_VALUES = ("Family",)

# How freely could someone operate here. A Location's answer to the question
# alignment asks of people, and deliberately its own vocabulary: "hostile
# ground" and "a hostile person" are not the same kind of claim.
#
# Denied is distinct from Non-permissive on purpose. Non-permissive means
# working here would be difficult and dangerous; denied means it is not an
# option at all, and an analyst who can only say "non-permissive" about both
# loses the distinction that decides whether a plan exists.
ENVIRONMENT_VALUES = ("Permissive", "Semi-permissive", "Non-permissive",
                      "Denied", "Unknown")

# Is this person alive? Blank is not the same as "Unknown": blank means nobody
# has assessed it, Unknown means someone tried and could not resolve it. An
# app that collapses those two is quietly asserting that no news is a finding.
LIFE_STATUS_VALUES = ("Living", "Deceased", "Unknown")

# Where this person stands relative to whoever is looking for them. Separate
# from life_status because they answer different questions.
#
# "At liberty" rather than "Normal": the other values describe the person's
# situation, and "Normal" describes the analyst's expectations, which is a
# different kind of statement to be putting in the same dropdown.
#
# Missing is deliberately not Evading. Evading asserts that someone is
# choosing to avoid being found; for most people whose whereabouts are
# unknown, the reporting supports no such thing, and offering only Evading
# would push analysts into asserting an intent they cannot see.
DISPOSITION_VALUES = ("At liberty", "Captured", "Detained", "Evading", "Missing")

# A fixed list rather than free text, covering the channels a personal/
# family emergency-communications plan actually uses — mixing phone/email
# with amateur and licensed-radio-service bands, since a Communication
# entity here can represent any of them. `medium_detail` (see
# COMMUNICATION_MEDIUM_DETAIL_LABELS in app.js for what it means per medium)
# is deliberately just free text at this layer rather than N differently-
# typed columns — a phone number, an email address, a radio frequency, and
# a GMRS channel number don't share a type worth modeling, and nothing here
# needs to query "all communications on frequency X" in a structured way.
COMMUNICATION_MEDIUM_VALUES = (
    "Cellphone", "Landline", "Text/SMS", "Satellite Phone", "Email",
    "HF Radio", "VHF Radio", "UHF Radio", "FM Radio", "GMRS", "FRS", "CB Radio",
    "In-Person", "Mail/Courier", "Other",
)

# What kind of thing it is, in the words a witness uses. A fixed list because
# the whole value of the field is that "panel van" is written the same way by
# everyone who enters one -- free text gives you "panel van", "Panel Van",
# "van (panel)" and no way to filter. Deliberately wider than cars: a boat and
# a light aircraft belong to the same question ("what did they arrive in"),
# and giving them their own entity type would split one question into three.
# Make/model/colour stay free text, because constraining those would mean
# shipping and maintaining a list of every manufacturer on earth.
VEHICLE_STYLE_VALUES = (
    "Sedan", "Coupe", "Hatchback", "SUV", "Pickup truck", "Van", "Panel van",
    "Box truck", "Semi-tractor", "Bus", "Motorcycle", "ATV/UTV", "Trailer",
    "Boat", "Aircraft", "Other",
)


# ----------------------------------------------------------------------------
# Pydantic models
# ----------------------------------------------------------------------------

class PersonDetails(BaseModel):
    aliases: Optional[list[str]] = None
    date_of_birth: Optional[date] = None
    alignment: Optional[str] = Field(default=None, pattern="^(" + "|".join(ALIGNMENT_VALUES + LEGACY_ALIGNMENT_VALUES) + ")$")
    occupation: Optional[str] = None
    physical_description: Optional[str] = None
    life_status: Optional[str] = Field(default=None, pattern="^(" + "|".join(LIFE_STATUS_VALUES) + ")$")
    disposition: Optional[str] = Field(default=None, pattern="^(" + "|".join(DISPOSITION_VALUES) + ")$")


class OrganizationDetails(BaseModel):
    org_type: Optional[str] = None
    founded_date: Optional[date] = None
    website: Optional[str] = None
    alignment: Optional[str] = Field(default=None, pattern="^(" + "|".join(ALIGNMENT_VALUES + LEGACY_ALIGNMENT_VALUES) + ")$")


class LocationDetails(BaseModel):
    address: Optional[str] = None
    environment: Optional[str] = Field(default=None, pattern="^(" + "|".join(ENVIRONMENT_VALUES) + ")$")
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)


class EventDetails(BaseModel):
    event_type: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    # "Relevant until" — see the event_details comment in db/init.sql. Not the
    # same as ended_at: when the thing stopped happening and when it stops
    # being worth knowing are different dates, and often very far apart.
    expires_at: Optional[date] = None


class SourceDetails(BaseModel):
    source_type: Optional[str] = None
    reliability_rating: Optional[str] = Field(default=None, pattern="^[A-F]$")
    handling_notes: Optional[str] = None
    alignment: Optional[str] = Field(default=None, pattern="^(" + "|".join(ALIGNMENT_VALUES + LEGACY_ALIGNMENT_VALUES) + ")$")


class CommunicationDetails(BaseModel):
    medium: Optional[str] = Field(default=None, pattern="^(" + "|".join(COMMUNICATION_MEDIUM_VALUES) + ")$")
    # Free text; what it means depends on medium (a phone number for
    # Cellphone, a frequency for HF/VHF/UHF/FM Radio, a channel number for
    # GMRS/FRS/CB, an email address for Email, etc.) — the frontend swaps
    # the field's label to match the selected medium, but nothing is
    # enforced server-side about the format, same as e.g. website above.
    medium_detail: Optional[str] = None
    occurred_at: Optional[datetime] = None
    participants_note: Optional[str] = None


class VehicleDetails(BaseModel):
    make: Optional[str] = None
    model: Optional[str] = None
    color: Optional[str] = None
    # Free text, stored as typed. Plates are not unique across issuing
    # authorities (that is what plate_region is for) and not unique over time
    # either -- a plate moves between vehicles -- so this is a description,
    # not a key. Normalising it on the way in would also destroy the thing
    # worth keeping: exactly how the source wrote it down.
    license_plate: Optional[str] = None
    plate_region: Optional[str] = None
    alignment: Optional[str] = Field(default=None, pattern="^(" + "|".join(ALIGNMENT_VALUES + LEGACY_ALIGNMENT_VALUES) + ")$")
    style: Optional[str] = Field(default=None, pattern="^(" + "|".join(VEHICLE_STYLE_VALUES) + ")$")
    notes: Optional[str] = None


class RecordDetails(BaseModel):
    record_kind: Optional[str] = Field(default=None, max_length=120)
    record_date: Optional[date] = None
    issued_by: Optional[str] = Field(default=None, max_length=256)
    body: Optional[str] = None


DETAIL_MODELS = {
    "person": PersonDetails,
    "organization": OrganizationDetails,
    "location": LocationDetails,
    "event": EventDetails,
    "source": SourceDetails,
    "communication": CommunicationDetails,
    "vehicle": VehicleDetails,
    "record": RecordDetails,
}


class EntityCreate(BaseModel):
    entity_type: str
    name: str = Field(min_length=1, max_length=256)
    description: Optional[str] = None
    details: dict = Field(default_factory=dict)


class EntityUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=256)
    description: Optional[str] = None
    is_active: Optional[bool] = None
    details: Optional[dict] = None


class RelationshipCreate(BaseModel):
    from_entity_id: str
    to_entity_id: str
    relationship_type: str
    confidence: str = "possible"
    discovery_date: Optional[date] = None
    notes: Optional[str] = None


class RelationshipUpdate(BaseModel):
    relationship_type: Optional[str] = None
    confidence: Optional[str] = None
    discovery_date: Optional[date] = None
    notes: Optional[str] = None


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _validate_entity_type(entity_type: str) -> None:
    if entity_type not in ENTITY_TYPES:
        raise HTTPException(status_code=400, detail=f"entity_type must be one of {ENTITY_TYPES}")


def _parsed_details(entity_type: str, details: dict) -> dict:
    """Validate a details block for one kind, strictly.

    Unknown keys are an error, not something to drop quietly. Pydantic ignores
    them by default, which used to mean that sending a Location an `alignment`
    -- a real field, on four other kinds -- created the record, returned 201,
    and silently kept none of it. The person who typed it has no way to tell
    that apart from success.

    The extraction pipeline is the deliberate exception and filters first
    through extraction._lenient_details: a model's proposals are best-effort,
    and one bad field should not block accepting an otherwise-good suggestion.
    A person filling in a form is owed the opposite.
    """
    model = DETAIL_MODELS[entity_type]
    allowed = set(model.model_fields.keys())
    unknown = sorted(set(details or {}) - allowed)
    if unknown:
        elsewhere = {
            key: sorted(k for k, m in DETAIL_MODELS.items() if key in m.model_fields)
            for key in unknown
        }
        hints = "; ".join(
            f"'{key}' belongs to: {', '.join(kinds)}" if kinds else f"'{key}' is not a field"
            for key, kinds in elsewhere.items())
        raise HTTPException(
            status_code=422,
            detail=f"{entity_type} has no field(s) {unknown}. {hints}.")
    try:
        parsed = model(**details)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid details for {entity_type}: {exc}")
    return parsed.model_dump()


def _check_legacy_alignment(cur, entity_type: str, entity_id, details: dict) -> None:
    """Refuse to SET a retired alignment value on a record that lacks it.

    "Family" is kept, not resurrected. A record that already carries it goes
    on carrying it -- rewriting somebody's own classification to a value they
    never chose would be worse than an untidy vocabulary -- but it cannot be
    put on anything new, whether from the form, the bulk importer or a script.
    Blanking it, or replacing it with a current value, is always allowed.
    """
    incoming = details.get("alignment")
    if incoming not in LEGACY_ALIGNMENT_VALUES:
        return
    table, _cols = DETAIL_TABLES[entity_type]
    existing = None
    if entity_id is not None:
        cur.execute(f"SELECT alignment FROM {table} WHERE entity_id = %s", (entity_id,))
        row = cur.fetchone()
        existing = row[0] if row else None
    if existing != incoming:
        raise HTTPException(
            status_code=422,
            detail=f"'{incoming}' is a retired alignment. Use one of: {', '.join(ALIGNMENT_VALUES)}, and record kinship as a family_of relationship.")


def _insert_details(cur, entity_type: str, entity_id: str, details: dict) -> None:
    _check_legacy_alignment(cur, entity_type, None, details)
    table, cols = DETAIL_TABLES[entity_type]
    values = [details.get(c) for c in cols]
    col_list = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    cur.execute(
        f"INSERT INTO {table} (entity_id, {col_list}) VALUES (%s, {placeholders})",
        [entity_id, *values],
    )


def _update_details(cur, entity_type: str, entity_id: str, details: dict) -> None:
    """Partial update — only columns actually present in `details` are
    touched, so PATCHing one field never clobbers the others back to NULL."""
    _check_legacy_alignment(cur, entity_type, entity_id, details)
    table, cols = DETAIL_TABLES[entity_type]
    set_cols = [c for c in cols if c in details]
    if not set_cols:
        return
    set_clause = ", ".join(f"{c} = %s" for c in set_cols)
    values = [details[c] for c in set_cols]
    cur.execute(
        f"UPDATE {table} SET {set_clause} WHERE entity_id = %s",
        [*values, entity_id],
    )


def _apply_location_derived_fields(cur, entity_id: str, address: Optional[str], lat: Optional[float], lng: Optional[float]) -> None:
    """The one place that decides "does this Location need geocoding" and
    "what's its Maidenhead grid square right now", given the row's current
    (address, lat, lng) — called after any create/update that touches those
    three columns, in the same transaction, so location_details never has a
    moment where lat/lng exist but the grid hasn't caught up (or vice
    versa).

    Rules, in order:
    - Both coordinates present -> (re)compute the grid square. geocode_status
      is left alone unless it still says a request is outstanding
      ('pending'/'processing') — an editor fixing a typo in the address
      after a successful (or manually-entered) geocode shouldn't wipe that
      history, since the coordinates themselves aren't changing.
    - No coordinates but a non-blank address -> queue for the worker
      (geocode_status='pending'), clearing any stale grid/error. This is
      also how an analyst forces a re-geocode: blank out lat and lng in the
      same edit that fixes the address.
    - Neither -> nothing to derive; clear everything back to NULL.
    """
    if lat is not None and lng is not None:
        grid = maidenhead_locator(lat, lng)
        cur.execute(
            "UPDATE location_details SET maidenhead_grid = %s, "
            "geocode_status = CASE WHEN geocode_status IN ('pending', 'processing') THEN NULL ELSE geocode_status END "
            "WHERE entity_id = %s",
            (grid, entity_id),
        )
    elif address and address.strip():
        cur.execute(
            "UPDATE location_details SET maidenhead_grid = NULL, geocode_status = 'pending', "
            "geocode_error = NULL, geocoded_at = NULL WHERE entity_id = %s",
            (entity_id,),
        )
    else:
        cur.execute(
            "UPDATE location_details SET maidenhead_grid = NULL, geocode_status = NULL, geocode_error = NULL "
            "WHERE entity_id = %s",
            (entity_id,),
        )


def _fetch_details(cur, entity_type: str, entity_id: str) -> dict:
    table, cols = DETAIL_TABLES[entity_type]
    col_list = ", ".join(cols)
    cur.execute(f"SELECT {col_list} FROM {table} WHERE entity_id = %s", (entity_id,))
    row = cur.fetchone()
    if row is None:
        return {}
    return dict(zip(cols, row))


def _entity_exists(cur, entity_id: str, active_only: bool = False) -> bool:
    query = "SELECT 1 FROM entities WHERE id = %s"
    params = [entity_id]
    if active_only:
        query += " AND is_active = TRUE"
    cur.execute(query, params)
    return cur.fetchone() is not None


def _entity_row_to_dict(row) -> dict:
    (entity_id, entity_type, name, description, is_active, created_by, created_at,
     updated_at, merged_into) = row
    return {
        "id": entity_id,
        "entity_type": entity_type,
        "name": name,
        "description": description,
        "is_active": is_active,
        "created_by": created_by,
        "created_at": created_at,
        "updated_at": updated_at,
        # Set when this record was folded into another (see api/merge.py). An
        # archived record with no explanation is a dead end; this is how every
        # view that shows one can say where it went instead.
        "merged_into": merged_into,
    }


ENTITY_CORE_COLS = ("id, entity_type, name, description, is_active, created_by, "
                    "created_at, updated_at, merged_into")


# ----------------------------------------------------------------------------
# Entity endpoints
# ----------------------------------------------------------------------------

@router.get("/relationship-types")
def relationship_types(user: dict = Depends(auth.require_user)):
    # `inverses` lets the frontend word an incoming edge correctly without
    # keeping its own copy of the mapping, which would drift.
    return {
        "suggested": list(SUGGESTED_RELATIONSHIP_TYPES),
        "inverses": dict(INVERSE_RELATIONSHIP_TYPES),
    }


@router.post("/entities", status_code=201)
def create_entity(payload: EntityCreate, user: dict = Depends(auth.require_user)):
    _validate_entity_type(payload.entity_type)
    details = _parsed_details(payload.entity_type, payload.details)
    entity_id = generate_id(payload.entity_type, payload.name)

    with db_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO entities (id, entity_type, name, description, created_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            (entity_id, payload.entity_type, payload.name, payload.description, user["id"]),
        )
        _insert_details(cur, payload.entity_type, entity_id, details)
        if payload.entity_type == "location":
            _apply_location_derived_fields(cur, entity_id, details.get("address"), details.get("lat"), details.get("lng"))

    audit.record("entity.create", user=user, object_type="entity",
                 object_id=entity_id, object_label=payload.name,
                 detail={"entity_type": payload.entity_type})
    return get_entity(entity_id, user=user)


@router.get("/entities")
def list_entities(
    entity_type: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None, description="Case-insensitive substring match on name"),
    active_only: bool = Query(default=True),
    hide_expired: bool = Query(
        default=False,
        description="Leave out Events whose relevant-until date has passed. Off by "
                    "default: an expired Event is flagged, never hidden, unless "
                    "someone asks for it to be."),
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(auth.require_user),
):
    if entity_type is not None:
        _validate_entity_type(entity_type)

    where = []
    params: list = []
    if entity_type:
        where.append("entity_type = %s")
        params.append(entity_type)
    if q:
        where.append("name ILIKE %s")
        params.append(f"%{q}%")
    if active_only:
        where.append("is_active = TRUE")
    if hide_expired:
        # Only Events can expire, so the condition has to exempt every other
        # type explicitly — a bare "expires_at >= today" would silently drop
        # every Person and Location in the app, none of which has the column.
        where.append(
            "(entity_type <> 'event' OR id IN ("
            "  SELECT entity_id FROM event_details"
            "  WHERE expires_at IS NULL OR expires_at >= CURRENT_DATE))"
        )
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM entities {where_clause}", params)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT {ENTITY_CORE_COLS} FROM entities {where_clause} "
            "ORDER BY name LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        rows = [_entity_row_to_dict(r) for r in cur.fetchall()]
        _attach_person_status(cur, rows)
        _attach_event_expiry(cur, rows)
        _attach_alignment(cur, rows)

    return {"items": rows, "total": total, "limit": limit, "offset": offset}


# The four kinds that carry an alignment, and where each keeps it. A Location
# has an `environment` instead and is deliberately absent: ground does not take
# a side.
_ALIGNMENT_TABLES = (
    ("person", "person_details"),
    ("organization", "organization_details"),
    ("source", "source_details"),
    ("vehicle", "vehicle_details"),
)


def fetch_alignments(cur, entity_ids: list) -> dict:
    """id -> alignment, for whichever of these ids have one.

    One UNION rather than four round trips, and public because three other
    modules need the same answer: a hostile record is marked wherever its name
    appears, and "wherever" spans the list, the graph and a record's own
    relationships. Ids without an alignment are simply absent from the result.
    """
    ids = [i for i in entity_ids if i]
    if not ids:
        return {}
    union = " UNION ALL ".join(
        f"SELECT entity_id, alignment FROM {table} "
        "WHERE entity_id = ANY(%s) AND alignment IS NOT NULL"
        for _kind, table in _ALIGNMENT_TABLES)
    cur.execute(union, [ids] * len(_ALIGNMENT_TABLES))
    return {row[0]: row[1] for row in cur.fetchall()}


def _attach_alignment(cur, rows: list[dict]) -> None:
    """Put `alignment` onto rows in a LIST response.

    Same shape as _attach_person_status below and for the same reason: a second
    query on ids already fetched, rather than widening ENTITY_CORE_COLS and
    putting NULLs on every row in the app.

    It earns its round trip on the same test those two fields pass — it changes
    what an analyst does on seeing a name in a list. A hostile record reads
    differently from the one above it, and having to open each one to find that
    out is how a list of forty names stops being scannable.
    """
    alignments = fetch_alignments(cur, [r["id"] for r in rows])
    for row in rows:
        row["alignment"] = alignments.get(row["id"])


def _attach_person_status(cur, rows: list[dict]) -> None:
    """Put life_status/disposition onto Person rows in a LIST response.

    Deliberately a second query on the ids already fetched rather than a join
    baked into ENTITY_CORE_COLS: that constant is shared by every read path in
    this module, and widening it for one type's two columns would put NULLs on
    every Location and Event row in the app to save one round trip on a page
    that is already paginated.

    These two are the only detail fields the list view carries, because they
    are the only ones whose value changes what an analyst does next on seeing
    a name in a list — a person being Deceased or Captured is not a detail you
    should have to open the record to find out.
    """
    person_ids = [r["id"] for r in rows if r["entity_type"] == "person"]
    if not person_ids:
        return
    cur.execute(
        "SELECT entity_id, life_status, disposition FROM person_details "
        "WHERE entity_id = ANY(%s)",
        (person_ids,),
    )
    statuses = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    for row in rows:
        if row["entity_type"] != "person":
            continue
        life, disposition = statuses.get(row["id"], (None, None))
        row["life_status"] = life
        row["disposition"] = disposition


def _event_is_expired(expires_at) -> bool:
    """An Event is expired once its relevant-until date is in the past.

    Compared against the local date rather than a timestamp: the field is a
    date, and "relevant until the 14th" plainly means through the end of the
    14th, not from midnight at its start.
    """
    if expires_at is None:
        return False
    if isinstance(expires_at, datetime):
        expires_at = expires_at.date()
    return expires_at < date.today()


def _attach_event_expiry(cur, rows: list[dict]) -> None:
    """Put expires_at/is_expired onto Event rows in a LIST response, so the
    list can badge a stale Event without a request per row. Same second-query
    approach, and the same reasoning, as _attach_person_status."""
    event_ids = [r["id"] for r in rows if r["entity_type"] == "event"]
    if not event_ids:
        return
    cur.execute(
        "SELECT entity_id, expires_at FROM event_details WHERE entity_id = ANY(%s)",
        (event_ids,),
    )
    expiries = {r[0]: r[1] for r in cur.fetchall()}
    for row in rows:
        if row["entity_type"] != "event":
            continue
        expires = expiries.get(row["id"])
        row["expires_at"] = expires.isoformat() if expires else None
        row["is_expired"] = _event_is_expired(expires)


@router.get("/entities/{entity_id}")
def get_entity(entity_id: str, user: dict = Depends(auth.require_user)):
    with db_cursor() as cur:
        cur.execute(f"SELECT {ENTITY_CORE_COLS} FROM entities WHERE id = %s", (entity_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Entity not found")
        entity = _entity_row_to_dict(row)
        entity["details"] = _fetch_details(cur, entity["entity_type"], entity_id)
        # Computed, never stored: "expired" is a fact about today, not about
        # the record, and a stored flag would be wrong the morning after it
        # was written.
        if entity["entity_type"] == "event":
            entity["is_expired"] = _event_is_expired(entity["details"].get("expires_at"))

        cur.execute(
            """
            SELECT r.id, r.relationship_type, r.confidence, r.discovery_date, r.notes,
                   'outgoing', e2.id, e2.name, e2.entity_type
            FROM relationships r JOIN entities e2 ON e2.id = r.to_entity_id
            WHERE r.from_entity_id = %s
            UNION ALL
            SELECT r.id, r.relationship_type, r.confidence, r.discovery_date, r.notes,
                   'incoming', e2.id, e2.name, e2.entity_type
            FROM relationships r JOIN entities e2 ON e2.id = r.from_entity_id
            WHERE r.to_entity_id = %s
            """,
            (entity_id, entity_id),
        )
        # `relationship_type` is always the stored direction (from -> to);
        # `reads_as` is how it should be worded on THIS entity's page. For an
        # outgoing edge those are the same. For an incoming one, reads_as is
        # the inverse, so Boris's page says "parent of Anna" for the same row
        # that Anna's page shows as "child of Boris" — one stored fact, worded
        # correctly at both ends rather than backwards at one of them.
        relationship_rows = cur.fetchall()
        # The other end's alignment, so a hostile neighbour is marked in the
        # list on this record's page rather than only on its own.
        neighbour_alignments = fetch_alignments(cur, [r[6] for r in relationship_rows])
        entity["relationships"] = [
            {
                "id": r[0], "relationship_type": r[1], "confidence": r[2],
                "discovery_date": r[3], "notes": r[4],
                "direction": r[5],
                "reads_as": r[1] if r[5] == "outgoing" else inverse_relationship_type(r[1]),
                "other_entity_id": r[6], "other_entity_name": r[7], "other_entity_type": r[8],
                "other_entity_alignment": neighbour_alignments.get(r[6]),
            }
            for r in relationship_rows
        ]

        # Only three types can hold them (see contacts.CONTACTABLE_TYPES); the
        # others get an empty list rather than the key being absent, so a
        # client never has to special-case the shape of an entity.
        entity["contacts"] = (
            contacts.fetch_for_entity(cur, entity_id)
            if entity["entity_type"] in contacts.CONTACTABLE_TYPES else []
        )

        cur.execute(
            """
            SELECT rep.id, rep.title, rep.status, rep.created_at
            FROM report_entities re JOIN reports rep ON rep.id = re.report_id
            WHERE re.entity_id = %s ORDER BY rep.created_at DESC
            """,
            (entity_id,),
        )
        entity["reports"] = [
            {"id": r[0], "title": r[1], "status": r[2], "created_at": r[3]}
            for r in cur.fetchall()
        ]

        cur.execute(
            "SELECT id, filename, mime_type, file_size_bytes, uploaded_at, extraction_status "
            "FROM attachments WHERE entity_id = %s ORDER BY uploaded_at",
            (entity_id,),
        )
        entity["attachments"] = [
            {
                "id": r[0], "filename": r[1], "mime_type": r[2], "file_size_bytes": r[3],
                "uploaded_at": r[4], "extraction_status": r[5],
            }
            for r in cur.fetchall()
        ]

    return entity


@router.patch("/entities/{entity_id}")
def update_entity(entity_id: str, payload: EntityUpdate, user: dict = Depends(auth.require_user)):
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT entity_type, name FROM entities WHERE id = %s", (entity_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Entity not found")
        entity_type, previous_name = row

        core_updates = payload.model_dump(exclude_unset=True, exclude={"details"})
        if core_updates:
            set_clause = ", ".join(f"{k} = %s" for k in core_updates)
            cur.execute(
                f"UPDATE entities SET {set_clause}, updated_at = now() WHERE id = %s",
                [*core_updates.values(), entity_id],
            )
        elif payload.details is not None:
            # A details-only PATCH still touches this entity — without this,
            # entities.updated_at would silently stay stale on any edit that
            # only changed detail-table fields (e.g. occupation, a source's
            # reliability rating), which is exactly the timestamp a report
            # editor or the correlation worker would reasonably trust as
            # "last touched."
            cur.execute("UPDATE entities SET updated_at = now() WHERE id = %s", (entity_id,))

        if payload.details is not None:
            # Partial detail update: validate + type-coerce only the keys
            # actually provided (every field on every detail model is
            # Optional with a None default, so instantiating the model with
            # a subset never fails on "missing required field" — only on a
            # genuinely bad value), then update only those columns so
            # unrelated fields already on the row survive untouched.
            model = DETAIL_MODELS[entity_type]
            allowed = set(model.model_fields.keys())
            unknown = set(payload.details.keys()) - allowed
            if unknown:
                raise HTTPException(status_code=422, detail=f"unknown detail fields for {entity_type}: {sorted(unknown)}")
            try:
                # model_dump(include=...) keeps only the caller-provided
                # keys, but with pydantic's coerced types (e.g. a "1990-05-01"
                # string becomes a real date object) — passing psycopg2 the
                # coerced value instead of the raw JSON string is what makes
                # this robust to any date format pydantic accepts, not just
                # whatever Postgres would accept as a literal.
                coerced = model(**payload.details).model_dump(include=set(payload.details.keys()))
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"invalid details for {entity_type}: {exc}")
            _update_details(cur, entity_type, entity_id, coerced)
            if entity_type == "location":
                # Re-derive from the row's full current state, not just the
                # keys this particular PATCH touched — e.g. an address-only
                # edit still needs to know the (unchanged) lat/lng to decide
                # whether anything about the grid/geocode status should move.
                cur.execute("SELECT address, lat, lng FROM location_details WHERE entity_id = %s", (entity_id,))
                addr, lat, lng = cur.fetchone()
                _apply_location_derived_fields(cur, entity_id, addr, lat, lng)

    # Archiving is the closest thing this app has to deleting an entity, so it
    # gets its own action rather than hiding inside a generic "update" that an
    # auditor would have to open the detail payload to understand.
    if "is_active" in core_updates:
        action = "entity.reactivate" if core_updates["is_active"] else "entity.archive"
    else:
        action = "entity.update"
    changed = sorted(set(core_updates) | set(payload.details or {}))
    audit.record(action, user=user, object_type="entity", object_id=entity_id,
                 object_label=core_updates.get("name") or previous_name,
                 detail={"entity_type": entity_type, "fields": changed})
    return get_entity(entity_id, user=user)


@router.post("/entities/{entity_id}/retry-geocode")
def retry_geocode(entity_id: str, user: dict = Depends(auth.require_user)):
    """Resets a failed geocode back to 'pending' so the worker picks it up
    again next poll cycle. Deliberately narrow: refuses if this isn't a
    Location, has no address to geocode, or already has coordinates (clear
    lat/lng first if the goal is to force a fresh geocode over ones you
    already have — see _apply_location_derived_fields) rather than silently
    no-op'ing or overwriting something an analyst may have set on purpose."""
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT entity_type FROM entities WHERE id = %s", (entity_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Entity not found")
        if row[0] != "location":
            raise HTTPException(status_code=400, detail="retry-geocode only applies to location entities")

        cur.execute("SELECT address, lat, lng FROM location_details WHERE entity_id = %s", (entity_id,))
        address, lat, lng = cur.fetchone()
        if not address or not address.strip():
            raise HTTPException(status_code=400, detail="This location has no address to geocode")
        if lat is not None and lng is not None:
            raise HTTPException(status_code=400, detail="This location already has coordinates — clear latitude/longitude first to re-geocode")

        cur.execute(
            "UPDATE location_details SET geocode_status = 'pending', geocode_error = NULL WHERE entity_id = %s",
            (entity_id,),
        )

    audit.record("entity.retry_geocode", user=user, object_type="entity", object_id=entity_id)
    return get_entity(entity_id, user=user)


# ----------------------------------------------------------------------------
# Relationship endpoints
# ----------------------------------------------------------------------------

@router.post("/relationships", status_code=201)
def create_relationship(payload: RelationshipCreate, user: dict = Depends(auth.require_user)):
    if payload.from_entity_id == payload.to_entity_id:
        raise HTTPException(status_code=400, detail="An entity cannot have a relationship with itself")
    if not RELATIONSHIP_TYPE_RE.match(payload.relationship_type):
        raise HTTPException(
            status_code=400,
            detail="relationship_type must be lowercase snake_case, e.g. 'employed_by'",
        )
    if payload.confidence not in CONFIDENCE_LEVELS:
        raise HTTPException(status_code=400, detail=f"confidence must be one of {CONFIDENCE_LEVELS}")

    with db_cursor(commit=True) as cur:
        if not _entity_exists(cur, payload.from_entity_id):
            raise HTTPException(status_code=404, detail=f"from_entity_id '{payload.from_entity_id}' not found")
        if not _entity_exists(cur, payload.to_entity_id):
            raise HTTPException(status_code=404, detail=f"to_entity_id '{payload.to_entity_id}' not found")

        cur.execute(
            """
            INSERT INTO relationships
                (from_entity_id, to_entity_id, relationship_type, confidence, discovery_date, notes, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (
                payload.from_entity_id, payload.to_entity_id, payload.relationship_type,
                payload.confidence, payload.discovery_date, payload.notes, user["id"],
            ),
        )
        rel_id, created_at = cur.fetchone()

    audit.record("relationship.create", user=user, object_type="relationship", object_id=rel_id,
                 object_label=f"{payload.from_entity_id} {payload.relationship_type} {payload.to_entity_id}",
                 detail={"from": payload.from_entity_id, "to": payload.to_entity_id,
                         "relationship_type": payload.relationship_type,
                         "confidence": payload.confidence})
    return {
        "id": rel_id,
        "from_entity_id": payload.from_entity_id,
        "to_entity_id": payload.to_entity_id,
        "relationship_type": payload.relationship_type,
        "confidence": payload.confidence,
        "discovery_date": payload.discovery_date,
        "notes": payload.notes,
        "created_at": created_at,
    }


@router.patch("/relationships/{relationship_id}")
def update_relationship(relationship_id: int, payload: RelationshipUpdate, user: dict = Depends(auth.require_user)):
    updates = payload.model_dump(exclude_unset=True)
    if "relationship_type" in updates and not RELATIONSHIP_TYPE_RE.match(updates["relationship_type"]):
        raise HTTPException(status_code=400, detail="relationship_type must be lowercase snake_case")
    if "confidence" in updates and updates["confidence"] not in CONFIDENCE_LEVELS:
        raise HTTPException(status_code=400, detail=f"confidence must be one of {CONFIDENCE_LEVELS}")
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    with db_cursor(commit=True) as cur:
        cur.execute("SELECT 1 FROM relationships WHERE id = %s", (relationship_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Relationship not found")
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(
            f"UPDATE relationships SET {set_clause} WHERE id = %s",
            [*updates.values(), relationship_id],
        )
    audit.record("relationship.update", user=user, object_type="relationship",
                 object_id=relationship_id, detail={"fields": sorted(updates)})
    return {"id": relationship_id, **updates}


@router.delete("/relationships/{relationship_id}", status_code=204)
def delete_relationship(relationship_id: int, user: dict = Depends(auth.require_user)):
    with db_cursor(commit=True) as cur:
        # Read it before deleting: once the row is gone there is nothing left
        # to describe what was removed, which is exactly what the audit entry
        # needs to say.
        cur.execute(
            "SELECT from_entity_id, to_entity_id, relationship_type FROM relationships WHERE id = %s",
            (relationship_id,),
        )
        existing = cur.fetchone()
        cur.execute("DELETE FROM relationships WHERE id = %s", (relationship_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Relationship not found")

    audit.record(
        "relationship.delete", user=user, object_type="relationship", object_id=relationship_id,
        object_label=f"{existing[0]} {existing[2]} {existing[1]}" if existing else None,
        detail={"from": existing[0], "to": existing[1], "relationship_type": existing[2]} if existing else None,
    )
    return None
