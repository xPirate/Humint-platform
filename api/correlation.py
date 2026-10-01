"""Correlation review queue: confirm/dismiss the likely-duplicate or
likely-linked entity/report pairs the worker flags (see worker/correlate.py).
As of the RSS ingest feature this also surfaces a third, cross-type pair —
'report_event' — flagging when an analyst's own Report looks related to a
later-ingested Event, so a report written before the news broke can still
get linked once the matching Event shows up.

"Confirm" here only acknowledges that an analyst agrees the two records are
related/duplicates — it does NOT merge them. Actually merging two entities
(reassigning relationships/reports/attachments from one onto the other,
then archiving the loser) is real work this build deliberately doesn't take
on; flagging that honestly rather than quietly half-building a merge
feature. For now, confirming a match is a signal for the analyst to go
handle manually — e.g. adding a relationship between two related-but-
distinct entities, or consolidating a genuine duplicate's data by hand.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import audit
import auth
from db import db_cursor

router = APIRouter(prefix="/api", tags=["correlation"])

SUGGESTION_STATUSES = ("pending", "confirmed", "dismissed")
# 'report_event' is the cross-type pair the RSS ingest feature adds (see
# worker/correlate.py): subject_a_id is always a report, subject_b_id is
# always an Event entity — never sorted, unlike the same-type pairs below.
SUBJECT_TYPES = ("entity", "report", "report_event")


def _label_for(cur, kind: str, subject_id: str) -> Optional[str]:
    if kind == "entity":
        cur.execute("SELECT name FROM entities WHERE id = %s", (subject_id,))
    else:
        cur.execute("SELECT title FROM reports WHERE id = %s", (subject_id,))
    row = cur.fetchone()
    return row[0] if row else None


def _kinds_for(subject_type: str) -> tuple[str, str]:
    """Returns (kind of subject_a, kind of subject_b) — both the same for a
    same-type pair, but fixed and different for the report<->event pair."""
    if subject_type == "report_event":
        return "report", "entity"
    return subject_type, subject_type


@router.get("/correlation-suggestions")
def list_correlation_suggestions(
    status: Optional[str] = Query(default="pending"),
    subject_type: Optional[str] = Query(default=None),
    min_score: Optional[float] = Query(default=None, ge=0, le=1),
    max_score: Optional[float] = Query(default=None, ge=0, le=1),
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(auth.require_user),
):
    if status is not None and status not in SUGGESTION_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {SUGGESTION_STATUSES}")
    if subject_type is not None and subject_type not in SUBJECT_TYPES:
        raise HTTPException(status_code=400, detail=f"subject_type must be one of {SUBJECT_TYPES}")

    where = []
    params: list = []
    if status:
        where.append("status = %s")
        params.append(status)
    if subject_type:
        where.append("subject_type = %s")
        params.append(subject_type)
    # A score band is what makes "clear the weak tail" a thing you can select
    # rather than a thing you scroll past. The queue is sorted strongest-first,
    # so the tail is exactly where a bulk dismiss is safe and a bulk anything
    # is least safe at the top.
    if min_score is not None:
        where.append("similarity_score >= %s")
        params.append(min_score)
    if max_score is not None:
        where.append("similarity_score <= %s")
        params.append(max_score)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM correlation_suggestions {where_clause}", params)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT id, subject_type, subject_a_id, subject_b_id, similarity_score, status, created_at "
            f"FROM correlation_suggestions {where_clause} ORDER BY similarity_score DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        rows = cur.fetchall()

        items = []
        for r in rows:
            item = {
                "id": r[0], "subject_type": r[1], "subject_a_id": r[2], "subject_b_id": r[3],
                "similarity_score": r[4], "status": r[5], "created_at": r[6],
            }
            # Best-effort display labels — a subject that's since been
            # archived/deleted just shows as null rather than breaking the list.
            a_kind, b_kind = _kinds_for(item["subject_type"])
            item["subject_a_label"] = _label_for(cur, a_kind, item["subject_a_id"])
            item["subject_b_label"] = _label_for(cur, b_kind, item["subject_b_id"])
            items.append(item)

    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _resolve_suggestion(suggestion_id: int, new_status: str, user: dict) -> dict:
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT status FROM correlation_suggestions WHERE id = %s", (suggestion_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Suggestion not found")
        if row[0] != "pending":
            raise HTTPException(status_code=409, detail=f"Suggestion already {row[0]}")
        cur.execute(
            "UPDATE correlation_suggestions SET status = %s, reviewed_by = %s, reviewed_at = now() WHERE id = %s",
            (new_status, user["id"], suggestion_id),
        )
    audit.record(f"correlation.{new_status}", user=user, object_type="correlation_suggestion",
                 object_id=suggestion_id)
    return {"id": suggestion_id, "status": new_status}


@router.post("/correlation-suggestions/{suggestion_id}/confirm")
def confirm_correlation_suggestion(suggestion_id: int, user: dict = Depends(auth.require_user)):
    return _resolve_suggestion(suggestion_id, "confirmed", user)


@router.post("/correlation-suggestions/{suggestion_id}/dismiss")
def dismiss_correlation_suggestion(suggestion_id: int, user: dict = Depends(auth.require_user)):
    return _resolve_suggestion(suggestion_id, "dismissed", user)


# A ceiling on one request's worth of ids. The filter path below is the answer
# for "all two thousand of them"; this one is for a screen of ticked rows.
MAX_BULK_IDS = 500


class BulkResolve(BaseModel):
    """Either a list of ids, or a filter plus the count the user was shown."""
    action: str
    ids: Optional[list[int]] = None
    # The filter path. `expected_count` is not belt-and-braces: the queue is
    # written to by a background worker, so between the moment the dialog said
    # "2,014 suggestions" and the moment somebody pressed the button, the
    # worker may have added more. Acting on a set larger than the one that was
    # agreed to is exactly the kind of surprise a bulk action must not spring.
    all_matching: bool = False
    status: str = "pending"
    subject_type: Optional[str] = None
    min_score: Optional[float] = Field(default=None, ge=0, le=1)
    max_score: Optional[float] = Field(default=None, ge=0, le=1)
    expected_count: Optional[int] = None


@router.post("/correlation-suggestions/bulk")
def bulk_resolve(payload: BulkResolve, user: dict = Depends(auth.require_user)):
    """Accept or dismiss many suggestions at once.

    Two ways to say which: a list of ids from ticked rows, or the filters the
    queue is currently showing plus the count that was on screen.

    Only pending suggestions are touched. Anything already decided is left
    alone and counted separately rather than silently re-decided -- a bulk
    action that quietly overwrote somebody else's review would be worse than
    no bulk action.

    One audit entry for the batch, not one per row: two thousand identical
    entries would bury the log this app expects people to actually read.
    """
    if payload.action not in ("confirmed", "dismissed"):
        raise HTTPException(status_code=400, detail="action must be confirmed or dismissed")

    where = ["status = 'pending'"]
    params: list = []
    described: dict = {}

    if payload.all_matching:
        if payload.status and payload.status != "pending":
            raise HTTPException(
                status_code=400,
                detail="Only pending suggestions can be resolved in bulk")
        if payload.subject_type:
            if payload.subject_type not in SUBJECT_TYPES:
                raise HTTPException(status_code=400,
                                    detail=f"subject_type must be one of {SUBJECT_TYPES}")
            where.append("subject_type = %s")
            params.append(payload.subject_type)
            described["subject_type"] = payload.subject_type
        if payload.min_score is not None:
            where.append("similarity_score >= %s")
            params.append(payload.min_score)
            described["min_score"] = payload.min_score
        if payload.max_score is not None:
            where.append("similarity_score <= %s")
            params.append(payload.max_score)
            described["max_score"] = payload.max_score
    else:
        ids = payload.ids or []
        if not ids:
            raise HTTPException(status_code=400, detail="Nothing selected")
        if len(ids) > MAX_BULK_IDS:
            raise HTTPException(
                status_code=400,
                detail=f"{len(ids)} is more than {MAX_BULK_IDS} in one request — "
                       "use 'select everything matching' instead")
        where.append("id = ANY(%s)")
        params.append(ids)
        described["ids"] = len(ids)

    where_clause = " AND ".join(where)
    with db_cursor(commit=True) as cur:
        cur.execute(f"SELECT COUNT(*) FROM correlation_suggestions WHERE {where_clause}", params)
        matching = cur.fetchone()[0]

        if payload.all_matching and payload.expected_count is not None \
                and matching != payload.expected_count:
            raise HTTPException(
                status_code=409,
                detail=f'This now matches {matching} suggestions, not the {payload.expected_count} you were shown. Refresh and try again.')

        cur.execute(
            f"UPDATE correlation_suggestions SET status = %s, reviewed_by = %s, "
            f"reviewed_at = now() WHERE {where_clause}",
            [payload.action, user["id"], *params])
        changed = cur.rowcount

    audit.record(f"correlation.bulk_{payload.action}", user=user,
                 object_type="correlation_suggestion",
                 object_label=f"{changed} suggestion(s)",
                 detail={"resolved": changed, "selection": described,
                         "already_decided_and_left_alone": (payload.ids and
                                                            len(payload.ids) - changed) or 0})
    return {"resolved": changed, "action": payload.action,
            "skipped_not_pending": (len(payload.ids) - changed) if payload.ids else 0}
