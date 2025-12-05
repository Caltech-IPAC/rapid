import os
import configparser
from astropy.io import ascii
from astropy.table import Table


swname = "generate_psfcat.py"
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

bkgest_dict = config_input['BKGEST']
zogy_dict = config_input['ZOGY']
psfcat_diffimage_dict = config_input['PSFCAT_DIFFIMAGE']



import modules.utils.rapid_pipeline_subs as util


if __name__ == '__main__':

    output_diffimage_file_infobits = 0

    n_clip_sigma = float(psfcat_diffimage_dict["n_clip_sigma"])
    n_thresh_sigma = float(psfcat_diffimage_dict["n_thresh_sigma"])

    fwhm = float(psfcat_diffimage_dict["fwhm"])


    # Override values.

    fwhm = os.getenv('FWHM')
    if fwhm is None:
        fwhm = 2.0
    sharplo = os.getenv('SHARPLO')
    if sharplo is None:
        sharplo = -1.0
    sharphi = os.getenv('SHARPHI')
    if sharphi is None:
        sharphi = 10.0
    roundlo = os.getenv('ROUNDLO')
    if roundlo is None:
        roundlo = -2.0
    roundhi = os.getenv('ROUNDHI')
    if roundhi is None:
        roundhi = 2.0
    min_separation = os.getenv('MINSEP')
    if min_separation is None:
        min_separation = 1.0          # pixels


    # Tried widening the allowed range of [sharplo,sharphi] from [0.2,1.0] to [-1.0,10.0], and
    # this increased the number of detections from 1707 to 1936 (for fwhm = 1.0), and to 1886 (for fwhm = 1.4).


    # What was learned:
    # fwhm=1.4 gives the highest number PhotUtils extracted sources matching injected fake sources
    # (77 out of 100 using 1.5-pixel match radius, and [sharphi,sharplo]=-1.0,10.0).


    fit_shape_str = psfcat_diffimage_dict["fit_shape"]
    fit_shape = tuple(int(x) for x in fit_shape_str.replace("(","").replace(")","").replace(" ", "").split(','))
    aperture_radius = float(psfcat_diffimage_dict["aperture_radius"])

    filename_bkg_subbed_science_image = bkgest_dict["filename_bkg_subbed_science_image"]

    filename_diffimage = zogy_dict['zogy_output_diffimage_file']
    filename_diffpsf = zogy_dict['zogy_output_diffpsf_file']
    filename_diffimage_masked = filename_diffimage.replace(".fits","_masked.fits")
    filename_diffimage_unc_masked = filename_diffimage_masked.replace("masked.fits","uncert_masked.fits")

    input_img_filename = filename_diffimage_masked
    input_unc_filename = filename_diffimage_unc_masked
    input_psf_filename = filename_diffpsf

    output_psfcat_filename = psfcat_diffimage_dict["output_zogy_psfcat_filename"]
    output_psfcat_finder_filename = psfcat_diffimage_dict["output_zogy_psfcat_finder_filename"]
    output_psfcat_residual_filename = psfcat_diffimage_dict["output_zogy_psfcat_residual_filename"]


    print("input_img_filename = ", input_img_filename)
    print("input_unc_filename = ", input_unc_filename)
    print("input_psf_filename = ", input_psf_filename)
    print("output_psfcat_filename = ", output_psfcat_filename)


    # Compute PSF catalog for difference image.  No background subtraction is done.

    psfcat_flag,phot,psfphot = util.compute_diffimage_psf_catalog(n_clip_sigma,
                                                                  n_thresh_sigma,
                                                                  fwhm,
                                                                  fit_shape,
                                                                  aperture_radius,
                                                                  input_img_filename,
                                                                  input_unc_filename,
                                                                  input_psf_filename,
                                                                  output_psfcat_residual_filename,
                                                                  sharplo=sharplo,
                                                                  sharphi=sharphi,
                                                                  roundlo=roundlo,
                                                                  roundhi=roundhi,
                                                                  min_separation=min_separation)


    print("psfcat_flag =",psfcat_flag)

    if not psfcat_flag:

        output_diffimage_file_infobits |= 2**0

    else:

        # Output psf-fit catalog is an PSFPhotometry astropy table with the PSF-fitting results
        # merged with the DAOStarFinder astropy table.
        # Output columns are documentated at
        # https://photutils.readthedocs.io/en/latest/api/photutils.psf.PSFPhotometry.html
        # https://photutils.readthedocs.io/en/stable/api/photutils.detection.DAOStarFinder.html

        keys = phot.keys()
        print("phot.keys() =",keys)
        # phot.keys() = ['id', 'group_id', 'group_size', 'local_bkg', 'x_init', 'y_init', 'flux_init', 'x_fit', 'y_fit', 'flux_fit', 'x_err', 'y_err', 'flux_err', 'npixfit', 'qfit', 'cfit', 'flags']


        try:
            phot['x_init'].info.format = '.4f'
            phot['y_init'].info.format = '.4f'
            phot['flux_init'].info.format = '.6f'
            phot['flux_fit'].info.format = '.6f'
            phot['x_err'].info.format = '.4f'
            phot['y_err'].info.format = '.4f'
            phot['flux_err'].info.format = '.5f'
            phot['qfit'].info.format = '.4f'
            phot['cfit'].info.format = '.4f'

            print(phot[('id', 'x_fit', 'y_fit', 'flux_fit','x_err', 'y_err', 'flux_err', 'npixfit', 'qfit', 'cfit', 'flags')])


            # Compute sky coordinates for given pixel coordinates.

            ra,dec = util.computeSkyCoordsFromPixelCoords(filename_bkg_subbed_science_image,
                                                          list(phot['x_fit']),
                                                          list(phot['y_fit']))

            phot['x_fit'].info.format = '.4f'
            phot['y_fit'].info.format = '.4f'
            phot.add_column(ra, name='ra')
            phot.add_column(dec, name='dec')


            # Write PSF-fit photometry catalog in astropy table to text file.

            print("output_psfcat_filename = ", output_psfcat_filename)

            ascii.write(phot, output_psfcat_filename, overwrite=True)


            # Write PSF-fit finder catalog in astropy table to text file.

            print("output_psfcat_finder_filename = ", output_psfcat_finder_filename)

            ascii.write(psfphot.finder_results, output_psfcat_finder_filename, overwrite=True)

        except Exception as e:
            print(f"PSF-fit PSFPhotometry and DAOStarFinder catalogs: An unexpected error occurred: {e}")

    print("output_diffimage_file_infobits = ",output_diffimage_file_infobits)


    # Terminate.

    exit(0)
