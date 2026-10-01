"""The inbox: documents dropped in before anyone knows what is in them.

WHY THIS EXISTS

Every other way into this app asks you to know the answer first. Creating an
entity asks who. Writing a report asks what happened. Attaching a file asks
which record it belongs to. But the job usually starts the other way round —
a PDF, a photographed noticeboard, a forwarded message arrives, and the whole
question is *who is in this*. Until this view existed, the only way to get a
document into the extraction pipeline was to first invent a record to hang it
off, which meant creating the thing you were trying to find out about.

WHAT A DOCUMENT IS

An `attachments` row with no report_id and no entity_id. Not a new table, not
a parallel pipeline — the same row, claimed by the same worker loop, OCR'd by
the same code, read by the same model, proposing into the same review queue.
The only thing that makes it a "document" is that nobody has filed it yet.

That is why this module is short. It is a view and a few verbs over a table
that already did all of this; the feature was one CHECK constraint away the
whole time.

FILING IT LATER

`POST /{id}/file-under` sets the parent, at which point the row stops being a
document and starts being that record's attachment. Nothing is copied and
nothing is re-extracted — the extracted text, the proposals already made from
it, and the file on disk all follow it across, because they were never
anywhere else.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import audit
import auth
import entities as entities_module
import extraction as extraction_module
from db import db_cursor

router = APIRouter(prefix="/api/documents", tags=["documents"])

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")

# A pasted block is written to disk as a .txt so that backup, download and text
# extraction all treat it exactly like an uploaded file. The cap is generous —
# a long debrief transcript is a legitimate paste — but not unbounded, because
# this arrives in a JSON body rather than a streamed upload.
MAX_PASTE_CHARS = 200_000

LIST_COLUMNS = (
    "id", "filename", "title", "source_note", "is_pasted", "mime_type",
    "file_size_bytes", "uploaded_at", "uploaded_by", "extraction_status",
    "extraction_error", "archived_at",
)

STATUS_FILTERS = ("all", "pending", "processing", "done", "failed", "skipped")


class PasteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_PASTE_CHARS)
    title: Optional[str] = Field(default=None, max_length=300)
    source_note: Optional[str] = Field(default=None, max_length=1000)


class DocumentUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    source_note: Optional[str] = Field(default=None, max_length=1000)


class FileUnderRequest(BaseModel):
    report_id: Optional[str] = None
    entity_id: Optional[str] = None


class RecordFromDocumentRequest(BaseModel):
    entity_type: str
    name: str = Field(min_length=1, max_length=300)
    description: Optional[str] = None
    details: dict = Field(default_factory=dict)
    # The sentence the name was read out of, when it was made by selecting
    # text. Kept as the evidence on the provenance row -- "where did this
    # record come from" deserves better than "somebody typed it".
    quote: Optional[str] = Field(default=None, max_length=2000)


class ToRecordRequest(BaseModel):
    """Save a whole document as a Record entity."""
    name: str = Field(min_length=1, max_length=256)
    description: Optional[str] = None
    record_kind: Optional[str] = Field(default=None, max_length=120)
    record_date: Optional[str] = None
    issued_by: Optional[str] = Field(default=None, max_length=256)
    # Omitted means "the document's extracted text as it stands".
    body: Optional[str] = None
    link_entity_ids: list[str] = Field(default_factory=list, max_length=200)
    relationship_type: str = "mentioned_in"
    confidence: str = "confirmed"
    file_original: bool = True


def _rows(cur) -> list[dict]:
    return [dict(zip(LIST_COLUMNS, r)) for r in cur.fetchall()]


@router.get("")
def list_documents(
    status: str = "all",
    archived: bool = False,
    q: str = "",
    limit: int = 100,
    offset: int = 0,
    user: dict = Depends(auth.require_user),
):
    """Unfiled documents, newest first.

    The search covers the title, the filename, the source note AND the
    extracted text, because six weeks later the thing you remember about a
    document is a phrase that was in it, not what the scanner called the file.
    """
    if status not in STATUS_FILTERS:
        raise HTTPException(status_code=400,
                            detail=f"status must be one of {', '.join(STATUS_FILTERS)}")
    limit = max(1, min(limit, 500))

    where = ["report_id IS NULL", "entity_id IS NULL"]
    params: list = []
    where.append("archived_at IS NOT NULL" if archived else "archived_at IS NULL")
    if status != "all":
        where.append("extraction_status = %s")
        params.append(status)
    if q.strip():
        where.append("(COALESCE(title, '') ILIKE %s OR filename ILIKE %s "
                     "OR COALESCE(source_note, '') ILIKE %s "
                     "OR COALESCE(extracted_text, '') ILIKE %s)")
        params.extend([f"%{q.strip()}%"] * 4)
    where_clause = " AND ".join(where)

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM attachments WHERE {where_clause}", params or None)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT {', '.join(LIST_COLUMNS)} FROM attachments WHERE {where_clause} "
            "ORDER BY uploaded_at DESC, id DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        items = _rows(cur)
        _attach_counts(cur, items)

    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _attach_counts(cur, items: list[dict]) -> None:
    """How many proposals each document produced, and how many are still
    waiting. Without the pending count the list cannot answer the only
    question a person actually has about a processed document — "is there
    anything here left for me to do?"."""
    if not items:
        return
    ids = [i["id"] for i in items]
    cur.execute(
        "SELECT attachment_id, COUNT(*), COUNT(*) FILTER (WHERE status = 'pending') "
        "FROM extraction_suggestions WHERE attachment_id = ANY(%s) GROUP BY attachment_id",
        (ids,),
    )
    counts = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    for item in items:
        total, pending = counts.get(item["id"], (0, 0))
        item["suggestion_count"] = total
        item["pending_suggestion_count"] = pending


@router.get("/{document_id}")
def get_document(document_id: int, user: dict = Depends(auth.require_user)):
    """One document, its extracted text, and every proposal made from it.

    Both halves in one response on purpose: the point of this view is reading a
    proposal next to the sentence it came from, and that cannot be done if
    checking the text means a second request to a different screen.
    """
    with db_cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(LIST_COLUMNS)}, extracted_text, report_id, entity_id "
            "FROM attachments WHERE id = %s",
            (document_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found")
        doc = dict(zip(LIST_COLUMNS, row[: len(LIST_COLUMNS)]))
        doc["extracted_text"] = row[len(LIST_COLUMNS)]
        doc["report_id"], doc["entity_id"] = row[len(LIST_COLUMNS) + 1:]
        doc["filed"] = bool(doc["report_id"] or doc["entity_id"])

        _attach_counts(cur, [doc])
        cur.execute(
            "SELECT id, suggestion_type, suggested_entity_type, suggested_name, "
            "       suggested_relationship_type, suggested_from_name, suggested_to_name, "
            "       details, confidence, status, source, resolved_entity_id "
            "FROM extraction_suggestions WHERE attachment_id = %s "
            "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, suggestion_type, id",
            (document_id,),
        )
        # source and resolved_entity_id come back so the page can distinguish
        # what a model proposed from what a person recorded by hand, and link
        # straight to the record either produced.
        cols = ("id", "suggestion_type", "suggested_entity_type", "suggested_name",
                "suggested_relationship_type", "suggested_from_name", "suggested_to_name",
                "details", "confidence", "status", "source", "resolved_entity_id")
        doc["suggestions"] = [dict(zip(cols, r)) for r in cur.fetchall()]
        # Same annotation the Review queue gets: which existing records each
        # name might be, and what type to offer for the ones that do not exist.
        # Without it a relationship from a document is only reviewable
        # somewhere else, which is a strange thing for the view that shows the
        # document it came from.
        extraction_module.annotate_relationship_candidates(cur, doc["suggestions"])

        doc["linked_entities"] = _entities_from_document(cur, document_id)
        cur.execute(
            "SELECT e.id, e.name FROM record_details r JOIN entities e ON e.id = r.entity_id "
            "WHERE r.source_attachment_id = %s ORDER BY e.created_at",
            (document_id,),
        )
        doc["records"] = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]

        if doc["uploaded_by"]:
            cur.execute("SELECT username FROM users WHERE id = %s", (doc["uploaded_by"],))
            found = cur.fetchone()
            doc["uploaded_by_name"] = found[0] if found else None

    return doc


@router.post("/text", status_code=201)
def paste_document(payload: PasteRequest, user: dict = Depends(auth.require_user)):
    """Text typed or pasted straight in, with no file to upload.

    An intercepted transmission read out over a radio, the body of a forwarded
    message, a note someone dictated. Written to disk as a .txt and then left
    entirely alone: the worker picks it up as a pending attachment and runs the
    same extraction path, so there is no second code path to keep correct.
    """
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Nothing to save — the text is empty.")

    now = datetime.now(timezone.utc)
    rel_dir = os.path.join(str(now.year), f"{now.month:02d}")
    os.makedirs(os.path.join(UPLOAD_DIR, rel_dir), exist_ok=True)
    storage_path = os.path.join(rel_dir, f"{uuid.uuid4().hex}.txt")
    abs_path = os.path.join(UPLOAD_DIR, storage_path)
    content = text.encode("utf-8")
    with open(abs_path, "wb") as f:
        f.write(content)

    title = (payload.title or "").strip() or None
    filename = _paste_filename(title, now)
    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO attachments
                    (filename, title, source_note, is_pasted, storage_path,
                     mime_type, file_size_bytes, uploaded_by)
                VALUES (%s, %s, %s, TRUE, %s, 'text/plain', %s, %s)
                RETURNING id, uploaded_at
                """,
                (filename, title, (payload.source_note or "").strip() or None,
                 storage_path, len(content), user["id"]),
            )
            document_id, uploaded_at = cur.fetchone()
    except Exception:
        try:
            os.remove(abs_path)
        except OSError:
            pass
        raise

    audit.record("document.paste", user=user, object_type="attachment",
                 object_id=document_id, object_label=title or filename,
                 detail={"chars": len(text)})
    return {"id": document_id, "filename": filename, "title": title,
            "is_pasted": True, "uploaded_at": uploaded_at,
            "extraction_status": "pending", "file_size_bytes": len(content)}


def _paste_filename(title: Optional[str], now: datetime) -> str:
    """A filename for something that never was a file. Derived from the title
    so a download is recognisable, and timestamped when there is no title, so
    two untitled pastes do not both arrive as "pasted-text.txt"."""
    if title:
        safe = "".join(c if c.isalnum() or c in " -_" else "-" for c in title).strip()
        safe = "-".join(safe.split())[:80]
        if safe:
            return f"{safe}.txt"
    return f"pasted-{now.strftime('%Y%m%d-%H%M%S')}.txt"


@router.patch("/{document_id}")
def update_document(document_id: int, payload: DocumentUpdate,
                    user: dict = Depends(auth.require_user)):
    """Name it, or record where it came from. Both can be set after the fact —
    you often cannot title a document usefully until you have read it."""
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return get_document(document_id, user=user)
    cleaned = {k: ((v or "").strip() or None) for k, v in updates.items()}
    with db_cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE attachments SET {', '.join(f'{k} = %s' for k in cleaned)} WHERE id = %s",
            [*cleaned.values(), document_id],
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Document not found")
    audit.record("document.update", user=user, object_type="attachment",
                 object_id=document_id, object_label=cleaned.get("title") or str(document_id),
                 detail=cleaned)
    return get_document(document_id, user=user)


@router.post("/{document_id}/file-under")
def file_under(document_id: int, payload: FileUnderRequest,
               user: dict = Depends(auth.require_user)):
    """Attach a document to a report or an entity, after the fact.

    This is a pointer change, not a copy. The file, the extracted text and
    every proposal already made from it stay exactly where they are — they were
    always on this row — and the document simply stops appearing in the inbox
    and starts appearing in that record's attachments.
    """
    if not payload.report_id and not payload.entity_id:
        raise HTTPException(status_code=400,
                            detail="Give a report or an entity to file this under.")
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT filename, title, report_id, entity_id FROM attachments WHERE id = %s",
                    (document_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found")
        filename, title, existing_report, existing_entity = row
        if existing_report or existing_entity:
            raise HTTPException(
                status_code=409,
                detail="That document is already filed. Detach it from the entity or report first, or upload a fresh copy.",
            )
        if payload.report_id:
            cur.execute("SELECT 1 FROM reports WHERE id = %s", (payload.report_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404,
                                    detail=f"report_id '{payload.report_id}' not found")
        if payload.entity_id:
            cur.execute("SELECT 1 FROM entities WHERE id = %s", (payload.entity_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404,
                                    detail=f"entity_id '{payload.entity_id}' not found")
        cur.execute(
            "UPDATE attachments SET report_id = %s, entity_id = %s, archived_at = NULL "
            "WHERE id = %s",
            (payload.report_id, payload.entity_id, document_id),
        )

    audit.record("document.file_under", user=user, object_type="attachment",
                 object_id=document_id, object_label=title or filename,
                 detail={"report_id": payload.report_id, "entity_id": payload.entity_id})
    return {"id": document_id, "report_id": payload.report_id,
            "entity_id": payload.entity_id, "filed": True}


@router.post("/{document_id}/record", status_code=201)
def record_from_document(document_id: int, payload: RecordFromDocumentRequest,
                         user: dict = Depends(auth.require_user)):
    """Create a record from something in a document that nothing proposed.

    The model misses things. A reporter's byline, a second company named once
    in passing, a registration plate written into the middle of a sentence —
    all real, all invisible in the review queue because nothing proposed them.
    You notice while READING the document, so this is where recording it
    belongs; making somebody leave the page, create the record, and come back
    is how those details get skipped.

    The record is a normal entity in every respect. What makes this more than
    a shortcut to the New Entity form is the provenance row: an
    extraction_suggestions row with source 'manual', already accepted and
    pointing at both the document and the new record. That keeps the one
    question this app always has to be able to answer — where did this come
    from — answerable for records a person made as well as records a model
    proposed, and it makes them show up in the document's own "already
    decided" list beside everything else that came out of it.
    """
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT filename, title FROM attachments WHERE id = %s", (document_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found")
        doc_label = row[1] or row[0]

        # Strictly validated, unlike accepting a model's proposal. A model's
        # detail fields are best-effort and a bad one is dropped so it cannot
        # block an otherwise-good suggestion; a person typing into a form
        # should be told their value was wrong, not have it silently
        # discarded -- and this is the same form as "+ New Entity", which
        # already behaves that way.
        entities_module._validate_entity_type(payload.entity_type)
        entities_module._parsed_details(payload.entity_type, payload.details)
        entity_id = extraction_module._create_entity(
            cur, user, payload.entity_type, payload.name,
            description=payload.description, raw_details=payload.details)

        quote = (payload.quote or "").strip()
        cur.execute(
            "INSERT INTO extraction_suggestions "
            "  (attachment_id, source, suggestion_type, suggested_entity_type, "
            "   suggested_name, evidence, confidence, status, resolved_entity_id, "
            "   reviewed_by, reviewed_at) "
            "VALUES (%s, 'manual', 'entity', %s, %s, %s, 1.0, 'accepted', %s, %s, now())",
            (document_id, payload.entity_type, payload.name,
             json.dumps({"recorded_by_hand": True,
                         "quote": quote or None,
                         "document": doc_label}),
             entity_id, user["id"]))

    audit.record("entity.create", user=user, object_type="entity", object_id=entity_id,
                 object_label=payload.name,
                 detail={"entity_type": payload.entity_type, "from_document": document_id,
                         "recorded_by_hand": True})
    return {"id": entity_id, "entity_type": payload.entity_type, "name": payload.name,
            "document_id": document_id}


def _entities_from_document(cur, document_id: int) -> list[dict]:
    """Active entities that came out of this document: accepted suggestions
    (the model's and hand-recorded ones) and both ends of accepted
    relationships. These are the ones a Record made from it starts linked to."""
    cur.execute(
        """
        SELECT DISTINCT e.id, e.entity_type, e.name
        FROM extraction_suggestions s
        JOIN entities e ON e.id IN (s.resolved_entity_id, s.suggested_from_entity_id,
                                    s.suggested_to_entity_id)
        WHERE s.attachment_id = %s AND s.status = 'accepted'
          AND e.is_active AND e.entity_type <> 'record'
        ORDER BY e.entity_type, e.name
        """,
        (document_id,),
    )
    return [{"id": r[0], "entity_type": r[1], "name": r[2]} for r in cur.fetchall()]


@router.post("/{document_id}/to-record", status_code=201)
def document_to_record(document_id: int, payload: ToRecordRequest,
                       user: dict = Depends(auth.require_user)):
    """Save the whole document as a Record entity, linked to the entities it
    mentions, with the original file filed under it."""
    if not entities_module.RELATIONSHIP_TYPE_RE.match(payload.relationship_type):
        raise HTTPException(status_code=400,
                            detail="relationship_type must be lowercase snake_case")
    if payload.confidence not in entities_module.CONFIDENCE_LEVELS:
        raise HTTPException(status_code=400, detail="Unknown confidence level")

    link_ids = list(dict.fromkeys(i for i in payload.link_entity_ids if i))
    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT filename, title, extracted_text, report_id, entity_id "
            "FROM attachments WHERE id = %s FOR UPDATE",
            (document_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found")
        filename, title, extracted, on_report, on_entity = row

        cur.execute("SELECT e.id, e.name FROM record_details r JOIN entities e ON e.id = r.entity_id "
                    "WHERE r.source_attachment_id = %s LIMIT 1", (document_id,))
        existing = cur.fetchone()
        if existing:
            raise HTTPException(status_code=409, detail={
                "message": f"This document is already saved as the Record \"{existing[1]}\".",
                "record_id": existing[0]})

        body = payload.body if payload.body is not None else (extracted or "")
        if not body.strip():
            raise HTTPException(
                status_code=400,
                detail="This document has no text yet. Wait for it to finish reading, or type the text in.")

        if link_ids:
            cur.execute("SELECT id FROM entities WHERE id = ANY(%s)", (link_ids,))
            found = {r[0] for r in cur.fetchall()}
            missing = [i for i in link_ids if i not in found]
            if missing:
                raise HTTPException(status_code=404,
                                    detail=f"Entity not found: {', '.join(missing)}")

        details = entities_module._parsed_details("record", {
            "record_kind": (payload.record_kind or "").strip() or None,
            "record_date": payload.record_date or None,
            "issued_by": (payload.issued_by or "").strip() or None,
            "body": body,
        })
        details["source_attachment_id"] = document_id
        name = payload.name.strip()
        entity_id = extraction_module._create_entity(
            cur, user, "record", name,
            description=(payload.description or "").strip() or None)
        entities_module._update_details(cur, "record", entity_id, details)

        for other in link_ids:
            cur.execute(
                "INSERT INTO relationships (from_entity_id, to_entity_id, relationship_type, "
                "  confidence, created_by) VALUES (%s, %s, %s, %s, %s)",
                (other, entity_id, payload.relationship_type, payload.confidence, user["id"]),
            )

        filed = False
        if payload.file_original and not on_report and not on_entity:
            cur.execute("UPDATE attachments SET entity_id = %s, archived_at = NULL WHERE id = %s",
                        (entity_id, document_id))
            filed = True

        # Provenance, the same way a hand-recorded entity gets it.
        cur.execute(
            "INSERT INTO extraction_suggestions "
            "  (attachment_id, source, suggestion_type, suggested_entity_type, "
            "   suggested_name, evidence, confidence, status, resolved_entity_id, "
            "   reviewed_by, reviewed_at) "
            "VALUES (%s, 'manual', 'entity', 'record', %s, %s, 1.0, 'accepted', %s, %s, now())",
            (document_id, name,
             json.dumps({"recorded_by_hand": True, "whole_document": True,
                         "document": title or filename}),
             entity_id, user["id"]))

    audit.record("entity.create", user=user, object_type="entity", object_id=entity_id,
                 object_label=name,
                 detail={"entity_type": "record", "from_document": document_id,
                         "links": len(link_ids), "filed_original": filed})
    return {"id": entity_id, "entity_type": "record", "name": name,
            "document_id": document_id, "links_created": len(link_ids),
            "filed_original": filed}


@router.post("/{document_id}/archive")
def archive_document(document_id: int, user: dict = Depends(auth.require_user)):
    """Out of the inbox, not off the disk.

    A document whose proposals are all reviewed has served its purpose but is
    still the evidence behind whatever was accepted from it, so archiving hides
    it and keeps it. DELETE /api/attachments/{id} is still there for the
    genuinely-wrong-file case.
    """
    return _set_archived(document_id, user, datetime.now(timezone.utc), "document.archive")


@router.post("/{document_id}/restore")
def restore_document(document_id: int, user: dict = Depends(auth.require_user)):
    return _set_archived(document_id, user, None, "document.restore")


def _set_archived(document_id: int, user: dict, value, action: str) -> dict:
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT filename, title FROM attachments WHERE id = %s", (document_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found")
        cur.execute("UPDATE attachments SET archived_at = %s WHERE id = %s", (value, document_id))
    audit.record(action, user=user, object_type="attachment", object_id=document_id,
                 object_label=row[1] or row[0], detail=None)
    return {"id": document_id, "archived_at": value}


@router.post("/{document_id}/re-extract")
def re_extract(document_id: int, user: dict = Depends(auth.require_user)):
    """Put it back in the worker's queue.

    Available at any status, not only after a failure. The case that needs it
    most does not look like a failure at all: if Ollama is off or its model is
    missing when a document is read, the text still extracts, the document is
    marked "Read", and it simply produces no proposals — indistinguishable from
    a document with nothing in it. Restricting this to failed documents left
    exactly the ones worth re-running unreachable.

    Pending proposals from the previous pass are cleared first, so a re-read
    replaces rather than duplicates. Anything already accepted or dismissed is
    kept: those are decisions a person made, and re-running is not a reason to
    ask them again.
    """
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT filename, title, extraction_status FROM attachments WHERE id = %s",
                    (document_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found")
        cur.execute("DELETE FROM extraction_suggestions "
                    "WHERE attachment_id = %s AND status = 'pending'", (document_id,))
        cleared = cur.rowcount
        cur.execute(
            "UPDATE attachments SET extraction_status = 'pending', extraction_error = NULL "
            "WHERE id = %s",
            (document_id,),
        )
    audit.record("document.re_extract", user=user, object_type="attachment",
                 object_id=document_id, object_label=row[1] or row[0],
                 detail={"was": row[2], "cleared_pending": cleared})
    return {"id": document_id, "extraction_status": "pending",
            "cleared_pending_suggestions": cleared}


class ReReadRequest(BaseModel):
    # Default deliberately narrow: "read, but produced nothing" is the
    # Ollama-was-down signature, and re-reading documents that DID produce
    # proposals would throw away pending ones somebody is halfway through.
    only_empty: bool = True
    # Failed documents are a different problem — an unreadable file, a corrupt
    # PDF — and they will fail again until the file itself is dealt with.
    # Including them by default would mean this operation never settles: it
    # would report work queued on every run and the summary would always
    # overstate what was achieved.
    include_failed: bool = False
    include_archived: bool = False


@router.post("/re-read-all")
def re_read_all(payload: ReReadRequest, user: dict = Depends(auth.require_user)):
    """Put a batch back in the queue after an Ollama outage.

    A model that was unreachable for an afternoon leaves every document
    uploaded in it marked "Read" with nothing proposed. Clicking through thirty
    of those one at a time is the kind of chore that means it does not get
    done, so this does the batch — by default only the ones that produced
    nothing, which is precisely the shape that outage leaves behind.
    """
    statuses = ["done", "skipped"] + (["failed"] if payload.include_failed else [])
    where = ["report_id IS NULL", "entity_id IS NULL",
             f"extraction_status IN ({', '.join(repr(s) for s in statuses)})"]
    if not payload.include_archived:
        where.append("archived_at IS NULL")
    if payload.only_empty:
        where.append("NOT EXISTS (SELECT 1 FROM extraction_suggestions s "
                     "WHERE s.attachment_id = attachments.id)")

    with db_cursor(commit=True) as cur:
        cur.execute(f"SELECT id FROM attachments WHERE {' AND '.join(where)}")
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM extraction_suggestions "
                        "WHERE attachment_id = ANY(%s) AND status = 'pending'", (ids,))
            cur.execute(
                "UPDATE attachments SET extraction_status = 'pending', extraction_error = NULL "
                "WHERE id = ANY(%s)", (ids,))

    audit.record("document.re_extract_all", user=user, object_type="attachment",
                 object_id=None, object_label=f"{len(ids)} document(s)",
                 detail={"count": len(ids), "only_empty": payload.only_empty,
                         "include_failed": payload.include_failed})
    return {"queued": len(ids),
            "message": (f"{len(ids)} document(s) queued to be read again."
                        if ids else
                        "Nothing to re-read — every document has produced proposals.")}
