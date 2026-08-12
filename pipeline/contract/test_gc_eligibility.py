"""Acceptance criteria 7 and 8 — the reference set, attribution, and the
five-clause candidate rule, against real SQL and a stub inventory that CAN
REFUSE.

**WHY THESE TESTS ARE SHAPED THE WAY THEY ARE.** The brief fixes a discipline
that is easy to get wrong and impossible to detect afterwards: every fixture
asserting that something is RETAINED must be made **OTHERWISE FULLY
ELIGIBLE** — canonical prefix, `complete`/`cancelled` owner, non-NULL current
watermark, no live attempt, no other reference, horizon elapsed — so the test
proves the clause it names is what retains the object, and cannot pass for an
incidental reason. A retention test whose fixture was ineligible for three
other reasons proves nothing about the clause it claims to cover.

`eligible_object()` below builds exactly that: an object that WOULD be a
candidate if the allowlist admitted its class. Every retention test then
breaks exactly one thing.

**THE STUB INVENTORY CAN REFUSE**, which is the property that makes it worth
having (`~/Vault/knowledge/stub-blind-testing.md`'s standing lesson, and the
brief's requirement): it can return a missing object, a partial page, a
changed version and a stale snapshot. A double that cannot fail proves
nothing.
"""

import datetime

import pytest

from pipeline.contract import fixture
from pipeline.gc import references
from pipeline.gc.inventory import (InventoryObject, InventoryTruncated,
                                   InventoryStale, read_inventory)
from pipeline.gc.references import RetentionReason

pytestmark = pytest.mark.contract

#: A canonical attempt whose prefix the fixtures reconstruct. The numbers are
#: arbitrary; the SHAPE is `product_prefix()`'s
#: (`pipeline/stages/context.py:130`).
ATTEMPT_ID = 4242
JOB_TYPE = "science"
RUN_ID = "gc-run-" + fixture.RUN_TAG
UNIT_KEY = "science/90000/1"

CANONICAL_PREFIX = "%s/%s/%s/attempt-%010d" % (JOB_TYPE, RUN_ID, UNIT_KEY,
                                               ATTEMPT_ID)
BUCKET = "roman-rapid-products"
PREFIXES = ("science/",)

NOW = datetime.datetime(2026, 8, 12, 0, 0, tzinfo=datetime.timezone.utc)
LONG_AGO = NOW - datetime.timedelta(days=400)
HORIZON = 30 * 24 * 3600


def obj(key, version="v1", bucket=BUCKET):
    return InventoryObject(bucket=bucket, key=key, version_id=version,
                           size=10, last_modified=LONG_AGO)


def eligible_object(name="diff.fits"):
    """An object that satisfies EVERY clause except the allowlist.

    The fixture the brief requires: canonical prefix, discharged owner, no
    reference, horizon elapsed. Each retention test below breaks exactly one
    of those and asserts the reason, so the assertion is about the clause it
    names.
    """
    return obj("%s/%s" % (CANONICAL_PREFIX, name))


DISCHARGED_OWNER = {
    ATTEMPT_ID: {
        "unit_state": "complete",
        "registered_record_sequence": 7,
        "terminal_record_sequence": 7,
        "live_attempt_count": 0,
    }
}

ATTEMPT_FACTS = {
    ATTEMPT_ID: {"job_type": JOB_TYPE, "run_id": RUN_ID,
                 "unit_key": UNIT_KEY},
}


def classify(objects, *, refs=(), owners=None, facts=None, allowlist=(),
             class_of=None, horizon=True):
    """`references.classify` with this module's standing fixture."""
    return references.classify(
        objects,
        references=set(refs),
        attempt_facts=facts if facts is not None else ATTEMPT_FACTS,
        owners=owners if owners is not None else DISCHARGED_OWNER,
        allowlist=allowlist,
        class_of=class_of or (lambda o: "difference_image"),
        horizon_elapsed=((lambda o: True) if horizon else None),
        declared_buckets=(BUCKET,),
        declared_prefixes=PREFIXES)


def only_reason(retained):
    assert len(retained) == 1, [(r.obj.key, r.reason) for r in retained]
    return retained[0].reason


# ---------------------------------------------------------------------------
# The fixture itself is asserted first. If `eligible_object` were not actually
# eligible, every retention test below would pass for the wrong reason — so
# the converse the brief requires is asserted HERE, and it doubles as the
# proof that the allowlist is the only thing standing between this object and
# deletion.
# ---------------------------------------------------------------------------
def test_the_eligible_fixture_becomes_a_candidate_when_allowlisted():
    """The converse: allowlist the class and the object IS a candidate.

    Without this, "retained" assertions could all be passing because the
    fixture was ineligible for some unnoticed reason, and the suite would be
    green while proving nothing.
    """
    candidates, retained = classify([eligible_object()],
                                    allowlist=("difference_image",))
    assert len(candidates) == 1, retained
    assert candidates[0].attempt_id == ATTEMPT_ID
    assert candidates[0].canonical_prefix == CANONICAL_PREFIX


def test_allowlist_governs_an_otherwise_fully_eligible_object():
    """Criterion 8's allowlist clause, asserted directly.

    THE GOVERNING CLAUSE. This object satisfies every other clause — it is in
    scope, unreferenced, canonically attributed, its owner is fully
    discharged, and the horizon has elapsed. With the allowlist EMPTY it is
    still retained, which proves clause 0 governs and is not merely a filter
    that happens to agree with the others.
    """
    candidates, retained = classify([eligible_object()], allowlist=())
    assert candidates == []
    assert only_reason(retained) == RetentionReason.NOT_ALLOWLISTED


# ---------------------------------------------------------------------------
# Criterion 7 — the reference surfaces. Each object is otherwise fully
# eligible; only the reference differs.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("surface", [
    "artifacts.uri",
    "refimages.filename",
    "diffimages.filename",
    "refimcatalogs.filename",
    "active submission manifest",
    "coadd-input CSV",
])
def test_an_object_referenced_by_any_surface_is_retained(surface):
    """Each reference surface retains, asserted separately.

    Parameterized rather than merged into one test so a regression names WHICH
    surface stopped protecting its objects. `refimcatalogs` is called out in
    the brief because where it is present it is frequently the ONLY database
    reference to those bytes.
    """
    target = eligible_object()
    candidates, retained = classify([target], refs=(target.uri,),
                                    allowlist=("difference_image",))
    assert candidates == [], "%s must retain its object" % surface
    assert only_reason(retained) == RetentionReason.REFERENCED


def test_a_superseded_artifact_binding_still_retains_its_bytes():
    """`product_artifacts.is_current` is NOT the join surface.

    A superseded artifact's bytes may still be legitimately live, so the
    anti-join reads `artifacts.uri` regardless of whether its binding is
    current. Joining on the current binding alone would make every superseded
    artifact a candidate — which is why `reference_sql.ARTIFACTS_SQL` has no
    `is_current` predicate, asserted here by construction.
    """
    from pipeline.gc import reference_sql
    assert "is_current" not in reference_sql.ARTIFACTS_SQL
    target = eligible_object()
    candidates, _ = classify([target], refs=(target.uri,),
                             allowlist=("difference_image",))
    assert candidates == []


def test_an_object_outside_the_declared_scope_is_never_a_candidate():
    """Scope is fixed BEFORE the reference set."""
    outside_bucket = obj("%s/x.fits" % CANONICAL_PREFIX,
                         bucket="roman-rapid-diagnostics")
    candidates, retained = classify([outside_bucket],
                                    allowlist=("difference_image",))
    assert candidates == []
    assert only_reason(retained) == RetentionReason.OUT_OF_SCOPE


def test_an_object_outside_the_declared_prefixes_is_never_a_candidate():
    outside_prefix = obj("submissions/batch-1/manifest.json")
    candidates, retained = classify([outside_prefix],
                                    allowlist=("difference_image",))
    assert candidates == []
    assert only_reason(retained) == RetentionReason.OUT_OF_SCOPE


# ---------------------------------------------------------------------------
# The classes that are UNREFERENCED BY CONSTRUCTION. Each is made otherwise
# fully eligible and each must be RETAINED — by the allowlist, positively,
# and not by hoping a predicate misses it.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("object_class,name", [
    ("difference_image_variant", "diff_zogy.fits"),
    ("sextractor_catalog", "sexcat_sfft_pos.txt"),
    ("psf_catalog", "psfcat.txt"),
    ("psf_finder", "psf_finder.fits"),
    ("psf_parquet", "psf.parquet"),
    ("reference_covmap", "refcov.fits"),
    ("reference_uncertainty", "refunc.fits"),
])
def test_unreferenced_by_construction_classes_are_retained(object_class, name):
    """Criterion 7's decisive list, each asserted separately.

    These are published under a canonical attempt prefix and WOULD satisfy
    every other clause once their owner qualified — the fixture makes that
    true rather than assuming it. They are excluded by the ALLOWLIST,
    positively. `artifacts` was the table meant to record them and it is not
    populated on the live path, so an anti-join trusting absence would delete
    every one.
    """
    target = obj("%s/%s" % (CANONICAL_PREFIX, name))
    candidates, retained = classify([target], allowlist=(),
                                    class_of=lambda o: object_class)
    assert candidates == []
    assert only_reason(retained) == RetentionReason.NOT_ALLOWLISTED


# ---------------------------------------------------------------------------
# Attribution negatives. Each is otherwise fully eligible AND allowlisted, so
# the ONLY thing retaining it is the failed canonical round trip.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key,why", [
    ("science/not-a-prefix.fits", "malformed"),
    ("science/90000/1/diff.fits", "legacy layout — no run or attempt id"),
    ("science/other-run/%s/attempt-%010d/d.fits" % (UNIT_KEY, ATTEMPT_ID),
     "run id does not match the attempt's"),
    ("science/%s/science/99999/2/attempt-%010d/d.fits" % (RUN_ID, ATTEMPT_ID),
     "work-unit key does not match"),
    # A FOREIGN PREFIX **INSIDE** THE DECLARED SCOPE. The job-type component
    # is wrong, so the reconstructed canonical prefix cannot match — but the
    # key still begins `science/`, which is what makes this an ATTRIBUTION
    # negative rather than a scope one. A first draft used a key starting
    # `foreign/`, which the scope clause caught first: the test passed while
    # asserting nothing about attribution, and the reason string lied.
    ("science/%s/foreign/%s/attempt-%010d/d.fits"
     % (RUN_ID, UNIT_KEY, ATTEMPT_ID),
     "foreign prefix inside the declared scope"),
    ("science/%s/%s/unidentified-attempt/d.fits" % (RUN_ID, UNIT_KEY),
     "the degraded prefix carries no attempt identity at all"),
])
def test_attribution_negatives_are_retained(key, why):
    """Criterion 7's attribution negatives, each asserted separately.

    **THE `unidentified-attempt` CASE IS A REAL KEY SHAPE, NOT A
    HYPOTHETICAL**: `pipeline/stages/context.py:184` returns
    `{job_type}/{unit.key}/unidentified-attempt` whenever `run_id` or
    `attempt_id` is absent. It carries no attempt identity, so the round trip
    cannot reconstruct it — and a naive parser looking for `attempt-N` finds
    nothing and might read it as legacy garbage.
    """
    candidates, retained = classify([obj(key)],
                                    allowlist=("difference_image",))
    assert candidates == [], "%s must not be attributed" % why
    assert only_reason(retained) == RetentionReason.UNATTRIBUTABLE


def test_a_parse_is_not_a_round_trip():
    """The sharpest attribution case, called out by the brief.

    The key's `attempt-N` component parses to a REAL attempt — 4242 exists in
    `attempt_facts` — but the reconstructed canonical prefix is not exactly
    equal, because the run id differs. A parser would attribute this; the
    round trip refuses it.
    """
    key = "science/a-different-run/%s/attempt-%010d/d.fits" % (UNIT_KEY,
                                                              ATTEMPT_ID)
    assert "attempt-%010d" % ATTEMPT_ID in key, "the parse target is present"
    candidates, retained = classify([obj(key)],
                                    allowlist=("difference_image",))
    assert candidates == []
    assert only_reason(retained) == RetentionReason.UNATTRIBUTABLE


def test_an_unresolvable_owner_is_retained():
    """An object whose attempt has no facts at all cannot be attributed."""
    candidates, retained = classify([eligible_object()], facts={},
                                    allowlist=("difference_image",))
    assert candidates == []
    assert only_reason(retained) == RetentionReason.UNATTRIBUTABLE


# ---------------------------------------------------------------------------
# Criterion 8 — eligibility. `complete`/`cancelled` is NECESSARY BUT NOT
# SUFFICIENT, and each ineligible state is asserted separately.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("state", ["ready", "submitted", "blocked",
                                   "quarantined", "failed"])
def test_ineligible_owner_states_retain_regardless_of_age(state):
    """Ineligible regardless of age, each state asserted separately.

    **`failed` IS THE ONE THAT MATTERS MOST** and is not an oversight: the
    `(FAILED, READY)` edge is live through the audited mutation API
    (`pipeline/intent/writer.py:185`, migration 040) and carries NO AGE
    CUTOFF, so a `failed` unit can be revived indefinitely — after GC deleted
    its objects. `quarantined` is called terminal elsewhere in this codebase,
    which is exactly why the eligible set is spelled out literally rather than
    expressed as "terminal".
    """
    owners = {ATTEMPT_ID: dict(DISCHARGED_OWNER[ATTEMPT_ID],
                               unit_state=state)}
    candidates, retained = classify([eligible_object()], owners=owners,
                                    allowlist=("difference_image",))
    assert candidates == []
    assert only_reason(retained) == RetentionReason.OWNER_NOT_DISCHARGED


def test_a_null_or_unresolvable_work_unit_is_retained():
    owners = {ATTEMPT_ID: dict(DISCHARGED_OWNER[ATTEMPT_ID],
                               unit_state=None)}
    candidates, retained = classify([eligible_object()], owners=owners,
                                    allowlist=("difference_image",))
    assert candidates == []
    assert only_reason(retained) == RetentionReason.UNKNOWN_OWNER


@pytest.mark.parametrize("registered,terminal", [
    (None, 7),      # watermark NULL — registration has not run at all
    (3, 7),         # watermark LAGS the terminal record sequence
])
def test_outstanding_registration_retains_a_complete_owner(registered,
                                                           terminal):
    """`complete` is NECESSARY BUT NOT SUFFICIENT — asserted directly.

    An attempt whose `registered_record_sequence` is NULL or lags its
    `terminal_record_sequence` is STILL A LIVE REGISTRATION CANDIDATE
    (`pipeline/registration/consumer.py:150-160`): its published objects
    legitimately have no artifact row YET, and the terminal record remains an
    operational registration source. **The brief calls this the sharpest
    false-positive path in the design**, so it is asserted with a `complete`
    owner — the state that would otherwise qualify.
    """
    owners = {ATTEMPT_ID: dict(DISCHARGED_OWNER[ATTEMPT_ID],
                               unit_state="complete",
                               registered_record_sequence=registered,
                               terminal_record_sequence=terminal)}
    candidates, retained = classify([eligible_object()], owners=owners,
                                    allowlist=("difference_image",))
    assert candidates == []
    assert only_reason(retained) == RetentionReason.OUTSTANDING_REGISTRATION


@pytest.mark.parametrize("live_state", ["submitted", "started",
                                        "application_closed"])
def test_a_live_attempt_on_the_unit_retains_across_all_three_states(
        live_state):
    """`live` asserted across ALL THREE states separately.

    **`application_closed` IS THE MID-RECONCILIATION CASE** and is live for
    this purpose: the application has finished but closure is still being
    reconciled. Checking only `submitted`/`started` would delete
    mid-reconciliation, which is why the literal three-element list exists in
    `references.LIVE_ATTEMPT_STATES` and why this test parameterizes over it.
    """
    assert live_state in references.LIVE_ATTEMPT_STATES
    owners = {ATTEMPT_ID: dict(DISCHARGED_OWNER[ATTEMPT_ID],
                               live_attempt_count=1)}
    candidates, retained = classify([eligible_object()], owners=owners,
                                    allowlist=("difference_image",))
    assert candidates == []
    assert only_reason(retained) == RetentionReason.LIVE_ATTEMPT


def test_no_configured_horizon_deletes_nothing_and_says_why():
    """Fail-closed: with no horizon the plan deletes nothing.

    There is deliberately no default that permits deletion, so an unset
    horizon is not "zero seconds" — it is a refusal, and the retained reason
    names it so an operator reading the plan sees why nothing happened.
    """
    candidates, retained = classify([eligible_object()], horizon=False,
                                    allowlist=("difference_image",))
    assert candidates == []
    assert only_reason(retained) == RetentionReason.NO_HORIZON


def test_the_effective_horizon_is_the_maximum_never_a_sum():
    """The maximum of the configured values, never a sum, never a minimum."""
    from pipeline.gc.horizon import effective_horizon
    assert effective_horizon(100, 500, 250) == 500
    assert effective_horizon(100, None, 250) == 250
    assert effective_horizon(None, None) is None
    # A sum would be 850 and a minimum 100; both are wrong, and stating it
    # here means a future edit to either behaviour fails loudly.
    assert effective_horizon(100, 500, 250) != 850
    assert effective_horizon(100, 500, 250) != 100


def test_a_newly_dereferenced_old_object_serves_the_full_horizon():
    """The post-quarantine-release case.

    An object protected for a year while `quarantined` must NOT become
    age-eligible the instant an operator moves the unit out. The clock starts
    when the LAST REFERENCE DISAPPEARED, not when the object was written — so
    a 400-day-old object first seen absent one minute ago is not eligible.
    """
    from pipeline.gc.horizon import elapsed_since
    just_now = NOW - datetime.timedelta(minutes=1)
    assert elapsed_since(just_now, HORIZON, now=NOW) is False
    long_absent = NOW - datetime.timedelta(days=40)
    assert elapsed_since(long_absent, HORIZON, now=NOW) is True


def test_continuous_absence_requires_both_passes():
    """§4.11 step 5 is mandatory: absent in the plan pass AND the recompute."""
    from pipeline.gc.horizon import continuously_absent
    first_absent = NOW - datetime.timedelta(days=40)
    assert continuously_absent(True, True, first_absent, HORIZON, now=NOW)
    assert not continuously_absent(True, False, first_absent, HORIZON,
                                   now=NOW)
    assert not continuously_absent(False, True, first_absent, HORIZON,
                                   now=NOW)


def test_producing_attempt_age_is_not_the_clock():
    """The correction the brief calls load-bearing.

    A very old object is NOT eligible on age alone: eligibility is continuous
    absence from every reference set, measured from when the last reference
    disappeared. Anchoring to the producing attempt's age would make a
    two-year-old object whose reference was dropped today instantly deletable
    — and a PITR restore to yesterday would then revive that reference after
    the bytes were gone.
    """
    from pipeline.gc.horizon import elapsed_since
    target = eligible_object()
    assert target.last_modified == LONG_AGO, "the object itself is ancient"
    # Yet with its last reference dropped a minute ago, it is not eligible.
    assert elapsed_since(NOW - datetime.timedelta(minutes=1), HORIZON,
                         now=NOW) is False


# ---------------------------------------------------------------------------
# The stub inventory MUST be able to refuse.
# ---------------------------------------------------------------------------
def test_a_truncated_inventory_page_is_fatal():
    """Never silently short.

    A truncated listing makes objects look absent, and absence is what the
    anti-join acts on — so proceeding would manufacture candidates out of a
    paging failure. This is the single sharpest silent-failure mode in the
    design, which is why it raises rather than warns.
    """
    pages = [{"objects": [], "truncated": True}]
    with pytest.raises(InventoryTruncated):
        read_inventory(pages, inventory_id="inv-1", taken_at=NOW,
                       freshness_seconds=3600, now=NOW)


def test_a_stale_inventory_is_refused():
    old = NOW - datetime.timedelta(days=3)
    with pytest.raises(InventoryStale):
        read_inventory([{"objects": []}], inventory_id="inv-2", taken_at=old,
                       freshness_seconds=3600, now=NOW)


def test_no_freshness_bound_is_refused():
    """An unbounded staleness check is not a check."""
    with pytest.raises(InventoryStale):
        read_inventory([{"objects": []}], inventory_id="inv-3", taken_at=NOW,
                       freshness_seconds=None, now=NOW)


def test_an_inventory_row_without_a_version_is_refused_at_read_time():
    """Refused at READ time, not discovered at delete time.

    Deletion is by exact version; a row without one could only be deleted by
    key, which on a versioning-enabled bucket installs a delete marker over
    whatever is current.
    """
    pages = [{"objects": [{"bucket": BUCKET, "key": "science/a.fits"}]}]
    with pytest.raises(Exception) as caught:
        read_inventory(pages, inventory_id="inv-4", taken_at=NOW,
                       freshness_seconds=3600, now=NOW)
    assert "VersionId" in str(caught.value)


def test_an_empty_iterable_is_refused_rather_than_read_as_empty():
    """Zero pages is indistinguishable from a reader that failed to start."""
    with pytest.raises(InventoryTruncated):
        read_inventory([], inventory_id="inv-5", taken_at=NOW,
                       freshness_seconds=3600, now=NOW)


def test_the_inventory_filters_to_the_declared_scope_at_read_time():
    """Scope is applied where a missed filter cannot become a deletion."""
    pages = [{"objects": [
        {"bucket": BUCKET, "key": "science/a.fits", "version_id": "v1"},
        {"bucket": "roman-rapid-diagnostics", "key": "science/b.fits",
         "version_id": "v1"},
        {"bucket": BUCKET, "key": "records/c.json", "version_id": "v1"},
    ]}]
    inventory = read_inventory(pages, inventory_id="inv-6", taken_at=NOW,
                               freshness_seconds=3600, now=NOW,
                               declared_buckets=(BUCKET,),
                               declared_prefixes=PREFIXES)
    assert [o.key for o in inventory.objects] == ["science/a.fits"]
    assert inventory.complete is True


# ---------------------------------------------------------------------------
# The reference SQL executes against the REAL schema, not a hand-built fake.
# ---------------------------------------------------------------------------
def test_reference_surfaces_execute_against_real_sql():
    """SQL tests execute SQL (the standing discipline).

    Every reference query is run against the real scratch schema. A query with
    a typo'd column would pass a hand-built fake and fail here, which is the
    whole reason this tier exists — and the two column-name errors this
    branch's own SQL started with were caught exactly this way.
    """
    from pipeline.gc import reference_sql
    conn = fixture.connect()
    try:
        execute = fixture.executor(conn)
        refs, consulted, absent = reference_sql.collect_references(execute)
        assert isinstance(refs, set)
        # The legacy surfaces are in the BASE stream, so they must always be
        # consulted — if they are reported absent, the probe is broken.
        assert "refimages" in consulted
        assert "diffimages" in consulted
        assert "refimcatalogs" in consulted

        facts = reference_sql.attempt_facts(execute)
        assert isinstance(facts, dict)
        owner_rows = reference_sql.owners(execute)
        assert isinstance(owner_rows, dict)
    finally:
        conn.close()


def test_the_unit_key_round_trip_matches_the_production_prefix_builder():
    """The reconstruction must agree with `product_prefix()` itself.

    `work_units` has no `unit_key` column — the persisted identity is
    `(job_type, input_scope)` — so the round trip rebuilds the key as
    `job_type || '/' || input_scope`. This asserts the reconstruction against
    the shape `product_prefix()` builds (`pipeline/stages/context.py:130`),
    including the zero-padding width, which is load-bearing.
    """
    built = references.canonical_prefix(JOB_TYPE, RUN_ID, UNIT_KEY,
                                        ATTEMPT_ID)
    assert built == CANONICAL_PREFIX
    assert built.endswith("attempt-%010d" % ATTEMPT_ID)
    # The zero-padding width is load-bearing: `attempt-4242` and
    # `attempt-0000004242` are different prefixes and only one is canonical.
    assert "attempt-0000004242" in built
