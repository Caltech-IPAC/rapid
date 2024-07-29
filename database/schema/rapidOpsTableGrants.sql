--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsTableGrants.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 15 April 2024
--------------------------------------------------------------------------------------------------------------------------


-------------------
-- Filters table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE filters FROM rapidreadrole;
GRANT SELECT ON TABLE filters TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE filters FROM rapidadminrole;
GRANT ALL ON TABLE filters TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE filters FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE filters TO rapidporole;


-------------------
-- Chips table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE chips FROM rapidreadrole;
GRANT SELECT ON TABLE chips TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE chips FROM rapidadminrole;
GRANT ALL ON TABLE chips TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE chips FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE chips TO rapidporole;


-------------------
-- Exposures table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE exposures FROM rapidreadrole;
GRANT SELECT ON TABLE exposures TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE exposures_expid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE exposures FROM rapidadminrole;
GRANT ALL ON TABLE exposures TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE exposures_expid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE exposures_expid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE exposures FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE exposures TO rapidporole;

REVOKE ALL ON SEQUENCE exposures_expid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE exposures_expid_seq TO rapidporole;


-------------------
-- L2files table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE l2files FROM rapidreadrole;
GRANT SELECT ON TABLE l2files TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE l2files_rid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE l2files FROM rapidadminrole;
GRANT ALL ON TABLE l2files TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE l2files_rid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE l2files_rid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE l2files FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE l2files TO rapidporole;

REVOKE ALL ON SEQUENCE l2files_rid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE l2files_rid_seq TO rapidporole;


-------------------
-- L2fileMeta table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE l2filemeta FROM rapidreadrole;
GRANT SELECT ON TABLE l2filemeta TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE l2filemeta FROM rapidadminrole;
GRANT ALL ON TABLE l2filemeta TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE l2filemeta FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE l2filemeta TO rapidporole;


-------------------
-- Pipelines table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE pipelines FROM rapidreadrole;
GRANT SELECT ON TABLE pipelines TO GROUP rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE pipelines FROM rapidadminrole;
GRANT ALL ON TABLE pipelines TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE pipelines FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE pipelines TO rapidporole;


-------------------
-- SwVersions table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE swversions FROM rapidreadrole;
GRANT SELECT ON TABLE swversions TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE swversions_svid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE swversions FROM rapidadminrole;
GRANT ALL ON TABLE swversions TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE swversions_svid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE swversions_svid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE swversions FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE swversions TO rapidporole;

REVOKE ALL ON SEQUENCE swversions_svid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE swversions_svid_seq TO rapidporole;


-------------------
-- ArchiveVersions table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE archiveversions FROM rapidreadrole;
GRANT SELECT ON TABLE archiveversions TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE archiveversions_avid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE archiveversions FROM rapidadminrole;
GRANT ALL ON TABLE archiveversions TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE archiveversions_avid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE archiveversions_avid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE archiveversions FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE archiveversions TO rapidporole;

REVOKE ALL ON SEQUENCE archiveversions_avid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE archiveversions_avid_seq TO rapidporole;


-------------------
-- RefImages table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE refimages FROM rapidreadrole;
GRANT SELECT ON TABLE refimages TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE refimages_rfid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE refimages FROM rapidadminrole;
GRANT ALL ON TABLE refimages TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE refimages_rfid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE refimages_rfid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE refimages FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE refimages TO rapidporole;

REVOKE ALL ON SEQUENCE refimages_rfid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE refimages_rfid_seq TO rapidporole;


-------------------
-- DiffImages table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE diffimages FROM rapidreadrole;
GRANT SELECT ON TABLE diffimages TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE diffimages_pid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE diffimages FROM rapidadminrole;
GRANT ALL ON TABLE diffimages TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE diffimages_pid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE diffimages_pid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE diffimages FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE diffimages TO rapidporole;

REVOKE ALL ON SEQUENCE diffimages_pid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE diffimages_pid_seq TO rapidporole;


-------------------
-- AlertNames table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE alertnames FROM rapidreadrole;
GRANT SELECT ON TABLE alertnames TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE alertnames_an24id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an25id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an26id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an27id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an28id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an29id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an30id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an31id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an32id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an33id_seq FROM rapidreadrole;
REVOKE ALL ON SEQUENCE alertnames_an34id_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE alertnames FROM rapidadminrole;
GRANT ALL ON TABLE alertnames TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE alertnames_an24id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an25id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an26id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an27id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an28id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an29id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an30id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an31id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an32id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an33id_seq FROM rapidadminrole;
REVOKE ALL ON SEQUENCE alertnames_an34id_seq FROM rapidadminrole;

GRANT ALL ON SEQUENCE alertnames_an24id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an25id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an26id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an27id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an28id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an29id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an30id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an31id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an32id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an33id_seq TO GROUP rapidadminrole;
GRANT ALL ON SEQUENCE alertnames_an34id_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE alertnames FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE alertnames TO rapidporole;

REVOKE ALL ON SEQUENCE alertnames_an24id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an25id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an26id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an27id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an28id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an29id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an30id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an31id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an32id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an33id_seq FROM rapidporole;
REVOKE ALL ON SEQUENCE alertnames_an34id_seq FROM rapidporole;

GRANT USAGE ON SEQUENCE alertnames_an24id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an25id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an26id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an27id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an28id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an29id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an30id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an31id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an32id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an33id_seq TO rapidporole;
GRANT USAGE ON SEQUENCE alertnames_an34id_seq TO rapidporole;


-------------------
-- Jobs table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE jobs FROM rapidreadrole;
GRANT SELECT ON TABLE jobs TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE jobs_jid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE jobs FROM rapidadminrole;
GRANT ALL ON TABLE jobs TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE jobs_jid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE jobs_jid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE jobs FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE jobs TO rapidporole;

REVOKE ALL ON SEQUENCE jobs_jid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE jobs_jid_seq TO rapidporole;
