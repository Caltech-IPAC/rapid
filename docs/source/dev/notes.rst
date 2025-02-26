RAPID Pipeline Development Notes
####################################################

Increasing AWS Cloud Limits
************************************

Submit a ticket to the IPAC Support Group (ISG) requesting an AWS increase
in the relevant limit for the RAPID project
(this involves Wendy submitting a ticket to AWS).

`ISG Request URL <https://jira.ipac.caltech.edu/servicedesk/customer/portal/4/>`_

Login with your IPAC credentials (not sure whether VPN must be running).


Developer Guidelines
************************************

#. Set up your text editor to clip trailing spaces when saving source-code file (e.g., BBEdit has a preference that does this).

#. Think strategically when pushing a source-code file to the git repo whether a simple git diff between revisions
   will allow a clear and unambiguous indication of the code changes.  For example, numerous stylistic changes can
   hide substantive changes that affect code behavior and should be deferred to a separate revision.

#. Before checking into the git repo modifications to someone else's source code,
   let that person know what to expect (and assure there is a sufficient level of trust beforehand).

#. Always test code changes before the code is put into operations; the development is not done until the code changes have been tested.

#. Include a sufficiency of comments in your source code!

#. Remember to ``git pull`` before any ``git push`` and often, in order to make sure your RAPID git repo is up to date.

Running RAPID Pipeline under AWS Batch
********************************************

1. Ensure the following environment variables are set on your laptop:

.. code-block::

   AWS_DEFAULT_REGION
   AWS_SECRET_ACCESS_KEY
   AWS_EC2_INSTANCE_ID
   AWS_ACCESS_KEY_ID
   AWS_EC2_VOLUME_ID
   AWS_EC2_VOLUME_DEVICE

The two latter ones are only needed if your EC2 instance is to have an EBS volume attached.

Your EC2 instance should have a large enough book-disk volume as docker build requires a lot of space; at least 32 GB is recommended.

2. Check your source-code changes into the RAPID git repo.

3. Ensure python3 is installed on your laptop and start your EC2 instance:

.. code-block::

   python /source-code/location/rapid/aws/start_ec2_instance.py

Here is how to stop your EC2 instance later:

.. code-block::

   python /source-code/location/rapid/aws/stop_ec2_instance.py

4. Log into your EC2 instance:

.. code-block::

   ssh -i ~/.ssh/my_ec2.pem ubuntu@ec2-54-212-213-65.us-west-2.compute.amazonaws.com

5. Under root on your EC2 instance, check out the lastest source code from the RAPID git repo, and then rebuild the Docker image for the RAPID pipeline:

.. code-block::

   sudo su
   cd /home/ubuntu/rapid
   git pull

The following command removes ALL Docker images from your EC2 instance,
but has the advantage of removing all Docker debris from the boot-disk volume,
thus reclaiming disk space:

.. code-block::

   docker system prune -a -f

.. warning::

   The above ``docker system prune`` command and the ``docker build`` command below will not work properly or as intended,
   meaning the expected disk space will not be reclaimed,
   unless all containers running the Docker image ``rapid_science_pipeline:1.0`` are stopped!

Here is how to get a listing of your docker images:

.. code-block::

   docker image ls

Rebuild the Docker image from scratch:

.. code-block::

   cd /home/ubuntu/rapid
   docker build --file /home/ubuntu/rapid/docker/Dockerfile_ubuntu_runSingleSciencePipeline --tag rapid_science_pipeline:1.0 .


6. Push Docker image to the Amazon public elastic container registry (ECR):

Note that the RAPID pipeline has already been registered at

.. code-block::

   public.ecr.aws/y9b1s7h8/rapid_science_pipeline

and so this step involves simply updating the Docker image in the registry.

Authenticate your Docker client to the registry as follows:

.. code-block::

   aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws/y9b1s7h8

Now get the Docker image ID as follows:

.. code-block::

   docker image ls

The response will be something like:

.. code-block::

   REPOSITORY               TAG       IMAGE ID       CREATED         SIZE
   rapid_science_pipeline   1.0       a76b1373bfe2   6 minutes ago   2.36GB

Tag the Docker image with "latest" and push to ECR with these two commands:

.. code-block::

   docker tag a76b1373bfe2 public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest
   docker push public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest


The following shows commands to launch an instance of the RAPID pipeline as AWS Batch job.
The to-be-run Docker container rapid_science_pipeline:1.0 has /code built in, so there is no need to mount an external volume for /code.
Since this Docker image contains the ENTRYPOINT instruction, you must override it  with the ``--entrypoint bash`` option
(and do not put ``bash`` at the end of the command).

.. code-block::

   mkdir -p /home/ubuntu/work/test_20240923
   cd /home/ubuntu/work/test_20240923
   aws s3 cp s3://rapid-pipeline-files/roman_tessellation_nside512.db /home/ubuntu/work/test_20240923/roman_tessellation_nside512.db

   sudo su

   docker stop russ-test-jobsubmit
   docker rm russ-test-jobsubmit

   docker run -it --entrypoint bash --name russ-test-jobsubmit -v /home/ubuntu/work/test_20240923:/work rapid_science_pipeline:1.0

   export DBPORT=5432
   export DBNAME=rapidopsdb
   export DBUSER=rapidporuss
   export DBSERVER=35.165.53.98
   export DBPASS="????"
   export AWS_DEFAULT_REGION=us-west-2
   export AWS_SECRET_ACCESS_KEY=????
   export AWS_ACCESS_KEY_ID=????
   export PYTHONUNBUFFERED=1
   export LD_LIBRARY_PATH=/code/c/lib
   export PATH=/code/c/bin:$PATH
   export export RAPID_SW=/code
   export export RAPID_WORK=/work
   export PYTHONPATH=/code
   export PYTHONUNBUFFERED=1

   git config --global --add safe.directory /code

   cd /tmp
   export ROMANTESSELLATIONDBNAME=/work/roman_tessellation_nside512.db
   export RID=172211
   python3.11 /code/pipeline/awsBatchSubmitJobs_launchSingleSciencePipeline.py

   exit

Python 3.11 is required and it is installed inside the Docker image (/usr/bin/python3.11).

After the AWS Batch job finishes, there are files written to S3 buckets that can be examined:

.. code-block::

   aws s3 ls --recursive s3://rapid-pipeline-files

   2024-10-28 11:18:38     120092 20241028/input_images_for_refimage_jid1.csv
   2024-10-28 11:18:38       6025 20241028/job_config_jid1.ini
   2024-09-03 16:42:56 1535762432 roman_tessellation_nside512.db

.. code-block::

   aws s3 ls --recursive s3://rapid-pipeline-logs

   2024-10-28 11:22:34      17129 20241028/rapid_pipeline_job_20241028_jid1_log.txt

.. code-block::

   aws s3 ls --recursive s3://rapid-product-files

   2024-10-28 11:22:29  196004160 20241028/jid1/awaicgen_output_mosaic_cov_map.fits
   2024-10-28 11:22:26  196004160 20241028/jid1/awaicgen_output_mosaic_image.fits

The general scheme for how the output files are organized in the S3 buckets is according to
processing date (Pacific Time) and the associated job ID (the same job ID can exist under
different processing dates if reprocessing occurred).  The reference-image products from ``awaicgen``
are given generic filenames in these buckets, and, later, will be renamed to filenames like:

.. code-block::

   rapid_field1234567_fid7_ppid15_v2_rfid12394758_refimage.fits
   rapid_field1234567_fid7_ppid15_v2_rfid12394758_covmap.fits

The above filenames are created after these products are registered in the RAPID pipeline operations database.
The products are then copied to
a more permanent location (and ultimately archived in MAST).  The ``ppid`` gives the pipeline number
that generated the reference image, which could be either the difference-image pipeline (``ppid=15``)
or a dedicated reference-image pipeline (``ppid=12``).

Download and examine log file:

.. code-block::

   aws s3 ls --recursive s3://rapid-pipeline-logs/20241028
   aws s3 cp s3://rapid-pipeline-logs/20241028/rapid_pipeline_job_20241028_jid1_log.txt rapid_pipeline_job_20241028_jid1_log.txt
   cat rapid_pipeline_job_20241028_jid1_log.txt

Last modified: Tue 2025 Jan 6 5:24 a.m.

