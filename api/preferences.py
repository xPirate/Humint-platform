"""Per-user UI preferences.

One JSONB document per account, stored on the user row. Currently it holds the
dashboard's panel layout; anything else the UI needs to remember about a
*person* rather than about a *machine* belongs here too.

Browser storage was the wrong home for this the moment the deployment story
became "Chromebooks kept at each site". An analyst working from three devices
in a week would arrange their dashboard three times, and two analysts sharing
one device would each inherit the other's arrangement — including, on a
shared machine, seeing which panels a colleague had hidden.

The server never looks inside the document. It validates that it is an object
and that it is not absurdly large, and otherwise treats the shape as the
frontend's business — which is what keeps a new UI toggle from needing a
migration.

Deliberately not audited. The audit trail exists to answer who changed the
case file and who moved data out of it; "someone hid the hotspots panel" is
neither, and a log that recorded it would bury the entries that matter.
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import auth
from db import db_cursor

router = APIRouter(prefix="/api", tags=["preferences"])

# Generous for UI state and small enough that nobody can use an account as a
# free-form document store. A dashboard layout is a few hundred bytes.
MAX_PREFERENCES_BYTES = 64 * 1024


class PreferencesUpdate(BaseModel):
    # Top-level keys are merged, not replaced wholesale, so the dashboard can
    # save its own section without having to know or resend anyone else's.
    preferences: dict = Field(default_factory=dict)


def _load(cur, user_id: int) -> dict:
    cur.execute("SELECT preferences FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return row[0] or {}


@router.get("/me/preferences")
def get_preferences(user: dict = Depends(auth.require_user)):
    with db_cursor() as cur:
        return {"preferences": _load(cur, user["id"])}


@router.patch("/me/preferences")
def update_preferences(payload: PreferencesUpdate, user: dict = Depends(auth.require_user)):
    if not isinstance(payload.preferences, dict):
        raise HTTPException(status_code=400, detail="preferences must be an object")

    with db_cursor(commit=True) as cur:
        current = _load(cur, user["id"])
        merged = {**current, **payload.preferences}
        # A key set to null removes it, so a client can forget a setting
        # without needing a second endpoint to do it.
        merged = {k: v for k, v in merged.items() if v is not None}

        encoded = json.dumps(merged)
        if len(encoded.encode("utf-8")) > MAX_PREFERENCES_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"preferences must be under {MAX_PREFERENCES_BYTES // 1024} KB — "
                       "this is for UI state, not for storing case data.",
            )
        cur.execute(
            "UPDATE users SET preferences = %s::jsonb WHERE id = %s RETURNING preferences",
            (encoded, user["id"]),
        )
        return {"preferences": cur.fetchone()[0] or {}}
