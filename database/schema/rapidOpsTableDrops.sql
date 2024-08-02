--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsTableDrops.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 15 April 2024
--------------------------------------------------------------------------------------------------------------------------


-------------------
-- L2FileMeta table
-------------------

DROP TABLE l2filemeta;


-------------------
-- L2Files table
-------------------

DROP TABLE l2files;
DROP SEQUENCE l2files_rid_seq;


-------------------
-- Exposures table
-------------------

DROP TABLE exposures;
DROP SEQUENCE exposures_expid_seq;


-------------------
-- Filters table
-------------------

DROP TABLE filters;


-------------------
-- SCAs table
-------------------

DROP TABLE scas;

-------------------
-- ArchiveVersions table
-------------------

DROP TABLE archiveversions;
DROP SEQUENCE archiveversions_avid_seq;

-------------------
-- RefImages table
-------------------

DROP TABLE refimages;
DROP SEQUENCE refimages_rfid_seq;


-------------------
-- DiffImages table
-------------------

DROP TABLE diffimages;
DROP SEQUENCE diffimages_pid_seq;

-------------------
-- Pipelines table
-------------------

DROP TABLE pipelines;


-------------------
-- SwVersions table
-------------------

DROP TABLE swversions;
DROP SEQUENCE swversions_svid_seq;

-------------------
-- AlertNames table
-------------------

DROP TABLE alertnames;
DROP SEQUENCE alertnames_an24id_seq;
DROP SEQUENCE alertnames_an25id_seq;
DROP SEQUENCE alertnames_an26id_seq;
DROP SEQUENCE alertnames_an27id_seq;
DROP SEQUENCE alertnames_an28id_seq;
DROP SEQUENCE alertnames_an29id_seq;
DROP SEQUENCE alertnames_an30id_seq;
DROP SEQUENCE alertnames_an31id_seq;
DROP SEQUENCE alertnames_an32id_seq;
DROP SEQUENCE alertnames_an33id_seq;
DROP SEQUENCE alertnames_an34id_seq;


-------------------
-- Jobs table
-------------------

DROP TABLE jobs;
DROP SEQUENCE jobs_jid_seq;


-------------------
-- RefImCatalogs table
-------------------

DROP TABLE refimcatalogs;
DROP SEQUENCE refimcatalogs_rfcatid_seq;
