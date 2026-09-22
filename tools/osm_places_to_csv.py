#!/usr/bin/env python3
"""Build a Location import CSV from OpenStreetMap.

Written for "put every police station, jail, hospital and fire station in the
OKC metro into the case file", but the bounding box and the categories are both
flags, so it does the same job for anywhere else.

It writes the columns the app's own Location template declares, in that order,
so the result drops straight into **Entities -> Import** with no editing.

Why a script rather than something inside the app: the data is a one-off bulk
load of public reference material, not case reporting, and the app should not
grow a standing outbound connection to a service it needs once. Run it, import
the file, throw the file away.

No third-party packages -- standard library only -- so it runs on anything with
python3 and does not need a virtualenv.

  # Straight from Overpass (needs internet)
  python3 osm_places_to_csv.py -o okc-emergency.csv

  # From a file exported by overpass-turbo.eu, for a machine that cannot
  # reach the API. --print-query gives you the query to paste there.
  python3 osm_places_to_csv.py --from-file export.geojson -o okc-emergency.csv

  # Only what is new since last time
  python3 osm_places_to_csv.py --skip-ids-from okc-emergency.csv -o new.csv

**Attribution.** OpenStreetMap data is published under the Open Database
License. Anything derived from it carries that licence, and the attribution
belongs on any map or report that shows it: "© OpenStreetMap contributors".
Each row's description records the OSM object it came from, which is both the
attribution and the thing that makes a second import skippable.

**Accuracy.** OSM is a volunteer map. Coverage of fire and police stations in a
US metro is generally good and hospital coverage is very good, but it is not a
government roster: a station can be missing, closed, or sited approximately.
Treat this as a starting set to correct, not as authoritative.
"""

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# The columns the app's Location import template declares, in its order. Taken
# from api/bulk_import.build_template_csv -- if that ever changes, this is the
# line to change with it.
COLUMNS = ["name", "description", "address", "environment", "lat", "lng"]

# Oklahoma City metro, wide enough to take in Guthrie, El Reno, Chickasha,
# Purcell, Norman and Shawnee's western edge. south, west, north, east.
DEFAULT_BBOX = (34.85, -98.20, 36.00, -96.90)

# What each category is in OSM's tagging, and what to call it in a description.
# Keys are what you pass to --categories.
CATEGORIES = {
    "police":    ("amenity", "police",           "Police station"),
    "fire":      ("amenity", "fire_station",     "Fire station"),
    "hospital":  ("amenity", "hospital",         "Hospital"),
    "jail":      ("amenity", "prison",           "Jail or prison"),
    "ems":       ("amenity", "ambulance_station", "Ambulance station"),
}
DEFAULT_CATEGORIES = ["police", "fire", "hospital", "jail"]

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

USER_AGENT = ("humint-platform osm_places_to_csv "
              "(one-off reference import; contact your deployment admin)")


def build_query(bbox, categories) -> str:
    """One Overpass query for every category, as nodes, ways and relations.

    `out center` gives ways and relations a single representative point, which
    is what a map marker needs -- a hospital is usually mapped as a building
    outline, and an outline cannot be a pin.
    """
    south, west, north, east = bbox
    box = f"({south},{west},{north},{east})"
    clauses = "".join(
        f'nwr["{CATEGORIES[c][0]}"="{CATEGORIES[c][1]}"]{box};' for c in categories)
    return f"[out:json][timeout:180];({clauses});out center tags;"


def fetch(query: str) -> dict:
    last_error = None
    for url in OVERPASS_URLS:
        try:
            request = urllib.request.Request(
                url,
                data=urllib.parse.urlencode({"data": query}).encode(),
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_error = f"{url}: {exc}"
            print(f"  {url} did not answer ({exc}); trying the next mirror",
                  file=sys.stderr)
    raise SystemExit(
        f"Could not reach Overpass.\n  Last error: {last_error}\n\n"
        "If this machine has no route to the API, run with --print-query, paste\n"
        "the query into https://overpass-turbo.eu, export the result as GeoJSON,\n"
        "and re-run with --from-file <that file>.")


def load_file(path: str) -> dict:
    """Accept either an Overpass JSON response or an overpass-turbo GeoJSON
    export -- people reach for whichever the button gave them."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if "elements" in data:
        return data
    if data.get("type") == "FeatureCollection":
        elements = []
        for feature in data.get("features", []):
            geometry = feature.get("geometry") or {}
            tags = dict(feature.get("properties") or {})
            # overpass-turbo puts the object's identity in @id / id as
            # "node/12345"; keep it, because it is the dedup key.
            raw_id = str(tags.pop("@id", None) or feature.get("id") or "")
            kind, _, number = raw_id.partition("/")
            point = _geojson_point(geometry)
            if point is None:
                continue
            elements.append({
                "type": kind or "node",
                "id": number or raw_id,
                "lat": point[0], "lon": point[1],
                "tags": {k: v for k, v in tags.items() if not k.startswith("@")},
            })
        return {"elements": elements}
    raise SystemExit(f"{path} is neither an Overpass response nor a GeoJSON "
                     "FeatureCollection.")


def _geojson_point(geometry):
    """A representative (lat, lon) for any GeoJSON geometry."""
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None
    if kind == "Point":
        return coords[1], coords[0]
    # Everything else: the mean of the outer ring / line, which is close enough
    # for a marker and always inside a convex footprint.
    ring = coords[0] if kind in ("Polygon", "MultiLineString") else coords
    if kind == "MultiPolygon":
        ring = coords[0][0]
    points = [p for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
    if not points:
        return None
    return (sum(p[1] for p in points) / len(points),
            sum(p[0] for p in points) / len(points))


def element_point(element):
    if element.get("lat") is not None and element.get("lon") is not None:
        return float(element["lat"]), float(element["lon"])
    centre = element.get("center")
    if centre:
        return float(centre["lat"]), float(centre["lon"])
    return None


def category_of(tags: dict):
    for key, (tag, value, label) in CATEGORIES.items():
        if tags.get(tag) == value:
            return key, label
    return None, None


def build_address(tags: dict) -> str:
    street = " ".join(x for x in (tags.get("addr:housenumber"),
                                  tags.get("addr:street")) if x)
    parts = [street, tags.get("addr:city"), tags.get("addr:state"),
             tags.get("addr:postcode")]
    return ", ".join(p for p in parts if p).strip(", ")


def build_name(tags: dict, label: str) -> str:
    """A name worth having, or nothing.

    A case file full of records called "Fire Station" is worse than a smaller
    one, so anything that cannot be told apart from its neighbours is dropped
    rather than imported as a near-duplicate. `ref` rescues most of them: a
    station tagged only with operator and ref becomes "Oklahoma City Fire
    Department Station 12", which is exactly what people call it.
    """
    name = (tags.get("name") or "").strip()
    if name:
        return name
    operator = (tags.get("operator") or "").strip()
    ref = (tags.get("ref") or "").strip()
    if operator and ref:
        return f"{operator} Station {ref}" if ref.isdigit() else f"{operator} {ref}"
    if operator:
        return f"{operator} {label.lower()}"
    return ""


def build_description(label: str, osm_refs, tags: dict) -> str:
    bits = [f"{label}."]
    if tags.get("operator"):
        bits.append(f"Operator: {tags['operator']}.")
    if tags.get("phone") or tags.get("contact:phone"):
        bits.append(f"Phone: {tags.get('phone') or tags['contact:phone']}.")
    if tags.get("emergency") == "yes" and label == "Hospital":
        bits.append("Has an emergency department.")
    # The provenance token. Machine-findable, so a later run can skip it, and
    # it is the OpenStreetMap attribution this data has to carry.
    #
    # EVERY id that collapsed into this row is listed, not just the survivor's.
    # A facility mapped as both a node and a building outline is one record
    # here, and if the outline's id went unrecorded the next run would not
    # recognise it and would import the place a second time.
    bits.append("Imported from OpenStreetMap " + ", ".join(osm_refs)
                + " (© OpenStreetMap contributors, ODbL).")
    return " ".join(bits)


_ID_RE = re.compile(r"\b(node|way|relation)/(\d+)\b")


def ids_in_csv(path: str) -> set:
    """OSM ids already present in a previously generated CSV."""
    found = set()
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                for kind, number in _ID_RE.findall(row.get("description") or ""):
                    found.add(f"{kind}/{number}")
    except FileNotFoundError:
        pass
    return found


def to_rows(payload: dict, environment: str, categories, skip_ids=frozenset()):
    """Elements -> CSV rows, with a tally of everything dropped and why."""
    wanted = set(categories)
    rows = []
    # A facility mapped as both a node and a building outline is one facility.
    # Keyed on name plus coordinates rounded to ~10 m; the first one wins,
    # and `out center tags` returns ways after nodes, so preferring the first
    # is not meaningful -- what matters is that only one survives.
    seen = {}
    stats = {"total": 0, "no_point": 0, "no_name": 0, "wrong_category": 0,
             "already_imported": 0, "duplicate": 0}

    for element in payload.get("elements", []):
        stats["total"] += 1
        tags = element.get("tags") or {}
        key, label = category_of(tags)
        if key not in wanted:
            stats["wrong_category"] += 1
            continue
        point = element_point(element)
        if point is None:
            stats["no_point"] += 1
            continue
        osm_ref = f"{element.get('type', 'node')}/{element.get('id')}"
        if osm_ref in skip_ids:
            stats["already_imported"] += 1
            continue
        name = build_name(tags, label)
        if not name:
            stats["no_name"] += 1
            continue

        dedup_key = (name.lower(), round(point[0], 4), round(point[1], 4))
        if dedup_key in seen:
            stats["duplicate"] += 1
            # Keep the id anyway, so the next run recognises this object as
            # already imported instead of adding the place all over again.
            seen[dedup_key]["_refs"].append(osm_ref)
            continue

        row = {
            "name": name,
            "_refs": [osm_ref],
            "_label": label,
            "_tags": tags,
            "address": build_address(tags),
            "environment": environment,
            # Six decimal places is about 10 cm -- far more than a building
            # centroid deserves, and it keeps the value from being written in
            # scientific notation by a spreadsheet later.
            "lat": f"{point[0]:.6f}",
            "lng": f"{point[1]:.6f}",
        }
        seen[dedup_key] = row
        rows.append(row)

    # Descriptions last, once every row knows all the ids that folded into it.
    for row in rows:
        row["description"] = build_description(row.pop("_label"), row.pop("_refs"),
                                               row.pop("_tags"))

    rows.sort(key=lambda r: r["name"].lower())
    return rows, stats


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Build a Location import CSV from OpenStreetMap.")
    parser.add_argument("-o", "--output", default="osm-places.csv")
    parser.add_argument("--bbox", help="south,west,north,east (default: OKC metro)")
    parser.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES),
                        help="comma-separated: " + ", ".join(CATEGORIES))
    parser.add_argument("--environment", default="Permissive",
                        help="value for every row's environment column; "
                             "pass '' to leave it unassessed")
    parser.add_argument("--from-file", help="an Overpass JSON or overpass-turbo "
                                            "GeoJSON export, instead of querying")
    parser.add_argument("--skip-ids-from", metavar="CSV",
                        help="omit anything already in this earlier CSV")
    parser.add_argument("--print-query", action="store_true",
                        help="print the Overpass query and exit")
    args = parser.parse_args()

    bbox = DEFAULT_BBOX
    if args.bbox:
        try:
            bbox = tuple(float(x) for x in args.bbox.split(","))
            if len(bbox) != 4:
                raise ValueError
        except ValueError:
            raise SystemExit("--bbox must be south,west,north,east in decimal degrees")

    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    unknown = [c for c in categories if c not in CATEGORIES]
    if unknown:
        raise SystemExit(f"Unknown categories {unknown}. Choose from: "
                         + ", ".join(CATEGORIES))

    query = build_query(bbox, categories)
    if args.print_query:
        print(query)
        return

    if args.from_file:
        payload = load_file(args.from_file)
        print(f"Read {len(payload.get('elements', []))} objects from {args.from_file}")
    else:
        print(f"Querying Overpass for {', '.join(categories)} in "
              f"{bbox[0]},{bbox[1]} .. {bbox[2]},{bbox[3]}")
        print("  (a metro-sized box takes a minute or two)")
        payload = fetch(query)
        print(f"  {len(payload.get('elements', []))} objects returned")

    skip_ids = ids_in_csv(args.skip_ids_from) if args.skip_ids_from else frozenset()
    if skip_ids:
        print(f"Skipping {len(skip_ids)} already in {args.skip_ids_from}")

    rows, stats = to_rows(payload, args.environment, categories, skip_ids)
    write_csv(rows, args.output)

    print(f"\nWrote {len(rows)} rows to {args.output}")
    print(f"  considered            {stats['total']}")
    print(f"  not a wanted category {stats['wrong_category']}")
    print(f"  no usable name        {stats['no_name']}   (dropped on purpose --"
          " see build_name)")
    print(f"  no coordinates        {stats['no_point']}")
    print(f"  duplicate of another  {stats['duplicate']}")
    if skip_ids:
        print(f"  already imported      {stats['already_imported']}")
    print("\nImport it with Entities -> Import, choosing Location.")
    print("Data © OpenStreetMap contributors, ODbL. It is a volunteer map, not a"
          " government roster -- check anything you will act on.")


if __name__ == "__main__":
    main()
