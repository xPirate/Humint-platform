"""Web Mercator slippy-map arithmetic, shared by the API and the worker.

Kept as an exact duplicate of worker/tilemath.py rather than a shared package
-- the api and worker containers are separate build contexts (see their
Dockerfiles' `COPY . .`), so there's no import path between them without
introducing a shared library layer this project doesn't otherwise need.

It matters more here than in the other duplicated modules that the two copies
agree: the API counts the tiles in an area to tell an admin what they are
about to download, and the worker then fetches them. If the two disagreed by
one row, every pack would finish reporting a count it never reached.
"""

import math


def lon_to_x(lon: float, zoom: int) -> int:
    return int((lon + 180.0) / 360.0 * (1 << zoom))


def lat_to_y(lat: float, zoom: int) -> int:
    # Clamped to the Mercator limit: the projection has no north or south
    # pole, and a bbox dragged to the top of the map would otherwise produce
    # a tile row that does not exist.
    lat = max(min(lat, 85.05112878), -85.05112878)
    return int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * (1 << zoom))


def tile_range(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
               zoom: int) -> tuple:
    """(x0, x1, y0, y1) inclusive for one zoom level, clamped to the world."""
    limit = (1 << zoom) - 1
    x0 = max(0, min(limit, lon_to_x(min_lon, zoom)))
    x1 = max(0, min(limit, lon_to_x(max_lon, zoom)))
    # y is inverted: the NORTH edge of the box is the LOW row number.
    y0 = max(0, min(limit, lat_to_y(max_lat, zoom)))
    y1 = max(0, min(limit, lat_to_y(min_lat, zoom)))
    return min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)


def count_tiles(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                min_zoom: int, max_zoom: int) -> list:
    """Per-zoom tile counts.

    Returned per zoom rather than as one total so the UI can show where the
    weight is -- it is almost always the last zoom level, by a factor of four
    over the one before it, and seeing that is what stops somebody asking for
    z19 over a county.
    """
    out = []
    for z in range(min_zoom, max_zoom + 1):
        x0, x1, y0, y1 = tile_range(min_lat, min_lon, max_lat, max_lon, z)
        out.append({"zoom": z, "tiles": (x1 - x0 + 1) * (y1 - y0 + 1)})
    return out


def flip_y(y: int, zoom: int) -> int:
    """XYZ row number <-> MBTiles (TMS) row number.

    MBTiles counts rows from the SOUTH edge; Leaflet and every XYZ tile server
    count from the north. This one line is the whole difference between the
    two schemes, and getting it wrong produces a map that looks plausible and
    is vertically mirrored.
    """
    return (1 << zoom) - 1 - y
