--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsProcs.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 16 April 2024
--------------------------------------------------------------------------------------------------------------------------


-- Insert a new record into the Exposures table or update existing one.
--
create function addExposure (
    dateobs_             timestamp,
    mjdobs_              double precision,
    field_               integer,
    filter_              character varying(16),
    exptime_             real,
    infobits_            integer,
    status_              smallint
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
                 fid,
                 exptime,
                 status,
                 infobits
                )
                values
                (dateobs_,
                 mjdobs_,
                 field_,
                 fid_,
                 exptime_,
                 status_,
                 infobits_
                )
                returning expid into strict expid_;
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in registerExposure: Row could not be inserted into Exposures table.';
            end;

        else

            -- Update record in Exposures table.

            expid_ := expid__;

            update Exposures
            set dateobs = dateobs_,
                mjdobs = mjdobs_,
                field = field_,
                fid = fid_,
                exptime = exptime_,
                status = status_,
                infobits = infobits_,
                created = now()
            where expid = expid_;

        end if;

        select expid_, fid_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Insert a new record into the L2Files table.
--
create function addL2File (
    expid_                integer,
    chipid_               smallint,
    field_                integer,
    fid_                  smallint,
    dateobs_              timestamp without time zone,
    mjdobs_               double precision,
    exptime_              real,
    infobits_             integer,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint,
    crval1_               double precision,
    crval2_               double precision,
    crpix1_               real,
    crpix2_               real,
    cd11_                 double precision,
    cd12_                 double precision,
    cd21_                 double precision,
    cd22_                 double precision,
    ctype1_               character varying(16),
    ctype2_               character varying(16),
    cunit1_               character varying(16),
    cunit2_               character varying(16),
    a_order_              smallint,
    a_0_2_                double precision,
    a_0_3_                double precision,
    a_0_4_                double precision,
    a_1_1_                double precision,
    a_1_2_                double precision,
    a_1_3_                double precision,
    a_2_0_                double precision,
    a_2_1_                double precision,
    a_2_2_                double precision,
    a_3_0_                double precision,
    a_3_1_                double precision,
    a_4_0_                double precision,
    b_order_              smallint,
    b_0_2_                double precision,
    b_0_3_                double precision,
    b_0_4_                double precision,
    b_1_1_                double precision,
    b_1_2_                double precision,
    b_1_3_                double precision,
    b_2_0_                double precision,
    b_2_1_                double precision,
    b_2_2_                double precision,
    b_3_0_                double precision,
    b_3_1_                double precision,
    b_4_0_                double precision,
    equinox_              real,
    ra_                   double precision,
    dec_                  double precision,
    paobsy_               real,
    pafpa_                real,
    zptmag_               real,
    skymean_              real
)
    returns record as $$

    declare

        r_               record;
        rid_              integer;
        version_          smallint;
        status_           smallint;
        vbest_            smallint;

    begin

        -- Processed images are versioned according to unique (expid, chipid) pairs.

        -- Note that the vBest flag is updated when database stored
        -- function updateL2File is executed.

        select coalesce(max(version), 0) + 1
        into version_
        from L2Files
        where expid = expid_
        and chipid = chipid_;

        if not found then
            version_ := 1;
        end if;

        status_ := 0;
        vbest_ := 0;

        -- Insert L2Files record.

        begin

            insert into L2Files
            (expid,chipid,version,status,vbest,
             field,fid,dateobs,mjdobs,exptime,infobits,
             filename,checksum,crval1,crval2,
             crpix1,crpix2,cd11,cd12,cd21,cd22,ctype1,ctype2,
             cunit1,cunit2,a_order,a_0_2,a_0_3,a_0_4,a_1_1,a_1_2,
             a_1_3,a_2_0,a_2_1,a_2_2,a_3_0,a_3_1,a_4_0,b_order,
             b_0_2,b_0_3,b_0_4,b_1_1,b_1_2,b_1_3,b_2_0,b_2_1,
             b_2_2,b_3_0,b_3_1,b_4_0,equinox,ra,dec,paobsy,pafpa,
             zptmag,skymean
            )
            values
            (expid_,chipid_,version_,status_,vbest_,
             field_,fid_,dateobs_,mjdobs_,exptime_,infobits_,
             filename_,checksum_,crval1_,crval2_,
             crpix1_,crpix2_,cd11_,cd12_,cd21_,cd22_,ctype1_,ctype2_,
             cunit1_,cunit2_,a_order_,a_0_2_,a_0_3_,a_0_4_,a_1_1_,a_1_2_,
             a_1_3_,a_2_0_,a_2_1_,a_2_2_,a_3_0_,a_3_1_,a_4_0_,b_order_,
             b_0_2_,b_0_3_,b_0_4_,b_1_1_,b_1_2_,b_1_3_,b_2_0_,b_2_1_,
             b_2_2_,b_3_0_,b_3_1_,b_4_0_,equinox_,ra_,dec_,paobsy_,pafpa_,
             zptmag_,skymean_
            )
            returning rid into strict rid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addL2File: L2Files record for expid,chipid=%,% not inserted.', expid_,chipid_;

        end;

        select rid_, version_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Update a L2Files record with a filename, checksum, status, and version.
-- The status must have a non-zero value for the record to be valid.
-- The vBest flag for the record is also updated automatically, from
-- zero to one, unless the record has been locked (with vBest flag = 2).
--
create function updateL2File (
    rid_      integer,
    filename_ varchar(255),
    checkSum_ varchar(32),
    status_   smallint,
    version_  smallint
)

    returns void as $$

    declare

        rid__            integer;
        currentVBest_    smallint;
        expid_           integer;
        chipid_          smallint;
        vBest_           smallint;
        bestIs2_         boolean;
        count_           integer;

    begin

        bestIs2_ := 'f';

        -- First, get the expid, chipid for the L2 image.

        begin

            select expid, chipid
            into strict expid_, chipid_
            from l2files
            where rid = rid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in updateL2File: L2Files record not found for rid=%', rid_;

        end;

        -- If this isn't the first L2Files record
        -- for the exposure and chip, then set the vBest flag to 0
        -- for records associated with all prior versions. Update
        -- the new L2Files record with its version number and
        -- vBest flag equal to 1 (latest is best).

        -- If any of the products associated with the L2Files record has a
        -- locked vBest flag (meaning vBest has been set to 2), then
        -- don't update any vBest values and set the new vBest value
        -- to 0. Otherwise, the record we are about to insert is the
        -- new best record (i.e., vBest = 1) and all others are not
        -- (i.e., vBest = 0).

        -- Raise exception if more than one record with vBest > 0 is found.


        select count(*)
        into count_
        from L2Files
        where expid = expid_
        and chipid = chipid_
        and vBest in (1, 2);

        if (count_ <> 0) then
            if (count_ > 1) then
                raise exception
                    '*** Error in updateL2File: More than one L2Files record with vBest>0 returned.';
            end if;

            select rid, vBest
            into rid__, currentVBest_
            from L2Files
            where expid = expid_
            and chipid = chipid_
            and vBest in (1, 2);

            if (currentvBest_ = 1) then -- vBest is not locked
                update L2Files
                set vBest = 0
                where rid = rid__;

                if not found then
                    raise exception '*** Error in updateL2File: Cannot update L2Files record.';
                end if;
            else
                bestIs2_ := 't';
            end if;

        end if;

        if bestIs2_ = 't' then
            vBest_ := 0;
        else
            vBest_ := 1;
        end if;

        update L2Files
        set filename = filename_,
        checkSum = checkSum_,
        status = status_,
        version = version_,
        vBest = vBest_
        where rid = rid_;

        exception --------> Required for turning entire block into a transaction.
            when no_data_found then
                raise exception
                    '*** Error in updateL2File: Cannot update L2Files record for rid=%', rid_;

    end;

$$ language plpgsql;
