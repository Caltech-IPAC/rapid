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
