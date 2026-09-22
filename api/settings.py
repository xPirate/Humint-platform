"""Admin-editable runtime overrides for Ollama configuration.

Everything else in this app that varies by deployment (Postgres creds,
session lifetime, RSS poll interval, ...) is a .env value, picked once and
requiring a container rebuild to change. Ollama's base URL/model/embed
model/enabled/timeout are the one set of settings worth making editable
without that — "try a different model" or "point at a different Ollama
host" are things you'd reasonably want to do more than once, from inside
the app, without SSHing in to edit a file.

app_settings (see db/init.sql) is a single row (id=1) with one nullable
column per overridable setting; NULL means "no override, use .env." This
module only ever reads/writes that row and the .env values used as
defaults — it has no dependency on the worker, which independently reads
the same table (see worker/settings.py) at the top of every poll cycle so
a change here takes effect within about WORKER_POLL_INTERVAL_SECONDS
without a restart.
"""

import os
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import audit
import auth
from db import db_cursor
from ollama_client import OllamaClient
from ollama_config import get_effective_ollama_config

router = APIRouter(prefix="/api/admin", tags=["admin"])

# A second router without the /api/admin prefix, for the one endpoint here
# that any logged-in analyst needs: whether Ollama is actually working. An
# analyst wondering why the extraction queue has been empty for two days
# shouldn't have to be an admin to find out the model was never pulled.
public_router = APIRouter(prefix="/api", tags=["settings"])

# Must match worker/settings.py's ENV_DEFAULTS and the values OllamaClient(...)
# used to be constructed with directly in worker/main.py before this existed.
ENV_DEFAULTS = {
    "ollama_base_url": os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434"),
    "ollama_model": os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
    "ollama_extract_model": os.environ.get("OLLAMA_EXTRACT_MODEL", ""),
    "ollama_embed_model": os.environ.get("OLLAMA_EMBED_MODEL", ""),
    "ollama_enabled": os.environ.get("OLLAMA_ENABLED", "true").lower() == "true",
    "ollama_timeout_seconds": int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "180")),
}

_SETTINGS_COLS = ["ollama_base_url", "ollama_model", "ollama_extract_model",
                  "ollama_embed_model", "ollama_enabled", "ollama_timeout_seconds"]


class OllamaSettingsUpdate(BaseModel):
    # Every field is Optional and *meaningfully* nullable: a field present
    # in the request body (even as null) is applied — null clears that
    # override back to "use .env" — while a field left out of the body
    # entirely is untouched. See update_ollama_settings's use of
    # model_dump(exclude_unset=True) below, the same technique
    # api/entities.py's update_entity already uses for partial detail
    # updates.
    ollama_base_url: Optional[str] = None
    ollama_model: Optional[str] = None
    ollama_extract_model: Optional[str] = None
    ollama_embed_model: Optional[str] = None
    ollama_enabled: Optional[bool] = None
    ollama_timeout_seconds: Optional[int] = Field(default=None, ge=1, le=3600)


def _row_to_response(row) -> dict:
    if row is None:
        overrides = {c: None for c in _SETTINGS_COLS}
        updated_at = updated_by = None
    else:
        overrides = dict(zip(_SETTINGS_COLS, row[: len(_SETTINGS_COLS)]))
        updated_at, updated_by = row[len(_SETTINGS_COLS):]

    fields = {}
    for col in _SETTINGS_COLS:
        override = overrides[col]
        fields[col] = {
            "value": override if override is not None else ENV_DEFAULTS[col],
            "overridden": override is not None,
            "env_default": ENV_DEFAULTS[col],
        }
    return {"fields": fields, "updated_at": updated_at, "updated_by": updated_by}


@router.get("/ollama-settings")
def get_ollama_settings(user: dict = Depends(auth.require_admin)):
    with db_cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_SETTINGS_COLS)}, updated_at, updated_by "
            "FROM app_settings WHERE id = 1"
        )
        row = cur.fetchone()
    return _row_to_response(row)


@router.patch("/ollama-settings")
def update_ollama_settings(payload: OllamaSettingsUpdate, user: dict = Depends(auth.require_admin)):
    updates = payload.model_dump(exclude_unset=True)
    with db_cursor(commit=True) as cur:
        # Ensure the singleton row exists before UPDATEing it -- harmless
        # no-op on every call after the first.
        cur.execute("INSERT INTO app_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        if updates:
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            cur.execute(
                f"UPDATE app_settings SET {set_clause}, updated_at = %s, updated_by = %s WHERE id = 1",
                [*updates.values(), datetime.now(timezone.utc), user["id"]],
            )
        cur.execute(
            f"SELECT {', '.join(_SETTINGS_COLS)}, updated_at, updated_by "
            "FROM app_settings WHERE id = 1"
        )
        row = cur.fetchone()
    # Values, not just names: which Ollama host this instance talks to and
    # which model it runs are configuration, not case data, and knowing what
    # they were changed *to* is the whole point of auditing the change.
    audit.record("settings.update", user=user, object_type="settings", object_id="ollama",
                 detail={"changes": {k: str(v) for k, v in updates.items()}})
    return _row_to_response(row)


# ----------------------------------------------------------------------------
# Model discovery
#
# The Model/Embed model fields used to be free text that had to match a model
# already pulled on the Ollama host exactly. A typo there produces no error
# anywhere: extraction just quietly never returns anything, and the only clue
# is an absence. These endpoints let the Admin page ask the host what it
# actually has, so picking a model is a choice from a list rather than a
# guess.
# ----------------------------------------------------------------------------

def _client_for(base_url: Optional[str] = None) -> OllamaClient:
    """A client against the currently-saved config, optionally pointed at a
    different base URL. That override is what lets the Admin page list the
    models on a host the admin has just TYPED into the Base URL box but not
    saved yet — without it, editing the URL and hitting Refresh would show
    you the old host's models, which is worse than showing nothing."""
    config = get_effective_ollama_config()
    if base_url:
        config["base_url"] = base_url.strip()
    return OllamaClient(**config)


@router.get("/ollama-models")
def list_ollama_models(
    base_url: Optional[str] = Query(default=None, description="Override the saved base URL"),
    user: dict = Depends(auth.require_admin),
):
    client = _client_for(base_url)
    models = client.list_models()
    return {
        "base_url": client.base_url,
        "reachable": models is not None,
        "models": models or [],
    }


@public_router.get("/ollama-status")
def ollama_status(user: dict = Depends(auth.require_user)):
    """A compact "is this actually working" check for any logged-in user, so
    the Review queues can explain themselves when they're empty because of
    configuration rather than because there's nothing to review. Deliberately
    narrower than the admin endpoint above: no base URL, no model inventory,
    just whether the configured models are present."""
    config = get_effective_ollama_config()
    client = OllamaClient(**config)
    models = client.list_models()
    installed = {m["name"] for m in models} if models is not None else set()

    def _installed(name):
        if not name:
            return None  # not configured, so "is it installed" isn't a meaningful question
        # Ollama reports "llama3.1:8b" for a model pulled as "llama3.1:8b",
        # but a bare "llama3.1" is stored as "llama3.1:latest" — so a settings
        # value without a tag has to be matched against the :latest form too,
        # or a perfectly working setup would be reported as missing.
        return name in installed or f"{name}:latest" in installed

    return {
        "enabled": client.enabled,
        "reachable": models is not None,
        "model": client.model,
        "model_installed": _installed(client.model) if models is not None else None,
        # Reported even when it is the same as `model` (i.e. no override), so
        # the status banner can say which model would actually read a document
        # rather than leaving the reader to work out the fallback.
        "extract_model": client.extract_model,
        "extract_model_installed": _installed(client.extract_model) if models is not None else None,
        "embed_model": client.embed_model or None,
        "embed_model_installed": _installed(client.embed_model) if models is not None else None,
    }


# ----------------------------------------------------------------------------
# Model pulling
#
# Pulling a model is a multi-gigabyte download that can take many minutes, so
# it runs on a background thread and the Admin page polls for progress rather
# than holding a request open for the duration (which would die on any page
# refresh, and tie up a worker meanwhile).
#
# State lives in this module rather than the database on purpose: a pull dies
# with the api process anyway, so persisting its progress would only ever
# produce a stale "downloading..." that never finishes after a restart. This
# assumes ONE api process, which is what the Dockerfile starts (uvicorn with
# no --workers). Running multiple workers would give each its own copy of this
# state and the page would poll whichever one it happened to hit.
# ----------------------------------------------------------------------------

_pull_lock = threading.Lock()
_pull_state = {
    "active": False,
    "model": None,
    "status": "",
    "percent": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
    "started_by": None,
    "ok": None,
}


class ModelPullRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    base_url: Optional[str] = None


def _set_pull_state(**changes):
    with _pull_lock:
        _pull_state.update(changes)


def _run_pull(client: OllamaClient, model: str):
    def on_progress(event):
        status = event.get("status") or ""
        total, completed = event.get("total"), event.get("completed")
        percent = None
        if isinstance(total, (int, float)) and total > 0 and isinstance(completed, (int, float)):
            percent = max(0, min(100, round(completed / total * 100, 1)))
        _set_pull_state(status=status, percent=percent)

    ok, message = client.pull_model(model, on_progress=on_progress)
    with _pull_lock:
        _pull_state.update({
            "active": False,
            "ok": ok,
            "status": message if ok else _pull_state.get("status") or "",
            "error": None if ok else message,
            "percent": 100 if ok else _pull_state.get("percent"),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })


@router.post("/ollama-pull")
def start_model_pull(payload: ModelPullRequest, user: dict = Depends(auth.require_admin)):
    with _pull_lock:
        if _pull_state["active"]:
            raise HTTPException(
                status_code=409,
                detail=f"Already pulling {_pull_state['model']} — wait for that to finish first.",
            )
        _pull_state.update({
            "active": True,
            "model": payload.model.strip(),
            "status": "starting",
            "percent": None,
            "error": None,
            "ok": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "started_by": user["username"],
        })

    client = _client_for(payload.base_url)
    # daemon=True so a pull in flight can never keep the container alive on
    # shutdown; an interrupted pull is resumable (Ollama keeps the layers it
    # already fetched), which is what makes abandoning it acceptable.
    audit.record("ollama.pull", user=user, object_type="ollama_model",
                 object_label=payload.model.strip(), detail={"base_url": client.base_url})
    threading.Thread(target=_run_pull, args=(client, payload.model.strip()), daemon=True).start()

    with _pull_lock:
        return dict(_pull_state)


@router.get("/ollama-pull-status")
def get_model_pull_status(user: dict = Depends(auth.require_admin)):
    with _pull_lock:
        return dict(_pull_state)
