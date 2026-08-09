"""The production registration callback factory.

MOVED FROM `pipeline/virtualPipelineOperator.py` (IR-1a extraction) —
see `pipeline.operator.submission`'s header for why importing anything
from that module is a hazard, and why the monolith imports this back
rather than keeping its own copy.

Split from `submission`: this builds what registers the products a
phase's jobs produce, a different responsibility with a different
lifetime than resolving what a submission binds to (built once per
phase, then re-bound per registration connection, versus resolved once
before anything runs).
"""

import os

import boto3


def production_registrar(replay_pre_binding_roles=False):
    """A factory for the real registration callback: connection -> callback.

    `replay_pre_binding_roles` is the deliberate replay switch. Attempts
    published before the difference-image role binding existed carry no
    `product_roles`, so registration refuses them — which is correct until
    someone is knowingly replaying exactly those. Set it and the running
    release's bindings answer for records that carry none, and only those;
    a record with its own binding always wins. Off by default, because a
    silent fallback would let the running release re-interpret history.

    IT TAKES THE PASS'S CONNECTION (round-4 finding #2), and that is the whole
    point of the extra layer. This used to return a callback built over
    `registrar(rapid_db.RAPIDDB, store)` — the class, as a factory — which
    meant the registrar opened a SECOND, autocommitting connection of its own
    while `run_registration(regconn, ...)` advanced the watermark on the
    first. Two connections cannot be one transaction, so product rows became
    durable before the watermark was attempted, and a crash between them left
    rows written with the attempt still a candidate: the next pass registered
    the same products all over again. That is round-3 finding #8, fixed in the
    registration job and reintroduced here.

    Handed a connection, the registrar builds its handle over that one
    (`RAPIDDB.borrowing`), whose commits are suppressed, so the product rows
    and the watermark land in one transaction — the same shape as
    `entrypoints.job.registrar_for(context, conn)`, which is the pattern this
    now follows deliberately rather than by coincidence.

    A FACTORY rather than a callback because the operator's three phases each
    open their own registration connection. One callback built once could only
    ever borrow one of them, which would put the split back for the other two.
    The expensive parts — reading the bucket name, building the S3 client and
    the store — still happen once, here; only the per-connection binding is
    deferred.

    `registrar` takes its database handle as a CALLABLE, so the VPO can build
    one from what it already has — a records bucket name and an S3 client —
    without standing up the stage machinery a job entrypoint has. Without this
    the VPO's only options were a dry run or nothing, which is exactly how
    registration came to report success while writing no rows.

    The bucket is read from the environment like every other deployment fact
    this module needs, and is REQUIRED: a registrar that cannot find the
    records it registers from would fail per-attempt, deep inside a pass,
    rather than here where it is one clear message.

    Returns None where DRY-RUN is explicitly asked for. Production defaults to
    production — the flag has to be set to get a rehearsal, never the other
    way round.
    """
    if os.environ.get('RAPID_VPO_DRY_RUN', '').lower() in ('1', 'true', 'yes'):
        print("*** RAPID_VPO_DRY_RUN is set: registration will DECIDE only "
              "and write no operation-table rows.")
        return None

    records_bucket = os.environ.get('RAPID_RECORDS_BUCKET')
    if not records_bucket:
        print("*** Error: Env. var. RAPID_RECORDS_BUCKET not set; the "
              "registrar reads each attempt's terminal record from it and "
              "cannot be built without it; quitting...")
        exit(64)

    from database.modules.utils import rapid_db
    from pipeline.registration.products import registrar
    from pipeline.runtime.boundaries import S3ObjectStore

    store = S3ObjectStore(records_bucket, client=boto3.client("s3"))

    fallback_roles = None
    if replay_pre_binding_roles:
        from pipeline.runtime import science_config
        fallback_roles = science_config.product_roles(science_config.load())
        print("*** replaying pre-binding records against the running "
              "release's product roles: {}".format(fallback_roles))

    def for_connection(conn):
        """The callback for ONE registration pass, on ITS connection."""
        return registrar(lambda: rapid_db.RAPIDDB.borrowing(conn), store,
                         fallback_roles=fallback_roles)

    return for_connection
