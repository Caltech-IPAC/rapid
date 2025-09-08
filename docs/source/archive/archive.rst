RAPID Archive Deliveries
####################################################

..
  Short description of the PIT data delivery timeline, data releases, and vision of success
  Short descriptive overview of expected data products and their utility
  Link(s) to relevant schedules, project plans, and documentation
  Links to Jupyter notebooks, scripts, or code snippets illustrating example workflows or data analysis


Introduction
************************************

RAPID delivers prompt time-domain products & services for Roman:

* Image-differencing of every new Roman image against a reference image
* Public alert stream of all transient and variable candidates in the difference images
* Source match-files of photometry for every candidate observed more than once
* Forced photometry at any observed sky location

Our goal is to alert within 1hr of receipt of L2 data from the SOC. Processesing is thus continous, and many products will be served directly by RAPID. These products will be periodically rolled-up into discrete deliveries to the MAST archive.


Notes
************************************

* RAPID is currently developing in us-west-2 but will transfer to us-east-1 for operations
* There is no ADSF C-library for existing software 

Unknowns:
* Detailed mission plan
* HLWAS reference strategy
* Galactic plane strategy
* Candidate density and thresholds
* Alert schema
* Reprocessing frequency
* RISE differencing of stacked images


Reference Images (Stacks)
************************************

Incoming images are stacked using AWAI



Difference Images
************************************

Each incoming exposure will be differenced once a reference is available.

Alerts
************************************

RAPID will follow community standards for alerts, packaging individual events in Apache AVRO format, then publishing via Apache Kafka. The alert stream is likely to split into multiple *topics* based on distinct criteria.

Kafka is designed as a high-performance messaging system, suitable for *hot* data rather than *cold* archiving. It is necessary to expire alerts after some time to save storage. cost and performance. The AVRO alerts will be separately packaged into periodic tarball for delivery to MAST for long-term storage. 


Light Curves
************************************

Currently RAPID is storing candidate data in a relational database, but will periodically export to Apache Parquet for delivery to MAST and onwards to the community.

It is not yet decided whether to partition the Parquet files following HATS. LINCC currently only offer a bulk-import tool, which would require a 2-step export then conversion. It may be possible to export natively by setting the partitioning in advance. 


Forced-Photometry service
************************************

RAPID development of this feature is scheduled for Q4 2025. There is demand for similar service elsewhere within Roman, and collaboration of developing/hosting a common tool is under discussion.

From ZTF experience:
* There is high demand for forced-photometry (which will only grow with Rubin)
* The forced-photometry service will require:

  * Request submission interface
  * Job submission and reporting infrastructure
  * High-performance, especially batching requests to save I/O

* A continously updated cache of likely/popular requests for common sources/catalogs