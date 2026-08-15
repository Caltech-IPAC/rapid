"""Admission-path registration for g0001's already-ingested inputs.

**WHAT THIS CLOSES.** g0001's 5,166 `l2files` rows were registered on
2026-08-05 by an earlier revision of `db_register_socsim_files.py`, at image
revision 7 — before that script's admission-bridge calls existed or were
exercised against this dataset. The legacy rows are correct and complete; what
is missing is their side of the admission record. `admission_manifests` and
`admission_l2files` are EMPTY, so `pipeline/repositories/data_class.py`'s join
(`admission_l2files a JOIN admission_manifests m ... WHERE a.rid = ANY(...)`)
has nothing to read, gathering correctly inherits no class, and every unit
built from these inputs falls through to the deployment-wide `data/class`
parameter. That fallback is a documented stopgap, not provenance. This script
supplies the missing half so the chain feeds itself.

**WHY A STANDALONE SCRIPT AND NOT THE INGEST PATH.** `admission_bridge.py`'s
helpers are ingest-time: they admit one grain at a time, inside the SAME
borrowed transaction as the legacy `add_l2file_fifth_order` write that
describes it, because "a crash between them would leave exactly the
split-brain state the sealed manifest exists to prevent." This run's contract
is the opposite one — the legacy rows have existed for ten days, and there is
no accompanying insert to be atomic with. Re-running the ingest script to make
its bridge calls fire is not available either: its `add_exposure` /
`add_l2file_fifth_order` writes are unconditional and not idempotent, so it
would duplicate the very rows this backfill exists to describe. A separate
entry point states that different contract in its own name rather than adding
a mode to a live ingest path.

**THE ORDER IS ENUMERATE, SEAL, THEN ADMIT — and it is not the order the
repository's method list suggests.** `admission_l2files_manifest_sealed` and
`admission_exposures_manifest_sealed` fire BEFORE INSERT and refuse any
admission citing a manifest whose `sealed_at IS NULL`: "a manifest is sealed
only once every entry is durable, so citing an unsealed one would record an
admission against a source that may still be partial." So the manifest's
entries are written first, the manifest is sealed, and only then may the
admissions cite it. Sealing is therefore not the last step of this script.

**IDEMPOTENT AS A WHOLE OPERATION.** Every write is `INSERT ... ON CONFLICT`
against a natural key: the manifest on `manifest_key`, entries on
`(manifest_id, source_bucket, source_key)`, exposures on `admission_identity`
(the sha256 of the dateobs grain), l2files on `admission_identity` (the sha256
of exposure/sca/checksum) with a second unique on `(expid, sca)`. `seal_manifest`
returns the recorded state rather than re-sealing. A second run writes nothing
and says so.

**NOTHING IS DELETED, AND NOTHING EXISTING IS REWRITTEN.** No `l2files`,
`exposures`, S3 object or product is touched. The admissions cite the `rid` and
`expid` values already in the database; none are minted.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pipeline.repositories.admission import (AdmissionRepository,  # noqa: E402
                                             AdmissionConflict)

#: The source bucket and generation this backfill describes. A literal rather
#: than a parameter: this script exists for one historical dataset, and a
#: generic "backfill any scope" tool would be a different thing with a
#: different review burden.
BUCKET = "roman-rapid-inputs-gbtds-sim"
GENERATION = "g0001"

#: One manifest for the whole generation. THE GRAIN IS THE INGEST RUN, and
#: g0001's registration WAS one run — "registration executed as a single Batch
#: job at image revision 7" (rapid_plan/decisions.md). Splitting it per
#: exposure would invent 287 ingest events that never happened; one manifest
#: per real ingest is what `admission_manifests` means.
MANIFEST_KEY = "gbtds-sim/g0001/backfill-2026-08-15"

#: The class these inputs carry, CITED not asserted:
#: `rapid_plan/research/input-coverage-findings.md` records g0001 as SOC
#: simulation release r00340 with injected sources — simulated substrate,
#: injected content. The same citation the 2026-08-15 closeout backfill used
#: for the 113 work units, so the manifest and those units agree by
#: construction rather than by coincidence.
DATA_CLASS = "sim-injected"

#: `socsim` names the PRODUCER, not the family (rapid_plan/design/naming.md's
#: Simulation sources registry says so explicitly, and reserves the token
#: against a second SOC family). The lineage is the SOC's own release
#: identifier, which every ingested filename carries
#: (`r0034001002001001003_0001_wfi01_f146_cal_lite.fits.gz`) and which the
#: source of truth is named for (`s3://stpubdata/roman/nexus/soc_simulations/
#: r00340/`). Registered as `socsim-r00340` in the token registry.
SOURCE_SCOPE = "socsim-r00340"

#: `external-versioned`: the bytes live in a bucket RAPID does not own the
#: lifecycle of (the inputs bucket is fed from stpubdata), so the pipeline does
#: not retain custody of them and must record the source version instead.
BYTE_CUSTODY = "external-versioned"

#: A release identity distinct from any deploy's. THE MANIFEST'S OWN
#: `release_identity` IS THE PROVENANCE RECORD that these rows came from a
#: backfill rather than from ordinary ingest — `AdmissionRepository` has no
#: dispatcher/actor parameter of its own (it is a plain repository, not a
#: `derived.*` audited function), so the release is where that fact can live
#: without inventing a column. Registering a normal `smdc-<sha>` release here
#: would make this indistinguishable from an ingest that ran under that build.
RELEASE_IDENTITY = "provenance-backfill-g0001-2026-08-15"

RELEASE_REASON = (
    "provenance backfill: admission rows for g0001's 5,166 l2files, "
    "registered 2026-08-05 by db_register_socsim_files.py at image revision 7 "
    "before the admission bridge was exercised against this dataset")

#: The idempotency key 047's replay path keys this registration on. Fixed, not
#: generated: a re-run of this backfill is the SAME operator act, and a fresh
#: key each time would defeat the replay that makes repetition a recorded
#: no-op rather than a second decision.
RELEASE_IDEMPOTENCY_KEY = "provenance-backfill-g0001-2026-08-15-release"

RELEASE_POLICY_CITATION = (
    "rapid_plan/decisions.md 'Per-unit data-class carrier' — follow-up: route "
    "socsim registration through admission so the manifest carries substrate "
    "and injection at ingest")

#: Every source object, with the identity fields the admission needs, straight
#: from the legacy rows. `l2files.filename` holds the full `s3://bucket/key`
#: URI the ingest recorded, so the manifest entry's bucket and key are read
#: back from what was actually ingested rather than reconstructed from a
#: pattern — and no S3 listing is needed to enumerate 5,166 objects.
#:
#: `vbest > 0` mirrors the gatherer's own eligibility predicate: a superseded
#: version is not what a unit would be built from, so it is not what the
#: manifest should claim as the admitted source. (All 5,166 rows are
#: version 1 / vbest 1 today; the predicate states the intent rather than
#: relying on that staying true.)
_SOURCES_SQL = """
SELECT l.rid, l.expid, l.sca, l.checksum, l.filename, l.dateobs,
       e.dateobs AS exposure_dateobs, l.field, l.fid, l.mjdobs
  FROM l2files l
  JOIN exposures e ON e.expid = l.expid
 WHERE l.filename LIKE %s
   AND l.vbest > 0
 ORDER BY l.expid, l.sca
"""


def _sources(conn):
    """Every g0001 input row, ordered by its natural grain."""
    prefix = "s3://%s/%s/%%" % (BUCKET, GENERATION)
    with conn.cursor() as cur:
        cur.execute(_SOURCES_SQL, (prefix,))
        return cur.fetchall()


def _key_of(filename):
    """The object key inside the bucket, from the recorded `s3://` URI.

    Split rather than parsed with a pattern: the URI was written by the ingest
    and is the record of what was read, so the key is whatever follows the
    bucket — this function must not have an opinion about the shape of the
    rest.
    """
    marker = "s3://%s/" % BUCKET
    if not filename.startswith(marker):
        raise ValueError(
            "row's filename %r is not under %s, so this backfill's scope "
            "does not describe it" % (filename, marker))
    return filename[len(marker):]


def _register_release(conn, dry_run):
    """Register the backfill's release identity through the audited route.

    NOT `AdmissionRepository.register_release()`, which INSERTs into
    `admission_releases` directly and is refused: that table grants SELECT to
    `rapid_pipeline_write`/`rapid_read` and holds INSERT with `postgres`
    alone. That is deliberate, and migration 057 exists precisely because of
    it — the release row is "the root row of the admission-identity chain",
    and writing it by a bare INSERT would mean "no actor, no reason, no
    idempotency key and no `derived.mutation_audit` entry". So the
    registration goes through `derived.register_admission_release`, which is
    SECURITY DEFINER, granted to `rapid_orchestrator`, and audited.

    The repository's own method stays untouched: it is the ingest-time API and
    its refusal here is the grant working as designed, not a bug to route
    around.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.register_admission_release("
            " p_idempotency_key := %s, p_release_identity := %s,"
            " p_reason := %s, p_dry_run := %s, p_policy_citation := %s)",
            (RELEASE_IDEMPOTENCY_KEY, RELEASE_IDENTITY, RELEASE_REASON,
             dry_run, RELEASE_POLICY_CITATION))
        return cur.fetchone()[0]


def _open_classified_manifest(conn, data_class):
    """Create (or find) the manifest, carrying its data class from birth.

    **THE CLASS IS SET AT INSERT, NEVER BY A LATER UPDATE, AND THAT IS A
    PRIVILEGE FACT AS WELL AS A DESIGN ONE.** Migration 051 grants the writer
    tier `SELECT, INSERT` on `admission_manifests` plus a column-scoped
    `UPDATE (sealed_at, entry_count, entries_checksum)` — "sealing a manifest
    is an UPDATE of exactly one column", with a REVOKE beside it so a later
    blanket grant cannot silently widen it. Migration 090 then added
    `data_class` to the same table and granted UPDATE on it to nobody. So the
    only reachable way to record a manifest's class is to state it when the
    row is created; an `UPDATE ... SET data_class` is refused for every role
    short of `postgres` (verified live). Setting it at INSERT is also the
    better shape on its own terms: the class is a property the manifest has
    from birth, and a window in which a manifest exists with no class is a
    window in which the join can read a manifest that does not yet know what
    it is.

    `AdmissionRepository.open_manifest()` cannot express this — it predates
    090 and its INSERT names four columns. Rather than widen the repository's
    ingest-time signature for a backfill's sake, the row is inserted here with
    the same `ON CONFLICT (manifest_key)` idempotency the repository uses, and
    `open_manifest` is then called to read it back through the repository's
    own contract (returning the existing row unchanged on a re-run).
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO admission_manifests"
            " (manifest_key, source_scope, release_identity, byte_custody,"
            "  data_class)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (manifest_key) DO NOTHING"
            " RETURNING manifest_id",
            (MANIFEST_KEY, SOURCE_SCOPE, RELEASE_IDENTITY, BYTE_CUSTODY,
             data_class))
        row = cur.fetchone()
    return row[0] if row else None


def _reset_role(conn):
    """Start from the login role, whatever the pooled session was left as.

    **THE CONNECTION IS POOLED AND `SET ROLE` OUTLIVES THE CLIENT.** This runs
    through pgbouncer, so the backend serving this script may be one an
    earlier run of it already switched to `rapid_pipeline_write` — a role
    change is session state, and the pooler hands the session on without
    resetting it. The second run then starts as the writer tier and is refused
    EXECUTE on the orchestrator-only audited function, which looks exactly
    like a revoked grant and is not one (observed 2026-08-15: the first
    re-run failed "permission denied for function register_admission_release"
    while the function's ACL still listed `rapid_orchestrator`, and
    `current_user` was `rapid_pipeline_write`).

    So the script states the role it starts from rather than inheriting it. An
    idempotent operation that only works on a cold pool is not idempotent.
    """
    with conn.cursor() as cur:
        cur.execute("RESET ROLE")


def _assume_writer_tier(conn):
    """`SET ROLE` to the tier the admission tables actually grant to.

    The login role that can reach this database (`rapid_orchestrator`) is a
    MEMBER of `rapid_pipeline_write`, which is what holds INSERT on the
    admission tables — membership alone does not confer the privilege, the
    role has to be assumed. This is the same explicit tier assumption
    `rapidctl`'s `operator_session()` makes, and making it explicit here keeps
    the writes attributable to the tier that is granted them rather than
    depending on whatever the connecting login happens to hold.
    """
    with conn.cursor() as cur:
        cur.execute("SET ROLE rapid_pipeline_write")


def backfill(conn, dry_run=True):
    """Register g0001's inputs through the admission path. Returns a summary."""
    _reset_role(conn)
    repo = AdmissionRepository(conn)
    if not repo.schema_present():
        raise SystemExit("admission schema (051) is not applied — refusing")

    rows = _sources(conn)
    if not rows:
        raise SystemExit("no g0001 rows matched — refusing to seal an empty "
                         "enumeration")

    exposures = {}
    for row in rows:
        exposures.setdefault(row[1], row[6])

    summary = {
        "sources": len(rows),
        "exposures": len(exposures),
        "entries_written": 0,
        "exposures_created": 0,
        "l2files_created": 0,
        "manifest_id": None,
        "sealed": False,
        "data_class_set": False,
        "dry_run": dry_run,
    }

    if dry_run:
        # A DRY RUN OPENS NOTHING. The `derived.*` functions' convention is
        # that a dry run reports what a real one would do without doing any of
        # it; opening a manifest to "preview" would leave an unsealed manifest
        # behind on every preview, which is precisely the state the seal
        # ordering exists to make rare and legible.
        print("DRY RUN — would register:")
        print("  manifest_key    : %s" % MANIFEST_KEY)
        print("  source_scope    : %s" % SOURCE_SCOPE)
        print("  release_identity: %s" % RELEASE_IDENTITY)
        print("  data_class      : %s" % DATA_CLASS)
        print("  byte_custody    : %s" % BYTE_CUSTODY)
        print("  manifest entries: %d" % len(rows))
        print("  exposures       : %d" % len(exposures))
        print("  l2file admissions: %d" % len(rows))
        first = rows[0]
        print("  first entry     : s3://%s/%s (checksum %s, rid %s, "
              "expid %s, sca %s)"
              % (BUCKET, _key_of(first[4]), first[3], first[0], first[1],
                 first[2]))
        # The audited function's OWN dry run, so the preview exercises the
        # real registration path (including its RA001 conflict checks) rather
        # than only describing it.
        summary["release"] = _register_release(conn, dry_run=True)
        print("  release preview : %s" % summary["release"])
        return summary

    # ORDER MATTERS: the audited registration is granted to the ORCHESTRATOR
    # role, the table writes to `rapid_pipeline_write`. Register first, under
    # the login role, then assume the writer tier for the inserts — assuming
    # it earlier would lose the EXECUTE on the audited function.
    summary["release"] = _register_release(conn, dry_run=False)
    _assume_writer_tier(conn)

    created_id = _open_classified_manifest(conn, DATA_CLASS)
    summary["data_class_set"] = created_id is not None

    # Read it back with `manifest_by_key`, NOT `open_manifest`.
    #
    # THIS IS NOT A PREFERENCE, IT IS THE ONLY ONE OF THE TWO THAT WORKS HERE,
    # and the reason is a latent defect worth recording rather than routing
    # silently around: `open_manifest`'s idempotent branch is
    # `ON CONFLICT (manifest_key) DO UPDATE SET manifest_key = EXCLUDED.
    # manifest_key`, a no-op write whose only purpose is to make RETURNING
    # fire on a conflict. A no-op it may be, but Postgres still requires
    # table-wide UPDATE to run it, and 051 grants the writer tier only
    # `UPDATE (sealed_at, entry_count, entries_checksum)`. So `open_manifest`
    # succeeds the first time (no conflict, so the DO UPDATE never executes)
    # and is refused with "permission denied" on every re-open — the method
    # is unusable under the exact tier it exists for, on exactly the
    # re-entrant path its docstring advertises. Recorded as a finding for the
    # repository's owner; this script does not paper over it by widening the
    # grant.
    manifest_row = repo.manifest_by_key(MANIFEST_KEY)
    manifest_id = manifest_row["manifest_id"]
    summary["manifest_id"] = manifest_id

    if not manifest_row["sealed"]:
        for row in rows:
            repo.add_manifest_entry(
                manifest_id, BUCKET, _key_of(row[4]), row[3],
                checksum_algorithm="md5")
            summary["entries_written"] += 1
        sealed = repo.seal_manifest(manifest_id)
        summary["sealed"] = sealed.sealed
    else:
        # Already sealed by a previous run: entries are frozen and the
        # admissions below are the only thing left that could be incomplete.
        summary["sealed"] = True

    for expid, dateobs in sorted(exposures.items()):
        try:
            admission = repo.admit_exposure(
                dateobs=dateobs, expid=expid,
                facts={"generation": GENERATION, "source": SOURCE_SCOPE},
                release_identity=RELEASE_IDENTITY,
                manifest_id=manifest_id)
        except AdmissionConflict as exc:
            # Same refusal as the l2file grain below. Not reachable from a
            # re-run of THIS script — every exposure's facts are the same two
            # literals, so a replay agrees with itself by construction — but
            # an exposure admitted by someone else under different facts for
            # the same `dateobs` would land here, and that is precisely the
            # case that must stop rather than be papered over.
            raise SystemExit(
                "REFUSING: %s — an existing exposure admission disagrees "
                "with the facts this backfill would record, which it must "
                "never overwrite" % exc)
        if admission.created:
            summary["exposures_created"] += 1

    for row in rows:
        rid, expid, sca, checksum = row[0], row[1], row[2], row[3]
        try:
            admission = repo.admit_l2file(
                exposure=expid, sca=sca, source_checksum=checksum, rid=rid,
                facts={"generation": GENERATION, "source": SOURCE_SCOPE,
                       "field": row[7], "fid": row[8]},
                release_identity=RELEASE_IDENTITY,
                manifest_id=manifest_id,
                checksum_algorithm="md5")
        except AdmissionConflict as exc:
            raise SystemExit(
                "REFUSING: %s — an existing admission disagrees with the "
                "legacy row, which this backfill must never overwrite" % exc)
        if admission.created:
            summary["l2files_created"] += 1

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="report what would be written, write nothing")
    parser.add_argument("--execute", action="store_true", default=False,
                        help="actually write (required; dry run is the "
                             "default posture)")
    args = parser.parse_args()

    if not args.execute and not args.dry_run:
        parser.error("pass --dry-run or --execute explicitly")
    if args.execute and args.dry_run:
        parser.error("--dry-run and --execute are mutually exclusive")

    import psycopg2
    conn = psycopg2.connect(
        host=os.environ["PGHOST"], port=os.environ.get("PGPORT", "6432"),
        user=os.environ["PGUSER"], dbname=os.environ.get("PGDATABASE", "rapid"),
        password=os.environ["PGPASSWORD"])
    try:
        summary = backfill(conn, dry_run=not args.execute)
        if args.execute:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        # RESET THE ROLE BEFORE HANDING THE CONNECTION BACK, on every path.
        # `SET ROLE` is session state and `rollback()` does not undo it, so a
        # failure anywhere after `_assume_writer_tier()` would return a
        # backend to pgbouncer's pool still elevated to `rapid_pipeline_write`
        # — for whichever client is handed it next, not just for this script.
        # `_reset_role()` at the top of `backfill()` already makes THIS script
        # self-healing; this makes it a good citizen of a shared pool, which
        # is the half that self-healing cannot cover.
        #
        # Best-effort and last: if the connection is already broken there is
        # no session left to reset, and raising here would mask the real
        # exception on its way out.
        try:
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
        except Exception:                                        # noqa: BLE001
            pass
        conn.close()

    print("SUMMARY: %s" % summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
