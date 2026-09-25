"""Scratch-run deletion, expiry and pinning: the cleanup role.

Implements the runs page's "Deletion"
(https://roman-rapid.readthedocs.io/en/latest/system/runs.html): "After
the deleting state commits, cleanup idempotently removes the run's object
versions and run-scoped science rows, then records completion." The
fence itself -- scratch only, owner only, no unresolved attempts, no
binding from outside the run -- is
:func:`rapidpipe.runs.repository.mark_run_deleting`; this module commits
that state first, then does the physical cleanup, then
:func:`~rapidpipe.runs.repository.mark_run_deleted`.

Unlike ``rapidpipe.runs.repository``, :func:`delete_run` and
:func:`expire_runs` COMMIT on the connection they are given, so they must
be called on a connection with no pending caller work. The ``deleting``
state is committed before any object or row is removed; then S3 is
emptied; then the science-row deletes, the instance marking and
``mark_run_deleted`` run as ONE transaction, committed once. A failure
anywhere after the first commit leaves the run ``deleting``, and a second
:func:`delete_run` call finishes it -- every step after that commit is
idempotent. :func:`pin_run` is an ordinary one-transaction repository
call; the caller commits.

What is removed, and only for rows whose ``run`` is this run:

- every S3 object version and delete marker under each attempt's
  ``s3://`` output location, which must be in the scratch bucket and
  under ``runs/<run>/`` (checked for every attempt before anything is
  marked or removed);
- ``dev`` science rows in ``sources`` (the inheritance parent: a DELETE
  on it reaches every ``sources_<date>_<sca>`` child, which carry ``run``
  since 20260923-04), ``diffimmeta``, ``diffimages``, ``l2filemeta``,
  ``l2files``, ``refimages`` and ``psfs``, in that order -- each table
  before the table its foreign keys point at (baseline:
  ``sources.pid`` and ``diffimmeta.pid`` -> ``diffimages``;
  ``diffimages.rid`` -> ``l2files``, ``diffimages.rfid`` -> ``refimages``;
  ``l2filemeta.rid`` -> ``l2files``; ``psfs`` has no inbound key).

``refimimages``, ``refimcatalogs`` and ``refimmeta`` carry no ``run``
column; they belong to a run through their ``rfid``
(:data:`RFID_SCOPED_TABLES`). Their rows whose ``rfid`` is one of this
run's `refimages` rows are deleted first, in the same transaction, since
`register` writes them with that run's reference image (supervisor step
8, ruling R7). ``xsources`` carries no ``run`` column and is never
cleaned. A row that references one of this run's rows and is not itself
cleaned with the run -- an ``xsources`` row, a `refimimages` row of
another run's reference naming this run's l2 image, a science row of
another run, or a ``dev`` row with no run -- blocks deletion: it is found
by a preflight and refused before the run is marked
(``_blocking_references``).

Run-model rows (runs, units, attempts, execution records, instances,
members, result sets, dependencies, promotions) are tombstones and are
never deleted; the run's instances are marked ``deletion_state =
'deleted'``.

This module composes ``rapidpipe.runs.repository`` and
``rapidpipe.products.storage`` only (``rapidpipe.runs`` may not import
``rapidpipe.launch``; see ``tests/unit/test_dependency_direction.py``), so
the scratch outputs-root lookup ``rapidpipe.launch.batch.outputs_root_for``
does is repeated here for the one environment pair it needs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg2.extensions

from rapidpipe.products.storage import parse_location
from rapidpipe.runs.repository import (
    DeletionRefused,
    RunDeletingOrDeleted,
    RunModelError,
    RunNotFound,
    mark_run_deleted,
    mark_run_deleting,
)

#: Run-scoped ``dev`` science tables, in foreign-key-safe delete order
#: (children before the tables they reference; module docstring).
SCIENCE_TABLES = (
    "sources",
    "diffimmeta",
    "diffimages",
    "l2filemeta",
    "l2files",
    "refimages",
    "psfs",
)

#: Satellite tables of `refimages` with no ``run`` column, cleaned with
#: the run that owns their ``rfid`` (module docstring), before
#: :data:`SCIENCE_TABLES`: ``refimimages`` also references ``l2files``.
RFID_SCOPED_TABLES = ("refimimages", "refimcatalogs", "refimmeta")

#: Every foreign key in the baseline that points at a run-scoped science
#: table, as (referencing table, its column, referenced table, its key
#: column). A referencing row that does not belong to the run being
#: deleted blocks the deletion (module docstring). The first four have no
#: ``run`` column at all, so any referencing row blocks.
_INBOUND_REFERENCES = (
    ("xsources", "pid", "diffimages", "pid"),
    ("refimimages", "rid", "l2files", "rid"),
    ("refimimages", "rfid", "refimages", "rfid"),
    ("refimcatalogs", "rfid", "refimages", "rfid"),
    ("refimmeta", "rfid", "refimages", "rfid"),
    ("sources", "pid", "diffimages", "pid"),
    ("diffimmeta", "pid", "diffimages", "pid"),
    ("diffimages", "rid", "l2files", "rid"),
    ("diffimages", "rfid", "refimages", "rfid"),
    ("l2filemeta", "rid", "l2files", "rid"),
)
_RUNLESS_TABLES = ("xsources",)
#: A satellite row outside this run: its ``rfid`` is not one of this run's
#: `refimages` rows (``r`` is the referencing row, as in the preflight).
_OUTSIDE_BY_RFID = (
    "NOT EXISTS (SELECT 1 FROM refimages o WHERE o.rfid = r.rfid AND o.run = %(run)s)")

#: The ``requested_by`` :func:`expire_runs` deletes as.
EXPIRY_ACTOR = "expire_runs"

#: ``delete_objects`` takes at most 1000 keys per call (an S3 API limit).
_DELETE_OBJECTS_BATCH_SIZE = 1000


class CleanupFailed(RunModelError):
    """S3 reported per-object errors from ``delete_objects`` (which it
    does with HTTP 200).

    The run is left ``deleting`` and nothing in the database changes; a
    later :func:`delete_run` resumes it.
    """


@dataclass
class DeletionReport:
    """What :func:`delete_run` did (or, from :func:`expire_runs`, refused).

    ``already_deleted`` is true when the run was ``deleted`` before the
    call and nothing was done. ``refused`` carries the refusal message
    when :func:`expire_runs` caught one; :func:`delete_run` itself raises
    rather than returning a refusal.
    """

    run_id: str
    objects_deleted: int = 0
    versions_deleted: int = 0
    rows_deleted: dict[str, int] = field(default_factory=dict)
    instances_marked: int = 0
    already_deleted: bool = False
    refused: str | None = None


def _default_scratch_bucket() -> str:
    bucket = os.environ.get("RAPIDPIPE_SCRATCH_BUCKET")
    if bucket:
        return bucket
    root = (os.environ.get("RAPIDPIPE_OUTPUTS_ROOT_SCRATCH")
            or os.environ.get("RAPIDPIPE_OUTPUTS_ROOT"))
    if not root:
        raise DeletionRefused(
            "cannot tell which bucket is the scratch bucket: none of "
            "RAPIDPIPE_SCRATCH_BUCKET, RAPIDPIPE_OUTPUTS_ROOT_SCRATCH or "
            "RAPIDPIPE_OUTPUTS_ROOT is set; refusing to delete S3 objects")
    location = parse_location(root)
    if not location.is_s3():
        raise DeletionRefused(
            f"the scratch outputs root {root!r} is not an s3:// location; "
            "refusing to delete S3 objects")
    return location.bucket  # type: ignore[return-value]


def _default_s3_client() -> Any:
    from rapidpipe.products import storage

    return storage.s3_client()


def _run_row(cur, run_id: str) -> tuple[str, str, str]:
    cur.execute("SELECT kind, owner, state FROM runs WHERE id = %s", (run_id,))
    row = cur.fetchone()
    if row is None:
        raise RunNotFound(f"run {run_id!r} does not exist")
    return row


def _s3_prefixes(conn, run_id: str, scratch_bucket: str | None) -> list[tuple[str, str]]:
    """Every (bucket, key prefix ending '/') of the run's ``s3://`` attempt
    outputs, all checked before any is deleted."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, output_location FROM attempts "
            "WHERE run = %s AND output_location LIKE 's3://%%' ORDER BY id",
            (run_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return []

    bucket_allowed = scratch_bucket or _default_scratch_bucket()
    prefixes: list[tuple[str, str]] = []
    for attempt_id, output_location in rows:
        location = parse_location(output_location)
        if location.bucket != bucket_allowed:
            raise DeletionRefused(
                f"attempt {attempt_id!r} of run {run_id!r} wrote to "
                f"{output_location!r}, outside the scratch bucket "
                f"{bucket_allowed!r}; refusing (the run stays 'deleting')")
        prefix = location.prefix or ""
        # The key must contain /runs/<run>/ (runs page, "Storage layout":
        # <root>/runs/<run>/<stage>/<unit>/<attempt>) and end in the
        # attempt's own id; anything else is not this run's to empty.
        if f"/runs/{run_id}/" not in f"/{prefix}/" or not prefix.endswith(attempt_id):
            raise DeletionRefused(
                f"attempt {attempt_id!r} output location {output_location!r} is "
                f"not under runs/{run_id}/.../{attempt_id}; refusing")
        # Trailing delimiter: .../<attempt>/ never matches a sibling
        # .../<attempt>X.
        prefixes.append((location.bucket, prefix + "/"))  # type: ignore[arg-type]
    return prefixes


def _blocking_references(conn, run_id: str) -> list[str]:
    """Describe every row outside this run that references one of its
    science rows (``_INBOUND_REFERENCES``); empty when deletion is clear."""
    blocking: list[str] = []
    with conn.cursor() as cur:
        for table, column, target, key in _INBOUND_REFERENCES:
            if table in _RUNLESS_TABLES:
                outside = "TRUE"
            elif table in RFID_SCOPED_TABLES:
                outside = _OUTSIDE_BY_RFID
            else:
                outside = "r.run IS DISTINCT FROM %(run)s"
            # Names come from the fixed tuple above, never caller input.
            cur.execute(
                f"""
                SELECT count(*) FROM {table} r
                JOIN {target} t ON t.{key} = r.{column}
                WHERE t.run = %(run)s AND {outside}
                """,
                {"run": run_id},
            )
            (count,) = cur.fetchone()
            if count:
                blocking.append(f"{count} {table} row(s) via {table}.{column} -> {target}")
    return blocking


def _preflight(conn, run_id: str, scratch_bucket: str | None) -> list[tuple[str, str]]:
    """Everything that must hold before a run is marked or cleaned: every
    S3 location is this run's, in the scratch bucket; nothing outside the
    run references its rows. Returns the S3 prefixes to empty."""
    prefixes = _s3_prefixes(conn, run_id, scratch_bucket)
    blocking = _blocking_references(conn, run_id)
    if blocking:
        raise DeletionRefused(
            f"run {run_id!r} has rows referenced from outside it, which "
            f"cleanup does not remove: {'; '.join(blocking)}; refusing")
    return prefixes


def _delete_prefix(s3, bucket: str, prefix: str) -> tuple[int, int]:
    """Remove every object version and delete marker under ``prefix``.

    Returns ``(distinct keys, versions and markers removed)``.
    """
    targets: list[dict[str, str]] = []
    keys: set[str] = set()
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    while True:
        response = s3.list_object_versions(**kwargs)
        for entry in (response.get("Versions") or []) + (response.get("DeleteMarkers") or []):
            targets.append({"Key": entry["Key"], "VersionId": entry["VersionId"]})
            keys.add(entry["Key"])
        if not response.get("IsTruncated"):
            break
        kwargs["KeyMarker"] = response.get("NextKeyMarker")
        version_marker = response.get("NextVersionIdMarker")
        if version_marker:
            kwargs["VersionIdMarker"] = version_marker
        else:
            kwargs.pop("VersionIdMarker", None)

    for start in range(0, len(targets), _DELETE_OBJECTS_BATCH_SIZE):
        chunk = targets[start:start + _DELETE_OBJECTS_BATCH_SIZE]
        response = s3.delete_objects(
            Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})
        errors = response.get("Errors") or []
        if errors:
            first = errors[0]
            raise CleanupFailed(
                f"S3 refused {len(errors)} of {len(chunk)} deletions under "
                f"s3://{bucket}/{prefix} (first: {first.get('Key')!r} "
                f"{first.get('Code')!r} {first.get('Message')!r}); the run "
                "stays 'deleting' and a later delete resumes it")
    return len(keys), len(targets)


def delete_run(
    conn: psycopg2.extensions.connection,
    run_id: str,
    requested_by: str,
    *,
    s3_client: Any = None,
    scratch_bucket: str | None = None,
    expiry: bool = False,
) -> DeletionReport:
    """Delete a scratch run: preflight, fence, commit, clean up, record completion.

    COMMITS on ``conn``: call it on a connection with no pending work.

    By state: ``open``/``finished`` runs the full fence; ``deleting``
    resumes; ``deleted`` returns at once with ``already_deleted=True``.

    1. Preflight (every state but ``deleted``): every attempt's ``s3://``
       output location must be ``s3://<scratch bucket>/.../runs/<run>/...
       /<attempt>`` (``scratch_bucket``, else ``RAPIDPIPE_SCRATCH_BUCKET``,
       else the bucket of ``RAPIDPIPE_OUTPUTS_ROOT_SCRATCH`` /
       ``RAPIDPIPE_OUTPUTS_ROOT``), and no row outside the run may
       reference one of its science rows. A failure raises
       :class:`DeletionRefused` and leaves the run in the state it had.
    2. :func:`~rapidpipe.runs.repository.mark_run_deleting` (scratch only;
       owner only, or with ``expiry=True`` the expiry predicate instead;
       no unresolved attempts; no outside bindings), then ``commit``. The
       preflight is repeated once the fence holds, since no new attempt
       can appear after it.
    3. Every object version and delete marker under each location plus
       ``/`` is removed (``list_object_versions``, paginated;
       ``delete_objects`` in batches of 1000, quiet). Any per-object error
       raises :class:`CleanupFailed`; the run stays ``deleting`` and the
       database is untouched.
    4. In ONE transaction: the :data:`RFID_SCOPED_TABLES` rows whose
       ``rfid`` is one of this run's `refimages` rows, then ``DELETE FROM
       <table> WHERE run = %s`` for each of :data:`SCIENCE_TABLES` in
       order, counted per table; the run's
       instances marked ``deletion_state = 'deleted'``;
       :func:`~rapidpipe.runs.repository.mark_run_deleted`; then one
       ``commit``. A failure rolls all of it back and leaves ``deleting``.

    Rows with a NULL ``run`` or another run's id, and every run-model row
    (tombstones), are never touched.
    """
    report = DeletionReport(run_id=run_id)

    with conn.cursor() as cur:
        _kind, _owner, state = _run_row(cur, run_id)
    if state == "deleted":
        report.already_deleted = True
        return report

    _preflight(conn, run_id, scratch_bucket)
    if state != "deleting":
        mark_run_deleting(conn, run_id, requested_by, expiry=expiry)
        conn.commit()
    prefixes = _preflight(conn, run_id, scratch_bucket)

    if prefixes:
        s3 = s3_client if s3_client is not None else _default_s3_client()
        for bucket, prefix in prefixes:
            objects, versions = _delete_prefix(s3, bucket, prefix)
            report.objects_deleted += objects
            report.versions_deleted += versions

    try:
        with conn.cursor() as cur:
            for table in RFID_SCOPED_TABLES:
                # table comes from the fixed tuple above, never caller input.
                cur.execute(
                    f"DELETE FROM {table} WHERE rfid IN "
                    "(SELECT rfid FROM refimages WHERE run = %s)",
                    (run_id,))
                report.rows_deleted[table] = cur.rowcount
            for table in SCIENCE_TABLES:
                # table comes from the fixed tuple above, never caller input.
                cur.execute(f"DELETE FROM {table} WHERE run = %s", (run_id,))
                report.rows_deleted[table] = cur.rowcount
            cur.execute(
                "UPDATE product_instances SET deletion_state = 'deleted' "
                "WHERE run = %s AND deletion_state <> 'deleted'",
                (run_id,),
            )
            report.instances_marked = cur.rowcount
        mark_run_deleted(conn, run_id)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return report


def expire_runs(
    conn: psycopg2.extensions.connection,
    *,
    now: datetime | None = None,
    s3_client: Any = None,
    scratch_bucket: str | None = None,
) -> list[DeletionReport]:
    """Delete every expired scratch run; return one report per run tried.

    COMMITS on ``conn``: call it on a connection with no pending work.

    A run is a candidate when it is ``scratch``, ``expires_at < now`` (the
    database's ``now()`` when ``now`` is ``None``), not pinned, and
    ``open`` or ``finished``. Each is deleted with :func:`delete_run` with
    ``expiry=True``: the sweeper is an explicitly authorised actor, so
    ``mark_run_deleting`` replaces the owner check with the expiry
    predicate (scratch, not pinned, ``expires_at < now()``), re-checked
    under the run's row lock -- a run pinned after this listing is
    refused there. A refusal or cleanup failure is rolled back, caught and
    recorded in that run's report (``refused``), and the sweep moves on.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, owner FROM runs
            WHERE kind = 'scratch'
              AND NOT pinned
              AND state IN ('open', 'finished')
              AND expires_at IS NOT NULL
              AND expires_at < COALESCE(%s::timestamptz, now())
            ORDER BY expires_at, id
            """,
            (now,),
        )
        candidates = cur.fetchall()
    conn.commit()

    reports: list[DeletionReport] = []
    for run_id, _owner in candidates:
        try:
            reports.append(delete_run(
                conn, run_id, EXPIRY_ACTOR, s3_client=s3_client,
                scratch_bucket=scratch_bucket, expiry=True))
        except RunModelError as exc:
            conn.rollback()
            reports.append(DeletionReport(run_id=run_id, refused=str(exc)))
    return reports


def pin_run(conn: psycopg2.extensions.connection, run_id: str, pinned: bool) -> None:
    """Set a run's ``pinned`` flag; a pinned scratch run never expires.

    Refuses a missing run (:class:`RunNotFound`) and a deleting or
    deleted one (:class:`RunDeletingOrDeleted`). The caller commits.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run_id,))
        row = cur.fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id!r} does not exist")
        if row[0] in ("deleting", "deleted"):
            raise RunDeletingOrDeleted(f"run {run_id!r} is {row[0]!r}; refusing")
        cur.execute("UPDATE runs SET pinned = %s WHERE id = %s", (bool(pinned), run_id))
