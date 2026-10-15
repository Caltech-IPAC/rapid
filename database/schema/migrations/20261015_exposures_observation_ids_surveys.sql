--------------------------------------------------------------------------------------------------------------------------
-- 20261015_exposures_observation_ids_surveys
--
-- Add the Roman observation identifiers to Exposures, the Surveys table, and the new addExposure().
-- For an existing database only; run by itself in one transaction, then load the Surveys rows:
--
--     psql ... --single-transaction -v ON_ERROR_STOP=1 -f 20261015_exposures_observation_ids_surveys.sql
--     psql ... -v ON_ERROR_STOP=1 -f database/schema/rapidOpsSurveysInserts.sql
--------------------------------------------------------------------------------------------------------------------------


-- The Surveys table.  Identical to the definition in database/schema/rapidOpsTables.sql.

SET default_tablespace = pipeline_data_01;

CREATE TABLE surveys (
    program_id integer NOT NULL,
    program_title character varying(255) NOT NULL,
    category character varying(6),
    created timestamp without time zone
        DEFAULT now() NOT NULL
);

ALTER TABLE surveys OWNER TO rapidadminrole;

SET default_tablespace = pipeline_indx_01;

ALTER TABLE ONLY surveys ADD CONSTRAINT surveys_pkey PRIMARY KEY (program_id);

CREATE INDEX surveys_program_title_idx ON surveys (program_title);
CREATE INDEX surveys_category_idx ON surveys (category);

RESET default_tablespace;

REVOKE ALL ON TABLE surveys FROM rapidreadrole;
GRANT SELECT ON TABLE surveys TO GROUP rapidreadrole;

REVOKE ALL ON TABLE surveys FROM rapidadminrole;
GRANT ALL ON TABLE surveys TO GROUP rapidadminrole;

REVOKE ALL ON TABLE surveys FROM rapidporole;
GRANT INSERT,UPDATE,SELECT,REFERENCES ON TABLE surveys TO rapidporole;


-- Nullable with no default, so the table is not rewritten.

ALTER TABLE exposures
    ADD COLUMN program_id integer,
    ADD COLUMN execution_plan smallint,
    ADD COLUMN pass smallint,
    ADD COLUMN segment smallint,
    ADD COLUMN observation smallint,
    ADD COLUMN visit smallint,
    ADD COLUMN exposure smallint,
    ADD COLUMN visit_id character varying(19),
    ADD COLUMN observation_id character varying(28);

SET default_tablespace = pipeline_indx_01;

CREATE INDEX exposures_program_id_idx ON exposures (program_id);
CREATE INDEX exposures_visit_id_idx ON exposures (visit_id);
CREATE INDEX exposures_observation_id_idx ON exposures (observation_id);

RESET default_tablespace;


-- Drop the old nine-argument addExposure() so it does not remain as a second overload.

DROP FUNCTION addExposure (
    dateobs_             timestamp,
    mjdobs_              double precision,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    filter_              character varying(16),
    exptime_             real,
    infobits_            integer,
    status_              smallint
);


-- Identical to the definition in database/schema/rapidOpsProcs.sql.

create function addExposure (
    dateobs_             timestamp,
    mjdobs_              double precision,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    filter_              character varying(16),
    exptime_             real,
    infobits_            integer,
    status_              smallint,
    program_id_          integer DEFAULT NULL,
    execution_plan_      smallint DEFAULT NULL,
    pass_                smallint DEFAULT NULL,
    segment_             smallint DEFAULT NULL,
    observation_         smallint DEFAULT NULL,
    visit_               smallint DEFAULT NULL,
    exposure_            smallint DEFAULT NULL,
    visit_id_            character varying(19) DEFAULT NULL,
    observation_id_      character varying(28) DEFAULT NULL
)
    returns record as $$

    declare

        r_               record;
        fid_             smallint;
        expid_           integer;
        expid__          integer;

    begin


        begin

            select fid
            into strict fid_
            from Filters
            where filter = filter_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addExposure: Filters record filter=% not found.', filter_;

        end;


        -- Insert or update record.

        expid__ := null;

        select expid
        into expid__
        from Exposures
        where dateobs = dateobs_;

        if (expid__ is null) then

            -- Insert Exposures record.

            begin

                insert into Exposures
                (dateobs,
                 mjdobs,
                 field,
                 hp6,
                 hp9,
                 fid,
                 exptime,
                 status,
                 infobits,
                 program_id,
                 execution_plan,
                 pass,
                 segment,
                 observation,
                 visit,
                 exposure,
                 visit_id,
                 observation_id
                )
                values
                (dateobs_,
                 mjdobs_,
                 field_,
                 hp6_,
                 hp9_,
                 fid_,
                 exptime_,
                 status_,
                 infobits_,
                 program_id_,
                 execution_plan_,
                 pass_,
                 segment_,
                 observation_,
                 visit_,
                 exposure_,
                 visit_id_,
                 observation_id_
                )
                returning expid into strict expid_;
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in addExposure: Row could not be inserted into Exposures table.';
            end;

        else

            -- Update record in Exposures table.

            expid_ := expid__;

            update Exposures
            set dateobs = dateobs_,
                mjdobs = mjdobs_,
                field = field_,
                hp6 = hp6_,
                hp9 = hp9_,
                fid = fid_,
                exptime = exptime_,
                status = status_,
                infobits = infobits_,
                program_id = program_id_,
                execution_plan = execution_plan_,
                pass = pass_,
                segment = segment_,
                observation = observation_,
                visit = visit_,
                exposure = exposure_,
                visit_id = visit_id_,
                observation_id = observation_id_,
                created = now()
            where expid = expid_;

        end if;

        select expid_, fid_ into r_;

        return r_;

    end;

$$ language plpgsql;

grant EXECUTE on FUNCTION addExposure (
    dateobs_             timestamp,
    mjdobs_              double precision,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    filter_              character varying(16),
    exptime_             real,
    infobits_            integer,
    status_              smallint,
    program_id_          integer,
    execution_plan_      smallint,
    pass_                smallint,
    segment_             smallint,
    observation_         smallint,
    visit_               smallint,
    exposure_            smallint,
    visit_id_            character varying(19),
    observation_id_      character varying(28)
) to rapidporole;
