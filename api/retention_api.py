"""Admin endpoints for the retention policy.

The policy logic itself is in retention.py, which the worker runs on the same
code. This file is the part that only makes sense inside a request: reading
the page, saving it, previewing what it would do, and running it on demand.

Everything here is admin-only. Setting a window is a decision about the whole
file, and the one non-admin thing in the feature — putting a hold on a single
record — lives with the record, in entities.py, where the analyst is.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import audit
import auth
import retention
from db import db_cursor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["retention"])

# A year of slack either side of anything sensible. The low end stops a typo
# from setting a one-day window across a live file; the high end is about
# twenty years, past which "never" is the honest setting and is available as
# a blank field.
MIN_DAYS = 7
MAX_DAYS = 7300


class RetentionSettings(BaseModel):
    enabled: bool = False
    grace_days: int = Field(default=14, ge=0, le=365)
    exempt_linked: bool = True
    exempt_hostile: bool = True
    # {entity_type: days or None}. Absent types are left as they are, so a
    # partial save from an older frontend cannot silently clear a window.
    policy: dict[str, int | None] = Field(default_factory=dict)


def _clean_policy(raw: dict) -> dict:
    out = {}
    for entity_type, days in raw.items():
        if entity_type not in retention.ENTITY_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown entity type: {entity_type}")
        if days is None or days == 0 or days == "":
            out[entity_type] = None
            continue
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail=f"{entity_type}: that isn't a number of days.")
        if not (MIN_DAYS <= days <= MAX_DAYS):
            raise HTTPException(
                status_code=400,
                detail=(f"{entity_type}: a window has to be between {MIN_DAYS} and "
                        f"{MAX_DAYS} days, or blank for never."))
        out[entity_type] = days
    return out


def _page(cur) -> dict:
    settings = retention.load_settings(cur)
    policy = retention.load_policy(cur)
    return {
        "enabled": bool(settings["retention_enabled"]),
        "grace_days": settings["retention_grace_days"],
        "exempt_linked": bool(settings["retention_exempt_linked"]),
        "exempt_hostile": bool(settings["retention_exempt_hostile"]),
        "last_run": settings["retention_last_run"].isoformat()
                    if settings["retention_last_run"] else None,
        "policy": policy,
        "survey": retention.survey(cur, policy, settings),
        "limits": {"min_days": MIN_DAYS, "max_days": MAX_DAYS},
    }


@router.get("/retention")
def get_retention(user: dict = Depends(auth.require_admin)):
    """The page as it stands, including what the saved policy would catch now."""
    with db_cursor() as cur:
        return _page(cur)


@router.post("/retention/preview")
def preview_retention(payload: RetentionSettings, user: dict = Depends(auth.require_admin)):
    """What a policy would do, before it is saved.

    An administrator setting these numbers is guessing about a file they
    cannot see the shape of. Answering "180 days on Communications catches
    412 records, and 900 of them are spared by the linked exemption" turns
    the guess into a decision.
    """
    policy = _clean_policy(payload.policy)
    with db_cursor() as cur:
        stored = retention.load_policy(cur)
        stored.update(policy)
        settings = {
            "retention_enabled": payload.enabled,
            "retention_grace_days": payload.grace_days,
            "retention_exempt_linked": payload.exempt_linked,
            "retention_exempt_hostile": payload.exempt_hostile,
        }
        survey = retention.survey(cur, stored, settings)
        # Same numbers with each exemption switched off, so the page can say
        # what each one is costing rather than only that it is on.
        without = {}
        for flag in ("retention_exempt_linked", "retention_exempt_hostile"):
            if settings[flag]:
                alt = dict(settings, **{flag: False})
                without[flag] = retention.survey(cur, stored, alt)["totals"]["due"]
    return {"survey": survey, "due_without_exemption": without}


@router.put("/retention")
def put_retention(payload: RetentionSettings, user: dict = Depends(auth.require_admin)):
    policy = _clean_policy(payload.policy)
    with db_cursor(commit=True) as cur:
        before = retention.load_settings(cur)
        before_policy = retention.load_policy(cur)
        retention.save_policy(cur, policy)
        cur.execute(
            "INSERT INTO app_settings (id, retention_enabled, retention_grace_days, "
            "        retention_exempt_linked, retention_exempt_hostile, updated_at, updated_by) "
            "VALUES (1, %s, %s, %s, %s, now(), %s) "
            "ON CONFLICT (id) DO UPDATE SET retention_enabled = EXCLUDED.retention_enabled, "
            "    retention_grace_days = EXCLUDED.retention_grace_days, "
            "    retention_exempt_linked = EXCLUDED.retention_exempt_linked, "
            "    retention_exempt_hostile = EXCLUDED.retention_exempt_hostile, "
            "    updated_at = now(), updated_by = EXCLUDED.updated_by",
            (payload.enabled, payload.grace_days, payload.exempt_linked,
             payload.exempt_hostile, user["id"]))
        page = _page(cur)

    changed = {t: [before_policy.get(t), policy[t]] for t in policy
               if before_policy.get(t) != policy[t]}
    audit.record("retention.policy", user=user, object_type="app_settings", object_id=1,
                 object_label="Retention policy",
                 detail={"enabled": [bool(before["retention_enabled"]), payload.enabled],
                         "grace_days": [before["retention_grace_days"], payload.grace_days],
                         "windows_changed": changed})
    return page


@router.get("/retention/due")
def get_due(limit: int = Query(default=200, ge=1, le=1000),
            user: dict = Depends(auth.require_admin)):
    """Everything currently flagged, soonest first."""
    with db_cursor() as cur:
        return {"items": retention.due_list(cur, limit)}


@router.post("/retention/run")
def run_now(dry_run: bool = Query(default=False), user: dict = Depends(auth.require_admin)):
    """Run the sweep now rather than waiting for the worker.

    Useful twice: straight after saving a policy, when nobody wants to wait an
    hour to see whether it did what they meant; and as a dry run, which
    reports the same numbers and writes nothing.
    """
    with db_cursor(commit=not dry_run) as cur:
        result = retention.run_sweep(cur, user=user, dry_run=dry_run)
        result["survey"] = retention.survey(cur, retention.load_policy(cur),
                                            retention.load_settings(cur))
    if not dry_run:
        audit.record("retention.run", user=user, object_type="app_settings", object_id=1,
                     object_label="Retention sweep",
                     detail={k: result[k] for k in ("cleared", "flagged", "archived")})
    return result
