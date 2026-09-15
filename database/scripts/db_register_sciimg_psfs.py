"""
Register the science PSFs of one sealed PSF generation in the PSFs database table.

Ported from dev (Russ Laher, db_register_sciimg_psfs.py and rapid_db 70565e0c) for
the SMDC layout.  The PSFs live under a sealed inputs-bucket generation, one object
per detector:

    s3://roman-rapid-inputs-gbtds-sim/g0006-psf-f146/psfs/sciimage_psf_f146_sca07.fits

A generation is complete when its `_manifest.json` exists -- it is written last, as
the completeness marker (rapid_systems/docs/reference/gbtds-20260821-staging-provenance.md,
Seal) -- and this script refuses a generation without one: an abandoned prefix
(g0004-psf-f146 is one) must not become a durable database pointer.

Each object under --prefix whose name matches sciimage_psf_<filter>_sca<NN>.fits is
downloaded, MD5-summed, and inserted through the schema's addPSF stored function at
vbest = 0 and version = max + 1 for its (fid, sca): insert never promotes
(design/catalog.md, Promotion).  With --promote, updatePSF then promotes each new row
to vbest = 1 -- the addPSF + updatePSF pair the g0002-psf carry was registered with
(rapid_systems/docs/reference/psf-carry-provenance.md, Registration) -- as a separate,
deliberate step, so which PSF the next science unit runs with changes only when asked.
An operator-pinned vbest = 2 is never demoted (updatePSF refuses and leaves the new
row at 0); the script reports the vbest each row actually holds after the call.

The objects registered are the science PSFs of ONE filter, named by --filter (the
token in the filename, e.g. f146) and identified by --fid; an object whose filename
carries another filter's token is refused rather than registered under the wrong
identity, since PSFs is keyed by (fid, sca).  --filter and --fid are also checked
against each other: the Filters row for --fid must carry the same three-digit
wavelength code as --filter (Filters names the bands W146, H158, Z087, ...; the PSF
filenames say f146, f158, f087), so `--filter f146 --fid 7` is refused before any row
is written instead of filing eighteen F146 PSFs under Z087.

The seal check tells absence from failure: only a 404 on `_manifest.json` means the
generation is unsealed.  Any other S3 error (AccessDenied, a throttled or failed
request) stops the run with its own message, because "not sealed" is a statement
about the generation and a permission problem is not.

Every row lands in ONE transaction on the connection this script opens
(database.modules.utils.rapid_db_connect, the sanctioned path; the writes go through
pipeline.repositories.psfs, since RAPIDDB is frozen): either every detector's row is
inserted or none is, and the promotion requests go with them.

The filename stored is the full s3:// URI, which is what submission hands the science
job as its psf_uri fact and what pipeline/stages/reference_psf.py locates the
reference PSF from (the refimage_psfs/ sibling of psfs/ in the same generation).

Runs on rapid-admin with the database environment set (the endpoint and credential
rapid_db_connect reads) and an identity that can read the inputs bucket:

    python3 -m database.scripts.db_register_sciimg_psfs \\
        --prefix s3://roman-rapid-inputs-gbtds-sim/g0006-psf-f146/psfs/ \\
        --filter f146 --fid 8 [--promote] [--dry-run]

--dry-run lists what would be registered and touches neither the database nor S3
beyond the listing and the seal check.
"""

import argparse
import os
import posixpath
import re
import sys
import tempfile

# The database modules (psycopg2 underneath) and boto3 are imported inside main(),
# after argument checking and the dry run: the pure helpers above main() are what
# database/scripts/test/test_db_register_sciimg_psfs.py exercises, and they import
# anywhere that way -- no stub modules standing in for psycopg2's package tree.

swname = "db_register_sciimg_psfs.py"
swvers = "2.2"

#: One science PSF per detector: sciimage_psf_<filter>_sca<NN>.fits.
PSF_OBJECT_PATTERN = re.compile(r"^sciimage_psf_(?P<filter>[a-z]\d{3})_sca(?P<sca>\d{2})\.fits$",
                                re.IGNORECASE)

#: The directory the science PSFs live in under a generation.
SCIENCE_PSF_DIRECTORY = "psfs"

#: A generation's completeness marker, written last by the staging procedure.
GENERATION_MANIFEST = "_manifest.json"

#: The botocore error codes that mean "no such object" on a head_object, as
#: pipeline/runtime/boundaries.py classifies them.  Everything else is a failure to
#: find out, not a finding.
NOT_FOUND_CODES = ("404", "NoSuchKey", "NotFound")

#: The three-digit wavelength code shared by a Filters name (W146) and a PSF
#: filename's filter token (f146).
WAVELENGTH_CODE = re.compile(r"(\d{3})")


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


def generation_manifest_key(key_prefix):

    """
    The key of the generation's _manifest.json for a psfs/ key prefix, or a ValueError
    when the prefix is not a generation's psfs/ directory.
    """

    parent, leaf = posixpath.split(key_prefix.rstrip("/"))

    if leaf != SCIENCE_PSF_DIRECTORY or not parent:
        raise ValueError("--prefix must be a generation's {}/ directory, got {!r}".format(
            SCIENCE_PSF_DIRECTORY, key_prefix))

    return posixpath.join(parent, GENERATION_MANIFEST)


def select_psf_objects(keys, key_prefix, filter_name=None):

    """
    The (key, sca) pairs among keys that are science PSFs directly under key_prefix.

    Objects deeper than one level, and names that do not match PSF_OBJECT_PATTERN, are
    skipped: a generation's psfs/ directory may carry other files.  With filter_name
    given, a science PSF whose filename carries a DIFFERENT filter token is an error
    (ValueError naming it): every row is registered under one --fid, and a second
    filter's PSF under it would become a version of the wrong (fid, sca) identity,
    promotable over the right one.
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

        if filter_name is not None and \
                string_match.group("filter").lower() != filter_name.lower():
            raise ValueError("{} is a {} PSF; this registration is for {} (--filter)".format(
                key, string_match.group("filter"), filter_name))

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


def s3_error_code(error):

    """
    The botocore error code carried by a ClientError ("404", "AccessDenied", ...),
    or "" when the exception carries none.
    """

    response = getattr(error, "response", None)

    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))

    return ""


def generation_is_sealed(s3_client, bucket, manifest_key):

    """
    Whether the generation's completeness marker exists.

    False only when S3 says the marker is absent (NOT_FOUND_CODES).  Any other
    ClientError -- AccessDenied above all -- is re-raised: the caller could not find
    out whether the generation is sealed, which is not the same as it being unsealed,
    and reporting it as "abandoned or still being staged" would send an operator to
    look at the wrong thing.
    """

    try:
        s3_client.head_object(Bucket=bucket, Key=manifest_key)
    except s3_client.exceptions.ClientError as error:
        if s3_error_code(error) in NOT_FOUND_CODES:
            return False
        raise

    return True


def filter_codes_agree(filter_token, filters_name):

    """
    Whether a --filter token (f146) and a Filters-table name (W146) denote the same
    band: both carry one three-digit wavelength code and the codes are equal.

    Filters (rapid_systems 009-seed-data.sql) names the bands with Roman's letter
    designations -- F184 H158 J129 K213 R062 Y106 Z087 W146 -- while the PSF filenames
    carry f<nnn>, so the letter cannot be compared and the code is what identifies the
    band in both spellings.  Either side without exactly one code is a disagreement.
    """

    codes = []

    for value in (filter_token, filters_name):
        found = WAVELENGTH_CODE.findall(value or "")
        if len(found) != 1:
            return False
        codes.append(found[0])

    return codes[0] == codes[1]


def main(argv=None):

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefix", required=True,
                        help="s3://<bucket>/<generation>/psfs/ holding the science PSFs")
    parser.add_argument("--filter", required=True,
                        help="filter token in the PSF filenames, e.g. f146; any other filter's PSF under the prefix is refused")
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

    try:
        bucket, key_prefix = split_s3_prefix(args.prefix)
        manifest_key = generation_manifest_key(key_prefix)
    except ValueError as error:
        print("*** Error: {}; quitting...".format(error))
        return 64

    import boto3
    s3_client = boto3.client("s3")

    try:
        sealed = generation_is_sealed(s3_client, bucket, manifest_key)
    except s3_client.exceptions.ClientError as error:
        print("*** Error: Could not check s3://{}/{} ({}); whether the generation is "
              "sealed is unknown, not 'no'; quitting...".format(
                  bucket, manifest_key, s3_error_code(error) or error))
        return 66

    if not sealed:
        print("*** Error: s3://{}/{} does not exist, so the generation is not complete "
              "(abandoned, or still being staged); quitting...".format(bucket, manifest_key))
        return 64

    try:
        objects = select_psf_objects(list_keys(s3_client, bucket, key_prefix), key_prefix,
                                     filter_name=args.filter)
    except ValueError as error:
        print("*** Error: {}; quitting...".format(error))
        return 64

    if not objects:
        print("*** Error: No science PSF objects under {}; quitting...".format(args.prefix))
        return 64

    print("Found {} science PSF objects under {} (generation sealed: s3://{}/{}):".format(
        len(objects), args.prefix, bucket, manifest_key))

    for key, sca in objects:
        print("    sca {:2d}  s3://{}/{}".format(sca, bucket, key))

    if args.dry_run:
        print("Dry run: nothing registered.")
        return 0

    import database.modules.utils.rapid_db as db
    import database.modules.utils.rapid_db_connect as db_connect
    from pipeline.repositories.psfs import PsfRepository


    # Download and checksum every PSF before any row is written, so a bad object stops
    # the run with the database untouched.  The downloads are only needed for their
    # checksums, so the directory is released as soon as those are in hand.

    rows = []

    with tempfile.TemporaryDirectory(prefix="register_psfs_") as workdir:

        for key, sca in objects:

            filename = os.path.join(workdir, os.path.basename(key))

            print("Downloading s3://{}/{} into {}...".format(bucket, key, filename))
            s3_client.download_file(bucket, key, filename)

            checksum = db.compute_checksum(filename)

            if not isinstance(checksum, str):
                print("*** Error: Could not compute checksum of {} (code {}); quitting...".format(filename, checksum))
                return 65

            rows.append((sca, "s3://{}/{}".format(bucket, key), checksum))


    # One connection, one transaction: every row or none.

    not_promoted = []

    with db_connect.connection(swname) as conn:

        repo = PsfRepository(conn)


        # --fid names the identity every row lands under; make sure it is the filter
        # the files say they are, before the first write.

        filters_name = repo.filter_name(args.fid)

        if filters_name is None:
            print("*** Error: fid = {} is not in the Filters table; quitting...".format(args.fid))
            return 64

        if not filter_codes_agree(args.filter, filters_name):
            print("*** Error: --fid {} is {} in the Filters table, but --filter says {}; "
                  "nothing registered; quitting...".format(args.fid, filters_name, args.filter))
            return 64

        try:

            for sca, psf_filename, checksum in rows:

                row = repo.add(args.fid, sca, psf_filename, checksum, args.status)

                print("Inserted psfid = {}, version = {}, vbest = 0 for fid = {}, sca = {}".format(
                    row.psfid, row.version, args.fid, sca))

                if args.promote:

                    vbest = repo.promote(row.psfid, psf_filename, checksum, args.status, row.version)

                    if vbest == 1:
                        print("Promoted psfid = {} to vbest = 1".format(row.psfid))
                    else:
                        not_promoted.append((sca, row.psfid))
                        print("psfid = {} stays at vbest = {}: the incumbent for fid = {}, sca = {} is pinned (vbest = 2)".format(
                            row.psfid, vbest, args.fid, sca))

            conn.commit()

        except Exception as error:

            conn.rollback()
            print("*** Error: {}; transaction rolled back, nothing registered; quitting...".format(error))
            return 67

    if not args.promote:
        outcome = "vbest = 0, not promoted"
    elif not_promoted:
        outcome = "{} promoted to vbest = 1, {} left at vbest = 0 behind a pinned incumbent (sca {})".format(
            len(rows) - len(not_promoted), len(not_promoted),
            ", ".join(str(sca) for sca, _psfid in not_promoted))
    else:
        outcome = "all promoted to vbest = 1"

    print("Registered {} PSFs for fid = {} ({})".format(len(rows), args.fid, outcome))

    return 0


if __name__ == "__main__":
    sys.exit(main())
