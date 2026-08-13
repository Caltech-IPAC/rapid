"""
File:    schema_contract.py

The startup schema preflight: does the deployed database carry the migration
state this build of the application was written against?

**WHY THIS EXISTS** (rule 18's last clause, verbatim: "Services and payloads
preflight the application/schema contract at startup"). The application and
the schema live in two repositories and deploy on two schedules — the
migration stream is `rapid_systems/cloudformation/db-migrations/`, applied by
that repo's own applier; the code is here. Nothing checked that the two
agreed. A service started against a database missing the migration its SQL
was written for did not fail at startup; it failed later, one query at a
time, as an `UndefinedColumn` in whichever code path happened to run first,
at whatever hour that path was first exercised. Expand/contract makes that
gap NORMAL and BOUNDED rather than exceptional — old code runs against a
newer schema all through a deployment, by design — so the contract this check
enforces is deliberately one-directional (see below).

**THE CONTRACT IS A FLOOR, NOT AN EQUALITY.** The check asserts that every
migration this build REQUIRES has been applied. It does not assert the
converse. A database carrying migrations this build has never heard of is the
expand half of expand/contract — the schema moved first, the old workers'
results stay acceptable, and the rollback window is exactly the period during
which that inequality is the intended state. A preflight demanding equality
would refuse to start precisely the deployment step rule 18 requires to work,
so `REQUIRED_MIGRATIONS` is a floor and a surplus is reported at INFO.

**WHAT THE FLOOR IS DERIVED FROM.** Each entry names a migration whose
objects this repository's SQL actually references, with the call site that
needs it. It is not "every migration that exists": pinning the head would
make every unrelated `rapid_systems` migration a false startup failure here,
and a check that fails for reasons unrelated to this code is a check that
gets commented out. Adding SQL against a new migration's objects means adding
that migration to this list — that is the maintenance cost, and it is the
point.

**`REQUIRED_MIGRATIONS` VS `ROUTE_MIGRATIONS`.** Every route claims an
attempt and a work unit, so `REQUIRED_MIGRATIONS` — the attempt/work-unit
machinery — binds every route the same way, and every preflight caller checks
it unconditionally regardless of which route it is or whether it is a route
at all (the reconciler and operatorctl preflight with no route in view). A
migration whose objects only ONE route's SQL touches belongs in
`ROUTE_MIGRATIONS` instead, keyed by job type, checked only by a caller that
knows the route — see that dict's own docstring for why a route-specific
migration in the global floor would be a false startup failure for every
OTHER caller.

**HOW `schema_migrations` IS POPULATED.** By the applier
(`apply-db-migrations.sh`), one row per file, never by the migration files
themselves — each file's own trailer says so. So this table records what the
applier ran, which is exactly the fact the preflight needs, and a hand-applied
migration that skipped the applier is INVISIBLE here. That is the honest
reading: this check verifies the recorded deployment state, and a schema
changed outside the applier is unrecorded by construction.
"""

import logging

logger = logging.getLogger("rapid.intent.schema_contract")

#: The table the applier records into. Created by `000-schema-migrations.sql`,
#: which is therefore the one migration whose absence this check cannot
#: diagnose as a missing migration — it reports it as an unusable table.
MIGRATIONS_TABLE = "schema_migrations"


class SchemaContractUnmet(RuntimeError):
    """The deployed schema does not satisfy this build's requirements.

    A start failure, deliberately: raised before a service builds anything,
    so the process exits its normal start-failure path rather than crashing
    later inside a query. Carries the full list of missing migrations —
    an operator fixing a deployment wants all of them, not the first.
    """

    def __init__(self, missing, present_count):
        self.missing = tuple(missing)
        self.present_count = present_count
        listed = "\n".join(f"  - {name}: {why}" for name, why in self.missing)
        super().__init__(
            f"the deployed schema is missing {len(self.missing)} migration(s) "
            f"this build requires ({present_count} recorded as applied):\n"
            f"{listed}\n"
            "Apply the migration stream "
            "(rapid_systems/cloudformation/apply-db-migrations.sh) before "
            "starting this service.")


#: Migration file -> the call site whose SQL stops working without it.
#:
#: The reason string is not decoration: it is what an operator reads at 3am to
#: decide whether this is "the migration step was skipped" or "this image is
#: older than this database". Each was established by finding the SQL in this
#: repo that names the object, not by reading the migration stream's own
#: table of contents.
REQUIRED_MIGRATIONS = (
    ("011-attempt-records.sql",
     "the `attempts` and `logical_jobs` tables every attempt write uses "
     "(observability/attempts.py)"),
    ("013-attempt-record-amendments.sql",
     "`resolve_attempt()`, the only attempt-acquisition path "
     "(observability.attempts.AttemptWriter.resolve_attempt)"),
    ("017-protocol-fixes.sql",
     "`attempts.application_claim_index`, read by mark_started's COALESCE "
     "(observability/attempts.py)"),
    ("018-registration-attempt-idempotence.sql",
     "the registration watermark columns `registered_at` / "
     "`registered_record_sequence` (pipeline/registration/consumer.py)"),
    ("022-attempts-closure-record-checksum.sql",
     "`attempts.terminal_record_checksum`, written at closure and read by "
     "the registrar (pipeline/registration/consumer.py)"),
    ("024-attempts-registration-outcome.sql",
     "`attempts.registration_outcome`, the append-once jsonb outcome "
     "document (pipeline/registration/consumer.py)"),
    ("025-attempts-retry-policy-version.sql",
     "`attempts.retry_policy_version`, stamped by mark_started "
     "(observability/attempts.py, pipeline/intent/retry_policy.py)"),
    ("036-intent-schema-v1.sql",
     "the `work_units` / `unit_events` tables and the partial unique index "
     "the claim race resolves through (pipeline/intent/writer.py)"),
    ("039-typed-identity-and-definition-loader.sql",
     "`workflow_definitions` and `derived.load_workflow_definition`, "
     "without which no work unit can satisfy its definition FK "
     "(pipeline/intent/definitions.py)"),
    ("040-scoped-retry-unit-transition.sql",
     "the scoped retry transition the reconciler's closure policy issues "
     "(pipeline/reconciler/service.py)"),
)

#: Per-route floors, layered ON TOP of `REQUIRED_MIGRATIONS` rather than
#: replacing it. `REQUIRED_MIGRATIONS` is what every route needs — the
#: attempt/work-unit machinery no job type can run without, and every
#: preflight caller (payload or service) checks it unconditionally. This is
#: what ONE route additionally needs, keyed by the `submission.routes`
#: job-type string, and checked only by a caller that KNOWS which route it is
#: about to run — currently just the payload entrypoint
#: (`pipeline/entrypoints/job.py:_database`, via `required_for_route`).
#:
#: 049 and 050 are NOT in `REQUIRED_MIGRATIONS` despite both routes being
#: unconditionally implemented (`submission.routes.IMPLEMENTED_JOB_TYPES` has
#: no rollout flag for either): the reconciler and operatorctl preflight too,
#: and neither touches `association_watermarks` or `alert_outbox` — putting
#: route-specific migrations in the global floor would fail THEM closed over
#: schema they never query, which is exactly the false-startup-failure this
#: module's own derivation rule warns against. The per-route floor is the
#: correct shape for that reason alone, independent of any future rollout
#: flag.
#: Keyed by the LITERAL job-type strings `submission.routes.
#: JOB_TYPE_CROSSMATCH` / `JOB_TYPE_ALERT_PRODUCTION` carry, not by an
#: import of those constants: this module is preflighted at the very start
#: of five different entrypoints (`pipeline/entrypoints/job.py`,
#: `pipeline/reconciler/main.py`, `pipeline/operatorctl/main.py`,
#: `pipeline/operator/service.py`) and stays free of every import beyond
#: `logging` on purpose, the same reason `REQUIRED_MIGRATIONS` above needs
#: nothing but tuples of strings. `pipeline/intent/test/test_schema_contract.
#: py` is what catches the two drifting apart if either side is renamed.
ROUTE_MIGRATIONS: dict[str, tuple] = {
    "crossmatch": (
        ("049-association-sets-and-watermarks.sql",
         "`association_watermarks`, read and CAS-advanced by every "
         "crossmatch unit's acceptance transaction "
         "(pipeline/association/watermark.py, "
         "pipeline/stages/post_db.py:crossmatch_sources)"),
    ),
    "alert-production": (
        ("050-alert-outbox-and-publisher.sql",
         "`alert_outbox` and `insert_alert_outbox_packet`, written by "
         "every alert-production unit's confirmation transaction "
         "(pipeline/repositories/alert_outbox.py, "
         "pipeline/stages/alert_production.py)"),
    ),
}


def required_for_route(job_type, base=REQUIRED_MIGRATIONS):
    """The floor one route's payload preflights against: `base` plus its own.

    The composition every call site would otherwise hand-roll — `base +
    ROUTE_MIGRATIONS.get(job_type, ())` — kept here so the two lists stay a
    single addition rather than a pattern copied at each preflight call site.
    An unknown `job_type` contributes nothing extra, which is correct: a job
    type absent from `ROUTE_MIGRATIONS` needs only what every route needs.
    """
    return tuple(base) + ROUTE_MIGRATIONS.get(job_type, ())


def applied_migrations(execute):
    """The set of migration filenames the applier has recorded.

    Takes the same one-callable `execute(sql, params)` executor the rest of
    the intent layer takes, so a service preflighting on a borrowed
    connection and a contract test on a scratch database call it identically.

    A missing `schema_migrations` table is NOT caught here. It propagates,
    because "this database has never had the applier run against it" is not
    the same fault as "migration N is missing" and should not be reported as
    though it were — the caller's start-failure path names it accurately.
    """
    rows = execute(f"SELECT filename FROM {MIGRATIONS_TABLE}", [])
    applied = set()
    for row in rows or ():
        # The executor's row shape is the driver's: psycopg2 hands back
        # tuples, some call sites use dict cursors. Both are accepted for
        # the same reason the writers' `_single_value` accepts both — this
        # module does not get to dictate the caller's cursor factory.
        if isinstance(row, dict):
            applied.add(row["filename"])
        elif isinstance(row, (list, tuple)):
            applied.add(row[0])
        else:
            applied.add(row)
    return applied


def verify_schema_contract(execute, required=REQUIRED_MIGRATIONS):
    """Fail closed unless every required migration is recorded as applied.

    Returns the number of migrations verified, so a caller can log it — and
    so a service that preflighted is distinguishable in the journal from one
    that did not.

    Raises `SchemaContractUnmet` naming EVERY missing migration and why this
    build needs each one.
    """
    applied = applied_migrations(execute)

    missing = [(name, why) for name, why in required if name not in applied]
    if missing:
        raise SchemaContractUnmet(missing, len(applied))

    # THE SURPLUS IS LOGGED, NEVER FAILED — the expand half of
    # expand/contract (see the module docstring). An operator seeing this
    # line during a deployment is seeing the intended state; seeing it
    # OUTSIDE a deployment window is the signal that this image is older
    # than the database it is talking to, which is worth knowing and is not
    # worth refusing to start over.
    surplus = len(applied) - len(required)
    if surplus > 0:
        logger.info(
            "schema preflight: %s required migrations present; the database "
            "carries %s further migration(s) this build does not require "
            "(expected during a deployment's expand phase)",
            len(required), surplus)
    else:
        logger.info("schema preflight: %s required migrations present",
                    len(required))
    return len(required)
