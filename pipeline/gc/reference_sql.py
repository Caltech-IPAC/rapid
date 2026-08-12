"""The reference surfaces, as real SQL against the real schema.

**THE SET IS DEFINED BY ENUMERATION OVER SCOPES, NOT BY ONE TABLE**, and this
module is where that enumeration lives. The reason is stated at length in
`references.py`: `artifacts` is not populated on the live registration path,
so an anti-join keyed on `artifacts.uri` alone would classify every real
product as garbage.

Each query below is a REFERENCE SURFACE. An object appearing in ANY of them is
referenced and therefore retained. The list is a floor that this branch
re-derived rather than took on trust; the three classes the re-derivation
added beyond the brief's list are handled in `references.py` by clause 3
(unattributable), because they carry no database row of any kind to join
against — which is itself the finding.

**DRAFT-GATED SURFACES ARE PROBED, NEVER CAUGHT.** `artifacts` (DRAFT 048) and
`submissions` (DRAFT 044) may not exist. Catching `UndefinedTable` would abort
the caller's transaction; asking `to_regclass` first never does. And a surface
that cannot be read is NOT skipped — `collect_references` refuses the plan,
because a reference surface that was not consulted is a set of objects nobody
checked for references.
"""

from pipeline.gc.references import PlanRefused

#: `artifacts.uri` — DRAFT 048. REGARDLESS OF WHETHER ITS BINDING IS CURRENT
#: OR SUPERSEDED: a superseded artifact's bytes may still be legitimately
#: live, so `product_artifacts.is_current` is deliberately NOT the join
#: surface. Joining on the current binding alone would make every superseded
#: artifact a candidate.
ARTIFACTS_SQL = "SELECT uri FROM artifacts WHERE uri IS NOT NULL"

#: The legacy product columns, still written independently and migrated by no
#: reader (brief D deliberately migrated none).
REFIMAGES_SQL = (
    "SELECT filename FROM refimages WHERE filename IS NOT NULL")
DIFFIMAGES_SQL = (
    "SELECT filename FROM diffimages WHERE filename IS NOT NULL")

#: `refimcatalogs.filename` — a LIVE S3 URI written from `entry["uri"]`
#: (`pipeline/registration/products.py:215`) whenever the optional catalogue
#: entry exists. Not written for every reference image, and WHERE PRESENT IT
#: IS FREQUENTLY THE ONLY DATABASE REFERENCE TO THOSE BYTES.
REFIMCATALOGS_SQL = (
    "SELECT filename FROM refimcatalogs WHERE filename IS NOT NULL")

#: Active submission manifests — DRAFT 044's `submissions.manifest_uri`.
#:
#: **"ACTIVE" IS DEFINED HERE, NOT LEFT TO INTERPRETATION.** DRAFT 044's
#: `bound`/`found` states mean the submission API call RESOLVED, not that its
#: Batch children finished. So a manifest is active — and retained — unless
#: EVERY work unit and attempt it submitted has reached an eligible-owner
#: state. Attempts do not carry the work-unit `complete`/`cancelled`
#: vocabulary, so the predicate is spelled out: every linked attempt resolves
#: to a work unit, every such unit is `complete` or `cancelled`, and no linked
#: attempt is live.
#:
#: A manifest whose children cannot be resolved is RETAINED.
ACTIVE_MANIFESTS_SQL = """
SELECT s.manifest_uri
  FROM submissions s
 WHERE s.manifest_uri IS NOT NULL
   AND (
     -- No resolvable children at all: unattributable, therefore retained.
     NOT EXISTS (SELECT 1 FROM attempts a WHERE a.run_id = s.run_id)
     OR EXISTS (
       SELECT 1 FROM attempts a
        LEFT JOIN work_units w ON w.work_unit_id = a.work_unit_id
        WHERE a.run_id = s.run_id
          AND (w.work_unit_id IS NULL
               OR w.state NOT IN ('complete', 'cancelled')
               OR a.lifecycle_state IN ('submitted', 'started',
                                        'application_closed'))))
"""


def surface_present(execute, relation):
    """Is this reference surface deployed? Probed, never caught."""
    rows = execute("SELECT to_regclass(%s) IS NOT NULL", ["public." + relation])
    return bool(rows and _first(rows[0]))


def collect_references(execute, *, require_all=True):
    """Every referenced URI, from every deployed surface.

    Returns `(references, consulted, absent)`.

    **A SURFACE THAT CANNOT BE READ REFUSES THE PLAN.** `require_all` governs
    surfaces that are DEPLOYED but unreadable — a permission problem, a
    corrupted table — which is different from a surface that is not deployed
    at all. An undeployed DRAFT surface is recorded in `absent` and the run
    continues (there is genuinely nothing there to reference anything); a
    deployed surface that errors is fatal, because the objects it would have
    protected would otherwise be silently unprotected.
    """
    surfaces = (
        ("artifacts", ARTIFACTS_SQL),
        ("refimages", REFIMAGES_SQL),
        ("diffimages", DIFFIMAGES_SQL),
        ("refimcatalogs", REFIMCATALOGS_SQL),
        ("submissions", ACTIVE_MANIFESTS_SQL),
    )

    references, consulted, absent = set(), [], []
    for relation, sql in surfaces:
        if not surface_present(execute, relation):
            absent.append(relation)
            continue
        try:
            rows = execute(sql, [])
        except Exception as exc:                      # noqa: BLE001
            if require_all:
                raise PlanRefused(
                    "reference surface %r is deployed but could not be read "
                    "(%s). The plan is REFUSED rather than computed without "
                    "it: objects that surface would have protected would "
                    "otherwise be silently unprotected, and this design "
                    "resolves every ambiguity toward not deleting."
                    % (relation, exc)) from exc
            absent.append(relation)
            continue
        for row in rows or ():
            value = _first(row)
            if value:
                references.add(str(value))
        consulted.append(relation)

    return references, consulted, absent


def attempt_facts(execute, run_ids=None):
    """Every attempt's own job type, run id, work-unit key and attempt id.

    The inputs to the canonical round trip. Read from the attempt's OWN row
    rather than parsed from a key — that direction is the whole point.

    **THE UNIT KEY IS RECONSTRUCTED, NOT STORED.** `work_units` has no
    `unit_key` column: the persisted identity is `(job_type, input_scope)`,
    and `input_scope` is the declared-subject tuple with its leading
    `job_type` element DROPPED and the rest joined with `/`
    (`submission/subjects.build_input_scope`, wrapped by
    `pipeline/seams.py:411`). `ProcessingUnit.key` — the thing
    `product_prefix()` interpolates — is that same grammar WITH the job_type,
    so the round trip rebuilds it as `job_type || '/' || input_scope`.
    Verified against `036-intent-schema-v1.sql:111-122` on this branch's head
    rather than assumed.
    """
    sql = """
    SELECT a.attempt_id,
           coalesce(lj.job_type, w.job_type) AS job_type,
           a.run_id,
           CASE WHEN w.job_type IS NULL OR w.input_scope IS NULL THEN NULL
                ELSE w.job_type || '/' || w.input_scope END AS unit_key
      FROM attempts a
      LEFT JOIN logical_jobs lj ON lj.logical_job_id = a.logical_job_id
      LEFT JOIN work_units w ON w.work_unit_id = a.work_unit_id
    """
    params = []
    if run_ids:
        sql += " WHERE a.run_id = ANY(%s)"
        params.append(list(run_ids))
    facts = {}
    for row in execute(sql, params) or ():
        attempt_id, job_type, run_id, unit_key = _row_values(row, 4)
        facts[attempt_id] = {"job_type": job_type, "run_id": run_id,
                             "unit_key": unit_key}
    return facts


def owners(execute, attempt_ids=None):
    """The discharge facts for each attempt's owning work unit.

    One row per attempt carrying: the unit's state, the attempt's registration
    watermark and terminal-record sequence, and a count of LIVE attempts on
    the same unit across all three live states.
    """
    sql = """
    SELECT a.attempt_id,
           w.state AS unit_state,
           a.registered_record_sequence,
           a.terminal_record_sequence,
           (SELECT count(*) FROM attempts la
             WHERE la.work_unit_id = a.work_unit_id
               AND la.lifecycle_state IN ('submitted', 'started',
                                          'application_closed')) AS live_count
      FROM attempts a
      LEFT JOIN work_units w ON w.work_unit_id = a.work_unit_id
    """
    params = []
    if attempt_ids:
        sql += " WHERE a.attempt_id = ANY(%s)"
        params.append(list(attempt_ids))
    result = {}
    for row in execute(sql, params) or ():
        (attempt_id, unit_state, registered, terminal,
         live_count) = _row_values(row, 5)
        result[attempt_id] = {
            "unit_state": unit_state,
            "registered_record_sequence": registered,
            "terminal_record_sequence": terminal,
            "live_attempt_count": live_count or 0,
        }
    return result


def _first(row):
    if isinstance(row, dict):
        return next(iter(row.values()))
    if isinstance(row, (list, tuple)):
        return row[0]
    return row


def _row_values(row, count):
    if isinstance(row, dict):
        return tuple(row.values())[:count]
    return tuple(row)[:count]
