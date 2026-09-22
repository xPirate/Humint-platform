"""Whole-instance backup and restore.

Two admin-only endpoints: GET /api/admin/backup downloads a .zip of
everything, POST /api/admin/restore replaces everything with the contents of
one of those zips. Built for two jobs — moving a deployment to another
machine (a Pi, a new laptop, a rebuilt server) and getting back to a known
state without re-typing a case by hand.

WHAT'S IN A BACKUP

Every table in the `public` schema except `sessions`, discovered from
information_schema at backup time rather than hardcoded here. That's
deliberate: a hardcoded table list is one forgotten line away from a backup
that silently omits a whole feature's data, and you'd only find out on the
day you actually needed to restore it. Discovery means a table added later is
included automatically, with no maintenance here and no silent gap.

That includes `users` — usernames, roles, and bcrypt password hashes — so
people can log in on the restored instance with the passwords they already
have. It means a backup file is as sensitive as the database itself; see
"Backup and restore" in README.md.

`sessions` is the sole exclusion, and it's excluded in both directions: never
written to a backup, and always wiped on restore. Restoring a different
users table underneath live session rows would leave tokens pointing at user
ids that now belong to someone else entirely — so everyone, including the
admin who just clicked Restore, logs in again afterward.

Attachments are bundled as real files under uploads/ in the zip, keyed by
their storage_path, so a restored instance has working documents/images
rather than rows pointing at files that aren't there.

WHY JSON AND NOT pg_dump

pg_dump would be more faithful and less code, but it needs the postgres
client binaries in the api image, ties a backup to a Postgres major version,
and can't be restored from inside the running app without dropping the schema
out from under the very connection doing the work. A JSON payload is
inspectable (open data.json and read it), portable across Postgres versions,
and restorable inside one ordinary transaction — which is what makes the
safety property below possible.

THE SAFETY PROPERTY

A restore wipes real data, so the entire DB half runs in ONE transaction:
TRUNCATE every table, re-insert, reset sequences, commit. Postgres makes
TRUNCATE transactional, so any failure anywhere in that block — a bad row, a
constraint violation, a dropped connection — rolls the whole thing back and
leaves the existing data exactly as it was. There is deliberately no
half-restored state to recover from.

Attachment files can't participate in that transaction, so they're handled
around it: the zip is fully validated before anything is touched, files are
extracted to a staging directory inside UPLOAD_DIR (same filesystem, so
moving them into place afterward is a rename, not a copy), and they're only
moved into place after the database transaction has committed.
"""

import json
import os
import shutil
import tempfile
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg2
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from psycopg2.extras import execute_values
from starlette.background import BackgroundTask

import audit
import auth
from db import db_cursor

router = APIRouter(prefix="/api/admin", tags=["admin"])

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")

# Deliberately NOT MAX_UPLOAD_MB (25 by default): that limit exists to stop
# someone attaching a 500 MB video to a report, whereas a legitimate backup of
# a case with real scans in it can easily be bigger than any single
# attachment. Same reason it's a separate .env knob rather than a shared one.
MAX_RESTORE_BYTES = int(os.environ.get("MAX_RESTORE_MB", "2048")) * 1024 * 1024

BACKUP_FORMAT = "humint-platform-backup"
BACKUP_FORMAT_VERSION = 1

# See the module docstring — excluded from backups AND always wiped on
# restore, which is why this is a set of its own rather than just an omission.
EXCLUDED_TABLES = {"sessions"}

# Typed into the restore form to confirm the destructive action. Matched
# exactly (after stripping surrounding whitespace) — an accidental click can
# produce a submitted form, but it can't produce this string.
RESTORE_CONFIRM_PHRASE = "REPLACE ALL DATA"

_STAGING_PREFIX = ".restore-staging-"


# ----------------------------------------------------------------------------
# Schema introspection
# ----------------------------------------------------------------------------

def _all_tables(cur) -> list[str]:
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
        "ORDER BY table_name"
    )
    return [r[0] for r in cur.fetchall()]


def _table_columns(cur, table: str) -> dict:
    """{column_name: data_type} in ordinal order (dicts keep insertion order,
    so callers that want the column list get it in the table's own order)."""
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return {name: data_type for name, data_type in cur.fetchall()}


def _serial_columns(cur) -> list[tuple[str, str]]:
    """(table, column) for every column backed by a sequence — i.e. SERIAL
    primary keys. Restoring explicit ids leaves those sequences behind the
    data, so they have to be fast-forwarded afterward or the next insert
    collides with a restored row; see _reset_sequences."""
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_default LIKE 'nextval(%%'"
    )
    return [(t, c) for t, c in cur.fetchall()]


def _fk_edges(cur) -> list[tuple[str, str]]:
    """(child, parent) for every foreign key inside the public schema.
    Self-references are dropped — a table can't be ordered before itself, and
    none of this schema's FKs are self-referential anyway."""
    cur.execute(
        """
        SELECT child.relname, parent.relname
        FROM pg_constraint con
        JOIN pg_class child ON child.oid = con.conrelid
        JOIN pg_class parent ON parent.oid = con.confrelid
        JOIN pg_namespace ns ON ns.oid = child.relnamespace
        WHERE con.contype = 'f' AND ns.nspname = 'public'
        """
    )
    return [(c, p) for c, p in cur.fetchall() if c != p]


def _insertion_order(tables: list[str], edges: list[tuple[str, str]]) -> list[str]:
    """Topological sort so a row is never inserted before the row it
    references — entities before person_details, users before entities, and
    so on. Derived from the live FK graph rather than a hand-maintained
    ordering for the same reason the table list is: a new FK added later
    orders itself correctly with no changes here."""
    table_set = set(tables)
    depends_on = {t: set() for t in tables}
    for child, parent in edges:
        if child in table_set and parent in table_set:
            depends_on[child].add(parent)

    ordered: list[str] = []
    placed: set[str] = set()
    remaining = set(tables)
    while remaining:
        ready = sorted(t for t in remaining if depends_on[t] <= placed)
        if not ready:
            # A genuine FK cycle, which this schema doesn't have and which
            # couldn't be inserted in any order without deferrable
            # constraints. Fall through in name order rather than looping
            # forever -- the insert itself will then fail loudly (and roll
            # back) instead of hanging.
            ordered.extend(sorted(remaining))
            break
        for t in ready:
            ordered.append(t)
            placed.add(t)
            remaining.discard(t)
    return ordered


# ----------------------------------------------------------------------------
# Value encoding
# ----------------------------------------------------------------------------

def _json_default(value):
    """psycopg2 hands back real Python objects for dates/times/numerics;
    json can't serialize those, so they go out as ISO-8601 strings and plain
    numbers. Both come back in as untyped literals on restore, which Postgres
    coerces to the destination column's type -- see _adapt."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, bytes):
        raise TypeError(
            "This database has a binary (bytea) column, which the JSON backup "
            "format doesn't cover. No table in this app's schema uses one -- if "
            "you added it, back that column up separately."
        )
    raise TypeError(f"Don't know how to put a {type(value).__name__} in a backup")


def _adapt(data_type: str, value):
    """json/jsonb columns are the one type that needs help on the way back in:
    psycopg2 would try to adapt a Python dict/list directly and fail, so the
    value is re-serialized to a JSON string, which Postgres coerces to
    json/jsonb. Everything else round-trips as-is -- lists become Postgres
    arrays, ISO strings become timestamps/dates, None becomes NULL."""
    if data_type in ("json", "jsonb"):
        return None if value is None else json.dumps(value)
    return value


# ----------------------------------------------------------------------------
# Upload-path safety
# ----------------------------------------------------------------------------

def _safe_upload_path(storage_path: str):
    """Resolve a storage_path (e.g. "2026/09/abc123.pdf") against UPLOAD_DIR,
    returning None for anything that would escape it. storage_path values in
    a backup came from a file this app wrote, but a backup is an
    admin-uploaded file like any other -- a hand-edited one containing
    "../../etc/whatever" must not be able to read or write outside the
    uploads volume."""
    if not storage_path or os.path.isabs(storage_path) or "\x00" in storage_path:
        return None
    base = os.path.realpath(UPLOAD_DIR)
    target = os.path.realpath(os.path.join(base, storage_path))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


# ----------------------------------------------------------------------------
# Backup
# ----------------------------------------------------------------------------

@router.get("/backup")
def download_backup(user: dict = Depends(auth.require_admin)):
    with db_cursor() as cur:
        tables = [t for t in _all_tables(cur) if t not in EXCLUDED_TABLES]
        table_blocks = {}
        for table in tables:
            columns = list(_table_columns(cur, table).keys())
            col_sql = ", ".join(f'"{c}"' for c in columns)
            cur.execute(f'SELECT {col_sql} FROM "{table}"')
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            table_blocks[table] = {"columns": columns, "rows": rows}

    storage_paths = [
        row["storage_path"]
        for row in table_blocks.get("attachments", {}).get("rows", [])
        if row.get("storage_path")
    ]

    created_at = datetime.now(timezone.utc)
    stamp = created_at.strftime("%Y%m%d-%H%M%S")
    filename = f"humint-backup-{stamp}.zip"

    # Built into a temp directory and streamed from disk rather than held in
    # memory: a case with real scans attached makes for a big file, and
    # FileResponse can stream it out while the BackgroundTask below cleans up
    # once the client has it.
    tmp_dir = tempfile.mkdtemp(prefix="humint-backup-")
    zip_path = os.path.join(tmp_dir, filename)

    try:
        files_included, files_missing = 0, []
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for storage_path in storage_paths:
                abs_path = _safe_upload_path(storage_path)
                if abs_path and os.path.isfile(abs_path):
                    zf.write(abs_path, f"uploads/{storage_path}")
                    files_included += 1
                else:
                    # A row whose file vanished from the volume (deleted by
                    # hand, or a volume restored without its contents). Worth
                    # recording in the manifest rather than failing the whole
                    # backup over -- the row itself is still worth keeping.
                    files_missing.append(storage_path)

            manifest = {
                "format": BACKUP_FORMAT,
                "format_version": BACKUP_FORMAT_VERSION,
                "created_at": created_at.isoformat(),
                "created_by": user["username"],
                "row_counts": {t: len(b["rows"]) for t, b in table_blocks.items()},
                "attachment_files_included": files_included,
                "attachment_files_missing": files_missing,
                "excluded_tables": sorted(EXCLUDED_TABLES),
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.writestr(
                "data.json",
                json.dumps(
                    {
                        "format": BACKUP_FORMAT,
                        "format_version": BACKUP_FORMAT_VERSION,
                        "created_at": created_at.isoformat(),
                        "tables": table_blocks,
                    },
                    indent=2,
                    default=_json_default,
                ),
            )
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # The single biggest egress event this app has: one file containing every
    # record, every attachment, and every account's password hash.
    audit.record("backup.download", user=user, object_type="backup", object_label=filename,
                 detail={"row_counts": manifest["row_counts"],
                         "attachment_files": files_included,
                         "size_bytes": os.path.getsize(zip_path)})
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(shutil.rmtree, tmp_dir, True),
    )


# ----------------------------------------------------------------------------
# Restore
# ----------------------------------------------------------------------------

def _read_upload_to_temp(file: UploadFile, tmp_dir: str) -> str:
    """Streams the uploaded zip to disk in chunks, enforcing MAX_RESTORE_BYTES
    as it goes -- never reads the whole thing into memory, which for a
    multi-gigabyte restore would be the difference between working and
    OOM-killing the api container."""
    path = os.path.join(tmp_dir, "restore.zip")
    total = 0
    with open(path, "wb") as out:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESTORE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Backup exceeds MAX_RESTORE_MB limit "
                        f"({MAX_RESTORE_BYTES // (1024 * 1024)} MB) — raise it in .env "
                        "and rebuild if this is a legitimate backup."
                    ),
                )
            out.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    return path


def _backup_prefix(names: list) -> str | None:
    """Where data.json lives inside the zip, as a prefix to put on every other
    member name.

    A backup this app writes has data.json at the top level, and that is the
    first thing checked. But a zip that has been round-tripped through a
    desktop — expanded on download and re-compressed, or "Compress" on the
    unpacked folder — comes back as one wrapper directory with everything
    inside it. The bytes are the same backup; only the names moved. Rather
    than refuse a file the operator correctly believes is good, find the one
    data.json and read the rest relative to it.

    Returns "" for a top-level backup, "dir/" for a wrapped one, or None if
    there is no data.json at all.
    """
    if "data.json" in names:
        return ""
    # Only entries whose own name is data.json count; anything else is a file
    # that merely ends in those characters.
    candidates = sorted({n[: -len("data.json")] for n in names
                         if n.endswith("/data.json") and n.count("/") == 1})
    # More than one means a zip of several backups, which is not a backup.
    return candidates[0] if len(candidates) == 1 else None


def _load_payload(zip_path: str) -> tuple[dict, str]:
    """Returns the parsed data.json and the prefix its siblings live under."""
    if not zipfile.is_zipfile(zip_path):
        raise HTTPException(
            status_code=400,
            detail="That isn't a .zip file — restore expects a backup produced by this app.",
        )
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        prefix = _backup_prefix(names)
        if prefix is None:
            # Say what was actually in there. "No data.json" on its own sends
            # an operator hunting for a fault in a backup that is fine, when
            # the answer is nearly always that a different zip got picked.
            listing = ", ".join(sorted(names)[:6]) or "nothing"
            if len(names) > 6:
                listing += f", … ({len(names)} entries)"
            raise HTTPException(
                status_code=400,
                detail=("No data.json inside that zip, so it isn't a backup from this app. "
                        f"It contains: {listing}."),
            )
        try:
            payload = json.loads(zf.read(prefix + "data.json"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="data.json in that backup isn't valid JSON.")

    if not isinstance(payload, dict) or payload.get("format") != BACKUP_FORMAT:
        raise HTTPException(
            status_code=400,
            detail="That backup wasn't produced by this app (unexpected format marker).",
        )
    version = payload.get("format_version")
    if version != BACKUP_FORMAT_VERSION:
        raise HTTPException(
            status_code=400,
            detail=(
                f"That backup is format version {version}, and this build only "
                f"understands version {BACKUP_FORMAT_VERSION}."
            ),
        )
    if not isinstance(payload.get("tables"), dict):
        raise HTTPException(status_code=400, detail="That backup has no readable tables section.")
    return payload, prefix


# Columns this app has renamed, and what they are called now. Applied to a
# backup on the way in, before it is checked against the schema.
#
# Without this, a rename is indistinguishable from a column that does not
# exist, and _validate_against_schema below rejects the whole archive with
# "upgrade this deployment first" -- advice that cannot work, because the
# deployment is already newer than the backup. Every backup taken before the
# rename, including the sample case files that ship with the project, would
# become unrestorable.
#
# Renames belong here forever, not just for one release. An archive is only
# worth having if it can still be read years later, which is the whole reason
# the backup format is JSON and the column list travels with the data.
RENAMED_COLUMNS: dict[tuple, str] = {
    # `faction` became `alignment` when named groups became entities in their
    # own right; the values are unchanged, including the retired 'Family'.
    ("person_details", "faction"): "alignment",
}


def _apply_column_renames(payload: dict) -> list:
    """Rewrite retired column names in a loaded backup. Returns what it did,
    for the restore's own report -- an admin should be told that an old
    archive was adapted rather than left to wonder."""
    applied = []
    for table, block in payload.get("tables", {}).items():
        if not isinstance(block, dict):
            continue
        columns = block.get("columns") or []
        for old_name in list(columns):
            new_name = RENAMED_COLUMNS.get((table, old_name))
            if not new_name:
                continue
            # A backup that somehow holds both is a backup we cannot reconcile
            # without guessing which one the analyst meant. Leave it alone and
            # let the schema check reject it with a specific message.
            if new_name in columns:
                continue
            block["columns"] = [new_name if c == old_name else c for c in columns]
            columns = block["columns"]
            for row in block.get("rows", []):
                if isinstance(row, dict) and old_name in row:
                    row[new_name] = row.pop(old_name)
            applied.append(f"{table}.{old_name} -> {new_name}")
    return applied


def _validate_against_schema(payload: dict, db_tables: set, column_types: dict) -> None:
    """Everything that can be checked before a single row is touched, gets
    checked before a single row is touched. A restore that fails halfway is
    survivable (the transaction rolls back), but one that's rejected up front
    is better -- the admin gets a specific reason instead of a rolled-back
    attempt to interpret."""
    for table, block in payload["tables"].items():
        if table in EXCLUDED_TABLES:
            continue
        if table not in db_tables:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"That backup contains a table this database doesn't have "
                    f"('{table}'). It's probably from a newer version of the app — "
                    "upgrade this deployment (including any migrations in the README) "
                    "before restoring it."
                ),
            )
        if not isinstance(block, dict) or not isinstance(block.get("rows"), list):
            raise HTTPException(status_code=400, detail=f"Table '{table}' in that backup is malformed.")
        unknown = [c for c in block.get("columns", []) if c not in column_types[table]]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"That backup has column(s) {unknown} on '{table}' that this database "
                    "doesn't have. Restoring would silently drop them — upgrade this "
                    "deployment first."
                ),
            )


def _extract_uploads(zip_path: str, staging_dir: str, prefix: str = "") -> tuple[int, list]:
    """Extracts uploads/* into a staging directory, refusing any member whose
    path escapes it (zip-slip). Returns (extracted_count, rejected_names).

    `prefix` is whatever wrapper directory data.json was found under, so a
    re-compressed backup brings its attachments along too."""
    extracted, rejected = 0, []
    staging_real = os.path.realpath(staging_dir)
    root = prefix + "uploads/"
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.startswith(root):
                continue
            rel = info.filename[len(root):]
            if not rel:
                continue
            target = os.path.realpath(os.path.join(staging_real, rel))
            if target != staging_real and not target.startswith(staging_real + os.sep):
                rejected.append(info.filename)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted += 1
    return extracted, rejected


def _reset_sequences(cur, serial_columns: list) -> None:
    for table, column in serial_columns:
        # setval(seq, MAX(id), true) leaves the next value at MAX(id)+1; on an
        # empty table setval(seq, 1, false) leaves it at 1. The is_called flag
        # is what distinguishes the two, hence "MAX(...) IS NOT NULL".
        cur.execute(
            f'SELECT setval(pg_get_serial_sequence(%s, %s), '
            f'COALESCE(MAX("{column}"), 1), MAX("{column}") IS NOT NULL) FROM "{table}"',
            (table, column),
        )


def _move_staged_files(staging_dir: str) -> int:
    moved = 0
    base = os.path.realpath(UPLOAD_DIR)
    for root, _dirs, files in os.walk(staging_dir):
        for name in files:
            src = os.path.join(root, name)
            rel = os.path.relpath(src, staging_dir)
            dst = _safe_upload_path(rel)
            if dst is None:
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)  # same filesystem (staging lives inside UPLOAD_DIR), so this is a rename
            moved += 1
    return moved


def _prune_unreferenced_uploads(referenced: set) -> int:
    """After a replace-everything restore, files belonging to the case that
    was just replaced are still sitting in the uploads volume with nothing
    pointing at them. On a tool that holds photos and scanned documents about
    real people, leaving those behind after what the admin was told was a
    full replacement is the wrong default -- so they go."""
    removed = 0
    base = os.path.realpath(UPLOAD_DIR)
    if not os.path.isdir(base):
        return 0
    for root, dirs, files in os.walk(base):
        # Never descend into staging/hidden directories -- a concurrent
        # restore's staging area is not an orphan.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, base)
            if rel not in referenced:
                try:
                    os.remove(abs_path)
                    removed += 1
                except OSError:
                    pass
    return removed


@router.post("/restore")
def restore_backup(
    file: UploadFile = File(...),
    # Defaulted rather than required so that a missing or blank confirmation
    # falls through to the explicit message below, instead of FastAPI's
    # generic "field required" validation error — for the one endpoint in
    # this app that destroys data, the reason it refused should always be
    # stated in the app's own words.
    confirm: str = Form(default=""),
    user: dict = Depends(auth.require_admin),
):
    if (confirm or "").strip() != RESTORE_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail=f'Type "{RESTORE_CONFIRM_PHRASE}" to confirm — this replaces everything currently in the app.',
        )

    tmp_dir = tempfile.mkdtemp(prefix="humint-restore-")
    staging_dir = os.path.join(UPLOAD_DIR, f"{_STAGING_PREFIX}{os.getpid()}-{int(datetime.now().timestamp())}")
    try:
        zip_path = _read_upload_to_temp(file, tmp_dir)
        payload, zip_prefix = _load_payload(zip_path)

        with db_cursor() as cur:
            db_tables = set(_all_tables(cur))
            column_types = {t: _table_columns(cur, t) for t in db_tables}
            edges = _fk_edges(cur)
            serials = _serial_columns(cur)

        renamed = _apply_column_renames(payload)
        _validate_against_schema(payload, db_tables, column_types)

        backup_tables = {t: b for t, b in payload["tables"].items() if t not in EXCLUDED_TABLES}
        insert_order = [t for t in _insertion_order(sorted(db_tables), edges) if t in backup_tables]
        # Tables this database has that the backup doesn't mention: they get
        # truncated like everything else and simply end up empty, which is the
        # honest reading of "replace everything with this backup" when
        # restoring one taken before a feature existed. Reported back so the
        # admin isn't surprised by it.
        emptied_not_in_backup = sorted(db_tables - set(backup_tables) - EXCLUDED_TABLES)

        os.makedirs(staging_dir, exist_ok=True)
        files_extracted, files_rejected = _extract_uploads(zip_path, staging_dir, zip_prefix)
        if files_rejected:
            raise HTTPException(
                status_code=400,
                detail=f"That backup contains unsafe file paths and was not restored: {files_rejected[:5]}",
            )

        # --- the destructive part, all of it inside one transaction ---
        restored_counts = {}
        try:
            with db_cursor(commit=True) as cur:
                wipe = ", ".join(f'"{t}"' for t in sorted(db_tables))
                # sessions is in db_tables and gets wiped here too, on purpose:
                # see the module docstring. RESTART IDENTITY resets sequences for
                # tables the backup has no rows for; _reset_sequences fast-forwards
                # the ones it does.
                cur.execute(f"TRUNCATE {wipe} RESTART IDENTITY CASCADE")

                for table in insert_order:
                    block = backup_tables[table]
                    columns = block.get("columns") or list(column_types[table].keys())
                    rows = block["rows"]
                    if not rows:
                        restored_counts[table] = 0
                        continue
                    col_sql = ", ".join(f'"{c}"' for c in columns)
                    values = [
                        tuple(_adapt(column_types[table][c], row.get(c)) for c in columns)
                        for row in rows
                    ]
                    execute_values(
                        cur,
                        f'INSERT INTO "{table}" ({col_sql}) VALUES %s',
                        values,
                        page_size=500,
                    )
                    restored_counts[table] = len(rows)

                _reset_sequences(cur, [(t, c) for t, c in serials if t in db_tables])
        except psycopg2.Error as exc:
            # The transaction has already rolled back by the time this runs
            # (db_cursor closes without committing when the body raises), so
            # the instance still holds exactly what it held before. That's the
            # single most important thing to tell whoever just clicked
            # Restore, so it leads — a bare 500 here would leave them
            # reasonably assuming the worst about a half-wiped database.
            message = (str(exc).strip().splitlines() or ["unknown database error"])[0]
            raise HTTPException(
                status_code=400,
                detail=(
                    "Restore failed and nothing was changed — this instance still has "
                    f"exactly the data it had before. Postgres rejected the backup's contents: {message}"
                ),
            )

        # --- committed; now the filesystem half ---
        referenced = {
            row.get("storage_path")
            for row in backup_tables.get("attachments", {}).get("rows", [])
            if row.get("storage_path")
        }
        files_moved = _move_staged_files(staging_dir)
        files_pruned = _prune_unreferenced_uploads(referenced)

        # Recorded AFTER the transaction commits, so it lands in the restored
        # (i.e. now-current) audit_log rather than being rolled back with a
        # failed restore. A restore replaces the audit trail with the
        # backup's -- see "Audit trail" in README.md -- so this entry is the
        # seam between the two histories.
        audit.record("backup.restore", user=user, object_type="backup",
                     object_label=file.filename,
                     detail={"backup_created_at": payload.get("created_at"),
                             "columns_renamed": renamed,
                             "row_counts": restored_counts,
                             "attachment_files_restored": files_moved,
                             "unreferenced_files_removed": files_pruned})
        return {
            "status": "restored",
            "restored_at": datetime.now(timezone.utc).isoformat(),
            "backup_created_at": payload.get("created_at"),
            "row_counts": restored_counts,
            # Named, not just counted: an admin restoring a year-old archive
            # should be told it was adapted on the way in, not left to find
            # out from a column that mysteriously still has its values.
            "columns_renamed": renamed,
            "tables_emptied_not_in_backup": emptied_not_in_backup,
            "attachment_files_restored": files_moved,
            "attachment_files_in_zip": files_extracted,
            "unreferenced_files_removed": files_pruned,
            "note": "Everyone has been logged out, including you — log back in with an account from the restored backup.",
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(staging_dir, ignore_errors=True)
