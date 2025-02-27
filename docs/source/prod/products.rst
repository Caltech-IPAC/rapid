RAPID Pipeline Products
####################################################

The products processed on any given date (``<yyyymmdd>`` Pacific Time) will be located in
the RAPID-product S3 bucket with the processing date as a prefix::

    aws s3 ls --recursive s3://rapid-product-files/<yyyymmdd>

For example, this command covers all jobs under processing date ``20250218``::

    aws s3 ls --recursive s3://rapid-product-files/20250218

Here are the available products for just one job (``jid=999``) under that processing date::

    aws s3 ls --recursive s3://rapid-product-files/20250218/jid999

Note that there is one science image differenced per job.

The associated product config output file is::

    aws s3 ls  --recursive s3://rapid-product-files/20250218/product_config_jid999.ini

This is parsed for metadata to load into the RAPID operations database after the processing.

The input and intermediate files for debugging and final products are listed in the table below.
The product filenames are canonical and predictable (they are the same from
one science-image case to the next).

==============================================================  =========================================================================================
Filename                                                        Description
==============================================================  =========================================================================================
Roman_TDS_simple_model_F184_1851_10_lite.fits.gz                Input science image (gzipped)
Roman_TDS_simple_model_F184_1851_10_lite_reformatted.fits       Reformated: Image data are contained in the PRIMARY header and resize to 4089x4089
Roman_TDS_simple_model_F184_1851_10_lite_reformatted_unc.fits   Associated uncertainty image computed via simple model (photon noise only)
Roman_TDS_simple_model_F184_1851_10_lite_reformatted_pv.fits    Reformatted science image with PV distortion
awaicgen_output_mosaic_image.fits                               Reference image
awaicgen_output_mosaic_cov_map.fits                             Coverage map for reference image
awaicgen_output_mosaic_uncert_image.fits                        Uncertainty image for reference image
awaicgen_output_mosaic_refimsexcat.txt                          SourceExtractor catalog from reference image
awaicgen_output_mosaic_image_resampled.fits                     Reference image, resampled to distortion grid of science image and background subtracted
awaicgen_output_mosaic_cov_map_resampled.fits                   RefIm coverage map, resampled to distortion grid of science image
awaicgen_output_mosaic_uncert_image_resampled.fits              RefIm uncertainty image, resampled to distortion grid of science image
bkg_subbed_science_image.fits                                   Science image, background subtracted, direct input to ZOGY
awaicgen_output_mosaic_image_resampled_gainmatched.fits         Gain-matched reference image, background subtracted, directory input to ZOGY
awaicgen_output_mosaic_image_resampled_refgainmatchsexcat.txt   SourceExtractor catalog from reference image for gain-matching purposes
bkg_subbed_science_image_scigainmatchsexcat.txt                 SourceExtractor catalog from science image for gain-matching purposes
diffimage_masked.fits                                           ZOGY output difference image with NaNs in zero-coverage pixels
diffimage_uncert_masked.fits                                    ZOGY output uncertainty difference image with NaNs in zero-coverage pixels
diffpsf.fits                                                    ZOGY output PSF
scorrimage_masked.fits                                          ZOGY output SCORR image with NaNs in zero-coverage pixels
diffimage_masked.txt                                            SourceExtractor catalog from difference image
diffimage_jid999.done                                           Done file indicating product metadata for job ingested into RAPID operations database
==============================================================  =========================================================================================


Public Access
***************

To download a RAPID pipeline product, the
user must construct a URL, knowing the filename in advance, like the following::

    https://rapid-product-files.s3.us-west-2.amazonaws.com/20250218/jid1022/awaicgen_output_mosaic_cov_map.fits

For a listing of the available product files,
download :download:`this text file <rapid-product-files_20250218.txt>`.

A simple Python script can be written to parse the listing and generate ``wget`` or ``curl`` download commands.
