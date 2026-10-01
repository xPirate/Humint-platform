"""Admin control for the link-signal pass.

It used to be LINK_SIGNALS_ENABLED in .env, which meant editing a file on the
server and rebuilding to answer "let me see what it finds" — and then doing it
again to put it back. That is a poor fit for something an analyst wants on for
ten minutes and off again.

Three layers, resolved in link_signals.effective_config(): a timed window, a
standing override, and the .env value underneath both. Nothing here changes
what the pass DOES; it only decides whether it runs.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import audit
import auth
import link_signals
from db import db_cursor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["link-signals"])

# The windows offered on the page. Long enough at the top to cover "leave it
# while I go through the queue", short enough at the bottom to be the thing
# you actually wanted: a look, not a commitment.
WINDOW_CHOICES = (10, 30, 60, 240, 480)
MAX_WINDOW_MINUTES = 1440


class LinkSignalSettings(BaseModel):
    # None means "clear the override and go back to whatever .env says", which
    # is a genuinely different state from off and worth being able to reach.
    enabled: bool | None = None
    # Minutes from now. 0 or absent means no time limit.
    for_minutes: int | None = Field(default=None, ge=0, le=MAX_WINDOW_MINUTES)


def _page() -> dict:
    config = link_signals.effective_config()
    return {
        "enabled": config["enabled"],
        "source": config["source"],
        "override": config["override"],
        "until": config["until"].isoformat() if config["until"] else None,
        "last_run": config["last_run"].isoformat() if config["last_run"] else None,
        "env_default": link_signals.ENV_ENABLED,
        "interval_seconds": link_signals.INTERVAL_SECONDS,
        "max_pending": link_signals.MAX_PENDING,
        "window_choices": list(WINDOW_CHOICES),
    }


@router.get("/link-signals")
def get_link_signals(user: dict = Depends(auth.require_admin)):
    return _page()


@router.put("/link-signals")
def put_link_signals(payload: LinkSignalSettings, user: dict = Depends(auth.require_admin)):
    """Switch it on, off, on for a while, or back to the .env default."""
    minutes = payload.for_minutes or 0
    if minutes and payload.enabled is False:
        raise HTTPException(status_code=400,
                            detail="A time window only makes sense when switching it on.")
    with db_cursor(commit=True) as cur:
        if minutes:
            cur.execute(
                "INSERT INTO app_settings (id, link_signals_enabled, link_signals_until) "
                "VALUES (1, TRUE, now() + make_interval(mins => %s)) "
                "ON CONFLICT (id) DO UPDATE SET link_signals_enabled = TRUE, "
                "  link_signals_until = now() + make_interval(mins => %s)", (minutes, minutes))
        else:
            # Switching it on or off outright ends any window that was open:
            # the two controls would otherwise disagree about what happens in
            # nine minutes' time.
            cur.execute(
                "INSERT INTO app_settings (id, link_signals_enabled, link_signals_until) "
                "VALUES (1, %s, NULL) ON CONFLICT (id) DO UPDATE "
                "SET link_signals_enabled = EXCLUDED.link_signals_enabled, "
                "    link_signals_until = NULL", (payload.enabled,))
    page = _page()
    audit.record("link_signals.configure", user=user, object_type="app_settings", object_id=1,
                 object_label="Link signals",
                 detail={"enabled": page["enabled"], "for_minutes": minutes or None,
                         "source": page["source"]})
    return page


@router.post("/link-signals/run")
def run_now(user: dict = Depends(auth.require_admin)):
    """One pass, right now, whatever the schedule says.

    This is the reason the toggle is usable at all: the pass runs on its own
    timer (LINK_SIGNALS_INTERVAL_SECONDS, fifteen minutes by default), so
    switching it on for ten minutes could otherwise come and go without a
    single pass happening inside the window.

    It runs here in the API rather than being handed to the worker because
    somebody is standing in front of it waiting to see what came out — and
    link_signals.py needs nothing but a database cursor, which is why the same
    module is carried in both containers.
    """
    before = _pending()
    try:
        proposed = link_signals.run_link_signals(force=True)
    except Exception as exc:          # the pass itself promises not to raise
        logger.exception("link signal pass failed")
        raise HTTPException(status_code=500, detail=f"The pass failed: {exc}")
    after = _pending()
    audit.record("link_signals.run", user=user, object_type="app_settings", object_id=1,
                 object_label="Link signals", detail={"added": after - before})
    page = _page()
    page["ran"] = True
    page["proposed"] = max(0, after - before)
    page["did_work"] = bool(proposed)
    page["pending"] = after
    return page


def _pending() -> int:
    """Proposals from this pass that are still waiting.

    Deliberately the pass's own counter rather than a query written here:
    link-signal proposals land in extraction_suggestions with source='signal',
    not in correlation_suggestions, and counting the wrong queue would report
    "proposed 0" every time while the rows piled up somewhere else.
    """
    with db_cursor() as cur:
        return link_signals._pending_count(cur)
