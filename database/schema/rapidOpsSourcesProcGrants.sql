--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsSourcesProcGrants.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 29 August 2025
--------------------------------------------------------------------------------------------------------------------------


grant EXECUTE on FUNCTION addAstroObjects (
    ra0_                  double precision,
    dec0_                 double precision,
    flux0_                real,
    meanra_               double precision,
    stdevra_              real,
    meandec_              double precision,
    stdevdec_             real,
    meanflux_             real,
    stdevflux_            real,
    nsources_             smallint,
    field_                integer,
    hp6_                  integer,
    hp9_                  integer
) to rapidporole;


grant EXECUTE on FUNCTION registerMerge (
    aid_ integer,
    sid_ integer
) to rapidporole;
