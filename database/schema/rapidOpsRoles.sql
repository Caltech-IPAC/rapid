--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsRoles.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 15 April 2024
--------------------------------------------------------------------------------------------------------------------------

create role rapidadminrole LOGIN SUPERUSER CREATEDB CREATEROLE;
create role rapidporole;
create role rapidreadrole;

GRANT rapidadminrole to ubuntu;
GRANT rapidporole to ubuntu;
GRANT rapidreadrole to ubuntu;

GRANT rapidporole to rapidporuss;

GRANT rapidreadrole to apollo;

-- Verified apollo inherits the following:
ALTER ROLE rapidreadrole CONNECTION LIMIT -1;

ALTER ROLE rapidporole CONNECTION LIMIT -1;
