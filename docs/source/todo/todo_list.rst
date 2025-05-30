To-Do List
####################################################


Pipeline Software
*************************************

+-----------------+----------------------------------------------------------+-------+
|Task             | Comment                                                  | Done? |
+=================+==========================================================+=======+
| Ingest L2 files | L2 files from the SOC will be in ASDF format.  We need   | No    |
|                 | to copy them to our S3 bucket and register records in    |       |
|                 | the L2 files table of our operations database.           |       |
+-----------------+----------------------------------------------------------+-------+
| QA system       | Continue improving and refining.                         | Basic |
+-----------------+----------------------------------------------------------+-------+
| Parallel DB     | Modify registerCompletedJobsInDB.py to                   | Yes   |
| record insert   | register database records in parallel.                   |       |
+-----------------+----------------------------------------------------------+-------+
| Solar system    | Joe Masiero's code needs to be integrated                | No    |
| Objects         | into the science pipeline.                               |       |
+-----------------+----------------------------------------------------------+-------+
| SFFT crossconv  | Propagate from configuration file.                       | No    |
| flag            |                                                          |       |
+-----------------+----------------------------------------------------------+-------+
| FILEDATE        | Added FILEDATE to headers of all FITS-file products.     | No    |
| needed          |                                                          |       |
+-----------------+----------------------------------------------------------+-------+



Operations Database
*************************************

+-----------------+----------------------------------------------------------+-------+
|Task             | Comment                                                  | Done? |
+=================+==========================================================+=======+
| Database        | Need automated software in place to periodically         | No    |
| backups         | backup the operations database                           |       |
|                 |                                                          |       |
+-----------------+----------------------------------------------------------+-------+
| Database        | Need to select a powerful enough EC2 machine that runs   | No    |
| for pipeline    | 24/7 (consider cost), and set up PostgreSQL database on  |       |
| operations      | it for pipeline operations.  Must have multiple cores.   |       |
|                 | Cost it out on AWS.                                      |       |
+-----------------+----------------------------------------------------------+-------+
| New Fields      | Nice to have would be a Fields table indexed by field    | No    |
| table           | that has (ra,dec) of center and 4 corners of tile.       |       |
|                 | Get data from our SQLite Roman tesselation database.     |       |
+-----------------+----------------------------------------------------------+-------+
