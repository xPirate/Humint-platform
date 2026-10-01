"""Generate the cross-language title fixture from the console's own code.

A report's title is composed twice: on the handset, from the pattern in
api/field_templates.json, and here on the console when something posts
without one. Two implementations of the same rule in two languages drift,
and the drift is quiet -- a vehicle filed under a title nobody expects.

So the Android unit test does not hand-write its expected answers. This
script runs the console's own compose_title over a list of cases and writes
the result as a fixture the Kotlin test replays. Change either side without
the other and TitleParityTest fails.

Run it from anywhere; it finds the repository around itself and writes the
fixture to its usual home unless you name another:

    python3 tools/gen_title_cases.py
    python3 tools/gen_title_cases.py /somewhere/else.json
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(
    ROOT, "android", "app", "src", "test", "resources", "title_cases.json")
sys.path.insert(0, os.path.join(ROOT, "api"))
import field_templates as ft

CASES = [
    ("vehicle", {"color": "white", "make": "Ford", "model": "Ranger", "plate": "ABC 1234"}),
    ("vehicle", {"make": "Ford", "model": "Ranger"}),
    ("vehicle", {"plate": "ABC 1234"}),
    ("vehicle", {"color": "white"}),
    ("vehicle", {}),
    ("vehicle", {"color": "  white  ", "make": "Ford"}),
    ("sigint", {"frequency": "146.940", "station_id": "W5ABC"}),
    ("sigint", {"frequency": "146.940"}),
    ("sigint", {"station_id": "W5ABC"}),
    ("sigint", {}),
    ("df", {"frequency": "146.94", "bearing_deg": 212}),
    ("df", {"bearing_deg": 212}),
    ("df", {"frequency": "146.94"}),
    ("df", {}),
    ("individual", {"name": "the tall one in the grey coat"}),
    ("individual", {"aliases": ["Grey coat", "Tom"]}),
    ("place", {"place_name": "The north gate"}),
    ("salute", {"activity": "Loading crates into a panel van"}),
    ("note", {"summary": "Gate open at the north compound"}),
    ("note", {"body": "no summary given"}),
    # A verbatim flag is a boolean; it must not leak the word "False" into a
    # title if anyone ever puts one in a pattern.
    ("sigint", {"frequency": "146.940", "station_id": "W5ABC", "verbatim": False}),
]
out = [{"template": t, "fields": f, "expected": ft.compose_title(t, f)} for t, f in CASES]
dest = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
os.makedirs(os.path.dirname(dest), exist_ok=True)
with open(dest, "w") as fh:
    json.dump({"generated_from": "api/field_templates.py compose_title",
               "cases": out}, fh, indent=2)
print(f"{len(out)} cases -> {dest}")
for c in out:
    print(f"  {c['template']:11} {json.dumps(c['fields'])[:58]:60} -> {c['expected']!r}")
