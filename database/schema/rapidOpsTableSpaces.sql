--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsTableSpaces.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 15 April 2024
--------------------------------------------------------------------------------------------------------------------------


-- The TABLESPACE directory must exist before the TABLESPACE can be created in the database.

CREATE TABLESPACE pipeline_data_01 LOCATION '/data/db/tablespacedata1';
CREATE TABLESPACE pipeline_indx_01 LOCATION '/data/db/tablespaceindx1';
