--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsSourcesTableGrants.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 8 August 2025
--------------------------------------------------------------------------------------------------------------------------


-------------------
-- Sources table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE sources FROM rapidreadrole;
GRANT SELECT ON TABLE sources TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE sources_sid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE sources FROM rapidadminrole;
GRANT ALL ON TABLE sources TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE sources_sid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE sources_sid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE sources FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE sources TO rapidporole;

REVOKE ALL ON SEQUENCE sources_sid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE sources_sid_seq TO rapidporole;

