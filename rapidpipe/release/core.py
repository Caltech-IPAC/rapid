"""Cut, show, list and verify releases: the release tool's Python API.

See the package docstring (``rapidpipe.release``) for the interface and the
hook contract; this module implements them. Every git operation is a
``git -C <repo>`` subprocess; every database operation runs on the
``psycopg2``-compatible connection the caller passes, and ``cut`` commits
it at each checkpoint (tag, then one commit per validated hook result),
so a failure leaves the ``releases`` row at its last good state for
``--resume``.

This module imports nothing from ``rapidpipe.stages``, ``rapidpipe.launch``
or ``rapidpipe.cli``; the exit codes in ``hooks`` mirror
``rapidpipe.stages.contract.ExitCode`` values by number rather than by
import (``rapidpipe/__init__.py``: ``release`` may import ``db`` and
``runs`` only).
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from rapidpipe.release.hooks import (
    HOOKS,
    INSPECT_HOOK,
    NEXT_HOOK,
    HookFailed,
    ReleaseRefused,
    ReleaseUsage,
    hook_available,
    run_hook,
)

DEFAULT_REMOTE = "origin"
DEFAULT_REF = "origin/rebuild"
DEFAULT_PREFIX = "rebuild-v0."
HOOKS_ENV = "RAPIDPIPE_RELEASE_HOOKS"

TAG_RE = re.compile(r"^rebuild-v[0-9]+\.[0-9]+$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
MIGRATION_RE = re.compile(r"^[0-9]{8}-[0-9]{2}-[a-z0-9-]+\.sql$")
MIGRATIONS_DIR = "database/migrations"


@dataclass
class Deployment:
    consumer: str
    job_definition: str
    deployed_at: datetime | None = None
    deployed_by: str | None = None


@dataclass
class Release:
    tag: str
    source_revision: str
    schema_version: str
    image_digest: str | None
    image_ref: str | None
    state: str
    cut_by: str
    cut_at: datetime | None
    completed_at: datetime | None
    notes: dict[str, Any] = field(default_factory=dict)
    deployments: list[Deployment] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        def _conv(value):
            return value.isoformat() if isinstance(value, datetime) else value
        data = asdict(self)
        for key in ("cut_at", "completed_at"):
            data[key] = _conv(data[key])
        data["deployments"] = [
            {k: _conv(v) for k, v in d.items()} for d in data["deployments"]]
        return data


# ======================================================================
# git
# ======================================================================

def _git(repo: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise ReleaseRefused(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result


def _git_out(repo: str | Path, *args: str) -> str:
    return _git(repo, *args).stdout.strip()


def _remote_tags(repo: str | Path, remote: str, pattern: str) -> dict[str, str]:
    """``{tag: commit sha}`` at ``remote`` for tags matching ``pattern``.

    An annotated tag's ``ls-remote`` output has the tag object line and a
    peeled ``^{}`` line; the peeled line (the commit) wins.
    """
    # An exact pattern does not match the peeled "<tag>^{}" ref, so ask
    # for both; a wildcard pattern's duplicates collapse below.
    out = _git_out(repo, "ls-remote", "--tags", remote,
                   f"refs/tags/{pattern}", f"refs/tags/{pattern}^{{}}")
    tags: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        name = ref[len("refs/tags/"):]
        if name.endswith("^{}"):
            tags[name[:-3]] = sha
        else:
            tags.setdefault(name, sha)
    return tags


def _local_tag_commit(repo: str | Path, tag: str) -> str | None:
    result = _git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}",
                  check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def next_tag(repo: str | Path, remote: str = DEFAULT_REMOTE,
             prefix: str = DEFAULT_PREFIX) -> str:
    """``<prefix><n>`` with ``n`` one more than the greatest existing.

    Existing tags are the remote's (``git ls-remote --tags``, the
    authoritative list) plus any local ones; a tag whose suffix is not a
    plain integer is ignored. With none, ``n`` is 1.
    """
    names = set(_remote_tags(repo, remote, f"{prefix}*"))
    names |= set(_git_out(repo, "tag", "-l", f"{prefix}*").split())
    numbers = [int(name[len(prefix):]) for name in names
               if name.startswith(prefix) and name[len(prefix):].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1}"


def _resolve_commit(repo: str | Path, ref: str) -> str:
    result = _git(repo, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False)
    if result.returncode != 0:
        raise ReleaseUsage(f"--ref {ref!r} does not name a commit in {repo}")
    return result.stdout.strip()


def _require_ancestor(repo: str | Path, sha: str, upstream: str) -> None:
    upstream_sha = _resolve_commit(repo, upstream)
    result = _git(repo, "merge-base", "--is-ancestor", sha, upstream_sha, check=False)
    if result.returncode != 0:
        raise ReleaseUsage(
            f"{sha} is not an ancestor of or equal to {upstream} ({upstream_sha}); "
            "a release is cut only from the rebuild line")


def _require_clean(repo: str | Path) -> None:
    status = _git_out(repo, "status", "--porcelain", "--untracked-files=no")
    if status:
        raise ReleaseUsage(
            f"the working tree in {repo} has uncommitted changes to tracked files; "
            "commit or stash them first:\n" + status)


def migration_files(repo: str | Path, rev: str) -> list[str]:
    """Every ``*.sql`` file name under ``database/migrations/`` at ``rev``."""
    out = _git_out(repo, "ls-tree", "--name-only", f"{rev}:{MIGRATIONS_DIR}")
    return sorted(name for name in out.splitlines() if name.endswith(".sql"))


def schema_version_at(repo: str | Path, rev: str) -> str:
    """The greatest migration filename at ``rev`` matching the dated form."""
    names = [n for n in migration_files(repo, rev) if MIGRATION_RE.match(n)]
    if not names:
        raise ReleaseRefused(f"{rev} has no dated migration under {MIGRATIONS_DIR}/")
    return max(names)


def _migration_sha256(repo: str | Path, rev: str, name: str) -> str:
    blob = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{MIGRATIONS_DIR}/{name}"],
        capture_output=True, check=True).stdout
    return hashlib.sha256(blob).hexdigest()


def _tag_manifest(repo: str | Path, tag: str) -> dict[str, Any]:
    """The JSON body of an annotated tag's message (the initial manifest)."""
    body = _git_out(repo, "for-each-ref", "--format=%(contents)", f"refs/tags/{tag}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ReleaseRefused(f"tag {tag} message is not the release manifest JSON: {exc}")


def _hooks_dir_revision(hooks_dir: str | Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(hooks_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _hook_env(release: Release, repo: str | Path, database_target: str) -> dict[str, str]:
    env = {
        "RELEASE_TAG": release.tag,
        "RELEASE_REVISION": release.source_revision,
        "RELEASE_SCHEMA_VERSION": release.schema_version,
        "RELEASE_DATABASE_TARGET": database_target,
        "RELEASE_REPO": str(Path(repo).resolve()),
    }
    if release.state in ("built", "deployed", "complete"):
        # From the build checkpoint onward: deploy, pins and inspect see
        # the image build recorded (empty if build was skipped).
        env["RELEASE_IMAGE_DIGEST"] = release.image_digest or ""
        env["RELEASE_IMAGE_REF"] = release.image_ref or ""
    return env


# ======================================================================
# database
# ======================================================================

def _database_target(conn) -> str:
    info = getattr(conn, "info", None)
    name = getattr(info, "dbname", None) if info is not None else None
    return name or os.environ.get("PGDATABASE", "")


def _applied_migrations(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename, sha256 FROM schema_migrations")
        return dict(cur.fetchall())


def _check_migrations_applied(conn, repo: str | Path, tag: str) -> list[str]:
    """Problems: tagged-tree migration files absent from, or with a
    different sha256 than, ``schema_migrations``."""
    applied = _applied_migrations(conn)
    problems = []
    for name in migration_files(repo, tag):
        recorded = applied.get(name)
        if recorded is None:
            problems.append(f"{name} is not in schema_migrations")
        elif recorded != _migration_sha256(repo, tag, name):
            problems.append(f"{name} is in schema_migrations with a different sha256")
    return problems


_RELEASE_COLUMNS = (
    "tag, source_revision, schema_version, image_digest, image_ref, state, "
    "cut_by, cut_at, completed_at, notes")


def _row_to_release(row) -> Release:
    (tag, source_revision, schema_version, image_digest, image_ref, state,
     cut_by, cut_at, completed_at, notes) = row
    if isinstance(notes, str):
        notes = json.loads(notes)
    return Release(tag, source_revision, schema_version, image_digest, image_ref,
                   state, cut_by, cut_at, completed_at, dict(notes or {}))


def _releases_table_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('releases') IS NOT NULL")
        return bool(cur.fetchone()[0])


def _refuse_unfinished(conn, resume: str | None) -> None:
    """Concurrent cuts are serialised by the record (supervisor step 9,
    2026-09-25, R8): refuse to start -- before any tag -- while a
    ``releases`` row is in a state other than 'complete', unless
    ``resume`` names that row. Raises :class:`ReleaseRefused` (exit 1)
    naming each such row's tag and state."""
    if not _releases_table_exists(conn):
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tag, state FROM releases WHERE state <> 'complete' ORDER BY cut_at, tag")
        unfinished = [(tag, state) for tag, state in cur.fetchall() if tag != resume]
    if unfinished:
        rows = ", ".join(f"{tag} (state {state!r})" for tag, state in unfinished)
        first = unfinished[0][0]
        raise ReleaseRefused(
            f"release {rows} is not complete; finish it with "
            f"'rapidpipe release cut --resume {first}' before starting another cut")


def _fetch_release(conn, tag: str) -> Release | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_RELEASE_COLUMNS} FROM releases WHERE tag = %s", (tag,))
        row = cur.fetchone()
        if row is None:
            return None
        release = _row_to_release(row)
        cur.execute(
            "SELECT consumer, job_definition, deployed_at, deployed_by "
            "FROM release_deployments WHERE release = %s ORDER BY consumer", (tag,))
        release.deployments = [Deployment(*r) for r in cur.fetchall()]
    return release


def _insert_release(conn, release: Release) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO releases (tag, source_revision, schema_version, state, "
            "cut_by, cut_at, notes) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (release.tag, release.source_revision, release.schema_version,
             release.state, release.cut_by, release.cut_at, json.dumps(release.notes)))


def _update_release(conn, release: Release, *, complete: bool = False) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE releases SET state = %s, image_digest = %s, image_ref = %s, "
            "notes = %s, completed_at = CASE WHEN %s THEN now() ELSE completed_at END "
            "WHERE tag = %s",
            (release.state, release.image_digest, release.image_ref,
             json.dumps(release.notes), complete, release.tag))


def _record_deployments(conn, tag: str, deployments: dict[str, str], by: str) -> None:
    with conn.cursor() as cur:
        for consumer, job_definition in sorted(deployments.items()):
            cur.execute(
                "INSERT INTO release_deployments (release, consumer, job_definition, "
                "deployed_by) VALUES (%s, %s, %s, %s) ON CONFLICT (release, consumer) "
                "DO UPDATE SET job_definition = EXCLUDED.job_definition, "
                "deployed_at = now(), deployed_by = EXCLUDED.deployed_by",
                (tag, consumer, job_definition, by))


# ======================================================================
# cut
# ======================================================================

@dataclass
class Plan:
    """What a cut will do: printed by ``--dry-run``, followed otherwise."""

    tag: str
    source_revision: str
    schema_version: str
    resume: bool
    start_state: str | None
    hooks: list[str]
    skipped: list[str]
    hooks_dir: str | None
    missing_hooks: list[str]

    def lines(self) -> list[str]:
        lines = [
            f"tag:            {self.tag}" + ("  (resume)" if self.resume else "  (new)"),
            f"source_revision: {self.source_revision}",
            f"schema_version: {self.schema_version}",
            f"hooks_dir:      {self.hooks_dir}",
        ]
        if self.resume:
            lines.append(f"row state:      {self.start_state or 'absent (re-enter at migrate)'}")
        steps = ([] if self.resume else ["tag + push"])
        for hook in self.hooks:
            steps.append(hook + (" (skipped)" if hook in self.skipped else ""))
            if hook == "migrate":
                steps.append("verify schema_migrations + record row (migrated)")
        lines.append("steps:          " + (" -> ".join(steps) if steps else "(none; complete)"))
        if self.missing_hooks:
            lines.append("missing hooks:  " + ", ".join(self.missing_hooks))
        return lines


def _remaining_hooks(state: str | None) -> list[str]:
    if state is None:
        return list(HOOKS)
    if state == "complete":
        return []
    return list(HOOKS[HOOKS.index(NEXT_HOOK[state]):])


def cut(
    conn,
    *,
    repo: str | Path,
    ref: str | None = None,
    tag: str | None = None,
    hooks_dir: str | Path | None = None,
    skip: Sequence[str] = (),
    resume: str | None = None,
    dry_run: bool = False,
    by: str | None = None,
    force_resume: bool = False,
    remote: str = DEFAULT_REMOTE,
    out: Callable[[str], None] | None = None,
) -> Release | Plan:
    """Cut (or resume) a release; return the final :class:`Release`.

    ``conn`` may be ``None`` only for a ``dry_run`` without ``resume``.
    With ``dry_run`` the :class:`Plan` is printed through ``out`` and
    returned, and nothing is touched: no fetch, no tag, no hook, no
    database write. See the package docstring for the full order.
    """
    say = out or print
    repo = Path(repo).resolve()
    by = by or getpass.getuser()
    hooks_dir = hooks_dir if hooks_dir is not None else os.environ.get(HOOKS_ENV)
    skip = list(dict.fromkeys(skip))
    unknown = [s for s in skip if s not in HOOKS]
    if unknown:
        raise ReleaseUsage(f"--skip names unknown hook(s) {unknown}; hooks are {list(HOOKS)}")
    if resume is not None and tag is not None and tag != resume:
        raise ReleaseUsage("--tag and --resume name different tags")
    if not (repo / ".git").exists() and _git(repo, "rev-parse", "--git-dir",
                                               check=False).returncode != 0:
        raise ReleaseUsage(f"--repo {repo} is not a git checkout")
    _require_clean(repo)
    if conn is not None:
        _refuse_unfinished(conn, resume)

    if resume is not None:
        return _cut_resume(conn, repo=repo, ref=ref, tag=resume, hooks_dir=hooks_dir,
                           skip=skip, dry_run=dry_run, by=by, force_resume=force_resume,
                           remote=remote, say=say, out=out)

    if not dry_run:
        _git(repo, "fetch", "--quiet", remote)
    ref = ref or DEFAULT_REF
    sha = _resolve_commit(repo, ref)
    _require_ancestor(repo, sha, DEFAULT_REF)
    tag = tag or next_tag(repo, remote)
    if not TAG_RE.match(tag):
        raise ReleaseUsage(f"tag {tag!r} is not of the form rebuild-v<major>.<n>")
    if _local_tag_commit(repo, tag) is not None or tag in _remote_tags(repo, remote, tag):
        raise ReleaseUsage(f"tag {tag} already exists; use --resume {tag} to continue it")
    schema_version = schema_version_at(repo, sha)

    missing = [h for h in HOOKS if h not in skip and not hook_available(hooks_dir, h)]
    plan = Plan(tag, sha, schema_version, False, None, list(HOOKS), skip,
                str(hooks_dir) if hooks_dir else None, missing)
    if dry_run:
        for line in plan.lines():
            say(line)
        return plan
    if hooks_dir is None:
        raise ReleaseUsage(f"no hooks directory: pass --hooks-dir or set {HOOKS_ENV}")
    if missing:
        raise ReleaseUsage(f"hooks not found or not executable in {hooks_dir}: {missing}")

    cut_at = datetime.now(timezone.utc).replace(microsecond=0)
    manifest = {"tag": tag, "source_revision": sha, "schema_version": schema_version,
                "cut_by": by, "cut_at": cut_at.isoformat()}
    _git(repo, "tag", "-a", tag, sha, "-m", json.dumps(manifest, sort_keys=True))
    push = _git(repo, "push", "--quiet", remote, f"refs/tags/{tag}", check=False)
    if push.returncode != 0:
        # Never published, so removing the local tag moves nothing anyone saw.
        _git(repo, "tag", "-d", tag, check=False)
        raise ReleaseRefused(f"git push {remote} refs/tags/{tag} failed: {push.stderr.strip()}")
    say(f"release {tag}: tagged {sha} and pushed to {remote}")

    release = Release(tag, sha, schema_version, None, None, "migrated", by, cut_at, None,
                      {"hooks_dir_revision": _hooks_dir_revision(hooks_dir),
                       "database_target": _database_target(conn),
                       "skipped": skip})
    return _run_from(conn, release, None, repo=repo, hooks_dir=hooks_dir, skip=skip,
                     by=by, say=say, out=out)


def _cut_resume(conn, *, repo, ref, tag, hooks_dir, skip, dry_run, by, force_resume,
                remote, say, out) -> Release | Plan:
    remote_tags = _remote_tags(repo, remote, tag)
    if tag not in remote_tags:
        raise ReleaseUsage(f"--resume {tag}: no such tag at {remote}")
    tag_sha = remote_tags[tag]
    if _local_tag_commit(repo, tag) is None:
        if dry_run:
            raise ReleaseUsage(f"--resume {tag}: tag is not fetched locally; "
                               f"run git fetch {remote} tag {tag} first")
        _git(repo, "fetch", "--quiet", remote, f"refs/tags/{tag}:refs/tags/{tag}")
    if _local_tag_commit(repo, tag) != tag_sha:
        raise ReleaseRefused(f"local tag {tag} does not match {remote}'s ({tag_sha})")
    if ref is not None and _resolve_commit(repo, ref) != tag_sha:
        raise ReleaseRefused(f"--resume {tag}: tag points at {tag_sha}, not at --ref {ref}")

    existing = None
    if conn is not None and _releases_table_exists(conn):
        existing = _fetch_release(conn, tag)
    if existing is not None and existing.source_revision != tag_sha:
        raise ReleaseRefused(
            f"release {tag} is recorded at {existing.source_revision}, but the tag "
            f"points at {tag_sha}")

    database_target = _database_target(conn) if conn is not None else os.environ.get(
        "PGDATABASE", "")
    hooks_rev = _hooks_dir_revision(hooks_dir) if hooks_dir else None
    if existing is not None and not force_resume:
        frozen = existing.notes
        if frozen.get("hooks_dir_revision") != hooks_rev:
            raise ReleaseRefused(
                f"resume {tag}: hooks_dir revision {hooks_rev} differs from the recorded "
                f"{frozen.get('hooks_dir_revision')}; pass --force-resume to proceed")
        if frozen.get("database_target") != database_target:
            raise ReleaseRefused(
                f"resume {tag}: database target {database_target!r} differs from the "
                f"recorded {frozen.get('database_target')!r}; pass --force-resume")

    manifest = _tag_manifest(repo, tag)
    schema_version = existing.schema_version if existing else schema_version_at(repo, tag)
    start_state = existing.state if existing else None
    remaining = _remaining_hooks(start_state)
    missing = [h for h in remaining if h not in skip and not hook_available(hooks_dir, h)]
    plan = Plan(tag, tag_sha, schema_version, True, start_state, remaining, skip,
                str(hooks_dir) if hooks_dir else None, missing)
    if dry_run:
        for line in plan.lines():
            say(line)
        return plan
    if not remaining:
        say(f"release {tag} is already complete; nothing to do")
        return existing
    if hooks_dir is None:
        raise ReleaseUsage(f"no hooks directory: pass --hooks-dir or set {HOOKS_ENV}")
    if missing:
        raise ReleaseUsage(f"hooks not found or not executable in {hooks_dir}: {missing}")

    if existing is None:
        cut_at = manifest.get("cut_at")
        release = Release(
            tag, tag_sha, schema_version, None, None, "migrated",
            manifest.get("cut_by") or by,
            datetime.fromisoformat(cut_at) if cut_at else None, None,
            {"hooks_dir_revision": hooks_rev, "database_target": database_target,
             "skipped": skip})
    else:
        release = existing
        release.notes["skipped"] = sorted(set(release.notes.get("skipped", [])) | set(skip))
        if force_resume:
            release.notes.setdefault("forced_resumes", []).append(
                {"hooks_dir_revision": hooks_rev, "database_target": database_target})
    say(f"release {tag}: resuming at {remaining[0]}")
    return _run_from(conn, release, start_state, repo=repo, hooks_dir=hooks_dir,
                     skip=skip, by=by, say=say, out=out)


def _run_from(conn, release: Release, start_state: str | None, *, repo, hooks_dir, skip,
              by, say, out) -> Release:
    database_target = release.notes.get("database_target") or _database_target(conn)
    for hook in _remaining_hooks(start_state):
        result = None
        if hook in skip:
            say(f"release {release.tag}: {hook} skipped")
        else:
            say(f"release {release.tag}: running {hook}")
            result = run_hook(hooks_dir, hook, _hook_env(release, repo, database_target),
                              out=out)
        conn.rollback()  # a fresh snapshot after the hook's own writes

        if hook == "migrate":
            if result is not None:
                release.notes["migrate"] = {"schema_version": result["schema_version"],
                                            "applied": result["applied"]}
            problems = _check_migrations_applied(conn, repo, release.tag)
            if problems:
                raise ReleaseRefused(
                    f"release {release.tag}: migrations of the tagged tree are not all "
                    "applied as shipped:\n  " + "\n  ".join(problems))
            release.state = "migrated"
            _insert_release(conn, release)
        elif hook == "build":
            if result is not None:
                release.image_digest = result["image_digest"]
                release.image_ref = result["image_ref"]
                if result.get("image_tag") is not None:
                    release.notes["image_tag"] = result["image_tag"]
            release.state = "built"
            _update_release(conn, release)
        elif hook == "deploy":
            if result is not None:
                _record_deployments(conn, release.tag, result["deployments"], by)
            release.state = "deployed"
            _update_release(conn, release)
        elif hook == "pins":
            if result is not None:
                release.notes["pins_rows"] = result["rows"]
            release.state = "complete"
            _update_release(conn, release, complete=True)
        conn.commit()
        say(f"release {release.tag}: state {release.state}")
    return _fetch_release(conn, release.tag) or release


# ======================================================================
# show / list / verify
# ======================================================================

def show(conn, tag: str) -> Release:
    release = _fetch_release(conn, tag)
    if release is None:
        raise ReleaseRefused(f"no release {tag!r}")
    return release


def list_releases(conn) -> list[Release]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_RELEASE_COLUMNS} FROM releases ORDER BY cut_at, tag")
        return [_row_to_release(row) for row in cur.fetchall()]


def verify(conn, tag: str, repo: str | Path, hooks_dir: str | Path | None = None,
           *, remote: str = DEFAULT_REMOTE,
           out: Callable[[str], None] | None = None) -> list[str]:
    """Recompute a release's facts; return the problems found (empty: ok).

    The tag exists locally and at ``remote`` and both point at the
    recorded ``source_revision``; every migration file in the tagged tree
    is in ``schema_migrations`` with its sha256; and, when an ``inspect``
    hook exists, the live job definitions it reports match
    ``release_deployments``.
    """
    release = show(conn, tag)
    problems: list[str] = []
    local = _local_tag_commit(repo, tag)
    if local is None:
        problems.append(f"tag {tag} does not exist locally")
    elif local != release.source_revision:
        problems.append(f"local tag {tag} points at {local}, not {release.source_revision}")
    remote_sha = _remote_tags(repo, remote, tag).get(tag)
    if remote_sha is None:
        problems.append(f"tag {tag} does not exist at {remote}")
    elif remote_sha != release.source_revision:
        problems.append(f"{remote}'s tag {tag} points at {remote_sha}, "
                        f"not {release.source_revision}")
    if local is not None:
        problems += _check_migrations_applied(conn, repo, tag)

    hooks_dir = hooks_dir if hooks_dir is not None else os.environ.get(HOOKS_ENV)
    if hook_available(hooks_dir, INSPECT_HOOK):
        env = _hook_env(release, repo, release.notes.get("database_target", ""))
        try:
            live = run_hook(hooks_dir, INSPECT_HOOK, env, out=out)["deployments"]
        except HookFailed as exc:
            problems.append(str(exc))
        else:
            recorded = {d.consumer: d.job_definition for d in release.deployments}
            if live != recorded:
                problems.append(f"live job definitions {live} differ from recorded {recorded}")
    return problems
