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

import logging
import os

logger = logging.getLogger("rapid.operator.registrar")

#: The probe for DRAFT 048's two identity tables, asked of the catalog before
#: any identity statement runs.
#:
#: BOTH TABLES, not just `products`: `register_identity` writes a product row
#: AND artifact rows AND the binding between them, so a schema carrying one
#: without the other is as unusable as one carrying neither — and probing only
#: the first would turn that into the aborted transaction the probe exists to
#: prevent. Spelled `to_regclass`, matching the probes in
#: `repositories/alert_outbox.py`, `repositories/admission.py` and
#: `repositories/association.py` rather than introducing a fourth spelling.
_IDENTITY_TABLES_PROBE = (
    "SELECT to_regclass('public.products') IS NOT NULL"
    "   AND to_regclass('public.artifacts') IS NOT NULL"
    "   AND to_regclass('public.product_artifacts') IS NOT NULL")


def production_registrar(replay_pre_binding_roles=False, parameters=None,
                         s3_client=None):
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

    # Environment-over-tree precedence, same as the service kernel's DB
    # endpoint resolution: the parameter tree's `s3/records-bucket` is the
    # bucket's one authoritative home (rapid-pipeline-params.yaml; the
    # reconciler reads the same key), and the env var is a per-invocation
    # override for probes. The env-only read was a monolith-era contract:
    # it held while no operational class ran, and the first `run`
    # disposition under the extracted service found the quadlet carries no
    # such variable — the tree the service already reads does.
    records_bucket = (os.environ.get('RAPID_RECORDS_BUCKET')
                      or (parameters or {}).get('s3/records-bucket'))
    if not records_bucket:
        print("*** Error: no records bucket: RAPID_RECORDS_BUCKET is unset "
              "and no `s3/records-bucket` parameter was supplied; the "
              "registrar reads each attempt's terminal record from it and "
              "cannot be built without it; quitting...")
        exit(64)

    from database.modules.utils import rapid_db
    from pipeline.registration.products import registrar
    from pipeline.runtime.boundaries import S3ObjectStore

    # `s3_client` is the caller's client, built from its assumed session —
    # the operator service reads records under the orchestrator role, whose
    # grants name `attempts/*` (the reconciler's client is built the same
    # way). The ambient default exists for the replay tool, which runs
    # under a human's own credentials. Building the client here from
    # ambient credentials was the instance-role AccessDenied class: the
    # host role never had — and should never need — records-bucket read.
    if s3_client is None:
        import boto3
        s3_client = boto3.client("s3")
    store = S3ObjectStore(records_bucket, client=s3_client)

    fallback_roles = None
    if replay_pre_binding_roles:
        from pipeline.runtime import science_config
        fallback_roles = science_config.product_roles(science_config.load())
        print("*** replaying pre-binding records against the running "
              "release's product roles: {}".format(fallback_roles))

    from pipeline.repositories.products import ProductRepository

    def identity_repository_for(conn):
        """`ProductRepository(conn)`, or None where DRAFT 048 is not deployed.

        **THE PROBE IS WHAT MAKES THIS WIRING SAFE TO SHIP AHEAD OF 048**, and
        it is not optional. 048 is still a DRAFT — it is not in the
        authoritative migration stream — so the live registrar will run against
        databases without `products`/`artifacts` for as long as the change
        request is pending. Passing a repository unconditionally there would
        NOT degrade: `register_identity` calls `upsert_product` first thing,
        `ProductRepository._query` re-raises the UndefinedTable as
        `RepositoryQueryFailed`, nothing between there and the consumer catches
        it, and the per-attempt transaction rolls back. Every registration
        would fail, permanently, for want of a table the legacy path does not
        need — a durable rejection, which is exactly what D's P8 forbids
        ("legacy-only, log why, never invent a key, never durably reject").

        ASKED OF THE CATALOG, NOT DISCOVERED BY CATCHING `UndefinedTable`.
        `pipeline/repositories/alert_outbox.py` states the rule for this same
        pair of tables and this same reason: a failed statement ABORTS the
        surrounding transaction, and this runs inside the registration
        consumer's per-attempt transaction, which also carries the legacy rows
        and the watermark. Recovering with a rollback would discard those; the
        probe never puts the transaction in that state. "This schema is not
        deployed" and "this query is wrong" stay two distinct facts.

        ONE PROBE PER REGISTRATION PASS, not per attempt — this is called once
        per connection, and a pass's schema does not change under it.
        """
        with conn.cursor() as cur:
            cur.execute(_IDENTITY_TABLES_PROBE)
            row = cur.fetchone()
        present = bool(row and (row[0] if not isinstance(row, dict)
                                else next(iter(row.values()))))
        if not present:
            # LOGGED AT WARNING, not info: on a database that HAS 048 this
            # line never appears, so its presence is the operator's signal
            # that products and artifacts are not being recorded for this
            # pass and rule 10's cardinality is not being enforced yet.
            logger.warning(
                "registration is running LEGACY-ONLY: DRAFT 048's products "
                "and artifacts tables are not deployed on this database, so "
                "no product or artifact rows will be written for this pass. "
                "The legacy registration is complete and unaffected.")
            return None
        return ProductRepository(conn)

    def for_connection(conn):
        """The callback for ONE registration pass, on ITS connection.

        THE IDENTITY REPOSITORY BORROWS THIS PASS'S CONNECTION, which is what
        makes the products and artifacts tables populate on the LIVE path.
        Until this, package D's identity machinery was code-complete and
        DORMANT: the only call site passing an `identity_repository` was
        `entrypoints.job.registrar_for`, on the `JOB_TYPE_REGISTRATION` route
        the operator service never takes, so `products.py`'s
        `identity_repository is None` branch — the one its own comment calls
        "the pre-rollout path" — was the only branch production ever ran.
        `pipeline/gc/references.py` records the consequence: `artifacts` was
        the table meant to record published files and was never populated,
        so an anti-join keyed on `artifacts.uri` would have classified every
        real product as unreferenced garbage.

        Built HERE, per connection, rather than once in the enclosing factory:
        the repository is bound to one connection, and the operator's three
        phases each open their own registration connection. One repository
        built once could only ever belong to one of them — the same reason
        this layer is a factory at all (round-4 finding #2). Constructing it
        costs one attribute assignment and no round trips, which is why it is
        eager here while the RAPIDDB handle stays lazy: that handle's
        `__init__` issues two queries.

        THE SAME CONNECTION AS THE LEGACY HANDLE, deliberately. Product rows,
        artifact rows, the legacy version rows and the registration watermark
        then commit or roll back together — rule 10's cardinality is only
        meaningful if the rows enforcing it are atomic with the watermark.
        Two connections would be round-3 finding #8 reintroduced through the
        new tables. Note that the class import is at factory scope but the
        INSTANCE is per-connection; importing the module cannot bind a
        connection, so nothing is shared across phases.

        Degradation for a database without DRAFT 048 is D's P8, and it is
        `identity_repository_for`'s catalog probe that delivers it: absent the
        tables the repository is None, which is `products.py`'s existing
        pre-rollout branch — legacy registration complete, no identity rows,
        nothing rejected. This wiring never invents a key and never durably
        refuses an attempt for want of the new tables.
        """
        return registrar(lambda: rapid_db.RAPIDDB.borrowing(conn), store,
                         fallback_roles=fallback_roles,
                         identity_repository=identity_repository_for(conn))

    return for_connection


def registration_callback(factory, conn):
    """The callback `run_registration` should be given on `conn`.

    MOVED FROM `pipeline/virtualPipelineOperator.py` alongside
    `production_registrar` (IR-2). A named seam rather than an inline
    conditional at each call site: the dry-run factory is None and must
    stay None (that is what makes `run_registration` pass `dry_run=True`),
    while a production factory has to be bound to the pass's own
    connection. Getting that wrong at any one call site reintroduces
    round-4 finding #2 for that phase alone, which is precisely the kind
    of defect that hides. `pipeline.operator.operator.Operator._register`
    inlines the same conditional for the service's own pass; this named
    form remains for callers outside that class, such as the role-binding
    replay script.
    """
    if factory is None:
        return None
    return factory(conn)
