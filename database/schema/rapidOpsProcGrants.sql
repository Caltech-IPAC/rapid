--------------------------------------------------------------------------------------------------------------------------
-- rapidOpsProcGrants.sql
--
-- Russ Laher (laher@ipac.caltech.edu)
--
-- 16 April 2024
--------------------------------------------------------------------------------------------------------------------------


grant EXECUTE on FUNCTION addExposure (
    dateobs_             timestamp,
    mjdobs_              double precision,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    filter_              character varying(16),
    exptime_             real,
    infobits_            integer,
    status_              smallint
) to rapidporole;


grant EXECUTE on FUNCTION addL2File (
    expid_                integer,
    sca_                  smallint,
    field_                integer,
    hp6_                 integer,
    hp9_                 integer,
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
) to rapidporole;


grant EXECUTE on FUNCTION updateL2File (
    rid_      integer,
    filename_ varchar(255),
    checkSum_ varchar(32),
    status_   smallint,
    version_  smallint
) to rapidporole;


grant EXECUTE on FUNCTION registerL2FileMeta (
    rid_                 integer,
    fid_                 smallint,
    sca_                 smallint,
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
) to rapidporole;


grant EXECUTE on FUNCTION addDiffImage (
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
) to rapidporole;


grant EXECUTE on FUNCTION updateDiffImage (
    pid_         integer,
    filename_    varchar(255),
    checkSum_    varchar(32),
    status_      smallint,
    version_     smallint
) to rapidporole;


grant EXECUTE on FUNCTION addRefImage (
    field_                integer,
    hp6_                  integer,
    hp9_                  integer,
    fid_                  smallint,
    ppid_                 smallint,
    infobits_             integer,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint
) to rapidporole;


grant EXECUTE on FUNCTION updateRefImage (
    rfid_        integer,
    filename_    varchar(255),
    checkSum_    varchar(32),
    status_      smallint,
    version_     smallint
) to rapidporole;


grant EXECUTE on FUNCTION addAlertName (
    name_   char(12),
    sca_    smallint,
    field_  integer,
    hp6_    integer,
    hp9_    integer,
    ra_     double precision,
    dec_    double precision,
    jd_     double precision,
    candId_ bigint
) to rapidporole;


grant EXECUTE on FUNCTION computeAlertName (
    yeartwodigits_ smallint
) to rapidporole;


grant EXECUTE on FUNCTION startJob (
    ppid_           smallint,
    fid_            smallint,
    expid_          integer,
    field_          integer,
    sca_            smallint,
    rid_            integer,
    machine_        smallint,
    slurm_          integer
) to rapidporole;


grant EXECUTE on FUNCTION endJob (
    jid_       integer,
    exitcode_  smallint,
    awsbatchjobid varchar(64)
) to rapidporole;


grant EXECUTE on FUNCTION getLatestSwVersion (
) to rapidporole;


grant EXECUTE on FUNCTION registerRefImCatalog (
    rfid_     integer,
    ppid_     smallint,
    catType_  smallint,
    field_    integer,
    hp6_      integer,
    hp9_      integer,
    fid_      smallint,
    filename_ varchar(255),
    checksum_ varchar(32),
    status_   smallint
) to rapidporole;


grant EXECUTE on FUNCTION registerRefImImage (
    rfid_ integer,
    rid_ integer
) to rapidporole;


grant EXECUTE on FUNCTION addSOCProc (
    datedeliv_            timestamp,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint
) to rapidporole;


grant EXECUTE on FUNCTION addPSF (
    fid_                  smallint,
    sca_                  smallint,
    filename_             character varying(255),
    checksum_             character varying(32),
    status_               smallint

) to rapidporole;


grant EXECUTE on FUNCTION updatePSF (
    psfid_      integer,
    filename_ varchar(255),
    checkSum_ varchar(32),
    status_   smallint,
    version_  smallint
) to rapidporole;


grant EXECUTE on FUNCTION registerDiffImMeta (
    pid_                 integer,
    fid_                 smallint,
    sca_                 smallint,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    nsexcatsources_      integer,
    scalefacref_         real
) to rapidporole;


grant EXECUTE on FUNCTION registerRefImMeta (
    rfid_                integer,
    fid_                 smallint,
    sca_                 smallint,
    field_               integer,
    hp6_                 integer,
    hp9_                 integer,
    nframes_             smallint,
    npixsat_             integer,
    npixnan_             integer,
    clmean_              real,
    clstddev_            real,
    clnoutliers          integer,
    gmedian_             real,
    datascale_           real,
    gmin_                real,
    gmax_                real,
    medncov_             real,
    medpixunc_           real,
    fwhmmedpix_          real,
    fwhmminpix_          real,
    fwhmmaxpix_          real,
    nsexcatsources_      integer
) to rapidporole;
