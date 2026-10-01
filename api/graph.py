"""The whole case file as one picture.

WHY THIS EXISTS

The Entities page used to be a wall of cards. That reads fine at thirty
records and not at all at three hundred: every record gets the same visual
weight, so the one that forty things point at looks exactly like the one
somebody typed in yesterday and never used again. The list answers "what is
in here"; it has never answered "what matters in here".

This endpoint answers the second question. One query returns every entity
the list view would show, each with the number of relationships attached to
it, plus the edges between them — enough for the frontend to draw the file
as a network where the heavily-connected records are obvious at a glance.

WHAT THE NUMBERS MEAN

`degree` is every relationship the record has, counted against the whole
database rather than against whatever is on screen. If you filter down to
Organizations, a company still shows the twelve people attached to it even
though none of them is drawn. The alternative — recounting within the
filtered set — makes the same record change colour depending on what you
happened to be looking at, which is worse than useless for judging
importance.

`edges` is the opposite: only relationships whose BOTH ends are in the
returned node set, because an edge to something that is not drawn has
nowhere to land.

SIZE

Capped, and honest about it. A force simulation over a few hundred nodes is
comfortable in a browser; over ten thousand it is a hang. The cap is applied
by degree — if something has to be left out, it should be the isolated
records rather than the hubs, which are the whole point of the picture.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import auth
import entities
from db import db_cursor

router = APIRouter(prefix="/api", tags=["graph"])

ENTITY_TYPES = ("person", "organization", "location", "event", "source", "communication",
                "vehicle", "record")

# Beyond this the simulation stops being interactive on modest hardware --
# this app is meant to run on a Raspberry Pi and be used from a laptop.
MAX_GRAPH_NODES = 400


@router.get("/entities/graph")
def entity_graph(
    entity_type: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    active_only: bool = Query(default=True),
    hide_expired: bool = Query(default=False),
    limit: int = Query(default=MAX_GRAPH_NODES, ge=1, le=MAX_GRAPH_NODES),
    user: dict = Depends(auth.require_user),
):
    if entity_type is not None and entity_type not in ENTITY_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"entity_type must be one of {list(ENTITY_TYPES)}")

    where = ["e.merged_into IS NULL"]
    params: list = []
    if entity_type:
        where.append("e.entity_type = %s")
        params.append(entity_type)
    if q:
        where.append("e.name ILIKE %s")
        params.append(f"%{q}%")
    if active_only:
        where.append("e.is_active = TRUE")
    if hide_expired:
        where.append(
            "(e.entity_type <> 'event' OR e.id IN ("
            "  SELECT entity_id FROM event_details"
            "  WHERE expires_at IS NULL OR expires_at >= CURRENT_DATE))")
    where_clause = "WHERE " + " AND ".join(where)

    with db_cursor() as cur:
        # One pass, not one query per entity. The degree subquery counts both
        # directions in a single scan of relationships rather than joining
        # twice and deduplicating afterwards.
        cur.execute(
            f"""
            WITH degree AS (
                SELECT entity_id, count(*) AS n FROM (
                    SELECT from_entity_id AS entity_id FROM relationships
                    UNION ALL
                    SELECT to_entity_id AS entity_id FROM relationships
                ) both_ends
                GROUP BY entity_id
            )
            SELECT e.id, e.name, e.entity_type, e.is_active,
                   COALESCE(d.n, 0) AS degree
              FROM entities e
              LEFT JOIN degree d ON d.entity_id = e.id
            {where_clause}
             ORDER BY COALESCE(d.n, 0) DESC, e.name, e.id
             LIMIT %s
            """,
            [*params, limit])
        nodes = [{"id": r[0], "name": r[1], "entity_type": r[2],
                  "is_active": r[3], "degree": r[4]} for r in cur.fetchall()]
        # So the network can mark a hostile record the same way every other
        # view does. One query for the whole node set, not one per node.
        alignments = entities.fetch_alignments(cur, [n["id"] for n in nodes])
        for node in nodes:
            node["alignment"] = alignments.get(node["id"])

        cur.execute(f"SELECT count(*) FROM entities e {where_clause}", params)
        total = cur.fetchone()[0]

        ids = [n["id"] for n in nodes]
        edges = []
        if ids:
            cur.execute(
                """
                SELECT id, from_entity_id, to_entity_id, relationship_type, confidence
                  FROM relationships
                 WHERE from_entity_id = ANY(%s) AND to_entity_id = ANY(%s)
                 ORDER BY id
                """,
                (ids, ids))
            edges = [{"id": r[0], "from": r[1], "to": r[2],
                      "relationship_type": r[3], "confidence": r[4]}
                     for r in cur.fetchall()]

    return {
        "nodes": nodes,
        "edges": edges,
        "total": total,
        # So the frontend can say "showing the 400 most connected of 900"
        # rather than quietly drawing a subset of the case file.
        "truncated": total > len(nodes),
        "max_degree": max((n["degree"] for n in nodes), default=0),
    }
