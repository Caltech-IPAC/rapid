--------------------------------------------------------------------------------------------------------------------------
-- 20260929_jobs_reqid
--
-- Add Jobs.reqid, the ProcReqs record of the processing request that ran the job.
--
-- The pipeline codes file their S3 objects under "<proc_date>/req<reqid>", and a job's
-- products can only be located again if the job records which request ran it.  Jobs that
-- ran before this column existed keep reqid = NULL, which the reading codes take to mean
-- the older layout, with the products directly under the processing date.
--
-- Apply to an existing database only.  A database built from scratch with
-- database/scripts/buildDatabase.sh already has all of this, because
-- database/schema/rapidOpsTables.sql and database/schema/rapidOpsProcs.sql define it.
--
-- This migration is self-contained: run it by itself, with nothing else.  startJob() is
-- the only stored function that changed, so re-running rapidOpsProcDrops.sql and
-- rapidOpsProcs.sql is not needed and would drop and recreate every other function on a
-- live operations database for no reason.
--
-- Run it in one transaction, so that a failure part way through leaves nothing behind:
--
--     psql ... --single-transaction -v ON_ERROR_STOP=1 -f 20260929_jobs_reqid.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 29 September 2026
--------------------------------------------------------------------------------------------------------------------------


-- Add the column.  It is nullable with no default, so PostgreSQL rewrites only the
-- catalog and not the table, and the statement returns immediately however large
-- the Jobs table has grown.

ALTER TABLE jobs ADD COLUMN reqid integer;

COMMENT ON COLUMN jobs.reqid IS 'ProcReqs record of the processing request that ran the job';


SET default_tablespace = pipeline_indx_01;

CREATE INDEX jobs_reqid_idx ON jobs (reqid);

RESET default_tablespace;


-- Replace startJob() with the version that records the processing request.
--
-- The old eight-argument function is named explicitly here.  Re-running
-- rapidOpsProcDrops.sql would not remove it, because that file now carries the
-- nine-argument signature, and leaving it behind would give the database two
-- overloads of startJob.

DROP FUNCTION startJob (
    ppid_           smallint,
    fid_            smallint,
    expid_          integer,
    field_          integer,
    sca_            smallint,
    rid_            integer,
    machine_        smallint,
    slurm_          integer
);


-- Identical to the definition in database/schema/rapidOpsProcs.sql.

create function startJob (
    ppid_           smallint,
    fid_            smallint,
    expid_          integer,
    field_          integer,
    sca_            smallint,
    rid_            integer,
    machine_        smallint,
    slurm_          integer,
    reqid_          integer
)
    returns integer as $$

    declare

        jid_     integer;

    begin

        -- Insert record if not found.

        if (expid_ is not null) then

            select jid
            into jid_
            from Jobs
            where ppid = ppid_
            and rid = rid_;

        else

            select jid
            into jid_
            from Jobs
            where ppid = ppid_
            and fid = fid_
            and field = field_;

        end if;

        if not found then

            -- Insert Jobs record.

            begin

                insert into Jobs
                (ppid,
                 expid,
                 field,
                 sca,
                 fid,
                 rid,
                 machine,
                 slurm,
                 reqid,
                 launched)
                values
                (ppid_,
                 expid_,
                 field_,
                 sca_,
                 fid_,
                 rid_,
                 machine_,
                 slurm_,
                 reqid_,
                 now())
                returning jid into strict jid_;
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in startJob: Row could not be inserted into Jobs table.';

            end;

        else

            -- Update Jobs record.

            update Jobs
            set machine = machine_,
                slurm = slurm_,
                reqid = reqid_,
                launched = now(),
                started = null,
                ended = null,
                elapsed = null,
                qwaited = null,
                exitcode = null,
                awsbatchjobid = null,
                status = 0
            where jid = jid_;

        end if;

        return jid_;

    end;

$$ language plpgsql;

grant EXECUTE on FUNCTION startJob (
    ppid_           smallint,
    fid_            smallint,
    expid_          integer,
    field_          integer,
    sca_            smallint,
    rid_            integer,
    machine_        smallint,
    slurm_          integer,
    reqid_          integer
) to rapidporole;
