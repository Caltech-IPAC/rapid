import modules.utils.rapid_pipeline_subs as util


#######################################################################################
# Need a function that, given the boresight sky position, find the (ra,dec) of the centers
# and corners of all the Roman WFI SCAs.  As a hack, we make use of the following metadata
#  from the OpenUniverse simulated images.
#
# Roman_TDS_obseq_11_6_23.fits (gives the boresight sky position):
# ra        dec        filter   exptime   date          pa
# 7.60523   -45.6541   R062     161.025   62000.02139   0.0

# Roman_TDS_obseq_11_6_23_radec.fits (corresponding row with SCA center sky positions):
# ra_1                 ra_2                 ra_3                 ra_4                 ra_5                 ra_6                 ra_7                 ra_8                 ra_9                 ra_10                ra_11                ra_12                ra_13                ra_14                ra_15                ra_16                ra_17                ra_18                dec_1                 dec_2                 dec_3                 dec_4                 dec_5                 dec_6                 dec_7                 dec_8                 dec_9                 dec_10                dec_11                dec_12                dec_13                dec_14                dec_15                dec_16                dec_17                dec_18                filter
# 7.70257523225097     7.7021784763940495   7.701810394150581    7.896975260747919    7.896212181342388    7.895393684952536    8.09131856656055     8.090468676252794    8.090776381136955    7.50817107670711     7.508424304595279    7.5087920545391045   7.313914400856016    7.314390664598548    7.315208829157174    7.119427328186973    7.1201343286056265   7.119683618863046    -45.689658660119015   -45.543259134568196   -45.41236073781342    -45.71642870400937    -45.569730686381085   -45.43913367529469    -45.78136918203001    -45.633972867283106   -45.50447180418685    -45.689658902909905   -45.54325925525537    -45.4123608577716     -45.71642979677176    -45.569731048951944   -45.439134036034716   -45.781470393591285   -45.63397347261506    -45.50447180418685    R062
#
# The boresight is at the midpoint of the centers of the SCAs 2 and 11, which for PA=0.0,
# their dec values will be the same.
# select * from l2files where mjdobs >= 62000.02138 and mjdobs <= 62000.02140 and sca=2;
# s3://sims-sn-r062-lite/0/Roman_TDS_simple_model_R062_0_2_lite.fits.gz
# CTYPE1  = 'RA---TAN-SIP'
# CTYPE2  = 'DEC--TAN-SIP'
# CRPIX1  =               2044.0
# CRPIX2  =               2044.0
# CD1_1   = 3.03200165117957E-05
# CD1_2   = -1.0207861394338E-07
# CD2_1   = -1.3704639694748E-07
# CD2_2   = 2.93216996860482E-05
# CUNIT1  = 'deg     '
# CUNIT2  = 'deg     '
# CRVAL1  =   7.7021784763940495
# CRVAL2  =    -45.5432591345682
# select * from l2files where mjdobs >= 62000.02138 and mjdobs <= 62000.02140 and sca=11;
# s3://sims-sn-r062-lite/0/Roman_TDS_simple_model_R062_0_11_lite.fits.gz
# CTYPE1  = 'RA---TAN-SIP'
# CTYPE2  = 'DEC--TAN-SIP'
# CRPIX1  =               2044.0
# CRPIX2  =               2044.0
# CD1_1   = 3.03109772868513E-05
# CD1_2   = 1.20119978244440E-07
# CD2_1   = 1.02651257648787E-07
# CD2_2   = 2.93149522609288E-05
# CUNIT1  = 'deg     '
# CUNIT2  = 'deg     '
# CRVAL1  =    7.508424304595279
# CRVAL2  =   -45.54325925525538
# Physical constraints force gaps between detectors; to minimize such gaps, the detectors
# are placed in two different orientations, so that the separation between rows is slightly
# different; namely, the detectors in the top two rows (positions 1, 4, 7, 10, 13, 16 and
# 2, 5, 8, 11, 14, 17) are placed "upside-down", with the low Y end at the top, while
# those in the third row (positions 3, 6, 9, 12, 15, 18) are placed top-up.
#
# Here is the layout of SCAs:
#
#                   3      12
#           6                       15
#    9              2      11               18
#           5                       14
#    8              1      10               17
#           4                       13
#    7                                      16
#
# Notice for sca=3, the CD matrix signs are different.
# select * from l2files where mjdobs >= 62000.02138 and mjdobs <= 62000.02140 and sca=3;
# s3://sims-sn-r062-lite/0/Roman_TDS_simple_model_R062_0_3_lite.fits.gz
# CTYPE1  = 'RA---TAN-SIP'
# CTYPE2  = 'DEC--TAN-SIP'
# CRPIX1  =               2044.0
# CRPIX2  =               2044.0
# CD1_1   = -3.0075409450827E-05
# CD1_2   = 2.54834746796580E-07
# CD2_1   = 2.74155200131641E-08
# CD2_2   = -2.8607800989939E-05
# CUNIT1  = 'deg     '
# CUNIT2  = 'deg     '
# CRVAL1  =    7.701810394150581
# CRVAL2  =   -45.41236073781343
# select * from l2files where mjdobs >= 62000.02138 and mjdobs <= 62000.02140 and sca=9;
# s3://sims-sn-r062-lite/0/Roman_TDS_simple_model_R062_0_9_lite.fits.gz
# CTYPE1  = 'RA---TAN-SIP'
# CTYPE2  = 'DEC--TAN-SIP'
# CRPIX1  =               2044.0
# CRPIX2  =               2044.0
# CD1_1   = -2.9768856598789E-05
# CD1_2   = 7.50986501808561E-07
# CD2_1   = 4.56778726218148E-07
# CD2_2   = -2.8965250501896E-05
# CUNIT1  = 'deg     '
# CUNIT2  = 'deg     '
# CRVAL1  =    8.090776381136955
# CRVAL2  =   -45.50447180418685
# select * from l2files where mjdobs >= 62000.02138 and mjdobs <= 62000.02140 and sca=6;
# s3://sims-sn-r062-lite/0/Roman_TDS_simple_model_R062_0_6_lite.fits.gz
# CTYPE1  = 'RA---TAN-SIP'
# CTYPE2  = 'DEC--TAN-SIP'
# CRPIX1  =               2044.0
# CRPIX2  =               2044.0
# CD1_1   = -2.9957305360623E-05
# CD1_2   = 4.70709382185402E-07
# CD2_1   = 3.24105777275702E-07
# CD2_2   = -2.8701655960462E-05
# CUNIT1  = 'deg     '
# CUNIT2  = 'deg     '
# CRVAL1  =    7.895393684952536
# CRVAL2  =   -45.43913367529469
# select * from l2files where mjdobs >= 62000.02138 and mjdobs <= 62000.02140 and sca=12;
# s3://sims-sn-r062-lite/0/Roman_TDS_simple_model_R062_0_12_lite.fits.gz
# CTYPE1  = 'RA---TAN-SIP'
# CTYPE2  = 'DEC--TAN-SIP'
# CRPIX1  =               2044.0
# CRPIX2  =               2044.0
# CD1_1   = -3.0057022830523E-05
# CD1_2   = -1.5382946425093E-07
# CD2_1   = -1.0755735063756E-07
# CD2_2   = -2.8591357809216E-05
# CUNIT1  = 'deg     '
# CUNIT2  = 'deg     '
# CRVAL1  =   7.5087920545391045
# CRVAL2  =    -45.4123608577716
# select * from l2files where mjdobs >= 62000.02138 and mjdobs <= 62000.02140 and sca=15;
# s3://sims-sn-r062-lite/0/Roman_TDS_simple_model_R062_0_15_lite.fits.gz
# CTYPE1  = 'RA---TAN-SIP'
# CTYPE2  = 'DEC--TAN-SIP'
# CRPIX1  =               2044.0
# CRPIX2  =               2044.0
# CD1_1   = -2.9959568298769E-05
# CD1_2   = -4.8222298114369E-07
# CD2_1   = -2.9198123555605E-07
# CD2_2   = -2.8702413129918E-05
# CUNIT1  = 'deg     '
# CUNIT2  = 'deg     '
# CRVAL1  =    7.315208829157174
# CRVAL2  =  -45.439134036034716
# select * from l2files where mjdobs >= 62000.02138 and mjdobs <= 62000.02140 and sca=18;
# s3://sims-sn-r062-lite/0/Roman_TDS_simple_model_R062_0_18_lite.fits.gz
# CTYPE1  = 'RA---TAN-SIP'
# CTYPE2  = 'DEC--TAN-SIP'
# CRPIX1  =               2044.0
# CRPIX2  =               2044.0
# CD1_1   = -2.9794674929248E-05
# CD1_2   = -7.1400719913419E-07
# CD2_1   = -4.7733665077020E-07
# CD2_2   = -2.8981937742477E-05
# CUNIT1  = 'deg     '
# CUNIT2  = 'deg     '
# CRVAL1  =    7.119683618863046
# CRVAL2  =   -45.50447180418685
#
# Ignore SCA rotation within FOV for now (because the above examples shows the CD matrices
# have 3 different combinations of signs, depending on the SCA).
#
# The current optical modelling of the WFI suggests that the geometric distortion is small
# and amounts to less than 2% over the field of view. As a result, the pixel scale is nearly
# constant at 110 mas per pixel. This results in an outer dimension of the field of view of
# about 2800 arcsec by 1400 arcsec, including gaps.
#######################################################################################

def compute_sca_center_and_corner_sky_positions_from_boresight_sky_position(ra,dec,pa):

    ra_boresight_ref = 7.60523
    dec_boresight_ref = -45.6541
    pa_boresight_ref = 0.0

    ra0_sca_refs = [7.70257523225097,7.7021784763940495,7.701810394150581,7.896975260747919,7.896212181342388,7.895393684952536,8.09131856656055,8.090468676252794,8.090776381136955,7.50817107670711,7.508424304595279,7.5087920545391045,7.313914400856016,7.314390664598548,7.315208829157174,7.119427328186973,7.1201343286056265,7.119683618863046]
    dec0_sca_refs = [-45.689658660119015,-45.543259134568196,-45.41236073781342,-45.71642870400937,-45.569730686381085,-45.43913367529469,-45.78136918203001,-45.633972867283106,-45.50447180418685,-45.689658902909905,-45.54325925525537,-45.4123608577716,-45.71642979677176,-45.569731048951944,-45.439134036034716,-45.781470393591285,-45.63397347261506,-45.50447180418685]


    # Define a tangent projection for the Roman WFI Field of View.

    pixel_scale = 0.11         # Arcseconds.
    cdelt1 = pixel_scale / 3600.0
    cdelt2 = pixel_scale / 3600.0

    naxis1 = 35000    # 2800 / 0.11 = 25,454.54545455
    naxis2 = 35000    # = n_pixels_x / 2

    crpix1 = naxis1 / 2.0 + 0.5
    crpix2 = naxis2 / 2.0 + 0.5

    print(f"crpix1,crpix2 = {crpix1},{crpix2}")



    # SCA image parameters.

    sca_naxis1 = 4088.0
    sca_naxis2 = 4088.0
    sca_crpix1 = sca_naxis1 / 2.0 + 0.5
    sca_crpix2 = sca_naxis2 / 2.0 + 0.5


    # Compute SCA centers and corners in FOV tangent plane.

    x_centers = []
    y_centers = []

    x_corners1 = []
    y_corners1 = []

    x_corners2 = []
    y_corners2 = []

    x_corners3 = []
    y_corners3 = []

    x_corners4 = []
    y_corners4 = []

    ras0 = []
    decs0 = []

    ras1 = []
    decs1 = []

    ras2 = []
    decs2 = []

    ras3 = []
    decs3 = []

    ras4 = []
    decs4 = []

    i = 0
    for ra0_sca_ref,dec0_sca_ref in zip(ra0_sca_refs,dec0_sca_refs):

        i += 1

        crval1 = ra_boresight_ref
        crval2 = dec_boresight_ref
        crota2 = pa_boresight_ref

        x0,y0 = util.rev_tan_proj(ra0_sca_ref,dec0_sca_ref,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)

        print(f"i,x,y = {i},{x0},{y0}")

        x_centers.append(x0)
        y_centers.append(y0)

        x1 = x0 - sca_crpix1 + 1
        y1 = y0 - sca_crpix2 + 1

        x2 = x0 + sca_crpix1 - 1
        y2 = y0 - sca_crpix2 + 1

        x3 = x0 + sca_crpix1 - 1
        y3 = y0 + sca_crpix2 - 1

        x4 = x0 - sca_crpix1 + 1
        y4 = y0 + sca_crpix2 - 1

        x_corners1.append(x1)
        y_corners1.append(y1)

        x_corners2.append(x2)
        y_corners2.append(y2)

        x_corners3.append(x3)
        y_corners3.append(y3)

        x_corners4.append(x4)
        y_corners4.append(y4)


        crval1 = ra
        crval2 = dec
        crota2 = pa

        ra0,dec0 = util.tan_proj(x0,y0,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)
        ra1,dec1 = util.tan_proj(x1,y1,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)
        ra2,dec2 = util.tan_proj(x2,y2,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)
        ra3,dec3 = util.tan_proj(x3,y3,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)
        ra4,dec4 = util.tan_proj(x4,y4,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)

        print(f"i,ra0,dec0 = {i},{ra0},{dec0}")

        ras0.append(ra0)
        decs0.append(dec0)

        ras1.append(ra1)
        decs1.append(dec1)

        ras2.append(ra2)
        decs2.append(dec2)

        ras3.append(ra3)
        decs3.append(dec3)

        ras4.append(ra4)
        decs4.append(dec4)


    return x_centers,y_centers,\
           naxis1,naxis2,\
           x_corners1,y_corners1,\
           x_corners2,y_corners2,\
           x_corners3,y_corners3,\
           x_corners4,y_corners4,\
           ras0,decs0,\
           ras1,decs1,\
           ras2,decs2,\
           ras3,decs3,\
           ras4,decs4
