"""Report authoring and attachment upload.

Reports are the analyst-written case narrative — markdown body, linked to
whichever entities it discusses, with document/image evidence attached
either to the report itself or directly to an entity (e.g. a mugshot on a
Person). Deletion policy is intentionally asymmetric between the two:

  * Reports have no DELETE endpoint of their own. A bad report is corrected
    by editing it or leaving it in 'draft' status, not removed — case
    write-ups are exactly the kind of thing you don't want silently
    disappearing. Permanent removal exists only as an admin action via
    destroy.py, which previews exactly what goes, requires typed
    confirmation, and writes the result to the audit log.
  * Attachments DO support delete — uploading the wrong file is a much more
    mechanical mistake than writing a wrong report — and deleting one also
    removes the underlying file from disk, not just the DB row, since a
    stray, unlisted file has no value once nothing references it.
"""

import os
import re
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import audit
import auth
import entities
import report_pdf
from db import db_cursor
from idgen import generate_id

router = APIRouter(prefix="/api", tags=["reports"])

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024
REPORT_STATUSES = ("draft", "confirmed")
CREDIBILITY_RATINGS = ("1", "2", "3", "4", "5", "6")

# Message precedence — a second axis alongside credibility, not a replacement
# for it. See the reports.criticality comment in db/init.sql for why the two
# are deliberately kept apart. Ordered most to least urgent, which is the
# order the UI offers them in and the order list sorting uses.
CRITICALITY_LEVELS = ("Flash", "Immediate", "Priority", "Routine")

# Extension allowlist for stored filenames — the original filename is kept
# only in the `filename` display column; the actual on-disk name is always
# generated (random uuid + this extension), so nothing about a stored path
# is attacker-controlled even though the display name is freeform text.
SAFE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".txt", ".rtf",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".bmp",
}


class ReportCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body_markdown: str = ""
    status: str = "draft"
    credibility_rating: Optional[str] = None
    criticality: Optional[str] = None
    entity_ids: list[str] = Field(default_factory=list)


class ReportUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    body_markdown: Optional[str] = None
    status: Optional[str] = None
    credibility_rating: Optional[str] = None
    criticality: Optional[str] = None
    entity_ids: Optional[list[str]] = None  # when provided, REPLACES the full linked-entity set


def _validate_report_fields(status: Optional[str], credibility_rating: Optional[str],
                            criticality: Optional[str] = None) -> None:
    if status is not None and status not in REPORT_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {REPORT_STATUSES}")
    if credibility_rating is not None and credibility_rating not in CREDIBILITY_RATINGS:
        raise HTTPException(status_code=400, detail=f"credibility_rating must be one of {CREDIBILITY_RATINGS}")
    if criticality is not None and criticality not in CRITICALITY_LEVELS:
        raise HTTPException(status_code=400, detail=f"criticality must be one of {CRITICALITY_LEVELS}")


def _entities_exist(cur, entity_ids: list[str]) -> list[str]:
    """Returns the subset of entity_ids that do NOT exist, so the caller can
    give one clear error listing every bad id instead of failing on the
    first one and forcing a fix-one-at-a-time loop."""
    if not entity_ids:
        return []
    cur.execute("SELECT id FROM entities WHERE id = ANY(%s)", (entity_ids,))
    found = {r[0] for r in cur.fetchall()}
    return [eid for eid in entity_ids if eid not in found]


# Entity mentions in a report body are stored as ordinary markdown links
# pointing at #/entities/<id> — e.g. [John Smith](#/entities/person-john-smith-a1b2c3).
#
# A plain markdown link rather than a custom syntax, for three reasons: it
# survives being copied into any other markdown tool, it renders as a link in
# anything that reads markdown (including this app's own renderer, which just
# needs to intercept the click), and a hash href is one DOMPurify already
# allows — a custom "entity:" scheme would be stripped as unsafe on the way to
# the browser.
MENTION_RE = re.compile(r"\]\(#/entities/([A-Za-z0-9_-]+)\)")


def extract_mentioned_entity_ids(body_markdown: str) -> list[str]:
    """Entity ids mentioned in a report body, in first-seen order."""
    seen = []
    for match in MENTION_RE.finditer(body_markdown or ""):
        entity_id = match.group(1)
        if entity_id not in seen:
            seen.append(entity_id)
    return seen


def _merge_mentioned_entities(cur, body_markdown: str, entity_ids: list[str]) -> list[str]:
    """Union of explicitly-linked entities and those mentioned in the prose.

    Done server-side rather than in the editor so that mentioning someone is
    ALWAYS what links them — writing "@John Smith" mid-interview shouldn't
    depend on the client remembering to also add him to a list, and any other
    client (a script, a future mobile view) gets the same behaviour for free.

    Union, not replacement: an entity linked deliberately but never named in
    the prose stays linked. Ids that don't resolve to a real entity are
    dropped rather than raising — a mention can outlive the entity it points
    at (someone archives and hard-deletes it in psql), and a report that can
    no longer be saved because of a stale link in its own text would be a
    genuinely bad failure mode.
    """
    mentioned = extract_mentioned_entity_ids(body_markdown)
    if not mentioned:
        return entity_ids
    combined = list(entity_ids)
    unknown = _entities_exist(cur, mentioned)
    for entity_id in mentioned:
        if entity_id not in combined and entity_id not in unknown:
            combined.append(entity_id)
    return combined


def _set_report_entities(cur, report_id: str, entity_ids: list[str]) -> None:
    cur.execute("DELETE FROM report_entities WHERE report_id = %s", (report_id,))
    for entity_id in set(entity_ids):
        cur.execute(
            "INSERT INTO report_entities (report_id, entity_id) VALUES (%s, %s)",
            (report_id, entity_id),
        )


def _report_dict(cur, report_id: str) -> dict:
    cur.execute(
        "SELECT id, title, body_markdown, status, credibility_rating, author_id, created_at, updated_at, "
        "criticality FROM reports WHERE id = %s",
        (report_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    report = {
        "id": row[0], "title": row[1], "body_markdown": row[2], "status": row[3],
        "credibility_rating": row[4], "author_id": row[5], "created_at": row[6], "updated_at": row[7],
        "criticality": row[8],
    }
    cur.execute(
        """
        SELECT e.id, e.name, e.entity_type
        FROM report_entities re JOIN entities e ON e.id = re.entity_id
        WHERE re.report_id = %s ORDER BY e.name
        """,
        (report_id,),
    )
    report["entities"] = [{"id": r[0], "name": r[1], "entity_type": r[2]} for r in cur.fetchall()]

    cur.execute(
        "SELECT id, filename, mime_type, file_size_bytes, uploaded_at, extraction_status "
        "FROM attachments WHERE report_id = %s ORDER BY uploaded_at",
        (report_id,),
    )
    report["attachments"] = [
        {
            "id": r[0], "filename": r[1], "mime_type": r[2], "file_size_bytes": r[3],
            "uploaded_at": r[4], "extraction_status": r[5],
        }
        for r in cur.fetchall()
    ]
    return report


# ----------------------------------------------------------------------------
# Report endpoints
# ----------------------------------------------------------------------------

@router.post("/reports", status_code=201)
def create_report(payload: ReportCreate, user: dict = Depends(auth.require_user)):
    _validate_report_fields(payload.status, payload.credibility_rating, payload.criticality)
    report_id = generate_id("report", payload.title)

    with db_cursor(commit=True) as cur:
        missing = _entities_exist(cur, payload.entity_ids)
        if missing:
            raise HTTPException(status_code=404, detail=f"entity_ids not found: {missing}")

        cur.execute(
            "INSERT INTO reports (id, title, body_markdown, status, credibility_rating, criticality, author_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (report_id, payload.title, payload.body_markdown, payload.status,
             payload.credibility_rating, payload.criticality, user["id"]),
        )
        linked = _merge_mentioned_entities(cur, payload.body_markdown, payload.entity_ids)
        _set_report_entities(cur, report_id, linked)
        result = _report_dict(cur, report_id)

    audit.record("report.create", user=user, object_type="report", object_id=report_id,
                 object_label=payload.title,
                 detail={"status": payload.status, "criticality": payload.criticality,
                         "linked_entities": len(result["entities"]),
                         "mentions": len(extract_mentioned_entity_ids(payload.body_markdown))})
    return result


@router.get("/reports")
def list_reports(
    status: Optional[str] = Query(default=None),
    entity_id: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None, description="Case-insensitive substring match on title"),
    criticality: Optional[str] = Query(default=None),
    sort: str = Query(default="recent", description="'recent' or 'criticality'"),
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(auth.require_user),
):
    if status is not None and status not in REPORT_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {REPORT_STATUSES}")
    if criticality is not None and criticality not in CRITICALITY_LEVELS:
        raise HTTPException(status_code=400, detail=f"criticality must be one of {CRITICALITY_LEVELS}")

    where = []
    params: list = []
    joins = ""
    if entity_id:
        joins = "JOIN report_entities re ON re.report_id = r.id"
        where.append("re.entity_id = %s")
        params.append(entity_id)
    if status:
        where.append("r.status = %s")
        params.append(status)
    if q:
        where.append("r.title ILIKE %s")
        params.append(f"%{q}%")
    if criticality:
        where.append("r.criticality = %s")
        params.append(criticality)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    # Sorting by precedence puts Flash at the top and — this is the part that
    # matters — puts UNSET last rather than first. An unrated report is not
    # the most urgent thing in the case file, which is what a plain text sort
    # on a nullable column would claim.
    #
    # The rank is selected as a column rather than only ordered by, because
    # the query is SELECT DISTINCT (it has to be, for the entity_id join) and
    # Postgres rejects ordering a DISTINCT result by an expression that isn't
    # in the select list. It is dropped again when the rows are built.
    rank_sql = ("CASE r.criticality WHEN 'Flash' THEN 0 WHEN 'Immediate' THEN 1 "
                "WHEN 'Priority' THEN 2 WHEN 'Routine' THEN 3 ELSE 4 END")
    order_sql = "crit_rank, r.created_at DESC" if sort == "criticality" else "r.created_at DESC"

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(DISTINCT r.id) FROM reports r {joins} {where_clause}", params)
        total = cur.fetchone()[0]
        cur.execute(
            f"""
            SELECT DISTINCT r.id, r.title, r.status, r.credibility_rating, r.author_id,
                            r.created_at, r.updated_at, r.criticality, {rank_sql} AS crit_rank
            FROM reports r {joins} {where_clause}
            ORDER BY {order_sql} LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        )
        rows = [
            {
                "id": r[0], "title": r[1], "status": r[2], "credibility_rating": r[3],
                "author_id": r[4], "created_at": r[5], "updated_at": r[6],
                "criticality": r[7],
            }
            for r in cur.fetchall()
        ]

    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/reports/{report_id}")
def get_report(report_id: str, user: dict = Depends(auth.require_user)):
    with db_cursor() as cur:
        return _report_dict(cur, report_id)


def _attach_storage_paths(cur, attachments: list) -> None:
    """Adds storage_path onto attachment dicts in place, for the PDF export
    (which needs to read the actual image files off disk). Deliberately not
    part of _report_dict/get_entity: storage_path is internal plumbing, and
    the regular API responses that reach the browser have no use for it."""
    ids = [a["id"] for a in attachments]
    if not ids:
        return
    cur.execute("SELECT id, storage_path FROM attachments WHERE id = ANY(%s)", (ids,))
    paths = dict(cur.fetchall())
    for a in attachments:
        a["storage_path"] = paths.get(a["id"])


@router.get("/reports/{report_id}/export.pdf")
def export_report_pdf(report_id: str, user: dict = Depends(auth.require_user)):
    """A formatted intel package for this report — see api/report_pdf.py.
    Any logged-in analyst can export; the export contains nothing they can't
    already read in the app."""
    with db_cursor() as cur:
        report = _report_dict(cur, report_id)
        _attach_storage_paths(cur, report["attachments"])

    # Full dossiers for each linked entity, via the same function the entity
    # detail page uses, so the package shows exactly what the app shows.
    dossiers = []
    for linked in report["entities"]:
        try:
            entity = entities.get_entity(linked["id"], user=user)
        except HTTPException:
            continue  # archived out from under us mid-export; skip rather than fail the whole package
        with db_cursor() as cur:
            _attach_storage_paths(cur, entity["attachments"])
        dossiers.append(entity)

    pdf_bytes = report_pdf.build_report_package(report, dossiers, user["username"])
    filename = report_pdf.export_filename(report)
    # An export packages the report AND every linked entity's dossier into one
    # file that leaves the system, so the entities included are worth naming.
    audit.record("report.export_pdf", user=user, object_type="report", object_id=report_id,
                 object_label=report["title"],
                 detail={"entities_included": [e["id"] for e in report["entities"]],
                         "size_bytes": len(pdf_bytes)})
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_header_safe_filename(filename)}"'},
    )


@router.patch("/reports/{report_id}")
def update_report(report_id: str, payload: ReportUpdate, user: dict = Depends(auth.require_user)):
    _validate_report_fields(payload.status, payload.credibility_rating, payload.criticality)
    core_updates = payload.model_dump(exclude_unset=True, exclude={"entity_ids"})

    with db_cursor(commit=True) as cur:
        cur.execute("SELECT 1 FROM reports WHERE id = %s", (report_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Report not found")

        if payload.entity_ids is not None:
            missing = _entities_exist(cur, payload.entity_ids)
            if missing:
                raise HTTPException(status_code=404, detail=f"entity_ids not found: {missing}")

        if core_updates:
            set_clause = ", ".join(f"{k} = %s" for k in core_updates)
            cur.execute(
                f"UPDATE reports SET {set_clause}, updated_at = now() WHERE id = %s",
                [*core_updates.values(), report_id],
            )
        elif payload.entity_ids is not None:
            # Same reasoning as entities.update_entity: a linked-entity-only
            # change is still a real edit and should bump updated_at.
            cur.execute("UPDATE reports SET updated_at = now() WHERE id = %s", (report_id,))

        # Mentions are re-read whenever the body OR the explicit links change.
        # Editing the prose to name someone new has to link them even when the
        # editor didn't touch the linked-entity list at all.
        if payload.entity_ids is not None or payload.body_markdown is not None:
            cur.execute("SELECT body_markdown FROM reports WHERE id = %s", (report_id,))
            current_body = cur.fetchone()[0]
            if payload.entity_ids is not None:
                base_ids = payload.entity_ids
            else:
                cur.execute("SELECT entity_id FROM report_entities WHERE report_id = %s", (report_id,))
                base_ids = [r[0] for r in cur.fetchall()]
            _set_report_entities(cur, report_id, _merge_mentioned_entities(cur, current_body, base_ids))

        updated = _report_dict(cur, report_id)

    # A report moving draft -> confirmed is a meaningful analytical act, not
    # just another field edit, so it gets its own action.
    action = "report.confirm" if core_updates.get("status") == "confirmed" else "report.update"
    fields = sorted(core_updates)
    if payload.entity_ids is not None:
        fields.append("entity_ids")
    audit.record(action, user=user, object_type="report", object_id=report_id,
                 object_label=updated["title"], detail={"fields": fields})
    return updated


# ----------------------------------------------------------------------------
# Attachment endpoints
# ----------------------------------------------------------------------------

def _safe_extension(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return ext if ext in SAFE_EXTENSIONS else ""


def _display_filename(filename: str) -> str:
    """Strip any directory components and control characters from a
    client-supplied filename before storing it for display — it is never
    used to build a filesystem path (see storage_path generation below),
    but it does get rendered back to other analysts in the UI."""
    base = os.path.basename(filename or "upload")
    base = re.sub(r"[\x00-\x1f]", "", base)
    return base[:255] or "upload"


@router.post("/attachments", status_code=201)
async def upload_attachment(
    file: UploadFile = File(...),
    report_id: Optional[str] = Form(default=None),
    entity_id: Optional[str] = Form(default=None),
    title: Optional[str] = Form(default=None),
    source_note: Optional[str] = Form(default=None),
    user: dict = Depends(auth.require_user),
):
    # Neither parent is allowed and is the Documents case: a file dropped in
    # before anyone knows what is in it. See the attachments comment in
    # db/init.sql — it is the same row, the same pipeline, no parent yet.
    with db_cursor() as cur:
        if report_id:
            cur.execute("SELECT 1 FROM reports WHERE id = %s", (report_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"report_id '{report_id}' not found")
        if entity_id:
            cur.execute("SELECT 1 FROM entities WHERE id = %s", (entity_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"entity_id '{entity_id}' not found")

    # Read in bounded chunks rather than `await file.read()` in one shot —
    # the latter buffers the entire body into memory before the size check
    # ever runs, so an oversized (or malicious) upload could exhaust memory
    # before MAX_UPLOAD_MB gets a chance to reject it.
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
                detail=f"File exceeds MAX_UPLOAD_MB limit ({MAX_UPLOAD_BYTES // (1024*1024)} MB)",
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    ext = _safe_extension(file.filename or "")
    now = datetime.utcnow()
    rel_dir = os.path.join(str(now.year), f"{now.month:02d}")
    abs_dir = os.path.join(UPLOAD_DIR, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    storage_path = os.path.join(rel_dir, stored_name)
    abs_path = os.path.join(UPLOAD_DIR, storage_path)

    with open(abs_path, "wb") as f:
        f.write(content)

    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO attachments
                    (report_id, entity_id, filename, title, source_note,
                     storage_path, mime_type, file_size_bytes, uploaded_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, uploaded_at
                """,
                (
                    report_id, entity_id, _display_filename(file.filename or "upload"),
                    (title or "").strip() or None, (source_note or "").strip() or None,
                    storage_path, file.content_type, len(content), user["id"],
                ),
            )
            attachment_id, uploaded_at = cur.fetchone()
    except Exception:
        # Don't leave an orphaned file on disk if the DB insert failed —
        # this is the one path where cleanup-on-failure matters, since
        # nothing will ever reference this path if the row never existed.
        try:
            os.remove(abs_path)
        except OSError:
            pass
        raise

    audit.record("attachment.upload", user=user, object_type="attachment", object_id=attachment_id,
                 object_label=_display_filename(file.filename or "upload"),
                 detail={"report_id": report_id, "entity_id": entity_id,
                         "mime_type": file.content_type, "size_bytes": len(content)})
    return {
        "id": attachment_id,
        "report_id": report_id,
        "entity_id": entity_id,
        "filename": _display_filename(file.filename or "upload"),
        "mime_type": file.content_type,
        "file_size_bytes": len(content),
        "uploaded_at": uploaded_at,
        "extraction_status": "pending",
    }


@router.get("/attachments/{attachment_id}")
def get_attachment(attachment_id: int, user: dict = Depends(auth.require_user)):
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, report_id, entity_id, filename, mime_type, file_size_bytes, "
            "uploaded_by, uploaded_at, extraction_status, extraction_error, extracted_text "
            "FROM attachments WHERE id = %s",
            (attachment_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Attachment not found")
    return {
        "id": row[0], "report_id": row[1], "entity_id": row[2], "filename": row[3],
        "mime_type": row[4], "file_size_bytes": row[5], "uploaded_by": row[6],
        "uploaded_at": row[7], "extraction_status": row[8], "extraction_error": row[9],
        "extracted_text": row[10],
    }


@router.get("/attachments/{attachment_id}/file")
def download_attachment(
    attachment_id: int,
    # The in-app preview (see the attachment lightbox in frontend/app.js)
    # needs the browser to RENDER the file rather than save it. An <img> would
    # display it either way, but a PDF in an <iframe> obeys
    # Content-Disposition and downloads instead of rendering when it says
    # "attachment" — hence this switch rather than a second near-identical
    # endpoint. Default stays "attachment" so every existing download link
    # behaves exactly as before.
    inline: bool = Query(default=False, description="Serve for in-browser display instead of download"),
    user: dict = Depends(auth.require_user),
):
    with db_cursor() as cur:
        cur.execute(
            "SELECT storage_path, filename, mime_type FROM attachments WHERE id = %s",
            (attachment_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Attachment not found")
    storage_path, filename, mime_type = row
    abs_path = os.path.join(UPLOAD_DIR, storage_path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File missing on disk")

    # Logged for both dispositions: an inline preview delivers exactly the
    # same bytes to the same person as a download does, so treating only the
    # download as "data leaving" would be a distinction without a difference.
    audit.record("attachment.download", user=user, object_type="attachment",
                 object_id=attachment_id, object_label=filename,
                 detail={"inline": bool(inline), "mime_type": mime_type})

    media_type = mime_type or "application/octet-stream"
    if not inline:
        return FileResponse(abs_path, media_type=media_type, filename=filename)

    # Passing filename= is what makes FileResponse set an "attachment"
    # disposition, so the inline variant sets the header itself. The filename
    # is still advertised, so "save as" from the preview keeps the real name.
    return FileResponse(
        abs_path,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{_header_safe_filename(filename)}"'},
    )


def _header_safe_filename(filename: str) -> str:
    """Strip quotes and control characters before putting a user-supplied name
    into a header value — display filenames are freeform text (see
    _display_filename), and an embedded quote would otherwise let the name
    break out of the quoted string in the Content-Disposition header."""
    return re.sub(r'[\r\n"\\]', "", filename or "file")


@router.delete("/attachments/{attachment_id}", status_code=204)
def delete_attachment(attachment_id: int, user: dict = Depends(auth.require_user)):
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT storage_path FROM attachments WHERE id = %s", (attachment_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Attachment not found")
        storage_path = row[0]
        cur.execute("SELECT filename FROM attachments WHERE id = %s", (attachment_id,))
        deleted_name = (cur.fetchone() or [None])[0]
        cur.execute("DELETE FROM attachments WHERE id = %s", (attachment_id,))

    abs_path = os.path.join(UPLOAD_DIR, storage_path)
    try:
        os.remove(abs_path)
    except OSError:
        pass  # already gone, or never made it to disk — the DB row is authoritative either way

    audit.record("attachment.delete", user=user, object_type="attachment",
                 object_id=attachment_id, object_label=deleted_name)
    return None
