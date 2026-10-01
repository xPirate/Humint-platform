"""The retention sweep, on the worker's poll loop.

retention.py holds the decisions; this holds the schedule. Once an hour is
plenty: the thing being measured is a record going untouched for months, and
checking every twenty seconds would only mean the same answer 180 times.

Like audit_prune, it returns True when it did something, so main() knows
whether it has earned a sleep.
"""

import os
import time

import retention
from db import db_cursor

SWEEP_INTERVAL_SECONDS = int(os.environ.get("RETENTION_SWEEP_INTERVAL_SECONDS", "3600"))

_last_sweep_at = 0.0


def run_retention_sweep() -> bool:
    global _last_sweep_at
    now = time.monotonic()
    # Deliberately checked before the enabled flag, so switching the policy on
    # does not trigger a sweep within seconds of somebody pressing Save. The
    # Admin page has a Run now button for when that is what you want, and it
    # is better that the first automatic run is one an administrator chose to
    # wait for than one that surprised them.
    if _last_sweep_at and (now - _last_sweep_at) < SWEEP_INTERVAL_SECONDS:
        return False
    _last_sweep_at = now

    with db_cursor(commit=True) as cur:
        result = retention.run_sweep(cur)
    if not result["enabled"]:
        return False
    if result["flagged"] or result["archived"] or result["cleared"]:
        print(f"[worker] retention: flagged {result['flagged']}, "
              f"archived {result['archived']}, cleared {result['cleared']}", flush=True)
    return True
