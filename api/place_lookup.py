"""What is at this point on the map.

Forward geocoding — address to coordinates — has been here since early on and
runs unattended in the worker. This is the other direction, and it is not the
same job at all: it happens because somebody just clicked, they are watching,
and the answer is a suggestion they will accept or reject rather than a fact
the app writes down.

TWO QUESTIONS, TWO SERVICES

"What is the address here" and "what is the business here" are different
lookups and only the first is reliable.

  * Nominatim's /reverse answers the first. Street address, and the name of
    whichever single feature it matched — often the building, sometimes the
    tenant, sometimes nothing.
  * Overpass answers the second properly: every named place within a radius,
    which is what you want when the question is "which of the four units in
    that strip is it".

Both are optional and both degrade quietly. An instance running off map packs
with no internet still drops pins; it just gets coordinates and says so.

NOTHING HERE IS TREATED AS TRUE

Every result is a candidate with a distance attached, and the analyst picks
one or none. A reverse geocode two doors down the street looks exactly like a
correct one, so the app does not get to decide — it shows how far away the
match actually was and lets a person judge.
"""

import json
import logging
import math
import os
import time
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import audit
import auth
from db import db_cursor
from geo import maidenhead_locator
from idgen import generate_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/map", tags=["place-lookup"])

# Shared with the worker's forward geocoder on purpose: one Nominatim base
# URL, one User-Agent, one rate limit to respect.
GEOCODE_ENABLED = os.environ.get("GEOCODE_ENABLED", "true").lower() == "true"
NOMINATIM_BASE_URL = os.environ.get("NOMINATIM_BASE_URL",
                                    "https://nominatim.openstreetmap.org").rstrip("/")
GEOCODE_USER_AGENT = os.environ.get("GEOCODE_USER_AGENT",
                                    "humint-platform (set GEOCODE_USER_AGENT in .env)")
GEOCODE_TIMEOUT_SECONDS = int(os.environ.get("GEOCODE_TIMEOUT_SECONDS", "10"))

OVERPASS_ENABLED = os.environ.get("OVERPASS_ENABLED", "true").lower() == "true"
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OVERPASS_TIMEOUT_SECONDS = int(os.environ.get("OVERPASS_TIMEOUT_SECONDS", "25"))

# How far out to look for named places. 120m is about a block: far enough to
# catch the unit next door when the pin landed on the wrong half of a
# building, close enough that the list is still about one place.
DEFAULT_RADIUS_M = int(os.environ.get("PLACE_LOOKUP_RADIUS_M", "120"))
MAX_RADIUS_M = 1000
MAX_PLACES = 25

# A Location already in the file this close to the pin is probably the same
# building. Worth saying before somebody makes a second record for it.
NEARBY_RECORD_M = 50

# Both services are public and shared. This is one interactive lookup at a
# time, which is well inside anybody's etiquette, but two analysts clicking at
# once should still not turn into a burst.
_last_call_at = {"nominatim": 0.0, "overpass": 0.0}
MIN_INTERVAL_SECONDS = float(os.environ.get("PLACE_LOOKUP_MIN_INTERVAL_SECONDS", "1.1"))


def _throttle(service: str) -> None:
    wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call_at[service])
    if wait > 0:
        time.sleep(wait)
    _last_call_at[service] = time.monotonic()


def _metres_between(lat1, lng1, lat2, lng2) -> float:
    """Haversine. Good to a metre or two at these distances, which is far
    better than the coordinates themselves deserve."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# The address
# ---------------------------------------------------------------------------

def reverse_geocode(lat: float, lng: float) -> tuple[dict | None, str | None]:
    """(result, error). Never raises."""
    if not GEOCODE_ENABLED:
        return None, "Geocoding is switched off (GEOCODE_ENABLED)."
    params = {"lat": f"{lat:.7f}", "lon": f"{lng:.7f}", "format": "jsonv2",
              # 18 is building level. Asking for the building rather than the
              # street is the difference between "12 Quay Road" and "Quay Road".
              "zoom": 18, "addressdetails": 1}
    try:
        _throttle("nominatim")
        resp = requests.get(f"{NOMINATIM_BASE_URL}/reverse?{urlencode(params)}",
                            headers={"User-Agent": GEOCODE_USER_AGENT},
                            timeout=GEOCODE_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return None, f"The address service could not be reached: {type(exc).__name__}."
    if not data or data.get("error"):
        return None, "No address is recorded at that point."

    addr = data.get("address") or {}
    # Nominatim's display_name is the whole chain down to the country, which
    # is too much for an address field. Build the postal-ish part and keep the
    # full string alongside for the analyst to see.
    house = addr.get("house_number")
    road = addr.get("road")
    line1 = " ".join(x for x in (house, road) if x) or None
    town = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("hamlet") or addr.get("suburb"))
    region = addr.get("state") or addr.get("county")
    postcode = addr.get("postcode")
    tidy = ", ".join(x for x in (line1, town, region, postcode) if x) or None

    try:
        mlat, mlng = float(data["lat"]), float(data["lon"])
        distance = round(_metres_between(lat, lng, mlat, mlng))
    except (KeyError, TypeError, ValueError):
        mlat = mlng = None
        distance = None

    return {
        "address": tidy,
        "display_name": data.get("display_name"),
        "name": data.get("name") or None,
        "category": data.get("category"),
        "type": data.get("type"),
        "lat": mlat, "lng": mlng,
        "distance_m": distance,
        "postcode": postcode,
        "town": town,
    }, None


# ---------------------------------------------------------------------------
# The places
# ---------------------------------------------------------------------------

# Named things a person would call "a place". Deliberately not every tagged
# object: a bench and a street lamp have names in OSM sometimes, and a list
# with those in it is a list nobody reads.
_OVERPASS_QUERY = """
[out:json][timeout:{timeout}];
(
  nwr(around:{radius},{lat},{lng})["name"]["amenity"];
  nwr(around:{radius},{lat},{lng})["name"]["shop"];
  nwr(around:{radius},{lat},{lng})["name"]["office"];
  nwr(around:{radius},{lat},{lng})["name"]["tourism"];
  nwr(around:{radius},{lat},{lng})["name"]["leisure"];
  nwr(around:{radius},{lat},{lng})["name"]["healthcare"];
  nwr(around:{radius},{lat},{lng})["name"]["craft"];
  nwr(around:{radius},{lat},{lng})["name"]["industrial"];
  nwr(around:{radius},{lat},{lng})["name"]["building"~"^(commercial|retail|industrial|office|warehouse|public|civic)$"];
);
out center tags {maxsize};
"""


def _place_kind(tags: dict) -> str | None:
    for key in ("amenity", "shop", "office", "tourism", "leisure",
                "healthcare", "craft", "industrial", "building"):
        if tags.get(key) and tags[key] != "yes":
            return f"{tags[key]}".replace("_", " ")
    return None


def nearby_places(lat: float, lng: float, radius: int) -> tuple[list, str | None]:
    """(places, error). Never raises."""
    if not OVERPASS_ENABLED:
        return [], "Place lookup is switched off (OVERPASS_ENABLED)."
    query = _OVERPASS_QUERY.format(timeout=OVERPASS_TIMEOUT_SECONDS, radius=radius,
                                   lat=f"{lat:.7f}", lng=f"{lng:.7f}",
                                   maxsize=MAX_PLACES * 4)
    try:
        _throttle("overpass")
        resp = requests.post(OVERPASS_URL, data={"data": query},
                             headers={"User-Agent": GEOCODE_USER_AGENT},
                             timeout=OVERPASS_TIMEOUT_SECONDS + 5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return [], f"The place service could not be reached: {type(exc).__name__}."

    out = []
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        centre = element.get("center") or {"lat": element.get("lat"), "lon": element.get("lon")}
        try:
            plat, plng = float(centre["lat"]), float(centre["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        house = tags.get("addr:housenumber")
        road = tags.get("addr:street")
        out.append({
            "name": name,
            "kind": _place_kind(tags),
            "lat": plat, "lng": plng,
            "distance_m": round(_metres_between(lat, lng, plat, plng)),
            "address": " ".join(x for x in (house, road) if x) or None,
            "phone": tags.get("phone") or tags.get("contact:phone"),
            "website": tags.get("website") or tags.get("contact:website"),
            "operator": tags.get("operator"),
            "osm": f"{element.get('type')}/{element.get('id')}",
        })
    out.sort(key=lambda p: p["distance_m"])
    return out[:MAX_PLACES], None


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

@router.get("/what-is-here")
def what_is_here(lat: float = Query(..., ge=-90, le=90),
                 lng: float = Query(..., ge=-180, le=180),
                 radius: int = Query(default=DEFAULT_RADIUS_M, ge=10, le=MAX_RADIUS_M),
                 user: dict = Depends(auth.require_user)):
    """Address, named places, and any Location already recorded near this point.

    The last part is the one that stops the file filling with three records
    for the same warehouse. Everything else here is a suggestion from a public
    dataset; that is the app's own knowledge and it is worth more.
    """
    address, address_error = reverse_geocode(lat, lng)
    places, places_error = nearby_places(lat, lng, radius)

    # Locations already in the file, within a generous box then filtered
    # properly — a bounding box is what an index can help with, the haversine
    # is what gives the right answer.
    deg = (radius + NEARBY_RECORD_M) / 111_320.0
    with db_cursor() as cur:
        cur.execute(
            "SELECT e.id, e.name, d.address, d.lat, d.lng, e.is_active "
            "  FROM entities e JOIN location_details d ON d.entity_id = e.id "
            " WHERE d.lat BETWEEN %s AND %s AND d.lng BETWEEN %s AND %s",
            (lat - deg, lat + deg, lng - deg / max(math.cos(math.radians(lat)), 0.01),
             lng + deg / max(math.cos(math.radians(lat)), 0.01)))
        existing = []
        for eid, name, addr, elat, elng, active in cur.fetchall():
            if elat is None or elng is None:
                continue
            d = round(_metres_between(lat, lng, float(elat), float(elng)))
            if d <= max(radius, NEARBY_RECORD_M):
                existing.append({"id": eid, "name": name, "address": addr,
                                 "distance_m": d, "is_active": active})
        existing.sort(key=lambda r: r["distance_m"])

    audit.record("map.lookup", user=user, object_type="map", object_id=None,
                 object_label=f"{lat:.5f},{lng:.5f}",
                 detail={"address_found": bool(address), "places": len(places),
                         "radius_m": radius})

    return {
        "lat": lat, "lng": lng,
        "grid": maidenhead_locator(lat, lng),
        "address": address,
        "address_error": address_error,
        "places": places,
        "places_error": places_error,
        "existing": existing,
        # So the page can say "this one is already recorded" rather than
        # offering to create a duplicate.
        "duplicate_within_m": NEARBY_RECORD_M,
        "radius_m": radius,
    }


class ApplyRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    name: str = Field(..., min_length=1, max_length=300)
    address: str | None = None
    # Set to fill in an existing Location instead of creating one.
    entity_id: str | None = None
    # Where the suggestion came from, kept in the description so nobody later
    # mistakes a dataset guess for something an analyst established.
    provenance: str | None = None


@router.post("/place-to-location", status_code=201)
def place_to_location(payload: ApplyRequest, user: dict = Depends(auth.require_user)):
    """Turn a picked result into a Location, or write it onto an existing one."""
    note = payload.provenance or "Placed from the map."
    with db_cursor(commit=True) as cur:
        if payload.entity_id:
            cur.execute("SELECT entity_type, name FROM entities WHERE id = %s",
                        (payload.entity_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="That record doesn't exist.")
            if row[0] != "location":
                raise HTTPException(status_code=400,
                                    detail="Only a Location can take an address from the map.")
            cur.execute(
                "UPDATE location_details SET address = COALESCE(%s, address), "
                "       lat = %s, lng = %s WHERE entity_id = %s",
                (payload.address, payload.lat, payload.lng, payload.entity_id))
            entity_id, created = payload.entity_id, False
        else:
            entity_id = generate_id("location", payload.name)
            cur.execute(
                "INSERT INTO entities (id, entity_type, name, description, created_by) "
                "VALUES (%s, 'location', %s, %s, %s)",
                (entity_id, payload.name.strip(), note, user["id"]))
            cur.execute(
                "INSERT INTO location_details (entity_id, address, lat, lng) "
                "VALUES (%s, %s, %s, %s)",
                (entity_id, payload.address, payload.lat, payload.lng))
            created = True

        # Coordinates came straight from the map, so the grid follows and
        # there is nothing to geocode — marking it done stops the worker
        # queuing a lookup for an address it already has a position for.
        cur.execute(
            "UPDATE location_details SET maidenhead_grid = %s, geocode_status = 'done', "
            "       geocode_error = NULL WHERE entity_id = %s",
            (maidenhead_locator(payload.lat, payload.lng), entity_id))
        cur.execute("UPDATE entities SET updated_at = now() WHERE id = %s", (entity_id,))

    audit.record("entity.create" if created else "entity.update", user=user,
                 object_type="entity", object_id=entity_id,
                 object_label=payload.name.strip(),
                 detail={"entity_type": "location", "source": "map lookup",
                         "address": payload.address, "provenance": note})
    return {"id": entity_id, "created": created}
