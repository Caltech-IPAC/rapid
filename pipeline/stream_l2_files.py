import argparse
import os
from astropy.time import Time
from astropy.table import Table
from astroquery.mast import MastMissions
import numpy as np
import datetime
import boto3
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed


p = argparse.ArgumentParser(
    description="Download files from MAST given a program id and other optional parameters")

p.add_argument("program", type=int,
                help="Roman program ID, e.g. 999 for the HLTDS")
p.add_argument("--ingest_bucket", type=str, default='rapid-commissioning-ingest-files',
                help="s3 bucket that files will be written to. default: rapid-commissioning-ingest-files")
p.add_argument("--filter", default=None,
                help="filter name, e.g. F158; default is all filters")
p.add_argument("--detector", type=int, default=None,
                help="detector (SCA) number, 1-18; default is all detectors")
p.add_argument("--pass", type=int, default=None, dest="pass_num",
                help="pass number; default is all passes")
p.add_argument("--segment", type=int, default=None,
                help="segment number; default is all segments")
p.add_argument("--visit", type=int, default=None,
                help="visit number; default is all visits")
p.add_argument("--mjdmin", type=float, default=None,
                help="minimum exposure start date; default is all time")
p.add_argument("--mjdmax", type=float, default=None,
                help="maximum exposure start date; default is all time")
p.add_argument("--dryrun", action='store_true',
                help="query the archive but don't download the files")
p.add_argument("--unguided", action='store_true',
                help="include unguided images in archive query")
p.add_argument("--workers", type=int, default=1,
                help="number of files to transfer in parallel")

args = p.parse_args()

program = args.program
roman_filter = args.filter
pass_num = args.pass_num
segment = args.segment
visit = args.visit
detector = args.detector
mjdmin = args.mjdmin
mjdmax = args.mjdmax
ingest_bucket = args.ingest_bucket
dryrun = args.dryrun
unguided = args.unguided
workers = args.workers

s3 = boto3.client('s3')

def transfer_file(filename):
    """
    Stream one product from its MAST presigned S3 URL into the ingest bucket without decoding it.

    Parameters
    ----------
    filename : str
        Roman product filename, e.g. ``r0099..._cal.asdf``.

    Returns
    -------
    filename : str
        The filename that was transferred.
    """
    # MAST answers with a redirect to a short-lived presigned S3 URL; fetch it right before use
    resp = requests.get(missions._get_download_url(filename),
                        headers={"Authorization": f"token {token}"},
                        allow_redirects=False, timeout=60)
    resp.raise_for_status()
    with requests.get(resp.headers["Location"], stream=True, timeout=60) as src:
        src.raise_for_status()
        s3.upload_fileobj(src.raw, ingest_bucket, filename)
    return filename


start = datetime.datetime.now()

# Create MastMissions object and assign mission to 'roman'
missions = MastMissions(mission='roman')

# Login to search and retrieve Roman data
token = os.getenv("MAST_API_TOKEN")
if token is None:
    try:
        with open("mast_api_token.txt") as f:
            token = f.read().strip()
    except FileNotFoundError:
        raise ValueError("MAST token not found!")

missions.login(token=token)

# Make a list of column names to return in the search results. The results will not be ordered
# in this order, so we will re-order later. The fileSetName, which we need for retrieval,
# is not in this list, but will still be returned.
col_list = ['ra', 'dec', 'program', 'execution_plan', 'pass', 'segment', 'visit', 'observation',
            'optical_element', 'exposure_type', 'instrument_name', 'detector', 'productLevel',
            'product_type', 'exposure_time', 'exposure_start_time', 'exposure_end_time', 'fileSetName']

# Create a dictionary of search criteria
search = {'program': program,
          'instrument_name':'WFI'}
if roman_filter is not None:
    search['optical_element']=roman_filter
else:
    search['optical_element'] = ['!GRISM', '!PRISM']
if detector is not None:
    search['detector']=f"WFI{detector:02d}"
if pass_num is not None:
    search['pass'] = pass_num
if segment is not None:
    search['segment'] = segment
if visit is not None:
    search['visit'] = visit
if unguided==False:
    search['guide_star_id']=['!PGS0100001','!PGS0200001','!PGS0300001','!PGS0400001','!PGS0500001','!PGS0600001','!PGS0700001',
                            '!PGS0800001','!PGS0900001','!PGS1000001','!PGS1100001','!PGS1200001','!PGS1300001','!PGS1400001',
                            '!PGS1500001','!PGS1600001', '!PGS1700001', '!PGS100001']
if mjdmin is not None:
    search['exposure_start_time']=f">{Time(mjdmin, format='mjd').fits}"
if mjdmax is not None:
    search['exposure_end_time'] = f"<{Time(mjdmax, format='mjd').fits}"


# Query with column criteria
results = missions.query_criteria(
    **search,
    select_cols=col_list,
    limit=50000,
    productLevel=2
)

# Build L2 filenames directly from the dataset IDs instead of fetching every product
# from MAST; assumes each Roman L2 file is named <fileSetName>_cal.asdf
filesets = np.unique(results['fileSetName'])
l2_filenames = [f"{fs}_cal.asdf" for fs in filesets]
#l2_filenames = [f"{fs}_cal.asdf" for fs in filesets]
print(f"{len(l2_filenames)} to download")

if dryrun is True:
    for filename in l2_filenames:
        print(filename)
else:
    if ingest_bucket=='local':
        missions.download_products(Table({'filename': l2_filenames}),
                                      download_dir='.', flat=True)
    else:
        failed = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(transfer_file, ifile): ifile for ifile in l2_filenames}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as err:
                    failed.append(futures[fut])
                    print(f"FAILED {futures[fut]}: {err}")
        if failed:
            print(f"{len(failed)} files failed: {failed}")
end = datetime.datetime.now()
elapsed_min = (end - start).total_seconds() / 60
print(f"Elapsed: {elapsed_min:.2f} min")
if len(l2_filenames)>0:
    print(f'{elapsed_min/len(l2_filenames)} min per file')