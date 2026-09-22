"""What this instance is called, and what it looks like out of the box.

Three admin-editable values, stored on the same single-row app_settings table
as the Ollama overrides and following the same NULL-means-".env default"
convention:

  instance_name    the name in the topbar, the browser tab, the login screen
                   and the running header of every exported PDF
  brand_accent     an optional #rrggbb that overrides the palette's accent
  default_palette  the palette a user sees before choosing one of their own

WHY THE READ ENDPOINT IS PUBLIC

The login screen has to show the instance name, and it is drawn before anyone
has logged in. So GET /api/branding takes no session.

That is a deliberate, bounded exception and the payload is exactly those three
fields. An instance name is not a secret — it is printed on every PDF that
leaves the building and shown to everyone who reaches the login page, which is
the same set of people who can already see the login page. Nothing about the
case file, the user list, the version, or the configuration goes through here,
and nothing should be added to it that would not be equally happy on a
billboard outside the building.

WHY THE DEFAULT IS A DEFAULT AND NOT A LOCK

An organisation gets to set what its instance looks like; it does not get to
decide that an analyst standing in direct sunlight has to keep squinting at the
corporate palette. The instance default is what an account starts with, and any
user can then pick their own — the same way the dashboard layout works.
"""

import os
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

import audit
import auth
from db import db_cursor

router = APIRouter(prefix="/api", tags=["branding"])

# Must stay in step with the palette blocks in frontend/styles.css and the
# PALETTES list in the pre-paint script in index.html. Validated here so a
# typo in an admin form cannot put every user's browser into a state that has
# no palette block at all.
PALETTES = ("terminal", "slate", "graphite", "archive")

MAX_NAME_LENGTH = 60

ENV_DEFAULTS = {
    "instance_name": os.environ.get("INSTANCE_NAME", "HUMINT Platform"),
    "brand_accent": os.environ.get("BRAND_ACCENT", "") or None,
    "default_palette": os.environ.get("DEFAULT_PALETTE", "terminal"),
}

_COLS = ("instance_name", "brand_accent", "default_palette")

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _effective(cur) -> dict:
    """The stored override where there is one, the .env default where there
    isn't. Never raises and never returns a partial document — a branding read
    that failed would take the login page down with it."""
    values = dict(ENV_DEFAULTS)
    try:
        cur.execute(f"SELECT {', '.join(_COLS)} FROM app_settings WHERE id = 1")
        row = cur.fetchone()
    except Exception:
        row = None
    if row:
        for col, stored in zip(_COLS, row):
            if stored is not None and stored != "":
                values[col] = stored
    # A palette that is no longer offered (renamed, or typed in by hand before
    # this validation existed) falls back rather than leaving the frontend with
    # an attribute matching no block.
    if values["default_palette"] not in PALETTES:
        values["default_palette"] = "terminal"
    if values["brand_accent"] and not _HEX.match(values["brand_accent"]):
        values["brand_accent"] = None
    return values


def instance_name() -> str:
    """For callers outside the request cycle — the PDF builders in
    particular, which need the name for the running header."""
    try:
        with db_cursor() as cur:
            return _effective(cur)["instance_name"]
    except Exception:
        # A PDF export must not fail because the settings row could not be
        # read. The stock name is a worse header, not a broken one.
        return ENV_DEFAULTS["instance_name"]


class BrandingUpdate(BaseModel):
    # Every field optional and meaningfully nullable, as in settings.py: a
    # field present as null clears the override back to the .env default, and
    # a field left out of the body entirely is untouched.
    instance_name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    brand_accent: str | None = None
    default_palette: str | None = None

    @field_validator("instance_name")
    @classmethod
    def _name_not_blank(cls, v):
        if v is None:
            return None
        v = v.strip()
        # An empty string means "go back to the default", which is what a
        # cleared text field in the admin form sends. It is not an error.
        return v or None

    @field_validator("brand_accent")
    @classmethod
    def _accent_is_hex(cls, v):
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not _HEX.match(v):
            raise ValueError("brand_accent must be a six-digit hex colour, e.g. #1f6feb")
        return v.lower()

    @field_validator("default_palette")
    @classmethod
    def _palette_known(cls, v):
        if v is None:
            return None
        v = v.strip().lower()
        if not v:
            return None
        if v not in PALETTES:
            raise ValueError(f"default_palette must be one of {PALETTES}")
        return v


@router.get("/branding")
def get_branding():
    """Deliberately unauthenticated — see the module docstring."""
    with db_cursor() as cur:
        values = _effective(cur)
    return {**values, "palettes": list(PALETTES)}


@router.get("/admin/branding")
def get_branding_admin(user: dict = Depends(auth.require_admin)):
    """The same values, plus which of them are overrides and what the .env
    default underneath each one is — so the admin form can show a Reset that
    means something."""
    with db_cursor() as cur:
        cur.execute(f"SELECT {', '.join(_COLS)} FROM app_settings WHERE id = 1")
        row = cur.fetchone() or (None,) * len(_COLS)
        stored = dict(zip(_COLS, row))
        effective = _effective(cur)
        cur.execute("SELECT updated_at, u.username FROM app_settings s "
                    "LEFT JOIN users u ON u.id = s.updated_by WHERE s.id = 1")
        meta = cur.fetchone() or (None, None)
    return {
        "fields": {
            col: {
                "value": effective[col],
                "overridden": stored[col] is not None and stored[col] != "",
                "env_default": ENV_DEFAULTS[col],
            }
            for col in _COLS
        },
        "palettes": list(PALETTES),
        "updated_at": meta[0],
        "updated_by": meta[1],
    }


@router.patch("/admin/branding")
def update_branding(payload: BrandingUpdate, user: dict = Depends(auth.require_admin)):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No branding fields given")

    with db_cursor(commit=True) as cur:
        cur.execute("INSERT INTO app_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        assignments = ", ".join(f"{col} = %s" for col in updates)
        cur.execute(
            f"UPDATE app_settings SET {assignments}, updated_at = now(), updated_by = %s "
            "WHERE id = 1",
            (*updates.values(), user["id"]),
        )
        effective = _effective(cur)

    # Audited, unlike the per-user theme choice. Renaming an instance changes
    # what every exported PDF says it came from, which is a claim about
    # provenance and belongs in the trail; one analyst preferring Graphite is
    # not.
    audit.record(
        "branding.update", user=user, object_type="settings", object_id="branding",
        object_label=effective["instance_name"],
        detail={"changed": sorted(updates), **{k: updates[k] for k in updates}},
    )
    return {**effective, "palettes": list(PALETTES)}
