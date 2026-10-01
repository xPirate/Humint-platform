"""Map zones: an area, an assessment of it, and a clock.

The NOTAM idea. Somewhere is different from normal, for a while, and anyone
opening the map should see that without reading a report first — a protest, a
cordon, a stretch of road nobody should be on tonight.

Three things make this more than a coloured shape:

  * **It is not a Location.** A Location is a place that exists. A zone is a
    judgement about ground over a period, and the same ground can carry
    several at once. Storing it on the Location record would mean editing the
    town to say its square was briefly dangerous.

  * **It changes, and the changes are the point.** A protest that goes
    semi-permissive to non-permissive at 18:40 is the finding. Every change is
    kept as case material on the zone itself, not only in the audit log —
    an analyst should not have to ask an admin to read their own timeline.

  * **Expiry does not delete.** An expired zone stops being drawn as live and
    stays exactly where it was, still attached to its Event and to every
    report written while it was running. "Where was the cordon that night" is
    asked weeks later.

Geometry is GeoJSON in JSONB and point-in-polygon is done here, in Python,
rather than by adding PostGIS. The ray-casting test below is twenty lines and
runs against a bounding-box shortlist; an extension would put a migration
between a user and `docker compose up` for arithmetic this app can do itself.
"""

import json
import math
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import audit
import auth
import entities as entities_module
from db import db_cursor
from idgen import generate_id

router = APIRouter(prefix="/api", tags=["zones"])

# A zone is a judgement about ground, so it uses the same words a Location
# does. Imported rather than redefined: two lists that must agree and are
# written down twice eventually disagree.
ENVIRONMENT_VALUES = entities_module.ENVIRONMENT_VALUES

SHAPES = ("polygon", "rectangle", "circle")

# A drawn ring with more points than this is either a mistake or a traced
# coastline; either way it is not something to store in a JSONB column and
# re-check on every containment test.
MAX_RING_POINTS = 2000
MAX_RADIUS_M = 500_000.0

_ZONE_COLS = ["id", "name", "environment", "shape", "geometry", "radius_m",
              "min_lat", "min_lon", "max_lat", "max_lon", "event_id",
              "valid_from", "valid_until", "notes", "created_by",
              "created_at", "updated_at"]


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

class ZoneGeometry(BaseModel):
    """What the map sends: a ring of points, or a centre and a radius."""
    shape: str
    # [[lat, lon], ...] in drawing order. Leaflet's order, not GeoJSON's, and
    # converted on the way in — asking the frontend to swap coordinates is how
    # you get a zone in the Indian Ocean.
    points: Optional[list] = None
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    radius_m: Optional[float] = Field(default=None, gt=0, le=MAX_RADIUS_M)


def _validate_ring(points) -> list:
    if not isinstance(points, list) or len(points) < 3:
        raise HTTPException(status_code=400,
                            detail="A zone needs at least three points")
    if len(points) > MAX_RING_POINTS:
        raise HTTPException(status_code=400,
                            detail=f"That shape has {len(points)} points, over the "
                                   f"{MAX_RING_POINTS} limit")
    ring = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise HTTPException(status_code=400,
                                detail="Each point must be [latitude, longitude]")
        try:
            lat, lon = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Points must be numbers")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(status_code=400, detail="Point is off the map")
        ring.append((lat, lon))
    return ring


def _geometry_from(payload: ZoneGeometry) -> tuple:
    """(geojson, radius_m, bounds) for a drawn shape."""
    if payload.shape not in SHAPES:
        raise HTTPException(status_code=400,
                            detail="shape must be one of: " + ", ".join(SHAPES))

    if payload.shape == "circle":
        if payload.lat is None or payload.lon is None or not payload.radius_m:
            raise HTTPException(status_code=400,
                                detail="A circle needs a centre and a radius")
        # Degrees of latitude are ~constant; degrees of longitude shrink with
        # the cosine of latitude. Using one figure for both would make the
        # bounding box of a circle near the poles far too narrow and lose
        # zones that genuinely contain a point.
        lat_pad = payload.radius_m / 111_320.0
        lon_scale = max(math.cos(math.radians(payload.lat)), 1e-6)
        lon_pad = payload.radius_m / (111_320.0 * lon_scale)
        geojson = {"type": "Point", "coordinates": [payload.lon, payload.lat]}
        bounds = (payload.lat - lat_pad, payload.lon - lon_pad,
                  payload.lat + lat_pad, payload.lon + lon_pad)
        return geojson, float(payload.radius_m), bounds

    ring = _validate_ring(payload.points)
    lats = [p[0] for p in ring]
    lons = [p[1] for p in ring]
    # GeoJSON is [longitude, latitude] and closes its rings; Leaflet is
    # [latitude, longitude] and does not. Both conventions are correct and
    # neither is negotiable, so the conversion lives in exactly this one place.
    coords = [[lon, lat] for lat, lon in ring]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    geojson = {"type": "Polygon", "coordinates": [coords]}
    return geojson, None, (min(lats), min(lons), max(lats), max(lons))


# About a centimetre at the equator. Used only to decide that a point sitting
# exactly on a drawn boundary is inside it.
_EDGE_EPSILON = 1e-7


def _on_segment(lat, lon, x1, y1, x2, y2) -> bool:
    """Is the point on this edge, to within a hair?

    Ray casting gives an arbitrary answer for a point exactly on the boundary,
    and "arbitrary" is not something to hand an analyst asking whether an
    address is inside a cordon. It happens for real: somebody drops a zone
    corner on a junction and later records a Location at the same junction.
    On the line counts as inside.
    """
    cross = (lon - x1) * (y2 - y1) - (lat - y1) * (x2 - x1)
    if abs(cross) > _EDGE_EPSILON:
        return False
    # Collinear with the edge -- now check it is actually between the ends
    # rather than out along the same line.
    return (min(x1, x2) - _EDGE_EPSILON <= lon <= max(x1, x2) + _EDGE_EPSILON
            and min(y1, y2) - _EDGE_EPSILON <= lat <= max(y1, y2) + _EDGE_EPSILON)


def _point_in_ring(lat: float, lon: float, ring: list) -> bool:
    """Ray casting, with the boundary counted as inside.

    `ring` is GeoJSON order, [[lon, lat], ...]. A horizontal ray runs east
    from the point and crossings of the ring are counted; odd means inside.
    The `(y1 > lat) != (y2 > lat)` test is what stops a vertex exactly level
    with the ray being counted twice — the classic way this algorithm is got
    wrong.
    """
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        if _on_segment(lat, lon, x1, y1, x2, y2):
            return True
        if (y1 > lat) != (y2 > lat):
            x_at = x1 + (lat - y1) * (x2 - x1) / ((y2 - y1) or 1e-12)
            if lon < x_at:
                inside = not inside
    return inside


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def zone_contains(zone: dict, lat: float, lon: float) -> bool:
    """Is this point inside this zone? The bounding box is checked first."""
    if not (zone["min_lat"] <= lat <= zone["max_lat"]
            and zone["min_lon"] <= lon <= zone["max_lon"]):
        return False
    geometry = zone["geometry"]
    if isinstance(geometry, str):
        geometry = json.loads(geometry)
    if zone["shape"] == "circle":
        centre = geometry["coordinates"]
        return _haversine_m(lat, lon, centre[1], centre[0]) <= (zone["radius_m"] or 0)
    return _point_in_ring(lat, lon, geometry["coordinates"][0])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ZoneCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    environment: str
    geometry: ZoneGeometry
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=4000)
    # Attach to an Event that already exists...
    event_id: Optional[str] = None
    # ...or make one as part of drawing the zone. A protest is an Event; this
    # saves the analyst leaving the map to say so, and it is what makes the
    # reports written during the zone's life findable afterwards.
    create_event: Optional[str] = Field(default=None, max_length=200)


class ZoneUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    environment: Optional[str] = None
    geometry: Optional[ZoneGeometry] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=4000)
    event_id: Optional[str] = None
    # Why it changed. Kept against the change, not the zone: "police line
    # moved" explains one transition, not the zone's whole existence.
    change_note: Optional[str] = Field(default=None, max_length=500)


def _validate_environment(value: str) -> None:
    if value not in ENVIRONMENT_VALUES:
        raise HTTPException(status_code=400,
                            detail="environment must be one of: " + ", ".join(ENVIRONMENT_VALUES))


def _row_to_zone(row) -> dict:
    zone = dict(zip(_ZONE_COLS, row))
    if isinstance(zone.get("geometry"), str):
        zone["geometry"] = json.loads(zone["geometry"])
    return zone


def _fetch_zone(cur, zone_id: int, for_update: bool = False) -> dict:
    cur.execute(f"SELECT {', '.join(_ZONE_COLS)} FROM map_zones WHERE id = %s"
                + (" FOR UPDATE" if for_update else ""), (zone_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Zone not found")
    return _row_to_zone(row)


def _event_label(cur, event_id):
    if not event_id:
        return None
    cur.execute("SELECT name FROM entities WHERE id = %s AND entity_type = 'event'",
                (event_id,))
    row = cur.fetchone()
    return row[0] if row else None


def _resolve_event(cur, user: dict, event_id, create_event, valid_from, valid_until):
    """Return the Event id this zone hangs off, making one if asked.

    A zone created with an Event gets that Event's dates from the zone's own
    clock, so "relevant until" on the dashboard and the zone's expiry are the
    same fact rather than two that can drift.
    """
    if event_id:
        cur.execute("SELECT entity_type FROM entities WHERE id = %s", (event_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No entity '{event_id}'")
        if row[0] != "event":
            raise HTTPException(status_code=400,
                                detail="A zone can only be linked to an Event")
        return event_id, False

    if not create_event:
        return None, False

    name = create_event.strip()
    if not name:
        return None, False
    new_id = generate_id("event", name)
    cur.execute(
        "INSERT INTO entities (id, entity_type, name, description, created_by) "
        "VALUES (%s, 'event', %s, %s, %s)",
        (new_id, name, "Created from a zone drawn on the map.", user["id"]))
    details = entities_module._parsed_details("event", {
        "started_at": valid_from,
        "ended_at": valid_until,
        # "Relevant until" is a date, and the zone's clock is a timestamp.
        # Taking the date part is right: an Event stops being worth knowing on
        # a day, not at a minute.
        "expires_at": valid_until[:10] if valid_until else None,
    })
    entities_module._insert_details(cur, "event", new_id, details)
    return new_id, True


def _record_change(cur, zone_id: int, environment: str, previous, note, user) -> None:
    cur.execute(
        "INSERT INTO map_zone_changes (zone_id, environment, previous_environment, "
        "note, changed_by) VALUES (%s, %s, %s, %s, %s)",
        (zone_id, environment, previous, (note or "").strip() or None, user["id"]))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/map/zones")
def list_zones(include_expired: bool = True, user: dict = Depends(auth.require_user)):
    """Every zone, newest first.

    Expired ones are included by default and flagged rather than filtered:
    they are the record of what the ground was like, and the map dims them
    instead of dropping them.
    """
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join('z.' + c for c in _ZONE_COLS)},
                   e.name,
                   (z.valid_until IS NOT NULL AND z.valid_until <= now()) AS expired,
                   (SELECT COUNT(*) FROM map_zone_changes c WHERE c.zone_id = z.id)
            FROM map_zones z
            LEFT JOIN entities e ON e.id = z.event_id
            {"" if include_expired else "WHERE z.valid_until IS NULL OR z.valid_until > now()"}
            ORDER BY z.created_at DESC, z.id DESC
            """
        )
        rows = cur.fetchall()
    items = []
    for r in rows:
        zone = _row_to_zone(r[: len(_ZONE_COLS)])
        zone["event_name"] = r[len(_ZONE_COLS)]
        zone["expired"] = bool(r[len(_ZONE_COLS) + 1])
        zone["change_count"] = r[len(_ZONE_COLS) + 2]
        items.append(zone)
    return {"items": items}


@router.get("/map/zones/containing/{entity_id}")
def zones_containing(entity_id: str, user: dict = Depends(auth.require_user)):
    """Which zones cover this Location. Used on a record's own page.

    Expired zones are returned too, flagged: "this address was inside the
    cordon that night" is exactly the kind of thing worth knowing when reading
    a record months later.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT lat, lng FROM location_details WHERE entity_id = %s", (entity_id,))
        row = cur.fetchone()
        if row is None or row[0] is None or row[1] is None:
            return {"items": []}
        lat, lng = row[0], row[1]
        cur.execute(
            f"""
            SELECT {', '.join('z.' + c for c in _ZONE_COLS)}, e.name,
                   (z.valid_until IS NOT NULL AND z.valid_until <= now()) AS expired
            FROM map_zones z LEFT JOIN entities e ON e.id = z.event_id
            WHERE %s BETWEEN z.min_lat AND z.max_lat
              AND %s BETWEEN z.min_lon AND z.max_lon
            ORDER BY z.created_at DESC, z.id DESC
            """, (lat, lng))
        rows = cur.fetchall()

    items = []
    for r in rows:
        zone = _row_to_zone(r[: len(_ZONE_COLS)])
        if not zone_contains(zone, lat, lng):
            continue
        zone["event_name"] = r[len(_ZONE_COLS)]
        zone["expired"] = bool(r[len(_ZONE_COLS) + 1])
        items.append(zone)
    return {"items": items}


@router.get("/map/zones/{zone_id}")
def get_zone(zone_id: int, user: dict = Depends(auth.require_user)):
    """One zone, its timeline, and what of yours is inside it."""
    with db_cursor() as cur:
        zone = _fetch_zone(cur, zone_id)
        zone["event_name"] = _event_label(cur, zone["event_id"])

        cur.execute(
            """
            SELECT c.environment, c.previous_environment, c.note, c.changed_at, u.username
            FROM map_zone_changes c LEFT JOIN users u ON u.id = c.changed_by
            WHERE c.zone_id = %s
            ORDER BY c.changed_at DESC, c.id DESC
            """, (zone_id,))
        zone["history"] = [
            {"environment": r[0], "previous_environment": r[1], "note": r[2],
             "changed_at": r[3], "changed_by": r[4]}
            for r in cur.fetchall()
        ]

        # Bounding box first, in SQL and on an index; the exact test only runs
        # on what survives it. Archived records are left out -- the question is
        # what is in there now, not what once was.
        cur.execute(
            """
            SELECT e.id, e.name, ld.address, ld.lat, ld.lng, ld.environment
            FROM entities e JOIN location_details ld ON ld.entity_id = e.id
            WHERE e.is_active = TRUE AND e.merged_into IS NULL
              AND ld.lat IS NOT NULL AND ld.lng IS NOT NULL
              AND ld.lat BETWEEN %s AND %s AND ld.lng BETWEEN %s AND %s
            ORDER BY e.name
            """,
            (zone["min_lat"], zone["max_lat"], zone["min_lon"], zone["max_lon"]))
        candidates = cur.fetchall()

    zone["locations_inside"] = [
        {"id": r[0], "name": r[1], "address": r[2], "lat": r[3], "lng": r[4],
         "environment": r[5]}
        for r in candidates if zone_contains(zone, r[3], r[4])
    ]
    return zone


@router.post("/map/zones", status_code=201)
def create_zone(payload: ZoneCreate, user: dict = Depends(auth.require_user)):
    _validate_environment(payload.environment)
    geojson, radius_m, bounds = _geometry_from(payload.geometry)

    with db_cursor(commit=True) as cur:
        event_id, event_created = _resolve_event(
            cur, user, payload.event_id, payload.create_event,
            payload.valid_from, payload.valid_until)

        cur.execute(
            f"""
            INSERT INTO map_zones (name, environment, shape, geometry, radius_m,
                                   min_lat, min_lon, max_lat, max_lon, event_id,
                                   valid_from, valid_until, notes, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {', '.join(_ZONE_COLS)}
            """,
            (payload.name.strip(), payload.environment, payload.geometry.shape,
             json.dumps(geojson), radius_m, bounds[0], bounds[1], bounds[2], bounds[3],
             event_id, payload.valid_from or None, payload.valid_until or None,
             (payload.notes or "").strip() or None, user["id"]))
        zone = _row_to_zone(cur.fetchone())
        # The opening row of the timeline, so the history is complete rather
        # than starting at the first edit.
        _record_change(cur, zone["id"], payload.environment, None,
                       "Zone created", user)
        zone["event_name"] = _event_label(cur, event_id)

    audit.record("map_zone.create", user=user, object_type="map_zone",
                 object_id=zone["id"], object_label=zone["name"],
                 detail={"environment": payload.environment, "shape": payload.geometry.shape,
                         "event_id": event_id, "event_created": event_created,
                         "valid_until": payload.valid_until})
    zone["event_created"] = event_created
    return zone


@router.patch("/map/zones/{zone_id}")
def update_zone(zone_id: int, payload: ZoneUpdate, user: dict = Depends(auth.require_user)):
    """Change a zone. A changed environment writes a row on its timeline.

    This is the path that matters most: a protest going from semi-permissive
    to non-permissive is not a correction, it is the reporting. The previous
    value is kept alongside the new one so the timeline reads as transitions
    rather than as a list of states.
    """
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")
    if "environment" in fields:
        _validate_environment(fields["environment"])

    with db_cursor(commit=True) as cur:
        current = _fetch_zone(cur, zone_id, for_update=True)

        sets, values = [], []
        for key in ("name", "environment", "notes", "valid_from", "valid_until"):
            if key in fields:
                value = fields[key]
                sets.append(f"{key} = %s")
                values.append(value.strip() or None if isinstance(value, str) and key in
                              ("name", "notes") else (value or None))

        if payload.geometry is not None:
            geojson, radius_m, bounds = _geometry_from(payload.geometry)
            sets += ["shape = %s", "geometry = %s", "radius_m = %s",
                     "min_lat = %s", "min_lon = %s", "max_lat = %s", "max_lon = %s"]
            values += [payload.geometry.shape, json.dumps(geojson), radius_m,
                       bounds[0], bounds[1], bounds[2], bounds[3]]

        if "event_id" in fields:
            event_id, _created = _resolve_event(cur, user, fields["event_id"], None,
                                                current["valid_from"], current["valid_until"])
            sets.append("event_id = %s")
            values.append(event_id)

        sets.append("updated_at = now()")
        values.append(zone_id)
        cur.execute(f"UPDATE map_zones SET {', '.join(sets)} WHERE id = %s "
                    f"RETURNING {', '.join(_ZONE_COLS)}", values)
        zone = _row_to_zone(cur.fetchone())

        changed_env = ("environment" in fields
                       and fields["environment"] != current["environment"])
        if changed_env:
            _record_change(cur, zone_id, fields["environment"],
                           current["environment"], payload.change_note, user)
        zone["event_name"] = _event_label(cur, zone["event_id"])

    audit.record("map_zone.update", user=user, object_type="map_zone",
                 object_id=zone_id, object_label=zone["name"],
                 detail={"fields": sorted(fields.keys()),
                         "environment_from": current["environment"] if changed_env else None,
                         "environment_to": fields.get("environment") if changed_env else None})
    return zone


@router.delete("/map/zones/{zone_id}")
def delete_zone(zone_id: int, user: dict = Depends(auth.require_admin)):
    """Admin-only, and separate from expiry.

    Letting a zone expire is the normal end of its life and keeps it. Deleting
    one is for a zone drawn by mistake, and it takes the timeline with it --
    which is why it is not something an analyst can do to a record of what the
    ground was like on a night somebody wrote reports about.
    """
    with db_cursor(commit=True) as cur:
        zone = _fetch_zone(cur, zone_id, for_update=True)
        cur.execute("DELETE FROM map_zones WHERE id = %s", (zone_id,))

    audit.record("map_zone.delete", user=user, object_type="map_zone",
                 object_id=zone_id, object_label=zone["name"],
                 detail={"environment": zone["environment"], "event_id": zone["event_id"]})
    return {"deleted": True}
