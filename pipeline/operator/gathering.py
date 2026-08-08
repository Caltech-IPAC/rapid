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
"""

import logging

from pipeline.operator import classes as opclasses

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


def gatherer_for(operational_class, operator_input, parameters,
                 connection_factory, s3_client=None):
    """The ready-work query for one class, bound to this window.

    Returns a callable so the operator can ask for ready work each poll
    without knowing how it is found — which is also what lets a test
    supply a list instead.
    """
    from submission import gathering

    start = operator_input.start
    end = operator_input.end

    def gather():
        import database.modules.utils.rapid_db as rapid_db

        start_mjdobs, end_mjdobs = mjd_window(start, end)
        with connection_factory() as conn:
            handle = rapid_db.RAPIDDB.borrowing(conn)
            if operational_class.name == opclasses.REFERENCE_CONSTRUCTION:
                return list(gathering.gather_reference_units(
                    handle, start, end,
                    start_mjdobs=start_mjdobs, end_mjdobs=end_mjdobs,
                    min_images_to_coadd=min_images_to_coadd(),
                    s3_client=s3_client,
                    job_bucket=parameters["s3/products-bucket"],
                    run_id=None))
            return list(gathering.gather_science_units(
                handle, start, end,
                start_mjdobs=start_mjdobs, end_mjdobs=end_mjdobs,
                min_images_to_coadd=min_images_to_coadd()))

    return gather
