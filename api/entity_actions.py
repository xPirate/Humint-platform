"""Actions offered on a single record from the right-click menu.

Everything here starts from "I am looking at this record and want the app to do
one thing about it." The two that need a model are separated from the rest so
the menu can grey them out rather than fail when Ollama is off.

Nothing here writes to `relationships` or edits a record. Finding similar
records is a read; flagging a duplicate writes a *suggestion* into the review
queue, the same row the worker's correlation pass writes, and a person still
decides. That is the same promise the extraction and propose paths make.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import assistant as assistant_module
import audit
import auth
import entities as entities_module
import ollama_usage
from db import db_cursor
from ollama_client import OllamaClient
from ollama_config import get_effective_ollama_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["entity-actions"])

# Lower than the worker's duplicate threshold (0.88) on purpose: this list
# answers "what reads like this record", which is a wider question than "what
# IS this record". The score is shown so the analyst can judge.
SIMILAR_FLOOR = 0.55


class FlagDuplicate(BaseModel):
    other_entity_id: str
    score: float | None = None


def _entity_row(cur, entity_id: str) -> dict:
    cur.execute("SELECT id, entity_type, name, description, embedding, is_active "
                "FROM entities WHERE id = %s", (entity_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return dict(zip(("id", "entity_type", "name", "description", "embedding", "is_active"), row))


@router.get("/entities/{entity_id}/similar")
def similar_entities(entity_id: str,
                     limit: int = Query(default=8, ge=1, le=25),
                     same_type_only: bool = Query(default=False),
                     user: dict = Depends(auth.require_user)):
    """Records that read like this one, by embedding distance.

    Uses the embedding the worker already stored. If this record has none yet
    (it was created seconds ago, or the embed model was added later) one is
    computed for the question and thrown away — the worker still owns the
    stored copy.
    """
    with db_cursor() as cur:
        subject = _entity_row(cur, entity_id)
        vec = None
        if subject["embedding"]:
            try:
                vec = json.loads(subject["embedding"])
            except (TypeError, ValueError):
                vec = None
        if vec is None:
            config = get_effective_ollama_config()
            ollama = OllamaClient(**config, on_call=lambda **call: ollama_usage.record(
                "api", user_id=user["id"], **call))
            if not ollama.enabled or not ollama.embed_model:
                raise HTTPException(
                    status_code=503,
                    detail="No embedding for this record yet, and no embed model is configured.")
            text = subject["name"] + (". " + subject["description"] if subject["description"] else "")
            vec = ollama.embed(text)
            if vec is None:
                raise HTTPException(status_code=502, detail="Couldn't reach Ollama for an embedding.")

        scored = assistant_module._embedding_entities(cur, vec, limit * 4)
        ids = [eid for _, eid in scored if eid != entity_id]
        by_id = {}
        if ids:
            cur.execute(
                "SELECT id, entity_type, name, description, is_active FROM entities "
                "WHERE id = ANY(%s)", (ids,))
            by_id = {r[0]: {"id": r[0], "entity_type": r[1], "name": r[2],
                            "description": r[3], "is_active": r[4]} for r in cur.fetchall()}

        # Pairs a person has already ruled on stay out: offering the same
        # "is this the same thing?" question a second time is how a review
        # queue trains people to click through it.
        cur.execute(
            "SELECT subject_a_id, subject_b_id, status FROM correlation_suggestions "
            "WHERE subject_type = 'entity' AND (subject_a_id = %s OR subject_b_id = %s)",
            (entity_id, entity_id))
        seen = {}
        for a, b, status in cur.fetchall():
            seen[b if a == entity_id else a] = status

        out = []
        for score, eid in scored:
            if eid == entity_id or eid not in by_id:
                continue
            row = by_id[eid]
            if score < SIMILAR_FLOOR:
                continue
            if same_type_only and row["entity_type"] != subject["entity_type"]:
                continue
            row["score"] = round(float(score), 4)
            row["already_flagged"] = seen.get(eid)
            out.append(row)
            if len(out) >= limit:
                break

    return {"entity": {"id": subject["id"], "name": subject["name"],
                       "entity_type": subject["entity_type"]},
            "items": out, "floor": SIMILAR_FLOOR}


@router.post("/entities/{entity_id}/flag-duplicate", status_code=201)
def flag_duplicate(entity_id: str, payload: FlagDuplicate,
                   user: dict = Depends(auth.require_user)):
    """Put a pair into the correlation review queue by hand.

    For the case the worker cannot reach: two records an analyst can see are
    the same thing, whose text is too different for an embedding to notice.
    """
    other = payload.other_entity_id
    if other == entity_id:
        raise HTTPException(status_code=400, detail="That is the same record.")
    with db_cursor(commit=True) as cur:
        a = _entity_row(cur, entity_id)
        b = _entity_row(cur, other)
        # Stored in a fixed order so the unique index does its job whichever
        # way round the analyst was looking at the pair.
        first, second = sorted([entity_id, other])
        cur.execute(
            "INSERT INTO correlation_suggestions (subject_type, subject_a_id, subject_b_id, similarity_score) "
            "VALUES ('entity', %s, %s, %s) "
            "ON CONFLICT (subject_type, subject_a_id, subject_b_id) DO NOTHING RETURNING id",
            (first, second, float(payload.score or 1.0)))
        row = cur.fetchone()
    if row is None:
        return {"created": False, "message": "That pair is already in the review queue."}
    audit.record("correlation.flag", user=user, object_type="correlation_suggestion",
                 object_id=row[0], object_label=f"{a['name']} / {b['name']}",
                 detail={"a": entity_id, "b": other, "by_hand": True})
    return {"created": True, "id": row[0]}


@router.get("/entities/{entity_id}/action-context")
def action_context(entity_id: str, user: dict = Depends(auth.require_user)):
    """What the menu needs to know before it opens: what this record is, and
    which of the actions can do anything right now."""
    config = get_effective_ollama_config()
    with db_cursor() as cur:
        subject = _entity_row(cur, entity_id)
        cur.execute("SELECT count(*) FROM relationships WHERE from_entity_id = %s OR to_entity_id = %s",
                    (entity_id, entity_id))
        rels = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM report_entities WHERE entity_id = %s", (entity_id,))
        reports = cur.fetchone()[0]
        has_coords = False
        if subject["entity_type"] == "location":
            cur.execute("SELECT lat, lng FROM location_details WHERE entity_id = %s", (entity_id,))
            row = cur.fetchone()
            has_coords = bool(row and row[0] is not None and row[1] is not None)
    return {
        "id": subject["id"], "entity_type": subject["entity_type"], "name": subject["name"],
        "is_active": subject["is_active"],
        "relationship_count": rels, "report_count": reports,
        "has_coordinates": has_coords,
        "has_embedding": bool(subject["embedding"]),
        "ollama_enabled": bool(config.get("enabled")),
        "embed_model_configured": bool(config.get("embed_model")),
    }
