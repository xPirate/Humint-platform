"""Address -> coordinates geocoding for Location entities, plus writing back
the Maidenhead grid locator that falls out of any coordinates a Location
ends up with (see geo.py).

Runs as its own job on the same poll loop as everything else in main.py, not
its own thread/process — same "no real concurrency benefit at personal/lab
scale" reasoning as the other jobs there. What IS different here is the
external rate limit: the default provider (OpenStreetMap's public Nominatim
instance) asks automated clients to stay under ~1 request/second and to
identify themselves via User-Agent —
https://operations.osmfoundation.org/policies/nominatim/ — and hammering it
during a large bulk-CSV-imported batch of addresses would risk the whole
worker's IP getting blocked. process_one_geocode() self-throttles via
_last_request_at rather than relying on the caller to pace it, so main.py's
poll loop can call it exactly like every other process_one_* job with no
special-casing.

A Location only ever gets queued for automatic geocoding when it has an
address but BOTH lat and lng are blank (see api/entities.py's
_apply_location_derived_fields, which is the one place that decides this).
That's deliberate: entering coordinates by hand, or already having them
from a prior geocode, always wins — nothing here ever silently overwrites
coordinates an analyst may have manually corrected. To force a re-geocode
after fixing an address typo, clear both lat and lng in the same edit, or
use the "Retry geocoding" action in the UI on a failed one (POST
/api/entities/{id}/retry-geocode), which resets geocode_status back to
'pending' server-side.

Failures (no results, bad response, network/timeout error, disabled
provider) land in geocode_status='failed' with a human-readable
geocode_error rather than being retried forever — an unparseable or
fictional address would otherwise burn through the rate-limited request
budget on every single poll cycle, indefinitely, for no benefit.
"""

import os
import time
from urllib.parse import urlencode

import requests

from db import db_cursor
from geo import maidenhead_locator

GEOCODE_ENABLED = os.environ.get("GEOCODE_ENABLED", "true").lower() == "true"
NOMINATIM_BASE_URL = os.environ.get("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org").rstrip("/")
# Nominatim's usage policy requires a way to identify/contact the operator
# of an automated client. A generic default here would look identical
# across every self-hosted install of this app, which is exactly what
# their policy asks installations not to do — analysts should set this in
# .env to something that actually identifies them (e.g. an email address).
GEOCODE_USER_AGENT = os.environ.get("GEOCODE_USER_AGENT", "humint-platform (set GEOCODE_USER_AGENT in .env)")
GEOCODE_MIN_INTERVAL_SECONDS = float(os.environ.get("GEOCODE_MIN_INTERVAL_SECONDS", "1.1"))
GEOCODE_TIMEOUT_SECONDS = int(os.environ.get("GEOCODE_TIMEOUT_SECONDS", "10"))

_last_request_at = 0.0


def _claim_pending_location(cur):
    cur.execute(
        "SELECT entity_id, address FROM location_details "
        "WHERE geocode_status = 'pending' "
        "ORDER BY entity_id LIMIT 1 FOR UPDATE SKIP LOCKED"
    )
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("UPDATE location_details SET geocode_status = 'processing' WHERE entity_id = %s", (row[0],))
    return row


def _geocode(address: str) -> tuple[float, float]:
    """Returns (lat, lng) on success, or raises ValueError with a message
    that's safe to store in geocode_error and show the analyst directly —
    no results found, a malformed/unexpected response, or a network/HTTP
    failure all funnel through here as the same kind of "this address
    didn't resolve" outcome."""
    params = {"q": address, "format": "jsonv2", "limit": 1}
    url = f"{NOMINATIM_BASE_URL}/search?{urlencode(params)}"
    try:
        resp = requests.get(url, headers={"User-Agent": GEOCODE_USER_AGENT}, timeout=GEOCODE_TIMEOUT_SECONDS)
        resp.raise_for_status()
        results = resp.json()
    except Exception as exc:
        raise ValueError(f"geocoding service error: {exc}")
    if not results:
        raise ValueError("no results found for this address")
    try:
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"unexpected response from geocoding service: {exc}")


def process_one_geocode() -> bool:
    """Returns True if it did work (attempted a geocode, whether or not it
    succeeded) so main() knows not to sleep while there's a backlog; False
    if there's nothing pending, geocoding is disabled, or it's too soon
    since the last request to make another one without risking the
    provider's rate limit."""
    if not GEOCODE_ENABLED:
        return False

    global _last_request_at
    if time.monotonic() - _last_request_at < GEOCODE_MIN_INTERVAL_SECONDS:
        return False

    with db_cursor(commit=True) as cur:
        row = _claim_pending_location(cur)
        if row is None:
            return False
        entity_id, address = row

    _last_request_at = time.monotonic()
    try:
        lat, lng = _geocode(address)
    except ValueError as exc:
        with db_cursor(commit=True) as cur:
            cur.execute(
                "UPDATE location_details SET geocode_status = 'failed', geocode_error = %s, geocoded_at = now() "
                "WHERE entity_id = %s",
                (str(exc)[:2000], entity_id),
            )
        print(f"[worker] geocode {entity_id}: failed: {exc}", flush=True)
        return True

    grid = maidenhead_locator(lat, lng)
    with db_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE location_details SET lat = %s, lng = %s, maidenhead_grid = %s, "
            "geocode_status = 'done', geocode_error = NULL, geocoded_at = now() "
            "WHERE entity_id = %s",
            (lat, lng, grid, entity_id),
        )
    print(f"[worker] geocode {entity_id}: {lat}, {lng} ({grid})", flush=True)
    return True
