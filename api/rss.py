"""RSS/Atom feed management for the ingest pipeline — see worker/rss_ingest.py
for the actual polling/parsing/Event-creation logic, which runs entirely in
the background worker. This module only owns the feed list itself:
create/list/edit/deactivate/delete. No feed ships built in; every row here
was added by an admin, and the app never reaches out to a URL on its own
initiative beyond polling feeds an admin explicitly added.

Gated on auth.require_admin throughout, same as api/admin.py: adding a feed
is configuring a standing background data pipeline, not day-to-day casework,
so it lives alongside user management on the Admin page rather than being
open to every analyst.
"""

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import audit
import auth
import destroy
from db import db_cursor

router = APIRouter(prefix="/api", tags=["rss"])

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# id, url, label, is_active, last_polled_at, last_error, created_at — the
# trailing item_count is appended by the queries below, not a real column.
_FEED_COLS = ["id", "url", "label", "is_active", "max_age_days", "max_items_per_poll",
              "last_polled_at", "last_error", "created_at"]

_FEED_SELECT = f"""
    SELECT {', '.join(_FEED_COLS)},
           (SELECT COUNT(*) FROM rss_items i WHERE i.feed_id = f.id) AS item_count
    FROM rss_feeds f
"""


class FeedCreate(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    label: str = Field(min_length=1, max_length=256)
    # How far back this feed may reach, and how much it may take in one go.
    #
    # Defaulted rather than left open: a feed's FIRST poll sees the
    # publisher's whole current window, which for a busy newsroom is hundreds
    # of items. They land as Events, every Event is compared against every
    # other one, and news items about the same city read alike -- so an
    # unlimited feed can bury the correlation queue on the day it is added.
    # Seven days and fifty items is a normal week of a normal feed.
    max_age_days: Optional[int] = Field(default=7, ge=1, le=3650)
    max_items_per_poll: Optional[int] = Field(default=50, ge=1, le=1000)


class FeedUpdate(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=256)
    is_active: Optional[bool] = None
    max_age_days: Optional[int] = Field(default=None, ge=1, le=3650)
    max_items_per_poll: Optional[int] = Field(default=None, ge=1, le=1000)


def _validate_url(url: str) -> None:
    if not _URL_RE.match(url.strip()):
        raise HTTPException(status_code=400, detail="url must start with http:// or https://")


def _row_to_feed(row) -> dict:
    d = dict(zip(_FEED_COLS, row[: len(_FEED_COLS)]))
    d["item_count"] = row[len(_FEED_COLS)]
    return d


def _fetch_feed_row(cur, feed_id: int):
    cur.execute(f"{_FEED_SELECT} WHERE f.id = %s", (feed_id,))
    return cur.fetchone()


@router.get("/rss-feeds")
def list_feeds(user: dict = Depends(auth.require_admin)):
    with db_cursor() as cur:
        cur.execute(f"{_FEED_SELECT} ORDER BY f.label")
        rows = cur.fetchall()
    return {"items": [_row_to_feed(r) for r in rows]}


@router.post("/rss-feeds", status_code=201)
def create_feed(payload: FeedCreate, user: dict = Depends(auth.require_admin)):
    url = payload.url.strip()
    _validate_url(url)
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT 1 FROM rss_feeds WHERE url = %s", (url,))
        if cur.fetchone() is not None:
            raise HTTPException(status_code=409, detail="A feed with this URL already exists")
        cur.execute(
            "INSERT INTO rss_feeds (url, label, max_age_days, max_items_per_poll, created_by) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (url, payload.label.strip(), payload.max_age_days,
             payload.max_items_per_poll, user["id"]),
        )
        feed_id = cur.fetchone()[0]
        row = _fetch_feed_row(cur, feed_id)
    audit.record("rss_feed.create", user=user, object_type="rss_feed", object_id=feed_id,
                 object_label=payload.label.strip(), detail={"url": url})
    return _row_to_feed(row)


@router.get("/rss-feeds/{feed_id}")
def get_feed(feed_id: int, user: dict = Depends(auth.require_admin)):
    with db_cursor() as cur:
        row = _fetch_feed_row(cur, feed_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Feed not found")
    return _row_to_feed(row)


@router.patch("/rss-feeds/{feed_id}")
def update_feed(feed_id: int, payload: FeedUpdate, user: dict = Depends(auth.require_admin)):
    updates = payload.model_dump(exclude_unset=True)
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT 1 FROM rss_feeds WHERE id = %s", (feed_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Feed not found")
        if updates:
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            cur.execute(f"UPDATE rss_feeds SET {set_clause} WHERE id = %s", [*updates.values(), feed_id])
        row = _fetch_feed_row(cur, feed_id)
    audit.record("rss_feed.update", user=user, object_type="rss_feed", object_id=feed_id,
                 object_label=row[2] if row else None, detail={"changes": updates})
    return _row_to_feed(row)


@router.get("/rss-feeds/{feed_id}/records")
def feed_records(feed_id: int, user: dict = Depends(auth.require_admin)):
    """What this feed has put in the case file, before anyone deletes it.

    The delete dialog needs to be able to say "this feed created 214 Events,
    3 of which are cited by confirmed reports" rather than asking someone to
    decide blind. The last figure matters most: those three cannot be deleted
    and should not be, because a confirmed report referring to a record that
    no longer exists is a hole in the reporting.
    """
    with db_cursor() as cur:
        cur.execute("SELECT label FROM rss_feeds WHERE id = %s", (feed_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Feed not found")

        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE e.id IS NOT NULL),
                   COUNT(*) FILTER (WHERE e.is_active),
                   COUNT(*) FILTER (WHERE e.id IS NOT NULL AND NOT e.is_active),
                   COUNT(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM report_entities re JOIN reports r ON r.id = re.report_id
                       WHERE re.entity_id = e.id AND r.status = 'confirmed'))
            FROM rss_items i LEFT JOIN entities e ON e.id = i.entity_id
            WHERE i.feed_id = %s
            """, (feed_id,))
        total, active, archived, cited = cur.fetchone()
    return {"feed_id": feed_id, "label": row[0], "records": total or 0,
            "active": active or 0, "archived": archived or 0,
            "cited_by_confirmed_reports": cited or 0}


@router.delete("/rss-feeds/{feed_id}", status_code=200)
def delete_feed(feed_id: int, records: str = "leave",
                user: dict = Depends(auth.require_admin)):
    """Delete a feed, and say what should happen to the Events it created.

    `records` is one of:

      leave    -- the default and the old behaviour. The Events stay as
                  ordinary records; only the polling stops.
      archive  -- hide them from the default lists but keep every row, so
                  reports and audit entries still resolve. Reversible.
      delete   -- remove them for good, the same way an admin delete does.

    Deleting is offered because a feed added by mistake is the case this
    exists for, and hunting two hundred Events out of the Entities view one
    at a time is not a reasonable ask. It is opt-in per delete rather than
    automatic, because a feed that ran for a month has records somebody has
    since built on.

    **Records cited by a confirmed report are never deleted**, whichever mode
    is chosen -- they are archived instead and reported back. A confirmed
    report pointing at a record that no longer exists is a hole in the
    reporting, and this is not the place to punch one.
    """
    if records not in ("leave", "archive", "delete"):
        raise HTTPException(status_code=400,
                            detail="records must be leave, archive or delete")

    deleted = archived = protected = 0
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT label, url FROM rss_feeds WHERE id = %s FOR UPDATE", (feed_id,))
        existing = cur.fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Feed not found")

        if records != "leave":
            cur.execute(
                """
                SELECT e.id, EXISTS (
                           SELECT 1 FROM report_entities re JOIN reports r ON r.id = re.report_id
                           WHERE re.entity_id = e.id AND r.status = 'confirmed')
                FROM rss_items i JOIN entities e ON e.id = i.entity_id
                WHERE i.feed_id = %s
                """, (feed_id,))
            for entity_id, cited in cur.fetchall():
                if records == "delete" and not cited:
                    destroy._delete_entity(cur, entity_id)
                    deleted += 1
                else:
                    cur.execute("UPDATE entities SET is_active = FALSE WHERE id = %s "
                                "AND is_active = TRUE", (entity_id,))
                    archived += cur.rowcount
                    if cited and records == "delete":
                        protected += 1

        # rss_items cascades with the feed; anything above has already dealt
        # with the entities those rows pointed at.
        cur.execute("DELETE FROM rss_feeds WHERE id = %s", (feed_id,))

    audit.record("rss_feed.delete", user=user, object_type="rss_feed", object_id=feed_id,
                 object_label=existing[0],
                 detail={"url": existing[1], "records": records, "deleted": deleted,
                         "archived": archived,
                         "kept_because_a_confirmed_report_cites_them": protected})
    return {"deleted": True, "records_deleted": deleted, "records_archived": archived,
            "records_kept_cited_by_confirmed_reports": protected}
