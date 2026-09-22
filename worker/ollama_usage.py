"""Recording what Ollama actually did, one row per call.

Duplicated byte-for-byte between api/ and worker/ for the same reason geo.py,
audit.py and ollama_client.py are: both services make Ollama calls, both have
their own `db` module, and a shared package would mean a build step this
project deliberately does not have. If you change this file, change the other
copy — there is a test that fails if the two drift.

NEVER RAISES

Recording a call must not be able to break the call it is recording. An
extraction that succeeded and then failed to write its own telemetry row has
still succeeded, and the right behaviour is to lose the row and carry on. Every
public function here swallows its own exceptions, in the same way audit.record
does — and for a weaker reason, because unlike the audit trail this data is
diagnostics rather than a record anybody is entitled to.

WHAT IS NOT RECORDED

No prompt text, no reply text, no document content. The point of this table is
timings and outcomes, and a copy of every question anyone asked the assistant
would be a second, unaudited copy of the case file sitting in a diagnostics
table. The assistant's own conversation rows already hold the text, scoped to
the user who wrote it.
"""

import logging

from db import db_cursor

logger = logging.getLogger(__name__)

# Ollama reports durations in nanoseconds.
_NS_PER_MS = 1_000_000

# Long enough to tell a connection refused from a model-not-found, short
# enough that this stays a diagnostics table rather than a log file.
MAX_ERROR_LENGTH = 300


def _ms(nanoseconds):
    """Nanoseconds to whole milliseconds, or None if absent.

    None rather than 0 for a missing value: "not reported" and "took no time"
    are different facts, and averaging the second into a rate quietly makes a
    model look faster than it is.
    """
    if not isinstance(nanoseconds, (int, float)):
        return None
    return int(nanoseconds / _NS_PER_MS)


def _int_or_none(value):
    return value if isinstance(value, int) else None


def from_response(payload: dict) -> dict:
    """Pull the usage fields out of an Ollama response body.

    Ollama returns these on /api/chat and /api/generate. The older
    /api/embeddings returns none of them, which is why every field here is
    allowed to come back None rather than defaulted.
    """
    if not isinstance(payload, dict):
        return {}
    return {
        "model": payload.get("model"),
        "prompt_tokens": _int_or_none(payload.get("prompt_eval_count")),
        "eval_tokens": _int_or_none(payload.get("eval_count")),
        "total_duration_ms": _ms(payload.get("total_duration")),
        "load_duration_ms": _ms(payload.get("load_duration")),
        "eval_duration_ms": _ms(payload.get("eval_duration")),
    }


def record(source: str, operation: str, *, model=None, user_id=None,
           outcome: str = "success", error=None, usage: dict = None) -> None:
    """Write one row. Swallows everything."""
    usage = usage or {}
    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO ollama_calls
                    (source, operation, model, user_id, prompt_tokens, eval_tokens,
                     total_duration_ms, load_duration_ms, eval_duration_ms, outcome, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    source, operation,
                    usage.get("model") or model,
                    user_id,
                    usage.get("prompt_tokens"),
                    usage.get("eval_tokens"),
                    usage.get("total_duration_ms"),
                    usage.get("load_duration_ms"),
                    usage.get("eval_duration_ms"),
                    outcome,
                    (str(error)[:MAX_ERROR_LENGTH] if error else None),
                ),
            )
    except Exception as exc:  # noqa: BLE001 — see the module docstring
        logger.debug("Could not record Ollama usage: %s", exc)


def prune(retention_days: int) -> int:
    """Delete rows older than the window. Returns how many went.

    Unlike the audit log, this has a real default retention and no "keep
    forever" mode in practice: these are diagnostics, they accumulate on every
    extraction, and a year of them on a memory card buys nothing that a month
    does not.
    """
    if retention_days <= 0:
        return 0
    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM ollama_calls WHERE occurred_at < now() - make_interval(days => %s)",
                (retention_days,),
            )
            return cur.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not prune Ollama usage: %s", exc)
        return 0
