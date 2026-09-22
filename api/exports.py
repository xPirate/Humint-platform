"""Export packages: dossiers, hotspot workings, and the executive summary.

Three exports beyond the per-report package that already existed:

  * **Dossier** — one entity and everything one hop from it. Three shapes
    (org / target / plain) that differ in how the same material is *arranged*,
    not in what is collected. See SHAPES below for why that is one code path
    and not three.
  * **Hotspot** — a Location, its hotspot arithmetic, and every record that
    produced it. Counted by the same SQL as the dashboard panel
    (insights.hotspot_detail), because a package that disagreed with the panel
    that sent you to it would be worse than no package.
  * **Executive summary** — a period's work, by the numbers and by analyst.

WHAT THESE ARE NOT

None of them is an assessment. They collect, arrange and count what is already
in the case file; no export writes a conclusion the analyst did not write, and
nothing here calls a model. An executive summary that said "activity is
escalating" would be inventing a judgement out of a bar chart, so it reports
counts and lets the reader draw it.

ACCESS

Any logged-in user can produce any of these. Every one of them contains only
records that user can already read in the app, so an export boundary would be
theatre — but each one is audited, because the difference between reading a
record on screen and carrying fifty of them out as a file is the whole reason
the audit trail exists.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response

import audit
import auth
import contacts
import entities
import insights
import package_pdf
import report_pdf
from db import db_cursor

router = APIRouter(prefix="/api/exports", tags=["exports"])

# The three framings a dossier can take. They select the same records — the
# entity, one hop of relationships, every report that mentions it, its
# attachments — and differ only in how those are grouped and headed.
#
# Kept as one code path because the alternative was two near-identical builders
# that would drift: a fix to how contacts print would land in one and not the
# other, and the difference would show up as a bug report six weeks later.
# What genuinely differs between an "org report" and a "target package" is the
# question the reader is holding, and that is a matter of grouping and wording.
SHAPES = {
    "org": {
        "label": "Organisation report",
        "subtitle": "Structure, membership and holdings",
        # Relationship types that answer "who belongs to this organisation".
        # Everything else still prints, under Other connections.
        "primary_groups": [
            ("People", ("member_of", "employed_by", "affiliated_with", "associate_of")),
            ("Holdings and control", ("controls", "owns", "located_at")),
            ("Standing and conflict", ("in_conflict_with", "financed_by")),
        ],
    },
    "target": {
        "label": "Target package",
        "subtitle": "Everything on file for one record",
        "primary_groups": [
            ("Status and disposition", ()),   # rendered from details, not edges
            ("Affiliations", ("member_of", "employed_by", "affiliated_with", "associate_of")),
            ("Family", ("spouse_of", "significant_of", "parent_of", "child_of", "family_of")),
            ("Places and movements", ("located_at", "present_at")),
            ("Contact and communications", ("communicated_with",)),
        ],
    },
    "plain": {
        "label": "Entity dossier",
        "subtitle": "The record and what it connects to",
        "primary_groups": [],   # one flat, chronological list
    },
}
DEFAULT_SHAPE = "plain"

# How many days an executive summary covers by default, and what it will accept.
EXEC_WINDOW_CHOICES = (7, 14, 30, 60, 90)
DEFAULT_EXEC_WINDOW = 30


def _header_safe_filename(filename: str) -> str:
    """Same treatment as reports._header_safe_filename — an entity name is
    freeform text and goes into a Content-Disposition header."""
    return "".join(c for c in (filename or "") if c.isprintable() and c not in '"\\;\r\n') or "export.pdf"


# ---------------------------------------------------------------------------
# Dossier
# ---------------------------------------------------------------------------

def _neighbour_details(cur, entity_ids: list[str]) -> dict:
    """The facts about a one-hop neighbour that belong in a package.

    An organisation report that lists "Ruth Okafor" and stops has told the
    reader nothing they could act on; the same line with her occupation and
    disposition is the point of the page. This pulls only the columns a
    printed dossier actually shows, for every neighbour at once, rather than
    calling get_entity per neighbour — on a well-connected organisation that
    would be forty round trips and forty times the data.
    """
    if not entity_ids:
        return {}
    cur.execute(
        """
        SELECT e.id, e.entity_type, e.name, e.description, e.is_active,
               p.occupation, p.life_status, p.disposition,
               COALESCE(p.alignment, o.alignment, s.alignment, v.alignment),
               o.org_type, l.environment,
               l.address, l.lat, l.lng,
               ev.event_type, ev.started_at, ev.expires_at,
               v.make, v.model, v.color, v.license_plate, v.style
        FROM entities e
        LEFT JOIN person_details p ON p.entity_id = e.id
        LEFT JOIN organization_details o ON o.entity_id = e.id
        LEFT JOIN location_details l ON l.entity_id = e.id
        LEFT JOIN event_details ev ON ev.entity_id = e.id
        LEFT JOIN vehicle_details v ON v.entity_id = e.id
        LEFT JOIN source_details s ON s.entity_id = e.id
        WHERE e.id = ANY(%s)
        """,
        (entity_ids,),
    )
    out = {}
    for r in cur.fetchall():
        out[r[0]] = {
            "id": r[0], "entity_type": r[1], "name": r[2], "description": r[3],
            "is_active": r[4],
            "occupation": r[5], "life_status": r[6], "disposition": r[7],
            # One column whatever the kind: the neighbour line asks "whose
            # side is this on", and a Person, an Organization, a Source and a
            # Vehicle all answer it from their own table.
            "alignment": r[8], "org_type": r[9], "environment": r[10],
            "address": r[11], "lat": r[12], "lng": r[13],
            "event_type": r[14], "started_at": r[15], "expires_at": r[16],
            # A vehicle's one-line identity. Not the whole record — this is
            # what a neighbour line has room for, and "white Ford panel van,
            # ABC-123" is the part somebody reading a dossier can act on.
            "make": r[17], "model": r[18], "color": r[19],
            "license_plate": r[20], "style": r[21],
        }
    return out


def _reports_for_entity(cur, entity_id: str) -> list[dict]:
    """Every report that mentions this entity, with the fields a package needs.

    get_entity already returns a report list, but without criticality — and
    precedence is exactly what a reader scanning a target package's reporting
    history needs to see first.
    """
    cur.execute(
        """
        SELECT rep.id, rep.title, rep.status, rep.criticality, rep.credibility_rating,
               rep.created_at, rep.updated_at, u.username
        FROM report_entities re
        JOIN reports rep ON rep.id = re.report_id
        LEFT JOIN users u ON u.id = rep.author_id
        WHERE re.entity_id = %s
        ORDER BY rep.created_at DESC
        """,
        (entity_id,),
    )
    return [
        {"id": r[0], "title": r[1], "status": r[2], "criticality": r[3],
         "credibility_rating": r[4], "created_at": r[5], "updated_at": r[6],
         "author": r[7]}
        for r in cur.fetchall()
    ]


def gather_dossier(entity_id: str, shape: str, user: dict) -> dict:
    """Everything a dossier prints, in one dict.

    One hop: the entity, each directly related entity with the facts worth
    printing about it, every report that mentions the entity, and the entity's
    own attachments. Deliberately not two hops — on the contested-metro sample
    a two-hop package from a large organization runs past sixty pages, and a package
    nobody reads to the end is not a better package.
    """
    entity = entities.get_entity(entity_id, user=user)

    with db_cursor() as cur:
        neighbour_ids = sorted({
            rel["other_entity_id"] for rel in entity["relationships"]
            if rel.get("other_entity_id")
        })
        neighbours = _neighbour_details(cur, neighbour_ids)
        linked_reports = _reports_for_entity(cur, entity_id)
        _attach_storage_paths(cur, entity["attachments"])
        # Contacts for the neighbours too: in an organisation report, "how do I
        # reach this person" is most of the value, and it lives one table over.
        neighbour_contacts = {}
        for nid, n in neighbours.items():
            if n["entity_type"] in contacts.CONTACTABLE_TYPES:
                # One line per neighbour, so only the first preferred contact —
                # a neighbour's full contact list belongs in that neighbour's
                # own dossier, not inlined into somebody else's.
                preferred = contacts.preferred_for_entity(cur, nid)
                if preferred:
                    neighbour_contacts[nid] = preferred[0]

    return {
        "entity": entity,
        "shape": shape,
        "shape_spec": SHAPES[shape],
        "neighbours": neighbours,
        "neighbour_contacts": neighbour_contacts,
        "reports": linked_reports,
    }


def _attach_storage_paths(cur, attachments: list) -> None:
    """As reports._attach_storage_paths. Duplicated rather than imported to
    keep exports.py from depending on the reports router module, which imports
    this one's sibling — the import cycle is the only reason."""
    ids = [a["id"] for a in attachments or []]
    if not ids:
        return
    cur.execute("SELECT id, storage_path FROM attachments WHERE id = ANY(%s)", (ids,))
    paths = dict(cur.fetchall())
    for att in attachments:
        att["storage_path"] = paths.get(att["id"])


@router.get("/entities/{entity_id}/dossier.pdf")
def export_dossier(
    entity_id: str,
    shape: str = Query(default=DEFAULT_SHAPE, description=f"One of {tuple(SHAPES)}"),
    user: dict = Depends(auth.require_user),
):
    if shape not in SHAPES:
        raise HTTPException(
            status_code=400,
            detail=f'shape must be one of {tuple(SHAPES)}',
        )
    data = gather_dossier(entity_id, shape, user)
    pdf_bytes = package_pdf.build_dossier(data, user["username"])
    filename = package_pdf.dossier_filename(data["entity"], shape)

    audit.record(
        "export.dossier", user=user, object_type="entity", object_id=entity_id,
        object_label=data["entity"]["name"],
        detail={
            "shape": shape,
            # Naming what left is the point of the entry: a dossier carries
            # every neighbour's details out with it, not just the subject's.
            "neighbours_included": sorted(data["neighbours"]),
            "reports_included": [r["id"] for r in data["reports"]],
            "size_bytes": len(pdf_bytes),
        },
    )
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_header_safe_filename(filename)}"'},
    )


# ---------------------------------------------------------------------------
# Hotspot
# ---------------------------------------------------------------------------

def gather_hotspot(location_id: str, days: int, user: dict) -> dict:
    """The location, its hotspot arithmetic, and every contributing record.

    The records come back from the same SQL the dashboard panel uses, so the
    package and the panel can never disagree about what made a place hot.
    """
    location = entities.get_entity(location_id, user=user)
    if location["entity_type"] != "location":
        raise HTTPException(
            status_code=400,
            detail="Hotspot packages are for Locations. Use the dossier export for other entities.",
        )

    with db_cursor() as cur:
        working = insights.hotspot_detail(cur, location_id, days)
        _attach_storage_paths(cur, location["attachments"])

        record_ids = [r["id"] for r in (working or {}).get("records", [])]
        report_ids = [r["id"] for r in (working or {}).get("records", []) if r["kind"] == "report"]
        event_ids = [r["id"] for r in (working or {}).get("records", []) if r["kind"] == "event"]

        reports = []
        if report_ids:
            cur.execute(
                """
                SELECT rep.id, rep.title, rep.status, rep.criticality,
                       rep.credibility_rating, rep.created_at, u.username
                FROM reports rep LEFT JOIN users u ON u.id = rep.author_id
                WHERE rep.id = ANY(%s)
                ORDER BY rep.created_at DESC
                """,
                (report_ids,),
            )
            reports = [
                {"id": r[0], "title": r[1], "status": r[2], "criticality": r[3],
                 "credibility_rating": r[4], "created_at": r[5], "author": r[6]}
                for r in cur.fetchall()
            ]

        events = []
        if event_ids:
            cur.execute(
                """
                SELECT e.id, e.name, e.description, d.event_type, d.started_at,
                       d.ended_at, d.expires_at
                FROM entities e LEFT JOIN event_details d ON d.entity_id = e.id
                WHERE e.id = ANY(%s)
                ORDER BY COALESCE(d.started_at, e.created_at) DESC
                """,
                (event_ids,),
            )
            events = [
                {"id": r[0], "name": r[1], "description": r[2], "event_type": r[3],
                 "started_at": r[4], "ended_at": r[5], "expires_at": r[6]}
                for r in cur.fetchall()
            ]

        # Who else is at this place. A hotspot package that lists the traffic
        # but not the people and organisations tied to the location leaves the
        # reader to go and look them up, which is the thing a package is for.
        neighbour_ids = sorted({
            rel["other_entity_id"] for rel in location["relationships"]
            if rel.get("other_entity_id")
        })
        neighbours = _neighbour_details(cur, neighbour_ids)

    return {
        "location": location,
        "working": working,
        "window_days": days,
        "threshold": insights.HOTSPOT_MIN_RECORDS,
        "reports": reports,
        "events": events,
        "neighbours": neighbours,
        "record_count": len(record_ids),
    }


@router.get("/hotspots/{location_id}/package.pdf")
def export_hotspot(
    location_id: str,
    days: int = Query(default=insights.DEFAULT_WINDOW,
                      description=f"Window in days; one of {insights.WINDOW_CHOICES}"),
    user: dict = Depends(auth.require_user),
):
    if days not in insights.WINDOW_CHOICES:
        raise HTTPException(status_code=400,
                            detail=f"days must be one of {insights.WINDOW_CHOICES}")
    data = gather_hotspot(location_id, days, user)
    pdf_bytes = package_pdf.build_hotspot_package(data, user["username"])
    filename = package_pdf.hotspot_filename(data["location"])

    audit.record(
        "export.hotspot", user=user, object_type="entity", object_id=location_id,
        object_label=data["location"]["name"],
        detail={
            "window_days": days,
            "records_included": data["record_count"],
            "reports_included": [r["id"] for r in data["reports"]],
            "events_included": [e["id"] for e in data["events"]],
            "size_bytes": len(pdf_bytes),
        },
    )
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_header_safe_filename(filename)}"'},
    )


# ---------------------------------------------------------------------------
# Executive summary
# ---------------------------------------------------------------------------

def _period_counts(cur, days: int) -> dict:
    """Headline counts for the period, each with the previous period beside it.

    A bare "41 reports" tells a team lead nothing — 41 against 12 last month is
    a different conversation from 41 against 58. Every headline figure carries
    its own comparison for that reason, and none of them carries an adjective.
    """
    cur.execute(
        """
        SELECT
          (SELECT count(*) FROM reports WHERE created_at >= now() - %(d)s * interval '1 day'),
          (SELECT count(*) FROM reports
             WHERE created_at >= now() - 2 * %(d)s * interval '1 day'
               AND created_at <  now() - %(d)s * interval '1 day'),
          (SELECT count(*) FROM entities WHERE created_at >= now() - %(d)s * interval '1 day'),
          (SELECT count(*) FROM entities
             WHERE created_at >= now() - 2 * %(d)s * interval '1 day'
               AND created_at <  now() - %(d)s * interval '1 day'),
          (SELECT count(*) FROM relationships WHERE created_at >= now() - %(d)s * interval '1 day'),
          (SELECT count(*) FROM attachments WHERE uploaded_at >= now() - %(d)s * interval '1 day'),
          (SELECT count(*) FROM reports
             WHERE status = 'draft' AND created_at >= now() - %(d)s * interval '1 day'),
          (SELECT count(*) FROM reports
             WHERE criticality IN ('Flash','Immediate')
               AND created_at >= now() - %(d)s * interval '1 day')
        """,
        {"d": days},
    )
    r = cur.fetchone()
    return {
        "reports": r[0], "reports_previous": r[1],
        "entities": r[2], "entities_previous": r[3],
        "relationships": r[4], "attachments": r[5],
        "drafts": r[6], "urgent": r[7],
    }


def _entities_by_type(cur, days: int) -> list[dict]:
    cur.execute(
        """
        SELECT entity_type, count(*)
        FROM entities
        WHERE created_at >= now() - %s * interval '1 day'
        GROUP BY entity_type ORDER BY count(*) DESC, entity_type
        """,
        (days,),
    )
    return [{"entity_type": r[0], "count": r[1]} for r in cur.fetchall()]


def _reports_by_criticality(cur, days: int) -> list[dict]:
    cur.execute(
        """
        SELECT criticality, count(*)
        FROM reports
        WHERE created_at >= now() - %s * interval '1 day'
        GROUP BY criticality
        ORDER BY CASE criticality WHEN 'Flash' THEN 0 WHEN 'Immediate' THEN 1
                                  WHEN 'Priority' THEN 2 WHEN 'Routine' THEN 3
                                  ELSE 4 END
        """,
        (days,),
    )
    return [{"criticality": r[0], "count": r[1]} for r in cur.fetchall()]


def _weekly_by_analyst(cur, days: int) -> dict:
    """Reports filed and entities created, per analyst, per week.

    Weeks rather than days because a daily series over thirty days is thirty
    columns of mostly one and zero, which reads as noise rather than as work.
    generate_series supplies the empty weeks, so a quiet fortnight shows as a
    gap in the chart instead of silently closing up.
    """
    cur.execute(
        """
        WITH weeks AS (
            SELECT generate_series(
                date_trunc('week', now() - %(d)s * interval '1 day'),
                date_trunc('week', now()),
                interval '1 week'
            ) AS week_start
        ),
        people AS (
            SELECT DISTINCT u.id, u.username
            FROM users u
            WHERE EXISTS (SELECT 1 FROM reports r
                          WHERE r.author_id = u.id
                            AND r.created_at >= now() - %(d)s * interval '1 day')
               OR EXISTS (SELECT 1 FROM entities e
                          WHERE e.created_by = u.id
                            AND e.created_at >= now() - %(d)s * interval '1 day')
        )
        SELECT w.week_start, p.username,
               (SELECT count(*) FROM reports r
                 WHERE r.author_id = p.id
                   AND date_trunc('week', r.created_at) = w.week_start),
               (SELECT count(*) FROM entities e
                 WHERE e.created_by = p.id
                   AND date_trunc('week', e.created_at) = w.week_start)
        FROM weeks w CROSS JOIN people p
        ORDER BY w.week_start, p.username
        """,
        {"d": days},
    )
    rows = cur.fetchall()
    weeks, analysts = [], {}
    for week_start, username, report_count, entity_count in rows:
        iso = week_start.date().isoformat()
        if iso not in weeks:
            weeks.append(iso)
        a = analysts.setdefault(username, {"reports": {}, "entities": {}})
        a["reports"][iso] = report_count
        a["entities"][iso] = entity_count

    # Unattributed work is counted separately rather than dropped. Records
    # imported in bulk or created by a since-deleted account have no author,
    # and a chart that silently omitted them would understate the period.
    cur.execute(
        """
        SELECT date_trunc('week', created_at)::date, count(*)
        FROM reports
        WHERE author_id IS NULL AND created_at >= now() - %s * interval '1 day'
        GROUP BY 1
        """,
        (days,),
    )
    unattributed_reports = {r[0].isoformat(): r[1] for r in cur.fetchall()}
    cur.execute(
        """
        SELECT date_trunc('week', created_at)::date, count(*)
        FROM entities
        WHERE created_by IS NULL AND created_at >= now() - %s * interval '1 day'
        GROUP BY 1
        """,
        (days,),
    )
    unattributed_entities = {r[0].isoformat(): r[1] for r in cur.fetchall()}
    if unattributed_reports or unattributed_entities:
        analysts["(unattributed)"] = {
            "reports": unattributed_reports,
            "entities": unattributed_entities,
        }

    return {
        "weeks": weeks,
        "analysts": [
            {
                "username": name,
                "reports": [series["reports"].get(w, 0) for w in weeks],
                "entities": [series["entities"].get(w, 0) for w in weeks],
                "report_total": sum(series["reports"].values()),
                "entity_total": sum(series["entities"].values()),
            }
            for name, series in sorted(analysts.items())
        ],
    }


def gather_executive(days: int, user: dict) -> dict:
    with db_cursor() as cur:
        counts = _period_counts(cur, days)
        by_type = _entities_by_type(cur, days)
        by_criticality = _reports_by_criticality(cur, days)
        weekly = _weekly_by_analyst(cur, days)
        # The window a hotspot can be measured over is a fixed set; an
        # executive period of 60 days has no hotspot window of its own, so the
        # nearest supported one is used and the summary says which.
        hotspot_days = min(insights.WINDOW_CHOICES,
                           key=lambda choice: (abs(choice - days), choice))
        hotspots = insights._hotspots(cur, hotspot_days)
        attention = insights._needs_attention(cur)
    return {
        "window_days": days,
        "generated_at": datetime.now(timezone.utc),
        "counts": counts,
        "entities_by_type": by_type,
        "reports_by_criticality": by_criticality,
        "weekly": weekly,
        "hotspots": hotspots,
        "hotspot_window_days": hotspot_days,
        "needs_attention": attention,
    }


@router.get("/executive-summary.pdf")
def export_executive(
    days: int = Query(default=DEFAULT_EXEC_WINDOW,
                      description=f"Period in days; one of {EXEC_WINDOW_CHOICES}"),
    user: dict = Depends(auth.require_user),
):
    if days not in EXEC_WINDOW_CHOICES:
        raise HTTPException(status_code=400,
                            detail=f"days must be one of {EXEC_WINDOW_CHOICES}")
    data = gather_executive(days, user)
    pdf_bytes = package_pdf.build_executive_summary(data, user["username"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"humint-executive-summary-{days}d-{stamp}.pdf"

    audit.record(
        "export.executive", user=user, object_type="summary", object_id=None,
        object_label=f"Executive summary, {days} days",
        detail={
            "window_days": days,
            # Named because a summary attributes work to individuals: who
            # appeared in it is the part someone might later ask about.
            "analysts_named": [a["username"] for a in data["weekly"]["analysts"]],
            "size_bytes": len(pdf_bytes),
        },
    )
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
