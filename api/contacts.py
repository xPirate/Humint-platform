"""Contact points: how you would actually reach an entity.

A repeatable list hanging off a Person, Organization or Location, rather than
phone/email/address columns on each detail table. One organisation has a
switchboard, a press office and an after-hours number; flattening that into
one column each loses whichever one you didn't pick, and "second number" ends
up jammed into a free-text note where nothing can find it.

Deliberately not the same thing as a Communication entity. A Communication is
a channel that is itself an object of interest — a radio net being monitored,
a number that keeps appearing in reporting — and belongs in the entity list,
the graph and the correlation queue. A contact point is directory information:
the office switchboard is not a lead, it is how you phone the office. Putting
switchboards in the entity graph would bury the channels that actually matter
under a phone book.

Only three entity types take contact points. An Event or a Communication has
no one to contact; a Source's handling arrangements are a different and more
sensitive thing than a phone number in a directory, and deliberately stay in
that type's handling_notes rather than becoming a list anyone can add to.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

import audit
import auth
from db import db_cursor

router = APIRouter(prefix="/api", tags=["contacts"])

# Which entity types can hold contact points. See the module docstring for why
# this is not all six.
CONTACTABLE_TYPES = ("person", "organization", "location")

# Validated here rather than with a DB CHECK, same as relationship_type and
# alignment — a vocabulary a deployment can outgrow shouldn't need a migration.
CONTACT_KINDS = (
    "Phone", "Mobile", "Email", "Address", "Radio", "Messaging",
    "Social", "Website", "Other",
)

MAX_CONTACTS_PER_ENTITY = 50

COLUMNS = ("id", "entity_id", "kind", "label", "value", "notes",
           "is_preferred", "created_by", "created_at", "updated_at")
_COL_SQL = ", ".join(COLUMNS)


def _require_non_blank(value):
    """min_length=1 alone lets "   " through, which then strips to nothing and
    stores a contact point with no contact in it."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("value cannot be blank")
    return stripped


class ContactCreate(BaseModel):
    kind: str
    label: Optional[str] = Field(default=None, max_length=120)
    value: str = Field(min_length=1, max_length=500)
    notes: Optional[str] = None
    is_preferred: bool = False

    _clean_value = field_validator("value")(_require_non_blank)


class ContactUpdate(BaseModel):
    kind: Optional[str] = None
    label: Optional[str] = Field(default=None, max_length=120)
    value: Optional[str] = Field(default=None, min_length=1, max_length=500)
    notes: Optional[str] = None
    is_preferred: Optional[bool] = None

    _clean_value = field_validator("value")(_require_non_blank)


def _row_to_dict(row) -> dict:
    out = dict(zip(COLUMNS, row))
    for key in ("created_at", "updated_at"):
        if isinstance(out[key], datetime):
            out[key] = out[key].isoformat()
    return out


def _validate_kind(kind: str) -> str:
    kind = (kind or "").strip()
    if kind not in CONTACT_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {', '.join(CONTACT_KINDS)}",
        )
    return kind


def _entity_or_404(cur, entity_id: str) -> tuple[str, str]:
    cur.execute("SELECT entity_type, name FROM entities WHERE id = %s", (entity_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    if row[0] not in CONTACTABLE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"{row[0].title()} entities don't hold contact details — "
                   f"only {', '.join(t.title() for t in CONTACTABLE_TYPES)} do.",
        )
    return row[0], row[1]


def fetch_for_entity(cur, entity_id: str) -> list[dict]:
    """Used by entities.get_entity so a contact list arrives with the record
    rather than needing a second request from every client."""
    cur.execute(
        f"SELECT {_COL_SQL} FROM contact_points WHERE entity_id = %s "
        # Preferred first, then oldest first: the row someone marked as the
        # way to make contact should not move around as others are added.
        "ORDER BY is_preferred DESC, id",
        (entity_id,),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def preferred_for_entity(cur, entity_id: str) -> list[dict]:
    """Only the rows marked preferred — what an exported PDF carries."""
    return [c for c in fetch_for_entity(cur, entity_id) if c["is_preferred"]]


@router.get("/contact-kinds")
def contact_kinds(user: dict = Depends(auth.require_user)):
    return {"kinds": list(CONTACT_KINDS), "entity_types": list(CONTACTABLE_TYPES)}


@router.get("/entities/{entity_id}/contacts")
def list_contacts(entity_id: str, user: dict = Depends(auth.require_user)):
    with db_cursor() as cur:
        _entity_or_404(cur, entity_id)
        return {"items": fetch_for_entity(cur, entity_id)}


@router.post("/entities/{entity_id}/contacts", status_code=201)
def create_contact(entity_id: str, payload: ContactCreate,
                   user: dict = Depends(auth.require_user)):
    kind = _validate_kind(payload.kind)
    with db_cursor(commit=True) as cur:
        _, entity_name = _entity_or_404(cur, entity_id)
        cur.execute("SELECT COUNT(*) FROM contact_points WHERE entity_id = %s", (entity_id,))
        if cur.fetchone()[0] >= MAX_CONTACTS_PER_ENTITY:
            raise HTTPException(
                status_code=400,
                detail=f"That's {MAX_CONTACTS_PER_ENTITY} contact points on one entity, the maximum.",
            )
        cur.execute(
            "INSERT INTO contact_points (entity_id, kind, label, value, notes, is_preferred, created_by) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_COL_SQL}",
            (entity_id, kind, payload.label, payload.value.strip(), payload.notes,
             payload.is_preferred, user["id"]),
        )
        created = _row_to_dict(cur.fetchone())

    # The VALUE is not audited, only that a contact of this kind was added.
    # An audit log that recorded every phone number written into the app would
    # become a second, less protected copy of the directory — the same reason
    # entity edits record field names and not field values.
    audit.record("contact.create", user=user, object_type="entity",
                 object_id=entity_id, object_label=entity_name,
                 detail={"kind": kind, "label": payload.label,
                         "preferred": payload.is_preferred,
                         "contact_id": created["id"]})
    return created


@router.patch("/contacts/{contact_id}")
def update_contact(contact_id: int, payload: ContactUpdate,
                   user: dict = Depends(auth.require_user)):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")
    if "kind" in updates and updates["kind"] is not None:
        updates["kind"] = _validate_kind(updates["kind"])
    if "value" in updates and updates["value"] is not None:
        updates["value"] = updates["value"].strip()

    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT c.entity_id, e.name FROM contact_points c "
            "JOIN entities e ON e.id = c.entity_id WHERE c.id = %s",
            (contact_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Contact point not found")
        entity_id, entity_name = row

        assignments = ", ".join(f"{col} = %s" for col in updates)
        cur.execute(
            f"UPDATE contact_points SET {assignments}, updated_at = now() "
            f"WHERE id = %s RETURNING {_COL_SQL}",
            [*updates.values(), contact_id],
        )
        updated = _row_to_dict(cur.fetchone())

    audit.record("contact.update", user=user, object_type="entity",
                 object_id=entity_id, object_label=entity_name,
                 detail={"fields": sorted(updates), "contact_id": contact_id})
    return updated


@router.delete("/contacts/{contact_id}", status_code=204)
def delete_contact(contact_id: int, user: dict = Depends(auth.require_user)):
    """Hard delete, unlike an entity. A wrong phone number is a small,
    mechanical fact with no history hanging off it — the same reasoning that
    makes relationships deletable while entities are only ever archived."""
    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT c.entity_id, c.kind, c.label, e.name FROM contact_points c "
            "JOIN entities e ON e.id = c.entity_id WHERE c.id = %s",
            (contact_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Contact point not found")
        entity_id, kind, label, entity_name = row
        cur.execute("DELETE FROM contact_points WHERE id = %s", (contact_id,))

    audit.record("contact.delete", user=user, object_type="entity",
                 object_id=entity_id, object_label=entity_name,
                 detail={"kind": kind, "label": label, "contact_id": contact_id})
    return None
