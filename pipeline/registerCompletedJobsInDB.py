import sys
import os
import time
import signal
import configparser
import boto3
import re
import healpy as hp

import modules.utils.rapid_pipeline_subs as plsubs
import database.modules.utils.rapid_db as db

level6 = 6
nside6 = 2**level6

level9 = 9
nside9 = 2**level9


swname = "registerCompletedJobInDB.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


rfid = None
istop = 0

def signal_handler(signum, frame):
    print('Caught signal', signum)
    global istop
    istop = 1


# Get processing date of interest from command-line argument.

datearg = (sys.argv)[1]

print("datearg =",datearg)


# Read environment variables.

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


# Other required environment variables.

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

if aws_access_key_id is None:

    print("*** Error: Env. var. AWS_ACCESS_KEY_ID not set; quitting...")
    exit(64)

if aws_secret_access_key is None:

    print("*** Error: Env. var. AWS_SECRET_ACCESS_KEY not set; quitting...")
    exit(64)


# Read input parameters from .ini file.

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

verbose = int(config_input['JOB_PARAMS']['verbose'])
debug = int(config_input['JOB_PARAMS']['debug'])
job_info_s3_bucket_base = config_input['JOB_PARAMS']['job_info_s3_bucket_base']
job_logs_s3_bucket_base = config_input['JOB_PARAMS']['job_logs_s3_bucket_base']
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']
awaicgen_output_mosaic_image_file = config_input['AWAICGEN']['awaicgen_output_mosaic_image_file']
zogy_output_diffimage_file = config_input['ZOGY']['zogy_output_diffimage_file']


# Set signal hander.

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGQUIT, signal_handler)


# Open loop.

s3_resource = boto3.resource('s3')
s3_client = boto3.client('s3')

exitcode = 0

i = 0

while True:


    # Examine log files for given processing date.

    logs_bucket = s3_resource.Bucket(job_logs_s3_bucket_base)

    njobs = 0
    log_filenames = []
    jids = []

    for logs_bucket_object in logs_bucket.objects.all():

        if debug > 0:
            print(logs_bucket_object.key)

        input_file = logs_bucket_object.key

        filename_match = re.match(r"(\d\d\d\d\d\d\d\d)/(.+jid(\d+)_log\.txt)",input_file)

        try:
            subdir_only = filename_match.group(1)
            filename_only = filename_match.group(2)
            jid = filename_match.group(3)

            if subdir_only == datearg:

                print("-----0-----> subdir_only =",subdir_only)
                print("-----1-----> filename_only =",filename_only)
                print("-----2-----> jid =",jid)

                log_filenames.append(filename_only)
                jids.append(jid)

                njobs += 1

        except:
            if debug > 0:
                print("-----2-----> No match in",input_file)


    print("End of logs S3 bucket listing...")
    print("njobs = ",njobs)


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Loop over jobs for a given processing date.

    for jid,log_fname in zip(jids,log_filenames):


        # Check whether done file exists in S3 bucket for job, and skip if it exists.
        # This is done by attempting to download the done file.  Regardless the sub
        # always returns the filename and subdirs by parsing the s3_full_name.

        s3_full_name_done_file = "s3://" + product_s3_bucket_base + "/" + datearg + '/jid' + str(jid) + "/diffimage_jid" +  str(jid)  + ".done"
        done_filename,subdirs_done,downloaded_from_bucket = plsubs.download_file_from_s3_bucket(s3_client,s3_full_name_done_file)

        if downloaded_from_bucket:
            print("*** Warning: Done file exists ({}); skipping...".format(done_filename))
            continue


        # Download log file from S3 bucket.

        s3_bucket_object_name = datearg + '/' + log_fname

        print("Downloading s3://{}/{} into {}...".format(job_logs_s3_bucket_base,s3_bucket_object_name,log_fname))

        response = s3_client.download_file(job_logs_s3_bucket_base,s3_bucket_object_name,log_fname)

        print("response =",response)


        # Grep log file for aws_batch_job_id and terminating_exitcode.

        job_exitcode = 64
        aws_batch_job_id = 'not_found'

        file = open(log_fname, "r")
        search_string1 = "aws_batch_job_id"
        search_string2 = "terminating_exitcode"

        for line in file:
            if re.search(search_string1, line):
                line = line.rstrip("\n")
                print(line)
                tokens = re.split(r'\s*=\s*',line)
                aws_batch_job_id = tokens[1]
            elif re.search(search_string2, line):
                line = line.rstrip("\n")
                print(line)
                tokens = re.split(r'\s*=\s*',line)
                job_exitcode = tokens[1]


        # Update Jobs record.

        dbh.end_job(jid,job_exitcode,aws_batch_job_id)

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        # Download job config file, in order to harvest some of its metadata.

        job_config_ini_filename = job_config_filename_base + str(jid) + ".ini"

        s3_bucket_object_name = datearg + '/' + job_config_ini_filename

        print("Downloading s3://{}/{} into {}...".format(job_info_s3_bucket_base,s3_bucket_object_name,job_config_ini_filename))

        response = s3_client.download_file(job_info_s3_bucket_base,s3_bucket_object_name,job_config_ini_filename)

        print("response =",response)


        # Harvest job metadata from job config file

        job_config_input = configparser.ConfigParser()
        job_config_input.read(job_config_ini_filename)

        fid = int(job_config_input['SCI_IMAGE']['fid'])

        rtid = int(job_config_input['SKY_TILE']['rtid'])
        field = rtid
        ra0 = float(job_config_input['SKY_TILE']['ra0'])
        dec0 = float(job_config_input['SKY_TILE']['dec0'])

        print("rtid,ra0,dec0 =",rtid,ra0,dec0 )


        # Compute level-6 healpix index (NESTED pixel ordering).

        hp6 = hp.ang2pix(nside6,ra0,dec0,nest=True,lonlat=True)


        # Compute level-9 healpix index (NESTED pixel ordering).

        hp9 = hp.ang2pix(nside9,ra0,dec0,nest=True,lonlat=True)

        print("hp6,hp9 =",hp6,hp9)


        # Inventory products associated with job.

        product_bucket = s3_resource.Bucket(product_s3_bucket_base)

        job_prefix = datearg + '/jid' + str(jid)

        print("job_prefix =",job_prefix)

        for product_bucket_object in product_bucket.objects.filter(Prefix=job_prefix):

            print("------------->",product_bucket_object)


            # Download product config file, in order to read the MD5 checksum of the reference image.

            product_config_ini_filename = product_config_filename_base + str(jid) + ".ini"

            s3_bucket_object_name = datearg + '/' + product_config_ini_filename

            print("Downloading s3://{}/{} into {}...".format(product_s3_bucket_base,s3_bucket_object_name,product_config_ini_filename))

            response = s3_client.download_file(product_s3_bucket_base,s3_bucket_object_name,product_config_ini_filename)

            print("response =",response)


            # Read input parameters from product config .ini file.

            product_config_input_filename = product_config_ini_filename
            product_config_input = configparser.ConfigParser()
            product_config_input.read(product_config_input_filename)


            # Reference image.

            if awaicgen_output_mosaic_image_file in product_bucket_object.key:

                print("Found in reference image in S3 product bucket: {}".format(awaicgen_output_mosaic_image_file))


                # Harvest select product metadata from product config file

                try:
                    rfid_str = product_config_input['REF_IMAGE']['rfid']
                except:
                    rfid_str == 'Not found'

                if rfid_str == 'None':
                    rfid = None

                    ppid_refimage = int(product_config_input['REF_IMAGE']['ppid'])

                    checksum_refimage = product_config_input['REF_IMAGE']['awaicgen_output_mosaic_image_file_checksum']
                    filename_refimage = product_config_input['REF_IMAGE']['awaicgen_output_mosaic_image_file']
                    infobits_refimage = int(product_config_input['REF_IMAGE']['awaicgen_output_mosaic_image_infobits'])
                    status_refimage = int(product_config_input['REF_IMAGE']['awaicgen_output_mosaic_image_status'])


                    # Insert record in RefImages database table.

                    dbh.add_refimage(ppid_refimage,field,fid,hp6,hp9,infobits_refimage,status_refimage,filename_refimage,checksum_refimage)

                    if dbh.exit_code >= 64:
                        exit(dbh.exit_code)

                    rfid = dbh.rfid
                    version_refimage = dbh.version

                    print("rfid =",rfid)
                    print("version_refimage =",version_refimage)


                    # Finalize record in RefImages database table, (in order to set vbest = 1 for current record).
                    # Filename, checksum, infobits, and status are unchanged.

                    dbh.update_refimage(rfid,filename_refimage,checksum_refimage,status_refimage,version_refimage)

                    if dbh.exit_code >= 64:
                        exit(dbh.exit_code)


                    # Parse metadata for reference-image catalog.

                    sextractor_refimage_catalog_filename_for_db = product_config_input['REF_IMAGE']['sextractor_refimage_catalog_filename_for_db']
                    sextractor_refimage_catalog_checksum = product_config_input['REF_IMAGE']['sextractor_refimage_catalog_checksum']
                    sextractor_refimage_catalog_cattype = product_config_input['REF_IMAGE']['sextractor_refimage_catalog_cattype']
                    sextractor_refimage_catalog_status = product_config_input['REF_IMAGE']['sextractor_refimage_catalog_status']


                    # Insert record in RefImCatalogs database table.

                    dbh.register_refimcatalog(rfid,
                                              ppid_refimage,
                                              sextractor_refimage_catalog_cattype,
                                              field,
                                              hp6,
                                              hp9,
                                              fid,
                                              sextractor_refimage_catalog_status,
                                              sextractor_refimage_catalog_filename_for_db,
                                              sextractor_refimage_catalog_checksum)

                    if dbh.exit_code >= 64:
                        exit(dbh.exit_code)

                    rfcatid = dbh.rfcatid
                    svid = dbh.svid

                    print("rfcatid =",rfcatid)
                    print("svid =",svid)


            # Difference image.

            if zogy_output_diffimage_file in product_bucket_object.key:

                print("Found in difference image in S3 product bucket: {}".format(zogy_output_diffimage_file))


                # Harvest select product metadata from product config file

                try:
                    zogy_output_diffimage_file_checksum = product_config_input['ZOGY']['zogy_output_diffimage_file_checksum']
                except:
                    zogy_output_diffimage_file_checksum == 'Not found'

                if zogy_output_diffimage_file_checksum != 'Not found':

                    zogy_output_diffimage_file = product_config_input['ZOGY']['zogy_output_diffimage_file']

                    rid_diffimage = product_config_input['ZOGY']['rid']
                    ppid_diffimage = product_config_input['ZOGY']['ppid']

                    if rfid is None:
                        rfid_diffimage = product_config_input['ZOGY']['rfid']
                    else:
                        rfid_diffimage = rfid

                    ra0_diffimage = product_config_input['ZOGY']['ra0']
                    dec0_diffimage = product_config_input['ZOGY']['dec0']
                    ra1_diffimage = product_config_input['ZOGY']['ra1']
                    dec1_diffimage = product_config_input['ZOGY']['dec1']
                    ra2_diffimage = product_config_input['ZOGY']['ra2']
                    dec2_diffimage = product_config_input['ZOGY']['dec2']
                    ra3_diffimage = product_config_input['ZOGY']['ra3']
                    dec3_diffimage = product_config_input['ZOGY']['dec3']
                    ra4_diffimage = product_config_input['ZOGY']['ra4']
                    dec4_diffimage = product_config_input['ZOGY']['dec4']

                    fid_diffimage = product_config_input['ZOGY']['fid']
                    sca_diffimage = product_config_input['ZOGY']['sca']
                    nsexcatsources_diffimage = product_config_input['ZOGY']['nsexcatsources']
                    scalefacref_diffimage = product_config_input['ZOGY']['scalefacref']

                    checksum_diffimage = product_config_input['ZOGY']['zogy_output_diffimage_file_checksum']
                    filename_diffimage = product_config_input['ZOGY']['zogy_output_diffimage_file']
                    status_diffimage = product_config_input['ZOGY']['zogy_output_diffimage_file_status']
                    infobits_diffimage = product_config_input['ZOGY']['zogy_output_diffimage_file_infobits']

                    infobits_refimage = product_config_input['ZOGY']['awaicgen_output_mosaic_image_infobits']


                    # Insert record in DiffImages database table.

                    dbh.add_diffimage(rid_diffimage,
                                      ppid_diffimage,
                                      rfid_diffimage,
                                      infobits_diffimage,
                                      infobits_refimage,
                                      ra0_diffimage,
                                      dec0_diffimage,
                                      ra1_diffimage,
                                      dec1_diffimage,
                                      ra2_diffimage,
                                      dec2_diffimage,
                                      ra3_diffimage,
                                      dec3_diffimage,
                                      ra4_diffimage,
                                      dec4_diffimage,
                                      status_diffimage,
                                      filename_diffimage,
                                      checksum_diffimage)

                    if dbh.exit_code >= 64:
                        exit(dbh.exit_code)

                    pid = dbh.pid
                    version_diffimage = dbh.version

                    print("pid =",pid)
                    print("version_diffimage =",version_diffimage)


                    # Finalize record in DiffImages database table, (in order to set vbest = 1 for current record).
                    # Filename, checksum, infobits, and status are unchanged.

                    dbh.update_diffimage(pid,filename_diffimage,checksum_diffimage,status_diffimage,version_diffimage)

                    if dbh.exit_code >= 64:
                        exit(dbh.exit_code)


                    # Insert record in DiffImMeta database table.

                    dbh.register_diffimmeta(pid,fid_diffimage,sca_diffimage,field,hp6,hp9,nsexcatsources_diffimage,scalefacref_diffimage)

                    if dbh.exit_code >= 64:
                        exit(dbh.exit_code)


        # Touch done file.

        touch_cmd = ['touch', done_filename]
        exitcode_from_touch = plsubs.execute_command(touch_cmd)


        # Upload done file to S3 bucket.

        product_s3_bucket = product_s3_bucket_base
        s3_object_name_done_filename = datearg + "/jid" + str(jid) + "/" + done_filename
        filenames = [done_filename]
        objectnames = [s3_object_name_done_filename]
        plsubs.upload_files_to_s3_bucket(s3_client,product_s3_bucket,filenames,objectnames)


        # Close database connection.

        dbh.close()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


    # Test code.

    a = 1
    for n in range(10):
        a = a + 1
        print("n,a =",n,a)

    print("Sleeping 30 seconds...")
    time.sleep(30)
    print("Waking up...")

    if i == 3:
        #os.kill(os.getpid(), signal.SIGQUIT)          # Quits gracefully via signal handler upon receiving control-/
        #os.kill(os.getpid(), signal.SIGINT)           # Quits gracefully via signal handler upon receiving control-c
        os.kill(os.getpid(), signal.SIGSTOP)           # Quits unconditionally and immediately.

    if istop == 1:
        print("Terminating gracefully now...")
        exitcode = 7
        exit(exitcode)

    i += 1
    print("i = ",i)


# Termination.

exit(exitcode)
