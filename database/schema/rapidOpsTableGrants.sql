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
