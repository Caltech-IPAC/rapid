"""
File:    fixture.py

The contract tier's connection to a real PostgreSQL, and the row-building
helpers every contract test shares.

**LOCATION-PARAMETERIZED** (brief B, required outcome 1: "The test fixture is
location-parameterized (takes a connection target), so the same suite runs in
CI and on rapid-admin unchanged"). The target comes from the standard libpq
environment variables — PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE — and from
nothing else. There is no config file, no `--host` flag, and no branch on
"am I in CI". A GitHub Actions service container and a podman container on
rapid-admin differ in host and port and in nothing this suite can observe,
which is the property that makes the rapid-admin run acceptance-equivalent to
the CI run rather than merely similar to it. `pipeline.intent.test.
live_brief_a_acceptance` already took its target this way; this module is that
convention, factored out.

**WHY A REAL DATABASE** (rule 23). Every behaviour this tier asserts is a
property of PostgreSQL, not of Python:

  * the claim race is resolved by migration 036's partial unique index
    raising SQLSTATE 23505 in one of two genuinely concurrent transactions;
  * `blocked` requires a non-NULL `blocked_reason` by CHECK constraint;
  * the registration watermark's monotonicity is a `WHERE` predicate
    evaluated under concurrent writers;
  * `resolve_attempt` is a PL/pgSQL function in the migration stream — this
    repository has no copy of it and CANNOT stub it faithfully, because its
    advisory-lock key derivation and its two partial unique indexes are the
    behaviour under test.

The last one is the sharpest argument for this whole tier: the acquisition
path every attempt takes is code this repository does not contain.

**FIXTURE HONESTY** (the discipline `live_brief_a_acceptance` established and
this inherits). Each test builds its own rows under a unique run tag and
deletes only what it created. Nothing truncates a table or assumes an empty
database, so a re-run is safe, two runs may overlap on one database, and a
failure leaves its own rows behind for inspection.

**DOUBLES MUST BE ABLE TO REFUSE.** This module deliberately provides no fake
executor. A contract test that wanted one would be a stub test in the wrong
directory; the tier exists because the fakes could not refuse what the live
system refuses (`pipeline/contract/test_double_agreement.py` is the standing
proof of that, one probe per protocol).
"""

import os
import uuid

#: One tag per process run, so concurrent or repeated runs never collide on
#: the uniqueness constraints these tests deliberately provoke.
RUN_TAG = uuid.uuid4().hex[:12]

#: The job type contract tests create units under. `science` is used because
#: it is the one job type present in every registry AND shipped as a
#: definition file, so a loaded definition exists for it after the deployment
#: step — the same choice, for the same reason, as brief A's acceptance suite.
JOB_TYPE = "science"

DEFINITION_VERSION = 1


def connection_target():
    """The libpq connection parameters, resolved from the environment.

    Returned as a dict rather than applied directly so a caller that needs a
    second, independent connection (the concurrency tests need two) builds it
    from the same resolved target instead of re-reading the environment and
    possibly disagreeing with the first.
    """
    return {
        "host": os.environ.get("PGHOST", "127.0.0.1"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", ""),
        "dbname": os.environ.get("PGDATABASE", "rapid"),
    }


def connect():
    """One new connection to the target, autocommit off.

    psycopg2 is imported inside the function, not at module scope. The stub
    tier stubs `psycopg2` into `sys.modules`, and a module-level import here
    would bind whichever object won that race if the two tiers were ever
    collected into one interpreter — the contract tier must talk to the real
    driver or not run at all.
    """
    import psycopg2

    return psycopg2.connect(**connection_target())


def executor(conn):
    """The `execute(sql, params)` callable the intent layer takes.

    Rows for statements with a result set, `rowcount` otherwise — the exact
    contract `observability.attempts.Executor` and
    `pipeline.intent.writer.Executor` document, implemented over a real
    cursor. This shim is what makes the production writer classes run
    unmodified against real PostgreSQL: the tests exercise the SAME writer
    code the services do, differing only in what its one injected callable
    talks to.
    """
    def execute(statement, params=None):
        with conn.cursor() as cur:
            cur.execute(statement, params)
            if cur.description is not None:
                return cur.fetchall()
            return cur.rowcount
    return execute


def has_table(conn, table_name, schema="public"):
    """Is this table present? The DRAFT-schema probe.

    Brief C ships new schema as DRAFT migration files under
    `migrations-draft/`, which are change requests against `rapid_systems`
    rather than part of the authoritative stream — so a contract test needing
    one must SKIP where it is absent, and must PROBE rather than assume
    ("probe the schema, don't assume"). CI builds its database from the
    authoritative stream and therefore skips those tests; the rapid-admin
    acceptance run applies base + drafts and therefore runs them.

    Asking the catalog rather than trying a query and interpreting the failure
    keeps "this schema is not deployed" apart from "the query is wrong" — two
    facts a test must never conflate, because conflating them turns a broken
    test into a silent skip.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables"
            " WHERE table_schema = %s AND table_name = %s LIMIT 1",
            [schema, table_name])
        return cur.fetchone() is not None


def has_function(conn, function_name, schema="derived"):
    """Is this function present? The DRAFT-schema probe for 046's entry point."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_proc p JOIN pg_namespace n"
            "  ON n.oid = p.pronamespace"
            " WHERE n.nspname = %s AND p.proname = %s LIMIT 1",
            [schema, function_name])
        return cur.fetchone() is not None


def admits_state(conn, state, table="work_units"):
    """Does this table's state CHECK admit `state`?

    Distinct from `has_table`: DRAFT 045 amends an EXISTING constraint rather
    than adding an object, so presence is not the question — vocabulary is.
    Asked by probing the constraint's own text, which is what the database
    will actually enforce, rather than by attempting a write and rolling back
    (that would leave the test's transaction in a failed state and force the
    caller to reason about savepoints to recover).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c"
            " JOIN pg_class t ON t.oid = c.conrelid"
            " WHERE t.relname = %s AND c.conname = %s",
            [table, f"{table}_state_ck"])
        row = cur.fetchone()
    return bool(row) and f"'{state}'" in row[0]


def scope(name):
    """A run-unique `input_scope`, so two runs never collide on identity.

    The partial unique index is on `(job_type, input_scope)` where the unit
    is not superseded — so an un-tagged scope would make a second run of this
    suite fail against the first run's leftover rows, and the failure would
    look exactly like the defect the test is hunting.
    """
    return f"{name}-{RUN_TAG}"


def ensure_definition(conn):
    """A loaded `workflow_definitions` row, so `work_units`'s FK is satisfiable.

    Inserted directly rather than through `derived.load_workflow_definition`
    because this suite runs as the scratch superuser, not `rapid_operator`,
    and the loader's own behaviour is asserted separately through the real
    function. Idempotent.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workflow_definitions"
            "  (job_type, definition_version, checksum, source_path,"
            "   description)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (job_type, definition_version) DO NOTHING",
            [JOB_TYPE, DEFINITION_VERSION, "contract-fixture",
             "cdf/workflow/science-v1.toml", "contract tier fixture"])
    conn.commit()


def make_logical_job(conn, run_id=None, with_binding=False):
    """One `logical_jobs` row, returning its id.

    `attempts.logical_job_id` carries an FK to this table, so every attempt
    fixture needs a parent. A fixture that cannot satisfy the real
    constraints is a fixture testing a schema nobody deployed.

    **`with_binding` IS REQUIRED FOR ANYTHING GOING THROUGH `resolve_attempt`**,
    and finding that out is a small worked example of why this tier exists.
    The resolver's INSERT copies the execution binding from the logical job
    (`v_binding.job_definition_arn` and friends), and
    `attempts_state_submitted_check` — as amended by migration 013 — requires
    a `submitted` row at `schema_version >= 2` to carry a non-NULL
    job-definition ARN, image digest and manifest checksum. A binding-less
    logical job therefore produces an attempt the schema refuses, from inside
    the resolver, with the failure attributed to a CHECK constraint two levels
    away from the fixture that caused it.

    That is the sealed-submission discipline of rule 7 being enforced by the
    database rather than by convention: an attempt cannot exist without the
    exact queue/job-definition/image identity it will run under. No fake
    executor in this repository enforces it, which is exactly why the first
    version of this fixture did not set it.
    """
    run_id = run_id or f"contract-{RUN_TAG}"
    logical_job_id = f"lj-{RUN_TAG}-{uuid.uuid4().hex[:8]}"
    columns = ["logical_job_id", "run_id"]
    values = [logical_job_id, run_id]
    if with_binding:
        columns += ["job_definition_arn", "job_definition_rev",
                    "image_digest", "release_identity", "manifest_checksum"]
        values += [
            # A SYNTHETIC ARN CARRYING NO ACCOUNT FIELD AT ALL. The binding's
            # CONTENT is never interpreted by anything under test — the
            # constraint requires it to be present and non-NULL, nothing more.
            # A real account number here would be an identifier published to a
            # public repository for no test's benefit, and the repo's pre-push
            # guard rejects any 12-digit run alike, placeholder included. The
            # honest resolution is not to allowlist a guard that is protecting
            # a public repo, but to stop putting an account-shaped field in a
            # string nothing parses.
            "arn:aws:batch:us-east-1:account:job-definition/contract:1",
            1,
            "sha256:" + "0" * 64,
            f"contract-release-{RUN_TAG}",
            "sha256:" + "1" * 64,
        ]
    placeholders = ", ".join(["%s"] * len(values))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO logical_jobs ({', '.join(columns)})"
            f" VALUES ({placeholders}) ON CONFLICT DO NOTHING",
            values)
    return logical_job_id, run_id


def make_attempt(conn, work_unit_id=None, error_category=None,
                 registered=False, lifecycle="submitted",
                 terminal_record_sequence=None):
    """One `attempts` row, minimal but real: the table's own constraints honoured.

    Returns its attempt_id.

    **THE LIFECYCLE STATE DECIDES WHICH COLUMNS MAY BE SET**, and the schema
    enforces it per state — `attempts_state_submitted_check` requires a
    `submitted` row to carry NO outcome facts at all (including
    `error_category`), while `attempts_state_terminal_without_start_check`
    requires a `terminal_without_start` row to carry `ended_at` and
    `scheduler_state` and NO `started_at`. That is the schema refusing to let
    a row claim a failure category while claiming to be still in flight, and
    it is exactly the kind of invariant a hand-built fake cannot enforce —
    the first version of brief A's equivalent fixture wrote `error_category`
    onto a `submitted` row and only real PostgreSQL objected.
    """
    if error_category is not None and lifecycle == "submitted":
        lifecycle = "terminal_without_start"
    logical_job_id, run_id = make_logical_job(conn)
    terminal = lifecycle in ("terminal_without_start", "terminal_after_start",
                             "application_closed")
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
        schema_version = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO attempts"
            "  (run_id, schema_version, logical_job_id, lifecycle_state,"
            "   created_at, submitted_at, work_unit_id, error_category,"
            "   ended_at, scheduler_state, registered_at,"
            "   registered_record_sequence, terminal_record_sequence,"
            "   terminal_record_key)"
            " VALUES (%s, %s, %s, %s, now(), now(), %s, %s,"
            "         CASE WHEN %s THEN now() ELSE NULL END,"
            "         CASE WHEN %s THEN 'FAILED' ELSE NULL END,"
            "         CASE WHEN %s THEN now() ELSE NULL END,"
            # `attempts_registered_pair_check`: the acceptance timestamp and
            # the record sequence it was accepted at are set together or not
            # at all — a registered_at with no sequence would name an
            # acceptance nobody can locate.
            "         CASE WHEN %s THEN 1 ELSE NULL END,"
            "         %s,"
            "         CASE WHEN %s IS NULL THEN NULL"
            "              ELSE %s END)"
            " RETURNING attempt_id",
            [run_id, schema_version, logical_job_id, lifecycle, work_unit_id,
             error_category, terminal, terminal, registered, registered,
             terminal_record_sequence,
             terminal_record_sequence,
             f"records/{RUN_TAG}/{uuid.uuid4().hex[:8]}.json"])
        return cur.fetchone()[0]


def _diffimage_parents(conn, field, tag):
    """The four FK parents a `diffimages` row needs. Returns their ids.

    `filters` and `pipelines` ARE seeded by the stream (`009-seed-data.sql`),
    so `fid` is read rather than written — inventing a filter would put a row
    in the catalogue the pipeline reads. `exposures`, `l2files` and
    `refimages` are NOT seeded, so a minimal parent is created per call.

    Created per call rather than shared: `refimagespk` is UNIQUE on
    `(field, fid, ppid, version)` and these tests deliberately place several
    images on one field, so a shared reference image would collide on the
    second one. The `version` is taken from a sequence-free max+1 for the same
    reason.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
        row = cur.fetchone()
        if row is None:
            raise AssertionError(
                "no rows in `filters`; 009-seed-data.sql seeds them, so an "
                "empty table means the stream was not fully applied")
        fid = row[0]

        cur.execute(
            "INSERT INTO exposures (dateobs, field, fid, exptime, mjdobs,"
            "                       hp6, hp9)"
            " VALUES (now(), %s, %s, 100.0, 60000.0, 1, 1) RETURNING expid",
            [field, fid])
        expid = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO l2files (expid, sca, version, vbest, field, fid,"
            "                     dateobs, mjdobs, exptime, filename,"
            "                     checksum, crval1, crval2, crpix1, crpix2,"
            "                     cd11, cd12, cd21, cd22)"
            " VALUES (%s, 1, 1, 1, %s, %s, now(), 60000.0, 100.0, %s, %s,"
            "         10.0, 10.0, 1.0, 1.0, 1.0, 0.0, 0.0, 1.0)"
            " RETURNING rid",
            [expid, field, fid, f"l2/{RUN_TAG}/{tag}.fits", tag[:8]])
        rid = cur.fetchone()[0]

        cur.execute(
            "SELECT coalesce(max(version), 0) + 1 FROM refimages"
            " WHERE field = %s AND fid = %s AND ppid = 12", [field, fid])
        version = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO refimages (field, hp6, hp9, fid, ppid, version,"
            "                       vbest, svid, filename, checksum)"
            " VALUES (%s, 1, 1, %s, 12, %s, 1, 1, %s, %s) RETURNING rfid",
            [field, fid, version, f"ref/{RUN_TAG}/{tag}.fits", tag[:8]])
        rfid = cur.fetchone()[0]

    return expid, rid, fid, rfid


def make_diffimage(conn, attempt_id, field, ppid, created=None, vbest=1,
                   sca=1):
    """One `diffimages` row, minimal but real, for the work-inventory tests.

    Returns its `pid`.

    Every column the science-work predicate reads is a PARAMETER here —
    `ppid`, `vbest`, `field`, `created` — because the whole point of the
    agreement test is to place rows at the edges of those predicates and see
    whether two queries classify them the same way. A builder that fixed any
    of them would quietly remove the case it was meant to cover.

    The geometry columns are filled with in-range constants: `diffimages` has
    ten CHECK constraints on ra/dec, so a fixture cannot skip them, and the
    tests reading these rows care about the ordering predicates rather than
    where on the sky the image was.

    `attempt_id` requires `registered_record_sequence` alongside it
    (`diffimages_attempt_identity_check`, migration 018) — both halves or
    neither, so the pair is written together here.

    **THE PARENT ROWS ARE REAL.** `diffimages` carries five foreign keys —
    `expid` to `exposures`, `rid` to `l2files`, `fid` to `filters`, `ppid` to
    `pipelines`, `rfid` to `refimages` — and PostgreSQL enforces every one of
    them. `_reference_row` below reuses whatever the applied stream already
    seeded and mints a minimal parent only when the table is empty, so this
    fixture neither depends on a particular seed nor duplicates rows the
    stream already provides. A first version passed literal `1`s for all five
    and real PostgreSQL refused it on `diffimages_expid_fk`, which is the
    referential half of the same lesson the CHECK constraints taught above.
    """
    tag = uuid.uuid4().hex[:8]
    expid, rid, fid, rfid = _diffimage_parents(conn, field, tag)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO diffimages"
            "  (rid, expid, sca, ppid, version, vbest, rfid, field, hp6, hp9,"
            "   fid, jd, ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4,"
            "   dec4, infobitssci, infobitsref, filename, status, svid,"
            "   created, attempt_id, registered_record_sequence)"
            " VALUES (%s, %s, %s, %s, 1, %s, %s, %s, 1, 1,"
            "         %s, 2460000.5, 10.0, 10.0, 10.0, 10.0, 10.1, 10.0,"
            "         10.1, 10.1, 10.0, 10.1, 0, 0, %s, 0, 1,"
            "         COALESCE(%s::timestamptz, now()), %s, 1)"
            " RETURNING pid",
            [rid, expid, sca, ppid, vbest, rfid, field, fid,
             f"diffimages/{RUN_TAG}/{tag}.fits", created, attempt_id])
        return cur.fetchone()[0]


def make_completed_attempt(conn, rapid_outcome="success", field=None,
                           processing_date=None):
    """One attempt that RAN and finished — the shape a science outcome needs.

    Returns its attempt_id.

    **THE SCHEMA DECIDES WHAT A FINISHED ATTEMPT LOOKS LIKE, AND IT IS STRICT.**
    An attempt carrying `rapid_outcome` must be `terminal_after_start`, and
    the sibling state `terminal_without_start` forbids `rapid_outcome`
    outright — nothing ran, so nothing succeeded. There is no third option: a
    successful science attempt is a fully-populated terminal-after-start row
    or it is not representable.

    The live CHECK is migration 014's, not 011's — the constraint was replaced
    twice (013 then 014) and only the last one is on the table. At
    `schema_version >= 2` it requires SIXTEEN columns together: the nine
    run-and-finish fields (`started_at`, `scheduler_job_id`, `source_sha`,
    `container_digest`, `job_definition_rev`, `config_digest`, `ended_at`,
    `scheduler_state`, `rapid_outcome`, `product_disposition`) PLUS the
    version-gated six — the binding triple (`binding_job_definition_arn`,
    `binding_image_digest`, `binding_manifest_checksum`),
    `scheduler_observed_exit`, and the terminal record pair
    (`terminal_record_key`, `terminal_record_sequence`).

    Two successive versions of this fixture were refused by real PostgreSQL —
    the first bolted an outcome onto a half-built row, the second satisfied
    011's list and missed 014's version-gated additions. Both are the lesson
    `make_attempt`'s own docstring records: this class of invariant is exactly
    what a hand-built fake cannot enforce, and reading the CURRENT constraint
    rather than the one a grep found first is the difference between a fixture
    that documents the schema and one that guesses at it.
    """
    logical_job_id, run_id = make_logical_job(conn)
    tag = uuid.uuid4().hex[:8]
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
        schema_version = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO attempts"
            "  (run_id, schema_version, logical_job_id, lifecycle_state,"
            "   created_at, submitted_at, started_at, ended_at,"
            "   scheduler_job_id, scheduler_state, scheduler_observed_exit,"
            "   source_sha, container_digest, job_definition_rev,"
            "   config_digest, process_exit_code, rapid_outcome,"
            "   product_disposition, binding_job_definition_arn,"
            "   binding_image_digest, binding_manifest_checksum,"
            "   terminal_record_key, terminal_record_sequence,"
            "   field, processing_date)"
            " VALUES (%s, %s, %s, 'terminal_after_start',"
            "         now(), now(), now(), now(),"
            "         %s, 'SUCCEEDED', 0,"
            "         %s, 'sha256:' || %s, 1,"
            "         'sha256:' || %s, 0, %s,"
            "         'published', %s,"
            "         'sha256:' || %s, 'sha256:' || %s,"
            "         %s, 1,"
            "         %s, %s::date)"
            " RETURNING attempt_id",
            [run_id, schema_version, logical_job_id,
             f"job-{tag}", f"sha-{tag}", tag, tag, rapid_outcome,
             f"arn:aws:batch:us-east-1:000000000000:job-definition/f-{tag}:1",
             tag, tag,
             f"records/{RUN_TAG}/{tag}.json",
             field, processing_date])
        return cur.fetchone()[0]


def unit_state(conn, work_unit_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, blocked_reason FROM work_units WHERE work_unit_id=%s",
            [work_unit_id])
        return cur.fetchone()


def unit_events(conn, work_unit_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT from_state, to_state, writer FROM unit_events"
            " WHERE work_unit_id = %s ORDER BY unit_event_id", [work_unit_id])
        return cur.fetchall()


def create_unit(conn, input_scope, state=None):
    """A work unit in `state`, created through the production writer.

    Goes through `WorkUnitWriter` rather than a hand-written INSERT on
    purpose: a fixture that writes rows the production writer would never
    write tests a schema the application does not use.
    """
    from pipeline.intent.writer import (READY, WRITER_ORCHESTRATOR,
                                        WRITER_VALIDATION_INGEST,
                                        WorkUnitIdentity, WorkUnitWriter)

    state = READY if state is None else state
    writer = WorkUnitWriter(executor(conn))
    identity = WorkUnitIdentity(
        job_type=JOB_TYPE, input_scope=input_scope,
        operational_class="prompt-processing",
        definition_version=DEFINITION_VERSION)
    work_unit_id = writer.create_work_unit(
        identity, writer=WRITER_VALIDATION_INGEST, state=READY)
    if state != READY:
        writer.transition_unit(work_unit_id, READY, state,
                               writer=WRITER_ORCHESTRATOR)
    conn.commit()
    return work_unit_id
