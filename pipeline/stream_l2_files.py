import argparse
import os
import fsspec
from astropy.time import Time
from astroquery.mast import MastMissions
import asdf
import numpy as np
"""
Download files from MAST given a program id and other optional parameters
"""

p = argparse.ArgumentParser(
    description="<One-line description of what the script does.>")

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
search = {'program': program}
if roman_filter is not None:
    search['optical_element']=roman_filter
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
)

products = missions.get_unique_product_list(results)
filtered_products = missions.filter_products(products, file_suffix='_cal')
print(f"{len(filtered_products)} to download")

for filename in filtered_products['filename']:
    if dryrun is True:
        print(filename)
    else:
        af = missions.read_product(filename, lazy_load=False, memmap=False)
        if ingest_bucket=='local':
            af.write_to(filename)
        else:
            s3_url = f"s3://{ingest_bucket}/{filename}"
            with fsspec.open(s3_url, "wb") as f:
                af.write_to(f)
