"""The bridge from the three pre-pipeline-convention ingest scripts to the
carved admission repository (rule 20).

**WHY A BRIDGE AND NOT A REWRITE.** `db_register_socsim_files.py`,
`db_register_rimtimsim_files.py` and `db_register_troxel_sim_files.py` predate
this repository's pipeline conventions: they are top-level procedural scripts
that build a `RAPIDDB` handle, parse FITS headers, and call stored procedures
directly. Rewriting them is not brief H's scope, and a rewrite would be a
large behavioural change to the live ingest path made at 3am by an unattended
worker.

What IS in scope, and what this module delivers: **all three production ingest
scripts write their admissions through the carved repository**, so the
criterion cannot pass against an isolated repository while production still
goes through `RAPIDDB` alone. This is the seam that makes that true with one
call per grain per script.

**THE CONNECTION IS BORROWED, NEVER OPENED.** `RAPIDDB` owns a connection
(`dbh.conn`); this module borrows it so the admission commits with whatever
else the script's transaction is doing. Opening a second connection would put
the admission in a different transaction from the legacy row it describes, and
a crash between them would leave exactly the split-brain state the sealed
manifest exists to prevent.

**FAILURE IS LOUD.** These helpers raise. The scripts they serve report
failure by `exit_code` and, in three places, by calling `exit(0)` on a
checksum failure — a failure reported to the scheduler as success (recorded as
proposal P-H2, not fixed here). An admission that could not be written must
not be one of those silences.
"""

import os

from pipeline.repositories.admission import (AdmissionConflict,
                                             AdmissionRepository,
                                             AdmissionSchemaAbsent,
                                             ReleasePointerUnset)

#: Set by the ingest driver once per run, from the pointer, so every admission
#: in one sealed manifest carries the SAME release even if an operator
#: switches the pointer mid-run. THE LINEARIZATION THE BRIEF FIXES: an
#: admission run reads the pointer once, at the start of the manifest, and a
#: pointer switch does not split one manifest across two releases.
_RUN_RELEASE = {"identity": None, "manifest_id": None}


def begin_admission_run(dbh, manifest_key=None, source_scope="socsim",
                        byte_custody="external-versioned"):
    """Read the release pointer ONCE and open the run's source manifest.

    Called at the start of an ingest run. Returns the release identity every
    admission in this run will carry.

    A missing pointer is FATAL here rather than defaulted: rule 18 requires the
    admission to record the release it was admitted under, and an unstamped
    admission silently reintroduces the gap this package closes.
    """
    repo = AdmissionRepository(dbh.conn)
    release = repo.current_release()
    _RUN_RELEASE["identity"] = release

    if manifest_key:
        manifest = repo.open_manifest(manifest_key, source_scope, release,
                                      byte_custody)
        _RUN_RELEASE["manifest_id"] = manifest.manifest_id
    return release


def seal_admission_run(dbh):
    """Seal the run's manifest — the LAST write of the enumeration.

    Ordering is the guarantee: every entry is durable before the seal, so a
    crash leaves either a complete replayable record or an explicitly unsealed
    one, never a sealed manifest whose entries are partial.
    """
    manifest_id = _RUN_RELEASE.get("manifest_id")
    if manifest_id is None:
        return None
    repo = AdmissionRepository(dbh.conn)
    sealed = repo.seal_manifest(manifest_id)
    dbh.conn.commit()
    return sealed


def enumerate_source(dbh, bucket, key, checksum, version_id=None,
                     size=None, algorithm="sha256"):
    """Record one source object in the run's UNSEALED manifest.

    Called as each input is discovered, BEFORE its admission. The version
    reference is what lets a replay name the exact bytes rather than whatever
    now sits at that key; where the input bucket is unversioned it is None and
    the manifest's `byte_custody` says what the guarantee actually rests on.
    """
    manifest_id = _RUN_RELEASE.get("manifest_id")
    if manifest_id is None:
        return None
    repo = AdmissionRepository(dbh.conn)
    return repo.add_manifest_entry(manifest_id, bucket, key, checksum,
                                   source_version_id=version_id,
                                   checksum_algorithm=algorithm,
                                   source_bytes=size)


def record_exposure_admission(dbh, dateobs, expid, facts):
    """Admit one exposure, or return its existing admission.

    Identity is `dateobs` ALONE — no checksum — because an exposure is an
    observational fact and not a file, and ingestion is per-detector-file so
    there is no exposure-level file whose checksum could enter it.
    """
    repo = AdmissionRepository(dbh.conn)
    if not repo.schema_present():
        # FAIL CLOSED. Falling back to the legacy path would reintroduce the
        # duplicate-minting this package removes, at the moment nobody is
        # watching.
        raise AdmissionSchemaAbsent(
            "DRAFT 051 is not applied; refusing to ingest rather than "
            "admitting without database-enforced idempotency")
    return repo.admit_exposure(
        dateobs=dateobs, expid=expid, facts=_clean(facts),
        release_identity=_release(),
        manifest_id=_RUN_RELEASE.get("manifest_id"))


def record_l2file_admission(dbh, exposure, sca, source_checksum, rid, facts,
                            checksum_algorithm="sha256"):
    """Admit one L2 detector file, or return its existing admission.

    Identity is a content key over `(expid, sca)` plus the source checksum —
    the grain where a file, and therefore a checksum, exists. A repeat returns;
    a DIFFERENT checksum for the same `(expid, sca)` is refused rather than
    re-versioned, which is what `addl2file`'s `max(version) + 1` does today.
    """
    repo = AdmissionRepository(dbh.conn)
    if not repo.schema_present():
        raise AdmissionSchemaAbsent(
            "DRAFT 051 is not applied; refusing to ingest rather than "
            "minting a duplicate L2 admission")
    return repo.admit_l2file(
        exposure=exposure, sca=sca, source_checksum=source_checksum, rid=rid,
        facts=_clean(facts), release_identity=_release(),
        manifest_id=_RUN_RELEASE.get("manifest_id"),
        checksum_algorithm=checksum_algorithm)


def _release():
    identity = _RUN_RELEASE.get("identity")
    if not identity:
        raise ReleasePointerUnset(
            "begin_admission_run() has not been called, so this ingest has "
            "no release to stamp its admissions with. The pointer is read "
            "ONCE per run so a switch mid-run cannot split one manifest "
            "across two releases (rule 18).")
    return identity


def _clean(facts):
    """Drop None-valued facts so an absent value is not recorded as a fact.

    A recorded `None` would compare unequal to a later real value and turn a
    newly-parsed header into a spurious admission conflict.
    """
    return {key: value for key, value in (facts or {}).items()
            if value is not None}
