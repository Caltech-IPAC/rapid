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
    hp6_                 integer,
    hp9_                 integer,
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
                 hp6,
                 hp9,
                 fid,
                 exptime,
                 status,
                 infobits
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
                 infobits_
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
    sca_                  smallint,
    field_                integer,
    hp6_                  integer,
    hp9_                  integer,
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

        -- Processed images are versioned according to unique (expid, sca) pairs.

        -- Note that the vBest flag is updated when database stored
        -- function updateL2File is executed.

        select coalesce(max(version), 0) + 1
        into version_
        from L2Files
        where expid = expid_
        and sca = sca_;

        if not found then
            version_ := 1;
        end if;

        status_ := 0;
        vbest_ := 0;

        -- Insert L2Files record.

        begin

            insert into L2Files
            (expid,sca,version,status,vbest,
             field,hp6,hp9,fid,dateobs,mjdobs,exptime,infobits,
             filename,checksum,crval1,crval2,
             crpix1,crpix2,cd11,cd12,cd21,cd22,ctype1,ctype2,
             cunit1,cunit2,a_order,a_0_2,a_0_3,a_0_4,a_1_1,a_1_2,
             a_1_3,a_2_0,a_2_1,a_2_2,a_3_0,a_3_1,a_4_0,b_order,
             b_0_2,b_0_3,b_0_4,b_1_1,b_1_2,b_1_3,b_2_0,b_2_1,
             b_2_2,b_3_0,b_3_1,b_4_0,equinox,ra,dec,paobsy,pafpa,
             zptmag,skymean
            )
            values
            (expid_,sca_,version_,status_,vbest_,
             field_,hp6_,hp9_,fid_,dateobs_,mjdobs_,exptime_,infobits_,
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
                        '*** Error in addL2File: L2Files record for expid,sca=%,% not inserted.', expid_,sca_;

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
        sca_             smallint;
        vBest_           smallint;
        bestIs2_         boolean;
        count_           integer;

    begin

        bestIs2_ := 'f';

        -- First, get the expid, sca for the L2 image.

        begin

            select expid, sca
            into strict expid_, sca_
            from l2files
            where rid = rid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in updateL2File: L2Files record not found for rid=%', rid_;

        end;

        -- If this isn't the first L2Files record
        -- for the exposure and sca, then set the vBest flag to 0
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
        and sca = sca_
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
            and sca = sca_
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


-- Insert a new record into or update an existing record in the L2FileMeta table.
--
create function registerL2FileMeta (
    rid_                 integer,
    ra0_                 double precision,
    dec0_                double precision,
    ra1_                 double precision,
    dec1_                double precision,
    ra2_                 double precision,
    dec2_                double precision,
    ra3_                 double precision,
    dec3_                double precision,
    ra4_                 double precision,
    dec4_                double precision,
    x_                   double precision,
    y_                   double precision,
    z_                   double precision,
    hp6_                 integer,
    hp9_                 integer
)
    returns void as $$

    declare

        rid__    integer;

    begin


        -- Insert or update record, as appropriate.

        select rid
        into rid__
        from L2FileMeta
        where rid = rid_;

        if not found then


            -- Insert L2FileMeta record.

            begin

                insert into L2FileMeta
                (rid,
                 ra0,
                 dec0,
                 ra1,
                 dec1,
                 ra2,
                 dec2,
                 ra3,
                 dec3,
                 ra4,
                 dec4,
                 x,
                 y,
                 z,
                 hp6,
                 hp9
                )
                values
                (rid_,
                 ra0_,
                 dec0_,
                 ra1_,
                 dec1_,
                 ra2_,
                 dec2_,
                 ra3_,
                 dec3_,
                 ra4_,
                 dec4_,
                 x_,
                 y_,
                 z_,
                 hp6_,
                 hp9_
                );
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in registerL2FileMeta: L2FileMeta record for rid=% not inserted.', rid_;

            end;

        else


            -- Update L2FileMeta record.

            update L2FileMeta
            set ra0 = ra0_,
                dec0 = dec0_,
                ra1 = ra1_,
                dec1 = dec1_,
                ra2 = ra2_,
                dec2 = dec2_,
                ra3 = ra3_,
                dec3 = dec3_,
                ra4 = ra4_,
                dec4 = dec4_,
                x = x_,
                y = y_,
                z = z_,
                hp6 = hp6_,
                hp9 = hp9_
            where rid = rid_;

        end if;

    end;

$$ language plpgsql;


-- Insert a new record into the DiffImages table.
--
create function addDiffImage (
    rid_                  integer,
    ppid_                 smallint,
    rfid_                 integer,
    infobitssci_          integer,
    infobitsref_          integer,
    ra0_                  double precision,
    dec0_                 double precision,
    ra1_                  double precision,
    dec1_                 double precision,
    ra2_                  double precision,
    dec2_                 double precision,
    ra3_                  double precision,
    dec3_                 double precision,
    ra4_                  double precision,
    dec4_                 double precision,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint
)
    returns record as $$

    declare

        r_                record;
        pid_              integer;
        version_          smallint;
        status_           smallint;
        vbest_            smallint;
        svid_             smallint;
        expid_            integer;
        sca_              smallint;
        field_            integer;
        hp6_              integer;
        hp9_              integer;
        fid_              smallint;
        mjdobs_           double precision;
        jd_               double precision;

    begin

        -- Difference images are versioned according to unique (rid, ppid) pairs.

        -- Note that the vBest flag is updated when database stored
        -- function updateDiffImage is executed.

        select coalesce(max(version), 0) + 1
        into version_
        from DiffImages
        where rid = rid_
        and ppid = ppid_;

        if not found then
            version_ := 1;
        end if;

        -- Get foreign-key values and other quantities for table normalization.

        begin

            select expid, sca, field, hp6, hp9, fid, mjdobs
            into strict expid_, sca_, field_, hp6_, hp9_, fid_, mjdobs_
            from L2Files
            where rid = rid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addProcImage: RawImages record rid=% not found.', rid_;

        end;

        jd_ := mjdobs_ + 2400000.5;

        -- Get software version number.

        begin

            select svid
            into strict svid_
            from SwVersions
            order by svid desc
            limit 1;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addDiffImage: SwVersions record not found.';

        end;

        status_ := 0;
        vbest_ := 0;

        -- Insert DiffImages record.

        begin

            insert into DiffImages
            (rid, ppid, version, status, vbest, filename, checksum,
             expid, sca, field, hp6, hp9, fid, jd, svid,
             rfid, infobitssci, infobitsref,
             ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4
            )
            values
            (rid_, ppid_, version_, status_, vbest_, filename_, checksum_,
             expid_, sca_, field_, hp6_, hp9_, fid_, jd_, svid_,
             rfid_, infobitssci_, infobitsref_,
             ra0_, dec0_, ra1_, dec1_, ra2_, dec2_, ra3_, dec3_, ra4_, dec4_
            )
            returning pid into strict pid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addDiffImage: DiffImages record for rid,ppid=%,% not inserted.', rid_,ppid_;

        end;

        select pid_, version_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Update a DiffImages record with a filename, checksum, status, and version.
-- The status must have a non-zero value for the record to be valid.
-- The vBest flag for the record is also updated automatically, from
-- zero to one, unless the record has been locked (with vBest flag = 2).
--
create function updateDiffImage (
    pid_         integer,
    filename_    varchar(255),
    checkSum_    varchar(32),
    status_      smallint,
    version_     smallint
)

    returns void as $$

    declare

        pid__            integer;
        currentVBest_    smallint;
        rid_             integer;
        ppid_            smallint;
        vBest_           smallint;
        bestIs2_         boolean;
        count_           integer;

    begin

        bestIs2_ := 'f';

        -- First, get the rid, ppid for the difference image.

        begin

            select rid, ppid
            into strict rid_, ppid_
            from DiffImages
            where pid = pid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in updateDiffImage: DiffImages record not found for pid=%', pid_;

        end;

        -- If this isn't the first DiffImages record
        -- for the exposure and sca, then set the vBest flag to 0
        -- for records associated with all prior versions. Update
        -- the new DiffImages record with its version number and
        -- vBest flag equal to 1 (latest is best).

        -- If any of the products associated with the DiffImages record has a
        -- locked vBest flag (meaning vBest has been set to 2), then
        -- don't update any vBest values and set the new vBest value
        -- to 0. Otherwise, the record we are about to insert is the
        -- new best record (i.e., vBest = 1) and all others are not
        -- (i.e., vBest = 0).

        -- Raise exception if more than one record with vBest > 0 is found.


        select count(*)
        into count_
        from DiffImages
        where rid = rid_
        and ppid = ppid_
        and vBest in (1, 2);

        if (count_ <> 0) then
            if (count_ > 1) then
                raise exception
                    '*** Error in updateDiffImage: More than one DiffImages record with vBest>0 returned.';
            end if;

            select pid, vBest
            into pid__, currentVBest_
            from DiffImages
            where rid = rid_
            and ppid = ppid_
            and vBest in (1, 2);

            if (currentvBest_ = 1) then -- vBest is not locked
                update DiffImages
                set vBest = 0
                where pid = pid__;

                if not found then
                    raise exception '*** Error in updateDiffImage: Cannot update DiffImages record.';
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

        update DiffImages
        set filename = filename_,
        checkSum = checkSum_,
        status = status_,
        version = version_,
        vBest = vBest_
        where pid = pid_;

        exception --------> Required for turning entire block into a transaction.
            when no_data_found then
                raise exception
                    '*** Error in updateDiffImage: Cannot update DiffImages record for pid=%', pid_;

    end;

$$ language plpgsql;


-- Insert a new record into the RefImages table.
--
create function addRefImage (
    sca_                  smallint,
    field_                integer,
    hp6_                  integer,
    hp9_                  integer,
    fid_                  smallint,
    ppid_                 smallint,
    rfid_                 integer,
    infobits_             integer,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint
)
    returns record as $$

    declare

        r_                record;
        rfid_             integer;
        version_          smallint;
        status_           smallint;
        vbest_            smallint;
        svid_             smallint;

    begin

        -- Reference images are versioned according to unique (sca, field, fid, ppid) quartets.

        -- Note that the vBest flag is updated when database stored
        -- function updateRefImage is executed.

        select coalesce(max(version), 0) + 1
        into version_
        from RefImages
        where sca = sca_
        and field = field_
        and fid = fid_
        and ppid = ppid_;

        if not found then
            version_ := 1;
        end if;

        -- Get software version number.

        begin

            select svid
            into strict svid_
            from SwVersions
            order by svid desc
            limit 1;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addRefImage: SwVersions record not found.';

        end;

        status_ := 0;
        vbest_ := 0;

        -- Insert RefImages record.

        begin

            insert into RefImages
            (sca, field, hp6, hp9, fid, ppid, version, status, vbest, filename, checksum, infobits, svid)
            values
            (sca_, field_, hp6_, hp9_, fid_, ppid_, version_, status_, vbest_, filename_, checksum_, infobits, svid_)
            returning rfid into strict rfid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addRefImage: RefImages record for sca,field,fid,ppid=%,%,%,% not inserted.', sca_,field_,fid_,ppid_;

        end;

        select rfid_, version_ into r_;

        return r_;

    end;

$$ language plpgsql;


-- Update a RefImages record with a filename, checksum, status, and version.
-- The status must have a non-zero value for the record to be valid.
-- The vBest flag for the record is also updated automatically, from
-- zero to one, unless the record has been locked (with vBest flag = 2).
--
create function updateRefImage (
    rfid_        integer,
    filename_    varchar(255),
    checkSum_    varchar(32),
    status_      smallint,
    version_     smallint
)

    returns void as $$

    declare

        rfid__            integer;
        currentVBest_     smallint;
        sca_              smallint;
        field_            integer;
        fid_              smallint;
        ppid_             smallint;
        vBest_            smallint;
        bestIs2_          boolean;
        count_            integer;

    begin

        bestIs2_ := 'f';

        -- First, get the sca, field, fid, ppid for the reference image.

        begin

            select sca, field, fid, ppid
            into strict sca_, field_ fid_, ppid_
            from RefImages
            where rfid = rfid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in updateRefImage: RefImages record not found for rfid=%', rfid_;

        end;

        -- If this isn't the first RefImages record
        -- for the exposure and sca, then set the vBest flag to 0
        -- for records associated with all prior versions. Update
        -- the new RefImages record with its version number and
        -- vBest flag equal to 1 (latest is best).

        -- If any of the products associated with the RefImages record has a
        -- locked vBest flag (meaning vBest has been set to 2), then
        -- don't update any vBest values and set the new vBest value
        -- to 0. Otherwise, the record we are about to insert is the
        -- new best record (i.e., vBest = 1) and all others are not
        -- (i.e., vBest = 0).

        -- Raise exception if more than one record with vBest > 0 is found.


        select count(*)
        into count_
        from RefImages
        where sca = sca_
        and field = field_
        and fid = fid_
        and ppid = ppid_
        and vBest in (1, 2);

        if (count_ <> 0) then
            if (count_ > 1) then
                raise exception
                    '*** Error in updateRefImage: More than one RefImages record with vBest>0 returned.';
            end if;

            select rfid, vBest
            into rfid__, currentVBest_
            from RefImages
            where sca = sca_
            and field = field_
            and fid = fid_
            and ppid = ppid_
            and vBest in (1, 2);

            if (currentvBest_ = 1) then -- vBest is not locked
                update RefImages
                set vBest = 0
                where rfid = rfid__;

                if not found then
                    raise exception '*** Error in updateRefImage: Cannot update RefImages record.';
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

        update RefImages
        set filename = filename_,
        checkSum = checkSum_,
        status = status_,
        version = version_,
        vBest = vBest_
        where rfid = rfid_;

        exception --------> Required for turning entire block into a transaction.
            when no_data_found then
                raise exception
                    '*** Error in updateRefImage: Cannot update RefImages record for rfid=%', rfid_;

    end;

$$ language plpgsql;


-- Insert a new record into the AlertNames table.
--
create function addAlertName (
    name_   char(12),
    sca_    smallint,
    field_  integer,
    hp6_    integer,
    hp9_    integer,
    ra_     double precision,
    dec_    double precision,
    jd_     double precision,
    candId_ bigint
)
    returns void as $$

    begin


        -- Insert AlertNames record.

        begin

            insert into AlertNames (name, sca, field, hp6, hp9, ra, dec, jd, candId)
            values (name_, sca_, field_, hp6_, hp9_, ra_, dec_, jd_, candId_);
            exception
                when no_data_found then
                    raise exception
                        '*** Error in addAlertName: AlertNames record for name=% not inserted.', name_;

        end;

    end;

$$ language plpgsql;


-- Insert a new record into the AlertNames table.
--
create function computeAlertName (
    yeartwodigits_ smallint
)
    returns char(12) as $$

    declare

        anId_    bigint;
        start_   bigint;
        num_     bigint;
        rem_     bigint;
        res_     varchar(7);
        name_    char(12);
        let_     char(1);
        c        char(1)[];

    begin

        c[1] := 'a';
        c[2] := 'b';
        c[3] := 'c';
        c[4] := 'd';
        c[5] := 'e';
        c[6] := 'f';
        c[7] := 'g';
        c[8] := 'h';
        c[9] := 'i';
        c[10] := 'j';
        c[11] := 'k';
        c[12] := 'l';
        c[13] := 'm';
        c[14] := 'n';
        c[15] := 'o';
        c[16] := 'p';
        c[17] := 'q';
        c[18] := 'r';
        c[19] := 's';
        c[20] := 't';
        c[21] := 'u';
        c[22] := 'v';
        c[23] := 'w';
        c[24] := 'x';
        c[25] := 'y';
        c[26] := 'z';



        -- perl -e '$start=321272407;
        -- @c = qw ( a b c d e f g h i j k l m n o p q r s t u v w x y z );
        -- $num = 0 + $start; $res=""; while ($num > 0) { $num--; $rem=$num % 26;
        -- $let = $c[$rem]; $res = $let . $res; $num = ($num - $rem) / 26; } print "$res\n";'
        -- aaaaaaa

        -- perl -e ' $start=321272407;
        -- @c = qw ( a b c d e f g h i j k l m n o p q r s t u v w x y z );
        -- $num = 8031810175 + $start; $res=""; while ($num > 0) { $num--; $rem=$num % 26;
        -- $let = $c[$rem]; $res = $let + $res . $num = ($num - $rem) / 26; } print "$res\n";'
        -- zzzzzzz

        start_ := 321272407;

        anId_ := - 1;

        if (yeartwodigits_ = 24) then
            select nextval('alertnames_an24id_seq') into anId_;
        elseif (yeartwodigits_ = 25) then
            select nextval('alertnames_an25id_seq') into anId_;
        elseif (yeartwodigits_ = 26) then
            select nextval('alertnames_an26id_seq') into anId_;
        elseif (yeartwodigits_ = 27) then
            select nextval('alertnames_an27id_seq') into anId_;
        elseif (yeartwodigits_ = 28) then
            select nextval('alertnames_an28id_seq') into anId_;
        elseif (yeartwodigits_ = 29) then
            select nextval('alertnames_an29id_seq') into anId_;
        elseif (yeartwodigits_ = 30) then
            select nextval('alertnames_an30id_seq') into anId_;
        elseif (yeartwodigits_ = 31) then
            select nextval('alertnames_an31id_seq') into anId_;
        elseif (yeartwodigits_ = 32) then
            select nextval('alertnames_an32id_seq') into anId_;
        elseif (yeartwodigits_ = 33) then
            select nextval('alertnames_an33id_seq') into anId_;
        elseif (yeartwodigits_ = 34) then
            select nextval('alertnames_an34id_seq') into anId_;
        else
            name_ := 'notsupported';
            return name_;
        end if;

        anId_ := anId_ - 1;               -- Algorithm is for zero-based input index.

        if (anId_ > 8031810175) then
            name_ := 'idoutofrange';
            return name_;
        end if;

        num_ := anId_ + start_;

        res_ := '';

        while (num_ > 0) loop
            num_ := num_ - 1;
            rem_ := num_ % 26;
            let_ := c[rem_ + 1];          -- PosgreSQL arrays are one-based.
            res_ := let_ || res_;         -- Need to reverse characters in string.
            num_ := (num_ - rem_) / 26;
        end loop;

        name_ := 'RAPID' || cast(yeartwodigits_ as char(2)) || cast(res_ as char(7));

        return name_;

    end;

$$ language plpgsql;


-- Load job into Jobs table.
--
create function startJob (
    ppid_           smallint,
    fid_            smallint,
    expid_          integer,
    field_          integer,
    sca_            smallint,
    rid_            integer,
    machine_        smallint,
    slurm_          integer
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
            and sca = sca_
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
                 started)
                values
                (ppid_,
                 expid_,
                 field_,
                 sca_,
                 fid_,
                 rid_,
                 machine_,
                 slurm_,
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
                started = now(),
                ended = null,
                elapsed = null,
                exitcode = null,
                status = 0
            where jid = jid_;

        end if;

        return jid_;

    end;

$$ language plpgsql;


-- Registers information about a completed job in the Jobs table.
--
create function endJob (
    jid_       integer,
    exitcode_  smallint
)
    returns void as $$

    declare

    started_ timestamp;
    ended_   timestamp;
    elapsed_ interval;

    begin

         begin

            select started
            into strict started_
            from Jobs
            where jid = jid_;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in endJob: Jobs record jid=% not found.', jid_;

        end;

        begin

            ended_ = now();
            elapsed_ := ended_ - started_;

            update Jobs
            set ended = ended_,
                elapsed = elapsed_,
                exitcode = exitcode_,
                status = 1
            where jid = jid_;
            exception --------> Required for turning entire block into a transaction.
                when no_data_found then
                    raise exception
                        '*** Error in endJob: Cannot update Jobs record for jid=%', jid_;
        end;

    end;

$$ language plpgsql;


-- Get latest software version.
--
create function getLatestSwVersion (
)
    returns smallint as $$

    declare

        svid_    smallint;

    begin

        begin

            select svid
            into strict svid_
            from SwVersions
            order by svid desc
            limit 1;
            exception
                when no_data_found then
                    raise exception
                        '*** Error in getLatestSwVersion: SwVersions record not found.';

        end;

        return svid_;

    end;

$$ language plpgsql;


-- Insert a new record into or update an existing record in the RefImCatalogs table.
--
create function registerRefImCatalog (
    rfcatid_  integer,
    rfid_     integer,
    ppid_     smallint,
    catType_  smallint,
    sca_      smallint,
    field_    integer,
    hp6_      integer,
    hp9_      integer,
    fid_      smallint,
    filename_ varchar(255),
    checksum_ varchar(32),
    status_   smallint
)
    returns void as $$

    declare

        rfcatid__  integer;
        svid_      smallint;

    begin


        -- Get latest software version number.

        select svid into svid_ from getLatestSwVersion();


        -- Insert or update record, as appropriate.

        select rfcatid
        into rfcatid__
        from RefImCatalogs
        where rfcatid = rfcatid_;

        if not found then


            -- Insert RefImCatalogs record.

            begin

                insert into RefImCatalogs
                (rfcatid, rfid, ppid, catType, field, hp6, hp9, sca, fid,
                 svid, filename, checksum, status, created)
                values
                (rfcatid_, rfid_, ppid_, catType_, field_, hp6_, hp9_, sca_, fid_,
                 svid_, filename_, checksum_, status_, now());
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in registerRefImCatalog: RefImCatalogs record for rfid=%, ppid=%, catType=% not inserted.', rfid_, ppid_, catType_;

            end;

        else


            -- Update RefImCatalogs record.

            update RefImCatalogs
            set rfid = rfid_,
                ppid = ppid_,
                catType = catType_,
                field = field_,
                hp6 = hp6_,
                hp9 = hp9_,
                sca = sca_,
                fid = fid_,
                svid = svid_,
                filename = filename_,
                checksum = checksum_,
                status = status_,
                created = now()
            where rfcatid = rfcatid_;

        end if;

    end;

$$ language plpgsql;


-- Insert a new record into the RefImImages table, if record is not found.
--
create function registerRefImImages (
    rfid_ integer,
    rid_ integer
)
    returns void as $$

    declare

        rfid__  integer;

    begin


        -- Insert or update record, as appropriate.

        select rfid
        into rfid__
        from RefImImages
        where rfid = rfid_
        and rid = rid_;

        if not found then

            begin

                insert into RefImImages
                (rfid, rid)
                values
                (rfid_, rid_);
                exception
                    when no_data_found then
                        raise exception
                            '*** Error in registerRefImImages: RefImImages record for rfid=%, rid=% not inserted.', rfid_, rid_;

            end;

        end if;

    end;

$$ language plpgsql;
