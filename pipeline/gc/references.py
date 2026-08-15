"""Step 2 — the reference set, and the five-clause candidate rule.

**THE DECISIVE CONSTRAINT, WHICH GOVERNS THIS WHOLE MODULE.** Inside the
declared scope, whole classes of legitimately-published objects have NO
DATABASE REFERENCE AT ALL. An audit of every S3 write path in the products
bucket found, and this branch's own re-derivation confirmed:

  * the two NON-SELECTED difference-image variants — `add_diffimage` records
    exactly one uri (`pipeline/registration/products.py:301`), the role-bound
    one, and `:273` selects the registering image BY ROLE BINDING, so an
    attempt publishes three and one is registered. Published-but-unregistered
    is a NORMAL state, not orphan evidence;
  * all six SExtractor catalogs (zogy/sfft/naive x pos/neg) — no per-catalog
    column exists anywhere;
  * PSF catalogs, PSF-finder and PSF-parquet products;
  * the reference cov-map and uncertainty image — `refimages.filename` is one
    scalar for the reference image itself, not its companions.

`artifacts` was the table meant to record them, and it is NOT POPULATED ON THE
LIVE PATH: `production_registrar` (`pipeline/operator/registrar.py:18`) never
passes an `identity_repository`, the only call site that does is on the
dormant `JOB_TYPE_REGISTRATION` route, and `products.py:233` calls the
`identity_repository is None` branch "the pre-rollout path". An anti-join
keyed on `artifacts.uri` would therefore classify EVERY REAL PRODUCT as
unreferenced garbage.

**THEREFORE: ABSENCE FROM THE REFERENCE SET IS NOT SUFFICIENT EVIDENCE OF
GARBAGE, AND THIS MODULE DOES NOT TREAT IT AS SUCH.** The candidate rule is
POSITIVE IDENTIFICATION, not absence. All five clauses must hold; none is
optional; and `complete`/`cancelled` alone is necessary but NOT sufficient.

**THE REFERENCE SET WAS RE-DERIVED ON THIS BRANCH, NOT TAKEN ON TRUST.** The
brief's list is a floor. Enumerating every S3 write path in the declared scope
found three further classes it does not name, each recorded in
`notes-brief-h-evidence.md`:

  * HATS catalogs written by `aws s3 sync`
    (`pipeline/generateSourceHATSCatalog.py:237`,
    `generateLightCurveHATSCatalog.py:384`) to a STABLE SHARED PREFIX carrying
    no run or attempt id, which successive runs overwrite by design;
  * content-addressed configuration snapshots
    (`pipeline/runtime/termination.py:217`,
    `{prefix}/config-snapshots/sha256/{digest}.json`), where ONE OBJECT IS
    SHARED BY EVERY ARRAY CHILD of every attempt resolving to the same
    configuration — so attempt-scoped reasoning is actively wrong for it;
  * the `unidentified-attempt` degraded prefix (`pipeline/stages/context.py`)
    carrying no run or attempt identity at all.

Each is retained, by clause 3 (unattributable) and again by clause 0.
"""

import typing

#: Work-unit states whose objects are ELIGIBLE for deletion. The LITERAL
#: predicate, and deliberately short.
#:
#: **`failed` IS NOT HERE, DESPITE BEING TERMINAL IN THE STATE MACHINE.** The
#: `(FAILED, READY)` edge is live through the audited mutation API
#: (`pipeline/intent/writer.py:185`, implemented by rapid_systems migration
#: `040-scoped-retry-unit-transition.sql`) and carries NO AGE CUTOFF, so a
#: `failed` unit can be revived indefinitely — after GC deleted its objects.
#: `quarantined` is likewise excluded, and is called terminal elsewhere in
#: this codebase, which is exactly why the predicate is spelled out here
#: rather than expressed as "terminal".
ELIGIBLE_OWNER_STATES = ("complete", "cancelled")

#: Attempt lifecycle states that mean a unit is LIVE. The literal list from
#: `observability/attempts.py`.
#:
#: **`application_closed` IS LIVE FOR THIS PURPOSE.** The application has
#: finished but closure is still being reconciled; checking only
#: `submitted`/`started` would delete mid-reconciliation.
LIVE_ATTEMPT_STATES = ("submitted", "started", "application_closed")

#: The deletable-class allowlist. **OPT-IN, AND IT STARTS EMPTY.**
#:
#: PROVENANCE IS NOT DISPOSABILITY: canonical attribution proves only which
#: attempt wrote the bytes, never that the bytes are unwanted. Every class
#: named in this module's docstring as unreferenced-by-construction is
#: published under a canonical attempt prefix and WOULD satisfy every other
#: clause once its owner qualified. They are excluded HERE, positively, and
#: not by hoping a predicate misses them.
#:
#: A class joins this tuple only when a ratified proposal names it TOGETHER
#: WITH the durable reference surface that makes its absence meaningful.
#: Nothing has such a surface today.
DELETABLE_CLASS_ALLOWLIST = ()


class RetentionReason(object):
    """Why an object was retained. Each is a counted category on the plan."""

    OUT_OF_SCOPE = "out-of-scope"
    REFERENCED = "referenced"
    UNATTRIBUTABLE = "unattributable"
    NOT_ALLOWLISTED = "not-allowlisted"
    OWNER_NOT_DISCHARGED = "owner-not-discharged"
    LIVE_ATTEMPT = "live-attempt"
    OUTSTANDING_REGISTRATION = "outstanding-registration"
    NO_HORIZON = "no-horizon"
    HORIZON_NOT_ELAPSED = "horizon-not-elapsed"
    UNKNOWN_OWNER = "unknown-owner"


class PlanRefused(Exception):
    """The whole plan is refused; nothing is deleted in this run.

    Distinct from a per-object retention, and the distinction is the brief's:
    an UNREADABLE IN-SCOPE MANIFEST refuses the PLAN, not the object, because
    "its referenced objects" cannot be identified without reading it. Guessing
    which objects an unreadable manifest covers is exactly the guess this
    design refuses to make.
    """

    error_category = "gc_plan_refused"


class Candidate(typing.NamedTuple):
    """One object that passed all five clauses."""

    obj: object
    object_class: str
    attempt_id: int
    canonical_prefix: str


class Retained(typing.NamedTuple):
    """One object that did not, and why."""

    obj: object
    reason: str
    detail: str = ""


def canonical_prefix(job_type, run_id, unit_key, attempt_id,
                     data_class=None):
    """Reconstruct the authoritative prefix from an attempt's own facts.

    **THIS IS A HAND-MAINTAINED MIRROR OF `product_prefix()`**
    (`pipeline/stages/context.py`), which is the one place the grammar is
    actually built. The two are not derived from a shared function and
    nothing enforces their agreement at import time, so THEY MUST BE CHANGED
    TOGETHER. The failure mode of forgetting is the quiet one: a builder that
    has moved on and a mirror that has not attributes NOTHING, every object
    falls to clause 3 as unattributable, and GC retains 100% of the bucket
    without raising, logging an error, or failing a run. It fails safe and it
    fails silently, which is why the lockstep is asserted by test
    (`pipeline/contract/test_gc_eligibility.py`) rather than by comment.

    Two grammars, and BOTH are live:

      * `data_class` set — the current shape,
        ``{data_class}/{job_type}/{run_id}/{unit_key}/attempt-{id:010d}``;
      * `data_class` None — the shape objects written before the data class
        led the key,
        ``{job_type}/{run_id}/{unit_key}/attempt-{id:010d}``.

    **THE COEXISTENCE CONTRACT.** Objects already in the bucket were written
    under the old grammar and their keys are immutable — a key, once written,
    names those bytes forever, so they will never acquire a leading
    component. `None` therefore is not a degenerate case to be tidied away
    later: it is how those objects stay ATTRIBUTABLE. Dropping it would make
    every pre-existing object unattributable at a stroke, which is precisely
    the silent 100% retention described above. On the interim parameter path
    `attempt_facts()` yields `None` for every attempt, so today this branch
    is the only one production takes; the other exists for when the data
    class is carried per-unit.

    **POSITIVE ATTRIBUTION IS A CANONICAL ROUND TRIP, NOT A PARSE.** Parsing
    `attempt-N` out of a key and finding attempt N is NOT sufficient: it
    proves only that a number in a string matches a row. The prefix is
    RECONSTRUCTED from the attempt's own job type, run id, work-unit key and
    attempt id, and must be EXACTLY EQUAL to the inventory key's prefix.
    Malformed, legacy-layout, mismatched-run, mismatched-unit and
    foreign-prefix keys all fail that equality and are retained.
    """
    if job_type is None or run_id is None or unit_key is None \
            or attempt_id is None:
        return None
    prefix = "%s/%s/%s/attempt-%010d" % (job_type, run_id, unit_key,
                                         int(attempt_id))
    if data_class is None:
        return prefix
    return "%s/%s" % (data_class, prefix)


def attribute(obj, attempt_facts):
    """The attempt this object's key canonically belongs to, or None.

    `attempt_facts` maps attempt_id -> (job_type, run_id, unit_key,
    data_class). The round trip is the whole check: an object is attributed
    only when some attempt's RECONSTRUCTED prefix is exactly the key's
    prefix.

    A `data_class` of None reconstructs the pre-data-class grammar, which is
    what keeps objects written under it attributable — see
    `canonical_prefix`'s coexistence contract.

    Returns `(attempt_id, prefix)` or `None`.
    """
    for attempt_id, facts in attempt_facts.items():
        prefix = canonical_prefix(facts.get("job_type"), facts.get("run_id"),
                                  facts.get("unit_key"), attempt_id,
                                  facts.get("data_class"))
        if prefix and obj.key.startswith(prefix + "/"):
            return attempt_id, prefix
    return None


def is_fully_discharged(owner):
    """Is this attempt's owner FULLY DISCHARGED?

    The term is defined exactly, and deliberately does NOT mean the schema's
    supersession concept. `work_units.superseded_by_unit_id` DOES exist
    (`036-intent-schema-v1.sql:118`; the brief's claim that it does not was
    wrong — see P-H10) and this code deliberately never consults it: a unit
    can be superseded from ANY state, `ready` included, so supersession does
    not imply discharge and reading it here would delete live work's objects.
    All three must hold:

      * the owning work unit is `complete` or `cancelled` — the literal
        predicate, because `failed` and `quarantined` are called terminal
        elsewhere in this codebase and must not be read in here;

      * NO REGISTRATION WORK REMAINS OUTSTANDING: `registered_record_sequence`
        is NOT NULL and is `>=` `terminal_record_sequence`. An attempt whose
        watermark lags is STILL A LIVE REGISTRATION CANDIDATE
        (`pipeline/registration/consumer.py:150-160`) — its published objects
        legitimately have no artifact row YET, and the terminal record remains
        an operational registration source. **Deleting there is the sharpest
        false-positive path in this design**;

      * NO LIVE ATTEMPT exists for the same work unit, across all three of
        `submitted`, `started` and `application_closed`.

    Returns `(True, None)` or `(False, reason)`.
    """
    if owner is None:
        return False, RetentionReason.UNKNOWN_OWNER

    state = owner.get("unit_state")
    if state is None:
        return False, RetentionReason.UNKNOWN_OWNER
    if state not in ELIGIBLE_OWNER_STATES:
        return False, RetentionReason.OWNER_NOT_DISCHARGED

    registered = owner.get("registered_record_sequence")
    terminal = owner.get("terminal_record_sequence")
    if registered is None:
        return False, RetentionReason.OUTSTANDING_REGISTRATION
    if terminal is not None and registered < terminal:
        return False, RetentionReason.OUTSTANDING_REGISTRATION

    if owner.get("live_attempt_count"):
        return False, RetentionReason.LIVE_ATTEMPT

    return True, None


def classify(objects, *, references, attempt_facts, owners,
             allowlist=DELETABLE_CLASS_ALLOWLIST, class_of=None,
             horizon_elapsed=None, declared_buckets=(),
             declared_prefixes=()):
    """Apply the five-clause candidate rule to every object.

    Returns `(candidates, retained)`.

    The five clauses, none optional:

      0. THE DELETABLE-CLASS ALLOWLIST — the governing clause. The object's
         class must appear on an explicit allowlist of classes GC is permitted
         to delete. Anything not on it is retained and reported, HOWEVER WELL
         ATTRIBUTED. With the allowlist empty, this alone retains everything —
         which is a correct, conforming outcome for this package.
      1. inside the declared scope;
      2. absent from every reference surface;
      3. POSITIVELY ATTRIBUTED by canonical round-trip key validation;
      4. that attempt's owner is FULLY DISCHARGED;
      5. continuously absent for the configured horizon.

    The order below is not the numbering: scope and references are checked
    first because they are the cheapest, and the allowlist last-but-one so
    that a test can distinguish "retained by the allowlist" from "retained
    because something else also failed". Every retention records WHICH clause
    stopped it.
    """
    candidates, retained = [], []
    class_of = class_of or (lambda obj: "unknown")

    for obj in objects:
        # Clause 1 — scope. Checked first: an object outside the declared
        # scope is never a candidate, whatever else is true of it.
        if declared_buckets and obj.bucket not in declared_buckets:
            retained.append(Retained(obj, RetentionReason.OUT_OF_SCOPE,
                                     obj.bucket))
            continue
        if declared_prefixes and not any(obj.key.startswith(p)
                                         for p in declared_prefixes):
            retained.append(Retained(obj, RetentionReason.OUT_OF_SCOPE,
                                     obj.key))
            continue

        # Clause 2 — absence from EVERY reference surface.
        if obj.uri in references or obj.key in references:
            retained.append(Retained(obj, RetentionReason.REFERENCED))
            continue

        # Clause 3 — positive attribution by canonical round trip.
        attributed = attribute(obj, attempt_facts)
        if attributed is None:
            retained.append(Retained(obj, RetentionReason.UNATTRIBUTABLE,
                                     obj.key))
            continue
        attempt_id, prefix = attributed

        # Clause 4 — the owner is FULLY DISCHARGED.
        discharged, why = is_fully_discharged(owners.get(attempt_id))
        if not discharged:
            retained.append(Retained(obj, why, "attempt %s" % attempt_id))
            continue

        # Clause 5 — the horizon. Continuous absence, judged by the caller
        # across BOTH passes; `None` means no horizon is configured, which
        # fails closed.
        if horizon_elapsed is None:
            retained.append(Retained(obj, RetentionReason.NO_HORIZON))
            continue
        if not horizon_elapsed(obj):
            retained.append(Retained(obj,
                                     RetentionReason.HORIZON_NOT_ELAPSED))
            continue

        # Clause 0 — THE ALLOWLIST GOVERNS. Checked last so that a retention
        # here is unambiguous: everything else about this object qualified,
        # and its class is what retains it.
        object_class = class_of(obj)
        if object_class not in allowlist:
            retained.append(Retained(obj, RetentionReason.NOT_ALLOWLISTED,
                                     object_class))
            continue

        candidates.append(Candidate(obj, object_class, attempt_id, prefix))

    return candidates, retained


def counted(retained):
    """Retention counts by category, for the plan's `retained_counts`.

    Retention is REPORTED, never silent. "Absence of a reference is not
    evidence of garbage when nothing enumerates that class of object at all",
    so every retained object lands in a named, counted bucket an operator can
    read.
    """
    counts = {}
    for entry in retained:
        counts[entry.reason] = counts.get(entry.reason, 0) + 1
    return counts
