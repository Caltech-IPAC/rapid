import boto3
import os
import numpy as np
from astropy.io import fits
import subprocess
import re


def execute_command(cmd,no_check=False):
    print("cmd = ",cmd)
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in p.stdout.readlines():
        print("--->",line)
        strvalue = line.decode('utf-8').strip()
        print(strvalue)
    retval = p.wait()
    print("retval =",retval)

    if not no_check:
        if (retval != 0):
            print("*** Error from execute_command; quitting...")
            exit(1)

    return retval

bucket_name = 'sims-sn-h158'

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

print("aws_access_key_id =",aws_access_key_id)
print("aws_secret_access_key =",aws_secret_access_key)


cmd = "mkdir new"
execute_command(cmd)

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
    cmd = "aws s3 ls " + file_to_check
    retval = execute_command(cmd,no_check=True)

    if (retval == 0):
        print("*** Warning: File exists in S3 bucket ({}); skipping...".format(file_to_check))
        continue

    nfiles += 1


    cmd = "aws s3 cp s3://" + bucket_name + "/" + subdir_only + "/" + only_gzfname_input + " ."
    execute_command(cmd)

    cmd = "gunzip " + only_gzfname_input
    execute_command(cmd)


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


    cmd = "rm " + fname_input
    execute_command(cmd)

    cmd = "gzip " + fname_output
    execute_command(cmd)

    cmd = "aws s3 cp " + gzfname_output + " s3://" + bucket_name + "/" + subdir_only + "_lite/" + gzfname_output
    execute_command(cmd)

    cmd = "rm " + gzfname_output
    execute_command(cmd)
