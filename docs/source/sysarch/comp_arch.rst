RAPID Computing Architecture
####################################################


System Architecture
**************************

Here is a high-level flowchart of the RAPID system architecture:

.. image:: sysarch.png

Everything is done in the AWS cloud, and is accessible via a laptop with Internet connection.

The database server is a very inexpensive ``t2.micro`` AWS machine,
which runs 24 hours a day, seven days a week.
A more powerful multi-core, high-memory machine can be utilized to execute RAPID pipeline instances,
which would only be activated as needed in order to save money.  This is just one way the
RAPID pipelines could be run in parallel; the next section explains another way, which has
been demonstrated to be a very feasible methodology indeed.

Our strategy for software interactions with the SQL database is that queries are executed only during
initial pipeline launching and final data aggregation stages, before and after pipeline instances are
executed separately on multiple CPU cores.  This ensures scalability
of the RAPID-pipeline computing system.


Computing Architecture
**************************

Here is a another flowchart of the RAPID computing architecture, which details how
parallel processing of the data is done on multiple machines, which is has proven
to be a very viable and practical approach from our extensive testing thus far:

.. image:: computing_architecture.png

Parallel processing on a massive scale is facilitated by the AWS Batch Service.

Database interactions with a PostgreSQL database are done only during initial pipeline launching
and final data aggregation stages, before and after pipeline instances are executed under the
AWS Batch Service.  This ensures scalability of the RAPID-pipeline computing system.


Pipeline Performance
**************************

In one of our initial large-scale tests,
RAPID pipeline instances were launched for all OpenUniverse simulated images with ``DATE-OBS >= 2028-09-07 00:00:00``
and ``DATE-OBS <= 2028-09-08 08:30:00``.  This is about 2000 jobs, one job per science image.  All jobs were successfully run,
except for 80 jobs in which a reference image could not be made due to lack of prior observations for the associated field.
The elapsed execution time for a RAPID pipeline job was measured
from the time it was launched to the time it finished running on an AWS Batch machine, of course, after writing
the pipeline products to the output S3 bucket.

Here is a histogram of the job execution times:

.. image:: rapid_job_elapsed_vs_time_1dhist.png

Here is a 2-D histogram of the job execution times versus number of input frames for the reference image that was generated:

.. image:: rapid_job_elapsed_vs_nframes_2dhist.png

It is obvious from the figure that the execution times have a contribution that is proporational
to the number of reference-image inputs.

The products from this test run are in the following S3 bucket::

    aws s3 ls --recursive s3://rapid-product-files/20250218

For example, here is a SourceExtractor catalog made from the difference image for one job (jid=999)::

    20250218/jid999/diffimage_masked.txt

A separate page describes all available :doc:`RAPID-pipeline products </prod/products>.`
