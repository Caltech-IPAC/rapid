RAPID-Pipeline Bulk Run
####################################################

Overview
************************************

There are many steps to running the RAPID pipeline.
It is crucial to follow the listed order below, and do not omit
any steps, as a given step relies on all previous steps.

1. Run science pipelines.

2. Register pipeline metadata in operations database.

3. Run post-processing pipelines.
   Relies on updated operations database from step 2.

4. Register additional pipeline metadata in operations database.


All steps are executed within the environment of a Docker container
that is running on an EC2 instance with access to the operations database.
Any scripts to automate the execution of pipelines must
necessarily involve the ``docker run`` command.

Launch scripts are used to run pipelines.
These query the operations database and launch pipeline-instance jobs under AWS Batch.
Individual AWS Batch jobs do not themselves interact with the operations database.
