To-Do List
####################################################

Pipeline Software
*************************************

.. list-table::
   :header-rows: 1
   :widths: 22 68 10

   * - Task
     - Comment
     - Done?
   * - Ingest L2 files
     - L2 files from the SOC will be in ASDF format. Copy them to our S3
       bucket and register records in the L2 files table of our operations
       database.
     - No
   * - QA system
     - Continue improving and refining.
     - Basic
   * - Parallel DB record insert
     - Modify registerCompletedJobsInDB.py to register database records
       in parallel.
     - Yes
   * - Solar system Objects
     - Integrate Joe Masiero's code into the science pipeline.
     - No
   * - SFFT crossconv flag
     - Propagate from the configuration file.
     - Yes
   * - FILEDATE needed
     - Update the DATE value in the header of all FITS-file products.
     - Yes
   * - Position refinement for ZOGY
     - Compute median offsets in delta x and y from sci/ref cross-matched
       isolated sources. Use them for orthogonal subpixel-shifting of
       reference image data before ZOGY.
     - Yes
   * - Astrometric error for ZOGY
     - Compute RMS delta x and y from sci/ref cross-matched isolated
       sources as ZOGY input.
     - Yes
   * - Gain-matching for ZOGY
     - Compute the image-data gain-match scale factor from sci/ref
       cross-matched isolated sources to scale reference image data
       before ZOGY.
     - Yes


Operations Database
*************************************

.. list-table::
   :header-rows: 1
   :widths: 22 68 10

   * - Task
     - Comment
     - Done?
   * - Database backups
     - Put automated software in place to periodically back up the
       operations database.
     - No
   * - Database for pipeline operations
     - Select a sufficiently powerful, multicore EC2 machine to run 24/7,
       cost it out on AWS, and set up a PostgreSQL database on it for
       pipeline operations.
     - No
   * - New Fields table
     - Nice to have: a Fields table indexed by field, with (ra,dec) of
       the tile center and 4 corners. Get the data from our SQLite Roman
       tesselation database.
     - No
   * - New columns in DiffImMeta table
     - dxrmsfin,dyrmsfin,dxmedianfin,dymedianfin
     - Yes
