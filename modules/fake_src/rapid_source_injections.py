import os
import numpy as np
import random
import argparse
import json


from astropy.io import fits, ascii
from astropy.table import Table
import astropy.units as u

from photutils.segmentation import detect_threshold, detect_sources, deblend_sources, SourceCatalog
from photutils.background import Background2D, MedianBackground

import romanisim.psf
import romanisim.image
import romanisim.catalog
import romanisim.bandpass
import galsim
import galsim.wcs
from galsim import roman

from injectionLightCurveModels import SinusoidalLightCurve, GaussianLightCurve

def detect_sources_in_image(image_data, detection_nsigma=10, npixels=8, bkg_box_size=512, bkg_filter_size=3,
                   segm_nlevels=8, segm_contrast=0.002):
    """
    Detect sources in the image and return a source catalog.

    Parameters:
    -----------
    image_data : np.array
        The image data array
    detection_nsigma : float
        Source detection threshold in sigma
    npixels : int
        Minimum number of connected pixels for source detection
    bkg_box_size : int
        Size of the background estimation box in pixels
    bkg_filter_size : int
        Size of the filter for background estimation in pixels
    segm_nlevels : int
        Number of deblending levels for segmentation
    segm_contrast : float
        Contrast threshold for deblending

    Returns:
    --------
    astropy.table.Table : Detected source catalog
    """

    # Subtract background
    bkg_estimator = MedianBackground()
    bkg = Background2D(image_data, bkg_box_size, filter_size=bkg_filter_size, bkg_estimator=bkg_estimator)
    image_data_bkgsub = image_data - bkg.background

    # Detect sources
    threshold = detect_threshold(image_data_bkgsub, nsigma=detection_nsigma)
    segm = detect_sources(image_data_bkgsub, threshold, npixels=npixels)
    segm_deblend = deblend_sources(image_data_bkgsub, segm, npixels=npixels,
                                  nlevels=segm_nlevels, contrast=segm_contrast)

    # Get source catalog
    source_cat = SourceCatalog(image_data_bkgsub, segm_deblend)
    source_table = source_cat.to_table()

    return source_table

def generate_injection_positions_fluxes(source_table, image_size, zeropoint, mag_range=(22.0, 27.0),
                                        size_factor=1.5, edge_buffer=10.0, num_injections=10,
                                        xcolname='xcentroid', ycolname='ycentroid', sizecolname='semimajor_sigma'):
    """
    Generate randomized injection positions and fluxes based on detected sources.

    Parameters:
    -----------
    source_catalog : astropy.table.Table
        Catalog of sources (e.g., galaxies) to do injections around, must have columns for x,y positions and sizes
    image_size : tuple
        Size of the image (y, x) in pixels
    zeropoint : float
        Zeropoint for converting magnitudes to image fluxes for injections
    mag_range : tuple
        Range of magnitudes to sample uniformly for injected sources
    size_factor : float
        Scale factor for randomizing injection positions relative to source size
    edge_buffer : float
        Minimum distance from image edges to for injection poisitions
    num_injections : int
        Number of sources to inject
    xcolname, ycolname, sizecolname : str
        Column names in the source table for x, y positions and size (e.g., semimajor axis)

    Returns:
    --------
    tuple : (xpix, ypix, flux)
        Arrays of x and y pixel positions and fluxes for the point sources to inject
    """

    # Aperture for background measurement
    #aperture_radius = 2.0
    #area = np.pi * aperture_radius**2

    # Filter sources away from edges
    ymax, xmax = image_size[0], image_size[1]
    goodidx = ((source_table[xcolname].value > edge_buffer) &
               (source_table[xcolname].value < xmax - edge_buffer) &
               (source_table[ycolname].value > edge_buffer) &
               (source_table[ycolname].value < ymax - edge_buffer))

    ds = source_table[sizecolname][goodidx].value
    xc = source_table[xcolname][goodidx].value
    yc = source_table[ycolname][goodidx].value

    # Generate random positions and fluxes
    n_inj = num_injections
    n_obj = len(ds)
    inj_idx = random.sample(range(0,n_obj),n_inj) #choose n_inj random objects from the catalog to do injections

    #initialize arrays for injected positions and fluxes (in image counts)
    flux = np.zeros(n_inj)
    xpix = np.zeros(n_inj)
    ypix = np.zeros(n_inj)
    for i,idx in enumerate(inj_idx):
        xpix[i] = xc[idx] + size_factor*random.uniform(-ds[idx],ds[idx])
        ypix[i] = yc[idx] + size_factor*random.uniform(-ds[idx],ds[idx])
        mag = random.uniform(mag_range[0], mag_range[1]) #uniformily sampled in magnitude
        flux[i] = 10**(-0.4*(mag - zeropoint)) #flux in image counts

    return xpix, ypix, flux

def generate_injection_positions_fluxes_from_catalogs_by_field(input_catalogs_filename, image_mjdobs, image_size, image_wcs, zeropoint):
    """
    Generate and light curve fluxes and x,y positions for injections based on input catalogs for each field (sky tile).
    This is meant to be used with the pre-generated injection catalogs by field, which have parameters for light curve models for each source.

    Parameters:
    -----------
    input_catalogs_filename : str
        text file with the list of input catalogs for each field to use for injections
    image_mjdobs : float
        MJD of the observation, used for evaluating light curves at the time of the image
    image_size : tuple
        Size of the image (y, x) in pixels
    astropy_wcs : astropy.wcs.WCS
        astropy WCS object for the image, used for converting RA/Dec to x,y positions for injections
    zeropoint : float
        Zeropoint for converting magnitudes to image fluxes for injections

    Returns:
    --------
    tuple : (xpix, ypix, flux)
        Arrays of x and y pixel positions and fluxes for the point sources to inject
    """

    with open(input_catalogs_filename, 'r') as file_handle:
        input_catalogs = file_handle.readlines()

    xpix = []
    ypix = []
    flux = []
    for input_catalog in input_catalogs:
        input_catalog = input_catalog.strip() #remove whitespace and newline characters
        with open(input_catalog, 'r') as f:
            catalog_sources_dict = json.load(f)
        catalog_sources = np.array(list(catalog_sources_dict.values()))

        #pull out the 'ra' and 'dec' entries for each source in catalog, which is a nested dictionary, for each source
        ra_coords = np.array([source['ra'] for source in catalog_sources])
        dec_coords = np.array([source['dec'] for source in catalog_sources])

        #convert the ra, dec to x,y positions using the image wcs
        #go a bit past the edges of the image to allow for sources that are just outside the image but could still have flux in the image due to the PSF
        #not sure if romanisim will handle this, need to test

        xposition, yposition = image_wcs.toImage(ra_coords, dec_coords, units='deg')
        goodidx = (xposition >= -50.0) & (xposition < image_size[1] + 50.0) & (yposition >= -50.0) & (yposition < image_size[0] + 50.0)

        for i,source in enumerate(catalog_sources[goodidx]):
            if source['type'] == 'sinusoidal':
                #do it in mag, though diff flux light curves will not be sinusoidal.
                mean_mag = source['parameters']['magnitude']
                mag_amplitude = source['parameters']['amplitude']
                period = source['parameters']['period'] #days
                phase = source['parameters']['phase'] #between 0 and 1

                #evaluate the sinusoidal light curve at the time of the image to get the flux for injection
                source_mag = SinusoidalLightCurve(image_mjdobs, mean_mag, mag_amplitude, period, phase)
                source_flux = 10**(-0.4*(source_mag - zeropoint)) #convert magnitude to flux for injection

            elif source['type'] == 'gaussian':
                peak_time = source['parameters']['peak_time'] #MJD
                sigma = source['parameters']['sigma'] #days, width of the gaussian
                static_flux = 10**(-0.4*(source['parameters']['magnitude'] - zeropoint))
                peak_amplitude = static_flux*(10**(0.4*source['parameters']['peak_amplitude']) - 1.0) #convert amplitude from magnitude to flux units, where amplitude is the difference between the baseline magnitude and is the amplitude of the outburst in delta mag.

                #evaluate the gaussian light curve at the time of the image to get the flux for injection
                source_flux = GaussianLightCurve(image_mjdobs, peak_time, peak_amplitude, sigma, static_flux)

            xpix.append(xposition[goodidx][i])
            ypix.append(yposition[goodidx][i])
            flux.append(source_flux)

    return np.array(xpix), np.array(ypix), np.array(flux)

def save_injection_catalog(xpos, ypos, fluxes, catalog_outfile):
    """Save injection catalog to file."""
    with open(catalog_outfile, 'w') as f:
        f.write('xpix    ypix     flux\n')
        for i in range(len(xpos)):
            f.write(f'{xpos[i]} {ypos[i]} {fluxes[i]}\n')

def load_injection_catalog(infile):
    """Load injection positions and fluxes from a file."""
    xpos, ypos, fluxes = [], [], []

    inject_table = ascii.read(infile)

    xpos = inject_table['xpix'].data
    ypos = inject_table['ypix'].data
    fluxes = inject_table['flux'].data

    return xpos, ypos, fluxes

def inject_point_sources(image, image_wcs, image_sca, image_filter,
                         xpos, ypos, fluxes, sed_temperature=None):
    """
    Inject point sources into the image using romanisim.

    Parameters:
    -----------
    image : galsim.Image
        The image object to inject sources into
    image_wcs : galsim.WCS
        World coordinate system
    image_sca : int
        SCA number (Sensor Chip Assembly for Roman WFI)
    image_filter : str
        Roman filter name
    xpos, ypos, fluxes : arrays
        Position and flux arrays for injection
    sed_temperature : float, optional
        Blackbody temperature in Kelvin for source SEDs. If None, uses a flat SED.
    """

    nobj = len(xpos)

    # Get galsim bandpass object for chromatic rendering
    galsim_filter = romanisim.bandpass.roman2galsim_bandpass[image_filter]
    galsim_bp = roman.getBandpasses(AB_zeropoint=True)[galsim_filter]

    if sed_temperature is not None:
        print(f"  Using blackbody SED at T={sed_temperature:.0f} K")
        waves = np.linspace(300, 2500, 500)
        h, c, k = 6.626e-34, 3e8, 1.38e-23
        wave_m = waves * 1e-9
        flambda = (2*h*c**2 / wave_m**5) / (np.exp(h*c / (k*wave_m*sed_temperature)) - 1)
        source_sed = galsim.SED(galsim.LookupTable(waves, flambda, interpolant='linear'),
                                wave_type='nm', flux_type='flambda').withFlux(1.0, galsim_bp)
    else:
        source_sed = galsim.SED(lambda wave: 1.0, wave_type='nm', flux_type='fphotons').withFlux(1.0, galsim_bp)

    # Build variable PSF once from SCA corners to avoid recomputing the full optical model per source
    # pupil_bin=4 and n_waves=5 match the OpenUniverse simulation settings
    print(f"  [1/4] Building chromatic variable PSF (SCA {image_sca}, {image_filter})...")
    variable_psf = romanisim.psf.make_psf(image_sca, image_filter, wcs=image_wcs,
                                          psftype='galsim', chromatic=True, variable=True,
                                          n_waves=5, pupil_bin=8)

    # Build the full chromatic catalog, then inject all sources in one call
    print(f"  [2/4] Building injection catalog ({nobj} sources)...")
    inject_objlist = []
    for i in range(nobj):
        image_pos = galsim.PositionD(x=xpos[i], y=ypos[i])
        image_world_pos = image_wcs.toWorld(image_pos)
        sky_pos = galsim.CelestialCoord(image_world_pos.ra, image_world_pos.dec)
        inject_objlist.append(romanisim.catalog.CatalogObject(sky_pos, galsim.DeltaFunction() * source_sed, flux=None))

    print(f"  [3/4] Injecting sources into image...")
    data_before = image.array.copy()
    flux_list = list(fluxes)
    romanisim.image.add_objects_to_image(image, inject_objlist, list(xpos), list(ypos),
                                         variable_psf, flux_list, bandpass=galsim_bp, add_noise=True,
                                         fastpointsources=False)

    # Apply charge diffusion to only the injected flux, matching OpenUniverse's ChargeDiff
    # photon op (OpenUniverse2024 paper eq. 7, section 5.2). The MTF is fit as a sech
    # approximated by a sum of three 2D Gaussians (sigma=0.3279 px):
    #   c_i = {0.4522, 0.8050, 1.4329}, w_i = {0.17519, 0.53146, 0.29335}
    # Effective RMS sigma ~0.325 pixels. Blurring the difference avoids re-blurring
    # existing sources that already have charge diffusion from the original simulation.
    print(f"  [4/4] Applying charge diffusion and saturation clip...")
    from scipy.ndimage import gaussian_filter
    _cd_sigma = 0.3279
    _cd_ci = [0.4522, 0.8050, 1.4329]
    _cd_wi = [0.17519, 0.53146, 0.29335]
    injected = image.array - data_before
    blurred = sum(w * gaussian_filter(injected, sigma=c * _cd_sigma)
                  for c, w in zip(_cd_ci, _cd_wi))
    image.array[:] = data_before + blurred

    # Clip at full-well saturation matching the OpenUniverse simple_model simulation
    np.clip(image.array, None, 100000.0, out=image.array)


def main():
    #example usage with command line arguments
    parser = argparse.ArgumentParser(description='Inject point sources into simulated Roman images')
    parser.add_argument('input_file', help='Input FITS file to inject sources into')
    parser.add_argument('--sci_ext', type=int, default=1, help='Science data extension number in the FITS file')
    parser.add_argument('--output_dir', help='Output directory for processed file')
    parser.add_argument('--injections_by_field_flag', action='store_true', help='Whether to use precomputed injection catalogs based field (sky tiles)')
    parser.add_argument('--field_catalogs_input_filename', default='Input_catalogs_for_fake_source_injections_by_field.csv', help='csv file with the input catalogs for each field to use for injections if injections_by_field_flag is set')
    parser.add_argument('--injections_by_image_flag', action='store_true', help='Whether to inject fake sources specific to the image.')
    parser.add_argument('--num_injections', type=int, default=10, help='Number of sources to inject by image.')# Ignored if inj_catalog is provided.')
    parser.add_argument('--mag_min', type=float, default=22.0, help='Minimum magnitude for random sources by image.')# Ignored if inj_catalog is provided.')
    parser.add_argument('--mag_max', type=float, default=27.0, help='Maximum magnitude for random sources by image.')# Ignored if inj_catalog is provided.')

    args = parser.parse_args()
    input_file = args.input_file
    sci_ext = args.sci_ext
    output_dir = args.output_dir
    injections_by_field_flag = args.injections_by_field_flag
    field_catalogs_input_filename = args.field_catalogs_input_filename
    injections_by_image_flag = args.injections_by_image_flag
    num_injections = args.num_injections
    mag_range = (args.mag_min, args.mag_max)

    # Create output directory if it doesn't exist
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(input_file))
        print(f"Using input file directory as output: {output_dir}")
    os.makedirs(output_dir, exist_ok=True)

    #load the input file
    with fits.open(args.input_file) as image_hdu:
        image_data = image_hdu[sci_ext].data
        image_hdr = image_hdu[sci_ext].header
        image_wcs = galsim.GSFitsWCS(header=image_hdr)

    image = galsim.Image(image_data, wcs=image_wcs)
    image_size = image_data.shape
    image_filter = image_hdr['FILTER']
    image_sca = image_hdr['SCA_NUM']
    image_mjdobs = image_hdr['MJD-OBS']
    zpthead = image_hdr['ZPTMAG']
    zptflt = roman.getBandpasses()[image_filter].zeropoint #filter effects zeropoint
    zeropoint = zpthead + zptflt  # Total zeropoint for magnitude to flux (counts) conversion for injections

    # Generate output image name with injected sources
    base_name = os.path.basename(input_file).split('.fits')[0]
    inj_image = os.path.join(output_dir, f'{base_name}_inject.fits')
    catalog_outfile = os.path.join(output_dir, f'{base_name}_inject.txt')

    if injections_by_field_flag:
        print(f"Computing source fluxes at {image_mjdobs:0.3f} generating injection catalog.")
        xpos_field, ypos_field, fluxes_field = generate_injection_positions_fluxes_from_catalogs_by_field(field_catalogs_input_filename, image_mjdobs, image_size, image_wcs, zeropoint)
    else:
        xpos_field, ypos_field, fluxes_field = np.array([]), np.array([]), np.array([]) #empty arrays if not using field catalogs for injections

    # Generate or load injection catalog
    if injections_by_image_flag:
        print("Detecting sources and generating randomized injection catalog for image.")

        #Detect sources in the image to generate injection positions around
        source_table = detect_sources_in_image(image_data, detection_nsigma=10, npixels=8)

        xpos_image, ypos_image, fluxes_image = generate_injection_positions_fluxes(source_table, image_size, zeropoint,
                                                                                   mag_range=mag_range, num_injections=num_injections)
    else:
        xpos_image, ypos_image, fluxes_image = np.array([]), np.array([]), np.array([]) #empty arrays if not using image specific injections

    xpos = np.concatenate((xpos_field, xpos_image))
    ypos = np.concatenate((ypos_field, ypos_image))
    fluxes = np.concatenate((fluxes_field, fluxes_image))
    save_injection_catalog(xpos, ypos, fluxes, catalog_outfile)
    print(f"Saved injection catalog to {catalog_outfile}")

    # Inject point sources into the image
    print(f"Injecting {len(xpos)} point sources into {inj_image}")
    inject_point_sources(image, image_wcs, image_sca, image_filter,
                         xpos, ypos, fluxes)

    #will have same header and extension structure as the input file, only the sci data will be modified with extensions
    with fits.open(input_file) as outimg_hdu:
        # Update the science data with the injected image data
        outimg_hdu[sci_ext].data = image.array
        outimg_hdu.writeto(inj_image, overwrite=True) #write it out to the injected image filename

if __name__ == "__main__":
    main()
