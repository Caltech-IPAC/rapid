--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsSourcesProcs.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 29 August 2025
--------------------------------------------------------------------------------------------------------------------------


-- Insert a new record into the AstroObjects table.
--
create function addAstroObjects (
    ra0_                  double precision,
    dec0_                 double precision,
    mag0_                 real,
    meanra_               double precision,
    stdevra_              real,
    meandec_              double precision,
    stdevdec_             real,
    meanmag_              real,
    stdevmag_             real,
    nsources_             smallint,
    field_                integer,
    hp6_                  integer,
    hp9_                  integer
)
    returns integer as $$

    declare

        aid_           bigint;

    begin

        begin

            insert into AstroObjects
            (ra0,
             dec0,
             mag0,
             meanra,
             stdevra,
             meandec,
             stdevdec,
             meanmag,
             stdevmag,
             nsources,
             field,
             hp6,
             hp9
            )
            values
            (ra0_,
             dec0_,
             mag0_,
             meanra_,
             stdevra_,
             meandec_,
             stdevdec_,
             meanmag_,
             stdevmag_,
             nsources_,
             field_,
             hp6_,
             hp9_
            )
            returning aid into strict aid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addAstroObjects: Row could not be inserted into AstroObjects table.';
        end;

        return aid_;

    end;

$$ language plpgsql;




