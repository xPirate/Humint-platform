"""Audit trail: who did what, when — and optionally forwarded to syslog.

Records every change made through the app, plus the reads that move data OUT
of it (attachment downloads and previews, PDF exports, backup downloads, AI
Assistant queries). Ordinary browsing is not recorded; see "Audit trail" in
README.md for why, and for what that leaves uncovered.

TWO PROPERTIES THIS MODULE EXISTS TO GUARANTEE

1. Auditing never breaks the thing being audited. Every failure path here is
   swallowed and logged to stderr instead of raised: a full disk, a syslog
   host that has gone away, a malformed detail payload. An audit trail that
   can take the application down with it is worse than one that occasionally
   misses a row, because the first failure mode is the one that gets auditing
   switched off entirely.

2. Auditing never blocks a request on the network. The database write is
   synchronous (it's a local INSERT, and losing it would lose the record),
   but syslog forwarding goes through a queue drained by a background thread.
   A TCP syslog host that accepts a connection and then stalls must not turn
   into a hung request.

WHY SYSLOG MATTERS MORE THAN THE TABLE

The table lives in the same database as the case data, on the same machine,
owned by the same Postgres role the app connects as. An admin with shell
access can rewrite it, and a backup restore replaces it wholesale. That is
inherent to self-hosting and can't be engineered away from inside the app.
Forwarding to a syslog collector on a different machine is what turns this
from "a log the local admin keeps" into "a record the local admin can't
quietly alter" — which is the version an outside party would actually put
weight on.
"""

import json
import logging
import logging.handlers
import os
import queue
import socket
import sys
import threading
from contextvars import ContextVar

from db import db_cursor

# ----------------------------------------------------------------------------
# Request context
#
# Set once per request by the middleware in api/main.py, so record() can pick
# up the caller's IP and user agent without every endpoint in the app having
# to take a Request parameter purely to pass it through.
# ----------------------------------------------------------------------------

_request_context: ContextVar = ContextVar("audit_request_context", default=None)

# Behind a reverse proxy the socket peer is the proxy, not the user, and the
# real address is in X-Forwarded-For — but that header is trivially forged by
# anyone talking to the app directly. Trusting it is therefore opt-in: turn it
# on only when something in front of the app is actually setting it.
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "false").lower() == "true"


def set_request_context(ip: str = None, user_agent: str = None) -> None:
    _request_context.set({"ip": ip, "user_agent": (user_agent or "")[:500]})


def clear_request_context() -> None:
    _request_context.set(None)


def context_from_request(request) -> dict:
    """Extracts {ip, user_agent} from a Starlette/FastAPI Request."""
    ip = request.client.host if request.client else None
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Left-most entry is the original client; the rest are proxies.
            ip = forwarded.split(",")[0].strip() or ip
    return {"ip": ip, "user_agent": request.headers.get("user-agent", "")[:500]}


# ----------------------------------------------------------------------------
# Syslog forwarding
# ----------------------------------------------------------------------------

SYSLOG_ENABLED = os.environ.get("AUDIT_SYSLOG_ENABLED", "false").lower() == "true"
SYSLOG_HOST = os.environ.get("AUDIT_SYSLOG_HOST", "")
SYSLOG_PORT = int(os.environ.get("AUDIT_SYSLOG_PORT", "514"))
SYSLOG_PROTOCOL = os.environ.get("AUDIT_SYSLOG_PROTOCOL", "udp").lower()
SYSLOG_FACILITY = os.environ.get("AUDIT_SYSLOG_FACILITY", "local0").lower()
SYSLOG_APP_NAME = os.environ.get("AUDIT_SYSLOG_APP_NAME", "humint-platform")

_FACILITIES = logging.handlers.SysLogHandler.facility_names

_syslog_logger = None
_syslog_queue = None
_syslog_listener = None
_syslog_dropped = 0
_syslog_lock = threading.Lock()


def _build_syslog_logger():
    """Returns a configured logger, or None if forwarding is off or
    unconfigurable. Never raises — a bad syslog setting must degrade to "the
    database still has everything", not to a container that won't start."""
    if not SYSLOG_ENABLED:
        return None, None, None
    if not SYSLOG_HOST:
        print("[audit] AUDIT_SYSLOG_ENABLED is true but AUDIT_SYSLOG_HOST is empty — "
              "forwarding disabled, the audit_log table is unaffected", file=sys.stderr)
        return None, None, None

    try:
        facility = _FACILITIES.get(SYSLOG_FACILITY, logging.handlers.SysLogHandler.LOG_LOCAL0)
        sock_type = socket.SOCK_STREAM if SYSLOG_PROTOCOL == "tcp" else socket.SOCK_DGRAM
        handler = logging.handlers.SysLogHandler(
            address=(SYSLOG_HOST, SYSLOG_PORT),
            facility=facility,
            socktype=sock_type,
        )
        handler.ident = f"{SYSLOG_APP_NAME}: "
        # Over TCP, syslog messages need a delimiter or the collector sees one
        # unbroken stream and concatenates events into each other. Python's
        # SysLogHandler doesn't add one (it's fine over UDP, where the datagram
        # boundary IS the delimiter), so the newline goes in via the formatter
        # — newline framing being what rsyslog/syslog-ng expect by default.
        terminator = "\n" if sock_type == socket.SOCK_STREAM else ""
        handler.setFormatter(logging.Formatter(f"%(message)s{terminator}"))

        logger = logging.getLogger("humint.audit.syslog")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        # QueueHandler/QueueListener is the stdlib's own answer to "don't do
        # network I/O on the calling thread". Bounded so that a syslog host
        # that stops accepting can never grow this queue without limit.
        q = queue.Queue(maxsize=10000)
        logger.handlers = [logging.handlers.QueueHandler(q)]
        listener = logging.handlers.QueueListener(q, handler, respect_handler_level=False)
        listener.daemon = True
        listener.start()
        return logger, q, listener
    except Exception as exc:
        print(f"[audit] could not set up syslog forwarding to {SYSLOG_HOST}:{SYSLOG_PORT} "
              f"({exc}) — forwarding disabled, the audit_log table is unaffected", file=sys.stderr)
        return None, None, None


_syslog_logger, _syslog_queue, _syslog_listener = _build_syslog_logger()


def _forward(entry: dict) -> None:
    if _syslog_logger is None:
        return
    global _syslog_dropped
    try:
        # A full queue means the collector is not keeping up. Dropping is the
        # right call over blocking the caller — the database copy is still
        # intact, and the drop count is reported so the gap is visible rather
        # than silent.
        if _syslog_queue is not None and _syslog_queue.full():
            with _syslog_lock:
                _syslog_dropped += 1
                if _syslog_dropped % 100 == 1:
                    print(f"[audit] syslog queue full — {_syslog_dropped} event(s) not forwarded "
                          "(all are still in the audit_log table)", file=sys.stderr)
            return
        _syslog_logger.info(json.dumps(entry, default=str, separators=(",", ":")))
    except Exception as exc:
        print(f"[audit] syslog forward failed: {exc}", file=sys.stderr)


def syslog_status() -> dict:
    """Surfaced on the Admin page so 'is forwarding actually on?' is
    answerable without reading container logs."""
    with _syslog_lock:
        dropped = _syslog_dropped
    return {
        "enabled": SYSLOG_ENABLED,
        "active": _syslog_logger is not None,
        "host": SYSLOG_HOST or None,
        "port": SYSLOG_PORT if SYSLOG_HOST else None,
        "protocol": SYSLOG_PROTOCOL,
        "facility": SYSLOG_FACILITY,
        "dropped": dropped,
    }


# ----------------------------------------------------------------------------
# Recording
# ----------------------------------------------------------------------------

def record(
    action: str,
    *,
    user: dict = None,
    actor_kind: str = None,
    object_type: str = None,
    object_id=None,
    object_label: str = None,
    outcome: str = "success",
    detail: dict = None,
    actor_username: str = None,
) -> None:
    """Write one audit entry. Never raises.

    `user` is the dict returned by auth.require_user. Pass actor_username on
    its own for the cases where there is no authenticated user but the
    attempted identity still matters — a failed login being the obvious one.
    """
    if user:
        actor_id = user.get("id")
        username = user.get("username")
        kind = actor_kind or "user"
    else:
        actor_id = None
        username = actor_username
        kind = actor_kind or ("anonymous" if actor_username is not None else "system")

    ctx = _request_context.get() or {}
    entry = {
        "action": action,
        "actor_id": actor_id,
        "actor_username": username,
        "actor_kind": kind,
        "object_type": object_type,
        "object_id": None if object_id is None else str(object_id),
        "object_label": object_label,
        "outcome": outcome,
        "ip_address": ctx.get("ip"),
        "user_agent": ctx.get("user_agent"),
        "detail": detail or None,
    }

    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO audit_log
                    (actor_id, actor_username, actor_kind, action, object_type, object_id,
                     object_label, outcome, ip_address, user_agent, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING occurred_at
                """,
                (
                    entry["actor_id"], entry["actor_username"], entry["actor_kind"],
                    entry["action"], entry["object_type"], entry["object_id"],
                    entry["object_label"], entry["outcome"], entry["ip_address"],
                    entry["user_agent"],
                    json.dumps(entry["detail"], default=str) if entry["detail"] else None,
                ),
            )
            row = cur.fetchone()
            entry["occurred_at"] = row[0].isoformat() if row else None
    except Exception as exc:
        # Most likely cause by far: running against a database that hasn't had
        # the audit_log migration applied yet. Say so once per failure rather
        # than failing the user's actual request.
        print(f"[audit] could not write audit entry for {action}: {exc}", file=sys.stderr)

    _forward(entry)


def changed_fields(before: dict, after: dict) -> list:
    """Field NAMES that differ — deliberately not their values. An audit trail
    that recorded old and new values would quietly become a second copy of the
    case data, with none of the access control the real records have."""
    names = []
    for key, new_value in (after or {}).items():
        if (before or {}).get(key) != new_value:
            names.append(key)
    return sorted(names)
