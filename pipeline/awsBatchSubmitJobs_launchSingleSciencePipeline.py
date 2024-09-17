import boto3
import os
from astropy.io import fits
import subprocess
import re
import configparser

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db
import database.modules.utils.roman_tessellation_db as sqlite

swname = "awsBatchSubmitJobs_launchSingleSciencePipeline.py"
swvers = "1.0"

ppid = 15
ppid_refimage = 12

    print("swname =", swname)
    print("swvers =", swvers)


# RID of input file.

rid = os.getenv('RID')

if rid is None:

    print("*** Error: Env. var. RID not set; quitting)
    exit(64)


# Ensure sqlite database that defines the Roman sky tessellation is available.

roman_tessellation_dbname = os.getenv('ROMANTESSELLATIONDBNAME')

if roman_tessellation_dbname is None:

    print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting)
    exit(64)

roman_tessellation_db = sqlite.RomanTessellationNSIDE512()


# Other required inputs.


bucket_name_input = 'sims-sn-f184'
bucket_name_output = 'sims-sn-f184-lite'

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

if aws_access_key_id is None:

    print("*** Error: Env. var. AWS_ACCESS_KEY_ID not set; quitting)
    exit(64)

if aws_secret_access_key is None:

    print("*** Error: Env. var. AWS_SECRET_ACCESS_KEY not set; quitting)
    exit(64)

print("aws_access_key_id =",aws_access_key_id)
print("aws_secret_access_key =",aws_secret_access_key)


# Set up AWS Batch.

client = boto3.client('batch')


# Define job definition.  Use AWS Batch Console to set this up once.

job_definition = "arn:aws:batch:us-west-2:891377127831:job-definition/Fetch_and_run:2"


# Define job queue.  Use AWS Batch Console to set this up once.

job_queue = 'arn:aws:batch:us-west-2:891377127831:job-queue/getting-started-wizard-job-queue'


# Define job name.

job_name_base = "rapid_science_pipeline"




def build_command_line_args(input_file,output_dir,orderlet):

    '''
    Build command line.
    '''

    code_to_execute_args = ["python"]
    code_to_execute_args.append(script_to_execute)
    code_to_execute_args.append("-f")
    code_to_execute_args.append(input_file)
    code_to_execute_args.append("-o")
    code_to_execute_args.append(orderlet)
    code_to_execute_args.append("--outdir")
    code_to_execute_args.append(output_dir)
    code_to_execute_args.append("--spectrum_plot")
    code_to_execute_args.append("True")
    code_to_execute_args.append("--fsr_plot")
    code_to_execute_args.append("True")
    code_to_execute_args.append("--fit_plot")
    code_to_execute_args.append("True")

    print("code_to_execute_args =",code_to_execute_args)

    return code_to_execute_args






def submit_job():

    cmd = build_command_line_args(l1_file,outdir,orderlet)
    exitcode_from_command = execute_command(cmd)


    s3 = boto3.resource('s3')


    # Parse input files in input S3 bucket.

    my_bucket_input = s3.Bucket(bucket_name_input)

    files_input = {}

    for my_bucket_input_object in my_bucket_input.objects.all():

        #print(my_bucket_input_object.key)

        gzfname_input = my_bucket_input_object.key

        filename_match = re.match(r"(.+)/(.+\.fits\.gz)",gzfname_input)

        try:
            subdir_only = filename_match.group(1)
            only_gzfname_input = filename_match.group(2)
            #print("-----0-----> subdir_only =",subdir_only)
            #print("-----1-----> only_gzfname_input =",only_gzfname_input)

        except:
            #print("-----2-----> No match in",gzfname_input)
            continue

        if subdir_only in files_input.keys():
            files_input[subdir_only].append(only_gzfname_input)
        else:
            files_input[subdir_only] = [only_gzfname_input]


    # Loop over subdirs and launch one AWS Batch job per subdir.

    njobs = 0

    for subdir_only in files_input.keys():

        print("subdir_only =",subdir_only)


        # Submit single job for 18 SCA_NUM input files in input S3 bucket for a given exposure.

        job_name = job_name_base + subdir_only

        response = client.submit_job(
            jobName=job_name,
            jobQueue=job_queue,
            jobDefinition=job_definition,
            containerOverrides={
                'environment': [
                    {
                        'name': 'INPUTSUBDIR',
                        'value': subdir_only
                    },
                    {
                        'name': 'BATCH_FILE_S3_URL',
                        'value': 's3://sims-sn-f184-lite/awsBatchJobLowLevelScript_CompressTroxelFitsFiles.sh'
                    },
                    {
                        'name': 'BATCH_FILE_TYPE',
                        'value': 'script'
                    }
                ]
            }
        )

        print("response =",response)


        # Increment number of jobs.

        njobs += 1

        # Comment out the two following lines for the full run.
        #if njobs > 4:
        #    exit(0)



if __name__ == '__main__':

    #
    # Given the RID of the input science image...
    #

    # Open database connection.

    dbh = db.RAPIDDB()


    # Query database for associated L2FileMeta record.

    sca,fid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4 = dbh.get_l2filemeta_record(rid)


    # Query database for select columns in L2Files record.

    image_info = dbh.get_info_for_l2file(rid)

    filename = image_info[0]
    expid = image_info[1]=
    sca = image_info[2]
    field = image_info[3]
    mjdobs = image_info[4]
    exptime = image_info[5]
    infobits = image_info[6]
    status = image_info[7]


    #
    # Query database for a reference image, or make one if it does not exist.
    #

    # Get field number (rtid) of sky tile containing center of input science image.

    roman_tessellation_db.get_rtid(ra0,dec0)
    rtid = roman_tessellation_db.rtid

    if rtid != field:
         print("*** Error: rtid does not match field; quitting....")
         exit(64)

    roman_tessellation_db.get_center_sky_position(rtid)
    field_ra0 = roman_tessellation_db.ra0
    field_dec0 = roman_tessellation_db.dec0
    roman_tessellation_db.get_corner_sky_positions(rtid)
    field_ra1 = roman_tessellation_db.ra1
    field_dec1 = roman_tessellation_db.dec1
    field_ra2 = roman_tessellation_db.ra2
    field_dec2 = roman_tessellation_db.dec2
    field_ra3 = roman_tessellation_db.ra3
    field_dec3 = roman_tessellation_db.dec3
    field_ra4 = roman_tessellation_db.ra4
    field_dec4 = roman_tessellation_db.dec4


    # Query RefImages database table for the best (latest unless version is locked) version of reference image.
    # A reference image depends only on pipeline number, field, filter, and version.

    rfid,refim_filename = dbh.get_best_reference_image(ppid_refimage,field,fid)

    if rfid is None:

        # Query L2FileMeta database table for RID,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,
        # and distance from tile center (degrees) for all science images that
        # overlap the sky tile associated with the input science image and its filter.
        # Use radius_of_initial_cone_search = 0.18 degrees.
        # Returned list is ordered by distance from tile center.

        radius_of_initial_cone_search = 0.18
        overlapping_images = dbh.get_overlapping_l2files(rid,fid,field_ra0,field_dec0,field_ra1,field_dec1,field_ra2,field_dec2,
                                                         field_ra3,field_dec3,field_ra4,field_dec4,radius_of_initial_cone_search)


        # For each overlapping image, query L2Files database table for filename, sca, mjdobs, exptime, infobits, and status.

        refim_inputs_filename_list = []
        refim_inputs_exid_list = []
        refim_inputs_sca_list = []
        refim_inputs_field_list = []
        refim_inputs_mjdobs_list = []
        refim_inputs_exptime_list = []
        refim_inputs_infobits_list = []
        refim_inputs_status_list = []

        for image in overlapping_images:
            refim_inputs_rid = image[0]

            image_info = dbh.get_info_for_l2file(refim_inputs_rid)

            refim_inputs_filename = image_info[0]
            refim_inputs_expid = image_info[1]=
            refim_inputs_sca = image_info[2]
            refim_inputs_field = image_info[3]
            refim_inputs_mjdobs = image_info[4]
            refim_inputs_exptime = image_info[5]
            refim_inputs_infobits = image_info[6]
            refim_inputs_status = image_info[7]

            refim_inputs_filename_list.append(refim_inputs_filename)
            refim_inputs_expid_list.append(refim_inputs_expid)
            refim_inputs_sca_list.append(refim_inputs_sca)
            refim_inputs_field_list.append(refim_inputs_field)
            refim_inputs_mjdobs_list.append(refim_inputs_mjdobs)
            refim_inputs_exptime_list.append(refim_inputs_exptime)
            refim_inputs_infobits_list.append(refim_inputs_infobits)
            refim_inputs_status_list.append(refim_inputs_status)


    # Insert or update record in Jobs database table and return job ID.

    jid = dbh.start_job(ppid,fid,expid,field,sca,rid)




    # Close database connection.

    dbh.close()


    # Write config file for job.


    submit_job()
