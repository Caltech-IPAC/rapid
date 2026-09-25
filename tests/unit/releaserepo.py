"""Temporary git repositories and stub hooks for the release tests.

``make_release_repo`` builds a bare ``origin`` and a clone whose
``rebuild`` branch carries ``database/migrations/*.sql`` and is pushed,
so ``origin/rebuild`` exists as in a real checkout. ``write_hooks`` writes
four (or five) stub hook scripts that append a line to ``log`` and print
their contract's JSON as the last stdout line; a file named
``fail-<hook>`` in the hooks directory makes that hook exit 3, and
``garble-<hook>`` makes it print a non-JSON last line.

``isolate_git`` points git at an empty global config (no signing, no
hooks, no templates from the operator's own configuration) and gives it
an identity, so tag creation behaves the same on every machine.
"""

from __future__ import annotations

import hashlib
import stat
import subprocess
from pathlib import Path

DIGEST = "sha256:" + "ab" * 32
IMAGE_REF = "registry.example/rapid-pipeline@" + DIGEST


def isolate_git(monkeypatch, tmp_path: Path) -> None:
    empty = tmp_path / "gitconfig-empty"
    empty.write_text("")
    for key, value in {
        "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Release Test", "GIT_AUTHOR_EMAIL": "release@test.invalid",
        "GIT_COMMITTER_NAME": "Release Test", "GIT_COMMITTER_EMAIL": "release@test.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RAPIDPIPE_RELEASE_HOOKS", raising=False)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def make_release_repo(tmp_path: Path, migrations: dict[str, bytes] | None = None) -> Path:
    """A clone of a bare origin with ``rebuild`` pushed; returns the clone."""
    migrations = migrations or {
        "20260921-01-baseline.sql": b"CREATE TABLE a (x int);\n",
        "20260924-08-releases.sql": b"CREATE TABLE releases (tag text);\n",
    }
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "rebuild", str(origin)], check=True)
    work = tmp_path / "rapid"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True,
                   capture_output=True)
    git(work, "checkout", "-q", "-B", "rebuild")
    mig = work / "database" / "migrations"
    mig.mkdir(parents=True)
    for name, body in migrations.items():
        (mig / name).write_bytes(body)
    (mig / "README.md").write_text("not a migration\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "migrations")
    git(work, "push", "-q", "origin", "rebuild")
    git(work, "fetch", "-q", "origin")
    return work


def migration_shas(repo: Path, rev: str = "HEAD") -> dict[str, str]:
    """``{filename: sha256}`` of every ``*.sql`` migration at ``rev``."""
    names = git(repo, "ls-tree", "--name-only", f"{rev}:database/migrations").splitlines()
    out = {}
    for name in names:
        if name.endswith(".sql"):
            blob = subprocess.run(
                ["git", "-C", str(repo), "show", f"{rev}:database/migrations/{name}"],
                check=True, capture_output=True).stdout
            out[name] = hashlib.sha256(blob).hexdigest()
    return out


_HOOK = r"""#!/bin/sh
set -e
d=$(dirname "$0")
tagged=$(git -C "$RELEASE_REPO" rev-parse -q --verify "refs/tags/$RELEASE_TAG^{{commit}}" || echo none)
echo "hook:{name}:tag=$tagged:digest=${{RELEASE_IMAGE_DIGEST-unset}}" >> "{log}"
echo "progress line from {name}"
if [ -e "$d/fail-{name}" ]; then echo "{name} failing" >&2; exit 3; fi
if [ -e "$d/garble-{name}" ]; then echo "not json"; exit 0; fi
echo '{result}'
"""

RESULTS = {
    "migrate": '{"schema_version": "20260924-08-releases.sql", "applied": []}',
    "build": '{"image_digest": "%s", "image_ref": "%s", "image_tag": "t"}' % (DIGEST, IMAGE_REF),
    "deploy": '{"deployments": {"rapid-rebuild": "rapid-rebuild:7", '
              '"rapid-rebuild-production": "rapid-rebuild-production:5"}}',
    "pins": '{"rows": 2}',
    "inspect": '{"deployments": {"rapid-rebuild": "rapid-rebuild:7", '
               '"rapid-rebuild-production": "rapid-rebuild-production:5"}}',
}


def write_hooks(hooks_dir: Path, log: Path, names=("migrate", "build", "deploy", "pins"),
                results: dict[str, str] | None = None) -> Path:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    results = {**RESULTS, **(results or {})}
    for name in names:
        path = hooks_dir / name
        path.write_text(_HOOK.format(name=name, log=log, result=results[name]))
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hooks_dir


def log_lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


