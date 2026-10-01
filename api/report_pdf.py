"""Report export: a formatted PDF intel package.

Exports one report as a self-contained document an analyst can hand to
someone who doesn't have access to this app — the report itself, plus the
case context that makes it readable on its own: a dossier for every entity
it's linked to, the photos attached to those entities, and its own
attachments. The point is that the recipient shouldn't have to ask "who is
R. Okonjo?" halfway down page two.

WHY REPORTLAB

The obvious alternative is HTML-to-PDF (WeasyPrint, wkhtmltopdf), which gives
prettier output for less layout code. Both need system libraries — pango and
cairo, or a bundled Chromium — and this app is expected to run on a Raspberry
Pi, where every apt dependency is image size and another thing that can fail
to build on arm. ReportLab is pure pip with no system packages at all, so the
export works anywhere the api container already runs.

FONTS

ReportLab's default Helvetica is Latin-1 only: a Cyrillic name, a Greek
letter, or even a stray typographic dash renders as a black box, silently, in
a document that's supposed to be a deliverable. So this registers a real
Unicode TTF at import, preferring DejaVu (installed in the api image, see the
Dockerfile) and falling back to the Bitstream Vera fonts ReportLab bundles.
Only if neither is loadable does it fall back to Helvetica, and in that case
text is transliterated rather than allowed to render as boxes — see
_sanitize_for_builtin_font.
"""

import io
import os
import re
from datetime import datetime, timezone

from PIL import Image as PILImage
from PIL import ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    Image,
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")
PAGE_SIZE = A4 if os.environ.get("PDF_PAGE_SIZE", "letter").lower() == "a4" else letter

# The organisation/case label printed in the page header. Deliberately generic
# by default rather than pretending to be a classification marking — see
# "Exports" in README.md.
#
# Set explicitly in .env, this wins outright — an organisation that has typed a
# specific running header wants that header, not one assembled from something
# else. Left unset, it is built from the instance name an admin chose in the
# app, so renaming the instance renames what its documents say they came from
# without anyone editing a file and rebuilding a container.
PDF_HEADER_LABEL_ENV = os.environ.get("PDF_HEADER_LABEL", "")


def header_label() -> str:
    if PDF_HEADER_LABEL_ENV:
        return PDF_HEADER_LABEL_ENV
    # Imported here rather than at module scope: branding imports db, db is
    # configured from the environment at import time, and report_pdf is
    # imported by tests that have no database at all.
    try:
        import branding
        return f"{branding.instance_name()} — Case Material"
    except Exception:
        return "HUMINT Platform — Case Material"


PDF_FOOTER_NOTE = os.environ.get(
    "PDF_FOOTER_NOTE",
    "Handle according to your own organisation's policy.",
)

MAX_IMAGE_HEIGHT = 3.6 * inch

# NATO/Admiralty labels. The database only stores the bare letter/number (see
# db/init.sql), which is fine inside the app where the meaning is one hover or
# one README away -- but an exported package goes to someone who has neither,
# so the grade is spelled out here.
CREDIBILITY_LABELS = {
    "1": "Confirmed by other sources",
    "2": "Probably true",
    "3": "Possibly true",
    "4": "Doubtful",
    "5": "Improbable",
    "6": "Truth cannot be judged",
}
RELIABILITY_LABELS = {
    "A": "Completely reliable",
    "B": "Usually reliable",
    "C": "Fairly reliable",
    "D": "Not usually reliable",
    "E": "Unreliable",
    "F": "Reliability cannot be judged",
}

# Detail-table columns that are server-computed bookkeeping rather than
# analyst-entered facts -- see the derived-column note in api/entities.py.
# They'd be noise in a printed dossier.
SKIP_DETAIL_FIELDS = {"geocode_status", "geocode_error", "geocoded_at",
                      # A Record's text is printed below its table: a table
                      # cell cannot break across pages, and a letter can be
                      # several pages long.
                      "body", "source_attachment_id"}

# Where the column name is not what an analyst would call the field. Only the
# ones that genuinely read wrong — "Expires at" sounds like a subscription,
# and the whole point of the field is that it is not the same as "ended".
DETAIL_FIELD_LABELS = {
    "expires_at": "Relevant until",
    "life_status": "Status",
    "record_kind": "Kind",
    "record_date": "Date on the document",
    "disposition": "Disposition",
    # "License plate" and "Plate region" are what the fallback would produce;
    # these read better in print and match the form's own labels.
    "license_plate": "Licence plate",
    "plate_region": "Plate issued by",
    "color": "Colour",
}


# ----------------------------------------------------------------------------
# Fonts
# ----------------------------------------------------------------------------

_FONT_CANDIDATES = [
    # (family label, regular, bold, italic, bold-italic)
    ("DejaVuSans", [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
    ]),
    ("Vera", None),  # resolved relative to the installed reportlab package below
]


def _vera_paths():
    import reportlab
    base = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
    return [
        os.path.join(base, "Vera.ttf"),
        os.path.join(base, "VeraBd.ttf"),
        os.path.join(base, "VeraIt.ttf"),
        os.path.join(base, "VeraBI.ttf"),
    ]


_MONO_CANDIDATES = [
    ("DejaVuSansMono", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
]


def _register_mono():
    """A distinct monospace face for code spans/blocks. Falls back to the
    built-in Courier, which is fine here: code and identifiers are
    overwhelmingly ASCII, so Courier's Latin-1-only coverage costs nothing in
    practice the way it would for body text."""
    for name, path in _MONO_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, path))
            return name
        except Exception:
            continue
    return "Courier"


def _register_fonts():
    """Returns (regular, bold, italic, mono) font names actually registered.
    Never raises: a missing font must degrade the look of an export, not break
    the endpoint."""
    mono = _register_mono()
    for family, paths in _FONT_CANDIDATES:
        paths = paths or _vera_paths()
        if not all(os.path.isfile(p) for p in paths):
            continue
        try:
            regular, bold, italic, bold_italic = (f"{family}", f"{family}-Bold", f"{family}-Italic", f"{family}-BoldItalic")
            pdfmetrics.registerFont(TTFont(regular, paths[0]))
            pdfmetrics.registerFont(TTFont(bold, paths[1]))
            pdfmetrics.registerFont(TTFont(italic, paths[2]))
            pdfmetrics.registerFont(TTFont(bold_italic, paths[3]))
            # Without this mapping, <b>/<i> inside a Paragraph would fall back
            # to a synthesized (smeared) bold rather than the real bold face.
            pdfmetrics.registerFontFamily(regular, normal=regular, bold=bold, italic=italic, boldItalic=bold_italic)
            return regular, bold, italic, mono
        except Exception:
            continue
    return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", mono


FONT_REGULAR, FONT_BOLD, FONT_ITALIC, FONT_MONO = _register_fonts()
USING_BUILTIN_FONT = FONT_REGULAR == "Helvetica"

# Only used when no TTF could be registered at all. Helvetica has no glyph for
# any of these, and ReportLab draws a filled box for a missing glyph -- an
# em-dash turning into a black rectangle in an exported intel package looks
# like a redaction, which is exactly the wrong impression.
_TRANSLITERATIONS = {
    "—": "--", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", "→": "->",
    "·": "-", "•": "*", " ": " ",
}


def _sanitize_for_builtin_font(text: str) -> str:
    if not USING_BUILTIN_FONT:
        return text
    for bad, good in _TRANSLITERATIONS.items():
        text = text.replace(bad, good)
    # Anything still outside Latin-1 would draw as a box; drop it rather than
    # print a wall of rectangles.
    return text.encode("latin-1", "ignore").decode("latin-1")


# ----------------------------------------------------------------------------
# Styles
# ----------------------------------------------------------------------------

def _styles():
    base = getSampleStyleSheet()
    s = {}
    s["title"] = ParagraphStyle(
        "IntelTitle", parent=base["Title"], fontName=FONT_BOLD, fontSize=19, leading=23,
        spaceAfter=2, alignment=0, textColor=colors.HexColor("#111111"),
    )
    s["subtitle"] = ParagraphStyle(
        "IntelSubtitle", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=9.5,
        leading=13, textColor=colors.HexColor("#666666"), spaceAfter=10,
    )
    s["body"] = ParagraphStyle(
        "IntelBody", parent=base["BodyText"], fontName=FONT_REGULAR, fontSize=10,
        leading=14.5, spaceAfter=7,
    )
    # List items get their own style: the body's paragraph spacing between
    # every bullet turns a short list into a page of scattered lines.
    s["listitem"] = ParagraphStyle(
        "IntelListItem", parent=s["body"], spaceAfter=2,
    )
    s["h1"] = ParagraphStyle(
        "IntelH1", parent=base["Heading1"], fontName=FONT_BOLD, fontSize=14, leading=18,
        spaceBefore=14, spaceAfter=6, textColor=colors.HexColor("#1a1a1a"),
    )
    s["h2"] = ParagraphStyle(
        "IntelH2", parent=base["Heading2"], fontName=FONT_BOLD, fontSize=11.5, leading=15,
        spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1a1a1a"),
    )
    s["h3"] = ParagraphStyle(
        "IntelH3", parent=base["Heading3"], fontName=FONT_BOLD, fontSize=10.5, leading=14,
        spaceBefore=8, spaceAfter=3, textColor=colors.HexColor("#333333"),
    )
    s["section"] = ParagraphStyle(
        "IntelSection", parent=base["Heading1"], fontName=FONT_BOLD, fontSize=12, leading=16,
        spaceBefore=4, spaceAfter=8, textColor=colors.HexColor("#0b5c2e"),
    )
    s["meta"] = ParagraphStyle(
        "IntelMeta", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=8.5, leading=11,
        textColor=colors.HexColor("#666666"),
    )
    s["caption"] = ParagraphStyle(
        "IntelCaption", parent=base["Normal"], fontName=FONT_ITALIC, fontSize=8.5, leading=11,
        textColor=colors.HexColor("#666666"), spaceBefore=3, spaceAfter=10, alignment=TA_CENTER,
    )
    s["quote"] = ParagraphStyle(
        "IntelQuote", parent=base["BodyText"], fontName=FONT_ITALIC, fontSize=10, leading=14,
        leftIndent=16, textColor=colors.HexColor("#444444"), spaceAfter=7,
    )
    s["code"] = ParagraphStyle(
        "IntelCode", parent=base["Code"], fontName=FONT_MONO, fontSize=8.5, leading=11,
        leftIndent=10, backColor=colors.HexColor("#f4f4f4"), spaceAfter=8,
    )
    s["cell"] = ParagraphStyle(
        "IntelCell", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=9, leading=12.5,
    )
    s["cell_label"] = ParagraphStyle(
        "IntelCellLabel", parent=base["Normal"], fontName=FONT_BOLD, fontSize=9, leading=12.5,
        textColor=colors.HexColor("#444444"),
    )
    return s


# ----------------------------------------------------------------------------
# Message precedence, in colour
# ----------------------------------------------------------------------------
#
# The UI colours the precedence badge and the export did not, so a Flash report
# printed exactly like a Routine one — the first thing a recipient needs off the
# front page was the one thing the page did not show them.
#
# These are the LIGHT-theme tokens from frontend/styles.css, not the dark ones.
# A PDF is printed on white, and the dark theme's #ff3b3b on white is thin and
# glaring where the light theme's #a01810 was already contrast-checked against
# exactly this background. Flash additionally gets a filled block rather than
# coloured text, because it is the one level that should be visible from across
# a room and the one that must survive a monochrome printer as a dark slab.
CRITICALITY_COLORS = {
    "Flash": {"fg": colors.HexColor("#ffffff"), "bg": colors.HexColor("#a01810")},
    "Immediate": {"fg": colors.HexColor("#a01810"), "bg": None},
    "Priority": {"fg": colors.HexColor("#7a4d00"), "bg": None},
    "Routine": {"fg": colors.HexColor("#44564b"), "bg": None},
}
CRITICALITY_UNSET_COLOR = colors.HexColor("#666666")


def criticality_color(criticality) -> colors.Color:
    """The text colour for a precedence, for callers drawing their own rows."""
    spec = CRITICALITY_COLORS.get(criticality or "")
    if not spec:
        return CRITICALITY_UNSET_COLOR
    # For Flash the foreground is white-on-red; as a bare text colour that is
    # invisible, so callers who cannot draw a filled block get the red instead.
    return spec["bg"] if spec["bg"] is not None else spec["fg"]


def criticality_markup(criticality) -> str:
    """The precedence badge as inline Paragraph markup.

    Inline rather than a flowable so it can sit in a table cell or mid-sentence
    without forcing a line of its own — which is exactly where a precedence
    needs to appear: at the front of a report row, and at the front of the
    subtitle on a package's first page.

    Flash gets a filled block and the rest coloured text, matching the pill in
    the app. An unset precedence returns the empty string rather than a fourth
    colour: "unset" is a real and deliberately common state in this app, and it
    should print as nothing rather than as a level of its own.
    """
    spec = CRITICALITY_COLORS.get(criticality or "")
    if not spec:
        return ""
    label = criticality.upper()
    fg = f"#{spec['fg'].hexval()[2:]}"
    if spec["bg"] is None:
        return f'<font color="{fg}"><b>{label}</b></font>'
    bg = f"#{spec['bg'].hexval()[2:]}"
    # The non-breaking spaces are the padding: an inline background has no box
    # model, and without them the block sits flush against the glyphs.
    return f'<font color="{fg}" backcolor="{bg}"><b>&nbsp;{label}&nbsp;</b></font>'


# ----------------------------------------------------------------------------
# Markdown -> flowables
# ----------------------------------------------------------------------------

def _escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_markdown(text: str) -> str:
    """Converts inline markdown to the small XML subset Paragraph understands.
    Escaping happens FIRST so that a literal < in a report body can never turn
    into markup; the tags added afterward are the only ones that survive."""
    out = _escape(_sanitize_for_builtin_font(text or ""))
    # Code spans before everything else: their contents shouldn't be scanned
    # for other markdown.
    out = re.sub(r"`([^`]+)`", rf'<font face="{FONT_MONO}">\1</font>', out)
    # Entity mentions (see MENTION_RE in api/reports.py) point at an in-app
    # hash route, which is a dead link in a printed document. They become bold
    # text instead — still visibly a named record, without inviting a click
    # that can't work. The entity's full dossier is in the package anyway.
    out = re.sub(r"\[([^\]]+)\]\(#/entities/[A-Za-z0-9_-]+\)", r"<b>\1</b>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<link href="\2" color="#0b5c2e">\1</link>', out)
    out = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<!\*)\*(?!\s)([^*]+?)\*", r"<i>\1</i>", out)
    out = re.sub(r"(?<![A-Za-z0-9_])__(.+?)__", r"<b>\1</b>", out)
    return out


def markdown_to_flowables(md: str, s: dict) -> list:
    """A deliberately small markdown subset: headings, paragraphs, bullet and
    numbered lists, blockquotes, fenced code, and horizontal rules, plus the
    inline marks above. Anything fancier degrades to plain text rather than
    being dropped — losing a sentence from an exported report because it used
    an unsupported construct would be much worse than rendering it plainly."""
    flow = []
    lines = (md or "").replace("\r\n", "\n").split("\n")
    i = 0
    para_buffer = []
    list_buffer = []
    list_ordered = False

    def flush_para():
        nonlocal para_buffer
        if para_buffer:
            flow.append(Paragraph(_inline_markdown(" ".join(para_buffer)), s["body"]))
            para_buffer = []

    def flush_list():
        nonlocal list_buffer
        if list_buffer:
            flow.append(ListFlowable(
                [ListItem(Paragraph(_inline_markdown(item), s["listitem"]), leftIndent=14) for item in list_buffer],
                bulletType="1" if list_ordered else "bullet",
                bulletFontName=FONT_REGULAR, bulletFontSize=9, leftIndent=16,
            ))
            list_buffer = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_para(); flush_list()
            i += 1
            code_lines = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            body = _sanitize_for_builtin_font("\n".join(code_lines))
            flow.append(Preformatted(body, s["code"]))
            continue

        if not stripped:
            flush_para(); flush_list()
            i += 1
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush_para(); flush_list()
            flow.append(Spacer(1, 4))
            flow.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#cccccc")))
            flow.append(Spacer(1, 6))
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush_para(); flush_list()
            level = len(heading.group(1))
            style = s["h1"] if level == 1 else s["h2"] if level == 2 else s["h3"]
            flow.append(Paragraph(_inline_markdown(heading.group(2)), style))
            i += 1
            continue

        if stripped.startswith(">"):
            flush_para(); flush_list()
            quote = [stripped.lstrip(">").strip()]
            i += 1
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            flow.append(Paragraph(_inline_markdown(" ".join(quote)), s["quote"]))
            continue

        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if bullet or numbered:
            flush_para()
            ordered = bool(numbered)
            if list_buffer and ordered != list_ordered:
                flush_list()
            list_ordered = ordered
            list_buffer.append((numbered or bullet).group(1))
            i += 1
            continue

        flush_list()
        para_buffer.append(stripped)
        i += 1

    flush_para()
    flush_list()
    return flow


# ----------------------------------------------------------------------------
# Building blocks
# ----------------------------------------------------------------------------

def _p(text, style):
    return Paragraph(_inline_markdown(str(text)), style)


def _plain(text, style):
    """Paragraph with NO markdown interpretation — for values out of the
    database (names, addresses, field values) where an asterisk is an
    asterisk, not emphasis."""
    return Paragraph(_escape(_sanitize_for_builtin_font(str(text))), style)


def _format_value(value) -> str:
    if value is None or value == "" or value == []:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    # Some callers hand over timestamps that have already been through
    # json/isoformat — the analytics layer returns ISO strings, not datetimes —
    # and printing "2026-09-10T02:45:36.738246+00:00" in a document a person
    # reads is not formatting, it is leaking the wire format onto the page.
    if isinstance(value, str):
        iso = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?"
                           r"(?:Z|[+-]\d{2}:?\d{2})?)?", value)
        if iso:
            return f"{iso.group(1)} {iso.group(2)} UTC" if iso.group(2) else iso.group(1)
    return str(value)


def _kv_table(rows: list, s: dict, content_width: float):
    """Two-column label/value table, used for the report header block and each
    entity's detail fields."""
    # A value may be a pre-built flowable (a Paragraph carrying its own markup,
    # or the coloured precedence badge) rather than plain text — those go in
    # untouched, since escaping them would print the tags.
    data = [
        [_plain(label, s["cell_label"]),
         value if hasattr(value, "wrap") else _plain(value, s["cell"])]
        for label, value in rows
        if value not in (None, "")
    ]
    if not data:
        return None
    label_w = 1.45 * inch
    table = Table(data, colWidths=[label_w, content_width - label_w], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#e8e8e8")),
    ]))
    return table


def _image_flowable(storage_path: str, content_width: float):
    """Loads an attachment image, corrects EXIF rotation, and scales it to fit
    the page. Returns None for anything unreadable — a corrupt or unsupported
    image must not take the whole export down with it."""
    abs_path = os.path.join(UPLOAD_DIR, storage_path or "")
    if not os.path.isfile(abs_path):
        return None
    try:
        with PILImage.open(abs_path) as img:
            img = ImageOps.exif_transpose(img)  # a phone photo is otherwise rotated in the PDF
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            width, height = img.size
            if not width or not height:
                return None
            scale = min(content_width / width, MAX_IMAGE_HEIGHT / height, 1.0)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
        return Image(buf, width=width * scale, height=height * scale)
    except Exception:
        return None


def _entity_section(entity: dict, s: dict, content_width: float) -> list:
    """One entity's dossier: identity, its detail fields, its relationships,
    and any photographs attached directly to it."""
    flow = [Paragraph(
        _escape(_sanitize_for_builtin_font(f"{entity['entity_type'].capitalize()}: {entity['name']}")),
        s["h2"],
    )]
    if not entity.get("is_active", True):
        flow.append(_plain("This entity is archived.", s["meta"]))

    if entity.get("description"):
        flow.append(_plain(entity["description"], s["body"]))

    rows = [("Entity ID", entity["id"])]
    for key, value in (entity.get("details") or {}).items():
        if key in SKIP_DETAIL_FIELDS:
            continue
        formatted = _format_value(value)
        if not formatted:
            continue
        label = DETAIL_FIELD_LABELS.get(key, key.replace("_", " ").capitalize())
        if key == "reliability_rating" and formatted in RELIABILITY_LABELS:
            formatted = f"{formatted} — {RELIABILITY_LABELS[formatted]}"
        if key == "expires_at" and entity.get("is_expired"):
            # A printed dossier has no badge to show, and a bare date leaves
            # the reader to work out today's date against it.
            formatted = f"{formatted} — lapsed"
        rows.append((label, formatted))
    table = _kv_table(rows, s, content_width)
    if table:
        flow.extend([Spacer(1, 3), table])

    body = ((entity.get("details") or {}).get("body") or "").strip()
    if entity.get("entity_type") == "record" and body:
        flow.append(Spacer(1, 7))
        flow.append(_plain("Full text", s["h3"]))
        for para in re.split(r"\n\s*\n", body):
            if para.strip():
                flow.append(_plain(" ".join(para.split()), s["body"]))

    # Only the rows marked preferred. The export is what leaves the building,
    # and a dossier that carried every number ever recorded against someone
    # would make sharing a report a bigger decision than it should be. The
    # full list stays in the app.
    contacts = [c for c in (entity.get("contacts") or []) if c.get("is_preferred")]
    if contacts:
        flow.append(Spacer(1, 7))
        flow.append(_plain("Contact", s["h3"]))
        contact_rows = []
        for c in contacts:
            label = c["kind"]
            if c.get("label"):
                label = f"{label} ({c['label']})"
            value = c["value"]
            if c.get("notes"):
                value = f"{value} — {c['notes']}"
            contact_rows.append((label, value))
        contact_table = _kv_table(contact_rows, s, content_width)
        if contact_table:
            flow.append(contact_table)

    rels = entity.get("relationships") or []
    if rels:
        flow.append(Spacer(1, 7))
        flow.append(_plain("Relationships", s["h3"]))
        items = []
        for r in rels:
            direction = "→" if r["direction"] == "outgoing" else "←"
            if USING_BUILTIN_FONT:
                direction = "->" if r["direction"] == "outgoing" else "<-"
            # `reads_as` is the wording for THIS end of the edge (see
            # entities.get_entity) — printing the stored direction here would
            # label an incoming "child_of" as though the subject were the
            # child, which is exactly backwards.
            wording = r.get("reads_as") or r["relationship_type"]
            items.append(ListItem(_plain(
                f"{direction} {wording} {direction} "
                f"{r['other_entity_type'].capitalize()}: {r['other_entity_name']} ({r['confidence']})",
                s["cell"],
            ), leftIndent=14))
        flow.append(ListFlowable(items, bulletType="bullet", bulletFontName=FONT_REGULAR,
                                 bulletFontSize=8, leftIndent=16))

    images = [a for a in (entity.get("attachments") or []) if (a.get("mime_type") or "").startswith("image/")]
    for att in images:
        img = _image_flowable(att.get("storage_path"), content_width)
        if img is None:
            continue
        caption = _plain(f"{att.get('filename', 'image')} — attached to {entity['name']}", s["caption"])
        # KeepTogether so a caption never orphans onto the page after its image.
        flow.extend([Spacer(1, 6), KeepTogether([img, caption])])

    flow.append(Spacer(1, 6))
    return flow


# ----------------------------------------------------------------------------
# Document template (header/footer, page numbers)
# ----------------------------------------------------------------------------

PAGE_MARGIN = 0.85 * inch


class _NumberedCanvas(rl_canvas.Canvas):
    """Draws "Page X of N" on every page.

    The total can't be known while the document is still being laid out, and
    multiBuild only re-runs when something (a TOC, say) declares an unresolved
    forward reference — which this document has none of, so it lays out
    exactly once and an "of N" computed during that pass is always wrong (it
    reads whatever the count was *so far*). The reliable approach, and the
    standard ReportLab recipe for this: buffer each page's state instead of
    emitting it, then at save() time — when the real total is simply the
    number of buffered pages — replay them all, stamping the correct number.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_page_number(total)
            super().showPage()
        super().save()

    def _draw_page_number(self, total):
        self.saveState()
        width, _height = self._pagesize
        self.setFont(FONT_REGULAR, 7.5)
        self.setFillColor(colors.HexColor("#777777"))
        self.drawRightString(width - PAGE_MARGIN, 0.45 * inch, f"Page {self._pageNumber} of {total}")
        self.restoreState()


class _IntelDocTemplate(BaseDocTemplate):
    """Adds the running header and the footer note. Page numbers are handled
    by _NumberedCanvas above, not here."""

    def __init__(self, *args, header_label="", footer_note="", report_ref="", **kwargs):
        super().__init__(*args, **kwargs)
        self.header_label = header_label
        self.footer_note = footer_note
        self.report_ref = report_ref
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="body")
        self.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=self._decorate)])

    def _decorate(self, canvas, doc):
        canvas.saveState()
        width, height = doc.pagesize

        canvas.setFont(FONT_REGULAR, 7.5)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(doc.leftMargin, height - 0.45 * inch, _sanitize_for_builtin_font(self.header_label))
        canvas.drawRightString(
            width - doc.rightMargin, height - 0.45 * inch,
            _sanitize_for_builtin_font(self.report_ref),
        )
        canvas.setStrokeColor(colors.HexColor("#dddddd"))
        canvas.setLineWidth(0.5)
        canvas.line(doc.leftMargin, height - 0.55 * inch, width - doc.rightMargin, height - 0.55 * inch)

        canvas.line(doc.leftMargin, 0.62 * inch, width - doc.rightMargin, 0.62 * inch)
        canvas.setFont(FONT_REGULAR, 7.5)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(doc.leftMargin, 0.45 * inch, _sanitize_for_builtin_font(self.footer_note))
        canvas.restoreState()


# ----------------------------------------------------------------------------
# The export itself
# ----------------------------------------------------------------------------

def build_report_package(report: dict, entities: list, generated_by: str) -> bytes:
    """report: as returned by reports._report_dict. entities: full dossiers as
    returned by entities.get_entity, one per linked entity."""
    s = _styles()
    buf = io.BytesIO()
    doc = _IntelDocTemplate(
        buf,
        pagesize=PAGE_SIZE,
        leftMargin=PAGE_MARGIN, rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN, bottomMargin=PAGE_MARGIN,
        title=report.get("title") or "Report",
        author=generated_by,
        header_label=header_label(),
        footer_note=PDF_FOOTER_NOTE,
        report_ref=report.get("id") or "",
    )
    content_width = doc.width
    generated_at = datetime.now(timezone.utc)

    story = []

    # --- title block ---
    story.append(_plain(report.get("title") or "Untitled report", s["title"]))
    credibility = report.get("credibility_rating")
    credibility_text = (
        f"{credibility} — {CREDIBILITY_LABELS.get(credibility, 'Unknown grade')}" if credibility else "Not rated"
    )
    # Precedence leads the subtitle when it is set: it is the first thing a
    # recipient needs off the front page, ahead of how credible it is.
    criticality = report.get("criticality")
    subtitle = f"{(report.get('status') or 'draft').capitalize()} report · Information credibility: {credibility_text}"
    if criticality:
        # Coloured, like the badge in the app. The precedence is the first thing
        # a recipient needs off the front page, and printing it in the same grey
        # as everything else was hiding it in plain sight.
        subtitle = f"{criticality_markup(criticality)} · {subtitle}"
    story.append(Paragraph(subtitle, s["subtitle"]))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#0b5c2e")))
    story.append(Spacer(1, 10))

    header_rows = [
        ("Report ID", report.get("id")),
        ("Status", (report.get("status") or "").capitalize()),
        ("Credibility", credibility_text),
        ("Criticality",
         Paragraph(f"{criticality_markup(criticality)} &mdash; message precedence", s["cell"])
         if criticality else "Not set"),
        ("Created", _format_value(report.get("created_at"))),
        ("Last updated", _format_value(report.get("updated_at"))),
        ("Linked entities", str(len(report.get("entities") or []))),
        ("Exported", f"{generated_at.strftime('%Y-%m-%d %H:%M UTC')} by {generated_by}"),
    ]
    table = _kv_table(header_rows, s, content_width)
    if table:
        story.extend([table, Spacer(1, 14)])

    # --- the report itself ---
    story.append(Paragraph("Report", s["section"]))
    body = markdown_to_flowables(report.get("body_markdown") or "", s)
    story.extend(body or [_plain("(This report has no body text.)", s["meta"])])

    # --- entity dossiers ---
    if entities:
        story.append(PageBreak())
        story.append(Paragraph("Linked entities", s["section"]))
        story.append(_plain(
            "Entities referenced by this report.",
            s["meta"],
        ))
        story.append(Spacer(1, 8))
        for entity in entities:
            story.extend(_entity_section(entity, s, content_width))

    # --- the report's own attachments ---
    attachments = report.get("attachments") or []
    if attachments:
        story.append(Spacer(1, 6))
        story.append(Paragraph("Attachments", s["section"]))
        images = [a for a in attachments if (a.get("mime_type") or "").startswith("image/")]
        others = [a for a in attachments if a not in images]

        for att in images:
            img = _image_flowable(att.get("storage_path"), content_width)
            if img is None:
                continue
            story.extend([
                Spacer(1, 4),
                KeepTogether([img, _plain(att.get("filename", "image"), s["caption"])]),
            ])

        if others:
            rows = [[_plain("File", s["cell_label"]), _plain("Type", s["cell_label"]), _plain("Text extracted", s["cell_label"])]]
            for att in others:
                rows.append([
                    _plain(att.get("filename", ""), s["cell"]),
                    _plain(att.get("mime_type") or "unknown", s["cell"]),
                    _plain(att.get("extraction_status") or "", s["cell"]),
                ])
            t = Table(rows, colWidths=[content_width * 0.5, content_width * 0.3, content_width * 0.2], hAlign="LEFT")
            t.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#999999")),
                ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#e8e8e8")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ]))
            story.extend([
                Spacer(1, 8),
                _plain("Files attached to this report but not reproduced above:", s["meta"]),
                Spacer(1, 4),
                t,
            ])

    doc.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()


def export_filename(report: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (report.get("title") or "report").lower()).strip("-")[:50]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"humint-report-{slug or 'report'}-{stamp}.pdf"
