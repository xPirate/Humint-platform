"""The seven shapes a field report can take, and what the console does with one.

`field_templates.json` beside this file is the contract; this module is the
console's half of it. The companion app ships a byte-identical copy of that
JSON compiled into its assets, because a handset is off-network until the
moment it uploads and cannot fetch anything.

The rule running through all of it: **a field report is never refused over its
shape.** An unknown template, a field key that was renamed two versions ago, a
value of a type nobody expected — every one of those renders as best it can
and is flagged for the person working the queue. Somebody stood outside and
typed that in. Losing it because a string arrived where a number was expected
would be the worst bug this feature could have.
"""

import json
import os
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "field_templates.json"), encoding="utf-8") as _f:
    REGISTRY = json.load(_f)

VERSION: int = REGISTRY["version"]
TEMPLATES: dict = {t["key"]: t for t in REGISTRY["templates"]}
CRITICALITY: list = REGISTRY["criticality"]

# Guards on what a device may put in the JSONB column. A submission over these
# is trimmed, never rejected -- see the module docstring.
MAX_FIELDS = 40
MAX_VALUE_CHARS = 5000
MAX_LIST_ITEMS = 25
MAX_KEY_CHARS = 64


def summary() -> dict:
    """What GET /api/intake/hello reports. Deliberately small: the app already
    holds the whole registry, and all it needs from the console is whether the
    two copies are the same one."""
    return {
        "version": VERSION,
        "templates": [{"key": t["key"], "label": t["label"]} for t in REGISTRY["templates"]],
    }


def clean_fields(raw) -> dict:
    """Trim a submitted field bag to something safe to store, keeping
    everything it can. Values are left in their submitted types so the
    renderer can tell a checkbox from the word "false"."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in list(raw.items())[:MAX_FIELDS]:
        if not isinstance(key, str) or not key.strip():
            continue
        key = key.strip()[:MAX_KEY_CHARS]
        if isinstance(value, str):
            value = value[:MAX_VALUE_CHARS]
        elif isinstance(value, list):
            value = [str(v)[:MAX_VALUE_CHARS] for v in value[:MAX_LIST_ITEMS]]
        elif isinstance(value, (int, float, bool)) or value is None:
            pass
        else:
            # An object, or something stranger. Keep it as text rather than
            # dropping it -- unreadable beats absent.
            value = json.dumps(value)[:MAX_VALUE_CHARS]
        if value is None or value == "" or value == []:
            continue
        out[key] = value
    return out


def _display(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def render(template_key, fields: dict) -> dict:
    """Lay a field bag out for a human: the template's own order first, then
    anything left over.

    The leftovers matter. They are what an app one version ahead of the
    console sends, and the point of listing them under their raw key is that
    the analyst can still read the value and act on it while somebody sorts
    out the version skew.
    """
    fields = fields or {}
    spec = TEMPLATES.get(template_key)
    rows, seen = [], set()
    if spec:
        for f in spec["fields"]:
            key = f["key"]
            if key in fields:
                seen.add(key)
                rows.append({"key": key, "label": f["label"],
                             "value": _display(fields[key]),
                             "long": f["type"] == "textarea", "known": True})
    extra = [{"key": k, "label": k.replace("_", " "), "value": _display(v),
              "long": isinstance(v, str) and len(v) > 120, "known": False}
             for k, v in fields.items() if k not in seen]
    return {
        "template": template_key,
        "label": spec["label"] if spec else (template_key or "Report"),
        "known_template": spec is not None,
        "rows": rows + extra,
        "unrecognised": [r["key"] for r in extra],
    }


def as_markdown(template_key, fields: dict) -> str:
    """The template's contents, for the body of the draft report that accepting
    one creates. A definition list rather than a table: it survives being
    edited by hand in the report editor, which a Markdown table does not."""
    laid_out = render(template_key, fields)
    if not laid_out["rows"]:
        return ""
    lines = [f"**{laid_out['label']} report**", ""]
    for row in laid_out["rows"]:
        if row["long"]:
            # A bullet list runs straight into the next paragraph unless
            # something separates them, and Markdown renders the result as
            # one more bullet.
            if lines and lines[-1].startswith("- "):
                lines.append("")
            lines.append(f"**{row['label']}**")
            lines.append("")
            lines.append(row["value"])
            lines.append("")
        else:
            lines.append(f"- **{row['label']}:** {row['value']}")
    return "\n".join(lines).rstrip() + "\n"


def entity_draft(template_key, fields: dict, *, lat=None, lng=None, observed_at=None):
    """The record this report is *about*, pre-filled — or None where the
    template describes an observation rather than a thing.

    A bearing is the clear case of the latter: it is a line, not a place, and
    three of them are a transmitter. Making a Location out of each one would
    fill the file with points that are not where anything is.
    """
    fields = fields or {}
    spec = TEMPLATES.get(template_key)
    if not spec or not spec.get("entity"):
        return None
    mapping = spec["entity"]

    specials = {"$lat": lat, "$lng": lng, "$observed_at": observed_at}
    details = {}
    for column, source in mapping["details"].items():
        value = specials.get(source) if source.startswith("$") else fields.get(source)
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, datetime):
            value = value.astimezone(timezone.utc).isoformat()
        details[column] = value

    # `name` may be a list of patterns, tried in order. A vehicle seen from
    # behind gives up a plate and nothing else, and "{color} {make} {model}"
    # fills to nothing -- which used to mean no record at all for the sighting
    # that had the single most identifying thing about it.
    patterns = mapping["name"]
    if isinstance(patterns, str):
        patterns = [patterns]
    name = next((n for n in (_fill(p, fields) for p in patterns) if n), "")
    if not name:
        # Every template with a mapping has a required field feeding its name,
        # but a report can arrive from an app that did not enforce that, and a
        # nameless entity is refused by the entities API.
        return None
    return {"entity_type": mapping["type"], "name": name[:256], "details": details}


def _fill(pattern: str, fields: dict) -> str:
    """Substitute {field} placeholders, dropping the ones with nothing behind
    them along with the separator that would have been left dangling."""
    out, buf, i = [], "", 0
    last_emitted = False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "{":
            end = pattern.find("}", i)
            if end == -1:
                buf += ch
                i += 1
                continue
            value = fields.get(pattern[i + 1:end])
            if value not in (None, "", []):
                out.append((buf, _display(value)))
                buf = ""
                last_emitted = True
            else:
                last_emitted = False
                # Drop the separator that preceded a missing value, so
                # "{color} {make} {model}" with no colour is not " Ford Ranger"
                # and "{a} · {b}" with no b does not end in a stray dot.
                buf = ""
            i = end + 1
        else:
            buf += ch
            i += 1
    # Trailing literal text belongs to the placeholder just before it:
    # "{bearing_deg}°" keeps its degree sign when there is a bearing and
    # loses it when there is not, so a frequency on its own is "146.94" and
    # not "146.94°".
    text = "".join(sep + val for sep, val in out) + (buf if last_emitted else "")
    # A separator whose left-hand value was dropped ends up at the front:
    # "{color} {make} {model} · {plate}" with only a plate would otherwise
    # come out as "· ABC 1234". A literal word is not a separator and stays,
    # so "{frequency} bearing {bearing_deg}°" still reads "bearing 212°".
    text = text.strip(" \t\n·-—–,:;|/")
    return " ".join(text.split())


def compose_title(template_key, fields: dict) -> str:
    """A fallback only. The app composes the title from this same pattern and
    sends it; this exists for anything posting to the intake by hand."""
    spec = TEMPLATES.get(template_key)
    if spec:
        title = _fill(spec["title"], fields)
        if title:
            return title[:200]
    for value in (fields or {}).values():
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return "Field report"
