"""
Register the science PSFs of one PSF generation in the PSFs database table.

Ported from dev (Russ Laher, db_register_sciimg_psfs.py and rapid_db 70565e0c) for
the SMDC layout.  The PSFs live under a sealed inputs-bucket generation, one object
per detector:

    s3://roman-rapid-inputs-gbtds-sim/g0004-psf-f146/psfs/sciimage_psf_f146_sca07.fits

Each object under --prefix whose name matches sciimage_psf_<filter>_sca<NN>.fits is
downloaded, MD5-summed, and inserted through the schema's addPSF stored function at
vbest = 0 and version = max + 1 for its (fid, sca): insert never promotes
(design/catalog.md, Promotion).  With --promote, updatePSF then promotes each new row
to vbest = 1 -- the addPSF + updatePSF pair the g0002-psf carry was registered with
(rapid_systems/docs/reference/psf-carry-provenance.md, Registration) -- as a separate,
deliberate step, so which PSF the next science unit runs with changes only when asked.
An operator-pinned vbest = 2 is never demoted (updatePSF refuses).

Every row lands in ONE transaction on the connection this script opens, borrowed to
the RAPIDDB handle so its per-call commits are suppressed: either all detectors are
registered (and promoted) or none are.  A partial registration -- some detectors on
the new version, the rest on the old -- is exactly the state the single-transaction
envelope exists to make impossible (design/catalog.md, The commit).

The filename stored is the full s3:// URI, which is what submission hands the science
job as its psf_uri fact and what pipeline/stages/reference_psf.py locates the
reference PSF from (the refimage_psfs/ sibling of psfs/ in the same generation).

Runs on rapid-admin with the database environment set (RAPID_DB_SECRET_ID or
DBUSER/DBPASS, plus DBSERVER/DBPORT/DBNAME) and an identity that can read the inputs
bucket:

    python3 -m database.scripts.db_register_sciimg_psfs \\
        --prefix s3://roman-rapid-inputs-gbtds-sim/g0004-psf-f146/psfs/ --fid 8 \\
        [--promote] [--dry-run]

--dry-run lists what would be registered and touches neither the database nor S3
beyond the listing.
"""

import argparse
import os
import re
import sys
import tempfile

import database.modules.utils.rapid_db as db

swname = "db_register_sciimg_psfs.py"
swvers = "2.0"

#: One science PSF per detector: sciimage_psf_<filter>_sca<NN>.fits.
PSF_OBJECT_PATTERN = re.compile(r"^sciimage_psf_(?P<filter>[a-z]\d{3})_sca(?P<sca>\d{2})\.fits$",
                                re.IGNORECASE)


def split_s3_prefix(prefix):

    """
    (bucket, key prefix) from an s3://bucket/prefix/ URI; the key prefix ends in "/".
    """

    string_match = re.match(r"^s3://([^/]+)/(.*)$", prefix)

    if string_match is None:
        raise ValueError("--prefix must be s3://<bucket>/<prefix>/, got {!r}".format(prefix))

    bucket = string_match.group(1)
    key_prefix = string_match.group(2)

    if key_prefix and not key_prefix.endswith("/"):
        key_prefix += "/"

    return bucket, key_prefix


def select_psf_objects(keys, key_prefix):

    """
    The (key, sca) pairs among keys that are science PSFs directly under key_prefix.

    Objects deeper than one level, and names that do not match PSF_OBJECT_PATTERN, are
    skipped: a generation's psfs/ directory may carry a manifest or other files.
    """

    selected = []

    for key in keys:

        if not key.startswith(key_prefix):
            continue

        name = key[len(key_prefix):]

        if "/" in name:
            continue

        string_match = PSF_OBJECT_PATTERN.match(name)

        if string_match is None:
            continue

        selected.append((key, int(string_match.group("sca"))))

    return sorted(selected, key=lambda pair: pair[1])


def list_keys(s3_client, bucket, key_prefix):

    """
    Every object key under the prefix, across pages.
    """

    keys = []
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix):
        for entry in page.get("Contents", []):
            keys.append(entry["Key"])

    return keys


def main(argv=None):

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefix", required=True,
                        help="s3://<bucket>/<generation>/psfs/ holding the science PSFs")
    parser.add_argument("--fid", required=True, type=int,
                        help="filter id of the PSFs (Filters table), e.g. 8 for F146")
    parser.add_argument("--status", type=int, default=1,
                        help="PSFs.status for the new rows (default 1)")
    parser.add_argument("--promote", action="store_true",
                        help="promote each new row to vbest = 1 via updatePSF")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be registered; no download, no database")
    args = parser.parse_args(argv)

    print("swname =", swname)
    print("swvers =", swvers)

    bucket, key_prefix = split_s3_prefix(args.prefix)

    import boto3
    s3_client = boto3.client("s3")

    objects = select_psf_objects(list_keys(s3_client, bucket, key_prefix), key_prefix)

    if not objects:
        print("*** Error: No science PSF objects under {}; quitting...".format(args.prefix))
        return 64

    print("Found {} science PSF objects under {}:".format(len(objects), args.prefix))

    for key, sca in objects:
        print("    sca {:2d}  s3://{}/{}".format(sca, bucket, key))

    if args.dry_run:
        print("Dry run: nothing registered.")
        return 0

    workdir = tempfile.mkdtemp(prefix="register_psfs_")


    # Download and checksum every PSF before any row is written, so a bad object stops
    # the run with the database untouched.

    rows = []

    for key, sca in objects:

        filename = os.path.join(workdir, os.path.basename(key))

        print("Downloading s3://{}/{} into {}...".format(bucket, key, filename))
        s3_client.download_file(bucket, key, filename)

        checksum = db.compute_checksum(filename)

        if not isinstance(checksum, str):
            print("*** Error: Could not compute checksum of {} (code {}); quitting...".format(filename, checksum))
            return 65

        rows.append((sca, "s3://{}/{}".format(bucket, key), checksum))


    # Open the database connection and borrow it to the handle: the per-call commits in
    # add_psf and update_psf are suppressed, and the one commit below is the transaction.

    owner = db.RAPIDDB()

    if owner.exit_code >= 64:
        return owner.exit_code

    dbh = db.RAPIDDB.borrowing(owner.conn)

    try:

        for sca, psf_filename, checksum in rows:

            dbh.add_psf(args.fid, sca, args.status, psf_filename, checksum)

            if dbh.exit_code >= 64:
                raise RuntimeError("addPSF failed for sca {} (exit code {})".format(sca, dbh.exit_code))

            psfid = dbh.psfid
            version = dbh.version

            print("Inserted psfid = {}, version = {}, vbest = 0 for fid = {}, sca = {}".format(
                psfid, version, args.fid, sca))

            if args.promote:

                dbh.update_psf(psfid, psf_filename, checksum, args.status, version)

                if dbh.exit_code >= 64:
                    raise RuntimeError("updatePSF failed for psfid {} (exit code {})".format(psfid, dbh.exit_code))

                print("Promoted psfid = {} to vbest = 1".format(psfid))

        owner.conn.commit()

    except Exception as error:

        owner.conn.rollback()
        print("*** Error: {}; transaction rolled back, nothing registered; quitting...".format(error))
        owner.close()
        return 67

    owner.close()

    print("Registered {} PSFs for fid = {} ({})".format(
        len(rows), args.fid, "promoted to vbest = 1" if args.promote else "vbest = 0, not promoted"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
