"""Admin-facing read API for the audit log.

Read-only on purpose: there is no endpoint here to edit or delete an entry,
because the whole value of the table depends on the app never offering a way
to rewrite it. The only deletion anywhere is the retention prune in
worker/audit_prune.py, and that only ever removes whole entries older than a
configured age — it can't be aimed at a specific one.

Kept separate from api/audit.py (which is the write path, and is duplicated
into the worker) so the worker never carries HTTP routing it has no use for.
"""

import csv
import io
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response

import audit
import auth
from db import db_cursor

router = APIRouter(prefix="/api/admin", tags=["admin"])

_COLUMNS = [
    "id", "occurred_at", "actor_id", "actor_username", "actor_kind", "action",
    "object_type", "object_id", "object_label", "outcome", "ip_address",
    "user_agent", "detail",
]

MAX_CSV_ROWS = 50000


def _build_filters(
    actor: Optional[str], action: Optional[str], object_type: Optional[str],
    object_id: Optional[str], outcome: Optional[str],
    since: Optional[datetime], until: Optional[datetime], q: Optional[str],
):
    where, params = [], []
    if actor:
        where.append("actor_username ILIKE %s")
        params.append(f"%{actor}%")
    if action:
        # Prefix match so "entity" finds entity.create/update/archive without
        # the caller needing to know the full vocabulary.
        where.append("action ILIKE %s")
        params.append(f"{action}%")
    if object_type:
        where.append("object_type = %s")
        params.append(object_type)
    if object_id:
        where.append("object_id = %s")
        params.append(object_id)
    if outcome:
        where.append("outcome = %s")
        params.append(outcome)
    if since:
        where.append("occurred_at >= %s")
        params.append(since)
    if until:
        where.append("occurred_at <= %s")
        params.append(until)
    if q:
        where.append("(object_label ILIKE %s OR object_id ILIKE %s OR action ILIKE %s)")
        params.extend([f"%{q}%"] * 3)
    return (f"WHERE {' AND '.join(where)}" if where else ""), params


def _row_to_dict(row) -> dict:
    return dict(zip(_COLUMNS, row))


@router.get("/audit-log")
def list_audit_log(
    actor: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    object_type: Optional[str] = Query(default=None),
    object_id: Optional[str] = Query(default=None),
    outcome: Optional[str] = Query(default=None),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
    q: Optional[str] = Query(default=None, description="Substring match on label, object id or action"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(auth.require_admin),
):
    where_clause, params = _build_filters(actor, action, object_type, object_id, outcome, since, until, q)
    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM audit_log {where_clause}", params)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM audit_log {where_clause} "
            "ORDER BY occurred_at DESC, id DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        items = [_row_to_dict(r) for r in cur.fetchall()]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/audit-log/actions")
def list_audit_actions(user: dict = Depends(auth.require_admin)):
    """The action vocabulary actually present in this instance's log, for
    populating the filter dropdown — better than a hardcoded list, which
    would drift the moment a new action is added anywhere in the app."""
    with db_cursor() as cur:
        cur.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")
        actions = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT actor_username FROM audit_log WHERE actor_username IS NOT NULL ORDER BY 1")
        actors = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT object_type FROM audit_log WHERE object_type IS NOT NULL ORDER BY 1")
        object_types = [r[0] for r in cur.fetchall()]
    return {
        "actions": actions,
        "actors": actors,
        "object_types": object_types,
        "syslog": audit.syslog_status(),
    }


@router.get("/audit-log.csv")
def export_audit_log_csv(
    actor: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    object_type: Optional[str] = Query(default=None),
    object_id: Optional[str] = Query(default=None),
    outcome: Optional[str] = Query(default=None),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
    q: Optional[str] = Query(default=None),
    user: dict = Depends(auth.require_admin),
):
    """The whole filtered set, for handing to someone outside the app. Capped
    at MAX_CSV_ROWS so a request against a very large log can't build an
    unbounded string in memory."""
    where_clause, params = _build_filters(actor, action, object_type, object_id, outcome, since, until, q)
    with db_cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM audit_log {where_clause} "
            "ORDER BY occurred_at DESC, id DESC LIMIT %s",
            [*params, MAX_CSV_ROWS],
        )
        rows = cur.fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_COLUMNS)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"humint-audit-{stamp}.csv"
    # Exporting the audit log is itself an egress event, and one an auditor
    # would very much expect to see recorded.
    audit.record("audit.export", user=user, object_type="audit_log", object_label=filename,
                 detail={"rows": len(rows), "truncated": len(rows) >= MAX_CSV_ROWS,
                         "filters": {k: v for k, v in {
                             "actor": actor, "action": action, "object_type": object_type,
                             "object_id": object_id, "outcome": outcome, "q": q,
                             "since": since, "until": until}.items() if v}})
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
