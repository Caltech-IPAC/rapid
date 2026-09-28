"""Creating a run: ``run create``'s code paths, shared with the loop.

:func:`create_run_record` is ``run create`` (and the processing-date
loop's run per date, ``rapidpipe.launch.loop``); :func:`create_only_failed_run`
is ``run create --seed <run> --only-failed`` (and the loop's
``--retry-failed``). Both compose :func:`rapidpipe.runs.repository.create_run`
with what the run row records about the code and schema it runs under
(runs.md §Rules). Neither commits.
"""

from __future__ import annotations

import os
from typing import Sequence

from rapidpipe.revision import git_revision
from rapidpipe.runs.repository import RunModelError


def _last_applied_schema_version(cur) -> str | None:
    """The filename of the most recently applied migration.

    ``database/apply-migrations.sh`` tracks applied migrations in
    ``schema_migrations(filename, sha256, applied_at)``; filenames sort
    lexicographically in applied order (``YYYYMMDD-NN-short-name.sql``),
    so the greatest filename is the last one applied.
    """
    cur.execute("SELECT filename FROM schema_migrations ORDER BY filename DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else None


class ReleaseNotComplete(Exception):
    """``run create --release`` (and ``loop run``) named a release that is
    absent or not ``complete``; the run is not created."""


def create_run_record(
    conn,
    *,
    kind: str,
    owner: str,
    purpose: str,
    stages: Sequence[str],
    release: str | None,
    lane: str,
    profile: str,
    db_target: str | None,
    max_attempts: int,
    settings_overlay_ref: str | None = None,
    input_selection_ref: str | None = None,
    check_policy_ref: str | None = None,
    auto_promote: bool = False,
    seed: str | None = None,
) -> str:
    """``run create``'s one code path (``rapidpipe loop run`` uses it too):
    with ``release``, the run's source revision and image digest are the
    ``releases`` row's, and a release that is absent or not ``complete``
    raises :class:`ReleaseNotComplete`; without, the checkout's revision
    and ``RAPIDPIPE_IMAGE_DIGEST``. Does not commit."""
    from rapidpipe.runs.repository import create_run

    with conn.cursor() as cur:
        schema_version = _last_applied_schema_version(cur) or "unknown"
        release_row = None
        if release is not None:
            cur.execute(
                "SELECT state, source_revision, image_digest FROM releases "
                "WHERE tag = %s", (release,))
            release_row = cur.fetchone()
    if release is not None:
        if release_row is None or release_row[0] != "complete":
            state = "absent" if release_row is None else release_row[0]
            raise ReleaseNotComplete(f"release {release} is {state}, not complete; refusing")
        _, code_revision, image_digest = release_row
    else:
        code_revision = git_revision() or "unknown"
        image_digest = os.environ.get("RAPIDPIPE_IMAGE_DIGEST")

    return create_run(
        conn,
        kind=kind,
        owner=owner,
        purpose=purpose,
        selected_stages=list(stages),
        code_revision=code_revision,
        image_digest=image_digest,
        schema_version=schema_version,
        settings_overlay_ref=settings_overlay_ref,
        input_selection_ref=input_selection_ref,
        lane=lane,
        resource_profile=profile,
        database_target=db_target or os.environ.get("PGDATABASE", ""),
        max_attempts_per_unit=max_attempts,
        auto_promote=auto_promote,
        check_policy_ref=check_policy_ref,
        release=release,
        seed_run=seed,
    )



class OnlyFailedKindMismatch(RunModelError):
    """``run create --seed <run> --only-failed --kind K`` with K not the seed's kind."""


def create_only_failed_run(
    conn,
    seed_run: str,
    *,
    owner: str | None = None,
    purpose: str | None = None,
    kind: str | None = None,
):
    """``run create --seed <run> --only-failed``'s one code path (the
    processing-date loop's ``--retry-failed`` uses it too): a run re-running
    the seed's non-complete units (runs page, "Rules").

    Configuration is copied from the seed row
    (:func:`~rapidpipe.runs.repository.failed_rerun_plan`); ``owner`` and
    ``purpose`` may be given, ``kind`` only if it equals the seed's
    (:class:`OnlyFailedKindMismatch` otherwise). Creates the run and its
    seeded units (:func:`~rapidpipe.runs.repository.seed_failed_units`);
    does not commit. Returns ``(run id, plan, seeded unit ids)``; a refusal
    is a :class:`RunModelError` (``SeedRefused``, ``RunNotFound``).
    """
    from rapidpipe.runs.repository import create_run, failed_rerun_plan, seed_failed_units

    plan = failed_rerun_plan(conn, seed_run)
    seed = plan.seed
    if kind is not None and kind != seed["kind"]:
        raise OnlyFailedKindMismatch(
            f"--kind {kind} differs from seed run {seed_run}'s kind {seed['kind']}; a "
            "--only-failed re-run keeps the seed's kind")
    with conn.cursor() as cur:
        schema_version = _last_applied_schema_version(cur) or "unknown"
    purpose = purpose or (
        f"re-run of failed units of {seed_run}: {seed['purpose']}"
        if seed["purpose"] else f"re-run of failed units of {seed_run}")
    run_id = create_run(
        conn,
        kind=seed["kind"],
        owner=owner or seed["owner"],
        purpose=purpose,
        selected_stages=plan.stages,
        code_revision=seed["code_revision"],
        image_digest=seed["image_digest"],
        schema_version=schema_version,
        settings_overlay_ref=seed["settings_overlay_ref"],
        input_selection_ref=seed["input_selection_ref"],
        lane=seed["lane"],
        resource_profile=seed["resource_profile"],
        database_target=seed["database_target"],
        max_attempts_per_unit=seed["max_attempts_per_unit"],
        auto_promote=False,
        check_policy_ref=seed["check_policy_ref"],
        release=seed["release"],
        seed_run=seed_run,
    )
    unit_ids = seed_failed_units(conn, seed_run=seed_run, new_run=run_id)
    return run_id, plan, unit_ids
