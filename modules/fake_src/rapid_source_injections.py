import os
import sys
import numpy as np
from astropy.io import fits, ascii
from astropy.table import Table
import astropy.units as u
from photutils.aperture import CircularAperture, aperture_photometry
from photutils.segmentation import detect_threshold, detect_sources, deblend_sources, SourceCatalog
from photutils.background import Background2D, MedianBackground
import random
import argparse

import romanisim.psf
import romanisim.image
import romanisim.catalog
import galsim
import galsim.wcs
from galsim import roman

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
               (source_table[xcolname] < xmax - edge_buffer) &
               (source_table[ycolname].value > edge_buffer) &
               (source_table[ycolname] < ymax - edge_buffer))

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
    #bkg_brightness = np.zeros(n_inj)
    for i,idx in enumerate(inj_idx):
        xpix[i] = xc[idx] + size_factor*random.uniform(-ds[idx],ds[idx])
        ypix[i] = yc[idx] + size_factor*random.uniform(-ds[idx],ds[idx])
        mag = random.uniform(mag_range[0], mag_range[1]) #uniformily sampled in magnitude
        flux[i] = 10**(-0.4*(mag - zeropoint)) #flux in image counts

        #might break this out into a separate function later
        # Measure background at injection position
        #positions = [(xpix[i], ypix[i])]
        #apertures = CircularAperture(positions, r=aperture_radius)
        #phot_table = aperture_photometry(image_data, apertures)
        #bkg_brightness[i] = phot_table['aperture_sum'].value[0] / area

    return xpix, ypix, flux

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
                         xpos, ypos, fluxes):
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
    """

    nobj = len(xpos)

    #doing them one at a time since PSF is position dependent, might be a better way to do this
    for i in range(nobj):
        obj_pos = (xpos[i], ypos[i])
        image_pos = galsim.PositionD(x=xpos[i], y=ypos[i])
        image_world_pos = image_wcs.toWorld(image_pos)

        # Convert to RA/Dec
        ra = np.array([(image_world_pos.ra.rad * u.rad).to(u.deg).value], dtype=np.float64)
        dec = np.array([(image_world_pos.dec.rad * u.rad).to(u.deg).value], dtype=np.float64)

        # Create injection catalog for romanisim
        type_arr = ['PSF']
        n = np.array([-1.0], dtype=np.float64)
        half_light_radius = np.array([0.0], dtype=np.float64)
        pa = np.array([0.0], dtype=np.float64)
        ba = np.array([1.0], dtype=np.float64)

        inject_table = Table([ra, dec, type_arr, n, half_light_radius, pa, ba, [fluxes[i]]],
                           names=('ra', 'dec', 'type', 'n', 'half_light_radius', 'pa', 'ba', image_filter))
        inject_catalog = romanisim.catalog.table_to_catalog(inject_table, bandpasses=[image_filter])

        # Generate PSF and inject
        # might be a betterway to do this for many objects at once
        #default to galsim psf, but can use stpsf if desired
        psf = romanisim.psf.make_one_psf(image_sca, image_filter, wcs=image_wcs,
                                         stpsf=False, pix=obj_pos, chromatic=False, oversample=4)
        romanisim.image.add_objects_to_image(image, inject_catalog, [obj_pos[0]], [obj_pos[1]],
                                           psf, 1.0, filter_name=image_filter, add_noise=True)

def main():
    #example usage with command line arguments
    parser = argparse.ArgumentParser(description='Inject point sources into simulated Roman images')
    parser.add_argument('input_file', help='Input FITS file to inject sources into')
    parser.add_argument('--sci_ext', type=int, default=1, help='Science data extension number in the FITS file')
    parser.add_argument('--output_dir', help='Output directory for processed file')
    parser.add_argument('--inj_catalog', help='Pre-existing catalog file to use for injections')
    parser.add_argument('--num_injections', type=int, default=10, help='Number of sources to inject. Ignored if inj_catalog is provided.')
    parser.add_argument('--mag_min', type=float, default=22.0, help='Minimum magnitude for random sources. Ignored if inj_catalog is provided.')
    parser.add_argument('--mag_max', type=float, default=27.0, help='Maximum magnitude for random sources. Ignored if inj_catalog is provided.')

    args = parser.parse_args()
    input_file = args.input_file
    sci_ext = args.sci_ext
    output_dir = args.output_dir
    inj_catalog = args.inj_catalog
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
    zpthead = image_hdr['ZPTMAG']
    zptflt = roman.getBandpasses()[image_filter].zeropoint #filter effects zeropoint
    zeropoint = zpthead + zptflt  # Total zeropoint for magnitude to flux (counts) conversion for injections


    # Generate output image name with injected sources
    base_name = os.path.basename(input_file).split('.fits')[0]
    inj_image = os.path.join(output_dir, f'{base_name}_inject.fits')

    # Generate or load injection catalog
    if inj_catalog is None:
        print("Detecting sources and generating injection catalog.")
        catalog_outfile = os.path.join(output_dir, f'{base_name}_inject.txt')

        #Detect sources in the image to generate injection positions around
        source_table = detect_sources_in_image(image_data, detection_nsigma=10, npixels=8)

        xpos, ypos, fluxes = generate_injection_positions_fluxes(source_table, image_size, zeropoint,
                                                                 mag_range=mag_range, num_injections=num_injections)
        save_injection_catalog(xpos, ypos, fluxes, catalog_outfile)
        print(f"Saved injection catalog to {catalog_outfile}")
    else:
        print(f"Loading injection catalog from {inj_catalog}")
        xpos, ypos, fluxes = load_injection_catalog(inj_catalog)

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

