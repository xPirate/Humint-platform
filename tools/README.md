# tools/

Standalone scripts. Nothing here runs as part of the app — no container builds
them, no service imports them. They exist for one-off jobs where making the app
do it would mean the app carrying a dependency it needs once.

Standard library only. `python3 <script>` on any machine, no virtualenv.

## `osm_places_to_csv.py`

Builds a **Location** import CSV from OpenStreetMap: police stations, fire
stations, hospitals and jails in a bounding box, in the exact column order the
app's Location template declares.

```bash
# Oklahoma City metro, the four categories, everything marked Permissive
python3 tools/osm_places_to_csv.py -o okc-emergency.csv

# Somewhere else
python3 tools/osm_places_to_csv.py --bbox 32.6,-97.6,33.0,-96.6 -o dfw.csv

# Add ambulance stations
python3 tools/osm_places_to_csv.py --categories police,fire,hospital,jail,ems -o all.csv

# Leave the assessment to the analyst instead of marking everything Permissive
python3 tools/osm_places_to_csv.py --environment "" -o okc.csv

# Months later: only what is new
python3 tools/osm_places_to_csv.py --skip-ids-from okc-emergency.csv -o new.csv
```

Then **Entities → Import**, choose Location, upload the file.

### If the machine cannot reach the Overpass API

Some networks block it, and an air-gapped instance blocks everything.

```bash
python3 tools/osm_places_to_csv.py --print-query
```

Paste that into <https://overpass-turbo.eu>, run it, **Export → GeoJSON**, then:

```bash
python3 tools/osm_places_to_csv.py --from-file export.geojson -o okc-emergency.csv
```

Coordinates from that route are a shade less precise than the API's — a
building outline is averaged to its centre here rather than by Overpass — but
they land within a few metres, which is well inside what a map pin means.

### What it drops, and why

- **Anything with no usable name.** A case file with thirty records called
  "Fire Station" is worse than a smaller one. A station tagged only with an
  operator and a number becomes "Oklahoma City Fire Department Station 12",
  which is what people actually call it; one with nothing at all is skipped and
  counted in the summary.
- **Anything with no coordinates**, which is the whole point of the import.
- **The second copy of a facility mapped twice** — often a node *and* a
  building outline. Both OSM ids are recorded on the surviving row, so a later
  `--skip-ids-from` run recognises either of them.

The summary at the end says how many of each, so a number that looks wrong is
visible rather than silent.

### Re-importing

Each row's description carries the OpenStreetMap ids it came from. **The app's
importer does not deduplicate** — importing the same file twice creates every
record twice — so use `--skip-ids-from <the previous CSV>` and keep the file
you imported.

### Licence and accuracy

OpenStreetMap data is published under the **Open Database License**. Anything
derived from it carries that licence, and the attribution belongs on any map or
report that shows it: *© OpenStreetMap contributors*. Each row's description
records this.

It is a volunteer map, not a government roster. Coverage of hospitals in a US
metro is very good and fire and police coverage is generally good, but a
station can be missing, closed, or sited approximately. **Treat the result as a
starting set to correct, not as authoritative** — particularly for anything
somebody would drive to in an emergency.
