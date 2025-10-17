import os
import configparser

import modules.utils.rapid_pipeline_subs as util


swname = "generate_sexcat.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


print("swname =", swname)
print("swvers =", swvers)

rapid_sw = os.getenv('RAPID_SW')

if rapid_sw is None:

    print("*** Error: Env. var. RAPID_SW not set; quitting...")
    exit(64)

rapid_work = os.getenv('RAPID_WORK')

if rapid_work is None:

    print("*** Error: Env. var. RAPID_WORK not set; quitting...")
    exit(64)

cfg_path = rapid_sw + "/cdf"

print("rapid_sw =",rapid_sw)
print("cfg_path =",cfg_path)


# Read input parameters from .ini file.

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

zogy_dict = config_input['ZOGY']
sextractor_diffimage_dict = config_input['SEXTRACTOR_DIFFIMAGE']


if __name__ == '__main__':


    # Input difference-image FITS file.

    filename_diffimage = zogy_dict['zogy_output_diffimage_file']
    filename_scorrimage = zogy_dict['zogy_output_scorrimage_file']

    filename_diffimage_masked = filename_diffimage.replace(".fits","_masked.fits")
    filename_scorrimage_masked = filename_scorrimage.replace(".fits","_masked.fits")


    # Assume diffimage uncertainty image exists in work directory,
    # which will be the weight image for sextractor_WEIGHT_IMAGE.

    filename_diffimage_unc_masked = filename_diffimage_masked.replace("masked.fits","uncert_masked.fits")

    filename_weight_image = filename_diffimage_unc_masked


    # Override SExtractor parameters in input config file (awsBatchSubmitJobs_launchSingleSciencePipeline.ini).
    # ZTF uses DEBLEND_NTHRESH=4, DEBLEND_MINCONT=0.005, DETECT_MINAREA=1.

    #sextractor_diffimage_dict["sextractor_DEBLEND_NTHRESH".lower()] = str(32)     # Objects: detected 974      / sextracted 864
    sextractor_diffimage_dict["sextractor_DEBLEND_NTHRESH".lower()] = str(4)      # Objects: detected 855      / sextracted 794

    # With DEBLEND_NTHRESH = 32
    #sextractor_diffimage_dict["sextractor_DEBLEND_MINCONT".lower()] = str(0.001)   # Objects: detected 995      / sextracted 883
    #sextractor_diffimage_dict["sextractor_DEBLEND_MINCONT".lower()] = str(0.005)   # Objects: detected 974      / sextracted 864
    #sextractor_diffimage_dict["sextractor_DEBLEND_MINCONT".lower()] = str(0.01)    # Objects: detected 946      / sextracted 851

    # With DEBLEND_NTHRESH = 4
    #sextractor_diffimage_dict["sextractor_DEBLEND_MINCONT".lower()] = str(0.001)   # Objects: detected 863      / sextracted 798
    sextractor_diffimage_dict["sextractor_DEBLEND_MINCONT".lower()] = str(0.005)    # Objects: detected 855      / sextracted 794
    #sextractor_diffimage_dict["sextractor_DEBLEND_MINCONT".lower()] = str(0.01)    # Objects: detected 847      / sextracted 788


    #sextractor_diffimage_dict["sextractor_DETECT_MINAREA".lower()] = str(5)         # Objects: detected 377      / sextracted 351
    #sextractor_diffimage_dict["sextractor_DETECT_MINAREA".lower()] = str(2)         # Objects: detected 875      / sextracted 799
    sextractor_diffimage_dict["sextractor_DETECT_MINAREA".lower()] = str(1)         # Objects: detected 1609     / sextracted 1403


    # What was learned:
    # Decreasing DETECT_MINAREA increases number of detections (strong effect; huge difference between values 1 and 2 [pixels])
    # Increasing DEBLEND_NTHRESH increases number of detections (moderate effect).
    # Decreasing DEBLEND_MINCONT increases number of detections (weak but noticible effect).


    # Compute SExtractor catalog for positive ZOGY masked difference image.
    # Execute SExtractor to first detect candidates on Scorr (S/N) match-filter
    # image, then use to perform aperture phot on difference image to generate
    # raw ascii catalog file.

    sextractor_diffimage_paramsfile = cfg_path + "/rapidSexParamsDiffImage.inp";
    filename_diffimage_sextractor_catalog = filename_diffimage_masked.replace(".fits",".txt")

    sextractor_diffimage_dict["sextractor_detection_image".lower()] = filename_scorrimage_masked
    sextractor_diffimage_dict["sextractor_input_image".lower()] = filename_diffimage_masked
    # Override the config-file parameter sextractor_WEIGHT_TYPE for ZOGY masked-difference-image catalog.
    sextractor_diffimage_dict["sextractor_WEIGHT_TYPE".lower()] = "NONE,MAP_RMS"
    sextractor_diffimage_dict["sextractor_WEIGHT_IMAGE".lower()] = filename_weight_image
    sextractor_diffimage_dict["sextractor_PARAMETERS_NAME".lower()] = sextractor_diffimage_paramsfile
    # Override the config-file parameter sextractor_FILTER for ZOGY masked-difference-image catalog.
    sextractor_diffimage_dict["sextractor_FILTER".lower()] = "N"
    sextractor_diffimage_dict["sextractor_FILTER_NAME".lower()] = cfg_path + "/rapidSexDiffImageFilter.conv"
    sextractor_diffimage_dict["sextractor_STARNNW_NAME".lower()] = cfg_path + "/rapidSexDiffImageStarGalaxyClassifier.nnw"
    sextractor_diffimage_dict["sextractor_CATALOG_NAME".lower()] = filename_diffimage_sextractor_catalog
    sextractor_cmd = util.build_sextractor_command_line_args(sextractor_diffimage_dict)
    exitcode_from_sextractor = util.execute_command(sextractor_cmd)


    # Parse SExtractor catalog for positive ZOGY masked difference image.

    params_to_get_diffimage = ["XWIN_IMAGE","YWIN_IMAGE","FLUX_APER_6"]

    vals_diffimage = util.parse_ascii_text_sextractor_catalog(filename_diffimage_sextractor_catalog,
                                                              sextractor_diffimage_paramsfile,
                                                              params_to_get_diffimage)

    nsexcatsources_diffimage = len(vals_diffimage)

    print("nsexcatsources_diffimage =",nsexcatsources_diffimage)





    # Terminate.

    exit(0)
