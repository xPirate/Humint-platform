"""Downloads map packs: an area of one tile source, into an MBTiles file.

This runs on its own thread rather than as another step in main.py's poll
loop. Every other job in this worker is "process one unit and return" and
finishes in seconds; a map pack is hours of small HTTP requests. Putting it in
the loop would mean an analyst's uploaded document waited behind a basemap.

**Rate limiting is not politeness, it is the deal.** Tile servers publish
usage policies and this walks one of them systematically. MAP_TILE_RATE_LIMIT
is deliberately low by default and deliberately global rather than per-pack:
two packs downloading at once from the same host is exactly the behaviour that
gets a deployment blocked.

The output is a plain MBTiles file -- SQLite with tile blobs in it -- which is
the same format ATAK, QGIS and every other offline map tool reads. A pack
built here works elsewhere, and a pack built elsewhere can be dropped in.

Resuming is free and deliberate. Tiles already in the file are skipped, so a
cancelled or crashed job continues where it stopped when the same area is
queued again, rather than re-fetching several hundred thousand tiles somebody
already paid for once.
"""

import os
import sqlite3
import threading
import time

import requests

from db import db_cursor
from tilemath import flip_y, tile_range

MAP_PACK_DIR = os.environ.get("MAP_PACK_DIR", "/data/map-packs")
DOWNLOAD_ENABLED = os.environ.get("MAP_DOWNLOAD_ENABLED", "true").lower() == "true"
POLL_SECONDS = int(os.environ.get("MAP_PACK_POLL_SECONDS", "10"))

# Requests per second across the whole worker. 4/s is slower than a browser
# panning a map and is the sort of rate a tile operator will not notice.
RATE_LIMIT = float(os.environ.get("MAP_TILE_RATE_LIMIT", "4"))
TIMEOUT_SECONDS = int(os.environ.get("MAP_TILE_TIMEOUT_SECONDS", "20"))

# Sent on every tile request. Several operators require a contactable agent
# string and will serve errors to anything that looks anonymous.
USER_AGENT = os.environ.get(
    "MAP_TILE_USER_AGENT",
    "humint-platform offline map packer (set MAP_TILE_USER_AGENT in .env)")

# Give up on a pack after this many failures in a row. One tile failing is
# weather; twenty-five in a row means the host is refusing us, and continuing
# to hammer it for another six hours helps nobody.
MAX_CONSECUTIVE_FAILURES = 25
RETRIES_PER_TILE = 2

# How often to write progress back and re-read the cancel flag. Every tile
# would be an UPDATE per HTTP request; every few hundred would make Stop feel
# broken. Fifty is roughly ten seconds at the default rate.
PROGRESS_EVERY = 50

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

_rate_lock = threading.Lock()
_last_request_at = [0.0]


def _throttle() -> None:
    if RATE_LIMIT <= 0:
        return
    interval = 1.0 / RATE_LIMIT
    with _rate_lock:
        wait = _last_request_at[0] + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at[0] = time.monotonic()


def _open_pack(path: str, pack: dict) -> sqlite3.Connection:
    """Open (creating if needed) an MBTiles file for this pack."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS tiles ("
                 "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS tile_index "
                 "ON tiles (zoom_level, tile_column, tile_row)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS metadata_name ON metadata (name)")
    meta = {
        "name": pack["name"],
        "format": pack["tile_format"],
        "type": "baselayer",
        "version": "1.1",
        "description": f"humint-platform pack #{pack['id']} from {pack['source_name']}",
        "bounds": f"{pack['min_lon']},{pack['min_lat']},{pack['max_lon']},{pack['max_lat']}",
        "minzoom": str(pack["min_zoom"]),
        "maxzoom": str(pack["max_zoom"]),
        "attribution": pack.get("attribution") or "",
    }
    for key, value in meta.items():
        conn.execute("INSERT INTO metadata (name, value) VALUES (?, ?) "
                     "ON CONFLICT (name) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()
    return conn


def _tile_url(pack: dict, z: int, x: int, y: int, index: int) -> str:
    url = (pack["url_template"]
           .replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y)))
    subs = pack.get("subdomains")
    if "{s}" in url:
        url = url.replace("{s}", subs[index % len(subs)] if subs else "a")
    return url


def _fetch_tile(url: str):
    """(data, outcome) where outcome is 'ok', 'missing' or an error string.

    A 404 is 'missing', not a failure. Most sources answer that way for tiles
    outside their coverage -- open sea, off the edge of an imagery mosaic --
    and a rectangle dragged over a coastline is full of them. Treating those
    as errors would fail a pack that downloaded perfectly.
    """
    try:
        resp = _session.get(url, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if resp.status_code in (404, 204):
        return None, "missing"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    if not resp.content:
        return None, "missing"
    return resp.content, "ok"


def _claim_pack():
    """Take the oldest queued pack, if there is one.

    FOR UPDATE SKIP LOCKED for the same reason as everywhere else in this
    worker: a second replica would take a different row rather than the two
    of them fighting over one.
    """
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            SELECT p.id, p.name, p.min_lat, p.min_lon, p.max_lat, p.max_lon,
                   p.min_zoom, p.max_zoom, p.tiles_total,
                   s.url_template, s.subdomains, s.tile_format, s.name, s.attribution,
                   s.allow_download
            FROM map_packs p JOIN map_sources s ON s.id = p.source_id
            WHERE p.status = 'pending'
            ORDER BY p.created_at, p.id
            LIMIT 1
            FOR UPDATE OF p SKIP LOCKED
            """
        )
        row = cur.fetchone()
        if row is None:
            return None
        pack = {
            "id": row[0], "name": row[1],
            "min_lat": row[2], "min_lon": row[3], "max_lat": row[4], "max_lon": row[5],
            "min_zoom": row[6], "max_zoom": row[7], "tiles_total": row[8],
            "url_template": row[9], "subdomains": row[10], "tile_format": row[11],
            "source_name": row[12], "attribution": row[13], "allow_download": row[14],
        }

        if not pack["allow_download"]:
            # Belt and braces: the API refuses this at creation time, but a
            # source can be marked non-downloadable after a pack was queued
            # against it, and the licence answer has to win.
            cur.execute("UPDATE map_packs SET status = 'failed', finished_at = now(), "
                        "last_error = %s WHERE id = %s",
                        ("Its source is no longer marked as downloadable.", pack["id"]))
            return None

        path = os.path.join(MAP_PACK_DIR, f"pack-{pack['id']}.mbtiles")
        cur.execute(
            "UPDATE map_packs SET status = 'downloading', started_at = now(), "
            "file_path = %s, last_error = NULL, cancel_requested = FALSE WHERE id = %s",
            (path, pack["id"]))
        pack["file_path"] = path
        return pack


def _report(pack_id: int, done: int, failed: int, byte_total: int) -> bool:
    """Write progress back; return True if an admin has asked us to stop."""
    with db_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE map_packs SET tiles_done = %s, tiles_failed = %s, bytes_total = %s "
            "WHERE id = %s RETURNING cancel_requested",
            (done, failed, byte_total, pack_id))
        row = cur.fetchone()
    return bool(row and row[0])


def _is_cancelled(pack_id: int) -> bool:
    with db_cursor() as cur:
        cur.execute("SELECT cancel_requested FROM map_packs WHERE id = %s", (pack_id,))
        row = cur.fetchone()
    return bool(row and row[0])


def _finish(pack_id: int, status: str, done: int, failed: int, byte_total: int,
            error=None) -> None:
    with db_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE map_packs SET status = %s, tiles_done = %s, tiles_failed = %s, "
            "bytes_total = %s, last_error = %s, finished_at = now() WHERE id = %s",
            (status, done, failed, byte_total, error, pack_id))


def _download_pack(pack: dict) -> None:
    conn = _open_pack(pack["file_path"], pack)
    done = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    failed = 0
    consecutive_failures = 0
    since_progress = 0
    # Counts every coordinate considered, including ones already in the file.
    # Resuming a large pack walks hundreds of thousands of rows it can skip,
    # and Stop has to work during that walk too, not just while fetching.
    scanned = 0
    index = 0
    subs = pack.get("subdomains") or ""

    print(f"[worker] map pack #{pack['id']} '{pack['name']}': starting "
          f"({pack['tiles_total']} tiles, {done} already present)", flush=True)

    try:
        for z in range(pack["min_zoom"], pack["max_zoom"] + 1):
            x0, x1, y0, y1 = tile_range(pack["min_lat"], pack["min_lon"],
                                        pack["max_lat"], pack["max_lon"], z)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    row = flip_y(y, z)
                    scanned += 1
                    have = conn.execute(
                        "SELECT 1 FROM tiles WHERE zoom_level = ? AND tile_column = ? "
                        "AND tile_row = ?", (z, x, row)).fetchone()
                    if have:
                        if scanned % 5000 == 0 and _is_cancelled(pack["id"]):
                            _seal(conn)
                            _finish(pack["id"], "cancelled", done, failed,
                                    _file_size(pack["file_path"]))
                            return
                        continue

                    index += 1
                    _throttle()
                    data, outcome = None, "missing"
                    for attempt in range(RETRIES_PER_TILE + 1):
                        data, outcome = _fetch_tile(
                            _tile_url({**pack, "subdomains": subs}, z, x, y, index))
                        if outcome in ("ok", "missing"):
                            break
                        # Back off a little between attempts rather than
                        # retrying instantly into whatever is failing.
                        time.sleep(1.5 * (attempt + 1))

                    if outcome == "ok":
                        conn.execute(
                            "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
                            "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                            (z, x, row, sqlite3.Binary(data)))
                        done += 1
                        consecutive_failures = 0
                    elif outcome == "missing":
                        # Nothing to store and nothing wrong. Counted as done
                        # so progress reaches 100% over a coastline.
                        done += 1
                        consecutive_failures = 0
                    else:
                        failed += 1
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            conn.commit()
                            size = _file_size(pack["file_path"])
                            _finish(pack["id"], "failed", done, failed, size,
                                    f"Gave up after {consecutive_failures} consecutive "
                                    f"failures. Last error: {outcome}")
                            print(f"[worker] map pack #{pack['id']}: giving up — {outcome}",
                                  flush=True)
                            return

                    since_progress += 1
                    if since_progress >= PROGRESS_EVERY:
                        conn.commit()
                        since_progress = 0
                        if _report(pack["id"], done, failed, _file_size(pack["file_path"])):
                            _seal(conn)
                            _finish(pack["id"], "cancelled", done, failed,
                                    _file_size(pack["file_path"]))
                            print(f"[worker] map pack #{pack['id']}: stopped by request "
                                  f"({done} tiles kept)", flush=True)
                            return

        _seal(conn)
        size = _file_size(pack["file_path"])
        _finish(pack["id"], "done", done, failed, size)
        print(f"[worker] map pack #{pack['id']} '{pack['name']}': done — "
              f"{done} tiles, {failed} failed, {size} bytes", flush=True)
    finally:
        try:
            conn.commit()
        except sqlite3.Error:
            pass
        conn.close()


def _seal(conn: sqlite3.Connection) -> None:
    """Fold the write-ahead log back into the file and leave WAL mode.

    WAL is right while downloading -- the API serves tiles out of a pack that
    is still being written -- but a finished pack should be ONE file. A
    stray -wal alongside it is a file somebody copies without, and an MBTiles
    pack is meant to be handed to ATAK or QGIS or another instance of this
    app by copying it.
    """
    try:
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[worker] could not seal pack file: {exc}", flush=True)


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def process_one_pack() -> bool:
    """Claim and download one pack. True if there was one to do."""
    pack = _claim_pack()
    if pack is None:
        return False
    try:
        _download_pack(pack)
    except Exception as exc:                     # noqa: BLE001 - the thread must survive
        print(f"[worker] map pack #{pack['id']} failed: {exc}", flush=True)
        _finish(pack["id"], "failed", 0, 0, _file_size(pack.get("file_path") or ""),
                str(exc)[:500])
    return True


def _loop() -> None:
    while True:
        try:
            if not process_one_pack():
                time.sleep(POLL_SECONDS)
        except Exception as exc:                 # noqa: BLE001 - never let the thread die
            print(f"[worker] map download loop error: {exc}", flush=True)
            time.sleep(POLL_SECONDS)


def start_download_thread() -> None:
    """Start the downloader, unless it is switched off.

    A daemon thread: if the worker is being shut down there is no value in
    waiting for a six-hour download to reach a tidy stopping point, and the
    pack resumes from what it wrote anyway.
    """
    if not DOWNLOAD_ENABLED:
        print("[worker] map pack downloads disabled (MAP_DOWNLOAD_ENABLED=false)", flush=True)
        return
    thread = threading.Thread(target=_loop, name="map-download", daemon=True)
    thread.start()
    print(f"[worker] map pack downloader started, rate limit = {RATE_LIMIT}/s", flush=True)
