"""Dashboard analytics: patterns an analyst would otherwise have to notice.

Every figure on this page is plain SQL over the case file. Nothing here calls
Ollama, needs an embedding model, or degrades if no model is installed — which
is the point. A team without the budget or the hardware to run a model should
still get pattern-spotting out of this app, and the extraction and correlation
queues are the only parts that were ever model-dependent.

That constraint is also why every panel is *explainable*. A hotspot row names
the exact records that made it hot; a rising name shows the two counts it was
derived from. An analyst can disagree with any number here by clicking through
to the rows behind it, which is not true of a similarity score.
"""

import os
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

import auth
from db import db_cursor

router = APIRouter(prefix="/api", tags=["insights"])

# The rolling window every "recently" figure is measured over. Offered as a
# choice rather than fixed because a fortnight is right for a fast-moving
# situation and far too short for a case that unfolds over months.
WINDOW_CHOICES = (7, 14, 30, 90)
DEFAULT_WINDOW = 14

# A Location needs this many linked records inside the window before it counts
# as a hotspot. Two is a coincidence; three is a pattern worth a look. Low
# enough to catch a quiet case, high enough that a busy week doesn't nominate
# every place in the file.
HOTSPOT_MIN_RECORDS = 3

# How far ahead "expiring soon" looks.
EXPIRING_SOON_DAYS = 7

# A draft nobody has touched in this long is probably forgotten rather than
# in progress.
STALE_DRAFT_DAYS = 14

TEMPO_WEEKS = 12


def _iso(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


@router.get("/dashboard/insights")
def dashboard_insights(
    days: int = Query(default=DEFAULT_WINDOW,
                      description=f"Rolling window in days; one of {WINDOW_CHOICES}"),
    user: dict = Depends(auth.require_user),
):
    if days not in WINDOW_CHOICES:
        raise HTTPException(status_code=400, detail=f"days must be one of {WINDOW_CHOICES}")

    with db_cursor() as cur:
        return {
            "window_days": days,
            "generated_at": datetime.now().astimezone().isoformat(),
            "needs_attention": _needs_attention(cur),
            "hotspots": _hotspots(cur, days),
            "tempo": _tempo(cur),
            "rising": _rising(cur, days),
        }


# ---------------------------------------------------------------------------
# Needs attention
# ---------------------------------------------------------------------------

def _needs_attention(cur) -> dict:
    """Everything with a clock on it, in one place.

    Deliberately four separate lists rather than one merged, ranked feed: they
    call for different actions, and an analyst scanning this wants to know
    whether they are about to read something urgent or something forgotten.
    """
    # Flash and Immediate, newest first. Draft or confirmed both count — a
    # Flash report still sitting in draft is *more* interesting, not less.
    cur.execute(
        """
        SELECT id, title, status, criticality, credibility_rating, created_at
        FROM reports
        WHERE criticality IN ('Flash', 'Immediate')
        ORDER BY CASE criticality WHEN 'Flash' THEN 0 ELSE 1 END, created_at DESC
        LIMIT 12
        """
    )
    urgent = [
        {"id": r[0], "title": r[1], "status": r[2], "criticality": r[3],
         "credibility_rating": r[4], "created_at": _iso(r[5])}
        for r in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT id, title, updated_at, criticality
        FROM reports
        WHERE status = 'draft' AND updated_at < now() - %s * interval '1 day'
        ORDER BY updated_at
        LIMIT 12
        """,
        (STALE_DRAFT_DAYS,),
    )
    stale_drafts = [
        {"id": r[0], "title": r[1], "updated_at": _iso(r[2]), "criticality": r[3]}
        for r in cur.fetchall()
    ]

    # Expiring soon, plus anything already lapsed in the last week — an Event
    # that went stale on Tuesday is exactly as actionable as one going stale on
    # Friday, and only showing the future half would hide it the day it matters.
    cur.execute(
        """
        SELECT e.id, e.name, d.expires_at, d.event_type
        FROM entities e JOIN event_details d ON d.entity_id = e.id
        WHERE e.is_active = TRUE
          AND d.expires_at IS NOT NULL
          AND d.expires_at BETWEEN CURRENT_DATE - 7 AND CURRENT_DATE + %s
        ORDER BY d.expires_at
        LIMIT 12
        """,
        (EXPIRING_SOON_DAYS,),
    )
    expiring = [
        {"id": r[0], "name": r[1], "expires_at": _iso(r[2]), "event_type": r[3],
         "already_expired": r[2] < date.today()}
        for r in cur.fetchall()
    ]

    # People whose situation is the reason someone is working this case.
    cur.execute(
        """
        SELECT e.id, e.name, p.disposition, p.life_status
        FROM entities e JOIN person_details p ON p.entity_id = e.id
        WHERE e.is_active = TRUE
          AND p.disposition IN ('Captured', 'Detained', 'Missing', 'Evading')
        ORDER BY CASE p.disposition
                   WHEN 'Captured' THEN 0 WHEN 'Missing' THEN 1
                   WHEN 'Detained' THEN 2 ELSE 3 END, e.name
        LIMIT 12
        """
    )
    people = [
        {"id": r[0], "name": r[1], "disposition": r[2], "life_status": r[3]}
        for r in cur.fetchall()
    ]

    return {
        "urgent_reports": urgent,
        "stale_drafts": stale_drafts,
        "stale_draft_days": STALE_DRAFT_DAYS,
        "expiring_events": expiring,
        "expiring_within_days": EXPIRING_SOON_DAYS,
        "people_of_concern": people,
    }


# ---------------------------------------------------------------------------
# Hotspots
# ---------------------------------------------------------------------------
#
# The three routes by which a record "touches" a Location, held once as SQL
# and used by both the dashboard panel and the hotspot export.
#
# Written twice, these two would agree on the day they were written and drift
# apart the first time either was tuned — and the drift would be silent, the
# exported package listing records that add up to a different number than the
# panel that sent the analyst to it. The panel's promise is that you can click
# a number and see the rows behind it; that promise only holds if the rows and
# the number come from the same query.
#
# Takes one parameter, the window in days.
_TOUCHES_SQL = """
    WITH window_bounds AS (
        SELECT now() - %s * interval '1 day' AS since
    ),
    touches AS (
        -- Reports linked directly to the location.
        SELECT rel.entity_id AS location_id, 'report' AS kind,
               r.id AS record_id, r.title AS record_label, r.created_at AS occurred
        FROM report_entities rel
        JOIN reports r ON r.id = rel.report_id
        WHERE r.created_at >= (SELECT since FROM window_bounds)

        UNION
        -- Events tied to the location by a relationship, in either direction.
        SELECT loc.id, 'event', ev.id, ev.name, COALESCE(d.started_at, ev.created_at)
        FROM entities loc
        JOIN relationships rl
          ON (rl.from_entity_id = loc.id AND rl.to_entity_id IS NOT NULL)
          OR (rl.to_entity_id = loc.id AND rl.from_entity_id IS NOT NULL)
        JOIN entities ev
          ON ev.id = CASE WHEN rl.from_entity_id = loc.id
                          THEN rl.to_entity_id ELSE rl.from_entity_id END
        LEFT JOIN event_details d ON d.entity_id = ev.id
        WHERE loc.entity_type = 'location' AND ev.entity_type = 'event'
          AND ev.is_active = TRUE
          AND COALESCE(d.started_at, ev.created_at) >= (SELECT since FROM window_bounds)

        UNION
        -- Reports about an Event that is tied to the location. This is the
        -- route the convoy case actually travels.
        SELECT loc.id, 'report', r.id, r.title, r.created_at
        FROM entities loc
        JOIN relationships rl
          ON rl.from_entity_id = loc.id OR rl.to_entity_id = loc.id
        JOIN entities ev
          ON ev.id = CASE WHEN rl.from_entity_id = loc.id
                          THEN rl.to_entity_id ELSE rl.from_entity_id END
         AND ev.entity_type = 'event'
        JOIN report_entities rel ON rel.entity_id = ev.id
        JOIN reports r ON r.id = rel.report_id
        WHERE loc.entity_type = 'location' AND ev.is_active = TRUE
          AND r.created_at >= (SELECT since FROM window_bounds)
    )
"""

# The aggregate over `touches`. `{having}` and `{limit}` are the only things
# the two callers differ by: the panel wants the top few locations that clear
# the threshold, the export wants one named location whether it clears it or
# not — an analyst is entitled to export a place that only has two records and
# see that for themselves.
_HOTSPOT_AGG_SQL = """
    SELECT loc.id, loc.name,
           COUNT(*) AS total,
           COUNT(*) FILTER (WHERE t.kind = 'event') AS event_count,
           COUNT(*) FILTER (WHERE t.kind = 'report') AS report_count,
           COUNT(DISTINCT t.occurred::date) AS active_days,
           MAX(t.occurred) AS last_seen,
           json_agg(json_build_object(
               'kind', t.kind, 'id', t.record_id, 'label', t.record_label,
               'occurred', t.occurred
           ) ORDER BY t.occurred DESC) AS records
    FROM touches t
    JOIN entities loc ON loc.id = t.location_id
    WHERE loc.entity_type = 'location' AND loc.is_active = TRUE
      {location_filter}
    GROUP BY loc.id, loc.name
    {having}
    ORDER BY total DESC, active_days DESC, last_seen DESC
    {limit}
"""


def _hotspot_row(row, record_cap=None) -> dict:
    records = row[7] or []
    for rec in records:
        rec["occurred"] = _iso(rec.get("occurred"))
    return {
        "id": row[0], "name": row[1], "total": row[2],
        "event_count": row[3], "report_count": row[4],
        "active_days": row[5], "last_seen": _iso(row[6]),
        # Capped for the panel; `total` is always the real figure.
        "records": records[:record_cap] if record_cap else records,
    }


def _hotspots(cur, days: int) -> list[dict]:
    """Locations that several records point at inside the window.

    The case this exists for: a filling station that turns up in four separate
    convoy reports over ten days. Nothing in any one of those reports says
    "this place matters" — the pattern is only visible across them, and it is
    the sort of thing that gets noticed weeks late or not at all.

    Counted per Location: Events related to it, plus Reports linked to it,
    plus Reports linked to those Events — that last one matters, because a
    convoy sighting is usually written up as a report about the *event*, and a
    location whose events are all being reported on is exactly what a hotspot
    is. Each contributing record comes back with the row so the number can be
    argued with.
    """
    cur.execute(
        _TOUCHES_SQL + _HOTSPOT_AGG_SQL.format(
            location_filter="", having="HAVING COUNT(*) >= %s", limit="LIMIT 8"),
        (days, HOTSPOT_MIN_RECORDS),
    )
    return [_hotspot_row(row, record_cap=6) for row in cur.fetchall()]


def hotspot_detail(cur, location_id: str, days: int):
    """One location's full hotspot working, for the export.

    Same counting as the panel, no record cap, and no minimum: a location that
    does not clear the threshold comes back with its real (small) numbers
    rather than as "not found", because "this place is quieter than you thought"
    is a useful thing for a package to be able to say.

    Returns None only when the location genuinely has no records in the window.
    """
    cur.execute(
        _TOUCHES_SQL + _HOTSPOT_AGG_SQL.format(
            location_filter="AND loc.id = %s", having="", limit=""),
        (days, location_id),
    )
    row = cur.fetchone()
    return _hotspot_row(row) if row else None


# ---------------------------------------------------------------------------
# Tempo and rising names
# ---------------------------------------------------------------------------

def _tempo(cur) -> dict:
    """Reports per week for the last twelve weeks.

    Weeks, not days: at this app's scale a daily count is mostly zeroes with
    noise on top, and the thing worth seeing is a fortnight that produced three
    times the usual traffic.
    """
    cur.execute(
        """
        SELECT weeks.week_start::date,
               COUNT(r.id) AS report_count
        FROM generate_series(
                 date_trunc('week', now()) - %s * interval '1 week',
                 date_trunc('week', now()),
                 interval '1 week'
             ) AS weeks(week_start)
        LEFT JOIN reports r
               ON r.created_at >= weeks.week_start
              AND r.created_at < weeks.week_start + interval '1 week'
        GROUP BY weeks.week_start
        ORDER BY weeks.week_start
        """,
        (TEMPO_WEEKS - 1,),
    )
    weeks = [{"week_start": _iso(r[0]), "reports": r[1]} for r in cur.fetchall()]
    counts = [w["reports"] for w in weeks]
    recent = counts[-1] if counts else 0
    # Compared against the median of the preceding weeks rather than the mean:
    # one exceptional week would drag a mean up and hide the next one.
    earlier = sorted(counts[:-1])
    median = earlier[len(earlier) // 2] if earlier else 0
    return {
        "weeks": weeks,
        "current": recent,
        "median": median,
        "peak": max(counts) if counts else 0,
    }


def _rising(cur, days: int) -> list[dict]:
    """Entities named in more reports this window than the one before it.

    "Rising" rather than "most mentioned": a name that is always busy is not
    news, and a name that went from nothing to four reports in a fortnight is.
    Both counts come back so the claim can be checked at a glance.
    """
    cur.execute(
        """
        WITH bounds AS (
            SELECT now() - %s * interval '1 day' AS current_start,
                   now() - 2 * %s * interval '1 day' AS previous_start
        ),
        counted AS (
            SELECT rel.entity_id,
                   COUNT(*) FILTER (
                       WHERE r.created_at >= (SELECT current_start FROM bounds)) AS current_count,
                   COUNT(*) FILTER (
                       WHERE r.created_at >= (SELECT previous_start FROM bounds)
                         AND r.created_at <  (SELECT current_start FROM bounds)) AS previous_count
            FROM report_entities rel
            JOIN reports r ON r.id = rel.report_id
            WHERE r.created_at >= (SELECT previous_start FROM bounds)
            GROUP BY rel.entity_id
        )
        SELECT e.id, e.name, e.entity_type, c.current_count, c.previous_count
        FROM counted c
        JOIN entities e ON e.id = c.entity_id
        WHERE e.is_active = TRUE
          AND c.current_count >= 2
          AND c.current_count > c.previous_count
        ORDER BY (c.current_count - c.previous_count) DESC, c.current_count DESC
        LIMIT 8
        """,
        (days, days),
    )
    return [
        {"id": r[0], "name": r[1], "entity_type": r[2],
         "current": r[3], "previous": r[4], "delta": r[3] - r[4]}
        for r in cur.fetchall()
    ]
