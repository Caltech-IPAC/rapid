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
)
    returns integer as $$

    declare

        aid_           bigint;

    begin

        begin

            insert into AstroObjects
            (ra0,
             dec0,
             flux0,
             meanra,
             stdevra,
             meandec,
             stdevdec,
             meanflux,
             stdevflux,
             nsources,
             field,
             hp6,
             hp9
            )
            values
            (ra0_,
             dec0_,
             flux0_,
             meanra_,
             stdevra_,
             meandec_,
             stdevdec_,
             meanflux_,
             stdevflux_,
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


-- Insert a new record into the Merges table, if record is not found.
--
create function registerMerge (
    aid_ integer,
    sid_ integer
)
    returns void as $$

    declare

        aid__  integer;

    begin

        select aid
        into aid__
        from Merges
        where aid = aid_
        and sid = sid_;

        if not found then

            begin

                insert into Merges
                (aid, sid)
                values
                (aid_, sid_);
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in registerMerge: Merges record for aid=%, sid=% not inserted.', aid_, sid_;

            end;

        end if;

    end;

$$ language plpgsql;




