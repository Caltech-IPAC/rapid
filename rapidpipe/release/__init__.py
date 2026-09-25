"""Releases of the rebuilt pipeline: cut, show, list, verify.

A release is an immutable annotated git tag ``rebuild-v0.<n>`` on the
``rebuild`` line, the schema it ships applied to the target database, one
container image built from it, the Batch job definitions deployed with
that image, and a ``releases`` row that records all of it (rapid_docs
``system/releases.md``; specification.md "Releases"). Only a run whose
attempts ran a ``complete`` release's image may be promoted
(``rapidpipe.runs.repository.promote``).

Interface
---------
``python -m rapidpipe.release <sub>`` and ``rapidpipe release <sub>`` are
one code path (``rapidpipe.release.__main__.build_parser``/``dispatch``):

- ``cut [--tag TAG] [--ref REF] [--repo PATH] [--hooks-dir DIR]
  [--skip HOOK]... [--resume TAG] [--force-resume] [--dry-run] [--by WHO]``
  -- the whole release. ``--ref`` defaults to ``origin/rebuild`` after
  ``git fetch origin`` and must be an ancestor of or equal to it; the
  tracked files of ``--repo`` must be clean; the tag must not exist
  locally or at ``origin`` unless ``--resume TAG``. ``--dry-run`` prints
  the plan (tag, sha, schema_version, hooks) and touches nothing: no
  fetch, no tag, no hook, no database write.
- ``show TAG`` -- the release row and its deployments, as JSON.
- ``list`` -- one line per release: tag, state, source_revision[:12],
  image_digest[:19], cut_at.
- ``verify TAG [--repo PATH] [--hooks-dir DIR]`` -- the tag exists locally
  and at ``origin`` at ``source_revision``; every migration file of the
  tagged tree is in ``schema_migrations`` with its sha256; and, when an
  ``inspect`` hook exists, the live job definitions equal
  ``release_deployments``.

Python API (``rapidpipe.release.core``): ``next_tag(repo, remote="origin",
prefix="rebuild-v0.") -> str``; ``cut(conn, *, repo, ref, tag, hooks_dir,
skip, resume, dry_run, by, force_resume=False) -> Release`` (a ``Plan``
under ``dry_run``); ``show(conn, tag) -> Release``; ``list_releases(conn)
-> list[Release]``; ``verify(conn, tag, repo, hooks_dir) -> list[str]``
(problems; empty means verified). ``Release(tag, source_revision,
schema_version, image_digest, image_ref, state, cut_by, cut_at,
completed_at, notes, deployments: list[Deployment(consumer,
job_definition, deployed_at, deployed_by)])``.

Exit codes: 0 success; 1 refused (a hook failed, a check failed, a resume
does not match, verify found a mismatch); 2 usage (bad arguments, dirty
tree, tag exists, missing hooks); 75 the database is unavailable (retry).

Order of a cut
--------------
``tag`` (annotated; message = the immutable initial manifest, JSON
``{"tag", "source_revision", "schema_version", "cut_by", "cut_at"}``;
pushed with ``git push origin refs/tags/<tag>``; never moved or deleted)
-> ``migrate`` hook -> verify every ``database/migrations/*.sql`` of the
tagged tree is in ``schema_migrations`` with an equal sha256 (else exit 1,
no row) -> insert the ``releases`` row, state ``migrated`` -> ``build``
(state ``built``) -> ``deploy`` (state ``deployed``, plus one
``release_deployments`` row per consumer) -> ``pins`` (state
``complete``, ``completed_at``). Each checkpoint commits only after its
hook's result validated; a failure leaves the row at its last good state.
The row is written after ``migrate`` because the ``releases`` table
ships in a release's own migrations.

``schema_version`` is the greatest filename in the tagged tree's
``database/migrations/`` matching ``^[0-9]{8}-[0-9]{2}-[a-z0-9-]+\\.sql$``.

Resume: ``--resume TAG`` requires the tag at ``origin`` (and at ``--ref``
if given). With a row, it re-enters after the row's state; without one, at
``migrate``. ``releases.notes`` freezes ``hooks_dir_revision`` (``git
rev-parse HEAD`` of the hooks directory, or null) and ``database_target``
at row creation; a resume that differs in either refuses (exit 1) unless
``--force-resume``. ``--skip HOOK`` runs nothing for that hook and records
it in ``notes["skipped"]``.

Hook contract
-------------
The hooks directory (``--hooks-dir``, default ``$RAPIDPIPE_RELEASE_HOOKS``)
holds ``rapid_systems``' executables ``migrate``, ``build``, ``deploy``,
``pins`` and optionally ``inspect``; ``rapid`` never names accounts, hosts
or buckets. Each runs with the caller's environment plus:

- ``RELEASE_TAG`` -- e.g. ``rebuild-v0.1``;
- ``RELEASE_REVISION`` -- the tagged commit, 40 hex;
- ``RELEASE_SCHEMA_VERSION`` -- as above;
- ``RELEASE_DATABASE_TARGET`` -- the database ``cut`` is connected to;
- ``RELEASE_REPO`` -- absolute path of ``--repo``, so a hook can ``git
  archive``/``git show`` the tagged tree;
- from the ``build`` checkpoint onward (``deploy``, ``pins``, ``inspect``):
  ``RELEASE_IMAGE_DIGEST`` and ``RELEASE_IMAGE_REF``.

Stdout is streamed to the operator; **the last non-empty stdout line must
be one JSON object**:

- ``migrate``: ``{"schema_version": "<greatest applied filename>",
  "applied": ["<file>", ...]}``
- ``build``: ``{"image_digest": "sha256:<64 hex>", "image_ref":
  "<registry/repo>@sha256:<64 hex>", "image_tag": "..."}``
- ``deploy``: ``{"deployments": {"<consumer>": "<name>:<revision>", ...}}``
  (at least one; every entry stored)
- ``pins``: ``{"rows": <int>}``
- ``inspect``: ``{"deployments": {...}}`` (read-only; extra keys allowed)

A non-zero exit or an unparsable last line fails the cut at that hook.
Hooks must be idempotent (a resumed ``build`` adopts the existing image
with that tag); ``cut`` never retries a hook itself.

Dependency direction: ``release`` may import ``db`` and ``runs``; only
``cli`` imports ``release`` (for the ``rapidpipe release`` subcommand).
"""

from rapidpipe.release.core import (  # noqa: F401
    Deployment,
    Plan,
    Release,
    cut,
    list_releases,
    next_tag,
    show,
    verify,
)
from rapidpipe.release.hooks import (  # noqa: F401
    HOOKS,
    HookFailed,
    ReleaseError,
    ReleaseRefused,
    ReleaseUsage,
)
