"""Bulk entity import via CSV: download a per-type template, fill it in,
upload it back. Meant for exactly the case a single "+ New Entity" form is
bad at — adding a dozen (or a hundred) People/Organizations/Locations at
once from a list you already have somewhere else.

Deliberately reuses entities.py's own DETAIL_TABLES/DETAIL_MODELS/
_parsed_details/_insert_details rather than re-declaring per-type field
lists here — that dict is the single source of truth for what each entity
type's fields are, and a template or an import path that drifted from it
would be a worse bug than the coupling is worth. Row-level validation goes
through the exact same Pydantic models the single-entity POST /api/entities
endpoint uses, so a row that would be rejected there is rejected here with
the same message, not a second, possibly-inconsistent set of rules.

Partial success, not all-or-nothing: unlike every other write endpoint in
this app (one HTTP request = one atomic change), an import is dozens or
hundreds of logically independent rows. Aborting the whole upload because
row 47 had a typo would mean re-uploading everything, including the 46 good
rows, after every single fix. Instead each row gets its own commit; a
csv the analyst can fix one row at a time. This is the one place in the
codebase that manages its own transactions per-row instead of using
db.db_cursor's one-transaction-per-request pattern, and it's why.
"""

import csv
import io
import os
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile

import auth
import audit
import entities
from db import get_conn
from idgen import generate_id

router = APIRouter(prefix="/api", tags=["bulk-import"])

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024
# A generous cap, not a real expected size — this exists so a mistaken
# upload (the wrong file entirely, or a spreadsheet export gone huge) fails
# fast with a clear message instead of one request processing for minutes.
# Split a genuinely bigger batch into multiple files.
MAX_IMPORT_ROWS = 5000

# Per-type hint text for each detail-table column, shown in the template's
# second row (see build_template_csv) so an analyst filling it in doesn't
# have to cross-reference the README to learn that alignment is a fixed set
# of values or that dates go YYYY-MM-DD. Keys must match
# entities.DETAIL_TABLES[type][1] exactly; nothing enforces that at import
# time beyond the two staying next to each other in the codebase.
TEMPLATE_HINTS: dict[str, dict[str, str]] = {
    "person": {
        "aliases": "optional; semicolon-separated, e.g. Johnny; JS",
        "date_of_birth": "optional; YYYY-MM-DD",
        "alignment": "optional; one of: " + ", ".join(entities.ALIGNMENT_VALUES),
        "occupation": "optional; free text",
        "physical_description": "optional; free text",
        "life_status": "optional; one of: " + ", ".join(entities.LIFE_STATUS_VALUES),
        "disposition": "optional; one of: " + ", ".join(entities.DISPOSITION_VALUES),
    },
    "organization": {
        "org_type": "optional; free text, e.g. company, NGO, agency, informal group",
        "founded_date": "optional; YYYY-MM-DD",
        "website": "optional; URL",
        "alignment": "optional; one of: " + ", ".join(entities.ALIGNMENT_VALUES),
    },
    "location": {
        "address": "optional; free text. Leave lat/lng blank and this gets geocoded automatically",
        "environment": "optional; one of: " + ", ".join(entities.ENVIRONMENT_VALUES),
        "lat": "optional; decimal degrees, e.g. 35.4676. Leave blank (with lng) to auto-geocode from address instead",
        "lng": "optional; decimal degrees, e.g. -97.5164. Leave blank (with lat) to auto-geocode from address instead",
    },
    "event": {
        "event_type": "optional; free text",
        "started_at": "optional; YYYY-MM-DD or YYYY-MM-DD HH:MM",
        "ended_at": "optional; YYYY-MM-DD or YYYY-MM-DD HH:MM",
        "expires_at": "optional; YYYY-MM-DD. When this stops being current intelligence "
                      "— not when the event ended",
    },
    "source": {
        "source_type": "optional; e.g. human, document, open-source, signal",
        "reliability_rating": "optional; one letter A-F (NATO/Admiralty reliability)",
        "handling_notes": "optional; free text",
    },
    "communication": {
        "medium": "optional; one of: " + ", ".join(entities.COMMUNICATION_MEDIUM_VALUES),
        "medium_detail": "optional; meaning depends on medium — phone number, email address, frequency, or channel",
        "occurred_at": "optional; YYYY-MM-DD or YYYY-MM-DD HH:MM",
        "participants_note": "optional; free text",
    },
    "vehicle": {
        "make": "optional; free text, e.g. Ford",
        "model": "optional; free text, e.g. Transit",
        "color": "optional; free text, e.g. white",
        "license_plate": "optional; as written on the plate — not normalised, "
                         "so record it the way your source gave it",
        "plate_region": "optional; issuing state, province or country",
        "style": "optional; one of: " + ", ".join(entities.VEHICLE_STYLE_VALUES),
        "notes": "optional; free text — damage, markings, equipment, who drives it",
    },
    "record": {
        "record_kind": "optional; e.g. letter, bank statement, registration",
        "record_date": "optional; YYYY-MM-DD, the date on the document",
        "issued_by": "optional; who produced it, e.g. an insurer or an agency",
        "body": "optional; the document's full text",
    },
}

# The only detail field across all eight types that's a list rather than a
# scalar (Person.aliases) — encoded in one CSV cell as semicolon-separated
# values and split back into a list here before Pydantic sees it. If a
# second list field is ever added to any type, it needs an entry here too.
LIST_DETAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "person": ("aliases",),
}


def build_template_csv(entity_type: str) -> str:
    # Deliberately the Pydantic model's declared fields, not
    # entities.DETAIL_TABLES[entity_type]'s column list directly — for most
    # types those are the same set, but location's detail table also carries
    # server-derived, read-only columns (maidenhead_grid, geocode_status,
    # geocode_error, geocoded_at — see api/entities.py's
    # _apply_location_derived_fields) that would otherwise show up as
    # fillable template columns despite anything typed into them being
    # silently ignored (Pydantic drops unrecognized kwargs by default). Using
    # the model here keeps the template limited to what an import can
    # actually set, the same set PATCH accepts.
    detail_cols = list(entities.DETAIL_MODELS[entity_type].model_fields.keys())
    columns = ["name", "description", *detail_cols]
    hints = {"name": "required", "description": "optional; free text"}
    hints.update(TEMPLATE_HINTS[entity_type])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    # Row 2 is a live example row, not just a comment — every cell starts
    # with "#" so it reads unambiguously as "don't import this", and the
    # importer specifically skips any row whose name cell starts with "#"
    # (see _looks_like_comment_row) so leaving this row in place by mistake
    # doesn't create a junk entity literally named after the hint text.
    writer.writerow([f"# {hints.get(c, 'optional')}" for c in columns])
    return buf.getvalue()


def _row_is_blank(row: dict) -> bool:
    return not any((v or "").strip() for v in row.values())


def _looks_like_comment_row(row: dict) -> bool:
    return (row.get("name") or "").strip().startswith("#")


def _row_to_details_dict(entity_type: str, row: dict) -> dict:
    """Pulls this row's detail-table columns out of the raw CSV dict, doing
    the one bit of pre-processing Pydantic can't do on its own (splitting a
    semicolon-separated cell into a list for Person.aliases). Blank cells
    are omitted entirely rather than passed through as "", so every detail
    model's Optional/default-None fields behave exactly as they do for a
    partial PATCH from the regular entity form. Reads the Pydantic model's
    field list rather than entities.DETAIL_TABLES[entity_type] directly for
    the same reason build_template_csv does — some detail tables (location)
    carry extra server-derived columns a CSV should never be able to set."""
    cols = entities.DETAIL_MODELS[entity_type].model_fields.keys()
    list_fields = LIST_DETAIL_FIELDS.get(entity_type, ())
    details: dict = {}
    for col in cols:
        raw = (row.get(col) or "").strip()
        if not raw:
            continue
        if col in list_fields:
            details[col] = [v.strip() for v in raw.split(";") if v.strip()]
        else:
            details[col] = raw
    return details


def _process_row(entity_type: str, row: dict) -> tuple[str, Optional[str], dict]:
    """Validates one row and returns (name, description, parsed_details) —
    or raises ValueError with a message safe to show the analyst directly.
    Reuses entities._parsed_details for the detail fields specifically so
    an import-time validation failure reads exactly like the one you'd get
    editing that same field by hand (same Pydantic models, same messages)."""
    name = (row.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > 256:
        raise ValueError("name must be 256 characters or fewer")

    description = (row.get("description") or "").strip() or None

    raw_details = _row_to_details_dict(entity_type, row)
    try:
        parsed_details = entities._parsed_details(entity_type, raw_details)
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc

    return name, description, parsed_details


@router.get("/entities/import-template")
def entity_import_template(
    entity_type: str = Query(...),
    user: dict = Depends(auth.require_user),
):
    entities._validate_entity_type(entity_type)
    csv_text = build_template_csv(entity_type)
    filename = f"{entity_type}_import_template.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/entities/import")
async def import_entities(
    entity_type: str = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(auth.require_user),
):
    entities._validate_entity_type(entity_type)

    # Bounded read, same reasoning as attachment upload in reports.py: check
    # the size limit as bytes arrive rather than buffering an unbounded body
    # into memory first.
    chunks = []
    total = 0
    chunk_size = 1024 * 1024
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds MAX_UPLOAD_MB limit ({MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # utf-8-sig quietly strips a BOM, which Excel adds to "CSV UTF-8"
    # exports and would otherwise land as a stray character glued onto the
    # first header name (making "name" fail to match and every row error
    # out with "name is required" for a completely mystifying reason).
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File isn't valid UTF-8 text — export the CSV as UTF-8")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "name" not in reader.fieldnames:
        raise HTTPException(
            status_code=400,
            detail="CSV must have a header row including a 'name' column — download the template for this entity type and use it as-is",
        )

    rows = list(reader)
    if len(rows) > MAX_IMPORT_ROWS:
        raise HTTPException(
            status_code=400,
            detail=f"{len(rows)} rows is more than this import accepts at once (max {MAX_IMPORT_ROWS}) — split it into multiple files",
        )

    created = []
    errors = []
    conn = get_conn()
    try:
        for line_no, row in enumerate(rows, start=2):  # row 1 is the header, matching what a spreadsheet shows
            if _row_is_blank(row) or _looks_like_comment_row(row):
                continue

            name_cell = (row.get("name") or "").strip()
            try:
                name, description, parsed_details = _process_row(entity_type, row)
            except ValueError as exc:
                errors.append({"row": line_no, "name": name_cell, "error": str(exc)})
                continue

            try:
                cur = conn.cursor()
                entity_id = generate_id(entity_type, name)
                cur.execute(
                    "INSERT INTO entities (id, entity_type, name, description, created_by) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (entity_id, entity_type, name, description, user["id"]),
                )
                entities._insert_details(cur, entity_type, entity_id, parsed_details)
                if entity_type == "location":
                    entities._apply_location_derived_fields(
                        cur, entity_id, parsed_details.get("address"), parsed_details.get("lat"), parsed_details.get("lng")
                    )
                conn.commit()
                created.append({"row": line_no, "id": entity_id, "name": name})
            except Exception as exc:
                conn.rollback()
                errors.append({"row": line_no, "name": name_cell, "error": f"database error: {exc}"})
    finally:
        conn.close()

    audit.record("entity.bulk_import", user=user, object_type="entity",
                 detail={"entity_type": entity_type, "created": len(created),
                         "errors": len(errors),
                         "created_ids": [c["id"] for c in created][:200]})
    return {
        "entity_type": entity_type,
        "created_count": len(created),
        "error_count": len(errors),
        "created": created,
        "errors": errors,
    }
