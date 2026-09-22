"""Retention pruning for the audit log.

AUDIT_RETENTION_DAYS defaults to 0, meaning keep everything forever. That is
the deliberate default for an audit trail: the failure mode of keeping too
much is a large table, while the failure mode of deleting too much is being
unable to answer a question about something that already happened. Set a
number only if you have a policy that requires one.

This is the only thing in the app that ever deletes from audit_log, and it
runs at most once an hour rather than on every poll cycle — the table is
append-only in normal operation, so there is nothing to gain from checking
more often than that.
"""

import os
import time

import ollama_usage
from db import db_cursor

RETENTION_DAYS = int(os.environ.get("AUDIT_RETENTION_DAYS", "0"))
PRUNE_INTERVAL_SECONDS = 3600

# The Ollama call log has a real default retention, unlike the audit trail.
# The two are not the same kind of record: the audit log answers questions
# about what people did and has a default of "keep forever" for that reason,
# while this is diagnostics that accrue a row on every extraction and buy
# nothing after a month that they did not buy in the first week.
OLLAMA_RETENTION_DAYS = int(os.environ.get("OLLAMA_USAGE_RETENTION_DAYS", "30"))

_last_prune_at = 0.0
_last_ollama_prune_at = 0.0


def prune_audit_log() -> bool:
    """Returns True if it actually did work, matching the convention the other
    poll-loop jobs use so main() knows whether to sleep."""
    global _last_prune_at
    if RETENTION_DAYS <= 0:
        return False
    now = time.monotonic()
    if _last_prune_at and (now - _last_prune_at) < PRUNE_INTERVAL_SECONDS:
        return False
    _last_prune_at = now

    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM audit_log WHERE occurred_at < now() - make_interval(days => %s)",
                (RETENTION_DAYS,),
            )
            deleted = cur.rowcount
    except Exception as exc:
        # Same principle as the audit writer itself: a problem with the audit
        # machinery must never take down the worker that does the real work.
        print(f"[worker] audit prune failed: {exc}", flush=True)
        return False

    if deleted:
        print(f"[worker] pruned {deleted} audit entr(ies) older than {RETENTION_DAYS} days", flush=True)
    return bool(deleted)


def prune_ollama_calls() -> bool:
    """Same shape and the same schedule as prune_audit_log above, kept as its
    own function because the two have different retention windows and
    different reasons for them."""
    global _last_ollama_prune_at
    if OLLAMA_RETENTION_DAYS <= 0:
        return False
    now = time.monotonic()
    if _last_ollama_prune_at and (now - _last_ollama_prune_at) < PRUNE_INTERVAL_SECONDS:
        return False
    _last_ollama_prune_at = now

    deleted = ollama_usage.prune(OLLAMA_RETENTION_DAYS)
    if deleted:
        print(f"[worker] pruned {deleted} Ollama call record(s) older than "
              f"{OLLAMA_RETENTION_DAYS} days", flush=True)
    return bool(deleted)
