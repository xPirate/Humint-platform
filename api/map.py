"""Map view support: just one endpoint, returning every active Location
entity that actually has coordinates set, for plotting as markers.

Each marker carries its Location's `environment`, because the colour of a pin
is the fastest thing on the page to read. It uses the same scale zones do, so
a permissive pin inside a non-permissive zone is a contradiction you can see
rather than one you have to click twice to find.

A Location entity with no lat/lng is still a perfectly usable entity
everywhere else in the app (you can attach reports/relationships/attachments
to it using only a free-text address) — it just can't be placed on a map, so
it's silently excluded here rather than the map endpoint erroring or the
frontend having to special-case a marker with no position.

What each marker's popup shows (name, address, and — the actual point of
this feature — which other entities, especially Events, are related to this
location) comes from the existing GET /api/entities/{id} endpoint, fetched
on demand when a marker is clicked. That endpoint already returns the full
relationship list; duplicating it here would just be two sources of truth
for the same data.
"""

from fastapi import APIRouter, Depends

import auth
from db import db_cursor

router = APIRouter(prefix="/api", tags=["map"])


@router.get("/map/locations")
def map_locations(user: dict = Depends(auth.require_user)):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.name, e.description, ld.address, ld.lat, ld.lng,
                   ld.environment
            FROM entities e JOIN location_details ld ON ld.entity_id = e.id
            WHERE e.is_active = TRUE AND ld.lat IS NOT NULL AND ld.lng IS NOT NULL
            ORDER BY e.name
            """
        )
        rows = cur.fetchall()

    return {
        "items": [
            {
                "id": r[0], "name": r[1], "description": r[2],
                "address": r[3], "lat": r[4], "lng": r[5],
                # Drives the marker's colour. A Location nobody has assessed
                # comes back None and is drawn neutral -- which is the honest
                # rendering of "no assessment", not of "fine".
                "environment": r[6],
            }
            for r in rows
        ]
    }
