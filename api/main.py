"""HUMINT Platform — API service.

Auth, admin account management, entity/relationship CRUD, bulk CSV entity
import, report authoring + attachment upload, both review queues
(extraction and correlation), RSS feed management, a dashboard summary
endpoint, the map's location-marker endpoint, the case-data-aware AI
assistant (see assistant.py), whole-instance backup/restore (see backup.py),
and the audit trail (see audit.py for the write path, audit_api.py for the
admin-facing read side) are all live, alongside the frontend that surfaces
them.
"""

import hashlib
import os
import re

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import admin
import assistant
import audit
import audit_api
import auth
import backup
import branding
import bulk_import
import contacts
import correlation
import dashboard
import destroy
import entities
import exports
import extraction
import graph
import insights
import map as map_module
import maptiles
import merge
import ollama_stats
import preferences
import entity_actions
import propose
import documents
import reports
import rss
import settings
import zones
from db import db_cursor

FRONTEND_DIR = "/app/frontend"

app = FastAPI(title="HUMINT Platform API")


@app.middleware("http")
async def audit_request_context(request, call_next):
    """Stashes the caller's IP and user agent for the duration of the request
    so audit.record() can attach them without every endpoint in the app
    taking a Request parameter it otherwise has no use for. Cleared after,
    since the worker threads FastAPI runs sync endpoints on are reused."""
    audit.set_request_context(**audit.context_from_request(request))
    try:
        return await call_next(request)
    finally:
        audit.clear_request_context()

app.include_router(auth.router)
app.include_router(admin.router)
# bulk_import's /entities/import and /entities/import-template must be
# registered before entities.router — FastAPI/Starlette matches routes in
# registration order with no specificity-based reordering, and entities.py
# declares GET/PATCH /entities/{entity_id}, a path parameter that would
# otherwise swallow "import" or "import-template" as if it were an entity
# id (and 404, since no entity actually has that id) before bulk_import's
# own, more specific routes ever got a chance to match.
app.include_router(bulk_import.router)
# Same reason as bulk_import above: graph declares GET /entities/graph, and
# entities.router's GET /entities/{entity_id} would match "graph" as an id.
app.include_router(graph.router)
app.include_router(entities.router)
app.include_router(merge.router)
app.include_router(contacts.router)
app.include_router(reports.router)
app.include_router(documents.router)
app.include_router(extraction.router)
app.include_router(correlation.router)
app.include_router(dashboard.router)
app.include_router(insights.router)
app.include_router(exports.router)
app.include_router(branding.router)
app.include_router(ollama_stats.router)
app.include_router(propose.router)
app.include_router(entity_actions.router)
app.include_router(map_module.router)
app.include_router(maptiles.router)
app.include_router(zones.router)
app.include_router(rss.router)
app.include_router(settings.router)
app.include_router(settings.public_router)
app.include_router(preferences.router)
app.include_router(assistant.router)
app.include_router(destroy.router)
app.include_router(backup.router)
app.include_router(audit_api.router)

class RevalidatingStatic(StaticFiles):
    """StaticFiles with a Cache-Control header on every response.

    Without one, the browser gets a file carrying only Last-Modified and
    falls back to *heuristic* freshness: it invents an expiry (commonly a
    tenth of the file's age) and serves the cached copy until then without
    asking the server anything. On a long-lived deployment that arithmetic
    grows without bound — a file last changed three months ago is treated
    as fresh for about a week — so an upgraded app.js can go unnoticed by a
    browser that has been using the app for a while. The symptom is
    particularly nasty: index.html is a navigation document and gets
    revalidated, so the new markup arrives and the new buttons appear,
    while the script that makes them work does not. "no-cache" does not
    mean "don't cache" — it means "always ask first", so the browser still
    keeps the file and still gets a cheap 304 when nothing has changed.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStatic(directory=FRONTEND_DIR), name="static")

# Belt and braces for the same problem: the asset URLs in index.html get a
# ?v= stamp derived from the files' own contents, so an upgrade changes the
# URL itself and no cache anywhere -- browser, or a reverse proxy someone
# has put in front of this -- can serve the old file against it. Computed
# once per file version and kept, since index.html is requested on every
# full page load and hashing two files on each of those would be wasteful.
# The trailing (?:\?[^"]*)? is deliberate: index.html ships with a plain
# hand-written stamp of its own so that unpacking a new build fixes stale
# caches even before the API image is rebuilt. Once this code is running it
# replaces that stamp with the real content hash.
_ASSET_RE = re.compile(
    r'(?P<attr>href|src)="/static/(?P<name>[A-Za-z0-9_.\-]+\.(?:css|js))(?:\?[^"]*)?"')
_asset_hashes: dict = {}   # name -> (stat key, hash)
_stamped_html: dict = {}   # combined key -> rendered index.html


def _stat_key(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _asset_version(name: str) -> str:
    """A short content hash, recomputed only when the file's mtime or size
    moves. The frontend directory is a read-only bind mount that changes
    when the operator unpacks a new build, so a stat per request is enough
    to notice -- and cheap enough not to matter."""
    path = os.path.join(FRONTEND_DIR, name)
    key = _stat_key(path)
    if key is None:
        # A missing or unreadable asset is the static mount's problem to
        # report, not this function's -- fall back to an unstamped URL.
        return ""
    cached = _asset_hashes.get(name)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            version = hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:
        return ""
    _asset_hashes[name] = (key, version)
    return version


def _stamped_index() -> str:
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    with open(index_path, encoding="utf-8") as fh:
        raw = fh.read()
    names = sorted({m.group("name") for m in _ASSET_RE.finditer(raw)})
    versions = {name: _asset_version(name) for name in names}
    key = (_stat_key(index_path), tuple(sorted(versions.items())))
    cached = _stamped_html.get(key)
    if cached is not None:
        return cached

    def stamp(match: "re.Match") -> str:
        version = versions.get(match.group("name"), "")
        if not version:
            return match.group(0)
        return f'{match.group("attr")}="/static/{match.group("name")}?v={version}"'

    html = _ASSET_RE.sub(stamp, raw)
    # Only ever one version of the page is current; the dict is a cache
    # keyed on the files, not a store that grows with traffic.
    _stamped_html.clear()
    _stamped_html[key] = html
    return html


@app.get("/health")
def health():
    """Liveness + DB connectivity check. `docker compose ps` and any
    external monitoring should hit this rather than assuming the container
    running means Postgres is reachable. Deliberately unauthenticated —
    it reveals nothing about the case data."""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}


@app.get("/api/ping")
def ping(user: dict = Depends(auth.require_user)):
    """Trivial authenticated smoke-test endpoint — confirms session auth is
    wired correctly end-to-end until real protected routes exist."""
    return {"pong": True, "user": user}


@app.get("/")
def index():
    # no-store rather than no-cache: this is the one document that decides
    # which version of everything else the browser will ask for, so it is
    # never worth serving from a cache at all.
    return HTMLResponse(_stamped_index(), headers={"Cache-Control": "no-store"})
