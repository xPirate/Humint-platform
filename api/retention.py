"""Retention: archiving records that nobody has touched for a long time.

WHY THIS EXISTS

A case file has an end. A living team file does not, and without something
like this a record entered two years ago and never looked at since sits in
every list, every search and every network with exactly the same weight as
the one entered this morning. The instance does not break; it just gets
quietly less useful every year, which is harder to notice and harder to fix.

WHAT IT DOES

Per entity type, an administrator sets how many days a record may sit with no
activity. A sweep (worker/main.py, once an hour) finds records past that
window and flags them with a visible "due to be archived on ..." date. If the
grace period also passes with no activity, the sweep archives them.

WHAT IT DOES NOT DO

It never deletes. Archiving sets is_active FALSE, which the app already
understands: the record keeps its relationships, its reports and its
documents, stays searchable behind "Show archived", still resolves from an
old export or audit entry, and comes back with one click. Permanent deletion
remains what it was — an admin typing a confirmation into api/destroy.py.

It also never touches event_details.expires_at, which is a different idea
wearing a similar word. That field is the analyst saying "this stops
mattering on the 14th", and the app deliberately does nothing with it but
draw a badge. This module is the opposite direction: the app noticing that
nobody has been near a record, and saying so before acting.

WHAT "TOUCHED" MEANS

Not "edited". A person nobody has opened in two years but who was named in a
report last week is not idle, and archiving them would be obviously wrong to
anyone who looked. LAST_ACTIVITY_SQL below is the whole definition: the
latest of the record's own updated_at, any relationship at either end, any
report that names it, and any document attached to it.

THE THREE EXEMPTIONS

A hold on the record (an analyst pointing at it and saying "not this one"),
optionally anything still linked to an active record, and optionally anything
assessed Hostile. The second is the dangerous one — in a well-linked file it
can spare nearly everything — which is why the Admin page counts what each
exemption would spare before the policy is saved rather than afterwards.

This file is duplicated byte-identical in api/ and worker/. Every function
takes a cursor rather than opening one, so the same code runs inside a
request and inside the poll loop.
"""

import logging

import audit

logger = logging.getLogger(__name__)

ENTITY_TYPES = ("person", "organization", "location", "event", "source",
                "communication", "vehicle", "record")

# The tables that carry an alignment column, for the Hostile exemption.
# Locations have an environment instead and events have neither, so neither
# can ever be exempt on this ground — which is correct: "Hostile" is a
# judgement about a party, and a field is not a party.
ALIGNED_DETAIL_TABLES = ("person_details", "organization_details",
                         "source_details", "vehicle_details")

# The definition of "touched", as one SQL expression against an `entities e`.
#
# GREATEST ignores NULLs in Postgres, so a record with no relationships and no
# reports falls back to its own updated_at, which is never NULL. Each subquery
# hits an index that already existed for the page that displays the same
# thing, so this costs about what opening the record costs.
LAST_ACTIVITY_SQL = """GREATEST(
    e.updated_at,
    (SELECT max(r.created_at) FROM relationships r
      WHERE r.from_entity_id = e.id OR r.to_entity_id = e.id),
    (SELECT max(rp.updated_at) FROM report_entities re
       JOIN reports rp ON rp.id = re.report_id
      WHERE re.entity_id = e.id),
    (SELECT max(a.uploaded_at) FROM attachments a WHERE a.entity_id = e.id)
)"""

# Exemption 2: still attached to something that is itself active. Written as
# EXISTS rather than a join so it stops at the first hit.
LINKED_TO_ACTIVE_SQL = """EXISTS (
    SELECT 1 FROM relationships r
      JOIN entities o ON o.id = CASE WHEN r.from_entity_id = e.id
                                     THEN r.to_entity_id ELSE r.from_entity_id END
     WHERE (r.from_entity_id = e.id OR r.to_entity_id = e.id)
       AND o.is_active
)"""

HOSTILE_SQL = "(" + " OR ".join(
    f"EXISTS (SELECT 1 FROM {t} d WHERE d.entity_id = e.id AND d.alignment = 'Hostile')"
    for t in ALIGNED_DETAIL_TABLES) + ")"


DEFAULT_SETTINGS = {
    "retention_enabled": False,
    "retention_grace_days": 14,
    "retention_exempt_linked": True,
    "retention_exempt_hostile": True,
    "retention_last_run": None,
}

SETTINGS_COLUMNS = tuple(DEFAULT_SETTINGS)


def load_settings(cur) -> dict:
    """The cross-type settings. Returns defaults when the singleton row has
    never been written, so a fresh install reads the same as a configured one
    that has left everything alone."""
    cur.execute(f"SELECT {', '.join(SETTINGS_COLUMNS)} FROM app_settings WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        return dict(DEFAULT_SETTINGS)
    out = dict(zip(SETTINGS_COLUMNS, row))
    # NOT NULL columns with defaults, but a restored backup from before this
    # feature can leave them NULL on the way through.
    for key, fallback in DEFAULT_SETTINGS.items():
        if out.get(key) is None and fallback is not None:
            out[key] = fallback
    return out


def load_policy(cur) -> dict:
    """{entity_type: retain_days or None} for every type, whether or not the
    table has a row for it. The caller should never have to think about which
    types have been configured."""
    cur.execute("SELECT entity_type, retain_days FROM retention_policy")
    stored = dict(cur.fetchall())
    return {t: stored.get(t) for t in ENTITY_TYPES}


def save_policy(cur, policy: dict) -> None:
    """Upserts the days per type. A type mapped to None is stored as a NULL
    row rather than deleted, so the page reads back what was set."""
    for entity_type, days in policy.items():
        if entity_type not in ENTITY_TYPES:
            continue
        cur.execute(
            "INSERT INTO retention_policy (entity_type, retain_days) VALUES (%s, %s) "
            "ON CONFLICT (entity_type) DO UPDATE SET retain_days = EXCLUDED.retain_days",
            (entity_type, days))


def _exemption_clause(settings: dict) -> str:
    """SQL that is TRUE for a record the policy must not touch."""
    parts = ["e.retention_hold"]
    if settings.get("retention_exempt_linked"):
        parts.append(LINKED_TO_ACTIVE_SQL)
    if settings.get("retention_exempt_hostile"):
        parts.append(HOSTILE_SQL)
    return "(" + " OR ".join(parts) + ")"


def _typed_windows(policy: dict) -> list:
    """The (type, days) pairs that actually have a window set. A policy with
    nothing configured produces an empty list, and every caller treats that as
    "there is nothing to do" rather than "archive everything"."""
    return [(t, d) for t, d in policy.items() if d and d > 0]


def survey(cur, policy: dict, settings: dict) -> dict:
    """What this policy would mean, right now, without changing anything.

    This is what the Admin page shows before you save. It answers the two
    questions an administrator actually has — how many records does this
    catch, and how many is each exemption sparing — and it answers them
    against the real file rather than in the abstract, because the effect of
    "exempt anything still linked" is unguessable from outside.
    """
    windows = _typed_windows(policy)
    per_type = {}
    cur.execute("SELECT entity_type, count(*) FROM entities WHERE is_active GROUP BY entity_type")
    active_counts = dict(cur.fetchall())

    exempt_sql = _exemption_clause(settings)
    total_due = total_spared = 0
    for entity_type in ENTITY_TYPES:
        days = policy.get(entity_type)
        row = {"active": active_counts.get(entity_type, 0), "retain_days": days,
               "idle": 0, "due": 0, "spared": 0}
        if days and days > 0:
            cur.execute(
                f"SELECT count(*) FILTER (WHERE NOT {exempt_sql}), "
                f"       count(*) FILTER (WHERE {exempt_sql}) "
                f"  FROM entities e "
                f" WHERE e.is_active AND e.entity_type = %s "
                f"   AND {LAST_ACTIVITY_SQL} < now() - make_interval(days => %s)",
                (entity_type, days))
            due, spared = cur.fetchone()
            row["due"], row["spared"] = due or 0, spared or 0
            row["idle"] = row["due"] + row["spared"]
            total_due += row["due"]
            total_spared += row["spared"]
        per_type[entity_type] = row

    cur.execute("SELECT count(*) FROM entities WHERE retention_hold")
    held = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM entities WHERE is_active AND retention_due_at IS NOT NULL")
    flagged = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM entities WHERE NOT is_active AND archived_reason = 'retention'")
    archived_by_policy = cur.fetchone()[0]

    return {
        "per_type": per_type,
        "totals": {"due": total_due, "spared_by_exemption": total_spared,
                   "on_hold": held, "flagged_now": flagged,
                   "archived_by_policy": archived_by_policy},
        "configured_types": len(windows),
    }


def due_list(cur, limit: int = 200) -> list:
    """Records currently carrying a flag, soonest first. This is the list the
    grace period exists to make readable."""
    cur.execute(
        "SELECT e.id, e.entity_type, e.name, e.retention_due_at, "
        f"       {LAST_ACTIVITY_SQL} AS last_activity "
        "  FROM entities e "
        " WHERE e.is_active AND e.retention_due_at IS NOT NULL "
        " ORDER BY e.retention_due_at ASC, e.name ASC LIMIT %s", (limit,))
    return [{"id": r[0], "entity_type": r[1], "name": r[2],
             "due_at": r[3].isoformat() if r[3] else None,
             "last_activity": r[4].isoformat() if r[4] else None}
            for r in cur.fetchall()]


def run_sweep(cur, *, user=None, dry_run: bool = False) -> dict:
    """One pass: unflag what came back to life, flag what has gone idle,
    archive what stayed idle through its grace period.

    Order matters. Clearing first means a record that was touched during its
    grace period is out of the running before anything looks at whether its
    date has passed — so touching a record always saves it, even if the sweep
    runs a second later.
    """
    settings = load_settings(cur)
    policy = load_policy(cur)
    result = {"enabled": bool(settings["retention_enabled"]),
              "cleared": 0, "flagged": 0, "archived": 0, "dry_run": dry_run}
    if not settings["retention_enabled"]:
        return result

    windows = _typed_windows(policy)
    exempt_sql = _exemption_clause(settings)

    # 1. Anything flagged that is no longer idle, or has become exempt, or
    #    whose type no longer has a window at all, loses its flag.
    idle_per_type = " OR ".join(
        [f"(e.entity_type = '{t}' AND {LAST_ACTIVITY_SQL} < now() - make_interval(days => {d}))"
         for t, d in windows]) or "FALSE"
    revived_where = (f"e.retention_due_at IS NOT NULL "
                     f"  AND (NOT ({idle_per_type}) OR {exempt_sql})")
    if dry_run:
        cur.execute(f"SELECT count(*) FROM entities e WHERE {revived_where}")
        result["cleared"] = cur.fetchone()[0]
    else:
        cur.execute(f"UPDATE entities e SET retention_due_at = NULL WHERE {revived_where}")
        result["cleared"] = cur.rowcount

    # 2. Idle, not exempt, not already flagged -> flag with a date.
    grace = int(settings["retention_grace_days"] or 0)
    select_new = (
        "SELECT e.id, e.entity_type, e.name FROM entities e "
        f" WHERE e.is_active AND e.retention_due_at IS NULL "
        f"   AND ({idle_per_type}) AND NOT {exempt_sql}")
    cur.execute(select_new)
    newly = cur.fetchall()
    result["flagged"] = len(newly)
    if newly and not dry_run:
        cur.execute(
            "UPDATE entities SET retention_due_at = now() + make_interval(days => %s) "
            " WHERE id = ANY(%s)", (grace, [r[0] for r in newly]))
        for eid, etype, name in newly:
            audit.record("retention.flagged", user=user, actor_kind=None if user else "system",
                         object_type="entity", object_id=eid, object_label=name,
                         detail={"entity_type": etype, "grace_days": grace})

    # 3. Flagged, still idle, and the date has passed -> archive.
    cur.execute(
        "SELECT e.id, e.entity_type, e.name FROM entities e "
        " WHERE e.is_active AND e.retention_due_at IS NOT NULL "
        "   AND e.retention_due_at <= now()")
    expired = cur.fetchall()
    result["archived"] = len(expired)
    if expired and not dry_run:
        # updated_at is deliberately left alone. Bumping it would reset the very
        # clock that produced this decision, so an un-archived record would
        # read as freshly touched and get another full window for free.
        cur.execute(
            "UPDATE entities SET is_active = FALSE, archived_reason = 'retention', "
            "       retention_due_at = NULL "
            " WHERE id = ANY(%s)", ([r[0] for r in expired],))
        for eid, etype, name in expired:
            audit.record("retention.archived", user=user, actor_kind=None if user else "system",
                         object_type="entity", object_id=eid, object_label=name,
                         detail={"entity_type": etype})

    if not dry_run:
        cur.execute("INSERT INTO app_settings (id, retention_last_run) VALUES (1, now()) "
                    "ON CONFLICT (id) DO UPDATE SET retention_last_run = now()")
    return result
