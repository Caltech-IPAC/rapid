import modules.utils.rapid_pipeline_subs as util
from astropy.io import ascii
from astropy.table import Table


if __name__ == '__main__':

    n_clip_sigma = 3.0             # For data clipping.
    n_thresh_sigma = 5             # For threshold computation.

    fwhm = 4.0
    fit_shape = (17, 17)
    aperture_radius = 8


    input_img_filename = "diffimage_masked.fits"
    input_unc_filename = "diffimage_uncert_masked.fits"
    input_psf_filename = "diffpsf.fits"
    output_psfcat_residual_filename = "diffimage_masked_psfcat_residual.fits"
    output_psfcat_filename = "diffimage_masked_psfcat.txt"

    print("output_psfcat_filename = ", output_psfcat_filename)


    # Compute PSF catalog for difference image.  No background subtraction is done.

    phot = util.compute_diffimage_psf_catalog(n_clip_sigma,
                                              n_thresh_sigma,
                                              fwhm,
                                              fit_shape,
                                              aperture_radius,
                                              input_img_filename,
                                              input_unc_filename,
                                              input_psf_filename,
                                              output_psfcat_residual_filename)


    # Output catalog is an astropy table with the PSF-fitting results.

    # Output columns are documentated at
    # https://photutils.readthedocs.io/en/latest/api/photutils.psf.PSFPhotometry.html

    keys = phot.keys()
    print("phot.keys() =",keys)
    # phot.keys() = ['id', 'group_id', 'group_size', 'local_bkg', 'x_init', 'y_init', 'flux_init', 'x_fit', 'y_fit', 'flux_fit', 'x_err', 'y_err', 'flux_err', 'npixfit', 'qfit', 'cfit', 'flags']


    phot['x_init'].info.format = '.4f'
    phot['y_init'].info.format = '.4f'
    phot['flux_init'].info.format = '.6f'
    phot['x_fit'].info.format = '.4f'
    phot['y_fit'].info.format = '.4f'
    phot['flux_fit'].info.format = '.6f'
    phot['x_err'].info.format = '.4f'
    phot['y_err'].info.format = '.4f'
    phot['flux_err'].info.format = '.5f'
    phot['qfit'].info.format = '.4f'
    phot['cfit'].info.format = '.4f'

    print(phot[('id', 'x_fit', 'y_fit', 'flux_fit','x_err', 'y_err', 'flux_err', 'npixfit', 'qfit', 'cfit', 'flags')])


    # Write PSF-fit catalog in astropy table to text file.

    ascii.write(phot, output_psfcat_filename, overwrite=True)


    # Terminate.

    exit(0)
