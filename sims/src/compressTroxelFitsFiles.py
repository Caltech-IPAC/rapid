import boto3
import os
import numpy as np
from astropy.io import fits
import re

from pipeline.runtime.process import run_tool
from pipeline.runtime.errors import ToolError

bucket_name = 'sims-sn-h158'

run_tool(['mkdir', 'new'])

os.chdir('new')
print("CWD =",os.getcwd())


s3 = boto3.resource('s3')

my_bucket = s3.Bucket(bucket_name)

nfiles = 0

for my_bucket_object in my_bucket.objects.all():

    print(my_bucket_object.key)

    gzfname_input = my_bucket_object.key

    if "_lite/" in gzfname_input:
        print("...skipping")
        continue

    #if nfiles > 18:
    #    exit(0)

    filename_match = re.match(r"(.+)/(.+\.fits\.gz)",gzfname_input)

    try:
        subdir_only = filename_match.group(1)
        only_gzfname_input = filename_match.group(2)
        print("-----0-----> subdir_only =",subdir_only)
        print("-----1-----> only_gzfname_input =",only_gzfname_input)

    except:
        print("-----2-----> No match in",gzfname_input)

    file_splitext = os.path.splitext(only_gzfname_input)
    fname_input = file_splitext[0]
    fname_output = fname_input.replace(".fits","_lite.fits")
    gzfname_output = fname_output + ".gz"

    print("fname_input =",fname_input)
    print("fname_output =",fname_output)


    # Check if lite FITS file already exists.

    file_to_check = "s3://" + bucket_name + "/" + subdir_only + "_lite/" + gzfname_output
    try:
        run_tool(['aws', 's3', 'ls', file_to_check])
    except ToolError:
        pass
    else:
        print("*** Warning: File exists in S3 bucket ({}); skipping...".format(file_to_check))
        continue

    nfiles += 1


    run_tool(['aws', 's3', 'cp', "s3://" + bucket_name + "/" + subdir_only + "/" + only_gzfname_input, '.'])

    run_tool(['gunzip', only_gzfname_input])


    print("Reducing size of FITS file...")

    hdul_input = fits.open(fname_input)

    ffis = ["SCI"]

    hdu_list = []

    primary_header = hdul_input[0].header
    hdu_list.append(fits.PrimaryHDU(data=None,header=primary_header))

    for ffi in ffis:
        data = hdul_input[ffi].data
        header = hdul_input[ffi].header

        hdu = fits.ImageHDU(data.astype(np.float32),header)
        hdu_list.append(hdu)

    hdu = fits.HDUList(hdu_list)
    hdu.writeto(fname_output,overwrite=True,checksum=True)


    run_tool(['rm', fname_input])

    run_tool(['gzip', fname_output])

    run_tool(['aws', 's3', 'cp', gzfname_output,
              "s3://" + bucket_name + "/" + subdir_only + "_lite/" + gzfname_output])

    run_tool(['rm', gzfname_output])
