RAPID Computing Architecture
####################################################


System Architecture
**************************

Here is a high-level flowchart of the RAPID system architecture:

.. image:: sysarch.png

Everything is done in the AWS cloud, and is accessible via a laptop with Internet connection.

The database server is a very inexpensive t2.micro AWS machine, which runs 24 hours a day, seven days a week.
A more powerful machine is utilized to launch RAPID pipeline instances, which is only activated as needed in order to save money.

Computing Architecture
**************************

Here is a more detailed flowchart of the RAPID computing architecture:

.. image:: computing_architecture.png

In this view, parallel processing on a massive scale is facilitated by the AWS Batch Service.

Database interactions are done only during intial pipeline launching and final data aggregation stages,
before and after pipeline instances are executed under the AWS Batch Service.  This ensures scalability
of the RAPID-pipeline computing system.
