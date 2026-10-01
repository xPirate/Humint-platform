"""Field devices and the intake queue.

WHY A DEVICE IS NOT A USER SESSION

The companion app is a different kind of client from the web frontend, and
handing it a session would be the wrong shape twice. A session lasts twelve
hours, which is useless to somebody in a vehicle for a shift. And a session
can read the entire case file, which is the last thing you want on a device
that leaves the building.

So a device gets its own credential: long-lived, revocable on its own, and
write-only. There is no endpoint reachable with a device token that returns
case data — not an entity, not a report, not a list, not even its own past
submissions. A lost phone leaks whatever is still queued on that phone and
nothing else, and that is a property of the routing rather than a promise the
app keeps.

The token is shown once, at enrollment, and stored as a hash. Same reasoning as
a password: a table an administrator can read back is a table an attacker can
read back.

WHY SUBMISSIONS DO NOT BECOME REPORTS

Everything that arrives from outside works this way here — feed items,
extraction suggestions, link signals — and the rule is the same: nothing
enters the case file until a person puts it there. A submission lands in its
own queue, an analyst reads it, and promotes it to a Report or rejects it.
That also means a compromised device cannot quietly author case content.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

from fastapi import (APIRouter, Depends, File, Form, Header, HTTPException,
                     Query, Request, UploadFile)
from pydantic import BaseModel, Field

import audit
import auth
import extraction as extraction_module
import field_templates as templates
import reports as reports_module
from db import db_cursor
from idgen import generate_id

logger = logging.getLogger(__name__)

# Two routers on purpose, so the write-only surface is one grep away from
# being auditable. Anything under /api/intake takes a device token and returns
# nothing but an acknowledgement; anything under /api/field takes a session.
intake = APIRouter(prefix="/api/intake", tags=["field-intake"])
manage = APIRouter(prefix="/api/field", tags=["field-management"])

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")
MAX_SUBMISSION_CHARS = int(os.environ.get("FIELD_MAX_SUBMISSION_CHARS", "20000"))
# Raised from 25 MB when video arrived: half a minute of 1080p off a Pixel is
# comfortably past the old cap, and an analyst who cannot send the clip they
# just shot will stop shooting clips. The per-submission total below is what
# actually bounds the damage a stuck device can do, since the rate limit alone
# multiplied by a big per-file cap is a lot of disk a minute.
MAX_FILE_BYTES = int(os.environ.get("FIELD_MAX_FILE_MB", "150")) * 1024 * 1024
MAX_SUBMISSION_BYTES = int(os.environ.get("FIELD_MAX_SUBMISSION_MB", "300")) * 1024 * 1024

# A field device sends what a phone records. Anything else is a mistake or
# something worse, and there is no reason for this endpoint to accept it.
# application/octet-stream is on the list because Android share intents use it
# for perfectly ordinary media.
ALLOWED_MEDIA_PREFIXES = ("image/", "audio/", "video/")
ALLOWED_MEDIA_EXACT = ("application/octet-stream", "text/plain")
MAX_FILES_PER_SUBMISSION = int(os.environ.get("FIELD_MAX_FILES", "10"))

# A phone with a stuck retry loop should not be able to fill the disk. Counted
# in this process only, which is honest for a single API container and is the
# cheap 95% — a real limiter would need shared state, and the thing being
# defended against here is a bug in our own app, not an adversary.
RATE_WINDOW_SECONDS = 60
RATE_MAX_PER_WINDOW = int(os.environ.get("FIELD_RATE_PER_MINUTE", "30"))
_rate: dict = {}

# From the template registry, so the app's picker and the console's CHECK
# constraint cannot drift apart.
CRITICALITIES = tuple(templates.CRITICALITY)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Device authentication
# ---------------------------------------------------------------------------

def require_device_unmetered(request: Request,
                             authorization: str | None = Header(default=None)) -> dict:
    """Resolve `Authorization: Bearer <token>` to a live device.

    Deliberately returns the same 401 for a missing, malformed, unknown and
    revoked token. A caller holding a wrong token learns only that it is
    wrong, which is all they are owed.

    This variant does not count against the rate limit, and is used only by
    /hello — an app with a queue to drain will ping that on every network
    change, and a liveness check that can exhaust the budget meant for actual
    submissions is a liveness check that causes the outage it reports.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="A device token is required.")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="A device token is required.")

    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT d.id, d.label, d.token_hash, d.scope, d.revoked_at, "
            "       d.user_id, u.username, u.is_active "
            "  FROM field_devices d JOIN users u ON u.id = d.user_id "
            " WHERE d.token_hash = %s", (_hash_token(token),))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="That device token is not valid.")
        (device_id, label, stored_hash, scope, revoked_at,
         user_id, username, user_active) = row
        # The lookup was already by hash, so this adds nothing against a
        # remote attacker — it is here so the comparison is constant-time
        # anywhere this function gets reused against a smaller candidate set.
        if not hmac.compare_digest(stored_hash, _hash_token(token)):
            raise HTTPException(status_code=401, detail="That device token is not valid.")
        if revoked_at is not None:
            raise HTTPException(status_code=401, detail="That device token is not valid.")
        if not user_active:
            raise HTTPException(status_code=401, detail="That device token is not valid.")

        client = request.client.host if request.client else None
        cur.execute("UPDATE field_devices SET last_seen_at = now(), last_seen_ip = %s "
                    " WHERE id = %s", (client, device_id))

    return {"id": device_id, "label": label, "scope": scope,
            "user_id": user_id, "username": username}


def require_device(device: dict = Depends(require_device_unmetered)) -> dict:
    """The metered version, used by everything that writes."""
    now = time.monotonic()
    hits = [t for t in _rate.get(device["id"], []) if now - t < RATE_WINDOW_SECONDS]
    if len(hits) >= RATE_MAX_PER_WINDOW:
        _rate[device["id"]] = hits
        raise HTTPException(
            status_code=429,
            detail=f"Too many submissions from this device — more than "
                   f"{RATE_MAX_PER_WINDOW} in a minute. Wait and retry.")
    hits.append(now)
    _rate[device["id"]] = hits
    return device


# ---------------------------------------------------------------------------
# What a device may do: post a submission, attach a file, and nothing else
# ---------------------------------------------------------------------------

@intake.get("/hello")
def hello(device: dict = Depends(require_device_unmetered)):
    """Does this token still work, and what does the server call this device.

    The app needs to be able to say "enrolled, ready" without sending
    anything, and a revoked device needs to find out before somebody types a
    report into it. Returns nothing about the case file — the device's own
    label and the server's clock, which is also how the app can tell that its
    own clock has drifted.
    """
    return {"ok": True, "device": device["label"], "user": device["username"],
            "scope": device["scope"], "server_time": datetime.now(timezone.utc).isoformat(),
            "max_chars": MAX_SUBMISSION_CHARS, "max_file_bytes": MAX_FILE_BYTES,
            "max_submission_bytes": MAX_SUBMISSION_BYTES,
            "max_files": MAX_FILES_PER_SUBMISSION,
            # So the app can say "this console is running a newer set of forms
            # than I have" in the one place a person can act on it, rather
            # than silently sending fields nobody will render properly.
            "templates": templates.summary()}


class Submission(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str | None = None
    criticality: str | None = None
    observed_at: datetime | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    location_accuracy_m: float | None = Field(default=None, ge=0, le=100000)
    location_note: str | None = None
    # The app's own id. A phone that sends, loses signal before hearing back,
    # and retries must not create two rows — so the client picks an id and a
    # repeat is treated as the same submission.
    client_ref: str | None = Field(default=None, max_length=128)

    # Which of the shapes in field_templates.json this was written as, and
    # the registry version the app held at the time. Both optional: posting
    # by hand with curl names neither, and an app one version ahead names a
    # template this console has not heard of. See the note on the insert.
    template: str | None = Field(default=None, max_length=64)
    template_version: int | None = None
    fields: dict | None = None


@intake.post("/submissions", status_code=201)
def create_submission(payload: Submission, device: dict = Depends(require_device)):
    if payload.criticality and payload.criticality not in CRITICALITIES:
        raise HTTPException(status_code=400,
                            detail=f"Criticality must be one of {', '.join(CRITICALITIES)}.")
    body = (payload.body or "")
    if len(body) > MAX_SUBMISSION_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"That submission is longer than {MAX_SUBMISSION_CHARS} characters.")

    # Deliberately not validated against the registry. A report is never
    # refused over its shape: an unknown template or a field key this console
    # has never seen is stored as sent and flagged to whoever works the
    # queue. Somebody stood outside and typed that in — losing it because the
    # two ends disagree about a name would be the worst bug this feature
    # could have.
    fields = templates.clean_fields(payload.fields)
    template = (payload.template or "").strip()[:64] or None

    with db_cursor(commit=True) as cur:
        if payload.client_ref:
            cur.execute("SELECT id, status FROM field_submissions "
                        " WHERE device_id = %s AND client_ref = %s",
                        (device["id"], payload.client_ref))
            existing = cur.fetchone()
            if existing:
                # Not an error. The phone is retrying something that already
                # landed, and the only useful answer is the id it already has.
                return {"id": existing[0], "duplicate": True, "status": existing[1]}

        cur.execute(
            "INSERT INTO field_submissions (device_id, device_label, user_id, title, body, "
            "        criticality, observed_at, lat, lng, location_accuracy_m, location_note, "
            "        client_ref, template, template_version, fields) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (device["id"], device["label"], device["user_id"], payload.title.strip(),
             body or None, payload.criticality, payload.observed_at, payload.lat, payload.lng,
             payload.location_accuracy_m, payload.location_note, payload.client_ref,
             template, payload.template_version, json.dumps(fields)))
        submission_id = cur.fetchone()[0]
        cur.execute("UPDATE field_devices SET submission_count = submission_count + 1 "
                    " WHERE id = %s", (device["id"],))

    audit.record("field.submission", actor_kind="device",
                 actor_username=device["username"], object_type="field_submission",
                 object_id=submission_id, object_label=payload.title.strip(),
                 detail={"device": device["label"], "criticality": payload.criticality,
                         "has_position": payload.lat is not None, "template": template,
                         "template_version": payload.template_version})
    return {"id": submission_id, "duplicate": False, "status": "new"}


@intake.post("/submissions/{submission_id}/files", status_code=201)
async def attach_file(submission_id: int,
                      file: UploadFile = File(...),
                      client_ref: str | None = Form(default=None),
                      # The phone already knows how long the clip is. Getting
                      # it server-side means ffprobe in the api image, which
                      # is a lot of megabytes to answer "how long is this" on
                      # a queue card.
                      duration_ms: int | None = Form(default=None),
                      device: dict = Depends(require_device)):
    """A photo, usually.

    Separate from the submission itself so the app can send the text the
    moment it has a sliver of signal and let the pictures follow — which is
    the difference between a report arriving and a report waiting for a
    30 MB upload to succeed.
    """
    kind = (file.content_type or "").split(";")[0].strip().lower()
    if kind and not (kind.startswith(ALLOWED_MEDIA_PREFIXES) or kind in ALLOWED_MEDIA_EXACT):
        raise HTTPException(
            status_code=415,
            detail="A field device sends pictures, audio and video. "
                   f"This was {kind}.")

    with db_cursor() as cur:
        cur.execute("SELECT device_id, status FROM field_submissions WHERE id = %s",
                    (submission_id,))
        row = cur.fetchone()
        if row is None or row[0] != device["id"]:
            # Same answer for "no such submission" and "not yours": a device
            # must not be able to probe for other devices' ids.
            raise HTTPException(status_code=404, detail="No such submission.")
        if row[1] != "new":
            raise HTTPException(status_code=409,
                                detail="That submission has already been dealt with.")
        cur.execute("SELECT count(*), coalesce(sum(file_size_bytes), 0) "
                    "  FROM field_submission_files WHERE submission_id = %s",
                    (submission_id,))
        file_count, bytes_so_far = cur.fetchone()
        if file_count >= MAX_FILES_PER_SUBMISSION:
            raise HTTPException(status_code=409,
                                detail=f"A submission may carry {MAX_FILES_PER_SUBMISSION} files.")
        if bytes_so_far >= MAX_SUBMISSION_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"That submission already carries "
                       f"{MAX_SUBMISSION_BYTES // (1024 * 1024)} MB of media.")
        if client_ref:
            cur.execute("SELECT id FROM field_submission_files "
                        " WHERE submission_id = %s AND client_ref = %s",
                        (submission_id, client_ref))
            dup = cur.fetchone()
            if dup:
                return {"id": dup[0], "duplicate": True}

    # Read in bounded chunks: awaiting the whole body first would buffer an
    # oversized upload into memory before the size check could reject it.
    chunks, total = [], 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"That file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        if bytes_so_far + total > MAX_SUBMISSION_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"That would take the submission past "
                       f"{MAX_SUBMISSION_BYTES // (1024 * 1024)} MB of media.")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise HTTPException(status_code=400, detail="That file is empty.")

    now = datetime.now(timezone.utc)
    rel_dir = os.path.join("field", str(now.year), f"{now.month:02d}")
    os.makedirs(os.path.join(UPLOAD_DIR, rel_dir), exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1][:12]
    ext = ext if re.fullmatch(r"\.[A-Za-z0-9]+", ext or "") else ""
    storage_path = os.path.join(rel_dir, f"{uuid.uuid4().hex}{ext}")
    with open(os.path.join(UPLOAD_DIR, storage_path), "wb") as fh:
        fh.write(content)

    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO field_submission_files (submission_id, filename, storage_path, "
                "        mime_type, file_size_bytes, client_ref, duration_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (submission_id, os.path.basename(file.filename or "photo"), storage_path,
                 file.content_type, len(content), client_ref,
                 duration_ms if duration_ms and 0 < duration_ms < 86_400_000 else None))
            file_id = cur.fetchone()[0]
    except Exception:
        try:
            os.remove(os.path.join(UPLOAD_DIR, storage_path))
        except OSError:
            pass
        raise
    return {"id": file_id, "duplicate": False, "bytes": len(content)}


# ---------------------------------------------------------------------------
# Enrolling and revoking devices (admin)
# ---------------------------------------------------------------------------

class EnrollRequest(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    # Whose device. Defaults to the admin doing the enrolling, which is the
    # common case of setting up your own phone.
    user_id: int | None = None
    # What the app should connect to. An admin knows the LAN address the
    # server answers on; this server does not reliably know its own. Kept on
    # the row so re-issuing a code later does not make somebody retype it.
    base_url: str | None = None


class ReissueRequest(BaseModel):
    """Showing a device's code again. See the endpoint for why this mints a
    new token rather than retrieving the old one."""
    base_url: str | None = None


def _device_row(row) -> dict:
    (did, label, prefix, scope, revoked_at, revoked_by, last_seen_at, last_seen_ip,
     count, created_at, username, enrolled_by_name, base_url) = row
    return {"id": did, "label": label, "token_prefix": prefix, "scope": scope,
            "revoked": revoked_at is not None,
            "revoked_at": revoked_at.isoformat() if revoked_at else None,
            "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
            "last_seen_ip": last_seen_ip, "submission_count": count,
            "created_at": created_at.isoformat() if created_at else None,
            "username": username, "enrolled_by": enrolled_by_name,
            "base_url": base_url}


_DEVICE_SELECT = """
    SELECT d.id, d.label, d.token_prefix, d.scope, d.revoked_at, d.revoked_by,
           d.last_seen_at, d.last_seen_ip, d.submission_count, d.created_at,
           u.username, e.username, d.base_url
      FROM field_devices d
      JOIN users u ON u.id = d.user_id
      LEFT JOIN users e ON e.id = d.enrolled_by
"""


@manage.get("/devices")
def list_devices(user: dict = Depends(auth.require_admin)):
    with db_cursor() as cur:
        cur.execute(_DEVICE_SELECT + " ORDER BY d.revoked_at NULLS FIRST, d.label")
        return {"items": [_device_row(r) for r in cur.fetchall()]}


@manage.post("/devices", status_code=201)
def enroll_device(payload: EnrollRequest, user: dict = Depends(auth.require_admin)):
    """Create a device credential. The token is in this response and nowhere
    else, ever again."""
    target = payload.user_id or user["id"]
    token = secrets.token_urlsafe(32)
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT username, is_active FROM users WHERE id = %s", (target,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such user.")
        if not row[1]:
            raise HTTPException(status_code=400,
                                detail="That account is deactivated, so its devices could not post.")
        cur.execute(
            "INSERT INTO field_devices (label, user_id, token_hash, token_prefix, "
            "        enrolled_by, base_url) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (payload.label.strip(), target, _hash_token(token), token[:6], user["id"],
             (payload.base_url or "").strip() or None))
        device_id = cur.fetchone()[0]

    audit.record("field.device.enroll", user=user, object_type="field_device",
                 object_id=device_id, object_label=payload.label.strip(),
                 detail={"for_user": row[0]})
    # The QR payload the app will scan. Versioned from the start so a later
    # app can tell an old code from a new one rather than guessing.
    enrollment = {"v": 1, "url": (payload.base_url or "").strip() or None,
                 "token": token, "label": payload.label.strip(), "user": row[0]}
    return {"id": device_id, "token": token, "username": row[0],
            "enrollment": enrollment, "enrollment_qr": _qr_svg(json.dumps(enrollment))}


def _qr_svg(text: str) -> str | None:
    """An SVG QR, rendered here rather than in the browser.

    The frontend loads no external scripts by design — the whole app has to
    work on a machine with no internet — so a client-side QR library would
    have to be vendored. segno is pure Python, has no dependencies of its own,
    and this is the only place it is used. Absent, the page falls back to
    showing the token as text, which still enrolls a device by typing.
    """
    try:
        import segno
    except ImportError:
        return None
    try:
        import io
        # segno writes bytes, so a text buffer here fails with a TypeError
        # that the except below would swallow into a silently missing QR.
        buf = io.BytesIO()
        segno.make(text, error="m").save(buf, kind="svg", scale=4, border=2)
        return buf.getvalue().decode("utf-8")
    except Exception:
        logger.exception("could not render enrollment QR")
        return None


@manage.post("/devices/{device_id}/code", status_code=201)
def reissue_code(device_id: int, payload: ReissueRequest,
                 user: dict = Depends(auth.require_admin)):
    """Show this device's code again — by issuing a new one.

    The obvious reading of "show me the code again" is that the console
    should hand back the token it gave out before. It cannot: only a SHA-256
    of the token was ever stored, deliberately, so that a console somebody
    gets into does not yield a set of working device credentials. That
    property is worth more than the convenience, so this does the other
    thing and mints a fresh token for the same device.

    The reason that is an acceptable answer rather than a fudge: **the app
    stores no credentials.** It scans a code at the moment it uploads and
    forgets it immediately afterwards. So a token only has to be valid for
    the minute it is being scanned, and rotating it between uploads costs
    nothing — nothing on any handset is invalidated, because nothing on any
    handset is holding the old one.

    What it does invalidate is a code somebody already printed and pinned
    up. The response says so and the page says so, in those words.

    The device keeps its identity: same row, same label, same account, same
    submission history. Everything it has already sent stays attached to it.
    """
    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT d.label, d.revoked_at, d.base_url, u.username, u.is_active "
            "  FROM field_devices d JOIN users u ON u.id = d.user_id WHERE d.id = %s",
            (device_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such device.")
        label, revoked_at, stored_url, username, is_active = row
        if revoked_at is not None:
            raise HTTPException(
                status_code=409,
                detail="That device is revoked. Enroll it again rather than "
                       "handing a revoked one a working code.")
        if not is_active:
            raise HTTPException(
                status_code=400,
                detail=f"{username}'s account is deactivated, so a new code would not work.")

        token = secrets.token_urlsafe(32)
        base_url = (payload.base_url or "").strip() or stored_url
        cur.execute(
            "UPDATE field_devices SET token_hash = %s, token_prefix = %s, base_url = %s "
            " WHERE id = %s",
            (_hash_token(token), token[:6], base_url, device_id))

    audit.record("field.device.reissue", user=user, object_type="field_device",
                 object_id=device_id, object_label=label,
                 detail={"for_user": username,
                         "note": "a new token was issued; any previously shown code "
                                 "for this device stopped working"})
    enrollment = {"v": 1, "url": base_url, "token": token, "label": label, "user": username}
    return {"id": device_id, "token": token, "username": username, "label": label,
            "base_url": base_url, "replaced_previous": True,
            "enrollment": enrollment, "enrollment_qr": _qr_svg(json.dumps(enrollment))}


@manage.post("/devices/{device_id}/revoke")
def revoke_device(device_id: int, user: dict = Depends(auth.require_admin)):
    """Stop this device posting. Immediate, and what it already sent stays."""
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT label, revoked_at FROM field_devices WHERE id = %s", (device_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such device.")
        if row[1] is not None:
            return {"id": device_id, "revoked": True, "already": True}
        cur.execute("UPDATE field_devices SET revoked_at = now(), revoked_by = %s "
                    " WHERE id = %s", (user["id"], device_id))
    audit.record("field.device.revoke", user=user, object_type="field_device",
                 object_id=device_id, object_label=row[0])
    return {"id": device_id, "revoked": True, "already": False}


@manage.delete("/devices/{device_id}", status_code=204)
def delete_device(device_id: int, user: dict = Depends(auth.require_admin)):
    """Remove the record of a device entirely. What it sent is kept — the
    submission rows carry a copy of the label for exactly this."""
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT label FROM field_devices WHERE id = %s", (device_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such device.")
        cur.execute("DELETE FROM field_devices WHERE id = %s", (device_id,))
    audit.record("field.device.delete", user=user, object_type="field_device",
                 object_id=device_id, object_label=row[0])


# ---------------------------------------------------------------------------
# The intake queue (any analyst)
# ---------------------------------------------------------------------------

@manage.get("/submissions")
def list_submissions(status: str = Query(default="new"),
                     limit: int = Query(default=100, ge=1, le=500),
                     user: dict = Depends(auth.require_user)):
    where, params = [], []
    if status != "all":
        if status not in ("new", "accepted", "rejected"):
            raise HTTPException(status_code=400, detail="Unknown status.")
        where.append("s.status = %s")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db_cursor() as cur:
        cur.execute(f"SELECT count(*) FROM field_submissions s{clause}", params)
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT s.id, s.device_label, s.title, s.body, s.criticality, s.observed_at, "
            "       s.lat, s.lng, s.location_accuracy_m, s.location_note, s.status, "
            "       s.report_id, s.received_at, s.handled_at, s.handled_note, "
            "       u.username, h.username, "
            "       (SELECT count(*) FROM field_submission_files f WHERE f.submission_id = s.id), "
            "       s.template, s.template_version, s.fields "
            "  FROM field_submissions s "
            "  LEFT JOIN users u ON u.id = s.user_id "
            "  LEFT JOIN users h ON h.id = s.handled_by "
            + clause
            # Waiting is a triage list, so it leads with how urgent the
            # sender said it was and only then with what arrived last. The
            # handled lists are history, and history reads in date order.
            + (" ORDER BY CASE s.criticality WHEN 'Flash' THEN 0 WHEN 'Immediate' THEN 1 "
               "                             WHEN 'Priority' THEN 2 WHEN 'Routine' THEN 3 "
               "                             ELSE 4 END, s.received_at DESC LIMIT %s"
               if status == "new" else " ORDER BY s.received_at DESC LIMIT %s"),
            [*params, limit])
        items = []
        for r in cur.fetchall():
            items.append({
                "id": r[0], "device_label": r[1], "title": r[2], "body": r[3],
                "criticality": r[4],
                "observed_at": r[5].isoformat() if r[5] else None,
                "lat": r[6], "lng": r[7], "location_accuracy_m": r[8],
                "location_note": r[9], "status": r[10], "report_id": r[11],
                "received_at": r[12].isoformat() if r[12] else None,
                "handled_at": r[13].isoformat() if r[13] else None,
                "handled_note": r[14], "from_user": r[15], "handled_by": r[16],
                "file_count": r[17],
                "template": r[18], "template_version": r[19], "fields": r[20] or {},
                # Laid out here rather than in the browser so the rule about
                # what happens to a field nobody recognises lives in one
                # place, and so an export or a script sees the same thing the
                # queue card does.
                "layout": templates.render(r[18], r[20] or {}),
                "app_ahead": bool(r[19] and r[19] > templates.VERSION),
                "suggests": (templates.entity_draft(
                    r[18], r[20] or {}, lat=r[6], lng=r[7], observed_at=r[5])
                    if r[10] == "new" else None),
            })
        if items:
            cur.execute(
                "SELECT submission_id, id, filename, mime_type, file_size_bytes, duration_ms "
                "  FROM field_submission_files WHERE submission_id = ANY(%s) ORDER BY id",
                ([i["id"] for i in items],))
            by_sub: dict = {}
            for sid, fid, name, mime, size, duration in cur.fetchall():
                by_sub.setdefault(sid, []).append(
                    {"id": fid, "filename": name, "mime_type": mime, "size": size,
                     "duration_ms": duration, "kind": _media_kind(mime, name)})
            for i in items:
                i["files"] = by_sub.get(i["id"], [])
    return {"items": items, "total": total,
            "unhandled": total if status == "new" else None}


def _media_kind(mime: str | None, filename: str | None) -> str:
    """image / audio / video / file — what the card should do with it.

    The mime type is what the phone claimed, and Android share intents claim
    application/octet-stream for perfectly ordinary media, so the extension
    is the fallback rather than the other way round.
    """
    mime = (mime or "").lower()
    for kind in ("image", "audio", "video"):
        if mime.startswith(kind + "/"):
            return kind
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"):
        return "image"
    if ext in (".m4a", ".aac", ".mp3", ".ogg", ".opus", ".wav", ".3gp", ".amr"):
        return "audio"
    if ext in (".mp4", ".mkv", ".webm", ".mov", ".3gpp"):
        return "video"
    return "file"


@manage.get("/submissions/{submission_id}/files/{file_id}")
def submission_file(submission_id: int, file_id: int,
                    request: Request,
                    user: dict = Depends(auth.require_user)):
    """Serve a submitted photo, clip or recording, so it can be looked at —
    or listened to — before anyone decides whether to accept it."""
    from fastapi.responses import FileResponse
    with db_cursor() as cur:
        cur.execute("SELECT storage_path, filename, mime_type FROM field_submission_files "
                    " WHERE id = %s AND submission_id = %s", (file_id, submission_id))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such file.")
    abs_path = os.path.join(UPLOAD_DIR, row[0])
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="That file is missing from disk.")
    # Only the opening request is audited. A <video> or <audio> element seeks
    # with Range requests, and a single analyst scrubbing through one clip
    # would otherwise write dozens of near-identical rows into the one log
    # that has to stay readable. The request without a Range header is the
    # one that means "somebody opened this".
    if not request.headers.get("range"):
        audit.record("field.file.view", user=user, object_type="field_submission",
                     object_id=submission_id, object_label=row[1])
    # Inline rather than as a download: passing filename= to FileResponse is
    # what sets an "attachment" disposition, and these are meant to be looked
    # at in the queue card. The real name is still advertised, so "save as"
    # keeps it.
    return FileResponse(
        abs_path, media_type=row[2] or "application/octet-stream",
        headers={"Content-Disposition":
                 f'inline; filename="{reports_module._header_safe_filename(row[1])}"'})


class HandleRequest(BaseModel):
    note: str | None = None
    # Accepting a Vehicle sighting can also start the Vehicle record, with the
    # plate already in it. Opt-in per accept rather than automatic: the
    # standing rule is that a person decides what enters the case file, and
    # "the app made a record because a stranger sent a photo" is exactly the
    # thing that rule exists to prevent.
    create_entity: bool = False
    # Accepting: the report starts as a draft so the analyst finishes it in
    # the editor rather than the submission becoming case output untouched.
    title: str | None = None


def _entity_from_draft(cur, draft: dict, device_label, user: dict) -> dict:
    """Create the record a field report describes, inside the accept's own
    transaction.

    This goes through the same creator the extraction queue uses, for one
    reason above all: that path is *lenient*. A strict parse rejects the whole
    details block over one bad value, and an app a version ahead of this
    console will eventually send a body style or an alignment that is not in
    the list yet. Strict would mean the accept raises, the transaction rolls
    back, and a report somebody stood outside to write cannot be accepted at
    all — over a dropdown. Lenient drops that one field and keeps the record.
    """
    entity_type = draft["entity_type"]
    description = ("Started from a field report"
                   + (f" sent by {device_label}" if device_label else "")
                   + ". Check it before relying on it.")
    entity_id = extraction_module._create_entity(
        cur, user, entity_type, draft["name"],
        description=description, raw_details=draft["details"])
    return {"id": entity_id, "entity_type": entity_type, "name": draft["name"]}


@manage.post("/submissions/{submission_id}/accept", status_code=201)
def accept_submission(submission_id: int, payload: HandleRequest,
                      user: dict = Depends(auth.require_user)):
    """Promote a submission to a draft Report, carrying its photos across.

    A draft, not a confirmed report: what arrived is one person's account from
    the field, and the app's whole posture is that a human decides what the
    case file says. The analyst opens it in the editor, adds what they know,
    and confirms it there.
    """
    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT title, body, criticality, observed_at, lat, lng, location_accuracy_m, "
            "       location_note, status, device_label, user_id, template, fields "
            "  FROM field_submissions WHERE id = %s", (submission_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such submission.")
        (title, body, criticality, observed_at, lat, lng, accuracy,
         loc_note, status, device_label, from_user, template, fields) = row
        fields = fields or {}
        if status != "new":
            raise HTTPException(status_code=409, detail="That submission is already handled.")

        # A provenance block at the top, because six months later "who saw
        # this and from where" is most of what makes a field report usable.
        head = [f"*Field submission from {device_label or 'an unknown device'}"
                + (f", {observed_at.strftime('%Y-%m-%d %H:%M UTC')}" if observed_at else "")
                + ".*"]
        if lat is not None and lng is not None:
            pos = f"*Position: {lat:.5f}, {lng:.5f}"
            if accuracy is not None:
                pos += f" (±{int(accuracy)} m)"
            head.append(pos + ".*")
        if loc_note:
            head.append(f"*Place as described: {loc_note}.*")

        # The template's own fields, laid out, above whatever free text came
        # with it. A field nobody recognises is rendered under its raw key
        # rather than dropped, so a report from an app one version ahead is
        # still readable and still worth accepting.
        blocks = ["\n".join(head)]
        rendered = templates.as_markdown(template, fields)
        if rendered:
            blocks.append(rendered.rstrip())
        if body:
            blocks.append(body)
        markdown = "\n\n".join(blocks)

        report_id = generate_id("report", payload.title or title)
        cur.execute(
            "INSERT INTO reports (id, title, body_markdown, status, criticality, author_id) "
            "VALUES (%s, %s, %s, 'draft', %s, %s)",
            (report_id, (payload.title or title).strip(), markdown, criticality,
             from_user or user["id"]))

        # Photos move into real attachments, pointed at the new report. The
        # file on disk is not copied — the row moves, the bytes stay. It has to
        # be a move rather than a copy: two rows pointing at one path means
        # deleting the attachment from the report (which does unlink the file)
        # leaves the queue card showing a thumbnail that is no longer there.
        # From here the report owns the photo, and the submission's link to it
        # is its report_id.
        cur.execute("SELECT filename, storage_path, mime_type, file_size_bytes "
                    "  FROM field_submission_files WHERE submission_id = %s ORDER BY id",
                    (submission_id,))
        files = cur.fetchall()
        for name, path, mime, size in files:
            cur.execute(
                "INSERT INTO attachments (report_id, filename, title, source_note, "
                "        storage_path, mime_type, file_size_bytes, uploaded_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (report_id, name, name, f"Field submission from {device_label or 'a device'}",
                 path, mime, size, user["id"]))

        cur.execute("DELETE FROM field_submission_files WHERE submission_id = %s",
                    (submission_id,))

        # The record the report is about, pre-filled from the template, linked
        # to the draft so the analyst lands on both at once. Still theirs to
        # correct or delete — but nobody retypes a plate.
        created_entity = None
        draft = templates.entity_draft(template, fields, lat=lat, lng=lng,
                                       observed_at=observed_at)
        if payload.create_entity and draft:
            created_entity = _entity_from_draft(cur, draft, device_label, user)
            cur.execute("INSERT INTO report_entities (report_id, entity_id) VALUES (%s, %s) "
                        " ON CONFLICT DO NOTHING", (report_id, created_entity["id"]))

        cur.execute(
            "UPDATE field_submissions SET status = 'accepted', report_id = %s, "
            "       handled_by = %s, handled_at = now(), handled_note = %s WHERE id = %s",
            (report_id, user["id"], (payload.note or "").strip() or None, submission_id))

    audit.record("field.submission.accept", user=user, object_type="field_submission",
                 object_id=submission_id, object_label=title,
                 detail={"report_id": report_id, "files": len(files),
                         "template": template,
                         "entity_id": created_entity["id"] if created_entity else None})
    if created_entity:
        audit.record("entity.create", user=user, object_type="entity",
                     object_id=created_entity["id"], object_label=created_entity["name"],
                     detail={"entity_type": created_entity["entity_type"],
                             "from_field_submission": submission_id})
    return {"id": submission_id, "report_id": report_id, "files": len(files),
            "entity": created_entity}


@manage.post("/submissions/{submission_id}/reject")
def reject_submission(submission_id: int, payload: HandleRequest,
                      user: dict = Depends(auth.require_user)):
    """Set it aside. Kept, not deleted: what a field device sent is part of
    the record of what happened, whether or not it was useful."""
    with db_cursor(commit=True) as cur:
        cur.execute("SELECT title, status FROM field_submissions WHERE id = %s",
                    (submission_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such submission.")
        if row[1] != "new":
            raise HTTPException(status_code=409, detail="That submission is already handled.")
        cur.execute(
            "UPDATE field_submissions SET status = 'rejected', handled_by = %s, "
            "       handled_at = now(), handled_note = %s WHERE id = %s",
            (user["id"], (payload.note or "").strip() or None, submission_id))
    audit.record("field.submission.reject", user=user, object_type="field_submission",
                 object_id=submission_id, object_label=row[0],
                 detail={"note": payload.note})
    return {"id": submission_id, "status": "rejected"}
