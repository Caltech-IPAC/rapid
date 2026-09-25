"""``python -m rapidpipe.release`` and ``rapidpipe release``: one code path.

:func:`build_parser` adds the four subcommands (``cut``, ``show``,
``list``, ``verify``) to a given parser -- the ``release`` subparser of
``rapidpipe.cli.main``, or a fresh top-level one here -- and
:func:`dispatch` runs the parsed arguments, so both forms behave
identically. Exit codes: 0 success, 1 refused or mismatch, 2 usage, 75
database unavailable (package docstring, "Exit codes").
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Callable, Sequence

from rapidpipe.release import core
from rapidpipe.release.hooks import (
    EXIT_REFUSED,
    EXIT_SUCCESS,
    EXIT_TRANSIENT,
    EXIT_USAGE,
    HOOKS,
    ReleaseError,
)


def _default_connect(**kwargs):
    from rapidpipe.db.connection import connect

    return connect(**kwargs)


#: Module-level indirection, as ``rapidpipe.cli.main.connect``: tests
#: monkeypatch ``rapidpipe.release.__main__.connect``.
connect: Callable = _default_connect


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    """Add the release subcommands to ``parser`` (or a new one); return it."""
    if parser is None:
        parser = argparse.ArgumentParser(
            prog="python -m rapidpipe.release",
            description="Cut, show, list and verify releases of the rebuilt pipeline.")
    sub = parser.add_subparsers(dest="release_command")

    cut = sub.add_parser(
        "cut", help="Tag, migrate, build, deploy and pin one release.",
        description="Cut a release: tag -> migrate -> record row -> build -> deploy -> "
                    "pins. Each checkpoint commits; --resume TAG continues a failed cut.")
    cut.add_argument("--tag", default=None,
                     help="Release tag (default: the next rebuild-v0.<n>).")
    cut.add_argument("--ref", default=None,
                     help=f"Commit to release (default {core.DEFAULT_REF}, after git fetch); "
                          f"must be an ancestor of or equal to {core.DEFAULT_REF}.")
    cut.add_argument("--repo", default=".",
                     help="The rapid checkout to tag from (default: the current directory).")
    cut.add_argument("--hooks-dir", default=None,
                     help=f"Directory of hook executables (default: ${core.HOOKS_ENV}).")
    cut.add_argument("--skip", action="append", default=[], choices=HOOKS, metavar="HOOK",
                     help="Skip a hook (repeatable); recorded in releases.notes.")
    cut.add_argument("--resume", default=None, metavar="TAG",
                     help="Continue an existing release from its recorded state.")
    cut.add_argument("--force-resume", action="store_true",
                     help="Resume even if the hooks-dir revision or database target changed.")
    cut.add_argument("--dry-run", action="store_true",
                     help="Print the plan; touch nothing (no fetch, tag, hook or DB write).")
    cut.add_argument("--by", default=None, help="Who is cutting (default: the current user).")

    show = sub.add_parser("show", help="Print one release and its deployments as JSON.")
    show.add_argument("tag")

    sub.add_parser("list", help="One line per release: tag, state, revision, digest, cut_at.")

    verify = sub.add_parser(
        "verify", help="Recompute a release's facts; exit 0 if they hold, 1 if not.")
    verify.add_argument("tag")
    verify.add_argument("--repo", default=".", help="The rapid checkout holding the tag.")
    verify.add_argument("--hooks-dir", default=None,
                        help=f"Directory holding an optional inspect hook "
                             f"(default: ${core.HOOKS_ENV}).")
    return parser


@contextlib.contextmanager
def _no_connection():
    yield None


def _open(name: str):
    return connect(application_name=f"rapidpipe-release-{name}")


def dispatch(args: argparse.Namespace) -> int:
    """Run parsed release arguments; return the exit code."""
    command = getattr(args, "release_command", None)
    if command is None:
        sys.stderr.write("rapidpipe release: a subcommand is required: cut, show, list, verify\n")
        return EXIT_USAGE
    try:
        from rapidpipe.db.connection import ConnectionConfigError, ConnectionUnavailable
    except ImportError:  # psycopg2 absent: only a dry-run can proceed
        ConnectionConfigError = ConnectionUnavailable = ()  # type: ignore[assignment]

    needs_db = not (command == "cut" and args.dry_run and args.resume is None)
    try:
        cm = _open(command) if needs_db else _no_connection()
    except ConnectionConfigError as exc:  # type: ignore[misc]
        sys.stderr.write(f"rapidpipe release {command}: database configuration error: {exc}\n")
        return EXIT_USAGE
    except ConnectionUnavailable as exc:  # type: ignore[misc]
        sys.stderr.write(f"rapidpipe release {command}: database unavailable: {exc}\n")
        return EXIT_TRANSIENT

    try:
        with cm as conn:
            try:
                return _run(command, args, conn)
            except BaseException:
                if conn is not None:
                    conn.rollback()
                raise
    except ReleaseError as exc:
        sys.stderr.write(f"rapidpipe release {command}: {exc}\n")
        return exc.exit_code


def _run(command: str, args: argparse.Namespace, conn) -> int:
    if command == "cut":
        result = core.cut(
            conn, repo=args.repo, ref=args.ref, tag=args.tag, hooks_dir=args.hooks_dir,
            skip=args.skip, resume=args.resume, dry_run=args.dry_run, by=args.by,
            force_resume=args.force_resume)
        if isinstance(result, core.Release):
            print(json.dumps(result.to_json(), indent=2, sort_keys=True))
        return EXIT_SUCCESS
    if command == "show":
        print(json.dumps(core.show(conn, args.tag).to_json(), indent=2, sort_keys=True))
        return EXIT_SUCCESS
    if command == "list":
        for r in core.list_releases(conn):
            cut_at = r.cut_at.isoformat(timespec="seconds") if r.cut_at else ""
            print(f"{r.tag}\t{r.state}\t{r.source_revision[:12]}\t"
                  f"{(r.image_digest or '')[:19]}\t{cut_at}")
        return EXIT_SUCCESS
    if command == "verify":
        problems = core.verify(conn, args.tag, args.repo, args.hooks_dir)
        for problem in problems:
            print(f"MISMATCH {problem}")
        if problems:
            return EXIT_REFUSED
        print(f"release {args.tag}: verified")
        return EXIT_SUCCESS
    sys.stderr.write(f"rapidpipe release: unknown subcommand {command!r}\n")
    return EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    return dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
