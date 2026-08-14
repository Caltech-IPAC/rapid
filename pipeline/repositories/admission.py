"""The admission repository.

The write path for the admission records DRAFT migration 051 adds:
`admission_manifests` / `admission_manifest_entries` (the sealed, replayable
source), `admission_exposures` and `admission_l2files` (one sidecar admission
record per admitted grain), and the read of `admission_release_pointer` that
stamps each admission with the release it was admitted under.

**WHY A REPOSITORY AND NOT A `RAPIDDB` METHOD.** `RAPIDDB` is frozen (rule 17;
brief G's ratified merge decision). It is the legacy handle whose methods set
an `exit_code` attribute instead of raising — and admission is where that
costs most: `add_exposure` (`rapid_db.py:474-531`) sets `exit_code = 67` and
bare-`return`s on failure, leaving `self.expid` at `None`, and **not one of
the three ingest scripts checks it**. That `None` then flows on as the L2
insert's `expid`. A typed raise is the whole difference, and
`pipeline/repositories/errors.py` exists to make it.

**THIS REPOSITORY NEVER COMMITS AND NEVER OPENS A CONNECTION.** It takes the
connection the caller's transaction is already running on, so the manifest,
its entries, the admission rows and whatever else the ingest writes commit
together or roll back together. A repository that opened its own connection
could not be in one transaction with the caller, and the sealed-manifest
ordering below would stop being a guarantee.

**IDEMPOTENCE IS THE DATABASE'S, NOT THIS MODULE'S.** Every admission insert
is `INSERT ... ON CONFLICT (<natural key>) DO ... RETURNING` against a real
constraint, never a SELECT-then-INSERT. That is the direct repair of
`addexposure`'s shape (`008-functions.sql:290-293`): two concurrent admissions
of one observation both read NULL there and both insert, so the loser takes a
unique violation instead of RECEIVING THE EXISTING ADMISSION. Here the loser
receives it, which is what rule 20 asks for.

**A REPEAT RETURNS; IT NEVER MUTATES.** The `ON CONFLICT` action is
deliberately `DO UPDATE SET admission_identity = EXCLUDED.admission_identity`
— a no-op write of the value to itself, used ONLY because `DO NOTHING`
returns no row and this path must return the existing admission. Nothing else
is touched, and 051's write-once triggers on `admitted_at` and
`admission_identity` are the backstop that makes that structural rather than
careful: whatever a future edit tries, the moment of first admission cannot be
rewritten. That is the direct repair of `addexposure`'s `else` branch, which
updates every field including `created = now()` (`008-functions.sql:331-345`)
and destroys the original ingest timestamp unrecoverably.

**CONFLICTS ARE REFUSED, NOT ABSORBED.** Rule 20 says a repeat returns its
existing admission; it does not say a repeat may redefine it. So the same
`dateobs` arriving with different observational facts, and the same
`(expid, sca)` arriving with a different source checksum, both raise
`AdmissionConflict` naming both values. Neither overwrites and neither
silently accepts.

**DRAFT-051-GATED OBJECTS ARE PROBED, NEVER CAUGHT.** `schema_present()` asks
`to_regclass` before any admission statement runs. Catching `UndefinedTable`
instead would put the CALLER'S OPEN TRANSACTION into an aborted state to be
discovered later, and recovering by `conn.rollback()` — what the `RAPIDDB`
revisions of the other carves did — would discard writes the caller had
already made and had not finished with (`alert_outbox.py:55-64` states this at
length). The probe spelling is `to_regclass`/`information_schema`, matching
the three probes already in this package, rather than the `pg_proc` spelling
the brief names; `pg_proc` IS used for the one function probe, where no
`to_regclass` equivalent exists. Recorded in `notes-h-proposals.md` as P-H1.

**AND THE DEGRADED PATH FAILS CLOSED.** When 051 is absent the repository
REFUSES TO ADMIT rather than falling back to the legacy stored procedures. The
brief fixes this: "it refuses to admit rather than silently minting a
duplicate". A fallback that quietly called `addl2file` would reintroduce the
`max(version)+1` duplicate-minting this package exists to remove, at exactly
the moment nobody was watching.
"""

import json
import typing

from pipeline.repositories import admission_identity as identity
from pipeline.repositories.errors import RepositoryQueryFailed

#: SQLSTATE 051 raises for its own refusals — the sealed-manifest precondition
#: and the two write-once triggers. Classified by code, never by message text,
#: the discipline `pipeline/operatorctl/contract.py` established for RA001 and
#: RA002 and for the same reason: message text is a presentation detail.
SQLSTATE_ADMISSION_INVARIANT = "RA010"

#: PL/pgSQL's bare `RAISE EXCEPTION`. 051 uses the explicit RA010 above, but a
#: trigger written later without `USING ERRCODE` would surface as P0001, and
#: treating that as a query failure would put an invariant violation in the
#: category callers may treat as retryable.
SQLSTATE_RAISE_EXCEPTION = "P0001"

#: Does DRAFT 051's admission schema exist? Asked before any admission
#: statement, so a missing draft never aborts the caller's transaction.
_SCHEMA_PROBE = (
    "SELECT to_regclass('public.admission_exposures') IS NOT NULL"
    "   AND to_regclass('public.admission_l2files') IS NOT NULL"
    "   AND to_regclass('public.admission_manifests') IS NOT NULL"
    "   AND to_regclass('public.admission_release_pointer') IS NOT NULL")

#: The one place a FUNCTION rather than a relation is probed, by exact
#: signature. `to_regclass` has no function equivalent, so this is `pg_proc`.
_RELEASE_MUTATION_PROBE = (
    "SELECT EXISTS ("
    "  SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
    "   WHERE n.nspname = 'derived' AND p.proname = 'set_admission_release')")


class AdmissionError(Exception):
    """Base for admission's own refusals."""

    error_category = "admission_error"


class AdmissionSchemaAbsent(AdmissionError):
    """DRAFT 051 is not applied, so admission cannot be idempotent.

    A REFUSAL, NOT A FALLBACK. Admitting through the legacy stored procedures
    would mint duplicates at the L2 grain by construction
    (`addl2file`'s `coalesce(max(version), 0) + 1` against a uniqueness that
    includes the version), which is the defect rule 20 names. Refusing is the
    fail-closed half the brief requires.
    """

    error_category = "admission_schema_absent"


class AdmissionConflict(AdmissionError):
    """A repeat arrived that would REDEFINE an existing admission.

    Names both values, because an operator reading this needs to know what
    disagreed and not merely that something did. Raised rather than
    overwriting (which is what `addexposure` does) and rather than silently
    accepting (which is what a bare `ON CONFLICT DO NOTHING` would do).
    """

    error_category = "admission_conflict"

    def __init__(self, grain, field, existing, arriving, identity_=None):
        super().__init__(
            "admission conflict at the %s grain: %s already admitted as %r, "
            "arriving as %r. A repeated observation RETURNS its existing "
            "admission (rule 20); it does not redefine it, so this is "
            "refused rather than overwritten. Resolve the disagreement at "
            "the source." % (grain, field, existing, arriving))
        self.grain = grain
        self.field = field
        self.existing = existing
        self.arriving = arriving
        self.admission_identity = identity_


class ManifestSealRaced(AdmissionError):
    """`seal_manifest`'s own CAS matched zero rows unexpectedly.

    Defense-in-depth (wave-E finding #7): `seal_manifest` reads
    `sealed_at IS NULL` under no lock, then races that same predicate into
    the sealing `UPDATE ... WHERE sealed_at IS NULL`. Nothing in the current
    ingest path calls `seal_manifest` concurrently for one `manifest_id` — a
    manifest belongs to one ingest — so this is unreachable today rather than
    a live race being exploited. But an `UPDATE` with no `RETURNING` and no
    rowcount check cannot tell "I sealed it" from "someone else sealed it
    between my read and my write, and I just silently no-opped", and the
    caller got `ManifestRecord(..., sealed=True, ...)` either way — a wrong
    answer to "did MY seal happen" that a future caller relying on that
    return value (rather than only on the row's final state) could act on
    incorrectly. Raised rather than assumed away, so the day something
    reachable races this path, it fails loud instead of lying.
    """

    error_category = "admission_manifest_seal_raced"

    def __init__(self, manifest_id):
        super().__init__(
            "seal_manifest's CAS for manifest %r matched zero rows: the "
            "row was not found with sealed_at IS NULL at UPDATE time, even "
            "though it was NULL moments earlier at read time. Concurrent "
            "sealing of one manifest is not a supported path — investigate "
            "rather than retry." % (manifest_id,))
        self.manifest_id = manifest_id


class ManifestNotSealed(AdmissionError):
    """An admission cited a manifest that is not sealed.

    The crash-ordering guarantee: a manifest is sealed only once every entry
    is durable, so citing an unsealed one would record an admission against a
    source that may still be partial.
    """

    error_category = "admission_manifest_unsealed"


class ReleasePointerUnset(AdmissionError):
    """No current release pointer, so an admission cannot be stamped.

    Fail-closed: rule 18 requires the admission to carry the release it was
    admitted under, and an unstamped admission would silently reintroduce the
    gap this package closes.
    """

    error_category = "admission_release_unset"


class Admission(typing.NamedTuple):
    """One admission record, as returned by the repository."""

    admission_id: int
    admission_identity: str
    release_identity: str
    admitted_at: object
    created: bool          # True when this call inserted it


class ManifestRecord(typing.NamedTuple):
    """One source manifest."""

    manifest_id: int
    manifest_key: str
    sealed: bool
    entry_count: int


class AdmissionRepository:
    """Admission writes over a connection the caller owns."""

    def __init__(self, conn):
        self._conn = conn

    # -- availability ---------------------------------------------------

    def schema_present(self):
        """Is DRAFT 051 applied? Asked before anything else touches it."""
        rows = self._query("schema_present", _SCHEMA_PROBE, ())
        return bool(rows and rows[0][0])

    def release_mutation_present(self):
        """Is `derived.set_admission_release` installed?"""
        rows = self._query("release_mutation_present",
                           _RELEASE_MUTATION_PROBE, ())
        return bool(rows and rows[0][0])

    def _require_schema(self):
        if not self.schema_present():
            raise AdmissionSchemaAbsent(
                "DRAFT 051's admission schema is not applied, so admission "
                "cannot be idempotent against a database constraint. "
                "REFUSING TO ADMIT rather than falling back to the legacy "
                "stored procedures, which mint a duplicate admission row for "
                "every re-ingest at the L2 grain "
                "(addl2file's coalesce(max(version), 0) + 1 against a "
                "uniqueness that includes the version). Apply 051 before "
                "ingesting.")

    # -- the release pointer --------------------------------------------

    def current_release(self):
        """The release future admissions are stamped with.

        READ ONCE PER SEALED MANIFEST BY THE CALLER, not once per admission —
        the linearization the brief fixes. A pointer switch mid-run must not
        split one manifest across two releases, so the ingest reads this at
        the start of a manifest's admission and passes the same value to every
        admission from it.
        """
        self._require_schema()
        rows = self._query(
            "current_release",
            "SELECT release_identity FROM admission_release_pointer"
            " WHERE is_current", ())
        if not rows:
            raise ReleasePointerUnset(
                "no current admission release pointer is set, so an "
                "admission cannot record the release it was admitted under "
                "(rule 18). Set one with `rapidctl set-admission-release` "
                "before ingesting.")
        return rows[0][0]

    def register_release(self, release_identity, manifest_uri=None,
                         manifest_checksum=None):
        """Record a release identity as known and resolvable.

        Idempotent: re-registering the same identity is a no-op rather than an
        error, so an ingest that runs twice does not fail on its own release.
        """
        self._require_schema()
        self._query(
            "register_release",
            "INSERT INTO admission_releases"
            " (release_identity, manifest_uri, manifest_checksum)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (release_identity) DO NOTHING",
            (release_identity, manifest_uri, manifest_checksum))
        return release_identity

    # -- the sealed source manifest -------------------------------------

    def open_manifest(self, manifest_key, source_scope, release_identity,
                      byte_custody):
        """Create an UNSEALED manifest, or return the existing one.

        UNSEALED IS THE ONLY STATE A MANIFEST CAN BE CREATED IN, and that is
        the crash-ordering guarantee rather than a default: entries are
        written next, and `seal_manifest` runs only once they are all durable.
        A crash anywhere in between leaves an explicitly unsealed manifest,
        which `admit_*` refuses to cite.
        """
        self._require_schema()
        if byte_custody not in ("pipeline-retained", "external-versioned",
                                "none"):
            raise AdmissionError(
                "byte_custody must state what the replay guarantee rests on: "
                "'pipeline-retained', 'external-versioned' or 'none'; got %r"
                % (byte_custody,))
        rows = self._query(
            "open_manifest",
            "INSERT INTO admission_manifests"
            " (manifest_key, source_scope, release_identity, byte_custody)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (manifest_key) DO UPDATE"
            "    SET manifest_key = EXCLUDED.manifest_key"
            " RETURNING manifest_id, manifest_key, sealed_at,"
            "           coalesce(entry_count, 0)",
            (manifest_key, source_scope, release_identity, byte_custody))
        row = rows[0]
        return ManifestRecord(row[0], row[1], row[2] is not None, row[3])

    def add_manifest_entry(self, manifest_id, source_bucket, source_key,
                           source_checksum, source_version_id=None,
                           checksum_algorithm="sha256", source_bytes=None):
        """Enumerate one source object into an unsealed manifest.

        Refuses once the manifest is sealed: a sealed manifest's entry list is
        what its checksum was computed over, and appending afterwards would
        make the seal a statement about something else.
        """
        self._require_schema()
        digest, algorithm = identity.normalized_checksum(source_checksum,
                                                         checksum_algorithm)
        rows = self._query(
            "add_manifest_entry",
            "SELECT sealed_at FROM admission_manifests WHERE manifest_id = %s",
            (manifest_id,))
        if not rows:
            raise AdmissionError("no manifest %r" % (manifest_id,))
        if rows[0][0] is not None:
            raise ManifestNotSealed(
                "manifest %s is already SEALED and cannot take further "
                "entries; its entries checksum was computed over the list as "
                "it stood at sealing" % (manifest_id,))
        self._query(
            "add_manifest_entry",
            "INSERT INTO admission_manifest_entries"
            " (manifest_id, source_bucket, source_key, source_checksum,"
            "  checksum_algorithm, source_version_id, source_bytes)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (manifest_id, source_bucket, source_key) DO UPDATE"
            "    SET source_checksum = EXCLUDED.source_checksum,"
            "        checksum_algorithm = EXCLUDED.checksum_algorithm,"
            "        source_version_id = EXCLUDED.source_version_id,"
            "        source_bytes = EXCLUDED.source_bytes",
            (manifest_id, source_bucket, source_key, digest, algorithm,
             source_version_id, source_bytes))
        return digest

    def seal_manifest(self, manifest_id):
        """Seal a complete manifest — the LAST write of the enumeration.

        Computes the entry count and a checksum over the canonical
        serialization of the entry list, and records both. Ordering is the
        whole point: everything the manifest describes is durable before the
        seal, so a crash cannot produce a sealed manifest with partial
        entries.

        Idempotent — re-sealing an already-sealed manifest returns its
        recorded state rather than recomputing, so a retried ingest converges.
        """
        self._require_schema()
        rows = self._query(
            "seal_manifest",
            "SELECT sealed_at, entry_count, entries_checksum"
            "  FROM admission_manifests WHERE manifest_id = %s",
            (manifest_id,))
        if not rows:
            raise AdmissionError("no manifest %r" % (manifest_id,))
        if rows[0][0] is not None:
            return ManifestRecord(manifest_id, None, True, rows[0][1])

        entries = self._query(
            "seal_manifest",
            "SELECT source_bucket, source_key, source_checksum,"
            "       checksum_algorithm, coalesce(source_version_id, '')"
            "  FROM admission_manifest_entries WHERE manifest_id = %s"
            " ORDER BY source_bucket, source_key",
            (manifest_id,))
        if not entries:
            raise AdmissionError(
                "manifest %s has no entries; sealing an empty enumeration "
                "would record a complete source that describes nothing"
                % (manifest_id,))
        # ORDERED IN SQL, not in Python: the checksum is over a canonical
        # order, and letting the driver's row order reach it would make the
        # seal depend on the database's return order.
        canonical = json.dumps(
            [list(entry) for entry in entries],
            sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        import hashlib
        digest = "sha256:" + hashlib.sha256(
            canonical.encode("utf-8")).hexdigest()
        # RETURNING, not a bare rowcount-less UPDATE (wave-E finding #7): the
        # CAS's `WHERE ... sealed_at IS NULL` guards against a concurrent
        # sealer, but `self._query` never surfaces `cur.rowcount` for a
        # RETURNING-less UPDATE — every prior version of this statement
        # therefore returned success whether or not the CAS actually matched
        # a row. Checking `rows` (not merely calling the query) is what makes
        # the guard real.
        sealed = self._query(
            "seal_manifest",
            "UPDATE admission_manifests"
            "   SET sealed_at = now(), entry_count = %s, entries_checksum = %s"
            " WHERE manifest_id = %s AND sealed_at IS NULL"
            " RETURNING manifest_id",
            (len(entries), digest, manifest_id))
        if not sealed:
            raise ManifestSealRaced(manifest_id)
        return ManifestRecord(manifest_id, None, True, len(entries))

    def manifest_by_key(self, manifest_key):
        """One manifest by its run key, with the release it was opened under.

        **THE RELEASE IS READ BACK OFF THE MANIFEST ROW, NEVER RE-READ FROM
        THE POINTER.** This is what lets a worker process that did not run
        `begin_admission_run` join a run already in progress without breaking
        the linearization: the manifest recorded its release once, when the
        run began, so every worker resolving through here gets THAT value and
        not whatever the pointer says now. Re-reading the pointer would be
        exactly the torn-manifest state the brief forbids.
        """
        self._require_schema()
        rows = self._query(
            "manifest_by_key",
            "SELECT manifest_id, manifest_key, release_identity, sealed_at,"
            "       coalesce(entry_count, 0)"
            "  FROM admission_manifests WHERE manifest_key = %s",
            (manifest_key,))
        if not rows:
            return None
        row = rows[0]
        return {"manifest_id": row[0], "manifest_key": row[1],
                "release_identity": row[2], "sealed": row[3] is not None,
                "entry_count": row[4]}

    def manifest_entries(self, manifest_id):
        """The enumerated sources, for a replay."""
        self._require_schema()
        rows = self._query(
            "manifest_entries",
            "SELECT source_bucket, source_key, source_checksum,"
            "       checksum_algorithm, source_version_id, source_bytes"
            "  FROM admission_manifest_entries WHERE manifest_id = %s"
            " ORDER BY source_bucket, source_key",
            (manifest_id,))
        return [dict(zip(("source_bucket", "source_key", "source_checksum",
                          "checksum_algorithm", "source_version_id",
                          "source_bytes"), row)) for row in rows]

    # -- admission, per grain -------------------------------------------

    def admit_exposure(self, *, dateobs, expid, facts, release_identity,
                       manifest_id=None):
        """Admit one exposure, or return its existing admission.

        Identity is `dateobs` ALONE — no checksum participates, because an
        exposure is an observational fact and not a file
        (`admission_identity.exposure_payload`).

        `facts` is every parsed admission fact, stored so a replay
        reconstructs the row from recorded facts rather than by re-parsing
        source bytes that may no longer exist. It is compared on a repeat: the
        same `dateobs` arriving with DIFFERENT observational facts is refused,
        naming the disagreeing field and both values.
        """
        self._require_schema()
        identity_, _payload = identity.exposure_identity(dateobs)
        existing = self._query(
            "admit_exposure",
            "SELECT admission_id, admission_identity, release_identity,"
            "       admitted_at, admitted_facts, expid"
            "  FROM admission_exposures WHERE admission_identity = %s",
            (identity_,))
        if existing:
            row = existing[0]
            self._refuse_fact_conflict("exposure", row[4], facts, identity_)
            return Admission(row[0], row[1], row[2], row[3], created=False)

        rows = self._query(
            "admit_exposure",
            "INSERT INTO admission_exposures"
            " (admission_identity, expid, manifest_id, release_identity,"
            "  admitted_facts)"
            " VALUES (%s, %s, %s, %s, %s)"
            # DO UPDATE rather than DO NOTHING, and the update is the identity
            # to itself: DO NOTHING returns no row, and this path must RETURN
            # THE EXISTING ADMISSION to a concurrent loser rather than raise.
            # Nothing else is touched, and 051's write-once triggers are the
            # structural backstop.
            " ON CONFLICT (admission_identity) DO UPDATE"
            "    SET admission_identity = EXCLUDED.admission_identity"
            " RETURNING admission_id, admission_identity, release_identity,"
            "           admitted_at, (xmax = 0) AS inserted",
            (identity_, expid, manifest_id, release_identity,
             json.dumps(facts, sort_keys=True)))
        row = rows[0]
        return Admission(row[0], row[1], row[2], row[3], created=bool(row[4]))

    def admit_l2file(self, *, exposure, sca, source_checksum, rid, facts,
                     release_identity, manifest_id=None,
                     checksum_algorithm="sha256"):
        """Admit one L2 detector file, or return its existing admission.

        Identity is a content key over `(expid, sca)` plus the source
        checksum. The `(expid, sca)` UNIQUE in 051 is the natural key this
        grain has never had: `l2filespk` includes the version, which is
        exactly what lets `addl2file`'s `max+1` mint a duplicate.

        Same `(expid, sca)` with a DIFFERENT checksum is refused — never
        silently re-versioned, which is what the legacy path does.
        """
        self._require_schema()
        digest, algorithm = identity.normalized_checksum(source_checksum,
                                                         checksum_algorithm)
        identity_, _payload = identity.l2file_identity(
            exposure=exposure, sca=sca, source_checksum=digest,
            checksum_algorithm=algorithm)

        # THE GRAIN IS CHECKED BEFORE THE IDENTITY. A different checksum for
        # the same (expid, sca) is a CONFLICT to report, not a new admission
        # to attempt — and attempting it would take a unique violation on
        # `admission_l2files_grain_uq` that says far less than this does.
        grain = self._query(
            "admit_l2file",
            "SELECT admission_id, admission_identity, release_identity,"
            "       admitted_at, source_checksum, admitted_facts"
            "  FROM admission_l2files WHERE expid = %s AND sca = %s",
            (int(exposure), int(sca)))
        if grain:
            row = grain[0]
            if row[4] != digest:
                raise AdmissionConflict(
                    "l2file", "source checksum for (expid=%s, sca=%s)"
                    % (exposure, sca), row[4], digest, row[1])
            self._refuse_fact_conflict("l2file", row[5], facts, row[1])
            return Admission(row[0], row[1], row[2], row[3], created=False)

        rows = self._query(
            "admit_l2file",
            "INSERT INTO admission_l2files"
            " (admission_identity, rid, expid, sca, source_checksum,"
            "  checksum_algorithm, manifest_id, release_identity,"
            "  admitted_facts)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (admission_identity) DO UPDATE"
            "    SET admission_identity = EXCLUDED.admission_identity"
            " RETURNING admission_id, admission_identity, release_identity,"
            "           admitted_at, (xmax = 0) AS inserted",
            (identity_, rid, int(exposure), int(sca), digest, algorithm,
             manifest_id, release_identity, json.dumps(facts, sort_keys=True)))
        row = rows[0]
        return Admission(row[0], row[1], row[2], row[3], created=bool(row[4]))

    def _refuse_fact_conflict(self, grain, recorded, arriving, identity_):
        """Refuse a repeat that would redefine the admission's facts.

        Compares only keys present in BOTH records. A newly-recorded fact
        (an ingest that learned to parse one more header) is not a conflict —
        it is more information about the same observation — while a
        DISAGREEMENT on a shared key means two ingests believe different
        things about one observation, and neither this code nor the database
        can decide which is right.
        """
        if not recorded or not arriving:
            return
        if isinstance(recorded, str):
            recorded = json.loads(recorded)
        for key in sorted(set(recorded) & set(arriving)):
            old, new = recorded[key], arriving[key]
            # Numeric facts compare by value: a float that round-tripped
            # through jsonb as 1.0 and arrives as 1 is the same observation.
            if isinstance(old, (int, float)) and isinstance(new, (int, float)):
                if float(old) == float(new):
                    continue
            elif old == new:
                continue
            raise AdmissionConflict(grain, key, old, new, identity_)

    # -- replay ----------------------------------------------------------

    def admissions_for_manifest(self, manifest_id):
        """Every admission made from one sealed manifest, for a replay check."""
        self._require_schema()
        exposures = self._query(
            "admissions_for_manifest",
            "SELECT admission_identity, expid, release_identity, admitted_at"
            "  FROM admission_exposures WHERE manifest_id = %s"
            " ORDER BY admission_identity", (manifest_id,))
        l2files = self._query(
            "admissions_for_manifest",
            "SELECT admission_identity, rid, expid, sca, release_identity,"
            "       admitted_at"
            "  FROM admission_l2files WHERE manifest_id = %s"
            " ORDER BY admission_identity", (manifest_id,))
        return {"exposures": exposures, "l2files": l2files}

    # -- plumbing --------------------------------------------------------

    def _query(self, method, sql, params):
        """Execute and fetch, re-typing failures — except a raised invariant.

        NOT ROLLED BACK HERE. These calls run inside the ingest's own
        transaction, which owns the boundary. Rolling back here would discard
        writes the caller had already made and had not finished with — the
        reasoning `alert_outbox.py`'s `_query` records, and the defect the
        `RAPIDDB` revisions of the other carves shipped before they were
        refused.
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                return cur.fetchall()
        except Exception as exc:                      # noqa: BLE001 — re-typed
            if _is_invariant_violation(exc):
                # 051'S OWN RAISES PASS THROUGH UNWRAPPED — the unsealed
                # manifest refusal and the two write-once triggers. Each is a
                # violated invariant rather than a query that failed to run,
                # and wrapping them as `RepositoryQueryFailed` would put them
                # in the category callers may treat as retryable. They are
                # neither retryable nor recoverable.
                raise
            raise RepositoryQueryFailed(method, str(exc)) from exc


def _is_invariant_violation(exc):
    """Is this 051's own RAISE, rather than a query failure?

    Matched on SQLSTATE, never on message text: the message is written for an
    operator and may be reworded, while the code is the database's own
    classification. RA010 is what 051 raises explicitly; P0001 catches a
    trigger added later without an explicit `USING ERRCODE`.
    """
    code = getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None)
    return code in (SQLSTATE_ADMISSION_INVARIANT, SQLSTATE_RAISE_EXCEPTION)
