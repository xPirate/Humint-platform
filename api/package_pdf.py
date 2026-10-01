"""PDF builders for the dossier, hotspot and executive-summary exports.

Shares every primitive with api/report_pdf.py — fonts, styles, the markdown
converter, the entity section, the numbered canvas and the doc template — so
all four exports a team can produce look like one document family. Anything
generally useful that gets added here belongs in report_pdf.py instead; this
module is only the three layouts.

CHARTS

The executive summary's charts are drawn with reportlab.graphics, which ships
inside reportlab itself. Matplotlib would be nicer to write and is a ~60MB
install with a compiled numpy underneath it — on a Raspberry Pi, in an image
this app is meant to stay small enough to deploy off a memory card, that is a
bad trade for two bar charts a month.
"""

import re
from datetime import datetime, timezone

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from report_pdf import (
    CREDIBILITY_LABELS,
    FONT_BOLD,
    FONT_REGULAR,
    PAGE_MARGIN,
    PAGE_SIZE,
    PDF_FOOTER_NOTE,
    header_label,
    _IntelDocTemplate,
    _NumberedCanvas,
    _entity_section,
    _format_value,
    _image_flowable,
    _kv_table,
    _plain,
    _styles,
    criticality_markup,
    markdown_to_flowables,
)

import io

# A colour-blind-safe qualitative ramp for the per-analyst charts. Deliberately
# not the app's own green/amber/red: those carry meaning here (precedence,
# status), and reusing them to mean "Priya" would be reading as a severity
# signal on a chart where nothing is severe.
ANALYST_COLORS = [
    colors.HexColor("#4477aa"), colors.HexColor("#ee6677"),
    colors.HexColor("#228833"), colors.HexColor("#ccbb44"),
    colors.HexColor("#66ccee"), colors.HexColor("#aa3377"),
    colors.HexColor("#bbbbbb"), colors.HexColor("#000000"),
]

RELATIONSHIP_WORDING = {
    "member_of": "member of", "employed_by": "employed by",
    "affiliated_with": "affiliated with", "associate_of": "associate of",
    "family_of": "family of", "spouse_of": "spouse of",
    "significant_of": "significant other of", "parent_of": "parent of",
    "child_of": "child of", "located_at": "located at",
    "present_at": "present at", "communicated_with": "communicated with",
    "reported_by": "reported by", "owns": "owns", "controls": "controls",
    "financed_by": "financed by", "in_conflict_with": "in conflict with",
    "has_member": "has member", "employs": "employs",
    "location_of": "location of", "controlled_by": "controlled by",
    "owned_by": "owned by", "finances": "finances",
}


def _wording(rel_type: str) -> str:
    return RELATIONSHIP_WORDING.get(rel_type or "", (rel_type or "").replace("_", " "))


def _doc(buf, title, author, ref=""):
    return _IntelDocTemplate(
        buf, pagesize=PAGE_SIZE,
        leftMargin=PAGE_MARGIN, rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN, bottomMargin=PAGE_MARGIN,
        title=title, author=author,
        header_label=header_label(), footer_note=PDF_FOOTER_NOTE,
        report_ref=ref,
    )


def _title_block(story, s, title, subtitle):
    story.append(_plain(title, s["title"]))
    story.append(Paragraph(subtitle, s["subtitle"]))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#0b5c2e")))
    story.append(Spacer(1, 10))


def _slug(text, limit=50):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit]


def _report_rows(reports, s, content_width, empty_note):
    """A table of reports with the precedence badge in colour.

    Used by every export that lists reporting. Precedence leads the row for the
    same reason it leads the report package's front page: it is what a reader
    scanning a list needs first, and it was previously invisible in print.
    """
    if not reports:
        return [_plain(empty_note, s["meta"])]
    rows = [[
        _plain("Precedence", s["cell_label"]),
        _plain("Report", s["cell_label"]),
        _plain("Status", s["cell_label"]),
        _plain("Filed", s["cell_label"]),
    ]]
    for r in reports:
        badge = criticality_markup(r.get("criticality"))
        rows.append([
            Paragraph(badge, s["cell"]) if badge else _plain("—", s["meta"]),
            _plain(r.get("title") or "(untitled)", s["cell"]),
            _plain((r.get("status") or "").capitalize(), s["cell"]),
            _plain(_format_value(r.get("created_at")), s["cell"]),
        ])
    t = Table(
        rows,
        colWidths=[content_width * 0.15, content_width * 0.5,
                   content_width * 0.13, content_width * 0.22],
        hAlign="LEFT", repeatRows=1,
    )
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#999999")),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#e8e8e8")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [t]


def _neighbour_line(rel, neighbour, contact, s):
    """One related record, worded from the subject's end.

    `reads_as` rather than `relationship_type`: on an organisation's page a
    stored `member_of` edge from a person reads as "has member", and printing
    the stored direction would have the dossier state the relationship
    backwards — quietly, and in a document nobody can check against the app.
    """
    name = (neighbour or {}).get("name") or rel.get("other_entity_name") or "(unknown)"
    bits = [f"<b>{_esc(name)}</b>"]
    kind = (neighbour or {}).get("entity_type") or rel.get("other_entity_type")
    if kind:
        bits.append(f"<font color='#666666'>{_esc(kind)}</font>")
    facts = []
    n = neighbour or {}
    for key, prefix in (("occupation", ""), ("org_type", ""), ("event_type", ""),
                        ("address", ""), ("environment", "environment: "),
                        ("alignment", "alignment: ")):
        if n.get(key):
            facts.append(f"{prefix}{n[key]}")
    # A vehicle reads as one phrase, not five fields: "white Ford Transit
    # panel van" is how somebody says it out loud, and the plate goes on the
    # end because that is the part you act on.
    if kind == "vehicle":
        described = " ".join(str(n[k]) for k in ("color", "make", "model", "style")
                             if n.get(k))
        if described:
            facts.append(described)
        if n.get("license_plate"):
            facts.append(f"plate {n['license_plate']}")
    if n.get("life_status") or n.get("disposition"):
        status = " / ".join(x for x in (n.get("life_status"), n.get("disposition")) if x)
        facts.append(status)
    if n.get("is_active") is False:
        facts.append("archived")
    if contact:
        facts.append(f"{contact.get('kind')}: {contact.get('value')}")
    line = " · ".join(bits)
    detail = f"<br/><font size='8' color='#666666'>{_esc(' · '.join(facts))}</font>" if facts else ""
    confidence = rel.get("confidence")
    conf = f" <font size='8' color='#666666'>({_esc(confidence)})</font>" if confidence else ""
    return Paragraph(
        f"{_esc(_wording(rel.get('reads_as') or rel.get('relationship_type')))} "
        f"&nbsp;{line}{conf}{detail}",
        s["listitem"],
    )


def _esc(text):
    return (str(text) if text is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Dossier
# ---------------------------------------------------------------------------

def build_dossier(data: dict, generated_by: str) -> bytes:
    entity = data["entity"]
    spec = data["shape_spec"]
    s = _styles()
    buf = io.BytesIO()
    doc = _doc(buf, f"{spec['label']}: {entity['name']}", generated_by, entity.get("id", ""))
    content_width = doc.width
    generated_at = datetime.now(timezone.utc)
    story = []

    _title_block(
        story, s, entity.get("name") or "Untitled",
        f"{spec['label'].upper()} · {spec['subtitle']} · "
        f"{(entity.get('entity_type') or '').capitalize()}",
    )

    header_rows = [
        ("Entity ID", entity.get("id")),
        ("Type", (entity.get("entity_type") or "").capitalize()),
        ("Status", "Active" if entity.get("is_active", True) else "Archived"),
        ("Related entities", str(len(entity.get("relationships") or []))),
        ("Reports referencing", str(len(data["reports"]))),
        ("Exported", f"{generated_at.strftime('%Y-%m-%d %H:%M UTC')} by {generated_by}"),
    ]
    table = _kv_table(header_rows, s, content_width)
    if table:
        story.extend([table, Spacer(1, 12)])

    story.append(Paragraph("Scope of this package", s["section"]))
    story.append(_plain(
        "The entity above, every entity directly related to it, and every report " "that mentions it. Connections more than one step away are not included.",
        s["meta"],
    ))
    story.append(Spacer(1, 10))

    # --- the subject itself ---
    story.append(Paragraph("The entity", s["section"]))
    story.extend(_entity_section(entity, s, content_width))

    # --- connections, grouped per the chosen shape ---
    story.append(Paragraph("Connections", s["section"]))
    rels = list(entity.get("relationships") or [])
    neighbours = data["neighbours"]
    neighbour_contacts = data["neighbour_contacts"]

    if not rels:
        story.append(_plain("No related entities.", s["meta"]))
    elif not spec["primary_groups"]:
        story.append(_plain(
            f"{len(rels)} related entit{'y' if len(rels) == 1 else 'ies'}, most recently discovered first.", s["meta"]))
        story.append(Spacer(1, 4))
        for rel in rels:
            story.append(_neighbour_line(
                rel, neighbours.get(rel.get("other_entity_id")),
                neighbour_contacts.get(rel.get("other_entity_id")), s))
    else:
        placed = set()
        for heading, types in spec["primary_groups"]:
            if not types:
                continue   # rendered elsewhere (status block on a target package)
            # Match on how the edge READS from this record, not how it is
            # stored — "has member" and "member_of" are the same fact, and an
            # organisation's People section must catch it from either side.
            group = [
                rel for rel in rels
                if (rel.get("reads_as") in types or rel.get("relationship_type") in types)
            ]
            if not group:
                continue
            placed.update(id(rel) for rel in group)
            story.append(Paragraph(f"{heading} ({len(group)})", s["h2"]))
            for rel in group:
                story.append(_neighbour_line(
                    rel, neighbours.get(rel.get("other_entity_id")),
                    neighbour_contacts.get(rel.get("other_entity_id")), s))
        rest = [rel for rel in rels if id(rel) not in placed]
        if rest:
            story.append(Paragraph(f"Other connections ({len(rest)})", s["h2"]))
            for rel in rest:
                story.append(_neighbour_line(
                    rel, neighbours.get(rel.get("other_entity_id")),
                    neighbour_contacts.get(rel.get("other_entity_id")), s))

    story.append(Spacer(1, 12))

    # --- reporting history ---
    story.append(Paragraph("Reports mentioning this entity", s["section"]))
    story.extend(_report_rows(
        data["reports"], s, content_width,
        "No report mentions this entity.",
    ))

    # --- the subject's own attachments ---
    images = [a for a in (entity.get("attachments") or [])
              if (a.get("mime_type") or "").startswith("image/")]
    if images:
        story.append(Spacer(1, 12))
        story.append(Paragraph("Attached images", s["section"]))
        for att in images:
            img = _image_flowable(att.get("storage_path"), content_width)
            if img is None:
                continue
            story.extend([Spacer(1, 4),
                          KeepTogether([img, _plain(att.get("filename", "image"), s["caption"])])])

    doc.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()


def dossier_filename(entity: dict, shape: str) -> str:
    kind = {"org": "org-report", "target": "target-package"}.get(shape, "dossier")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"humint-{kind}-{_slug(entity.get('name')) or 'entity'}-{stamp}.pdf"


# ---------------------------------------------------------------------------
# Hotspot
# ---------------------------------------------------------------------------

def build_hotspot_package(data: dict, generated_by: str) -> bytes:
    location = data["location"]
    working = data["working"]
    s = _styles()
    buf = io.BytesIO()
    doc = _doc(buf, f"Hotspot: {location['name']}", generated_by, location.get("id", ""))
    content_width = doc.width
    generated_at = datetime.now(timezone.utc)
    story = []

    total = (working or {}).get("total", 0)
    clears = total >= data["threshold"]
    _title_block(
        story, s, location.get("name") or "Location",
        f"HOTSPOT PACKAGE · {total} item(s) in {data['window_days']} days · "
        + ("above the threshold" if clears else "below the threshold"),
    )

    header_rows = [
        ("Location ID", location.get("id")),
        ("Window", f"{data['window_days']} days to {generated_at.strftime('%Y-%m-%d')}"),
        ("Items in window", str(total)),
        ("— of which reports", str((working or {}).get("report_count", 0))),
        ("— of which events", str((working or {}).get("event_count", 0))),
        ("Days with activity", str((working or {}).get("active_days", 0))),
        ("Most recent", _format_value((working or {}).get("last_seen"))),
        ("Threshold", f"{data['threshold']} items — this location is "
                      f"{'above' if clears else 'below'} it"),
        ("Exported", f"{generated_at.strftime('%Y-%m-%d %H:%M UTC')} by {generated_by}"),
    ]
    table = _kv_table(header_rows, s, content_width)
    if table:
        story.extend([table, Spacer(1, 12)])

    # --- how the number was arrived at ---
    story.append(Paragraph("How this number was arrived at", s["section"]))
    story.append(_plain(
        "A location counts as a hotspot when enough items point at it in the window: " "reports linked to it, Events linked to it, and reports about those Events.",
        s["meta"],
    ))
    story.append(Spacer(1, 4))
    story.append(_plain(
        "Every contributing item is listed below." if clears else "Every contributing item is listed below. This location is below the " "hotspot threshold.",
        s["meta"],
    ))
    story.append(Spacer(1, 10))

    if not clears and total == 0:
        story.append(Paragraph("Contributing items", s["section"]))
        story.append(_plain(
            f"Nothing points at this location in the last {data['window_days']} days. "
            "Widening the window may show activity outside it.", s["meta"]))

    # --- the reports ---
    if data["reports"]:
        story.append(Paragraph(f"Contributing reports ({len(data['reports'])})", s["section"]))
        story.extend(_report_rows(data["reports"], s, content_width, ""))
        story.append(Spacer(1, 12))

    # --- the events ---
    if data["events"]:
        story.append(Paragraph(f"Contributing events ({len(data['events'])})", s["section"]))
        rows = [[_plain("Event", s["cell_label"]), _plain("Type", s["cell_label"]),
                 _plain("Started", s["cell_label"]), _plain("Relevant until", s["cell_label"])]]
        for ev in data["events"]:
            rows.append([
                _plain(ev.get("name") or "", s["cell"]),
                _plain(ev.get("event_type") or "—", s["cell"]),
                _plain(_format_value(ev.get("started_at")), s["cell"]),
                _plain(_format_value(ev.get("expires_at")) or "—", s["cell"]),
            ])
        t = Table(rows, colWidths=[content_width * 0.44, content_width * 0.18,
                                   content_width * 0.19, content_width * 0.19],
                  hAlign="LEFT", repeatRows=1)
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#999999")),
            ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#e8e8e8")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.extend([t, Spacer(1, 12)])

    # --- the full chronology, every record ---
    records = (working or {}).get("records") or []
    if records:
        story.append(Paragraph(f"Full chronology ({len(records)} items)", s["section"]))
        story.append(_plain(
            "Everything counted above, most recent first.", s["meta"]))
        story.append(Spacer(1, 4))
        rows = [[_plain("When", s["cell_label"]), _plain("Kind", s["cell_label"]),
                 _plain("Item", s["cell_label"])]]
        for rec in records:
            rows.append([
                _plain(_format_value(rec.get("occurred")), s["cell"]),
                _plain((rec.get("kind") or "").capitalize(), s["cell"]),
                _plain(rec.get("label") or "", s["cell"]),
            ])
        t = Table(rows, colWidths=[content_width * 0.22, content_width * 0.13,
                                   content_width * 0.65],
                  hAlign="LEFT", repeatRows=1)
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#999999")),
            ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#e8e8e8")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.extend([t, Spacer(1, 12)])

    # --- who and what is tied to this place ---
    rels = location.get("relationships") or []
    if rels:
        story.append(PageBreak())
        story.append(Paragraph("Linked to this location", s["section"]))
        story.append(_plain(
            "Everything related to this location, whether or not it fell inside the "
            "window above. A hotspot is a place; this is who and what is at it.",
            s["meta"]))
        story.append(Spacer(1, 6))
        for rel in rels:
            story.append(_neighbour_line(
                rel, data["neighbours"].get(rel.get("other_entity_id")), None, s))

    # --- the location's own record ---
    story.append(Spacer(1, 12))
    story.append(Paragraph("The location", s["section"]))
    story.extend(_entity_section(location, s, content_width))

    doc.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()


def hotspot_filename(location: dict) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"humint-hotspot-{_slug(location.get('name')) or 'location'}-{stamp}.pdf"


# ---------------------------------------------------------------------------
# Executive summary
# ---------------------------------------------------------------------------

def _stacked_chart(weekly: dict, series_key: str, title: str, width, height=170):
    """Weeks along the bottom, one stacked band per analyst.

    Returns None when there is nothing to draw. An empty chart frame with no
    bars in it looks like a rendering failure, and a line of text saying the
    period was quiet is both smaller and more honest.
    """
    weeks = weekly["weeks"]
    analysts = [a for a in weekly["analysts"] if sum(a[series_key]) > 0]
    if not weeks or not analysts:
        return None

    d = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x = 34
    chart.y = 34
    chart.width = width - 150     # room for the legend on the right
    chart.height = height - 56
    chart.data = [a[series_key] for a in analysts]
    chart.categoryAxis.categoryNames = [w[5:] for w in weeks]   # MM-DD
    # The same font as the rest of the document. reportlab's graphics default
    # to Helvetica regardless of what the text styles use, and a chart set in a
    # different typeface from the page around it reads as pasted in.
    chart.categoryAxis.labels.fontName = FONT_REGULAR
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.angle = 0
    chart.categoryAxis.labels.dy = -2
    chart.valueAxis.valueMin = 0
    chart.valueAxis.labels.fontName = FONT_REGULAR
    chart.valueAxis.labels.fontSize = 7
    peak = max((sum(col) for col in zip(*chart.data)), default=0)
    chart.valueAxis.valueMax = max(peak, 1)
    # Whole numbers only: a y-axis offering "2.5 reports" is nonsense, and
    # reportlab will happily draw it on a short series.
    chart.valueAxis.valueStep = max(1, -(-peak // 5))
    chart.categoryAxis.style = "stacked"
    chart.groupSpacing = 6
    chart.barSpacing = 0
    for i, _ in enumerate(analysts):
        chart.bars[i].fillColor = ANALYST_COLORS[i % len(ANALYST_COLORS)]
        chart.bars[i].strokeColor = colors.white
        chart.bars[i].strokeWidth = 0.4
    d.add(chart)

    legend = Legend()
    legend.x = width - 108
    legend.y = height - 20
    legend.fontName = FONT_REGULAR
    legend.fontSize = 7.5
    legend.alignment = "right"
    legend.dxTextSpace = 4
    legend.dx = 6
    legend.dy = 6
    legend.deltay = 10
    legend.columnMaximum = 10
    legend.colorNamePairs = [
        (ANALYST_COLORS[i % len(ANALYST_COLORS)], a["username"])
        for i, a in enumerate(analysts)
    ]
    d.add(legend)
    d.add(String(0, height - 10, title, fontName=FONT_BOLD, fontSize=9,
                 fillColor=colors.HexColor("#333333")))
    return d


def _delta_text(current: int, previous: int) -> str:
    """The change, stated as a number and never as a word.

    "Up 12" is a fact. "Activity is increasing" is an assessment, and this
    export does not make those — a quiet month during a holiday and a quiet
    month because a source went dark produce the same bar chart.
    """
    if previous == 0 and current == 0:
        return "none in either period"
    if previous == 0:
        return f"{current} this period, none in the period before"
    diff = current - previous
    sign = "+" if diff > 0 else ""
    return f"{current} this period, {previous} the period before ({sign}{diff})"


def build_executive_summary(data: dict, generated_by: str) -> bytes:
    s = _styles()
    buf = io.BytesIO()
    days = data["window_days"]
    doc = _doc(buf, f"Executive summary — last {days} days", generated_by)
    content_width = doc.width
    generated_at = data["generated_at"]
    counts = data["counts"]
    story = []

    _title_block(
        story, s, f"Executive summary — last {days} days",
        f"Case activity to {generated_at.strftime('%Y-%m-%d')} · "
        f"{counts['reports']} reports · {counts['entities']} new entities",
    )

    story.append(Paragraph("What this is, and what it is not", s["section"]))
    story.append(_plain(
        "A count of what was done in the period, taken directly from the case file. "
        "Every figure carries the previous period beside it, because a number on its "
        "own cannot be read. Nothing here is an assessment of whether the situation "
        "is worsening or the team is performing: those questions need context this "
        "document does not have, and a summary that answered them from a bar chart "
        "would be inventing a judgement. The charts attribute work to named people — "
        "worth knowing before circulating this.",
        s["meta"],
    ))
    story.append(Spacer(1, 12))

    # --- headline counts ---
    story.append(Paragraph("Headline", s["section"]))
    headline = [
        ("Reports filed", _delta_text(counts["reports"], counts["reports_previous"])),
        ("Entities created", _delta_text(counts["entities"], counts["entities_previous"])),
        ("Relationships added", str(counts["relationships"])),
        ("Documents attached", str(counts["attachments"])),
        ("Still in draft", f"{counts['drafts']} of the {counts['reports']} filed this period"),
        ("Flash or Immediate", str(counts["urgent"])),
        ("Period", f"{days} days to {generated_at.strftime('%Y-%m-%d %H:%M UTC')}"),
        ("Prepared by", generated_by),
    ]
    table = _kv_table(headline, s, content_width)
    if table:
        story.extend([table, Spacer(1, 14)])

    # --- the charts ---
    story.append(Paragraph("Work over time, by analyst", s["section"]))
    chart_width = content_width
    reports_chart = _stacked_chart(data["weekly"], "reports",
                                   "Reports filed per week", chart_width)
    entities_chart = _stacked_chart(data["weekly"], "entities",
                                    "Entities created per week", chart_width)
    if reports_chart is None and entities_chart is None:
        story.append(_plain(
            "No reports or entities were created in this period.", s["meta"]))
    else:
        story.append(_plain(
            "Weekly, stacked by analyst. Weeks with no work are left empty rather "
            "than closed up, so a quiet fortnight reads as a gap.", s["meta"]))
        story.append(Spacer(1, 6))
        for chart in (reports_chart, entities_chart):
            if chart is not None:
                story.extend([chart, Spacer(1, 14)])

    # --- per-analyst totals ---
    analysts = data["weekly"]["analysts"]
    if analysts:
        rows = [[_plain("Analyst", s["cell_label"]), _plain("Reports", s["cell_label"]),
                 _plain("Entities", s["cell_label"])]]
        for a in sorted(analysts, key=lambda x: (-x["report_total"], x["username"])):
            rows.append([
                _plain(a["username"], s["cell"]),
                _plain(str(a["report_total"]), s["cell"]),
                _plain(str(a["entity_total"]), s["cell"]),
            ])
        t = Table(rows, colWidths=[content_width * 0.5, content_width * 0.25,
                                   content_width * 0.25], hAlign="LEFT", repeatRows=1)
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#999999")),
            ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#e8e8e8")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.extend([t, Spacer(1, 6)])
        story.append(_plain(
            "Counts of work produced, not of its value. A single well-sourced report "
            "can be worth a dozen routine notes, and this table cannot tell them "
            "apart — read it alongside the reporting, not instead of it.",
            s["meta"]))
        story.append(Spacer(1, 14))

    # No forced page break here. A summary of a quiet month is two pages, and a
    # hard break was printing half of page two as whitespace — which reads as a
    # missing section rather than as a section boundary.

    # --- composition ---
    story.append(Paragraph("What was added", s["section"]))
    by_type = data["entities_by_type"]
    if by_type:
        rows = [[_plain("Entity type", s["cell_label"]), _plain("Created", s["cell_label"])]]
        for row in by_type:
            rows.append([_plain(row["entity_type"].capitalize(), s["cell"]),
                         _plain(str(row["count"]), s["cell"])])
        t = Table(rows, colWidths=[content_width * 0.5, content_width * 0.5], hAlign="LEFT")
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#999999")),
            ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#e8e8e8")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.extend([t, Spacer(1, 12)])
    else:
        story.append(_plain("No new entities in the period.", s["meta"]))
        story.append(Spacer(1, 10))

    story.append(Paragraph("Reporting by precedence", s["section"]))
    by_crit = data["reports_by_criticality"]
    if by_crit:
        rows = [[_plain("Precedence", s["cell_label"]), _plain("Reports", s["cell_label"])]]
        for row in by_crit:
            badge = criticality_markup(row["criticality"])
            rows.append([
                Paragraph(badge, s["cell"]) if badge else _plain("Not set", s["meta"]),
                _plain(str(row["count"]), s["cell"]),
            ])
        t = Table(rows, colWidths=[content_width * 0.5, content_width * 0.5], hAlign="LEFT")
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#999999")),
            ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#e8e8e8")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.extend([t, Spacer(1, 4)])
        story.append(_plain(
            "Most reporting is deliberately left without a precedence. A file where "
            "everything is rated tells a reader nothing about what is urgent.",
            s["meta"]))
        story.append(Spacer(1, 12))
    else:
        story.append(_plain("No reports filed in the period.", s["meta"]))
        story.append(Spacer(1, 10))

    # --- hotspots ---
    story.append(Paragraph("Hotspots", s["section"]))
    if data["hotspot_window_days"] != days:
        story.append(_plain(
            f"Measured over {data['hotspot_window_days']} days — the nearest window "
            f"the hotspot calculation supports to this summary's {days}-day period.",
            s["meta"]))
        story.append(Spacer(1, 4))
    hotspots = data["hotspots"]
    if hotspots:
        rows = [[_plain("Location", s["cell_label"]), _plain("Items", s["cell_label"]),
                 _plain("Reports", s["cell_label"]), _plain("Events", s["cell_label"]),
                 _plain("Active days", s["cell_label"])]]
        for h in hotspots:
            rows.append([
                _plain(h["name"], s["cell"]), _plain(str(h["total"]), s["cell"]),
                _plain(str(h["report_count"]), s["cell"]),
                _plain(str(h["event_count"]), s["cell"]),
                _plain(str(h["active_days"]), s["cell"]),
            ])
        t = Table(rows, colWidths=[content_width * 0.4, content_width * 0.15,
                                   content_width * 0.15, content_width * 0.15,
                                   content_width * 0.15], hAlign="LEFT", repeatRows=1)
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#999999")),
            ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#e8e8e8")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.extend([t, Spacer(1, 4)])
        story.append(_plain(
            "Export a hotspot package for any of these from the location's page.",
            s["meta"]))
    else:
        story.append(_plain(
            "No location reached the hotspot threshold in this period.", s["meta"]))
    story.append(Spacer(1, 12))

    # --- what is outstanding ---
    na = data["needs_attention"]
    story.append(Paragraph("Outstanding at the end of the period", s["section"]))
    outstanding = [
        ("Flash / Immediate open", str(len(na["urgent_reports"]))),
        (f"Drafts untouched {na['stale_draft_days']}+ days", str(len(na["stale_drafts"]))),
        ("Events lapsing or lapsed", str(len(na["expiring_events"]))),
        ("People of concern", str(len(na["people_of_concern"]))),
    ]
    table = _kv_table(outstanding, s, content_width)
    if table:
        story.extend([table, Spacer(1, 8)])

    if na["urgent_reports"]:
        story.append(Paragraph("Urgent reporting still on the board", s["h2"]))
        story.extend(_report_rows(na["urgent_reports"], s, content_width, ""))
        story.append(Spacer(1, 8))

    if na["stale_drafts"]:
        story.append(Paragraph("Drafts nobody has come back to", s["h2"]))
        for d in na["stale_drafts"]:
            story.append(Paragraph(
                f"<b>{_esc(d['title'])}</b> "
                f"<font size='8' color='#666666'>last touched "
                f"{_esc(_format_value(d.get('updated_at')))}</font>",
                s["listitem"]))

    doc.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()
