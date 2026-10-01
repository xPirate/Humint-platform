"""Offline maps: tile sources, downloaded packs, and the tile server itself.

Three things live here because they are one feature:

  * **Sources** — an XYZ URL template plus its zoom range and attribution.
    Admin-managed, same as RSS feeds, and for the same reason: a source is a
    standing outbound connection this app will make, which is a decision
    about the deployment rather than about the case file.

  * **Packs** — an area of one source, downloaded into an MBTiles file so the
    map keeps working with no route out. The row is the job as well as the
    artifact; worker/map_download.py does the fetching.

  * **Tiles** — GET /api/map/tiles/... reads straight out of those MBTiles
    files. That is the whole "map server": MBTiles is a SQLite database with
    tile blobs in it, so serving one needs the standard library and nothing
    else. No tile-server container, no new dependency, no build step.

The licensing point is deliberately built into the schema rather than left to
the documentation. `allow_download` is separate from `is_active` because
viewing a source and scraping it wholesale are different permissions, and
most tile operators grant only the first -- OpenStreetMap's own tile usage
policy prohibits bulk download outright. The OSM source that ships with the
app therefore cannot be packed, and an admin adding a source has to assert
that they are entitled to cache it. What this app will not do is ship a list
of other people's tile servers and imply that downloading them is fine.
"""

import os
import re
import sqlite3
import xml.etree.ElementTree as ElementTree
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

import audit
import auth
from db import db_cursor
from tilemath import count_tiles, flip_y

router = APIRouter(prefix="/api", tags=["maps"])

# Where pack files live. A separate volume from uploads in the compose file:
# packs are large, reproducible from the source they came from, and have no
# business inflating a case-file backup.
MAP_PACK_DIR = os.environ.get("MAP_PACK_DIR", "/data/map-packs")

# A ceiling on one job, so a dragged box that happens to cover a continent is
# refused with a number rather than accepted and left running for a fortnight.
MAX_PACK_TILES = int(os.environ.get("MAP_PACK_MAX_TILES", "2000000"))

# Rough per-tile sizes for the pre-flight estimate. Real figures vary wildly
# with imagery vs. vector-rendered raster, so this is presented to the user as
# an approximation and never used for anything but that warning.
_ESTIMATED_TILE_BYTES = {"png": 18_000, "jpg": 14_000, "webp": 10_000}

_MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_SUBDOMAINS_RE = re.compile(r"^[A-Za-z0-9]{1,12}$")

_SOURCE_COLS = ["id", "name", "url_template", "subdomains", "min_zoom", "max_zoom",
                "tile_format", "attribution", "is_active", "allow_download",
                "is_builtin", "sort_order", "created_at"]

_PACK_COLS = ["id", "source_id", "name", "min_lat", "min_lon", "max_lat", "max_lon",
              "min_zoom", "max_zoom", "status", "tiles_total", "tiles_done",
              "tiles_failed", "bytes_total", "cancel_requested", "last_error",
              "created_at", "started_at", "finished_at"]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url_template: str = Field(min_length=1, max_length=2048)
    subdomains: Optional[str] = Field(default=None, max_length=12)
    min_zoom: int = Field(default=0, ge=0, le=22)
    max_zoom: int = Field(default=19, ge=0, le=22)
    tile_format: str = Field(default="png")
    attribution: Optional[str] = Field(default=None, max_length=512)
    allow_download: bool = False
    is_active: bool = True
    sort_order: int = Field(default=100, ge=0, le=9999)


class SourceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    url_template: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    subdomains: Optional[str] = Field(default=None, max_length=12)
    min_zoom: Optional[int] = Field(default=None, ge=0, le=22)
    max_zoom: Optional[int] = Field(default=None, ge=0, le=22)
    tile_format: Optional[str] = None
    attribution: Optional[str] = Field(default=None, max_length=512)
    allow_download: Optional[bool] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, ge=0, le=9999)


class SourceImport(BaseModel):
    """An ATAK/MOBAC map-source XML document, pasted in whole."""
    xml: str = Field(min_length=1, max_length=512_000)
    allow_download: bool = False


def _validate_source(url_template: str, subdomains, min_zoom: int, max_zoom: int,
                     tile_format: str) -> None:
    if not _URL_RE.match(url_template.strip()):
        raise HTTPException(status_code=400,
                            detail="url_template must start with http:// or https://")
    for token in ("{z}", "{x}", "{y}"):
        if token not in url_template:
            raise HTTPException(status_code=400,
                                detail=f"url_template is missing the {token} placeholder")
    if "{s}" in url_template and not (subdomains or "").strip():
        raise HTTPException(status_code=400,
                            detail="url_template uses {s}, so subdomains must be set "
                                   "(e.g. 'abc' for a, b and c)")
    if subdomains and not _SUBDOMAINS_RE.match(subdomains):
        raise HTTPException(status_code=400,
                            detail="subdomains must be letters/digits only, e.g. 'abc' or '123'")
    if tile_format not in _MEDIA_TYPES:
        raise HTTPException(status_code=400,
                            detail="tile_format must be one of: " + ", ".join(_MEDIA_TYPES))
    if max_zoom < min_zoom:
        raise HTTPException(status_code=400, detail="max_zoom cannot be below min_zoom")


def _row_to_source(row) -> dict:
    return dict(zip(_SOURCE_COLS, row))


def _fetch_source(cur, source_id: int, for_update: bool = False):
    cur.execute(f"SELECT {', '.join(_SOURCE_COLS)} FROM map_sources WHERE id = %s"
                + (" FOR UPDATE" if for_update else ""), (source_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Map source not found")
    return _row_to_source(row)


@router.get("/map/sources")
def list_sources(user: dict = Depends(auth.require_user)):
    """Every active source, for the layer switcher. Readable by any analyst:
    choosing a basemap is casework, not administration. Each source reports
    whether it has any usable offline coverage, so the switcher can mark the
    ones that will go blank when the machine loses its route out."""
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join('s.' + c for c in _SOURCE_COLS)},
                   COALESCE((SELECT COUNT(*) FROM map_packs p
                             WHERE p.source_id = s.id AND p.status IN ('done', 'downloading')), 0)
            FROM map_sources s
            WHERE s.is_active = TRUE
            ORDER BY s.sort_order, s.name
            """
        )
        rows = cur.fetchall()
    items = []
    for r in rows:
        src = _row_to_source(r[: len(_SOURCE_COLS)])
        src["pack_count"] = r[len(_SOURCE_COLS)]
        items.append(src)
    return {"items": items}


@router.get("/admin/map/sources")
def admin_list_sources(user: dict = Depends(auth.require_admin)):
    """Same list, but including deactivated ones -- an admin editing the
    registry needs to see what they switched off."""
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join('s.' + c for c in _SOURCE_COLS)},
                   COALESCE((SELECT COUNT(*) FROM map_packs p WHERE p.source_id = s.id), 0)
            FROM map_sources s ORDER BY s.sort_order, s.name
            """
        )
        rows = cur.fetchall()
    items = []
    for r in rows:
        src = _row_to_source(r[: len(_SOURCE_COLS)])
        src["pack_count"] = r[len(_SOURCE_COLS)]
        items.append(src)
    return {"items": items}


@router.post("/admin/map/sources", status_code=201)
def create_source(payload: SourceCreate, user: dict = Depends(auth.require_admin)):
    _validate_source(payload.url_template, payload.subdomains,
                     payload.min_zoom, payload.max_zoom, payload.tile_format)
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT 1 FROM map_sources WHERE lower(name) = lower(%s)", (payload.name,))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="A map source with that name already exists")
        cur.execute(
            f"""
            INSERT INTO map_sources (name, url_template, subdomains, min_zoom, max_zoom,
                                     tile_format, attribution, is_active, allow_download,
                                     sort_order, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {', '.join(_SOURCE_COLS)}
            """,
            (payload.name.strip(), payload.url_template.strip(),
             (payload.subdomains or "").strip() or None,
             payload.min_zoom, payload.max_zoom, payload.tile_format,
             (payload.attribution or "").strip() or None,
             payload.is_active, payload.allow_download, payload.sort_order, user["id"]),
        )
        source = _row_to_source(cur.fetchone())

    audit.record("map_source.create", user=user, object_type="map_source",
                 object_id=source["id"], object_label=source["name"],
                 detail={"allow_download": source["allow_download"]})
    return source


@router.patch("/admin/map/sources/{source_id}")
def update_source(source_id: int, payload: SourceUpdate,
                  user: dict = Depends(auth.require_admin)):
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")

    with db_cursor(commit=True) as cur:
        current = _fetch_source(cur, source_id, for_update=True)
        if current["is_builtin"] and "allow_download" in fields and fields["allow_download"]:
            # The shipped OSM source is the one thing an admin cannot simply
            # tick into being downloadable, because the reason it is not is a
            # licence term rather than a preference. Adding their own entry
            # for a tile server they are entitled to cache is one click away.
            raise HTTPException(
                status_code=409,
                detail="OpenStreetMap's policy doesn't allow bulk downloads. Add a source you're allowed to cache.")

        merged = {**current, **fields}
        _validate_source(merged["url_template"], merged["subdomains"],
                         merged["min_zoom"], merged["max_zoom"], merged["tile_format"])
        if "name" in fields:
            cur.execute("SELECT 1 FROM map_sources WHERE lower(name) = lower(%s) AND id <> %s",
                        (fields["name"], source_id))
            if cur.fetchone():
                raise HTTPException(status_code=409,
                                    detail="A map source with that name already exists")

        sets, values = [], []
        for key in ("name", "url_template", "subdomains", "min_zoom", "max_zoom",
                    "tile_format", "attribution", "is_active", "allow_download", "sort_order"):
            if key in fields:
                sets.append(f"{key} = %s")
                value = fields[key]
                values.append(value.strip() or None if isinstance(value, str) and key in
                              ("subdomains", "attribution") else value)
        values.append(source_id)
        cur.execute(f"UPDATE map_sources SET {', '.join(sets)} WHERE id = %s "
                    f"RETURNING {', '.join(_SOURCE_COLS)}", values)
        source = _row_to_source(cur.fetchone())

    audit.record("map_source.update", user=user, object_type="map_source",
                 object_id=source_id, object_label=source["name"],
                 detail={"fields": sorted(fields.keys())})
    return source


@router.delete("/admin/map/sources/{source_id}")
def delete_source(source_id: int, user: dict = Depends(auth.require_admin)):
    """Deleting a source deletes its packs' rows by cascade, so the files
    they point at are removed first -- a pack row is the only record of where
    its file lives, and dropping it without unlinking would orphan gigabytes
    on a disk nobody is watching."""
    with db_cursor(commit=True) as cur:
        source = _fetch_source(cur, source_id, for_update=True)
        if source["is_builtin"]:
            raise HTTPException(status_code=409,
                                detail="The built-in source cannot be deleted. Deactivate it "
                                       "instead if you do not want it offered.")
        cur.execute("SELECT id, file_path FROM map_packs WHERE source_id = %s", (source_id,))
        packs = cur.fetchall()
        cur.execute("DELETE FROM map_sources WHERE id = %s", (source_id,))

    removed = sum(_remove_pack_file(path) for _, path in packs)
    audit.record("map_source.delete", user=user, object_type="map_source",
                 object_id=source_id, object_label=source["name"],
                 detail={"packs_deleted": len(packs), "files_removed": removed})
    return {"deleted": True, "packs_deleted": len(packs)}


# ---------------------------------------------------------------------------
# Importing ATAK / MOBAC map source XML
# ---------------------------------------------------------------------------

# ATAK and MOBAC write their placeholders as {$x}; Leaflet and every XYZ
# consumer write them as {x}. That difference is the entire translation.
_ATAK_TOKENS = {
    "{$x}": "{x}", "{$y}": "{y}", "{$z}": "{z}",
    "{$serverpart}": "{s}", "{$s}": "{s}",
    "{$q}": "{q}",   # quadkey — recognised so it can be rejected with a reason
}


def _atak_to_xyz(url: str) -> str:
    for atak, xyz in _ATAK_TOKENS.items():
        url = url.replace(atak, xyz)
    return url


def _parse_atak_sources(xml_text: str) -> list:
    """Pull every <customMapSource> out of a pasted document.

    Parsed with the standard library's ElementTree, which ignores DTDs and so
    will not resolve external entities. The endpoint is admin-only and the
    payload is size-capped by the model above; this is configuration an
    administrator pasted in, not untrusted input off the wire.
    """
    try:
        root = ElementTree.fromstring(xml_text.strip())
    except ElementTree.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse that XML: {exc}")

    nodes = list(root.iter("customMapSource"))
    if root.tag == "customMapSource":
        nodes = [root] + [n for n in nodes if n is not root]
    if not nodes:
        raise HTTPException(
            status_code=400,
            detail="No <customMapSource> elements in that document. ATAK map source "
                   "files contain one per basemap.")

    out = []
    for node in nodes:
        def text(tag, default=None):
            found = node.find(tag)
            return found.text.strip() if found is not None and found.text else default

        name = text("name")
        url = text("url")
        if not name or not url:
            continue

        url = _atak_to_xyz(url)
        if "{q}" in url:
            out.append({"name": name, "skipped": "uses a quadkey URL, which this app does "
                                                 "not serve"})
            continue

        server_parts = text("serverParts") or ""
        # <serverParts>a b c</serverParts> is a space-separated list; Leaflet
        # wants the single string "abc".
        subdomains = "".join(p.strip() for p in server_parts.split()) or None

        tile_type = (text("tileType") or "png").lower().strip(".")
        if tile_type == "jpeg":
            tile_type = "jpg"
        if tile_type not in _MEDIA_TYPES:
            tile_type = "png"

        def as_int(tag, default):
            raw = text(tag)
            try:
                return max(0, min(22, int(raw)))
            except (TypeError, ValueError):
                return default

        out.append({
            "name": name,
            "url_template": url,
            "subdomains": subdomains,
            "min_zoom": as_int("minZoom", 0),
            "max_zoom": as_int("maxZoom", 19),
            "tile_format": tile_type,
            "attribution": None,
        })
    return out


@router.post("/admin/map/sources/import")
def import_sources(payload: SourceImport, user: dict = Depends(auth.require_admin)):
    """Import one or more sources from pasted ATAK map-source XML.

    Reports per-source outcomes rather than failing the whole paste on one bad
    entry: these files routinely hold a dozen basemaps and one of them using
    an unsupported URL scheme is not a reason to reject the other eleven.

    `allow_download` applies to everything in the paste and defaults to false.
    Whether the deployment is entitled to cache a given tile server is a
    question about that server's terms, and the honest default is no.
    """
    parsed = _parse_atak_sources(payload.xml)
    results = []
    created = 0

    with db_cursor(commit=True) as cur:
        for item in parsed:
            if item.get("skipped"):
                results.append({"name": item["name"], "status": "skipped",
                                "reason": item["skipped"]})
                continue
            try:
                _validate_source(item["url_template"], item["subdomains"],
                                 item["min_zoom"], item["max_zoom"], item["tile_format"])
            except HTTPException as exc:
                results.append({"name": item["name"], "status": "skipped",
                                "reason": exc.detail})
                continue

            cur.execute("SELECT 1 FROM map_sources WHERE lower(name) = lower(%s)",
                        (item["name"],))
            if cur.fetchone():
                results.append({"name": item["name"], "status": "skipped",
                                "reason": "a source with that name already exists"})
                continue

            cur.execute(
                """
                INSERT INTO map_sources (name, url_template, subdomains, min_zoom, max_zoom,
                                         tile_format, attribution, is_active, allow_download,
                                         sort_order, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s, 100, %s)
                """,
                (item["name"], item["url_template"], item["subdomains"],
                 item["min_zoom"], item["max_zoom"], item["tile_format"],
                 item["attribution"], payload.allow_download, user["id"]),
            )
            created += 1
            results.append({"name": item["name"], "status": "imported"})

    audit.record("map_source.import", user=user, object_type="map_source",
                 object_label=f"{created} source(s)",
                 detail={"created": created, "considered": len(parsed),
                         "allow_download": payload.allow_download})
    return {"created_count": created, "results": results}


# ---------------------------------------------------------------------------
# Packs
# ---------------------------------------------------------------------------

class PackEstimate(BaseModel):
    source_id: int
    min_lat: float = Field(ge=-90, le=90)
    min_lon: float = Field(ge=-180, le=180)
    max_lat: float = Field(ge=-90, le=90)
    max_lon: float = Field(ge=-180, le=180)
    min_zoom: int = Field(ge=0, le=22)
    max_zoom: int = Field(ge=0, le=22)


class PackCreate(PackEstimate):
    name: str = Field(min_length=1, max_length=160)


def _row_to_pack(row) -> dict:
    return dict(zip(_PACK_COLS, row))


def _remove_pack_file(path) -> int:
    if not path:
        return 0
    try:
        os.remove(path)
        return 1
    except OSError:
        # Already gone, or on a disk we cannot write. Either way the row is
        # going; a missing file is not a reason to refuse the delete.
        return 0


def _check_bbox(payload) -> None:
    if payload.max_lat <= payload.min_lat or payload.max_lon <= payload.min_lon:
        raise HTTPException(status_code=400,
                            detail="The area has no size — drag a box on the map first")
    if payload.max_zoom < payload.min_zoom:
        raise HTTPException(status_code=400, detail="max_zoom cannot be below min_zoom")


def _estimate(cur, payload) -> dict:
    source = _fetch_source(cur, payload.source_id)
    _check_bbox(payload)
    per_zoom = count_tiles(payload.min_lat, payload.min_lon, payload.max_lat,
                           payload.max_lon, payload.min_zoom, payload.max_zoom)
    total = sum(z["tiles"] for z in per_zoom)
    per_tile = _ESTIMATED_TILE_BYTES.get(source["tile_format"], 18_000)
    return {
        "source": source,
        "per_zoom": per_zoom,
        "tiles": total,
        "estimated_bytes": total * per_tile,
        "max_tiles": MAX_PACK_TILES,
        "over_limit": total > MAX_PACK_TILES,
        # Surfaced so the dialog can explain a greyed-out button rather than
        # just refusing when the user presses it.
        "allow_download": source["allow_download"],
        "beyond_source_zoom": payload.max_zoom > source["max_zoom"],
    }


@router.post("/admin/map/packs/estimate")
def estimate_pack(payload: PackEstimate, user: dict = Depends(auth.require_admin)):
    """Tile count and rough disk before anything is queued.

    This exists because tile counts quadruple per zoom level and nobody's
    intuition survives that. A box that looks reasonable at z12 is a
    quarter-million tiles at z16, and the moment to find that out is before
    the download starts, not six hours in.
    """
    with db_cursor() as cur:
        return _estimate(cur, payload)


@router.get("/admin/map/packs")
def list_packs(user: dict = Depends(auth.require_admin)):
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join('p.' + c for c in _PACK_COLS)}, s.name, s.tile_format
            FROM map_packs p JOIN map_sources s ON s.id = p.source_id
            ORDER BY p.created_at DESC, p.id DESC
            """
        )
        rows = cur.fetchall()
    items = []
    for r in rows:
        pack = _row_to_pack(r[: len(_PACK_COLS)])
        pack["source_name"] = r[len(_PACK_COLS)]
        pack["tile_format"] = r[len(_PACK_COLS) + 1]
        items.append(pack)
    return {"items": items}


@router.post("/admin/map/packs", status_code=201)
def create_pack(payload: PackCreate, user: dict = Depends(auth.require_admin)):
    with db_cursor(commit=True) as cur:
        estimate = _estimate(cur, payload)
        source = estimate["source"]

        if not source["allow_download"]:
            raise HTTPException(
                status_code=409,
                detail=f"'{source['name']}' isn't marked as downloadable. Mark it downloadable only if its terms allow it.")
        if estimate["over_limit"]:
            raise HTTPException(
                status_code=400,
                detail=f"That area is {estimate['tiles']:,} tiles, over the {MAX_PACK_TILES:,} limit for one pack. Narrow the area or lower the maximum zoom.")
        if payload.max_zoom > source["max_zoom"]:
            raise HTTPException(
                status_code=400,
                detail=f"'{source['name']}' only publishes up to zoom {source['max_zoom']}")

        cur.execute(
            f"""
            INSERT INTO map_packs (source_id, name, min_lat, min_lon, max_lat, max_lon,
                                   min_zoom, max_zoom, tiles_total, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {', '.join(_PACK_COLS)}
            """,
            (payload.source_id, payload.name.strip(), payload.min_lat, payload.min_lon,
             payload.max_lat, payload.max_lon, payload.min_zoom, payload.max_zoom,
             estimate["tiles"], user["id"]),
        )
        pack = _row_to_pack(cur.fetchone())

    pack["source_name"] = source["name"]
    audit.record("map_pack.create", user=user, object_type="map_pack",
                 object_id=pack["id"], object_label=pack["name"],
                 detail={"source": source["name"], "tiles": estimate["tiles"],
                         "zooms": f"{payload.min_zoom}-{payload.max_zoom}"})
    return pack


@router.post("/admin/map/packs/{pack_id}/cancel")
def cancel_pack(pack_id: int, user: dict = Depends(auth.require_admin)):
    """Ask the worker to stop. It checks between tiles rather than being
    killed, so whatever has already been written stays usable and queueing
    the same area again resumes instead of starting over."""
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT status, name FROM map_packs WHERE id = %s FOR UPDATE", (pack_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Map pack not found")
        status, name = row
        if status not in ("pending", "downloading"):
            raise HTTPException(status_code=409, detail=f"That pack is already {status}")
        if status == "pending":
            # Never claimed, so there is nobody to notice the flag.
            cur.execute("UPDATE map_packs SET status = 'cancelled', finished_at = now() "
                        "WHERE id = %s", (pack_id,))
        else:
            cur.execute("UPDATE map_packs SET cancel_requested = TRUE WHERE id = %s", (pack_id,))

    audit.record("map_pack.cancel", user=user, object_type="map_pack",
                 object_id=pack_id, object_label=name)
    return {"cancelled": True}


@router.delete("/admin/map/packs/{pack_id}")
def delete_pack(pack_id: int, user: dict = Depends(auth.require_admin)):
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT name, status, file_path, bytes_total FROM map_packs "
                    "WHERE id = %s FOR UPDATE", (pack_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Map pack not found")
        name, status, file_path, bytes_total = row
        if status == "downloading":
            raise HTTPException(status_code=409,
                                detail="That pack is still downloading — stop it first")
        cur.execute("DELETE FROM map_packs WHERE id = %s", (pack_id,))

    removed = _remove_pack_file(file_path)
    audit.record("map_pack.delete", user=user, object_type="map_pack",
                 object_id=pack_id, object_label=name,
                 detail={"bytes_freed": bytes_total if removed else 0})
    return {"deleted": True, "file_removed": bool(removed)}


# ---------------------------------------------------------------------------
# The tile server
# ---------------------------------------------------------------------------

def _read_tile(path: str, z: int, x: int, y: int):
    """One tile out of one MBTiles file, or None.

    MBTiles stores rows in TMS order, where row 0 is the SOUTH edge; XYZ (what
    Leaflet asks for) counts from the north. The flip below is the whole
    difference between the two schemes, and getting it wrong produces a map
    that looks plausible and is vertically mirrored.
    """
    tms_y = flip_y(y, z)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? "
            "AND tile_row = ?", (z, x, tms_y)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        # A half-written pack being read mid-download, or a file that is not
        # actually MBTiles. Treated as "no tile here", which degrades to a
        # blank square instead of a 500 on a map that is otherwise fine.
        return None
    finally:
        conn.close()


@router.get("/map/tiles/{source_id}/{z}/{x}/{y}")
def get_tile(source_id: int, z: int, x: int, y: int,
             user: dict = Depends(auth.require_user)):
    """Serve a tile from whatever pack covers it.

    A 204 rather than a 404 for a tile nobody downloaded: Leaflet renders an
    empty square either way, but a 404 fills the browser console with noise
    and makes a perfectly healthy partial pack look broken.
    """
    if z < 0 or z > 22 or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(status_code=400, detail="Tile coordinates out of range")

    with db_cursor() as cur:
        cur.execute("SELECT tile_format FROM map_sources WHERE id = %s", (source_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Map source not found")
        tile_format = row[0]
        # Newest pack first: if an area was downloaded twice, the later one is
        # the one somebody meant to have.
        cur.execute(
            """
            SELECT file_path FROM map_packs
            WHERE source_id = %s AND status IN ('done', 'downloading')
              AND file_path IS NOT NULL
              AND %s BETWEEN min_zoom AND max_zoom
            ORDER BY id DESC
            """,
            (source_id, z),
        )
        paths = [r[0] for r in cur.fetchall()]

    for path in paths:
        data = _read_tile(path, z, x, y)
        if data is not None:
            return Response(
                content=bytes(data),
                media_type=_MEDIA_TYPES.get(tile_format, "image/png"),
                # Tiles for a given z/x/y never change once downloaded, so this
                # is one of the few places in the app where a long immutable
                # cache is exactly right.
                headers={"Cache-Control": "public, max-age=604800, immutable"},
            )

    return Response(status_code=204, headers={"Cache-Control": "public, max-age=300"})
