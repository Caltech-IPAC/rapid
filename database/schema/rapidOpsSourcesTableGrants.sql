--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsSourcesTableGrants.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 8 August 2025
--------------------------------------------------------------------------------------------------------------------------


-------------------
-- Allow pipeline software to create sources and astroobjects like-tables:
-------------------

GRANT CREATE ON SCHEMA public TO rapidporuss;


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


-------------------
-- AstroObjects table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE astroobjects FROM rapidreadrole;
GRANT SELECT ON TABLE astroobjects TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE astroobjects_aid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE astroobjects FROM rapidadminrole;
GRANT ALL ON TABLE astroobjects TO GROUP rapidadminrole;

REVOKE ALL ON SEQUENCE astroobjects_aid_seq FROM rapidadminrole;
GRANT ALL ON SEQUENCE astroobjects_aid_seq TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE astroobjects FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE astroobjects TO rapidporole;

REVOKE ALL ON SEQUENCE astroobjects_aid_seq FROM rapidporole;
GRANT USAGE ON SEQUENCE astroobjects_aid_seq TO rapidporole;


-------------------
-- Merges table
-------------------

-- rapidreadrole

REVOKE ALL ON TABLE merges FROM rapidreadrole;
GRANT SELECT ON TABLE merges TO GROUP rapidreadrole;

REVOKE ALL ON SEQUENCE merges_aid_seq FROM rapidreadrole;

-- rapidadminrole

REVOKE ALL ON TABLE merges FROM rapidadminrole;
GRANT ALL ON TABLE merges TO GROUP rapidadminrole;

-- rapidporole

REVOKE ALL ON TABLE merges FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE merges TO rapidporole;
