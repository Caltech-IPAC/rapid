import modules.utils.rapid_pipeline_subs as util


if __name__ == '__main__':

    n_clip_sigma = 3.0             # For data clipping.
    n_thresh_sigma = 5             # For threshold computation.

    fwhm = 2.0
    fit_shape = (11, 11)
    aperture_radius = 4


    input_img_filename = "diffimage_masked.fits"
    input_unc_filename = "diffimage_uncert_masked.fits"
    input_psf_filename = "diffpsf.fits"


    # Compute PSF catalog for difference image.  No background subtraction is done.

    phot,finder_results = util.compute_diffimage_psf_catalog(n_clip_sigma,
                                                            n_thresh_sigma,
                                                            fwhm,
                                                            fit_shape,
                                                            aperture_radius,
                                                            input_img_filename,
                                                            input_unc_filename,
                                                            input_psf_filename)



    # Output catalog is an astropy table with the PSF-fitting results.

    # Output columns are documentated at
    # https://photutils.readthedocs.io/en/latest/api/photutils.psf.PSFPhotometry.html

    keys = phot.keys()
    print("phot.keys() =",keys)
    # phot.keys() = ['id', 'group_id', 'group_size', 'local_bkg', 'x_init', 'y_init', 'flux_init', 'x_fit', 'y_fit', 'flux_fit', 'x_err', 'y_err', 'flux_err', 'npixfit', 'qfit', 'cfit', 'flags']


    phot['x_fit'].info.format = '.4f'
    phot['y_fit'].info.format = '.4f'
    phot['flux_fit'].info.format = '.4f'
    phot['x_err'].info.format = '.4f'
    phot['y_err'].info.format = '.4f'
    phot['flux_err'].info.format = '.4f'
    phot['qfit'].info.format = '.4f'
    phot['cfit'].info.format = '.4f'

    print(phot[('id', 'x_fit', 'y_fit', 'flux_fit','x_err', 'y_err', 'flux_err', 'npixfit', 'qfit', 'cfit', 'flags')])

    # Further details about the PSF fitting can be obtained from attributes on the PSFPhotometry instance.
    # For example, the results from the finder instance called during PSF fitting can be accessed using the
    # finder_results attribute (the finder returns an astropy table).

    finder_results_keys = finder_results.keys()

    print("finder_results_keys =",finder_results_keys)

    finder_results['xcentroid'].info.format = '.4f'
    finder_results['ycentroid'].info.format = '.4f'
    finder_results['sharpness'].info.format = '.6f'
    finder_results['peak'].info.format = '.4f'
    finder_results['flux'].info.format = '.4f'
    finder_results['mag'].info.format = '.4f'
    finder_results['daofind_mag'].info.format = '.4f'
    finder_results['roundness1'].info.format = '.6f'
    finder_results['roundness2'].info.format = '.6f'

    print(finder_results)
