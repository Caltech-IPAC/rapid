"""Write one CSV row per published difference image of a run: its L2 file and its three product directories.

    scripts/run_products_csv.py --dataset <name> --run <id> --out FILE.csv

Outside the pipeline: it only reads the database, named as for every rapidpipe
command. Each row is one published difference job of the run with its L2 file's
stem, SCA, pointing (``RA_TARG``, ``DEC_TARG``), ``MJD-OBS``, ``DATE-OBS``, field,
filter and exposure time, and the published directories of the reference it
differenced against, of the difference job itself and of its packet job. A
directory is ``<run>/<stage>/<job>/<attempt>``, relative to the outputs root.
The reference and L2 file are the difference job's recorded inputs. The packet
job is one scoped to the same image that read the difference job's sources (a
packet job of another image may read them too, as xmembers): the officially
bound one, else the latest published, else none (an empty cell).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

COLUMNS = ["l2_stem", "sca", "ra", "dec", "mjdobs", "dateobs", "field", "filter", "exptime",
           "reference_dir", "difference_dir", "packet_dir"]

#: Suffix intake gives the gzipped FITS it makes from ``<stem>_cal.asdf``.
L2_SUFFIX = "_cal.fits.gz"

QUERY = """
SELECT l.filename AS l2_filename, d.sca, l.ra, l.dec, l.mjdobs, l.dateobs, l.field,
       f.filter, l.exptime,
       rj.run AS ref_run, rj.id AS ref_job, rj.published_attempt AS ref_attempt,
       dj.run AS diff_run, dj.id AS diff_job, dj.published_attempt AS diff_attempt,
       pj.run AS packet_run, pj.id AS packet_job, pj.published_attempt AS packet_attempt
FROM jobs dj
JOIN diffimages d ON d.job = dj.id
JOIN filters f ON f.fid = d.fid
JOIN job_inputs li ON li.job = dj.id AND li.kind = 'input' AND li.product = 'l2files'
JOIN l2files l ON l.job = li.source_job
    AND (l.dataset, l.expid, l.sca, l.fid) = (d.dataset, d.expid, d.sca, d.fid)
LEFT JOIN job_inputs ri ON ri.job = dj.id AND ri.kind = 'input' AND ri.product = 'refimages'
LEFT JOIN jobs rj ON rj.id = ri.source_job
LEFT JOIN LATERAL (
    SELECT j.run, j.id, j.published_attempt
    FROM job_inputs pi
    JOIN jobs j ON j.id = pi.job AND j.stage = 'packet' AND j.state = 'published'
    WHERE pi.kind = 'input' AND pi.product = 'sources' AND pi.source_job = dj.id
      AND (j.scope->>'expid')::int = d.expid AND (j.scope->>'sca')::int = d.sca
      AND (j.scope->>'fid')::int = d.fid
    ORDER BY EXISTS (SELECT 1 FROM official o WHERE o.closed_at IS NULL
                     AND o.product = 'packets' AND o.job = j.id) DESC, j.id DESC
    LIMIT 1) pj ON true
WHERE dj.run = %(run)s AND dj.stage = 'difference' AND dj.state = 'published'
  AND d.dataset = %(dataset)s
ORDER BY d.expid, d.sca, d.fid, dj.id
"""


def job_dir(run: int | None, stage: str, job: int | None, attempt: int | None) -> str:
    """A published job's directory relative to the outputs root, or an empty string when there is no job.

    Parameters
    ----------
    run : int or None
        The job's run.
    stage : str
        The job's stage.
    job : int or None
        The job id; None when there is no such job.
    attempt : int or None
        The job's published attempt.

    Returns
    -------
    str
        ``<run>/<stage>/<job>/<attempt>``, or ``""`` when ``job`` is None.
    """
    return "" if job is None else f"{run}/{stage}/{job}/{attempt}"


def l2_stem(filename: str) -> str:
    """The L2 file's stem: its basename without intake's ``_cal.fits.gz`` suffix.

    Parameters
    ----------
    filename : str
        The ``l2files.filename`` URL.

    Returns
    -------
    str
        The stem the source root's ``<stem>_cal.asdf`` was named by.
    """
    name = filename.rsplit("/", 1)[-1]
    return name[: -len(L2_SUFFIX)] if name.endswith(L2_SUFFIX) else name


def product_rows(conn, dataset: str, run: int) -> list[dict]:
    """Read one row per published difference image of ``run`` in ``dataset``, in ``COLUMNS`` order.

    Parameters
    ----------
    conn : psycopg2 connection
        The database connection.
    dataset : str
        The dataset name.
    run : int
        The run id.

    Returns
    -------
    list of dict
        One dict per difference job, keyed by ``COLUMNS``.

    Raises
    ------
    ValueError
        If the run does not exist or belongs to another dataset.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT dataset FROM runs WHERE id = %s", (run,))
        found = cur.fetchone()
        if found is None or found[0] != dataset:
            raise ValueError(f"run {run} is not a run of dataset {dataset!r}"
                             + ("" if found is None else f" (its dataset is {found[0]!r})"))
        cur.execute(QUERY, {"run": run, "dataset": dataset})
        names = [c.name for c in cur.description]
        records = [dict(zip(names, r)) for r in cur.fetchall()]
    conn.commit()
    return [{"l2_stem": l2_stem(r["l2_filename"]), "sca": r["sca"], "ra": r["ra"], "dec": r["dec"],
             "mjdobs": r["mjdobs"], "dateobs": r["dateobs"].isoformat(), "field": r["field"],
             "filter": r["filter"], "exptime": r["exptime"],
             "reference_dir": job_dir(r["ref_run"], "reference", r["ref_job"], r["ref_attempt"]),
             "difference_dir": job_dir(r["diff_run"], "difference", r["diff_job"], r["diff_attempt"]),
             "packet_dir": job_dir(r["packet_run"], "packet", r["packet_job"], r["packet_attempt"])}
            for r in records]


def write_csv(rows: list[dict], out: Path) -> None:
    """Write ``rows`` to ``out`` as CSV with a ``COLUMNS`` header.

    Parameters
    ----------
    rows : list of dict
        Rows keyed by ``COLUMNS``.
    out : Path
        The CSV file to write.
    """
    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments, read the run's rows and write the CSV; return the exit code.

    Parameters
    ----------
    argv : list of str, optional
        The arguments; ``sys.argv[1:]`` when None.

    Returns
    -------
    int
        0 on success, 65 when the run is not one of the dataset's.
    """
    from rapidpipe.exitcodes import ArgumentParser, ExitCode

    parser = ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dataset", required=True, help="the dataset name")
    parser.add_argument("--run", required=True, type=int, help="the run id")
    parser.add_argument("--out", required=True, type=Path, help="the CSV file to write")
    args = parser.parse_args(argv)

    from rapidpipe.db.connection import connect

    with connect(application_name="run_products_csv") as conn:
        try:
            rows = product_rows(conn, args.dataset, args.run)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return int(ExitCode.INPUT_REJECTED)
    write_csv(rows, args.out)
    print(f"{len(rows)} rows written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
