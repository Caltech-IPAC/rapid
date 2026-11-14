"""Copy a Roman program's WFI L2 files from MAST under a source root, where the loop and the intake read them.

Outside the pipeline, before any run: MAST's Roman mission search
(``astroquery.mast.MastMissions``) names the program's L2 files, WFI imaging
only (no GRISM or PRISM) and guided exposures only unless ``--unguided``, cut
by the optional filter, detector, pass, segment, visit and MJD terms. Each
file, ``<fileSetName>_cal.asdf``, is streamed from MAST's presigned URL to
``<source-root><file>``, the key ``rapidpipe loop`` lists, by one
conditional PUT (``If-None-Match: *``), the create-once write a sealed
inputs bucket requires: an object is never overwritten. A file already
under the source root is skipped, and one that appears there meanwhile (the
PUT's 412) counts as present, not failed; ``--dryrun`` queries and lists
but copies nothing. The MAST token is read from ``MAST_API_TOKEN``.
The source root is the run's ``intake.source_root``, given once in a shell
variable::

    SRC=s3://<bucket>/<prefix>/
    python scripts/stream_l2_files.py 1049 --source-root "$SRC"
    rapidpipe run create ... --set "intake.source_root=\\"$SRC\\""

Prints the counts found and to copy, one line per failed file, the count
found present at the PUT, the elapsed time, and exits 1 if any copy failed.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import numpy as np
import requests
from astropy.time import Time
from botocore.exceptions import ClientError
from astroquery.mast import MastMissions, conf

from rapidpipe.products import storage

#: The search columns; MastMissions adds fileSetName, the dataset id.
COLUMNS = ["ra", "dec", "program", "execution_plan", "pass", "segment", "visit", "observation",
           "optical_element", "exposure_type", "instrument_name", "detector", "productLevel",
           "product_type", "exposure_time", "exposure_start_time", "exposure_end_time", "fileSetName",
           "guide_star_id"]


def guided(exposure, key_colnames) -> bool:
    """Whether an exposure is guided: at least three of its detectors name a real guide star, not a ``PGS`` placeholder.

    Parameters
    ----------
    exposure : astropy.table.Table
        One exposure's rows, a group of the search result by ``exposure_start_time``.
    key_colnames : tuple of str
        The grouping columns, passed by ``TableGroups.filter`` and unused.

    Returns
    -------
    bool
        True if three or more rows have a guide star id not starting with ``PGS``.
    """
    return len(exposure) - int(np.char.startswith(np.asarray(exposure["guide_star_id"], dtype=str), "PGS").sum()) >= 3


def search_criteria(args: argparse.Namespace) -> dict:
    """The MAST search terms for the program and the optional cuts the arguments name.

    Parameters
    ----------
    args : argparse.Namespace
        The parsed arguments of :func:`main`.

    Returns
    -------
    dict
        Criteria for ``MastMissions.query_criteria``: the program, WFI, the filter or every
        imaging element, and each pass, segment, visit and MJD bound given.
    """
    search = {"program": args.program, "instrument_name": "WFI",
              "optical_element": args.filter if args.filter is not None else ["!GRISM", "!PRISM"]}
    for column, value in (("pass", args.pass_num), ("segment", args.segment), ("visit", args.visit)):
        if value is not None:
            search[column] = value
    if args.mjdmin is not None:
        search["exposure_start_time"] = f">{Time(args.mjdmin, format='mjd').fits}"
    if args.mjdmax is not None:
        search["exposure_end_time"] = f"<{Time(args.mjdmax, format='mjd').fits}"
    return search


def l2_filenames(missions, args: argparse.Namespace) -> list[str]:
    """The program's L2 file names the search, the guiding cut and the detector cut select.

    Parameters
    ----------
    missions : astroquery.mast.MastMissionsClass
        A logged-in Roman mission search.
    args : argparse.Namespace
        The parsed arguments of :func:`main`.

    Returns
    -------
    list of str
        Sorted, unique ``<fileSetName>_cal.asdf`` names.
    """
    results = missions.query_criteria(**search_criteria(args), select_cols=list(COLUMNS), limit=50000,
                                      productLevel=2)
    if not args.unguided:
        results = results.group_by("exposure_start_time").groups.filter(guided)
    if args.detector is not None:
        results = results[results["detector"] == f"WFI{args.detector:02d}"]
    return [f"{fileset}_cal.asdf" for fileset in np.unique(results["fileSetName"])]


def remove_existing(client, bucket: str, prefix: str, filenames: list[str]) -> list[str]:
    """The names not yet under the source root's prefix, which is the only prefix listed.

    Parameters
    ----------
    client : botocore.client.S3
        The S3 client.
    bucket : str
        The source root's bucket.
    prefix : str
        The source root's key prefix, ending in ``/`` or empty.
    filenames : list of str
        The L2 file names to copy.

    Returns
    -------
    list of str
        Sorted names with no key ``<prefix><name>``.
    """
    existing = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        existing.update(item["Key"][len(prefix):] for item in page.get("Contents", []))
    return sorted(set(filenames) - existing)


def download_url(filename: str) -> str:
    """MAST's Roman retrieval URL for one product, as astroquery's ``MastMissions.download_file`` builds it.

    Parameters
    ----------
    filename : str
        The product's file name.

    Returns
    -------
    str
        The URL, which answers with a redirect to a short-lived presigned S3 URL.
    """
    return f"{conf.server}/search/roman/api/v0.1/retrieve_product?product_name={quote(filename, safe='')}"


def transfer(client, token: str, bucket: str, prefix: str, filename: str) -> bool:
    """Copy one product from its MAST presigned URL to ``<prefix><filename>`` without decoding it.

    The download is spooled to a temporary file (``TMPDIR``) and written by
    one ``put_object`` with ``IfNoneMatch="*"``: a multipart upload cannot
    carry the condition, and a single PUT takes up to 5 GB (an L2 file is
    about 208 MB).

    Parameters
    ----------
    client : botocore.client.S3
        The S3 client.
    token : str
        The MAST API token.
    bucket : str
        The source root's bucket.
    prefix : str
        The source root's key prefix, ending in ``/`` or empty.
    filename : str
        The L2 file name.

    Returns
    -------
    bool
        True when copied, False when the key was already there (412).
    """
    # The presigned URL is short-lived, so it is fetched right before use.
    reply = requests.get(download_url(filename), headers={"Authorization": f"token {token}"},
                         allow_redirects=False, timeout=60)
    reply.raise_for_status()
    with requests.get(reply.headers["Location"], stream=True, timeout=60) as source, \
            tempfile.TemporaryFile() as spool:
        source.raise_for_status()
        shutil.copyfileobj(source.raw, spool)
        spool.seek(0)
        try:
            client.put_object(Bucket=bucket, Key=prefix + filename, Body=spool, IfNoneMatch="*")
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") == "PreconditionFailed":
                return False
            raise
    return True


def main(argv: list[str] | None = None) -> int:
    """Query MAST for the program's L2 files and copy the ones not yet under the source root.

    Parameters
    ----------
    argv : list of str, optional
        The arguments; ``sys.argv[1:]`` when None.

    Returns
    -------
    int
        0 when every copy succeeded (or ``--dryrun``), 1 when any failed.
    """
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("program", type=int, help="Roman program ID, e.g. 1049")
    p.add_argument("--source-root", required=True,
                   help="where the files go, s3://bucket/prefix/ with its trailing /: the run's intake.source_root")
    p.add_argument("--filter", default=None, help="filter name, e.g. F158; default is all imaging filters")
    p.add_argument("--detector", type=int, default=None, help="detector (SCA) number, 1-18; default is all")
    p.add_argument("--pass", type=int, default=None, dest="pass_num", help="pass number; default is all")
    p.add_argument("--segment", type=int, default=None, help="segment number; default is all")
    p.add_argument("--visit", type=int, default=None, help="visit number; default is all")
    p.add_argument("--mjdmin", type=float, default=None, help="minimum exposure start MJD; default is all time")
    p.add_argument("--mjdmax", type=float, default=None, help="maximum exposure end MJD; default is all time")
    p.add_argument("--dryrun", action="store_true", help="query the archive and the source root but copy nothing")
    p.add_argument("--unguided", action="store_true", help="include unguided exposures")
    p.add_argument("--workers", type=int, default=1, help="number of files to copy in parallel")
    p.add_argument("--verbose", action="store_true", help="with --dryrun, print the name of every file to copy")
    args = p.parse_args(argv)
    try:
        bucket, prefix = storage.split_source_root(args.source_root)
    except ValueError as exc:
        p.error(str(exc))
    token = os.environ.get("MAST_API_TOKEN")
    if not token:
        p.error("MAST_API_TOKEN is not set")

    start = datetime.datetime.now()
    missions = MastMissions(mission="roman")
    missions.login(token=token)
    filenames = l2_filenames(missions, args)
    print(f"{len(filenames)} identified")
    client = storage.s3_client()
    filenames = remove_existing(client, bucket, prefix, filenames)
    print(f"{len(filenames)} to download")
    failed, present = [], 0
    if args.dryrun:
        if args.verbose:
            for filename in filenames:
                print(filename)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(transfer, client, token, bucket, prefix, name): name for name in filenames}
            for future in as_completed(futures):
                try:
                    present += not future.result()
                except Exception as err:  # noqa: BLE001 - one file's failure is reported, the rest go on
                    failed.append(futures[future])
                    print(f"FAILED {futures[future]}: {err}")
        if present:
            print(f"{present} already present")
        if failed:
            print(f"{len(failed)} files failed: {sorted(failed)}")
    elapsed_min = (datetime.datetime.now() - start).total_seconds() / 60
    print(f"Elapsed: {elapsed_min:.2f} min")
    if filenames:
        print(f"{elapsed_min / len(filenames)} min per file")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
