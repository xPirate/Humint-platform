"""Reading the Ollama call log back: is it working, what is it spent on, is
this box keeping up.

Admin-only, because it names users and because "which model is slow on this
hardware" is a question for whoever chooses the model.

A NOTE ON WHAT THESE NUMBERS ARE NOT

They are not costs. A self-hosted model is free per token; what it spends is
time on one machine, and everything else is queued behind it. So the headline
figure here is not a token count — it is the share of calls that failed, and
after that the throughput. Tokens appear mostly as the denominator that turns
a duration into a rate you can compare between two models.

Medians rather than means throughout. One cold start with a 40-second model
load drags a mean into uselessness, and the question being asked is "what does
this normally do", which is what a median answers.
"""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

import auth
from db import db_cursor

router = APIRouter(prefix="/api/admin", tags=["ollama-stats"])

WINDOW_CHOICES = (1, 7, 14, 30)
DEFAULT_WINDOW = 7

RETENTION_DAYS = int(os.environ.get("OLLAMA_USAGE_RETENTION_DAYS", "30"))

# How many recent failures to name. Enough to see a pattern, few enough that
# the panel stays a panel.
RECENT_FAILURE_LIMIT = 10

OPERATION_LABELS = {
    "extract": "Document extraction",
    "embed": "Correlation embeddings",
    "chat": "AI assistant",
}


def _rows(cur) -> list[dict]:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _summary(cur, days: int) -> dict:
    cur.execute(
        """
        SELECT
          count(*)                                                   AS calls,
          count(*) FILTER (WHERE outcome = 'success')                AS successes,
          count(*) FILTER (WHERE outcome = 'failure')                AS failures,
          count(*) FILTER (WHERE outcome = 'timeout')                AS timeouts,
          sum(prompt_tokens)                                         AS prompt_tokens,
          sum(eval_tokens)                                           AS eval_tokens,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY total_duration_ms)
            FILTER (WHERE total_duration_ms IS NOT NULL)             AS median_duration_ms,
          percentile_cont(0.95) WITHIN GROUP (ORDER BY total_duration_ms)
            FILTER (WHERE total_duration_ms IS NOT NULL)             AS p95_duration_ms,
          -- Tokens per second, computed per call and then taken at the median.
          -- Summing tokens and dividing by summed time would let one long call
          -- dominate the figure, which is the opposite of what "typical
          -- throughput" means.
          percentile_cont(0.5) WITHIN GROUP (
            ORDER BY eval_tokens::float / NULLIF(eval_duration_ms, 0) * 1000)
            FILTER (WHERE eval_tokens IS NOT NULL AND eval_duration_ms > 0)
                                                                     AS median_tokens_per_sec,
          -- Model load time, separated out. A slow call because the model was
          -- cold and a slow call because the model is too big for this box are
          -- different problems with different fixes, and on a Pi the first one
          -- is very common.
          count(*) FILTER (WHERE load_duration_ms > 1000)             AS cold_starts,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY load_duration_ms)
            FILTER (WHERE load_duration_ms > 1000)                   AS median_load_ms,
          min(occurred_at)                                           AS first_seen,
          max(occurred_at)                                           AS last_seen
        FROM ollama_calls
        WHERE occurred_at >= now() - make_interval(days => %s)
        """,
        (days,),
    )
    row = _rows(cur)[0]
    calls = row["calls"] or 0
    bad = (row["failures"] or 0) + (row["timeouts"] or 0)
    row["failure_rate"] = round(bad / calls, 4) if calls else None
    for key in ("median_duration_ms", "p95_duration_ms", "median_load_ms"):
        if row[key] is not None:
            row[key] = int(row[key])
    if row["median_tokens_per_sec"] is not None:
        row["median_tokens_per_sec"] = round(row["median_tokens_per_sec"], 1)
    return row


def _by(cur, column: str, days: int) -> list[dict]:
    """Breakdown by operation or by model — same shape either way."""
    if column not in ("operation", "model"):
        raise ValueError(column)
    cur.execute(
        f"""
        SELECT COALESCE({column}, '(not reported)') AS key,
               count(*)                                    AS calls,
               count(*) FILTER (WHERE outcome <> 'success') AS problems,
               -- Deliberately not COALESCE'd to 0. An embeddings call reports
               -- no token counts at all, and a group of them summing to "0
               -- tokens" reads as "this was free" rather than "this was never
               -- measured". NULL here renders as an em dash in the panel.
               sum(prompt_tokens)                          AS prompt_tokens,
               sum(eval_tokens)                            AS eval_tokens,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY total_duration_ms)
                 FILTER (WHERE total_duration_ms IS NOT NULL) AS median_duration_ms,
               percentile_cont(0.5) WITHIN GROUP (
                 ORDER BY eval_tokens::float / NULLIF(eval_duration_ms, 0) * 1000)
                 FILTER (WHERE eval_tokens IS NOT NULL AND eval_duration_ms > 0)
                                                           AS median_tokens_per_sec
        FROM ollama_calls
        WHERE occurred_at >= now() - make_interval(days => %s)
        GROUP BY 1
        ORDER BY calls DESC, 1
        """,
        (days,),
    )
    out = _rows(cur)
    for row in out:
        if row["median_duration_ms"] is not None:
            row["median_duration_ms"] = int(row["median_duration_ms"])
        if row["median_tokens_per_sec"] is not None:
            row["median_tokens_per_sec"] = round(row["median_tokens_per_sec"], 1)
        if column == "operation":
            row["label"] = OPERATION_LABELS.get(row["key"], row["key"])
    return out


def _by_user(cur, days: int) -> list[dict]:
    """Only the assistant produces attributed calls, so this is a picture of
    chat usage and says so. It exists because contention on one GPU is real:
    an extraction queue that is not moving can be somebody else's chat."""
    cur.execute(
        """
        SELECT COALESCE(u.username, '(background work)') AS username,
               count(*)                        AS calls,
               sum(c.prompt_tokens)              AS prompt_tokens,
               sum(c.eval_tokens)                AS eval_tokens,
               COALESCE(sum(c.total_duration_ms), 0) AS total_duration_ms
        FROM ollama_calls c
        LEFT JOIN users u ON u.id = c.user_id
        WHERE c.occurred_at >= now() - make_interval(days => %s)
        GROUP BY 1
        ORDER BY total_duration_ms DESC, calls DESC
        LIMIT 20
        """,
        (days,),
    )
    return _rows(cur)


def _daily(cur, days: int) -> list[dict]:
    """One row per day including the empty ones, so a gap reads as a gap
    rather than closing up — the same reason the executive summary's charts
    use generate_series."""
    cur.execute(
        """
        WITH days AS (
            SELECT generate_series(
                (now() - make_interval(days => %(d)s))::date,
                now()::date,
                interval '1 day'
            )::date AS day
        )
        SELECT d.day,
               count(c.id)                                        AS calls,
               count(c.id) FILTER (WHERE c.outcome <> 'success')  AS problems,
               COALESCE(sum(c.prompt_tokens), 0)                  AS prompt_tokens,
               COALESCE(sum(c.eval_tokens), 0)                    AS eval_tokens
        FROM days d
        LEFT JOIN ollama_calls c ON c.occurred_at::date = d.day
        GROUP BY d.day
        ORDER BY d.day
        """,
        {"d": days},
    )
    out = _rows(cur)
    for row in out:
        row["day"] = row["day"].isoformat()
    return out


def _recent_failures(cur, days: int) -> list[dict]:
    cur.execute(
        """
        SELECT occurred_at, source, operation, model, outcome, error
        FROM ollama_calls
        WHERE outcome <> 'success'
          AND occurred_at >= now() - make_interval(days => %s)
        ORDER BY occurred_at DESC
        LIMIT %s
        """,
        (days, RECENT_FAILURE_LIMIT),
    )
    out = _rows(cur)
    for row in out:
        row["occurred_at"] = row["occurred_at"].isoformat()
    return out


@router.get("/ollama-usage")
def ollama_usage_stats(
    days: int = Query(default=DEFAULT_WINDOW,
                      description=f"Window in days; one of {WINDOW_CHOICES}"),
    user: dict = Depends(auth.require_admin),
):
    if days not in WINDOW_CHOICES:
        raise HTTPException(status_code=400, detail=f"days must be one of {WINDOW_CHOICES}")
    with db_cursor() as cur:
        return {
            "window_days": days,
            "retention_days": RETENTION_DAYS,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": _summary(cur, days),
            "by_operation": _by(cur, "operation", days),
            "by_model": _by(cur, "model", days),
            "by_user": _by_user(cur, days),
            "daily": _daily(cur, days),
            "recent_failures": _recent_failures(cur, days),
            "operation_labels": OPERATION_LABELS,
        }
