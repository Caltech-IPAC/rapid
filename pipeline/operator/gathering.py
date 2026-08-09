"""The operator's ready-work queries, and the helpers they need.

WHY THESE HELPERS LIVE HERE. `mjd_window`, `min_images_to_coadd` and
`active_definition` were functions in `pipeline/virtualPipelineOperator.py`
— and that module cannot be imported. It is a script: its top level reads
`sys.argv[1]`, prints diagnostics, and calls `exit(64)` when its
environment variables are absent. Importing it to borrow one function
therefore RUNS the old operator's startup, which is not a hypothetical —
it was found live on rapid-admin (2026-08-08): the first live rehearsal
resolved its credential, announced REHEARSAL MODE, then died with

    datearg = --start
    *** Error: Env. var. STARTDATETIME not set; quitting...
    exit 64

because the import at gather time re-read the new operator's own argv,
took `--start` as the legacy positional processing date, and demanded the
environment interface this restructure exists to retire.

That is exactly the coupling the service shape is supposed to break, so
the helpers move here rather than being imported across it. They are
small, self-contained, and their reasoning is preserved verbatim below.
The legacy module has since been retired (IR-2); these are now the only
copies.

THE GATHERER REGISTRY (integration review 2026-08, composite ruling 1).
`gatherer_for` used to be a two-branch class conditional — reference
construction, or else science — which is exactly the shape the ADOPTED
operations text rules out: "adding a job type is a registry entry, not a
branch in a class conditional." The registry below is THE enumeration for
every job type this operator gathers; there is no residual if/else on
class or job type anywhere in this module.

Registering a job type here is what makes it actually gather, accumulate
and submit through the live path — before this ruling, `submission.gathering`
already implemented all seven of the post-DB-chain-and-alert-production
gatherers (`gather_catalog_load_units` through `gather_alert_production_units`),
called only by probes and tests, because nothing wired them into
`gatherer_for`. That is the headline defect the ruling names: "the
deployed operator gathers only science and reference construction, so the
system cannot emit alerts or run the catalog chain in continuous
operation." This registry is what closes it.

**THE REGISTRY KEY IS NOT ALWAYS THE ROUTE JOB TYPE (IR-13-a, the
campaign-unit gatherer).** Every row before this build had one job type
serving BOTH roles at once: the key `gatherer_for` looks `_BY_JOB_TYPE` up
by, AND the job type `OperationalClass.route` resolves through
`submission.routes.route_for`. The campaign gatherer breaks that
coincidence on purpose — a test-class campaign gathers under the SCIENCE
route (`OperationalClass.job_type` must literally be
`submission.routes.JOB_TYPE_SCIENCE`, or `LiveSubmitter.submit`'s
`route.job_type` would submit under the wrong queue/definition/manifest
job_type entirely) while needing a REGISTRY KEY distinct from
`JOB_TYPE_SCIENCE`, because `_BY_JOB_TYPE` is a plain
`{job_type: (class_name, gather)}` dict — a second row keyed literally
`JOB_TYPE_SCIENCE` would silently overwrite (or be shadowed by, depending
on declaration order) `PROMPT_PROCESSING`'s own science row, and whichever
lost would gather nothing every pass with no error at all. Each `REGISTRY`
row is therefore `(registry_key, operational_class_name, gather,
route_job_type)`: `registry_key` is what `_BY_JOB_TYPE`/`gatherer_for` key
on and what `_classes_for_pass` (service.py) sets `OperationalClass.name`
to for the fanned-out instance; `route_job_type` is what that instance's
`.job_type` becomes, and therefore what `.route` resolves and what gets
submitted. For every row before this one, `route_job_type` IS the
registry key (the coincidence every other job type still has) — see
`_route_job_type` below, which defaults it so no existing row's four-tuple
needs spelling out explicitly.
"""

import datetime
import logging

from pipeline.operator import classes as opclasses
from submission import gathering
from submission.routes import (JOB_TYPE_ALERT_PRODUCTION,
                               JOB_TYPE_CATALOG_LOAD, JOB_TYPE_CROSSMATCH,
                               JOB_TYPE_MERGE_CURRENCY, JOB_TYPE_MERGE_DEDUP,
                               JOB_TYPE_REFERENCE_IMAGE, JOB_TYPE_SCIENCE,
                               JOB_TYPE_SOURCE_CURRENCY, JOB_TYPE_STATISTICS)

logger = logging.getLogger("rapid.operator.gathering")

#: The campaign gatherer's REGISTRY KEY (IR-13-a) — deliberately NOT
#: `submission.routes.JOB_TYPE_SCIENCE` and deliberately not a member of
#: that module's route vocabulary at all: it is never submitted, never
#: appears in a `Manifest.job_type`, and is never checked against
#: `submission.routes.ROUTES` — it exists ONLY as the dict key
#: `_BY_JOB_TYPE`/`gatherer_for` look up by and the `OperationalClass.name`
#: a fanned-out `TEST`-class operator carries (accumulator naming, logging,
#: run-id prefixing — see `_classes_for_pass`'s own docstring for what
#: `.name` distinguishes). See this module's header for why a distinct key
#: is required at all.
CAMPAIGN_GATHERING_KEY = "test-campaign-science"


def mjd_window(start, end):
    """(start_mjdobs, end_mjdobs) for the operator's window.

    The readiness query selects (field, filter) pairs by mjdobs while the
    L2 file selection is by timestamp. Both describe the SAME window, so
    it is converted here rather than accepted as two more values that
    nothing keeps equal.

    Returned as built-in floats, NOT the `numpy.float64` astropy hands
    back. These two values are bound straight into the readiness query,
    and psycopg2 has no adapter for a numpy scalar, so it falls back to
    repr() — which under NumPy 2 is `np.float64(61679.0)` rather than
    `61679.0`. That is pasted into the SQL as a schema-qualified name and
    Postgres rejects it with `schema "np" does not exist`, aborting the
    transaction so every later query in the pass is skipped too. The
    failure is silent in the worst way: the gathering helper catches it,
    prints, and returns None, so gathering reports "0 (field, filter)
    pairs" — indistinguishable from a night with no data.

    Takes datetimes OR strings. The legacy version took only the
    operator's STARTDATETIME/ENDDATETIME strings; this operator holds
    real datetimes, and formatting them back into a string to reparse
    them would be a third representation of one window.
    """
    from astropy.time import Time

    def as_isot(value):
        if hasattr(value, "isoformat"):
            # Drop the offset: astropy's isot format rejects it, and the
            # window is already normalised to UTC by `inputs`.
            text = value.replace(tzinfo=None).isoformat()
        else:
            text = str(value).replace(" ", "T")
        return text

    return (float(Time(as_isot(start), format='isot', scale='utc').mjd),
            float(Time(as_isot(end), format='isot', scale='utc').mjd))


def min_images_to_coadd():
    """The release's minimum coadd depth.

    Read from release CONTENT (cdf/science/pipeline.toml), which is the
    home the W4 re-homing gave it — not from the master .ini, whose copy
    is the duplicate that re-homing was undoing.
    """
    from pipeline.runtime import science_config

    science = science_config.load()
    return int(science_config.value(science, "ref_image",
                                    "min_n_images_to_coadd"))


def processing_date_for(operator_input):
    """The `yyyymmdd` processing date one pass's post-DB-chain gatherers act on.

    **JUDGMENT CALL, RECORDED HERE RATHER THAN GUESSED SILENTLY.** The
    post-DB chain and alert production are gathered per PROCESSING DATE
    (`submission.gathering.gather_catalog_load_units` et al. all take
    `proc_date: str`), but the operator's own input is a WINDOW
    (`start`/`end` datetimes, `pipeline/operator/inputs.py`) — the co-design
    that built the window input did not anticipate date-keyed gathering,
    and no evidence in the design texts or the run's artifacts states which
    end of the window is "the" processing date for a pass.

    This resolves it as `operator_input.end`'s UTC calendar date: the day
    the window closes on is the day whose science-chain arrivals a pass is
    acting on completing, which matches the ADOPTED ordering statement
    ("catalog load per processing date and SCA... gathered after catalog
    load completes" — the chain follows the day's ingest, not the day
    diffing started). Stated as one named function so a different rule is
    a one-line change, not a re-derivation at every call site.
    """
    return operator_input.end.astimezone(datetime.timezone.utc).strftime(
        "%Y%m%d")


def _release_identity():
    """The release identity alert-production gathering scopes emission by.

    Read from `RAPID_RELEASE_IDENTITY`, the same environment fact
    `pipeline.operator.submission.submission_env` resolves the execution
    binding's `release_identity` from (round-5 finding: "the rest stay in
    the ENVIRONMENT, because they are deployment facts that change with
    every image build"). One home: gathering and the binding it submits
    under must name the same release, or a unit could be gathered as
    outstanding under one release and recorded as emitted under another.
    """
    import os

    value = os.environ.get("RAPID_RELEASE_IDENTITY")
    if not value:
        raise RuntimeError(
            "alert-production gathering needs RAPID_RELEASE_IDENTITY; it "
            "is not set. This is the same environment fact the submission "
            "binding resolves its own release_identity from — see "
            "pipeline.operator.submission.submission_env.")
    return value


def _science_gatherer(operator_input, start_mjdobs, end_mjdobs, handle,
                      parameters, s3_client):
    # `parameters`/`s3_client` unused, taken anyway: every REGISTRY row's
    # gather function carries the identical six-argument union shape the
    # registry docstring states, so the dispatcher never branches. This
    # row was the ONE that didn't — a TypeError at first prompt-class
    # gather, found live at the mock's enablement.
    return list(gathering.gather_science_units(
        handle, operator_input.start, operator_input.end,
        start_mjdobs=start_mjdobs, end_mjdobs=end_mjdobs,
        min_images_to_coadd=min_images_to_coadd()))


def _reference_gatherer(operator_input, start_mjdobs, end_mjdobs, handle,
                        parameters, s3_client):
    return list(gathering.gather_reference_units(
        handle, operator_input.start, operator_input.end,
        start_mjdobs=start_mjdobs, end_mjdobs=end_mjdobs,
        min_images_to_coadd=min_images_to_coadd(),
        s3_client=s3_client,
        job_bucket=parameters["s3/products-bucket"],
        run_id=None))


def _catalog_load_gatherer(operator_input, start_mjdobs, end_mjdobs, handle,
                           parameters, s3_client):
    return list(gathering.gather_catalog_load_units(
        handle, processing_date_for(operator_input)))


def _crossmatch_gatherer(operator_input, start_mjdobs, end_mjdobs, handle,
                         parameters, s3_client):
    return list(gathering.gather_crossmatch_units(
        handle, processing_date_for(operator_input)))


def _statistics_gatherer(operator_input, start_mjdobs, end_mjdobs, handle,
                         parameters, s3_client):
    return list(gathering.gather_statistics_units(handle))


def _merge_currency_gatherer(operator_input, start_mjdobs, end_mjdobs,
                             handle, parameters, s3_client):
    return list(gathering.gather_merge_currency_units(handle))


def _source_currency_gatherer(operator_input, start_mjdobs, end_mjdobs,
                              handle, parameters, s3_client):
    return list(gathering.gather_source_currency_units(handle))


def _merge_dedup_gatherer(operator_input, start_mjdobs, end_mjdobs, handle,
                          parameters, s3_client):
    return list(gathering.gather_merge_dedup_units(handle))


def _alert_production_gatherer(operator_input, start_mjdobs, end_mjdobs,
                               handle, parameters, s3_client):
    return list(gathering.gather_alert_production_units(
        handle, _release_identity()))


def _campaign_gatherer(operator_input, start_mjdobs, end_mjdobs, handle,
                       parameters, s3_client):
    """The TEST class's one gathering entry (IR-13-a).

    Ignores the window/mjd arguments every other gatherer in this set
    takes: `submission.gathering.gather_campaign_units` reads campaign and
    work_unit STATE (active, ready), never a time window — a campaign's
    units become ready when campaign staging creates them, not when they
    fall inside a poll's observation window. Accepting and discarding the
    unused arguments (rather than a different call shape) is what keeps
    `gatherer_for`'s call site identical for every registry row, per this
    module's own "every entry has the identical call shape" rule.
    """
    return list(gathering.gather_campaign_units(handle))


#: THE REGISTRY. One entry per gathered unit of work, each a
#: `(registry_key, operational_class_name, gather, route_job_type)`
#: four-tuple: `gather` takes `(operator_input, start_mjdobs, end_mjdobs,
#: handle, parameters, s3_client)` — the union of what any one gatherer in
#: the set needs, so every entry has the identical call shape and
#: `gatherer_for` below does not branch on which arguments a given entry's
#: function wants.
#:
#: `operational_class_name` is which of the five declared classes
#: (`pipeline.operator.classes`) this entry gathers under — the class axis
#: stays the operator's `to_run` gate exactly as before this ruling; this
#: registry is what the CLASS axis fans out to, not a replacement for it.
#: Reference construction keeps its one job type; prompt processing fans
#: out to eight (science, the six post-DB types, alert production) — the
#: complete chain the ADOPTED operations text describes as
#: operator-scheduled; test fans out to its one campaign-gathering entry.
#:
#: `registry_key` and `route_job_type` are the SAME string for every row
#: except the campaign entry — see this module's header ("THE REGISTRY KEY
#: IS NOT ALWAYS THE ROUTE JOB TYPE") for exactly why that row differs:
#: `CAMPAIGN_GATHERING_KEY` is what `_BY_JOB_TYPE` keys on (so it cannot
#: collide with `PROMPT_PROCESSING`'s own `JOB_TYPE_SCIENCE` row), while
#: `route_job_type=JOB_TYPE_SCIENCE` is what the fanned-out
#: `OperationalClass.job_type` becomes, so it still submits under the
#: science route (v1 restriction). `_registry_row` fills `route_job_type`
#: from the registry key by default, so every OTHER row's tuple needs no
#: fourth element spelled out.
def _registry_row(registry_key, class_name, gather, route_job_type=None):
    return (registry_key, class_name, gather,
           registry_key if route_job_type is None else route_job_type)


REGISTRY = (
    _registry_row(JOB_TYPE_SCIENCE, opclasses.PROMPT_PROCESSING,
                 _science_gatherer),
    _registry_row(JOB_TYPE_REFERENCE_IMAGE, opclasses.REFERENCE_CONSTRUCTION,
                 _reference_gatherer),
    _registry_row(JOB_TYPE_CATALOG_LOAD, opclasses.PROMPT_PROCESSING,
                 _catalog_load_gatherer),
    _registry_row(JOB_TYPE_CROSSMATCH, opclasses.PROMPT_PROCESSING,
                 _crossmatch_gatherer),
    _registry_row(JOB_TYPE_STATISTICS, opclasses.PROMPT_PROCESSING,
                 _statistics_gatherer),
    _registry_row(JOB_TYPE_MERGE_CURRENCY, opclasses.PROMPT_PROCESSING,
                 _merge_currency_gatherer),
    _registry_row(JOB_TYPE_SOURCE_CURRENCY, opclasses.PROMPT_PROCESSING,
                 _source_currency_gatherer),
    _registry_row(JOB_TYPE_MERGE_DEDUP, opclasses.PROMPT_PROCESSING,
                 _merge_dedup_gatherer),
    _registry_row(JOB_TYPE_ALERT_PRODUCTION, opclasses.PROMPT_PROCESSING,
                 _alert_production_gatherer),
    # The campaign gatherer (IR-13-a): registered under a KEY distinct from
    # the route it submits under — see this module's header and
    # `_registry_row`'s own docstring comment above.
    _registry_row(CAMPAIGN_GATHERING_KEY, opclasses.TEST,
                 _campaign_gatherer, route_job_type=JOB_TYPE_SCIENCE),
)

_BY_JOB_TYPE = {registry_key: (class_name, gather, route_job_type)
                for registry_key, class_name, gather, route_job_type
                in REGISTRY}


def job_types_for_class(class_name):
    """Every registered gathering key that gathers under one operational class.

    In declaration order — the chain order the post-DB co-design states
    (catalog load before crossmatch, both before the sweeps) — which is
    what lets `service.py` build one `Operator` per job type in a
    deterministic, dependency-respecting sequence each pass.

    **RETURNS THE REGISTRY KEY, NOT NECESSARILY THE ROUTE JOB TYPE.** For
    every class but `TEST` the two coincide (this module's header). For
    `TEST` this returns `CAMPAIGN_GATHERING_KEY` — `service.py`'s
    `_classes_for_pass` reads `route_job_type_for` (below) to learn what
    the fanned-out `OperationalClass.job_type` should actually be, so a
    caller that wants the SUBMITTED job type, not the gathering key, uses
    that function instead of assuming this one's return value is it.
    """
    return tuple(registry_key for registry_key, class_name_, _, _ in REGISTRY
                if class_name_ == class_name)


def route_job_type_for(registry_key):
    """The route job type a registry key's `OperationalClass` submits under.

    For every row but the campaign gatherer's this equals `registry_key`
    itself; see this module's header for why the campaign entry's differs.
    `service.py`'s `_classes_for_pass` calls this to build each fanned-out
    `OperationalClass.job_type` correctly, decoupled from the gathering key
    `.name` carries.
    """
    if registry_key not in _BY_JOB_TYPE:
        raise ValueError(
            f"{registry_key!r} is not in the gatherer registry; registered "
            f"keys are {', '.join(sorted(_BY_JOB_TYPE))}")
    _, _, route_job_type = _BY_JOB_TYPE[registry_key]
    return route_job_type


def gatherer_for(registry_key, operator_input, parameters,
                 connection_factory, s3_client=None):
    """The ready-work query for one REGISTRY KEY, bound to this window.

    Returns a callable so the operator can ask for ready work each poll
    without knowing how it is found — which is also what lets a test
    supply a list instead.

    **REGISTRY LOOKUP, NOT A CLASS CONDITIONAL** (co-design ruling 1). This
    used to take `operational_class` and branch
    `if operational_class.name == REFERENCE_CONSTRUCTION: ... else: ...` —
    exactly the shape the ADOPTED text forbids. It now takes the registry
    key directly and looks it up in `REGISTRY`; adding an entry is adding a
    row above, never a new branch here.

    Takes the REGISTRY KEY, not necessarily the route job type it submits
    under (this module's header) — `service.py` passes
    `job_class.name` (the fanned-out `OperationalClass`'s own name, which
    `_classes_for_pass` sets FROM the registry key), never `job_class.
    job_type`, precisely so this lookup and the submission route can
    differ for the campaign entry without either side guessing at the
    other.
    """
    if registry_key not in _BY_JOB_TYPE:
        raise ValueError(
            f"job type {registry_key!r} is not in the gatherer registry; "
            f"registered types are {', '.join(sorted(_BY_JOB_TYPE))}")
    _, gather_fn, _ = _BY_JOB_TYPE[registry_key]

    def gather():
        import database.modules.utils.rapid_db as rapid_db
        from database.modules.utils.checked import CheckedHandle

        start_mjdobs, end_mjdobs = mjd_window(operator_input.start,
                                              operator_input.end)
        with connection_factory() as conn:
            # Wrapped before it becomes the `UnitSource` gathering sees
            # (integration review composite ruling 10): every call the
            # gatherers make either returns clean data or raises
            # `RapidDBCallFailed`, and the `getattr(handle, "exit_code",
            # 0)` checks that used to live in each gatherer are gone —
            # only the "is code 7 a real answer" judgment stays there,
            # because that is what each gatherer's own field means, not
            # what a failed call means.
            handle = CheckedHandle(rapid_db.RAPIDDB.borrowing(conn))
            return gather_fn(operator_input, start_mjdobs, end_mjdobs,
                             handle, parameters, s3_client)

    return gather
