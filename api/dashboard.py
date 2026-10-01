"""Dashboard summary: the landing page's data in a single call, so the
frontend isn't firing five separate requests just to render "what's new."
Nothing here is dashboard-only data — it's the same tables/rows the rest of
the app already exposes, just shaped for an at-a-glance view with drill-down
links back into the real detail pages.

This endpoint never makes an outbound call of its own: every figure on the
dashboard is read from Postgres, which keeps this container's job the same as
everywhere else and means the landing page renders with no route out.
"""

from fastapi import APIRouter, Depends

import auth
from db import db_cursor

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard/summary")
def dashboard_summary(user: dict = Depends(auth.require_user)):
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM entities WHERE is_active = TRUE")
        entity_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM reports")
        report_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM extraction_suggestions WHERE status = 'pending'")
        pending_extraction = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM correlation_suggestions WHERE status = 'pending'")
        pending_correlation = cur.fetchone()[0]

        cur.execute(
            "SELECT id, title, status, credibility_rating, created_at, criticality "
            "FROM reports ORDER BY created_at DESC LIMIT 10"
        )
        recent_reports = [
            {"id": r[0], "title": r[1], "status": r[2], "credibility_rating": r[3],
             "created_at": r[4], "criticality": r[5]}
            for r in cur.fetchall()
        ]

        cur.execute(
            "SELECT id, entity_type, name, created_at FROM entities "
            "WHERE is_active = TRUE ORDER BY created_at DESC LIMIT 10"
        )
        recent_entities = [
            {"id": r[0], "entity_type": r[1], "name": r[2], "created_at": r[3]}
            for r in cur.fetchall()
        ]

    return {
        "counts": {
            "entities": entity_count,
            "reports": report_count,
            "pending_extraction": pending_extraction,
            "pending_correlation": pending_correlation,
        },
        "recent_reports": recent_reports,
        "recent_entities": recent_entities,
    }
