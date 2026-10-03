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


# Query with column criteria
results = missions.query_criteria(
    **search,
    select_cols=col_list,
)

if mjdmin is not None:
    good_indx_min = Time(results['exposure_start_time']).mjd > mjdmin
else:
    good_indx_min = np.bool_(np.ones(len(results)))

if mjdmax is not None:
    good_indx_max = Time(results['exposure_end_time']).mjd < mjdmax
else:
    good_indx_max = np.bool_(np.ones(len(results)))

filtered_results = results[good_indx_min & good_indx_max]

products = missions.get_unique_product_list(filtered_results)
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
