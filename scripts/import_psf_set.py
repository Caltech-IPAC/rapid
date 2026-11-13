"""Import a reference PSF set: one import job for a directory's set files, skipping the keys already bound.

Outside the pipeline, once per set (the reference PSF set is a name a run's
``[difference] reference_psf_set`` gives, rapid_docs ``difference.md``).
The directory's files named ``refimage_psf_fid<N>.fits`` (one per filter,
any orientation) or ``refimage_psf_fid<N>_sca<NN>.fits`` (one SCA's) are the
set; other names are listed and left out. Each file's keys come from its
name: ``{dataset, psf_set, fid, sca, kind: "reference"}``, a per-filter
file one per SCA of the 18. The run's dataset's open ``psfs`` bindings are
read: a key already bound is skipped and named (a per-filter file only
when all 18 of its keys are; some but not all is refused), and with
``--force`` it is kept, its expects entry naming the binding it closes, so
publishing replaces it in the job's one change group (``official undo``
of that group restores it). Nothing left: nothing is submitted. Otherwise
one ``import`` job is recorded with the scope and one expects entry per
key, and run here (``rapidpipe job local``) or, with ``--job-definition``,
submitted to Batch (``rapidpipe job submit``); the command's JSON is
printed. The run must be live for its survey and dataset, or publishing
keeps the rows unbound with a refusal. The database is named as for every
rapidpipe command.

    python scripts/import_psf_set.py --run 240 --dir DIR --psf-set 1043_20260928 \\
        [--force] [--job-definition JD [--job-queue Q]] [--dry-run]
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from rapidpipe.exitcodes import ArgumentParser, CommandExit, ExitCode
from rapidpipe.stages.import_products import PSF_SET_FILE, SCAS

logger = logging.getLogger("import_psf_set")

KIND = "reference"


def set_files(directory: Path) -> list[tuple[str, int, list[int]]]:
    """The directory's set files with the keys their names give.

    Parameters
    ----------
    directory : pathlib.Path
        The set's directory.

    Returns
    -------
    list of tuple
        ``(name, fid, scas)`` by name; a per-filter file's ``scas`` are all 18.

    Raises
    ------
    CommandExit
        Exit 65 when two files give one (filter, SCA) or none is a set file.
    """
    files, owners = [], {}
    for path in sorted(p for p in directory.iterdir() if p.is_file()):
        match = PSF_SET_FILE.match(path.name)
        if match is None:
            logger.warning("%s is not refimage_psf_fid<N>[_sca<NN>].fits; left out", path.name)
            continue
        fid, scas = int(match[1]), [int(match[2])] if match[2] else list(SCAS)
        for sca in scas:
            if (fid, sca) in owners:
                raise CommandExit(int(ExitCode.INPUT_REJECTED),
                                  f"{path.name} and {owners[fid, sca]} both give fid {fid} sca {sca}")
            owners[fid, sca] = path.name
        files.append((path.name, fid, scas))
    if not files:
        raise CommandExit(int(ExitCode.INPUT_REJECTED), f"no reference PSF set file in {directory}")
    return files


def plan(conn, run: int, directory: Path, psf_set: str, force: bool = False) -> tuple[dict, list, list]:
    """The import job's scope and expects, and the keys skipped as already bound.

    Parameters
    ----------
    conn : connection
        The database; the run and the open ``psfs`` bindings of its dataset are read.
    run : int
        The run the job is recorded under, whose dataset the keys carry.
    directory : pathlib.Path
        The set's directory.
    psf_set : str
        The set's name.
    force : bool
        Keep the bound keys, each expects entry closing its binding.

    Returns
    -------
    tuple
        The scope (None when nothing is left), the expects, and the skipped keys.

    Raises
    ------
    CommandExit
        Exit 64 for an unknown run; exit 65 for a per-filter file some but not all of whose
        keys are bound without ``--force``, or a set file problem (:func:`set_files`).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT dataset FROM runs WHERE id = %s", (run,))
        row = cur.fetchone()
        if row is None:
            raise CommandExit(int(ExitCode.USAGE), f"run {run} does not exist")
        dataset = row[0]
        cur.execute("SELECT id, key FROM official WHERE closed_at IS NULL AND product = 'psfs'"
                    " AND key->>'dataset' = %s AND key->>'kind' = %s AND key->>'psf_set' = %s",
                    (dataset, KIND, psf_set))
        bound = {(int(key["fid"]), int(key["sca"])): binding for binding, key in cur.fetchall()}
    conn.commit()
    names, expects, skipped = [], [], []
    for name, fid, scas in set_files(directory):
        keys = [{"dataset": dataset, "psf_set": psf_set, "fid": fid, "sca": sca, "kind": KIND}
                for sca in scas]
        held = [k for k in keys if (k["fid"], k["sca"]) in bound]
        if held and not force:
            if len(held) != len(keys):
                raise CommandExit(int(ExitCode.INPUT_REJECTED),
                                  f"{name}: {len(held)} of its {len(keys)} keys are bound in set"
                                  f" {psf_set!r}; --force replaces them all")
            skipped += held
            continue
        names.append(name)
        expects += [{"product": "psfs", "key": k, "binding": bound.get((k["fid"], k["sca"]))}
                    for k in keys]
    if not names:
        return None, [], skipped
    scope = {"source_dir": str(directory.resolve()),
             "psf": {"kind": KIND, "psf_set": psf_set, "dir": ".", "files": names}}
    return scope, expects, skipped


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, type=int, help="the live run the import job is recorded under")
    parser.add_argument("--dir", required=True, type=Path, help="the set's directory")
    parser.add_argument("--psf-set", required=True, help="the set's name, a run's reference_psf_set")
    parser.add_argument("--force", action="store_true", help="replace the keys already bound")
    parser.add_argument("--job-definition", help="submit to Batch under this job definition instead of here")
    parser.add_argument("--job-queue", help="the Batch job queue (job submit's default otherwise)")
    parser.add_argument("--dry-run", action="store_true", help="print the scope and expects; submit nothing")
    args = parser.parse_args(argv)
    from rapidpipe.cli import main as cli
    from rapidpipe.db.connection import connect

    try:
        with connect(application_name="import-psf-set") as conn:
            scope, expects, skipped = plan(conn, args.run, args.dir, args.psf_set, args.force)
    except CommandExit as exc:
        sys.stderr.write(f"import_psf_set: {exc}\n")
        return exc.code
    for key in skipped:
        logger.warning("skipped, already bound: fid %d sca %d of set %s", key["fid"], key["sca"],
                       key["psf_set"])
    if scope is None:
        sys.stderr.write(f"import_psf_set: every key of set {args.psf_set!r} is bound; nothing submitted\n")
        return int(ExitCode.SUCCESS)
    if args.dry_run:
        print(json.dumps({"scope": scope, "expects": expects, "skipped": skipped}, sort_keys=True))
        return int(ExitCode.SUCCESS)
    verb = ["job", "submit", "--job-definition", args.job_definition] if args.job_definition else ["job", "local"]
    if args.job_definition and args.job_queue:
        verb += ["--job-queue", args.job_queue]
    return cli.main([*verb, "--run", str(args.run), "--stage", "import",
                     "--scope", json.dumps(scope), "--expects", json.dumps(expects)])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sys.exit(main())
