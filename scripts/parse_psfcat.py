import os
import numpy as np
import configparser
from astropy.table import QTable
from astropy.table import QTable, join
from astropy import units as u

import modules.utils.rapid_pipeline_subs as util

swname = "parse_psfcat.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


# Required environment variables.

rapid_sw = os.getenv('RAPID_SW')

if rapid_sw is None:

    print("*** Error: Env. var. RAPID_SW not set; quitting...")
    exit(64)

rapid_work = os.getenv('RAPID_WORK')

if rapid_work is None:

    print("*** Error: Env. var. RAPID_WORK not set; quitting...")
    exit(64)

cfg_path = rapid_sw + "/cdf"

print("rapid_sw =",rapid_sw)
print("cfg_path =",cfg_path)


# Read input parameters from .ini file.

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

output_psfcat_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_psfcat_filename'])
output_psfcat_finder_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_psfcat_finder_filename'])

psfcat_qtable = QTable.read(output_psfcat_filename,format='ascii')
psfcat_finder_qtable = QTable.read(output_psfcat_finder_filename,format='ascii')

# Inner join on 'id'

joined_table_inner = join(psfcat_qtable, psfcat_finder_qtable, keys='id', join_type='inner')
print("Inner Join:")
print(joined_table_inner)

nrows = len(joined_table_inner)
print("nrows =",nrows)


for row in joined_table_inner:
    id = row['id']
    ra = row['ra']
    dec = row['dec']
    fluxfit = row['flux_fit']
    roundness1 = row['roundness1']

    print(id,ra,dec,fluxfit,roundness1)



exit(0)
