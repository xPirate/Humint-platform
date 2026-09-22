"""Asking the assistant to propose links, into the review queue.

An analyst types "link my family" or "connect everyone at the rail yard"; this
retrieves the records that instruction is about, asks the model what
relationships it sees among them, and writes each one into the same review
queue the document-extraction suggestions go to.

NOTHING IS APPLIED

The model never writes to `relationships`. It writes proposals, each carrying
the instruction it was given and its own stated reason, and a person accepts or
dismisses them through the existing endpoints — which run the same validation an
analyst's own typing goes through. That is the app's standing promise and this
feature is the one most tempting to break it for, which is exactly why it does
not.

The cost of that choice is real: a request to link forty people produces forty
things to click. It is still the right trade. An analyst who accepts a bad edge
can see what they accepted; an analyst who discovers a model quietly wrote
thirty edges last Tuesday cannot easily tell which ones.

WHY IT ONLY PROPOSES RELATIONSHIPS

Not new entities, and not edits to existing records. A model inventing a Person
from a chat instruction is a different and much worse failure than it guessing
wrong about a connection between two records that a human already created, and
the extraction queue already covers "a document mentioned someone new".
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import assistant as assistant_module
import audit
import auth
import entities as entities_module
import ollama_usage
from db import db_cursor
from ollama_client import OllamaClient
from ollama_config import get_effective_ollama_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["propose"])

# How many records go into one proposal pass. Beyond this the prompt stops
# fitting a small model's context and the output quality falls off a cliff —
# and a reviewer facing sixty proposals from one request is not reviewing them.
MAX_CANDIDATES = 25

# A ceiling on what one request may add to the queue, independent of how many
# the model returns.
MAX_PROPOSALS = 20

PROPOSE_SYSTEM_PROMPT = """You are helping an analyst tidy a HUMINT case file. You \
will be given an instruction and a list of records that already exist in that file, \
each with its record id, type, name and description.

Propose relationships BETWEEN THE RECORDS LISTED. Respond with ONLY a JSON object \
with one key, "relationships", an array of objects each with:
  - "from_id": the record id of one entity, copied exactly from the list
  - "to_id": the record id of a different entity, copied exactly from the list
  - "relationship_type": lowercase snake_case, preferring one of: family_of, \
spouse_of, parent_of, child_of, sibling_of, significant_of, associate_of, \
employed_by, member_of, affiliated_with, located_at, present_at, communicated_with
  - "reason": one short sentence saying what in the records above supports this, \
quoting the specific detail. If the only reason is that the instruction asked \
for it, say that plainly.

Rules you must follow:
  - Only use record ids from the list. Never invent a record, a person, or an id.
  - Do not propose a relationship you cannot point at something for. An empty \
array is a perfectly good answer and is much better than a guess.
  - Do not propose the same pair twice.
  - "parent_of" means from_id is the PARENT of to_id. Get the direction right \
or leave it as family_of.
  - You are proposing questions for a person to check, not recording facts. \
Where you are unsure, prefer the vaguer type (family_of over parent_of).

Return {"relationships": []} if the records give you nothing to work with."""


class ProposeRequest(BaseModel):
    instruction: str = Field(min_length=3, max_length=1000)
    # Set when the request came from one record's menu rather than the box on
    # the Review page. That record is always in the candidate set, even if
    # retrieval on the instruction alone would not have found it.
    focus_entity_id: str | None = None


def _query_keywords(instruction: str) -> list[str]:
    """The assistant's keywords, plus a singular for each plural.

    Retrieval here is substring matching on names, and the single most natural
    way to write this instruction is a plural family name — "link the Smiths",
    "connect the Osbornes". Neither matches the record "Jane Smith". Trimming a
    trailing "s" (and "es" after s/x/z/ch/sh) is crude, but it is matching
    substrings against names, so a wrong singular simply finds nothing; the
    cost of guessing is zero and the benefit is that the obvious phrasing
    works."""
    keywords = assistant_module._keywords(instruction)
    extra = []
    for word in keywords:
        singular = None
        if len(word) > 4 and word.endswith(("ses", "xes", "zes", "ches", "shes")):
            singular = word[:-2]
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            singular = word[:-1]
        if singular and singular not in keywords and singular not in extra:
            extra.append(singular)
    return (keywords + extra)[:16]


def _expand_household(cur, seed_ids: list[str], limit: int) -> list[str]:
    """People who share a surname or a contact detail with a named record.

    An analyst who writes "link Jane Smith's family" has named one person and
    means a household. Without this the model is handed one record and can
    propose nothing. The two expansions are the same ones the signal rules
    already treat as evidence of a household, which keeps the two halves of
    this feature looking at the same world."""
    if not seed_ids or limit <= 0:
        return []
    cur.execute(
        """
        WITH seeds AS (
            SELECT id, lower(split_part(trim(name), ' ', -1)) AS surname
            FROM entities
            WHERE id = ANY(%(seeds)s) AND entity_type = 'person'
              AND array_length(string_to_array(trim(name), ' '), 1) > 1
        ),
        seed_contacts AS (
            SELECT regexp_replace(lower(trim(value)), '[^a-z0-9@.]', '', 'g') AS norm
            FROM contact_points WHERE entity_id = ANY(%(seeds)s)
        )
        SELECT DISTINCT e.id
        FROM entities e
        WHERE e.is_active AND e.entity_type = 'person'
          AND NOT (e.id = ANY(%(seeds)s))
          AND (
            lower(split_part(trim(e.name), ' ', -1)) IN (SELECT surname FROM seeds)
            OR EXISTS (
                SELECT 1 FROM contact_points c
                WHERE c.entity_id = e.id
                  AND regexp_replace(lower(trim(c.value)), '[^a-z0-9@.]', '', 'g')
                      IN (SELECT norm FROM seed_contacts)
            )
          )
        ORDER BY e.id
        LIMIT %(limit)s
        """,
        {"seeds": seed_ids, "limit": limit},
    )
    return [r[0] for r in cur.fetchall()]


def _candidates(cur, ollama, instruction: str, user: dict, focus_entity_id: str | None = None) -> list[dict]:
    """The records this instruction is about, via the assistant's own
    retrieval — so "link my family" finds the same records that asking the
    assistant about your family would."""
    entity_ids = []
    focus_text = None
    if focus_entity_id:
        cur.execute("SELECT name, description FROM entities WHERE id = %s AND is_active",
                    (focus_entity_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="That record no longer exists.")
        focus_text = row[0] + (". " + row[1] if row[1] else "")
        entity_ids.append(focus_entity_id)
    # Retrieval runs on the record's own text when there is one: "who is this
    # connected to" is a question about the record, and the instruction the
    # menu writes is only a sentence wrapped around its name.
    vec = (ollama.embed(focus_text or instruction)
           if ollama.enabled and ollama.embed_model else None)
    if vec is not None:
        for _, eid in assistant_module._embedding_entities(cur, vec, MAX_CANDIDATES):
            if eid not in entity_ids:
                entity_ids.append(eid)
    if len(entity_ids) < 2:
        for eid in assistant_module._keyword_entities(
                cur, _query_keywords(instruction), MAX_CANDIDATES):
            if eid not in entity_ids:
                entity_ids.append(eid)
    if not entity_ids:
        return []
    # Two retrieval passes can overshoot together; the prompt budget is the
    # same either way.
    entity_ids = entity_ids[:MAX_CANDIDATES]

    room = MAX_CANDIDATES - len(entity_ids)
    if room > 0:
        for extra_id in _expand_household(cur, entity_ids, room):
            if extra_id not in entity_ids:
                entity_ids.append(extra_id)

    cur.execute(
        """
        SELECT e.id, e.entity_type, e.name, e.description,
               p.occupation, p.date_of_birth
        FROM entities e
        LEFT JOIN person_details p ON p.entity_id = e.id
        WHERE e.id = ANY(%s) AND e.is_active
        ORDER BY e.entity_type, e.name
        """,
        (entity_ids,),
    )
    return [
        {"id": r[0], "entity_type": r[1], "name": r[2], "description": r[3],
         "occupation": r[4], "date_of_birth": r[5].isoformat() if r[5] else None}
        for r in cur.fetchall()
    ]


def _render_candidates(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        bits = [f"- id={r['id']} | {r['entity_type']} | {r['name']}"]
        if r.get("occupation"):
            bits.append(f"occupation: {r['occupation']}")
        if r.get("date_of_birth"):
            bits.append(f"born: {r['date_of_birth']}")
        if r.get("description"):
            bits.append(str(r["description"])[:300])
        lines.append(" | ".join(bits))
    return "\n".join(lines)


def _existing_pairs(cur, ids: list[str]) -> set:
    """Pairs already connected, in either direction. Proposing a link that is
    already recorded wastes the reviewer's only scarce resource."""
    if not ids:
        return set()
    cur.execute(
        "SELECT from_entity_id, to_entity_id FROM relationships "
        "WHERE from_entity_id = ANY(%s) AND to_entity_id = ANY(%s)",
        (ids, ids),
    )
    out = set()
    for a, b in cur.fetchall():
        out.add(frozenset((a, b)))
    return out


def _pending_by_pair(cur, ids: list[str]) -> dict:
    """Pairs that already have something waiting in the review queue.

    The signal pass and the assistant often reach the same two people from
    different directions, and two cards about the same pair is one decision
    presented twice. Where one already exists, the assistant's reasoning is
    added to it instead — see _annotate below."""
    if not ids:
        return {}
    cur.execute(
        "SELECT id, suggested_from_entity_id, suggested_to_entity_id, evidence "
        "FROM extraction_suggestions "
        "WHERE status = 'pending' AND suggestion_type = 'relationship' "
        "  AND suggested_from_entity_id = ANY(%s) AND suggested_to_entity_id = ANY(%s)",
        (ids, ids),
    )
    return {frozenset((a, b)): (row_id, evidence or {})
            for row_id, a, b, evidence in cur.fetchall()}


def _annotate(cur, row_id: int, evidence: dict, contribution: dict) -> bool:
    """Fold the assistant's view into a suggestion that is already queued.

    Nothing existing is overwritten — not the relationship type, not the
    confidence, not the original rule. The reviewer still makes one decision,
    and can now see that two independent things pointed at this pair. If the
    assistant's suggested type differs from the queued one it is recorded in
    the contribution, and the accept form lets the reviewer pick either."""
    merged = dict(evidence)
    entries = list(merged.get("all") or [])
    if any(e.get("signal") == "assistant" and e.get("instruction") == contribution.get("instruction")
           for e in entries if isinstance(e, dict)):
        return False  # the same instruction already annotated this one
    entries.append(contribution)
    merged["all"] = entries
    names = list(merged.get("signals") or ([merged["signal"]] if merged.get("signal") else []))
    if "assistant" not in names:
        names.append("assistant")
    merged["signals"] = names
    cur.execute("UPDATE extraction_suggestions SET evidence = %s::jsonb WHERE id = %s",
                (json.dumps(merged), row_id))
    return cur.rowcount > 0


@router.post("/suggestions/propose", status_code=201)
def propose_links(payload: ProposeRequest, user: dict = Depends(auth.require_user)):
    config = get_effective_ollama_config()
    ollama = OllamaClient(
        **config,
        on_call=lambda **call: ollama_usage.record("api", user_id=user["id"], **call),
    )
    if not ollama.enabled:
        raise HTTPException(
            status_code=503,
            detail="The assistant is disabled. An admin can enable Ollama under Admin settings → Model.",
        )

    with db_cursor() as cur:
                rows = _candidates(cur, ollama, payload.instruction, user, payload.focus_entity_id)
    if len(rows) < 2:
        raise HTTPException(
            status_code=404,
            detail="Couldn't find two existing entities matching that. Name them more specifically.",
        )

    prompt = (
        f"{PROPOSE_SYSTEM_PROMPT}\n\nThe analyst's instruction:\n{payload.instruction}\n\n"
        f"Records in the case file:\n{_render_candidates(rows)}"
    )
    raw = ollama.chat(prompt, [], "Propose the relationships now, as JSON only.")
    if raw is None:
        raise HTTPException(
            status_code=502,
            detail="Couldn't reach Ollama, or the configured model isn't available.",
        )

    proposals = _parse(raw, {r["id"] for r in rows})
    if not proposals:
        return {
            "created": 0, "considered": len(rows),
            "message": "The model didn't find anything it could support from these "
                       "records. That's a real answer, not a failure — nothing was added.",
        }

    by_id = {r["id"]: r for r in rows}
    created = 0
    annotated = 0
    with db_cursor(commit=True) as cur:
        existing = _existing_pairs(cur, list(by_id))
        pending = _pending_by_pair(cur, list(by_id))
        for p in proposals[:MAX_PROPOSALS]:
            pair = frozenset((p["from_id"], p["to_id"]))
            if pair in existing:
                continue
            contribution = {
                "signal": "assistant",
                "rule": "proposed by the assistant on request",
                "instruction": payload.instruction,
                "reason": p.get("reason") or "",
                "suggested_type": p["relationship_type"],
                "model": ollama.model,
            }
            if pair in pending:
                row_id, evidence = pending[pair]
                if _annotate(cur, row_id, evidence, contribution):
                    annotated += 1
                continue
            cur.execute(
                """
                INSERT INTO extraction_suggestions
                    (source, suggestion_type, suggested_relationship_type,
                     suggested_from_entity_id, suggested_to_entity_id,
                     suggested_from_name, suggested_to_name, confidence, evidence)
                VALUES ('assistant', 'relationship', %s, %s, %s, %s, %s, NULL, %s::jsonb)
                ON CONFLICT DO NOTHING
                """,
                (
                    p["relationship_type"], p["from_id"], p["to_id"],
                    by_id[p["from_id"]]["name"], by_id[p["to_id"]]["name"],
                    json.dumps(dict(contribution, signals=["assistant"])),
                ),
            )
            created += cur.rowcount

    audit.record(
        "suggestion.propose", user=user, object_type="suggestion", object_id=None,
        object_label=payload.instruction[:120],
        detail={"considered": len(rows), "created": created,
                "annotated": annotated, "model": ollama.model},
    )
    return {
        "created": created, "annotated": annotated, "considered": len(rows),
        "message": _outcome_message(created, annotated),
    }


def _outcome_message(created: int, annotated: int) -> str:
    tail = ("Nothing has been changed in the case file — accept or dismiss them "
            "in the review queue.")
    if created and annotated:
        return (f"{created} new proposal(s), and {annotated} suggestion(s) already "
                f"in the queue gained the assistant's reasoning. {tail}")
    if created:
        return f"{created} proposal(s) added to the review queue. {tail}"
    if annotated:
        return (f"Nothing new — the {annotated} pair(s) the assistant picked out were "
                f"already waiting for review, so its reasoning was added to them. {tail}")
    return ("Nothing to add — everything the assistant proposed is already recorded "
            "or already in the queue.")


def _parse(raw: str, valid_ids: set) -> list[dict]:
    """Everything the model returned that survives being checked.

    format=json is not used here (the assistant's chat path deliberately does
    not set it), so the reply may be wrapped in prose or a code fence. This
    finds the object, and then discards anything referring to a record that was
    not in the list — which is the guard that stops a hallucinated id becoming
    a proposal about a record that does not exist.
    """
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        logger.warning("Assistant proposal was not parseable JSON")
        return []

    out, seen = [], set()
    for item in (data.get("relationships") or []):
        if not isinstance(item, dict):
            continue
        from_id = str(item.get("from_id") or "").strip()
        to_id = str(item.get("to_id") or "").strip()
        rel = str(item.get("relationship_type") or "").strip().lower().replace(" ", "_")
        if from_id not in valid_ids or to_id not in valid_ids or from_id == to_id:
            continue
        if not entities_module.RELATIONSHIP_TYPE_RE.match(rel):
            continue
        key = frozenset((from_id, to_id))
        if key in seen:
            continue
        seen.add(key)
        out.append({"from_id": from_id, "to_id": to_id, "relationship_type": rel,
                    "reason": str(item.get("reason") or "")[:400]})
    return out
