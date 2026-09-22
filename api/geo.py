"""Maidenhead grid locator conversion — pure math, no network calls, no
dependencies beyond the standard library.

Maidenhead locators are the grid-square system amateur radio operators use
to describe a station's location (propagation/distance calculations, contest
logging, etc.) — a natural companion to the Communication entity's HF/VHF/
UHF/FM radio mediums, and a cheap, always-available add-on to any Location
that has coordinates, however they got there (typed in by hand, or filled
in by worker/geocode.py). Six-character precision (roughly 2.8km x 4.6km at
mid-latitudes) is the resolution normally shared on-air; finer precision
only matters for satellite/EME work this app has no other reason to
support, so this stops at six characters.

Duplicated verbatim in worker/geo.py: api/ and worker/ are separate Docker
build contexts (see worker/idgen.py for the same reasoning applied to ID
generation), so there's no single module either side could import from
without adding a shared package + build-context plumbing neither needs
otherwise. If this ever needs a third caller, that tradeoff is worth
revisiting; for two, keeping the ~20 lines in sync by hand is cheaper.
"""

_UPPER = "ABCDEFGHIJKLMNOPQR"
_LOWER = "abcdefghijklmnopqrstuvwx"


def maidenhead_locator(lat: float, lng: float) -> str:
    """Six-character Maidenhead grid locator (e.g. "IO91xl") for a
    latitude/longitude pair. Inputs are clamped to valid ranges rather than
    raising, so a value that's exactly on a boundary (lat=90, lng=180)
    or drifted a hair past one via float rounding can't index past the
    end of the letter/digit tables below — callers should still validate
    lat/lng are sane coordinates first (see LocationDetails' Pydantic
    bounds); this only guards the grid-square math itself.
    """
    lat = max(-90.0, min(90.0, lat)) + 90.0
    lng = max(-180.0, min(180.0, lng)) + 180.0

    field_lng = min(17, int(lng // 20))
    field_lat = min(17, int(lat // 10))
    lng -= field_lng * 20
    lat -= field_lat * 10

    square_lng = min(9, int(lng // 2))
    square_lat = min(9, int(lat // 1))
    lng -= square_lng * 2
    lat -= square_lat * 1

    subsquare_lng = min(23, int(lng // (2 / 24)))
    subsquare_lat = min(23, int(lat // (1 / 24)))

    return (
        _UPPER[field_lng] + _UPPER[field_lat]
        + str(square_lng) + str(square_lat)
        + _LOWER[subsquare_lng] + _LOWER[subsquare_lat]
    )
