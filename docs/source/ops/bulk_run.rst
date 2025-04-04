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

4. Register pipeline metadata in operations database.
