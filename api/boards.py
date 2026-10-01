"""The three optional boards: Roster, BOLO, and Intel priorities.

WHY THEY ARE OPTIONAL

The app gets used for jobs that have almost nothing in common. A homicide file
wants a BOLO on a vehicle and has no roster. A radio team running a fox hunt
wants priorities listing frequencies and areas and neither of the other two. A
standing team wants the roster and little else. Shipping all three to everyone
would put two dead tabs in most people's nav bar, and a dead tab is worse than
a missing one: it has to be explained every time somebody new arrives.

So an administrator turns each on separately, names it what the team calls it,
and everyone else simply has a page or does not. All three are off on a fresh
install and after an upgrade.

WHY ROSTER AND BOLO PIN RECORDS, AND PRIORITIES DO NOT

A BOLO on a vehicle is *that vehicle*, with a reason and an urgency attached.
Copying the plate onto a board would create a second version that drifts from
the record the moment somebody corrects one of them, so a board entry points
at the entity and carries only what is true of the posting rather than of the
thing.

A priority is not a thing at all. "Any transmission on 146.520 in the north
valley" is a question, and making it an entity would drop a requirement into
the relationship network as though it were a fact about the world. It gets its
own table, and links to the records it is about.

WHO DOES WHAT

An administrator decides which boards exist. Any analyst can post to one and
resolve an entry, because raising a BOLO is operational and waiting for an
admin to do it defeats the point. Everything is audited either way.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import audit
import auth
from db import db_cursor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["boards"])

BOARD_KINDS = ("roster", "bolo", "priorities")
PINNED_BOARDS = ("roster", "bolo")
URGENCIES = ("Info", "Caution", "Urgent", "Critical")
ENTRY_STATUSES = ("active", "resolved", "cancelled")
PRIORITY_STATUSES = ("open", "answered", "closed")

DEFAULT_LABELS = {"roster": "Roster", "bolo": "BOLO", "priorities": "Priorities"}
DEFAULT_BLURBS = {
    "roster": "Who is on this team, and how to reach them.",
    "bolo": "Be on the lookout. Anything the team should recognise on sight.",
    "priorities": "What we are looking for, and what would count as an answer.",
}


# ---------------------------------------------------------------------------
# Which boards exist
# ---------------------------------------------------------------------------

class BoardConfig(BaseModel):
    enabled: bool = False
    label: str | None = None
    blurb: str | None = None


def _load_boards(cur) -> dict:
    cur.execute("SELECT kind, enabled, label, blurb FROM boards")
    stored = {r[0]: {"enabled": r[1], "label": r[2], "blurb": r[3]} for r in cur.fetchall()}
    out = {}
    for kind in BOARD_KINDS:
        row = stored.get(kind, {"enabled": False, "label": None, "blurb": None})
        out[kind] = {
            "enabled": bool(row["enabled"]),
            # The stored label is an override; the default is what the nav
            # shows until somebody renames it.
            "label": row["label"] or DEFAULT_LABELS[kind],
            "blurb": row["blurb"] or DEFAULT_BLURBS[kind],
            "custom_label": row["label"],
            "custom_blurb": row["blurb"],
        }
    return out


@router.get("/boards")
def get_boards(user: dict = Depends(auth.require_user)):
    """Which boards this instance shows, and what they are called.

    Read by every user on login so the nav bar can be built. Not admin-only:
    knowing that a page exists is not a privilege.
    """
    with db_cursor() as cur:
        return {"boards": _load_boards(cur)}


@router.put("/admin/boards")
def put_boards(payload: dict[str, BoardConfig], user: dict = Depends(auth.require_admin)):
    with db_cursor(commit=True) as cur:
        before = _load_boards(cur)
        for kind, config in payload.items():
            if kind not in BOARD_KINDS:
                raise HTTPException(status_code=400, detail=f"Unknown board: {kind}")
            label = (config.label or "").strip() or None
            blurb = (config.blurb or "").strip() or None
            cur.execute(
                "INSERT INTO boards (kind, enabled, label, blurb, updated_at, updated_by) "
                "VALUES (%s, %s, %s, %s, now(), %s) "
                "ON CONFLICT (kind) DO UPDATE SET enabled = EXCLUDED.enabled, "
                "  label = EXCLUDED.label, blurb = EXCLUDED.blurb, "
                "  updated_at = now(), updated_by = EXCLUDED.updated_by",
                (kind, config.enabled, label, blurb, user["id"]))
        after = _load_boards(cur)
    changed = [k for k in BOARD_KINDS if before[k]["enabled"] != after[k]["enabled"]]
    audit.record("boards.configure", user=user, object_type="boards", object_id=None,
                 object_label="Boards",
                 detail={"enabled": {k: after[k]["enabled"] for k in BOARD_KINDS},
                         "toggled": changed})
    return {"boards": after}


def _require_board(cur, kind: str) -> dict:
    """A board that is switched off answers 404 rather than an empty list.

    An analyst cannot reach a disabled board through the UI at all, so a
    request for one is either a stale tab or somebody poking at the API; in
    both cases "there is no such page here" is the true answer.
    """
    boards = _load_boards(cur)
    if kind not in boards or not boards[kind]["enabled"]:
        raise HTTPException(status_code=404, detail="That board is not switched on.")
    return boards[kind]


# ---------------------------------------------------------------------------
# Roster and BOLO entries
# ---------------------------------------------------------------------------

class EntryCreate(BaseModel):
    entity_id: str
    role: str | None = None
    callsign: str | None = None
    contact_note: str | None = None
    reason: str | None = None
    urgency: str | None = None
    expires_at: str | None = None


class EntryUpdate(BaseModel):
    role: str | None = None
    callsign: str | None = None
    contact_note: str | None = None
    reason: str | None = None
    urgency: str | None = None
    expires_at: str | None = None
    status: str | None = None
    resolved_note: str | None = None
    sort_order: int | None = None


ENTRY_COLS = ("id, board, entity_id, role, callsign, contact_note, reason, urgency, "
              "sort_order, status, resolved_note, resolved_at, expires_at, "
              "created_by, created_at, updated_at")


def _entry_rows(cur, board: str, status: str | None):
    where = ["b.board = %s"]
    params: list = [board]
    if status:
        where.append("b.status = %s")
        params.append(status)
    cur.execute(
        f"SELECT {', '.join('b.' + c.strip() for c in ENTRY_COLS.split(','))}, "
        "       e.name, e.entity_type, e.is_active, e.portrait_attachment_id, "
        "       u.username "
        "  FROM board_entries b "
        "  JOIN entities e ON e.id = b.entity_id "
        "  LEFT JOIN users u ON u.id = b.created_by "
        f" WHERE {' AND '.join(where)} "
        " ORDER BY b.sort_order, b.created_at DESC", params)
    keys = [c.strip() for c in ENTRY_COLS.split(",")] + [
        "name", "entity_type", "entity_active", "portrait_attachment_id", "created_by_name"]
    out = []
    for row in cur.fetchall():
        item = dict(zip(keys, row))
        for k in ("resolved_at", "expires_at", "created_at", "updated_at"):
            item[k] = item[k].isoformat() if item[k] else None
        out.append(item)
    return out


def _entry_alignment(cur, items: list) -> None:
    """Fills in the alignment for entries whose type carries one, so a hostile
    name reads as hostile on the board exactly as it does everywhere else."""
    by_type: dict = {}
    for item in items:
        by_type.setdefault(item["entity_type"], []).append(item["entity_id"])
    tables = {"person": "person_details", "organization": "organization_details",
              "source": "source_details", "vehicle": "vehicle_details"}
    found = {}
    for entity_type, ids in by_type.items():
        table = tables.get(entity_type)
        if not table or not ids:
            continue
        cur.execute(f"SELECT entity_id, alignment FROM {table} WHERE entity_id = ANY(%s)", (ids,))
        found.update(dict(cur.fetchall()))
    for item in items:
        item["alignment"] = found.get(item["entity_id"])


@router.get("/boards/{board}/entries")
def list_entries(board: str,
                 status: str = Query(default="active"),
                 user: dict = Depends(auth.require_user)):
    if board not in PINNED_BOARDS:
        raise HTTPException(status_code=404, detail="No such board.")
    with db_cursor() as cur:
        config = _require_board(cur, board)
        items = _entry_rows(cur, board, None if status == "all" else status)
        _entry_alignment(cur, items)
    return {"board": board, "config": config, "items": items}


def _validate_entry(board: str, urgency: str | None) -> None:
    if urgency is not None and urgency not in URGENCIES:
        raise HTTPException(status_code=400,
                            detail=f"Urgency must be one of {', '.join(URGENCIES)}.")


@router.post("/boards/{board}/entries", status_code=201)
def create_entry(board: str, payload: EntryCreate, user: dict = Depends(auth.require_user)):
    if board not in PINNED_BOARDS:
        raise HTTPException(status_code=404, detail="No such board.")
    _validate_entry(board, payload.urgency)
    with db_cursor(commit=True) as cur:
        _require_board(cur, board)
        cur.execute("SELECT name FROM entities WHERE id = %s", (payload.entity_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="That record doesn't exist.")
        cur.execute("SELECT id FROM board_entries WHERE board = %s AND entity_id = %s "
                    "AND status = 'active'", (board, payload.entity_id))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="That record is already on this board.")
        # New entries go to the top. Somebody posting a BOLO right now means it
        # for right now; making them drag it up afterwards would be silly.
        cur.execute("SELECT COALESCE(min(sort_order), 0) - 1 FROM board_entries "
                    "WHERE board = %s AND status = 'active'", (board,))
        order = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO board_entries (board, entity_id, role, callsign, contact_note, "
            "        reason, urgency, expires_at, sort_order, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (board, payload.entity_id, payload.role, payload.callsign, payload.contact_note,
             payload.reason, payload.urgency, payload.expires_at or None, order, user["id"]))
        entry_id = cur.fetchone()[0]
    audit.record(f"board.{board}.post", user=user, object_type="board_entry",
                 object_id=entry_id, object_label=row[0],
                 detail={"entity_id": payload.entity_id, "urgency": payload.urgency})
    return {"id": entry_id}


@router.patch("/boards/{board}/entries/{entry_id}")
def update_entry(board: str, entry_id: int, payload: EntryUpdate,
                 user: dict = Depends(auth.require_user)):
    if board not in PINNED_BOARDS:
        raise HTTPException(status_code=404, detail="No such board.")
    _validate_entry(board, payload.urgency)
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("status") and updates["status"] not in ENTRY_STATUSES:
        raise HTTPException(status_code=400, detail="Unknown status.")
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    with db_cursor(commit=True) as cur:
        _require_board(cur, board)
        cur.execute("SELECT b.status, e.name FROM board_entries b JOIN entities e "
                    "ON e.id = b.entity_id WHERE b.id = %s AND b.board = %s", (entry_id, board))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such entry.")
        was_status, name = row
        # Closing one out stamps the time here rather than trusting a client
        # clock, and reopening clears it so a reopened entry does not claim to
        # have been resolved.
        extra = ""
        if "status" in updates:
            extra = (", resolved_at = now()" if updates["status"] != "active"
                     else ", resolved_at = NULL, resolved_note = NULL")
        if updates.get("expires_at") == "":
            updates["expires_at"] = None
        sets = ", ".join(f"{k} = %s" for k in updates)
        cur.execute(f"UPDATE board_entries SET {sets}, updated_at = now(){extra} WHERE id = %s",
                    [*updates.values(), entry_id])
    if "status" in updates and updates["status"] != was_status:
        audit.record(f"board.{board}.{updates['status']}", user=user, object_type="board_entry",
                     object_id=entry_id, object_label=name,
                     detail={"from": was_status, "note": updates.get("resolved_note")})
    else:
        audit.record(f"board.{board}.update", user=user, object_type="board_entry",
                     object_id=entry_id, object_label=name,
                     detail={"fields": sorted(updates)})
    return {"id": entry_id}


class ReorderRequest(BaseModel):
    ids: list[int]


@router.post("/boards/{board}/reorder")
def reorder(board: str, payload: ReorderRequest, user: dict = Depends(auth.require_user)):
    """The whole point of a board is that somebody decided what goes at the
    top, so the order is stored rather than derived."""
    if board not in PINNED_BOARDS:
        raise HTTPException(status_code=404, detail="No such board.")
    with db_cursor(commit=True) as cur:
        _require_board(cur, board)
        for position, entry_id in enumerate(payload.ids):
            cur.execute("UPDATE board_entries SET sort_order = %s WHERE id = %s AND board = %s",
                        (position, entry_id, board))
    return {"ok": True, "count": len(payload.ids)}


@router.delete("/boards/{board}/entries/{entry_id}", status_code=204)
def delete_entry(board: str, entry_id: int, user: dict = Depends(auth.require_user)):
    """Takes the posting off the board. The record itself is untouched —
    that is the whole reason a board entry is a pin and not a copy."""
    if board not in PINNED_BOARDS:
        raise HTTPException(status_code=404, detail="No such board.")
    with db_cursor(commit=True) as cur:
        _require_board(cur, board)
        cur.execute("SELECT e.name FROM board_entries b JOIN entities e ON e.id = b.entity_id "
                    "WHERE b.id = %s AND b.board = %s", (entry_id, board))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such entry.")
        cur.execute("DELETE FROM board_entries WHERE id = %s", (entry_id,))
    audit.record(f"board.{board}.remove", user=user, object_type="board_entry",
                 object_id=entry_id, object_label=row[0])


# ---------------------------------------------------------------------------
# Intel priorities
# ---------------------------------------------------------------------------

class PriorityWrite(BaseModel):
    title: str | None = None
    question: str | None = None
    priority: int | None = Field(default=None, ge=1, le=4)
    status: str | None = None
    indicators: list[str] | None = None
    owner_id: int | None = None
    due_date: str | None = None
    answered_note: str | None = None
    # Which records this requirement is about. Part of the body rather than a
    # query parameter: a list is not a scalar, so FastAPI would otherwise
    # nest the whole payload under a "payload" key to make room for it.
    # None means "leave the links alone"; [] means "clear them".
    entity_ids: list[str] | None = None


# Everything except the links, which are written to their own table.
PRIORITY_FIELDS = ("title", "question", "priority", "status", "indicators",
                   "owner_id", "due_date", "answered_note")


PRIORITY_COLS = ("id, title, question, priority, status, indicators, owner_id, due_date, "
                 "answered_note, answered_at, created_by, created_at, updated_at")


def _priority_rows(cur, status: str | None):
    where, params = [], []
    if status:
        where.append("p.status = %s")
        params.append(status)
    cur.execute(
        f"SELECT {', '.join('p.' + c.strip() for c in PRIORITY_COLS.split(','))}, "
        "       o.username, c.username "
        "  FROM intel_priorities p "
        "  LEFT JOIN users o ON o.id = p.owner_id "
        "  LEFT JOIN users c ON c.id = p.created_by "
        + (" WHERE " + " AND ".join(where) if where else "") +
        # Open first, then by how badly it is wanted, then by what is due
        # soonest. NULLS LAST so an undated priority does not outrank a
        # dated one purely by having less information on it.
        " ORDER BY CASE p.status WHEN 'open' THEN 0 ELSE 1 END, p.priority, "
        "          p.due_date NULLS LAST, p.created_at DESC", params)
    keys = [c.strip() for c in PRIORITY_COLS.split(",")] + ["owner_name", "created_by_name"]
    items = []
    for row in cur.fetchall():
        item = dict(zip(keys, row))
        item["indicators"] = list(item["indicators"] or [])
        for k in ("due_date", "answered_at", "created_at", "updated_at"):
            item[k] = item[k].isoformat() if item[k] else None
        items.append(item)

    if items:
        cur.execute(
            "SELECT pe.priority_id, e.id, e.name, e.entity_type "
            "  FROM intel_priority_entities pe JOIN entities e ON e.id = pe.entity_id "
            " WHERE pe.priority_id = ANY(%s) ORDER BY e.name", ([i["id"] for i in items],))
        linked: dict = {}
        for pid, eid, name, etype in cur.fetchall():
            linked.setdefault(pid, []).append({"id": eid, "name": name, "entity_type": etype})
        for item in items:
            item["entities"] = linked.get(item["id"], [])
    return items


@router.get("/priorities")
def list_priorities(status: str = Query(default="open"),
                    user: dict = Depends(auth.require_user)):
    with db_cursor() as cur:
        config = _require_board(cur, "priorities")
        items = _priority_rows(cur, None if status == "all" else status)
    return {"config": config, "items": items}


@router.post("/priorities", status_code=201)
def create_priority(payload: PriorityWrite, user: dict = Depends(auth.require_user)):
    if not (payload.title or "").strip():
        raise HTTPException(status_code=400, detail="A priority needs a title.")
    with db_cursor(commit=True) as cur:
        _require_board(cur, "priorities")
        cur.execute(
            "INSERT INTO intel_priorities (title, question, priority, indicators, owner_id, "
            "        due_date, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (payload.title.strip(), payload.question, payload.priority or 2,
             payload.indicators or [], payload.owner_id, payload.due_date or None, user["id"]))
        pid = cur.fetchone()[0]
        for eid in payload.entity_ids or []:
            cur.execute("INSERT INTO intel_priority_entities (priority_id, entity_id) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING", (pid, eid))
    audit.record("priority.create", user=user, object_type="intel_priority", object_id=pid,
                 object_label=payload.title.strip(), detail={"priority": payload.priority or 2})
    return {"id": pid}


@router.patch("/priorities/{priority_id}")
def update_priority(priority_id: int, payload: PriorityWrite,
                    user: dict = Depends(auth.require_user)):
    sent = payload.model_dump(exclude_unset=True)
    entity_ids = sent.pop("entity_ids", None)
    updates = {k: v for k, v in sent.items() if k in PRIORITY_FIELDS}
    if updates.get("status") and updates["status"] not in PRIORITY_STATUSES:
        raise HTTPException(status_code=400, detail="Unknown status.")
    with db_cursor(commit=True) as cur:
        _require_board(cur, "priorities")
        cur.execute("SELECT title, status FROM intel_priorities WHERE id = %s", (priority_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such priority.")
        was_title, was_status = row
        if updates:
            extra = ""
            if "status" in updates:
                extra = (", answered_at = now()" if updates["status"] == "answered"
                         else ", answered_at = NULL")
            if updates.get("due_date") == "":
                updates["due_date"] = None
            sets = ", ".join(f"{k} = %s" for k in updates)
            cur.execute(f"UPDATE intel_priorities SET {sets}, updated_at = now(){extra} "
                        "WHERE id = %s", [*updates.values(), priority_id])
        if entity_ids is not None:
            cur.execute("DELETE FROM intel_priority_entities WHERE priority_id = %s", (priority_id,))
            for eid in entity_ids:
                cur.execute("INSERT INTO intel_priority_entities (priority_id, entity_id) "
                            "VALUES (%s, %s) ON CONFLICT DO NOTHING", (priority_id, eid))
    action = ("priority." + updates["status"]
              if updates.get("status") and updates["status"] != was_status
              else "priority.update")
    audit.record(action, user=user, object_type="intel_priority", object_id=priority_id,
                 object_label=updates.get("title") or was_title,
                 detail={"fields": sorted(updates)})
    return {"id": priority_id}


@router.delete("/priorities/{priority_id}", status_code=204)
def delete_priority(priority_id: int, user: dict = Depends(auth.require_user)):
    with db_cursor(commit=True) as cur:
        _require_board(cur, "priorities")
        cur.execute("SELECT title FROM intel_priorities WHERE id = %s", (priority_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such priority.")
        cur.execute("DELETE FROM intel_priorities WHERE id = %s", (priority_id,))
    audit.record("priority.delete", user=user, object_type="intel_priority",
                 object_id=priority_id, object_label=row[0])


# ---------------------------------------------------------------------------
# What a record is on
# ---------------------------------------------------------------------------

@router.get("/entities/{entity_id}/boards")
def entity_boards(entity_id: str, user: dict = Depends(auth.require_user)):
    """Which boards this record is on, for its own page.

    An analyst reading a record needs to know it is on the BOLO — otherwise
    the board is a page people remember to check rather than something the
    app tells them.
    """
    with db_cursor() as cur:
        boards = _load_boards(cur)
        cur.execute(
            "SELECT id, board, role, callsign, reason, urgency, status, expires_at "
            "  FROM board_entries WHERE entity_id = %s AND status = 'active'", (entity_id,))
        entries = [{"id": r[0], "board": r[1], "role": r[2], "callsign": r[3], "reason": r[4],
                    "urgency": r[5], "status": r[6],
                    "expires_at": r[7].isoformat() if r[7] else None}
                   for r in cur.fetchall() if boards[r[1]]["enabled"]]
        priorities = []
        if boards["priorities"]["enabled"]:
            cur.execute(
                "SELECT p.id, p.title, p.priority, p.status FROM intel_priorities p "
                "  JOIN intel_priority_entities pe ON pe.priority_id = p.id "
                " WHERE pe.entity_id = %s AND p.status = 'open' ORDER BY p.priority",
                (entity_id,))
            priorities = [{"id": r[0], "title": r[1], "priority": r[2], "status": r[3]}
                          for r in cur.fetchall()]
    return {"entries": entries, "priorities": priorities,
            "labels": {k: boards[k]["label"] for k in BOARD_KINDS}}


# ---------------------------------------------------------------------------
# Portraits
# ---------------------------------------------------------------------------

class PortraitRequest(BaseModel):
    attachment_id: int | None = None


@router.post("/entities/{entity_id}/portrait")
def set_portrait(entity_id: str, payload: PortraitRequest,
                 user: dict = Depends(auth.require_user)):
    """Nominate one of the record's own images as the picture that represents
    it. Passing null clears it.

    Deliberately a pointer at an existing attachment rather than a second
    upload: the same photograph then serves the Roster card, the record page
    and a dossier export, and there is only ever one of it to keep current.
    """
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT name FROM entities WHERE id = %s", (entity_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Entity not found")
        if payload.attachment_id is not None:
            cur.execute("SELECT mime_type, entity_id FROM attachments WHERE id = %s",
                        (payload.attachment_id,))
            att = cur.fetchone()
            if att is None:
                raise HTTPException(status_code=404, detail="No such document.")
            if not (att[0] or "").startswith("image/"):
                raise HTTPException(status_code=400,
                                    detail="A portrait has to be an image.")
            if att[1] != entity_id:
                raise HTTPException(
                    status_code=400,
                    detail="That document belongs to a different record. File it here first.")
        cur.execute("UPDATE entities SET portrait_attachment_id = %s WHERE id = %s",
                    (payload.attachment_id, entity_id))
    audit.record("entity.portrait", user=user, object_type="entity", object_id=entity_id,
                 object_label=row[0], detail={"attachment_id": payload.attachment_id})
    return {"id": entity_id, "portrait_attachment_id": payload.attachment_id}
