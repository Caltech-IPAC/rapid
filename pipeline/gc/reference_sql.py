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


#: Every URI-bearing field in the payload family, ENUMERATED FROM THE
#: DATACLASSES rather than taken from any partial list. Verified at this
#: branch's head: `science_image_uri` (`payloads.py:308`), `psf_uri` (`:332`)
#: and `reference_image_uri` (`:341`) on `ImagingPayload`; `coadd_inputs_uri`
#: (`:433`) on `ReferenceImagePayload`. No other payload class declares one.
#:
#: **`reference_image_uri` IS A CROSS-ATTEMPT REFERENCE** — a reference image
#: published by one attempt is cited by many later science manifests — which
#: is why expanding manifest bodies matters and attempt-scoped reasoning alone
#: would delete live inputs.
MANIFEST_URI_FIELDS = ("science_image_uri", "psf_uri", "reference_image_uri",
                       "coadd_inputs_uri")


class ManifestUnreadable(PlanRefused):
    """An in-scope manifest could not be read or expanded.

    **THE PLAN IS REFUSED, NOT THE OBJECT.** "Its referenced objects" cannot
    be identified without reading it, so the fallback cannot be per-object:
    guessing which objects an unreadable manifest covers is exactly the guess
    this design refuses to make. Nothing is deleted in a run where this is
    raised.
    """

    error_category = "gc_manifest_unreadable"


def expand_manifest_bodies(uris, reader):
    """Every object an active manifest's BODY references.

    **AN ACTIVE MANIFEST PROTECTS ITS CONTENTS AS WELL AS ITSELF.** The
    manifest body carries input URIs, and anti-joining against the manifest
    row alone would leave every input it names looking unreferenced. So each
    active manifest is READ and expanded, and every URI field present in its
    units is added to the reference set.

    `reader(uri)` returns the parsed manifest body (a dict) or raises. Kept
    injectable so the contract tier can supply one that REFUSES — a reader
    that cannot fail could not exercise the plan-level refusal below, which is
    the whole safety property here.

    Every URI FIELD IS ENUMERATED, not coded to a partial list: an unknown
    key ending in `_uri` is treated as a reference too, so a payload gaining a
    new URI component does not silently drop out of the reference set.
    """
    referenced = set()
    for uri in sorted(uris):
        try:
            body = reader(uri)
        except Exception as exc:                      # noqa: BLE001
            raise ManifestUnreadable(
                "in-scope manifest %s could not be read (%s). THE PLAN IS "
                "REFUSED and nothing is deleted in this run: the objects that "
                "manifest references cannot be identified without reading it, "
                "and guessing which they are is precisely the guess this "
                "design does not make." % (uri, exc)) from exc
        if body is None:
            raise ManifestUnreadable(
                "in-scope manifest %s expanded to nothing; refusing the plan "
                "rather than treating an unreadable manifest as protecting no "
                "objects" % (uri,))
        referenced.update(_uris_in(body))
    return referenced


def _uris_in(node):
    """Every URI-valued leaf in a manifest body, at any depth.

    Walks rather than reading known keys: the units are nested inside the
    manifest and a top-level scan would see none of them. Both the enumerated
    field names and any other key ending `_uri` are collected, so a payload
    that gains a URI component is covered before anyone updates a list.
    """
    found = set()
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if (lowered in MANIFEST_URI_FIELDS or lowered.endswith("_uri")) \
                    and isinstance(value, str) and value.strip():
                found.add(value.strip())
            else:
                found.update(_uris_in(value))
    elif isinstance(node, (list, tuple)):
        for value in node:
            found.update(_uris_in(value))
    return found


def surface_present(execute, relation):
    """Is this reference surface deployed? Probed, never caught."""
    rows = execute("SELECT to_regclass(%s) IS NOT NULL", ["public." + relation])
    return bool(rows and _first(rows[0]))


#: Coadd-input CSV objects — `submission/gathering.py:983`, written under
#: `submissions/<run_id>/coadd-inputs/...` and CITED BY MANIFESTS while
#: carrying no row of their own in any product table. They reach the
#: reference set two ways: by manifest expansion (a reference image payload's
#: `coadd_inputs_uri`) and, for manifests predating that field, by this
#: prefix-scoped surface over the submissions table's own run ids.
COADD_INPUT_PREFIX = "coadd-inputs/"


def collect_references(execute, *, require_all=True, manifest_reader=None):
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
        surface_values = set()
        for row in rows or ():
            value = _first(row)
            if value:
                surface_values.add(str(value))
        references.update(surface_values)
        consulted.append(relation)

        # AN ACTIVE MANIFEST PROTECTS ITS CONTENTS AS WELL AS ITSELF. The
        # manifest row alone would leave every input it names looking
        # unreferenced — including `reference_image_uri`, which is a
        # CROSS-ATTEMPT reference cited by many later science manifests. So
        # the bodies are read and expanded here, and an unreadable one
        # REFUSES THE PLAN rather than being skipped.
        if relation == "submissions" and surface_values:
            if manifest_reader is None:
                # NOT SILENTLY SKIPPED. Without a reader the contents cannot
                # be identified, which is the same state as an unreadable
                # manifest and gets the same answer.
                raise ManifestUnreadable(
                    "%d active manifest(s) are in scope but no manifest "
                    "reader was supplied, so their referenced objects cannot "
                    "be identified. THE PLAN IS REFUSED: an active manifest "
                    "protects its contents as well as itself, and computing "
                    "a plan without expanding them would leave every input "
                    "they name looking unreferenced."
                    % (len(surface_values),))
            references.update(
                expand_manifest_bodies(surface_values, manifest_reader))
            consulted.append("submissions:bodies")

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

    **`data_class` IS NOW READ FROM THE UNIT, AND NULL IS AN ANSWER.**
    This SELECT used to hardcode `data_class=None` because there was no
    column to read: `product_prefix()` led the key with a class taken from
    the deployment-wide operational parameter tree, not from the unit.
    Migration 090 adds `work_units.data_class` and this reads it.

    A NULL is not a missing value to be defaulted away — it is the value
    that keeps old objects attributable. Every object written before 090
    carries the pre-data-class key grammar, and its key is immutable, so
    its unit's NULL is exactly what makes `canonical_prefix()` reconstruct
    that older shape (see its "THE COEXISTENCE CONTRACT"). Coalescing NULL
    to a token here — any token — would make every one of those objects
    unattributable at a stroke, which is the silent 100%-retention failure
    `pipeline/gc/references.py` documents. So the column is passed through
    untouched, NULL and all.
    """
    sql = """
    SELECT a.attempt_id,
           coalesce(lj.job_type, w.job_type) AS job_type,
           a.run_id,
           CASE WHEN w.job_type IS NULL OR w.input_scope IS NULL THEN NULL
                ELSE w.job_type || '/' || w.input_scope END AS unit_key,
           w.data_class
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
        attempt_id, job_type, run_id, unit_key, data_class = _row_values(
            row, 5)
        facts[attempt_id] = {"job_type": job_type, "run_id": run_id,
                             "unit_key": unit_key,
                             # NULL here is a REAL ANSWER, not a missing one
                             # — see the docstring. It selects the
                             # pre-data-class grammar, which is what every
                             # object written before 090 carries.
                             "data_class": data_class}
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
