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
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


print("swname =", swname)
print("swvers =", swvers)


# RID of input file.

rid = os.getenv('RID')

if rid is None:

    print("*** Error: Env. var. RID not set; quitting...")
    exit(64)


# Ensure sqlite database that defines the Roman sky tessellation is available.

roman_tessellation_dbname = os.getenv('ROMANTESSELLATIONDBNAME')

if roman_tessellation_dbname is None:

    print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...")
    exit(64)

roman_tessellation_db = sqlite.RomanTessellationNSIDE512()


# Other required inputs.


bucket_name_input = 'sims-sn-f184'
bucket_name_output = 'sims-sn-f184-lite'

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

if aws_access_key_id is None:

    print("*** Error: Env. var. AWS_ACCESS_KEY_ID not set; quitting...")
    exit(64)

if aws_secret_access_key is None:

    print("*** Error: Env. var. AWS_SECRET_ACCESS_KEY not set; quitting...")
    exit(64)

print("aws_access_key_id =",aws_access_key_id)
print("aws_secret_access_key =",aws_secret_access_key)

rapid_sw = os.getenv('RAPID_SW')

if rapid_sw is None:

    print("*** Error: Env. var. RAPID_SW not set; quitting...")
    exit(64)

cfg_path = rapid_sw + "/cdf"

print("rapid_sw =",rapid_sw)
print("cfg_path =",cfg_path)


# Read input parameters from .ini file.

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

verbose = int(config_input['DEFAULT']['verbose'])
debug = int(config_input['DEFAULT']['debug'])
ppid = int(config_input['SCI_IMAGE']['ppid'])
ppid_refimage = int(config_input['REF_IMAGE']['ppid_refimage'])
naxis1_refimage = int(config_input['REF_IMAGE']['naxis1_refimage'])
naxis2_refimage = int(config_input['REF_IMAGE']['naxis2_refimage'])
cdelt1_refimage = float(config_input['REF_IMAGE']['cdelt1_refimage'])
cdelt2_refimage = float(config_input['REF_IMAGE']['cdelt2_refimage'])
crota2_refimage = float(config_input['REF_IMAGE']['crota2_refimage'])


# Set up AWS Batch.

client = boto3.client('batch')


# Define job definition.  Use AWS Batch Console to set this up once.

job_definition = config_input['AWS_BATCH']['job_definition']


# Define job queue.  Use AWS Batch Console to set this up once.

job_queue = config_input['AWS_BATCH']['job_queue']


# Define job name.

job_name_base = config_input['AWS_BATCH']['job_name_base']






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

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    # Query database for associated L2FileMeta record.

    sca,fid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4 = dbh.get_l2filemeta_record(rid)

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for select columns in L2Files record.

    image_info = dbh.get_info_for_l2file(rid)

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    filename = image_info[0]
    expid = image_info[1]
    sca = image_info[2]
    field = image_info[3]
    mjdobs = image_info[4]
    exptime = image_info[5]
    infobits = image_info[6]
    status = image_info[7]
    vbest = image_info[8]
    version = image_info[9]

    if vbest == 0:
        print('*** Error: vbest is zero for rid = {}; quitting....'.format(rid))
        exit(64)


    # Get field number (rtid) of sky tile containing center of input science image.

    roman_tessellation_db.get_rtid(ra0,dec0)
    rtid = roman_tessellation_db.rtid

    if rtid != field:
        print("*** Error: rtid (= {}) does not match field (= {}); quitting....".format(rtid,field))
        exit(64)


    # Get sky positions of center and four corners of sky tile.

    roman_tessellation_db.get_center_sky_position(rtid)
    ra0_field = roman_tessellation_db.ra0
    dec0_field = roman_tessellation_db.dec0
    roman_tessellation_db.get_corner_sky_positions(rtid)
    ra1_field = roman_tessellation_db.ra1
    dec1_field = roman_tessellation_db.dec1
    ra2_field = roman_tessellation_db.ra2
    dec2_field = roman_tessellation_db.dec2
    ra3_field = roman_tessellation_db.ra3
    dec3_field = roman_tessellation_db.dec3
    ra4_field = roman_tessellation_db.ra4
    dec4_field = roman_tessellation_db.dec4


    # Compute the sky positions of the four corners of the reference.
    # Remember the reference image is centered on the sky tile with zero rotation.

    ra0_refimage = ra0_field
    dec0_refimage = dec0_field

    crpix1_refimage = 0.5 * float(naxis1_refimage) + 0.5
    crpix2_refimage = 0.5 * float(naxis2_refimage) + 0.5
    crval1_refimage = 11.067073
    crval2_refimage = -43.80449


    # Integer pixel coordinates are zero-based and centered on pixel.

    x1_refimage = 0.5 - 1.0     # We want the extreme outer image edges.
    y1_refimage = 0.5 - 1.0

    x2_refimage = naxis1_refimage + 0.5 - 1.0
    y2_refimage = 0.5 - 1.0

    x3_refimage = naxis1_refimage + 0.5 - 1.0
    y3_refimage = naxis2_refimage + 0.5 - 1.0

    x4_refimage = 0.5 - 1.0
    y4_refimage = naxis2_refimage + 0.5 - 1.0


    ra1_refimage,dec1_refimage = util.tan_proj(x1_refimage,y1_refimage,crpix1_refimage,crpix2_refimage,crval1_refimage,crval2_refimage,cdelt1_refimage,cdelt2_refimage,crota2_refimage)
    ra2_refimage,dec2_refimage = util.tan_proj(x2_refimage,y2_refimage,crpix1_refimage,crpix2_refimage,crval1_refimage,crval2_refimage,cdelt1_refimage,cdelt2_refimage,crota2_refimage)
    ra3_refimage,dec3_refimage = util.tan_proj(x3_refimage,y3_refimage,crpix1_refimage,crpix2_refimage,crval1_refimage,crval2_refimage,cdelt1_refimage,cdelt2_refimage,crota2_refimage)
    ra4_refimage,dec4_refimage = util.tan_proj(x4_refimage,y4_refimage,crpix1_refimage,crpix2_refimage,crval1_refimage,crval2_refimage,cdelt1_refimage,cdelt2_refimage,crota2_refimage)


    # Query RefImages database table for the best (latest unless version is locked) version of reference image.
    # A reference image depends only on pipeline number, field, filter, and version.
    # If a reference image does not exist, then aggregate all the inputs required to make one.

    rfid,filename_refimage = dbh.get_best_reference_image(ppid_refimage,field,fid)

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    if rfid is not None:
        input_images_csv_file = None
    else:
        input_images_csv_file = "input_images_for_refimage_"+ str(jid) + ".csv"

        # Query L2FileMeta database table for RID,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,
        # and distance from tile center (degrees) for all science images that
        # overlap the sky tile associated with the input science image and its filter.
        # Use radius_of_initial_cone_search = 0.18 degrees.
        # Returned list is ordered by distance from tile center.
        #
        # NOTE: The returned list includes all versions, and regardless of status (the query of the
        # L2FileMeta table does NOT join with the L2Files table, in order to optimize query speed).

        radius_of_initial_cone_search = 0.18
        overlapping_images = dbh.get_overlapping_l2files(rid,fid,ra0_field,dec0_field,ra1_field,dec1_field,ra2_field,dec2_field,
                                                         ra3_field,dec3_field,ra4_field,dec4_field,radius_of_initial_cone_search)

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        # For each overlapping image, query L2Files database table for filename, sca, mjdobs, exptime, infobits, and status.

        filename_refimage_inputs = []
        expid_refimage_inputs = []
        sca_refimage_inputs = []
        field_refimage_inputs = []
        mjdobs_refimage_inputs = []
        exptime_refimage_inputs = []
        infobits_refimage_inputs = []

        for image in overlapping_images:
            rid_refimage = image[0]

            image_info = dbh.get_info_for_l2file(rid_refimage)

            if dbh.exit_code >= 64:
                exit(dbh.exit_code)

            filename_refimage = image_info[0]
            expid_refimage = image_info[1]
            sca_refimage = image_info[2]
            field_refimage = image_info[3]
            mjdobs_refimage = image_info[4]
            exptime_refimage = image_info[5]
            infobits_refimage = image_info[6]
            status_refimage = image_info[7]
            vbest_refimage = image_info[8]
            version_refimage = image_info[9]

            if status_refimage == 0: continue             # Omit if status = 0
            if vbest_refimage == 0: continue              # Omit if not the best version

            filename.append(refim_inputs_filename_refimage)
            expid.append(refim_inputs_expid_refimage)
            sca.append(refim_inputs_sca_refimage)
            field.append(refim_inputs_field_refimage)
            mjdobs.append(refim_inputs_mjdobs_refimage)
            exptime.append(refim_inputs_exptime_refimage)
            infobits.append(refim_inputs_infobits_refimage)


    # Insert or update record in Jobs database table and return job ID.

    jid = dbh.start_job(ppid,fid,expid,field,sca,rid)

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Populate config-file dictionary for job.

    config_output = configparser.ConfigParser()

    config_output['DEFAULT'] = {'debug': str(debug),

                         'swname': swname,

                         'swvers': swvers,
                         'jid': str(jid)}
    config_output['DEFAULT']['verbose'] = str(verbose)

    config_output['SCI_IMAGE'] = {}

    config_output['SCI_IMAGE']['ppid'] = str(ppid)
    config_output['SCI_IMAGE']['rid'] = str(rid)
    config_output['SCI_IMAGE']['sca'] = str(sca)
    config_output['SCI_IMAGE']['fid'] = str(fid)

    config_output['SCI_IMAGE']['filename'] = str(filename)
    config_output['SCI_IMAGE']['expid'] = str(expid)
    config_output['SCI_IMAGE']['sca'] = str(sca)
    config_output['SCI_IMAGE']['field'] = str(field)
    config_output['SCI_IMAGE']['mjdobs'] = str(mjdobs)
    config_output['SCI_IMAGE']['exptime'] = str(exptime)
    config_output['SCI_IMAGE']['infobits'] = str(infobits)
    config_output['SCI_IMAGE']['status'] = str(status)

    config_output['SCI_IMAGE']['ra0'] = str(ra0)
    config_output['SCI_IMAGE']['dec0'] = str(dec0)
    config_output['SCI_IMAGE']['ra1'] = str(ra1)
    config_output['SCI_IMAGE']['dec1'] = str(dec1)
    config_output['SCI_IMAGE']['ra2'] = str(ra2)
    config_output['SCI_IMAGE']['dec2'] = str(dec2)
    config_output['SCI_IMAGE']['ra3'] = str(ra3)
    config_output['SCI_IMAGE']['dec3'] = str(dec3)
    config_output['SCI_IMAGE']['ra4'] = str(ra4)
    config_output['SCI_IMAGE']['dec4'] = str(dec4)

    config_output['SKY_TILE'] = {}

    config_output['SKY_TILE']['rtid'] = str(rtid)

    config_output['SKY_TILE']['ra0'] = str(ra0_field)
    config_output['SKY_TILE']['dec0'] = str(dec0_field)
    config_output['SKY_TILE']['ra1'] = str(ra1_field)
    config_output['SKY_TILE']['dec1'] = str(dec1_field)
    config_output['SKY_TILE']['ra2'] = str(ra2_field)
    config_output['SKY_TILE']['dec2'] = str(dec2_field)
    config_output['SKY_TILE']['ra3'] = str(ra3_field)
    config_output['SKY_TILE']['dec3'] = str(dec3_field)
    config_output['SKY_TILE']['ra4'] = str(ra4_field)
    config_output['SKY_TILE']['dec4'] = str(dec4_field)


    config_output['REF_IMAGE'] = {}

    config_output['REF_IMAGE']['ppid'] = str(ppid_refimage)
    config_output['REF_IMAGE']['naxis1'] = str(naxis1_refimage)
    config_output['REF_IMAGE']['naxis2'] = str(naxis2_refimage)
    config_output['REF_IMAGE']['rfid'] = str(rfid)
    config_output['REF_IMAGE']['filename'] = filename_refimage
    config_output['REF_IMAGE']['input_images_csv_file'] = input_images_csv_file

    config_output['REF_IMAGE']['ra0'] = str(ra0_refimage)
    config_output['REF_IMAGE']['dec0'] = str(dec0_refimage)
    config_output['REF_IMAGE']['ra1'] = str(ra1_refimage)
    config_output['REF_IMAGE']['dec1'] = str(dec1_refimage)
    config_output['REF_IMAGE']['ra2'] = str(ra2_refimage)
    config_output['REF_IMAGE']['dec2'] = str(dec2_refimage)
    config_output['REF_IMAGE']['ra3'] = str(ra3_refimage)
    config_output['REF_IMAGE']['dec3'] = str(dec3_refimage)
    config_output['REF_IMAGE']['ra4'] = str(ra4_refimage)
    config_output['REF_IMAGE']['dec4'] = str(dec4_refimage)


    # Write config file for job.

    config_output_filename = "job_config_jid" + str(jid) + ".ini"
    with open(config_output_filename, 'w') as config_outputfile:

        config_output.write(config_outputfile)

    #submit_job()
