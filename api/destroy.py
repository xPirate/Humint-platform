"""Permanent deletion, for admins, of records that should never have existed.

WHY THIS EXISTS, HAVING SAID IT NEVER WOULD

Everything else in this app archives rather than deletes, for a good reason:
reports, audit entries and old exports reference records, and a dead link is
worse than a redirect. That reasoning holds for a record that was *real* and
is now finished with.

It does not hold for noise. Extraction proposes an entity called
"ATTACHMENT A", somebody accepts it at four in the morning, and now it is in
the file forever — cluttering the tree, drawn in the network, proposed as a
correlation match against other junk, and impossible to remove. Archiving
hides it from one list and leaves it in the database. Enough of those and the
case file is mostly debris.

So: permanent deletion, deliberately narrow.

  * Admins only.
  * Never for a record a CONFIRMED report cites. A confirmed report is a
    statement somebody stands behind, and quietly removing something it names
    breaks that. Archive it, or unlink it from the report first.
  * Always previewed. The dialog says what is about to be destroyed, counted
    and named, before anything happens.
  * Always audited, with a snapshot. The record goes; the fact that it
    existed and was deleted, by whom, and what it was called, does not.

WHAT ACTUALLY HAS TO BE CLEANED UP

This is the part that makes it more than one DELETE. Most references cascade,
and two do not:

  * `extraction_suggestions.resolved_entity_id` is a plain foreign key with
    no ON DELETE clause, so a naive delete does not orphan anything — it
    fails outright with a foreign-key violation.
  * `correlation_suggestions.subject_a_id` and `subject_b_id` are TEXT with
    no foreign key at all, because a subject can be an entity or a report.
    Nothing cascades, nothing complains, and the rows sit there forever
    pointing at something that no longer exists. These are the orphans.

Both are handled here explicitly. The audit log is deliberately NOT cleaned
up: it stores ids as text precisely so that history survives the thing it
describes.
"""

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import audit
import auth
from db import db_cursor

router = APIRouter(prefix="/api", tags=["destroy"])

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")

# One confirmation, one transaction, and a person has to be able to read the
# summary afterwards. Beyond this it is a bulk operation wearing a delete's
# clothes, and bulk deletion of a case file is what restore-from-backup is
# for.
MAX_DELETE_TARGETS = 50


class DeleteRequest(BaseModel):
    entity_ids: list[str] = Field(default_factory=list, max_length=MAX_DELETE_TARGETS)
    report_ids: list[str] = Field(default_factory=list, max_length=MAX_DELETE_TARGETS)
    document_ids: list[int] = Field(default_factory=list, max_length=MAX_DELETE_TARGETS)


# ---------------------------------------------------------------------------
# What it would cost
# ---------------------------------------------------------------------------

def _entity_cost(cur, entity_id: str) -> dict:
    cur.execute("SELECT id, entity_type, name, is_active FROM entities WHERE id = %s",
                (entity_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Entity '{entity_id}' not found")
    out = {"id": row[0], "entity_type": row[1], "name": row[2], "is_active": row[3]}

    counts = {}
    for label, sql, params in (
        ("relationships",
         "SELECT count(*) FROM relationships WHERE from_entity_id = %s OR to_entity_id = %s",
         (entity_id, entity_id)),
        ("contacts", "SELECT count(*) FROM contact_points WHERE entity_id = %s", (entity_id,)),
        ("attachments", "SELECT count(*) FROM attachments WHERE entity_id = %s", (entity_id,)),
        ("suggestions",
         "SELECT count(*) FROM extraction_suggestions WHERE suggested_from_entity_id = %s "
         "OR suggested_to_entity_id = %s OR resolved_entity_id = %s",
         (entity_id, entity_id, entity_id)),
        ("correlation_rows",
         "SELECT count(*) FROM correlation_suggestions WHERE subject_a_id = %s "
         "OR subject_b_id = %s", (entity_id, entity_id)),
        ("merged_into_this",
         "SELECT count(*) FROM entities WHERE merged_into = %s", (entity_id,)),
    ):
        cur.execute(sql, params)
        counts[label] = cur.fetchone()[0]
    out["removes"] = counts

    # Reports are named rather than counted: "cited by 3 reports" is not a
    # decision anybody can make, and the status of each is what decides
    # whether this is allowed at all.
    cur.execute(
        "SELECT r.id, r.title, r.status FROM report_entities re "
        "JOIN reports r ON r.id = re.report_id WHERE re.entity_id = %s ORDER BY r.title",
        (entity_id,))
    out["cited_by"] = [{"id": r[0], "title": r[1], "status": r[2]} for r in cur.fetchall()]
    out["blocked_by"] = [r for r in out["cited_by"] if r["status"] == "confirmed"]
    return out


def _report_cost(cur, report_id: str) -> dict:
    cur.execute("SELECT id, title, status FROM reports WHERE id = %s", (report_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' not found")
    out = {"id": row[0], "title": row[1], "status": row[2]}
    counts = {}
    for label, sql in (
        ("entity_links", "SELECT count(*) FROM report_entities WHERE report_id = %s"),
        ("attachments", "SELECT count(*) FROM attachments WHERE report_id = %s"),
        ("correlation_rows",
         "SELECT count(*) FROM correlation_suggestions WHERE subject_a_id = %s "
         "OR subject_b_id = %s"),
    ):
        cur.execute(sql, (report_id, report_id) if "subject_a_id" in sql else (report_id,))
        counts[label] = cur.fetchone()[0]
    out["removes"] = counts
    # A confirmed report is the one thing this endpoint will not destroy on
    # the strength of a single click -- but unlike an entity, there is nothing
    # else it could break, so it is a warning rather than a refusal.
    out["warn_confirmed"] = row[2] == "confirmed"
    return out


def _document_cost(cur, attachment_id: int) -> dict:
    cur.execute(
        "SELECT id, COALESCE(title, filename), report_id, entity_id, storage_path "
        "FROM attachments WHERE id = %s", (attachment_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Document {attachment_id} not found")
    out = {"id": row[0], "title": row[1], "report_id": row[2], "entity_id": row[3],
           "storage_path": row[4]}
    cur.execute("SELECT count(*), count(*) FILTER (WHERE status = 'pending') "
                "FROM extraction_suggestions WHERE attachment_id = %s", (attachment_id,))
    total, pending = cur.fetchone()
    out["removes"] = {"suggestions": total, "pending_suggestions": pending}
    # Records already accepted out of this document are NOT touched: they are
    # part of the case file now, and where they came from is not a reason to
    # destroy them.
    cur.execute("SELECT count(*) FROM extraction_suggestions "
                "WHERE attachment_id = %s AND status = 'accepted' "
                "AND resolved_entity_id IS NOT NULL", (attachment_id,))
    out["accepted_records_kept"] = cur.fetchone()[0]
    return out


@router.post("/admin/delete-preview")
def delete_preview(payload: DeleteRequest, user: dict = Depends(auth.require_admin)):
    """What a delete would destroy, before anyone commits to it."""
    with db_cursor() as cur:
        entities = [_entity_cost(cur, i) for i in dict.fromkeys(payload.entity_ids)]
        reports = [_report_cost(cur, i) for i in dict.fromkeys(payload.report_ids)]
        documents = [_document_cost(cur, i) for i in dict.fromkeys(payload.document_ids)]
    blocked = [{"id": e["id"], "name": e["name"], "reports": e["blocked_by"]}
               for e in entities if e["blocked_by"]]
    return {"entities": entities, "reports": reports, "documents": documents,
            "blocked": blocked, "allowed": not blocked}


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

def _delete_entity(cur, entity_id: str) -> None:
    """The two references that do not look after themselves, then the row.

    Everything else -- detail tables, relationships, report links, contacts,
    attachments, the suggestion endpoints -- is ON DELETE CASCADE and goes
    with the parent."""
    # No FK at all: these are the rows that orphan today.
    cur.execute("DELETE FROM correlation_suggestions WHERE subject_a_id = %s "
                "OR subject_b_id = %s", (entity_id, entity_id))
    # A plain FK with no ON DELETE clause, so leaving this would not orphan
    # anything -- it would refuse the delete. The suggestion itself is history
    # of a decision about a record that is going, so it goes too.
    cur.execute("DELETE FROM extraction_suggestions WHERE resolved_entity_id = %s",
                (entity_id,))
    # merged_into is ON DELETE SET NULL, which would quietly turn "merged
    # away into X" into a plain archived record with no explanation. Say so in
    # the description instead, so the trail still reads.
    cur.execute(
        "UPDATE entities SET description = COALESCE(description || E'\\n\\n', '') || "
        "'(The record this was merged into has since been deleted.)', updated_at = now() "
        "WHERE merged_into = %s", (entity_id,))
    cur.execute("DELETE FROM entities WHERE id = %s", (entity_id,))


def _delete_report(cur, report_id: str) -> None:
    cur.execute("DELETE FROM correlation_suggestions WHERE subject_a_id = %s "
                "OR subject_b_id = %s", (report_id, report_id))
    cur.execute("DELETE FROM reports WHERE id = %s", (report_id,))


def _delete_document(cur, attachment_id: int) -> None:
    # Accepted records keep their provenance note but lose the pointer, since
    # the row it points at is going. The record itself stays: it is part of
    # the case file now.
    cur.execute("DELETE FROM attachments WHERE id = %s", (attachment_id,))


@router.post("/admin/delete")
def delete_records(payload: DeleteRequest, user: dict = Depends(auth.require_admin)):
    if not (payload.entity_ids or payload.report_ids or payload.document_ids):
        raise HTTPException(status_code=400, detail="Nothing was selected to delete.")

    removed_files = []
    with db_cursor(commit=True) as cur:
        entities = [_entity_cost(cur, i) for i in dict.fromkeys(payload.entity_ids)]
        reports = [_report_cost(cur, i) for i in dict.fromkeys(payload.report_ids)]
        documents = [_document_cost(cur, i) for i in dict.fromkeys(payload.document_ids)]

        blocked = [e for e in entities if e["blocked_by"]]
        if blocked:
            names = ", ".join(f"'{e['name']}'" for e in blocked[:3])
            titles = ", ".join(f"'{r['title']}'" for r in blocked[0]["blocked_by"][:2])
            raise HTTPException(
                status_code=409,
                detail=(f'{names} is cited by a confirmed report ({titles}). Archive it instead, or unlink it from the report first.'))

        # Files are collected before the rows go and removed after the
        # transaction commits -- an unlinked file on disk is recoverable, a
        # committed delete of a file that should have stayed is not.
        ids = [e["id"] for e in entities]
        if ids:
            cur.execute("SELECT storage_path FROM attachments WHERE entity_id = ANY(%s)", (ids,))
            removed_files += [r[0] for r in cur.fetchall() if r[0]]
        rids = [r["id"] for r in reports]
        if rids:
            cur.execute("SELECT storage_path FROM attachments WHERE report_id = ANY(%s)", (rids,))
            removed_files += [r[0] for r in cur.fetchall() if r[0]]
        removed_files += [d["storage_path"] for d in documents if d["storage_path"]]

        for e in entities:
            _delete_entity(cur, e["id"])
        for r in reports:
            _delete_report(cur, r["id"])
        for d in documents:
            _delete_document(cur, d["id"])

    for rel_path in removed_files:
        try:
            os.remove(os.path.join(UPLOAD_DIR, rel_path))
        except OSError:
            pass  # already gone, or never made it to disk

    # One audit entry per thing destroyed, each carrying enough of the record
    # to say what was lost. This is the only trace left.
    for e in entities:
        audit.record("entity.delete", user=user, object_type="entity", object_id=e["id"],
                     object_label=e["name"],
                     detail={"entity_type": e["entity_type"], "removed": e["removes"],
                             "was_cited_by": [r["title"] for r in e["cited_by"]]})
    for r in reports:
        audit.record("report.delete", user=user, object_type="report", object_id=r["id"],
                     object_label=r["title"],
                     detail={"status": r["status"], "removed": r["removes"]})
    for d in documents:
        audit.record("document.delete", user=user, object_type="attachment", object_id=d["id"],
                     object_label=d["title"], detail={"removed": d["removes"]})

    return {
        "entities": [{"id": e["id"], "name": e["name"]} for e in entities],
        "reports": [{"id": r["id"], "title": r["title"]} for r in reports],
        "documents": [{"id": d["id"], "title": d["title"]} for d in documents],
        "files_removed": len(removed_files),
    }
