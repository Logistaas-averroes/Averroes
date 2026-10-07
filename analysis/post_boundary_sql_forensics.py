"""
analysis/post_boundary_sql_forensics.py

PR-ADS-161C — why does a post-boundary contact that reached SQL carry no exact
SQL-entry timestamp? Pure: no database, no HubSpot, no clock.

The question this answers
-------------------------
PR-ADS-160 made a post-boundary undated SQL contact an INCIDENT. It did not say
WHY the date is missing, and its gate labelled every one of them "our gap". That
label was an assumption. A missing date has two very different owners:

* **code-owned** — HubSpot holds an exact timestamp and we lost it somewhere on
  the way in (not requested, not refreshed, dropped, unparsed, or stored but the
  incident never closed). These are bugs. They can be repaired, and the code
  that lost them must be fixed.
* **source-unresolvable** — HubSpot itself holds no exact SQL entry. The most
  common shape is a STAGE JUMP: the contact moved from a stage below SQL
  straight to opportunity or customer, so HubSpot never recorded it in
  ``salesqualifiedlead`` at all. Its current stage proves it REACHED SQL; nothing
  anywhere says WHEN. No engineering produces that date.

And a third group that is neither: **not determined** — we did not look, or we
looked and got no answer. "We did not look" is never reported as "there is
nothing".

What this module refuses to do
------------------------------
It never produces an SQL-entry date. Observation bounds are computed here —
``last_known_below_sql_at`` and ``first_observed_at_or_above_sql`` — and they
are INTERVAL BOUNDS taken from HubSpot's own version timestamps. Neither is the
event. The first is the last instant HubSpot recorded the contact BELOW SQL; the
second is the first instant of the current at-or-above-SQL run. A contact that
jumped from ``lead`` to ``opportunity`` has its opportunity instant as the
second bound, and that instant is an OPPORTUNITY timestamp — on the forbidden
list of SQL substitutes by name (docs/41 §2). It is stored, reported and named
as a bound, and nothing may read it as ``date_entered_sql``.
"""

from __future__ import annotations

from analysis.crm_lifecycle import (
    EVENT_SQL,
    STAGE_RANK,
    STAGE_SQL,
    normalize_lifecycle_stage,
    stages_implying_event,
)

#: The rank at and above which a stage proves SQL was reached. Taken from the
#: one lifecycle rule; never restated.
_SQL_RANK = STAGE_RANK[STAGE_SQL]

#: Where an observation bound came from. Only HubSpot's own version timestamps
#: qualify — a sync time, a detection time or a creation time is not a record
#: of the contact's stage at that instant.
BOUNDS_BASIS_HISTORY = "hubspot_lifecycle_history"

# ── Direct property state · denominator: one contact's direct SQL property ──
DIRECT_PRESENT = "present"
DIRECT_ABSENT = "absent"
#: HubSpot sent a non-empty value we could not parse. Ours to fix — distinct
#: from absence, which is HubSpot's answer.
DIRECT_UNPARSEABLE = "unparseable"
#: The read did not request the property, so it says nothing either way.
DIRECT_NOT_READ = "not_read"

DIRECT_STATES = (DIRECT_PRESENT, DIRECT_ABSENT, DIRECT_UNPARSEABLE,
                 DIRECT_NOT_READ)

# ── FACTS · denominator: one open incident. Several may hold at once. ───────
# The brief's vocabulary, verbatim. A fact is an observation, not a verdict:
# "history_payload_present" and "history_has_no_sql_transition" both hold for a
# stage jump, and neither alone says whose gap it is.
F_DIRECT_PRESENT = "direct_property_present"
F_DIRECT_ABSENT = "direct_property_absent"
F_DIRECT_UNPARSEABLE = "direct_property_unparseable"
F_STORED_PRESENT = "stored_property_present"
F_STORED_ABSENT = "stored_property_absent"
F_HISTORY_PRESENT = "history_payload_present"
F_HISTORY_ABSENT = "history_payload_absent"
F_HISTORY_FAILED = "history_request_failed"
F_HISTORY_EXACT = "history_has_exact_sql_transition"
F_HISTORY_NO_SQL = "history_has_no_sql_transition"
F_STAGE_JUMP = "stage_jump_skipped_sql"
#: PR-ADS-161C review: HubSpot set the direct property before our last
#: INGESTION of the contact. That does not prove the payload we read carried
#: it — ingestion happens after the read — so it is a fact, not a cause.
#: (`writer_dropped_evidence`, the brief's name for that cause, is therefore
#: never emitted: no timestamp this repository records can prove it.)
F_DIRECT_SET_BEFORE_INGEST = "direct_property_set_before_last_ingest"
F_CANDIDATE_NOT_REFRESHED = "candidate_not_refreshed"
F_LATE_PROPERTY = "late_property_not_refreshed"
F_CAUSE_UNRESOLVED = "cause_unresolved"
#: PR-ADS-161C additions, each independently demonstrable.
#: The local store already holds exact evidence, yet the incident is open. The
#: date is known; only the incident's closure was lost.
F_STORED_EVIDENCE_OPEN = "stored_evidence_incident_open"
#: The contact's current stage no longer implies SQL, so the detector — whose
#: population is "current stage implies SQL" — never looks at it again.
F_OUTSIDE_POPULATION = "outside_detector_population"
#: HubSpot did not return the contact at all (deleted, merged or not visible).
F_SOURCE_NOT_RETURNED = "source_contact_not_returned"
#: History was never requested for this contact (budget).
F_HISTORY_NOT_CONSULTED = "history_not_consulted"
#: An SQL version exists in history but its timestamp would not parse — ours.
F_HISTORY_SQL_UNPARSEABLE = "history_sql_timestamp_unparseable"
#: An SQL version exists in history and HubSpot recorded no timestamp — theirs.
F_HISTORY_SQL_UNDATED = "history_sql_version_undated"

FACTS = (
    F_DIRECT_PRESENT, F_DIRECT_ABSENT, F_DIRECT_UNPARSEABLE,
    F_STORED_PRESENT, F_STORED_ABSENT,
    F_HISTORY_PRESENT, F_HISTORY_ABSENT, F_HISTORY_FAILED,
    F_HISTORY_EXACT, F_HISTORY_NO_SQL, F_STAGE_JUMP,
    F_DIRECT_SET_BEFORE_INGEST, F_CANDIDATE_NOT_REFRESHED, F_LATE_PROPERTY,
    F_CAUSE_UNRESOLVED,
    F_STORED_EVIDENCE_OPEN, F_OUTSIDE_POPULATION, F_SOURCE_NOT_RETURNED,
    F_HISTORY_NOT_CONSULTED, F_HISTORY_SQL_UNPARSEABLE, F_HISTORY_SQL_UNDATED,
)

# ── CLASSIFICATION · denominator: one open incident. Exactly one each. ──────
# The ROOT CAUSE, chosen by a fixed precedence (see ``_classify``): where the
# evidence disappeared, not merely that it is missing.
C_STORED_EVIDENCE_OPEN = F_STORED_EVIDENCE_OPEN
C_CANDIDATE_NOT_REFRESHED = F_CANDIDATE_NOT_REFRESHED
C_LATE_PROPERTY = F_LATE_PROPERTY
C_DIRECT_UNPARSEABLE = F_DIRECT_UNPARSEABLE
C_HISTORY_EXACT_NOT_STORED = F_HISTORY_EXACT
C_HISTORY_SQL_UNPARSEABLE = F_HISTORY_SQL_UNPARSEABLE
C_HISTORY_SQL_UNDATED = F_HISTORY_SQL_UNDATED
C_STAGE_JUMP = F_STAGE_JUMP
C_HISTORY_NO_SQL = F_HISTORY_NO_SQL
C_HISTORY_FAILED = F_HISTORY_FAILED
C_HISTORY_ABSENT = F_HISTORY_ABSENT
C_HISTORY_NOT_CONSULTED = F_HISTORY_NOT_CONSULTED
C_CAUSE_UNRESOLVED = F_CAUSE_UNRESOLVED

#: HubSpot holds (or held) the evidence and our path lost it. Each is a bug.
CODE_OWNED = (
    C_STORED_EVIDENCE_OPEN, C_CANDIDATE_NOT_REFRESHED, C_LATE_PROPERTY,
    C_DIRECT_UNPARSEABLE, C_HISTORY_EXACT_NOT_STORED,
    C_HISTORY_SQL_UNPARSEABLE,
)
#: HubSpot answered, and its answer contains no exact SQL entry.
SOURCE_UNRESOLVABLE = (C_STAGE_JUMP, C_HISTORY_NO_SQL, C_HISTORY_SQL_UNDATED)
#: Nobody can say yet — we did not look, or got no answer.
NOT_DETERMINED = (C_HISTORY_FAILED, C_HISTORY_ABSENT, C_HISTORY_NOT_CONSULTED,
                  C_CAUSE_UNRESOLVED)

CLASSIFICATIONS = CODE_OWNED + SOURCE_UNRESOLVABLE + NOT_DETERMINED

OWNER_CODE = "code_owned_loss"
OWNER_SOURCE = "source_has_no_exact_sql_entry"
OWNER_UNKNOWN = "not_determined"


def owner_of(classification: str) -> str:
    """Whose gap is it? Fails closed: an unknown value is NOT determined."""
    if classification in CODE_OWNED:
        return OWNER_CODE
    if classification in SOURCE_UNRESOLVABLE:
        return OWNER_SOURCE
    return OWNER_UNKNOWN


#: The substitutions this programme forbids as an SQL-entry date, stated once so
#: the documentation, the audit output and the tests quote the same list.
FORBIDDEN_SQL_DATE_SUBSTITUTES = (
    "contact creation time",
    "database insertion time",
    "sync time",
    "ingestion time",
    "boundary time",
    "incident creation time",
    "first time the application noticed the contact",
    "the boundary upper bound",
    "first_observed_at_or_above_sql",
    "last_known_below_sql_at",
    "any inferred midpoint between two observations",
    "opportunity or customer entry timestamps",
)


# ═════════════════════════════════════════════════════════════════════════════
# Evidence shapes
# ═════════════════════════════════════════════════════════════════════════════

def direct_entry_state(raw_present: bool, parsed) -> str:
    """The direct SQL property's state from one read. Pure.

    ``raw_present`` is whether HubSpot sent a non-empty value; ``parsed`` is our
    parse of it. A present raw value that will not parse is OURS — it must never
    collapse into "absent", which is HubSpot's answer.
    """
    if not raw_present:
        return DIRECT_ABSENT
    return DIRECT_PRESENT if parsed is not None else DIRECT_UNPARSEABLE


def _stage_rank(stage):
    return STAGE_RANK.get(normalize_lifecycle_stage(stage))


def history_shape(versions) -> dict:
    """Summarise one contact's ``lifecyclestage`` history. Pure, no dates made.

    Returns::

        {"versions_seen", "stage_path", "has_sql_version",
         "sql_version_dated", "sql_version_unparseable", "sql_version_undated",
         "stage_jump_skipped_sql", "last_known_below_sql_at",
         "first_observed_at_or_above_sql", "bounds_basis"}

    ``stage_jump_skipped_sql`` is three-valued on purpose:

    * ``True``  — HubSpot recorded the contact BELOW SQL, then at a stage ABOVE
      SQL, and never in ``salesqualifiedlead`` anywhere in its history. It
      skipped the stage, so no SQL entry exists to recover;
    * ``False`` — an SQL version exists, so whatever is missing, it is not a
      skipped stage;
    * ``None``  — undeterminable: no ordered at-or-above-SQL run, or nothing
      known-below-SQL immediately before it (a contact created directly at
      opportunity, or preceded by a stage with no funnel rank).

    The two bounds are HubSpot's own version timestamps around the transition
    into the CURRENT at-or-above-SQL run. ``last_known_below_sql_at`` is NULL
    unless the version immediately before that run has a funnel rank below SQL —
    a stage with no rank (``other``, a custom stage) proves nothing about SQL,
    and an unknown lower bound stays unknown.
    """
    versions = list(versions or [])
    has_sql = any(normalize_lifecycle_stage(v.get("value")) == STAGE_SQL
                  for v in versions)
    sql_versions = [v for v in versions
                    if normalize_lifecycle_stage(v.get("value")) == STAGE_SQL]
    sql_dated = any(v.get("timestamp") is not None for v in sql_versions)
    sql_unparseable = (not sql_dated) and any(
        v.get("timestamp") is None
        and v.get("timestamp_raw") not in (None, "") for v in sql_versions)
    sql_undated = bool(sql_versions) and not sql_dated and not sql_unparseable

    dated = sorted(((v["timestamp"], normalize_lifecycle_stage(v.get("value")))
                    for v in versions if v.get("timestamp") is not None),
                   key=lambda pair: pair[0])
    path: list = []
    for _ts, stage in dated:
        if stage and (not path or path[-1] != stage):
            path.append(stage)

    # The trailing run of versions whose stage proves SQL was reached.
    i = len(dated) - 1
    while i >= 0:
        rank = _stage_rank(dated[i][1])
        if rank is None or rank < _SQL_RANK:
            break
        i -= 1
    streak = dated[i + 1:]
    first_at_or_above = streak[0][0] if streak else None
    before = dated[i] if (streak and i >= 0) else None
    before_rank = _stage_rank(before[1]) if before else None
    last_below = (before[0] if before is not None and before_rank is not None
                  and before_rank < _SQL_RANK else None)

    # A version HubSpot recorded without a usable timestamp cannot be placed
    # in the order, so it might sit between any two dated versions — and then
    # neither adjacency nor either bound is proven. (An SQL version still
    # proves "not a skip".)
    if any(v.get("timestamp") is None for v in versions):
        first_at_or_above = last_below = None
        streak = []

    if has_sql:
        jump = False
    elif streak and last_below is not None and streak[0][1] != STAGE_SQL:
        jump = True
    else:
        jump = None

    has_bounds = first_at_or_above is not None or last_below is not None
    return {
        "versions_seen": len(versions),
        "stage_path": ">".join(path) if path else None,
        "has_sql_version": has_sql,
        "sql_version_dated": sql_dated,
        "sql_version_unparseable": sql_unparseable,
        "sql_version_undated": sql_undated,
        "stage_jump_skipped_sql": jump,
        "last_known_below_sql_at": last_below,
        "first_observed_at_or_above_sql": first_at_or_above,
        "bounds_basis": BOUNDS_BASIS_HISTORY if has_bounds else None,
    }


def stage_implies_sql(stage) -> bool:
    """Does the CURRENT stage prove SQL was reached? The one lifecycle rule."""
    return normalize_lifecycle_stage(stage) in stages_implying_event(EVENT_SQL)


# ═════════════════════════════════════════════════════════════════════════════
# Classification
# ═════════════════════════════════════════════════════════════════════════════

# Incident reasons, by value. Imported as strings rather than from the service
# so this module stays pure; `tests/test_pr_ads_161c_*` pins that every reason
# the service can emit is mapped here.
_REASON_NOT_CONSULTED = "post_boundary_no_direct_sql_date"
_REASON_NO_SQL = "post_boundary_history_has_no_sql_transition"
_REASON_FAILED = "post_boundary_history_request_failed"
_REASON_ABSENT = "post_boundary_history_payload_absent"
_REASON_SQL_UNPARSEABLE = "post_boundary_history_sql_timestamp_unparseable"
_REASON_SQL_UNDATED = "post_boundary_history_sql_version_undated"

MAPPED_INCIDENT_REASONS = (_REASON_NOT_CONSULTED, _REASON_NO_SQL,
                           _REASON_FAILED, _REASON_ABSENT,
                           _REASON_SQL_UNPARSEABLE, _REASON_SQL_UNDATED)


def _local_facts(local: dict) -> list:
    facts = []
    stored_direct = local.get("direct_sql_entry_at") is not None
    stored_any = stored_direct or local.get("recovered_sql_entry_at") is not None
    facts.append(F_STORED_PRESENT if stored_direct else F_STORED_ABSENT)
    if stored_any:
        facts.append(F_STORED_EVIDENCE_OPEN)
    if local.get("funnel_row_present") and not stage_implies_sql(
            local.get("current_lifecycle_stage")):
        facts.append(F_OUTSIDE_POPULATION)
    return facts


def classify_local(local: dict) -> dict:
    """Classify one OPEN incident from the local store alone. Pure.

    ``local`` carries the incident row and the contact's stored evidence::

        {"reason", "history_state", "history_checked", "direct_property_state",
         "stage_jump_skipped_sql", "direct_sql_entry_at",
         "recovered_sql_entry_at", "current_lifecycle_stage",
         "funnel_row_present"}

    Local evidence cannot see HubSpot, so the upstream-present causes
    (candidate/late/writer) are not decidable here; ``--compare-hubspot``
    decides them. Nothing undecidable is guessed into a cause.
    """
    facts = _local_facts(local)
    reason = local.get("reason")
    direct_state = local.get("direct_property_state")

    if F_STORED_EVIDENCE_OPEN in facts:
        cls = C_STORED_EVIDENCE_OPEN
    elif direct_state == DIRECT_UNPARSEABLE:
        facts.append(F_DIRECT_UNPARSEABLE)
        cls = C_DIRECT_UNPARSEABLE
    elif reason == _REASON_SQL_UNPARSEABLE:
        facts += [F_HISTORY_PRESENT, F_HISTORY_SQL_UNPARSEABLE]
        cls = C_HISTORY_SQL_UNPARSEABLE
    elif reason == _REASON_SQL_UNDATED:
        facts += [F_HISTORY_PRESENT, F_HISTORY_SQL_UNDATED]
        cls = C_HISTORY_SQL_UNDATED
    elif reason == _REASON_FAILED:
        facts.append(F_HISTORY_FAILED)
        cls = C_HISTORY_FAILED
    elif reason == _REASON_ABSENT:
        facts.append(F_HISTORY_ABSENT)
        cls = C_HISTORY_ABSENT
    elif reason == _REASON_NOT_CONSULTED:
        facts.append(F_HISTORY_NOT_CONSULTED)
        cls = C_HISTORY_NOT_CONSULTED
    elif reason == _REASON_NO_SQL:
        facts += [F_HISTORY_PRESENT, F_HISTORY_NO_SQL]
        if local.get("stage_jump_skipped_sql") is True:
            facts.append(F_STAGE_JUMP)
            cls = C_STAGE_JUMP
        else:
            cls = C_HISTORY_NO_SQL
    else:
        # An unrecognised reason is not evidence of anything. Fail closed.
        facts.append(F_CAUSE_UNRESOLVED)
        cls = C_CAUSE_UNRESOLVED

    if direct_state == DIRECT_ABSENT:
        facts.append(F_DIRECT_ABSENT)
    cls = _require_direct_absence(cls, direct_state, facts)
    return {"classification": cls, "owner": owner_of(cls),
            "facts": _dedupe(facts), "basis": "local_store"}


def _require_direct_absence(cls: str, direct_state, facts: list) -> str:
    """A source-unresolvable verdict needs the DIRECT property read as absent.

    History alone cannot say HubSpot holds no exact SQL entry: the direct
    property is the other permitted source, and defect 1 was exactly a direct
    date HubSpot held while history showed no SQL version. Every incident
    recorded before PR-ADS-161C has no direct-property state at all, so until
    a detection pass (or a comparison) reads it, its owner is not determined.
    The history facts stay; only the verdict is withheld.
    """
    if cls in SOURCE_UNRESOLVABLE and direct_state != DIRECT_ABSENT:
        facts.append(F_CAUSE_UNRESOLVED)
        return C_CAUSE_UNRESOLVED
    return cls


def classify_with_source(local: dict, source: dict) -> dict:
    """Classify one OPEN incident against a fresh READ-ONLY HubSpot read. Pure.

    ``source`` is one contact's comparison read::

        {"request_failed": bool, "returned": bool,
         "direct_state", "direct_sql_entry_at", "direct_sql_entry_set_at",
         "last_modified_at", "history_state", "history_shape"}

    Precedence — where did the evidence disappear?

    1. stored locally but the incident is open        → incident closure lost
    2. the read itself failed                         → we did not look
    3. HubSpot did not return the contact             → not determined
    4. direct property sent but unparseable           → our parser
    5. direct property present upstream, absent here:
         HubSpot modified after our stored copy       → candidate not refreshed
         set after we last ingested this version      → late property
         set before we last ingested it               → writer dropped it
         set-instant unknown                          → not determined
    6. history unavailable                            → not determined
    7. history holds a dated SQL transition           → not stored by us
    8. SQL version with unparseable timestamp         → our parser
    9. SQL version with no timestamp                  → HubSpot recorded none
    10. stage jump proven                             → HubSpot never had SQL
    11. history holds no SQL version                  → HubSpot never had SQL
    """
    facts = _local_facts(local)
    source = source or {}
    shape = source.get("history_shape") or {}
    direct_state = source.get("direct_state")

    if source.get("request_failed"):
        facts.append(F_HISTORY_FAILED)
    elif source.get("returned") is False:
        facts.append(F_SOURCE_NOT_RETURNED)
    else:
        if direct_state == DIRECT_PRESENT:
            facts.append(F_DIRECT_PRESENT)
        elif direct_state == DIRECT_ABSENT:
            facts.append(F_DIRECT_ABSENT)
        elif direct_state == DIRECT_UNPARSEABLE:
            facts.append(F_DIRECT_UNPARSEABLE)
        if source.get("history_state") == "history_payload_present":
            facts.append(F_HISTORY_PRESENT)
            if shape.get("sql_version_dated"):
                facts.append(F_HISTORY_EXACT)
            elif not shape.get("has_sql_version"):
                # An SQL version with a bad or missing timestamp is NOT "no
                # transition" — that was defect 4, at the fact level.
                facts.append(F_HISTORY_NO_SQL)
            if shape.get("stage_jump_skipped_sql") is True:
                facts.append(F_STAGE_JUMP)
            if shape.get("sql_version_unparseable"):
                facts.append(F_HISTORY_SQL_UNPARSEABLE)
            if shape.get("sql_version_undated"):
                facts.append(F_HISTORY_SQL_UNDATED)
        else:
            facts.append(F_HISTORY_ABSENT)

    if F_STORED_EVIDENCE_OPEN in facts:
        cls = C_STORED_EVIDENCE_OPEN
    elif source.get("request_failed"):
        cls = C_HISTORY_FAILED
    elif source.get("returned") is False:
        cls = C_CAUSE_UNRESOLVED
    elif direct_state == DIRECT_UNPARSEABLE:
        cls = C_DIRECT_UNPARSEABLE
    elif direct_state == DIRECT_PRESENT:
        cls = _direct_loss_cause(local, source)
        facts.append(cls)
        set_at = source.get("direct_sql_entry_set_at")
        ingested_at = local.get("last_ingested_at")
        if (cls == C_CAUSE_UNRESOLVED and set_at is not None
                and ingested_at is not None and set_at <= ingested_at):
            facts.append(F_DIRECT_SET_BEFORE_INGEST)
    elif F_HISTORY_ABSENT in facts:
        cls = C_HISTORY_ABSENT
    elif shape.get("sql_version_dated"):
        cls = C_HISTORY_EXACT_NOT_STORED
    elif shape.get("sql_version_unparseable"):
        cls = C_HISTORY_SQL_UNPARSEABLE
    elif shape.get("sql_version_undated"):
        cls = C_HISTORY_SQL_UNDATED
    elif shape.get("stage_jump_skipped_sql") is True:
        cls = C_STAGE_JUMP
    else:
        cls = C_HISTORY_NO_SQL
    cls = _require_direct_absence(cls, direct_state, facts)
    if cls == C_CAUSE_UNRESOLVED:
        facts.append(F_CAUSE_UNRESOLVED)
    return {"classification": cls, "owner": owner_of(cls),
            "facts": _dedupe(facts), "basis": "hubspot_comparison"}


def _direct_loss_cause(local: dict, source: dict) -> str:
    """HubSpot holds the direct property and we do not. Where did it go?

    Decided only from timestamps HubSpot and our own row actually carry:

    * HubSpot's ``lastmodifieddate`` is newer than the copy we stored → the
      sync has not re-read this contact since it changed;
    * otherwise, the property's own history says when HubSpot SET it. Set
      after our last ingestion of this contact → it arrived without moving
      ``lastmodifieddate`` far enough for the watermark to re-select the
      contact.

    Set at or before our last ingestion proves nothing: ingestion happens
    AFTER the read, so HubSpot could have set it between the two. "The writer
    dropped it" would need the payload we actually read, which is not
    recorded — so that case, and any case missing an instant, is reported
    unresolved rather than picked.
    """
    upstream_modified = source.get("last_modified_at")
    stored_modified = local.get("last_modified_at")
    if (upstream_modified is not None and stored_modified is not None
            and upstream_modified > stored_modified):
        return C_CANDIDATE_NOT_REFRESHED
    set_at = source.get("direct_sql_entry_set_at")
    ingested_at = local.get("last_ingested_at")
    if set_at is not None and ingested_at is not None and set_at > ingested_at:
        return C_LATE_PROPERTY
    return C_CAUSE_UNRESOLVED


def _dedupe(items) -> list:
    seen: list = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def summarize(classified: list) -> dict:
    """Root-cause breakdown over classified incidents. Counts only; no guesses."""
    by_class = {c: 0 for c in CLASSIFICATIONS}
    by_owner = {OWNER_CODE: 0, OWNER_SOURCE: 0, OWNER_UNKNOWN: 0}
    for item in classified or []:
        cls = (item or {}).get("classification")
        by_class[cls] = by_class.get(cls, 0) + 1
        by_owner[owner_of(cls)] += 1
    return {"by_classification": {k: v for k, v in by_class.items() if v},
            "by_owner": by_owner,
            "code_owned_losses": by_owner[OWNER_CODE],
            "source_unresolvable": by_owner[OWNER_SOURCE],
            "not_determined": by_owner[OWNER_UNKNOWN]}
