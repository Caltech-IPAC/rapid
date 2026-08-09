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
The legacy module keeps its own copies for the phase logic still running
through it; these are the operator's, and the duplication ends when that
module is retired.

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


def _science_gatherer(operator_input, start_mjdobs, end_mjdobs, handle):
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


#: THE REGISTRY. One entry per job type this operator gathers, each a
#: `(job_type, operational_class_name, gather)` triple: `gather` takes
#: `(operator_input, start_mjdobs, end_mjdobs, handle, parameters,
#: s3_client)` — the union of what any one gatherer in the set needs, so
#: every entry has the identical call shape and `gatherer_for` below does
#: not branch on which arguments a given job type's function wants.
#:
#: `operational_class_name` is which of the four declared classes
#: (`pipeline.operator.classes`) this job type gathers under — the class
#: axis stays the operator's `to_run` gate exactly as before this ruling;
#: this registry is what the CLASS axis fans out to, not a replacement for
#: it. Reference construction keeps its one job type; prompt processing
#: now fans out to eight (science, the six post-DB types, alert
#: production) — the complete chain the ADOPTED operations text describes
#: as operator-scheduled.
REGISTRY = (
    (JOB_TYPE_SCIENCE, opclasses.PROMPT_PROCESSING, _science_gatherer),
    (JOB_TYPE_REFERENCE_IMAGE, opclasses.REFERENCE_CONSTRUCTION,
     _reference_gatherer),
    (JOB_TYPE_CATALOG_LOAD, opclasses.PROMPT_PROCESSING,
     _catalog_load_gatherer),
    (JOB_TYPE_CROSSMATCH, opclasses.PROMPT_PROCESSING, _crossmatch_gatherer),
    (JOB_TYPE_STATISTICS, opclasses.PROMPT_PROCESSING, _statistics_gatherer),
    (JOB_TYPE_MERGE_CURRENCY, opclasses.PROMPT_PROCESSING,
     _merge_currency_gatherer),
    (JOB_TYPE_SOURCE_CURRENCY, opclasses.PROMPT_PROCESSING,
     _source_currency_gatherer),
    (JOB_TYPE_MERGE_DEDUP, opclasses.PROMPT_PROCESSING,
     _merge_dedup_gatherer),
    (JOB_TYPE_ALERT_PRODUCTION, opclasses.PROMPT_PROCESSING,
     _alert_production_gatherer),
)

_BY_JOB_TYPE = {job_type: (class_name, gather)
                for job_type, class_name, gather in REGISTRY}


def job_types_for_class(class_name):
    """Every registered job type that gathers under one operational class.

    In declaration order — the chain order the post-DB co-design states
    (catalog load before crossmatch, both before the sweeps) — which is
    what lets `service.py` build one `Operator` per job type in a
    deterministic, dependency-respecting sequence each pass.
    """
    return tuple(job_type for job_type, class_name_, _ in REGISTRY
                if class_name_ == class_name)


def gatherer_for(job_type, operator_input, parameters, connection_factory,
                 s3_client=None):
    """The ready-work query for one JOB TYPE, bound to this window.

    Returns a callable so the operator can ask for ready work each poll
    without knowing how it is found — which is also what lets a test
    supply a list instead.

    **REGISTRY LOOKUP, NOT A CLASS CONDITIONAL** (co-design ruling 1). This
    used to take `operational_class` and branch
    `if operational_class.name == REFERENCE_CONSTRUCTION: ... else: ...` —
    exactly the shape the ADOPTED text forbids. It now takes the job type
    directly and looks it up in `REGISTRY`; adding a job type is adding a
    row above, never a new branch here.
    """
    if job_type not in _BY_JOB_TYPE:
        raise ValueError(
            f"job type {job_type!r} is not in the gatherer registry; "
            f"registered types are {', '.join(sorted(_BY_JOB_TYPE))}")
    _, gather_fn = _BY_JOB_TYPE[job_type]

    def gather():
        import database.modules.utils.rapid_db as rapid_db

        start_mjdobs, end_mjdobs = mjd_window(operator_input.start,
                                              operator_input.end)
        with connection_factory() as conn:
            handle = rapid_db.RAPIDDB.borrowing(conn)
            return gather_fn(operator_input, start_mjdobs, end_mjdobs,
                             handle, parameters, s3_client)

    return gather
