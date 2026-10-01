"""Runtime overrides for Ollama configuration, layered on top of the .env
values every other setting in this app still uses. See "Ollama Settings" in
the Admin page / api/settings.py for the admin-facing half of this.

Unlike everything else here, Ollama's base URL/model/etc. used to only be
changeable by editing .env and rebuilding — reasonable for a value picked
once at deploy time, annoying for "try a different model" or "point at a
different Ollama host," which is exactly the kind of thing worth changing
without a rebuild. app_settings (see db/init.sql) holds admin overrides;
NULL in any column there means "no override, use the .env value."

get_effective_ollama_config() is called fresh at the top of every poll loop
iteration in main.py rather than once at worker startup, so a change made
in the Admin page takes effect within one poll cycle (WORKER_POLL_INTERVAL_
SECONDS, 20s by default) — no worker restart needed. This is a cheap query
against a single-row table; reconstructing OllamaClient from it every
iteration is cheap too (it's a handful of attribute assignments, no
connection to hold open), so there's no reason to cache it across
iterations and risk it going stale.
"""

import os

from db import db_cursor

# Must match the defaults OllamaClient(...) was constructed with in main.py
# before this module existed — these are what "no override" falls back to.
ENV_DEFAULTS = {
    "base_url": os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434"),
    "model": os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
    "extract_model": os.environ.get("OLLAMA_EXTRACT_MODEL", ""),
    "embed_model": os.environ.get("OLLAMA_EMBED_MODEL", ""),
    "timeout": int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "180")),
    "enabled": os.environ.get("OLLAMA_ENABLED", "true").lower() == "true",
}


def get_effective_ollama_config() -> dict:
    """Returns {base_url, model, embed_model, timeout, enabled} — an
    app_settings override for a field if one is set, otherwise the .env
    value. Never raises: if app_settings doesn't exist yet (a deployment
    that hasn't run the migration) or the query fails for any reason, this
    falls all the way back to pure .env behavior, same as before this
    feature existed."""
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT ollama_base_url, ollama_model, ollama_extract_model, "
                "ollama_embed_model, ollama_enabled, ollama_timeout_seconds "
                "FROM app_settings WHERE id = 1"
            )
            row = cur.fetchone()
    except Exception as exc:
        print(f"[worker] could not read app_settings, using .env only: {exc}", flush=True)
        row = None

    if row is None:
        return _resolve(dict(ENV_DEFAULTS))

    base_url, model, extract_model, embed_model, enabled, timeout = row
    return _resolve({
        "base_url": base_url if base_url is not None else ENV_DEFAULTS["base_url"],
        "model": model if model is not None else ENV_DEFAULTS["model"],
        "extract_model": extract_model if extract_model is not None else ENV_DEFAULTS["extract_model"],
        "embed_model": embed_model if embed_model is not None else ENV_DEFAULTS["embed_model"],
        "enabled": enabled if enabled is not None else ENV_DEFAULTS["enabled"],
        "timeout": timeout if timeout is not None else ENV_DEFAULTS["timeout"],
    })


def _resolve(config: dict) -> dict:
    """Collapse the one fallback this config has, so callers never see a blank.
    Mirrors api/ollama_config.py — an unset extraction model means "use the
    assistant's"."""
    config["extract_model"] = (config.get("extract_model") or "").strip() or config["model"]
    return config
