import os
import csv
import configparser
import re
import math
import boto3


swname = "awsBatchSubmitJobs_runSingleSciencePipeline.py"
swvers = "1.0"

print("swname =", swname)
print("swvers =", swvers)


# JOBPROCDATE of pipeline job.

job_proc_date = os.getenv('JOBPROCDATE')

if job_proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# RAPID_JOB_ID of pipeline job.

jid = os.getenv('RAPID_JOB_ID')

if jid is None:

    print("*** Error: Env. var. RAPID_JOB_ID not set; quitting...")
    exit(64)


# JOBS3BUCKET of pipeline job.

job_info_s3_bucket = os.getenv('JOBS3BUCKET')

if job_info_s3_bucket is None:

    print("*** Error: Env. var. JOBS3BUCKET not set; quitting...")
    exit(64)


# JOBCONFIGOBJNAME of pipeline job.

job_config_ini_file_s3_bucket_object_name = os.getenv('JOBCONFIGOBJNAME')

if job_config_ini_file_s3_bucket_object_name is None:

    print("*** Error: Env. var. JOBCONFIGOBJNAME not set; quitting...")
    exit(64)


# REFIMAGEINPUTSOBJNAME of pipeline job.

input_images_csv_file_s3_bucket_object_name = os.getenv('REFIMAGEINPUTSOBJNAME')

if input_images_csv_file_s3_bucket_object_name is None:

    print("*** Error: Env. var. REFIMAGEINPUTSOBJNAME not set; quitting...")
    exit(64)



print("job_proc_date =",job_proc_date)
print("jid =",jid)
print("job_info_s3_bucket =",job_info_s3_bucket)
print("job_config_ini_file_s3_bucket_object_name =",job_config_ini_file_s3_bucket_object_name)
print("input_images_csv_file_s3_bucket_object_name =",input_images_csv_file_s3_bucket_object_name)






def build_awaicgen_command_line_args(ra0_refimage,
                                     dec0_refimage,
                                     cdelt1_refimage,
                                     crota2_refimage,
                                     refimage_inputs_txt_file,
                                     awaicgen_dict):

    '''
    Build awaicgen command line.
    '''

# ./awaicgen -f1 inplist.txt -X 1.72 -Y 1.72 -R 219.26262 -D -12.79843 -C 72.43 -pa 1.5 -wf 0 -sf 1 -sc 1 -nt 24 -o1 output2.fits -v

    software_to_execute = 'awaicgen'
    awaicgen_mosaic_size_x = int(awaicgen_dict["awaicgen_mosaic_size_x"])
    awaicgen_mosaic_size_y = int(awaicgen_dict["awaicgen_mosaic_size_y"])

    pixel_scale = math.fabs(cdelt1_refimage)
    X = pixel_scale * float(awaicgen_mosaic_size_x)
    Y = pixel_scale * float(awaicgen_mosaic_size_y)

    awaicgen_mosaic_size_y = float(awaicgen_dict["awaicgen_mosaic_size_y"])


    code_to_execute_args = [software_to_execute]
    code_to_execute_args.append("-f1")
    code_to_execute_args.append(refimage_inputs_txt_file)
    code_to_execute_args.append("-X")
    code_to_execute_args.append(X)
    code_to_execute_args.append("-Y")
    code_to_execute_args.append(Y)
    code_to_execute_args.append("-R")
    code_to_execute_args.append(ra0_refimage)
    code_to_execute_args.append("-D")
    code_to_execute_args.append(dec0_refimage)
    code_to_execute_args.append("-C")
    code_to_execute_args.append(crota2_refimage)





    print("code_to_execute_args =",code_to_execute_args)

    return code_to_execute_args








if __name__ == '__main__':



    # Download job configuration data file from S3 bucket.

    job_config_ini_file_filename = "job_config_jid" + str(jid) + ".ini"
    input_images_csv_filename = "input_images_for_refimage_jid"+ str(jid) + ".csv"

    s3_client = boto3.client('s3')

    print("Downloading s3://{}/{} into {}...".format(job_info_s3_bucket,job_config_ini_file_s3_bucket_object_name,job_config_ini_file_filename))

    response = s3_client.download_file(job_info_s3_bucket,job_config_ini_file_s3_bucket_object_name,job_config_ini_file_filename)

    print("response =",response)


    # Read in job configuration parameters from .ini file.

    config_input = configparser.ConfigParser()
    config_input.read(job_config_ini_file_filename)

    verbose = int(config_input['DEFAULT']['verbose'])
    debug = int(config_input['DEFAULT']['debug'])
    job_info_s3_bucket_base = config_input['DEFAULT']['job_info_s3_bucket_base']
    product_s3_bucket_base = config_input['DEFAULT']['product_s3_bucket_base']

    ppid_sciimage = int(config_input['SCI_IMAGE']['ppid'])

    rid_sciimage = int(config_input['SCI_IMAGE']['rid'])
    sca_sciimage = int(config_input['SCI_IMAGE']['sca'])
    fid_sciimage = int(config_input['SCI_IMAGE']['fid'])
    s3_bucket_object_name_sciimage = config_input['SCI_IMAGE']['objectname_sciimage']
    expid_sciimage = int(config_input['SCI_IMAGE']['expid'])
    field_sciimage = int(config_input['SCI_IMAGE']['field'])
    mjdobs_sciimage = float(config_input['SCI_IMAGE']['mjdobs'])
    exptime_sciimage = float(config_input['SCI_IMAGE']['exptime'])
    infobits_sciimage = int(config_input['SCI_IMAGE']['infobits'])
    status_sciimage = int(config_input['SCI_IMAGE']['status'])

    ra0_sciimage = float(config_input['SCI_IMAGE']['ra0'])
    dec0_sciimage = float(config_input['SCI_IMAGE']['dec0'])
    ra1_sciimage = float(config_input['SCI_IMAGE']['ra1'])
    dec1_sciimage = float(config_input['SCI_IMAGE']['dec1'])
    ra2_sciimage = float(config_input['SCI_IMAGE']['ra2'])
    dec2_sciimage = float(config_input['SCI_IMAGE']['dec2'])
    ra3_sciimage = float(config_input['SCI_IMAGE']['ra3'])
    dec3_sciimage = float(config_input['SCI_IMAGE']['dec3'])
    ra4_sciimage = float(config_input['SCI_IMAGE']['ra4'])
    dec4_sciimage = float(config_input['SCI_IMAGE']['dec4'])

    sky_tile_rtid = int(config_input['SKY_TILE']['rtid'])
    sky_tile_ra0 = float(config_input['SKY_TILE']['ra0'])
    sky_tile_dec0 = float(config_input['SKY_TILE']['dec0'])
    sky_tile_ra1 = float(config_input['SKY_TILE']['ra1'])
    sky_tile_dec1 = float(config_input['SKY_TILE']['dec1'])
    sky_tile_ra2 = float(config_input['SKY_TILE']['ra2'])
    sky_tile_dec2 = float(config_input['SKY_TILE']['dec2'])
    sky_tile_ra3 = float(config_input['SKY_TILE']['ra3'])
    sky_tile_dec3 = float(config_input['SKY_TILE']['dec3'])
    sky_tile_ra4 = float(config_input['SKY_TILE']['ra4'])
    sky_tile_dec4 = float(config_input['SKY_TILE']['dec4'])

    rfid = int(config_input['REF_IMAGE']['rfid'])
    ppid_refimage = int(config_input['REF_IMAGE']['ppid_refimage'])
    naxis1_refimage = int(config_input['REF_IMAGE']['naxis1_refimage'])
    naxis2_refimage = int(config_input['REF_IMAGE']['naxis2_refimage'])
    cdelt1_refimage = float(config_input['REF_IMAGE']['cdelt1_refimage'])
    cdelt2_refimage = float(config_input['REF_IMAGE']['cdelt2_refimage'])
    crota2_refimage = float(config_input['REF_IMAGE']['crota2_refimage'])
    ra0_refimage = float(config_input['REF_IMAGE']['ra0_refimage'])
    dec0_refimage = float(config_input['REF_IMAGE']['dec0_refimage'])
    ra1_refimage = float(config_input['REF_IMAGE']['ra1_refimage'])
    dec1_refimage = float(config_input['REF_IMAGE']['dec1_refimage'])
    ra2_refimage = float(config_input['REF_IMAGE']['ra2_refimage'])
    dec2_refimage = float(config_input['REF_IMAGE']['dec2_refimage'])
    ra3_refimage = float(config_input['REF_IMAGE']['ra3_refimage'])
    dec3_refimage = float(config_input['REF_IMAGE']['dec3_refimage'])
    ra4_refimage = float(config_input['REF_IMAGE']['ra4_refimage'])
    dec4_refimage = float(config_input['REF_IMAGE']['dec4_refimage'])

    awaicgen_dict = config_input['AWAICGEN']

    swarp_dict = config_input['SWARP']

    sextractor_dict = config_input['SEXTRACTOR']


    # Optionally read in CVS file containing inputs for generating reference image.

        if rfid is None:

            print("Downloading s3://{}/{} into {}...".format(job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name,input_images_csv_filename))

            response = s3_client.download_file(job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name,input_images_csv_filename)

            print("response =",response)

            refimage_input_metadata = []
            refimage_input_filenames = []

            with open(input_images_csv_filename, newline='') as csvfile:

                refimage_inputs_reader = csv.reader(csvfile, delimiter=',')

                for row in refimage_inputs_reader:

                    print(', '.join(row))

                    refimage_input_metadata.append(row)

                    refimage_input_s3_full_name = row[11]                                                   # TODO

                    filename_match = re.match(r"s3://(.+?)/(.+)", refimage_input_s3_full_name)              # TODO

                    try:
                        refimage_input_s3_bucket_name = filename_match.group(1)
                        refimage_input_s3_object_name = filename_match.group(2)
                        print("s3_bucket_name = {}, s3_object_name = {}".\
                            format(refimage_input_s3_bucket_name,refimage_input_s3_object_name))

                    except:
                        print("*** Error: Could not parse refimage_input_s3_full_name; quitting...")
                        exit(64)

                    filename_match2 = re.match(r".+?/(.+)", refimage_input_s3_object_name)                 # TODO

                    try:
                        refimage_input_filename = filename_match2.group(1)
                        print("refimage_input_filename = {}".format(refimage_input_filename))

                    except:
                        print("*** Error: Could not parse refimage_input_s3_object_name; quitting...")
                        exit(64)

                    refimage_input_filenames.append(refimage_input_filename)


                    # Download reference-image input from associated S3 bucket.

                    print("Downloading s3://{}/{} into {}...".format(refimage_input_s3_bucket_name,refimage_input_s3_object_name,refimage_input_filename))

                    response = s3_client.download_file(refimage_input_s3_bucket_name,refimage_input_s3_object_name,refimage_input_filename)

                    print("response =",response)


            # Write list of reference-image input filenames for awaicgen.

            refimage_inputs_txt_file = 'refimage_inputs.txt'
            f = open(refimage_inputs_txt_file, "w")
            for fname in refimage_input_filenames:
                f.write(fname + "\n")
            f.close()


            # Execute awaicgen to generate reference image.

            awaicgen_cmd = build_awaicgen_command_line_args(ra0_refimage,
                                                            dec0_refimage,
                                                            cdelt1_refimage,
                                                            crota2_refimage,
                                                            refimage_inputs_txt_file,
                                                            awaicgen_dict)
            exitcode_from_awaicgen = util.execute_command(awaicgen_cmd)



    exit(0)
