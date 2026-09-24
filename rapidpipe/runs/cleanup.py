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
:func:`expire_runs` commit on the connection they are given: the
``deleting`` state has to be durable before any object or row is removed,
so a crash part-way leaves a run ``deleting`` that a second
:func:`delete_run` call finishes. Everything after that first commit is
idempotent. :func:`pin_run` is an ordinary one-transaction repository
call; the caller commits.

What is removed, and only for rows whose ``run`` is this run:

- every S3 object version and delete marker under each attempt's
  ``s3://`` output location, which must be in the scratch bucket;
- ``dev`` science rows in ``sources`` (the inheritance parent: a DELETE
  on it reaches every ``sources_<date>_<sca>`` child, which carry ``run``
  since 20260923-04), ``diffimmeta``, ``diffimages``, ``psfs``,
  ``l2filemeta``, ``l2files`` and ``refimages``, in that order -- each
  table before the table its foreign keys point at (baseline:
  ``sources.pid`` and ``diffimmeta.pid`` -> ``diffimages``;
  ``diffimages.rid`` -> ``l2files``, ``diffimages.rfid`` -> ``refimages``;
  ``l2filemeta.rid`` -> ``l2files``).

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
    "psfs",
    "l2filemeta",
    "l2files",
    "refimages",
)

#: ``delete_objects`` takes at most 1000 keys per call (an S3 API limit).
_DELETE_OBJECTS_BATCH_SIZE = 1000


class CleanupFailed(RunModelError):
    """S3 reported per-object errors from ``delete_objects``.

    The run is left ``deleting``; a later :func:`delete_run` resumes it.
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
        # Belt and braces: an attempt's own location always ends in
        # runs/<run>/<stage>/<unit>/<attempt> (runs page, "Storage
        # layout"); anything else is not ours to empty.
        if f"runs/{run_id}/" not in f"/{prefix}/" or not prefix.endswith(attempt_id):
            raise DeletionRefused(
                f"attempt {attempt_id!r} output location {output_location!r} is "
                f"not under runs/{run_id}/.../{attempt_id}; refusing")
        prefixes.append((location.bucket, prefix + "/"))  # type: ignore[arg-type]
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
) -> DeletionReport:
    """Delete a scratch run: fence, commit, clean up, record completion.

    1. :func:`~rapidpipe.runs.repository.mark_run_deleting` (its fence:
       scratch only, owner only, no unresolved attempts, no outside
       bindings), then ``conn.commit()``. A run already ``deleting`` skips
       this and resumes; a run already ``deleted`` returns at once with
       ``already_deleted=True``.
    2. Every attempt's ``s3://`` output location must be in the scratch
       bucket (``scratch_bucket``, else ``RAPIDPIPE_SCRATCH_BUCKET``, else
       the bucket of ``RAPIDPIPE_OUTPUTS_ROOT_SCRATCH`` /
       ``RAPIDPIPE_OUTPUTS_ROOT``) -- checked for all attempts before any
       object is removed; otherwise :class:`DeletionRefused` and the run
       stays ``deleting``. Then every object version and delete marker
       under each location is removed (``list_object_versions``,
       paginated; ``delete_objects`` in batches of 1000, quiet).
    3. ``DELETE FROM <table> WHERE run = %s`` for each of
       :data:`SCIENCE_TABLES`, in that order, counted per table.
    4. The run's instances are marked ``deletion_state = 'deleted'``.
    5. :func:`~rapidpipe.runs.repository.mark_run_deleted`, then
       ``conn.commit()``.

    Steps 2-5 are idempotent, so a second call on a run left
    ``deleting`` by a failure finishes it. Rows with a NULL ``run`` or
    another run's id, and every run-model row, are never touched.
    """
    report = DeletionReport(run_id=run_id)

    with conn.cursor() as cur:
        _kind, _owner, state = _run_row(cur, run_id)
    if state == "deleted":
        report.already_deleted = True
        return report
    if state != "deleting":
        mark_run_deleting(conn, run_id, requested_by)
        conn.commit()

    prefixes = _s3_prefixes(conn, run_id, scratch_bucket)
    if prefixes:
        s3 = s3_client if s3_client is not None else _default_s3_client()
        for bucket, prefix in prefixes:
            objects, versions = _delete_prefix(s3, bucket, prefix)
            report.objects_deleted += objects
            report.versions_deleted += versions

    with conn.cursor() as cur:
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
    return report


def expire_runs(
    conn: psycopg2.extensions.connection,
    *,
    now: datetime | None = None,
    s3_client: Any = None,
    scratch_bucket: str | None = None,
) -> list[DeletionReport]:
    """Delete every expired scratch run; return one report per run tried.

    A run is expired when it is ``scratch``, ``expires_at < now`` (the
    database's ``now()`` when ``now`` is ``None``), not pinned, and
    ``open`` or ``finished``. Each is deleted with :func:`delete_run` as
    its owner. A refusal or cleanup failure is rolled back, caught and
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
    for run_id, owner in candidates:
        try:
            reports.append(delete_run(
                conn, run_id, owner, s3_client=s3_client, scratch_bucket=scratch_bucket))
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
