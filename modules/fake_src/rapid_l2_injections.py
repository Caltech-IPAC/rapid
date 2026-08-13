import argparse
import importlib
import json
import numpy as np

import asdf
import crds
import roman_datamodels as rdm
import roman_datamodels.datamodels as rdm_datamodels

from astropy.table import Table
from romancal.assign_wcs import AssignWcsStep

import romanisim.models.parameters as rsim_parameters
import romanisim.psf
import romanisim.wcs
from romanisim.image import inject_sources_into_l2



class GriddedEPSF:
    """Spatially variable ePSF backed by the full CRDS GriddedPSFModel.

    Implements the at_position(x, y) interface that romanisim's
    add_objects_to_image expects, using the full focal-plane grid rather
    than the 4-corner bilinear approximation in romanisim's VariablePSF.
    """

    def __init__(self, gridded_model, pixelscale, wcs=None):
        self.gridded_model = gridded_model
        self.pixelscale = pixelscale
        self.wcs = wcs
        # Keeps the fastpointsources path disabled in add_objects_to_image
        self.psfinterpolators = None

    def at_position(self, x, y):
        stamp = romanisim.psf.psf_from_grid(self.gridded_model, x, y)
        return romanisim.psf.psfstamp_to_galsimimage(
            stamp, self.pixelscale, wcs=self.wcs, pix=(x, y))


def _open_l2_asdf(asdf_path):
    """Open an L2 ASDF file from a local path or S3 URI.

    For local files the ASDF file handle stays open (backed by the model)
    so block data remains accessible throughout processing. For S3,
    copy_arrays=True forces all arrays into numpy memory before the
    connection closes.
    """
    if asdf_path.startswith('s3://'):
        import s3fs
        fs = s3fs.S3FileSystem(anon=True)
        with fs.open(asdf_path, 'rb') as f:
            with asdf.open(f, copy_arrays=True) as af:
                dm = rdm.open(af).copy()
    else:
        dm = rdm_datamodels.open(asdf_path)
    return dm


def _fix_wcs(dm):
    """Rerun AssignWcsStep to restore the nominal WCS.

    Required for current SOC GBTDS simulations where the Gaia alignment
    step finds an incorrect solution (no real Gaia stars in the sim).
    Not needed for real Roman data.
    """
    AssignWcsStep.call(dm)
    return dm


def _fetch_crds_ref(dm, reftype):
    """Fetch a CRDS reference file for the given reftype and return its local path.

    Uses crds.getreferences to download the file to the local cache if needed,
    ensuring the file exists before opening. The crds:// URIs stored in
    dm.meta.ref_file are labels only and cannot be opened directly.
    """
    ref_paths = crds.getreferences(dm.get_crds_parameters(),
                                   reftypes=[reftype], observatory='roman')
    return ref_paths[reftype]


def _get_injection_gain(dm):
    """Get the median gain from the photom CRDS reference file.

    The L2 photometric zeropoint was derived using this gain to convert
    from DN/s to MJy/sr, so injection must use the same value for
    photometric self-consistency.
    """
    photom_model = rdm_datamodels.open(_fetch_crds_ref(dm, 'photom'))
    return photom_model.meta['instrument']['median_gain']


def _build_psf(dm):
    """Build a GriddedEPSF from the ePSF reference file used to calibrate this image.

    Fetches the same CRDS ePSF reference recorded in dm.meta.ref_file via
    crds.getreferences (downloads to local cache if not already present).
    """
    epsf_model = rdm_datamodels.open(_fetch_crds_ref(dm, 'epsf'))
    gridded_psf = romanisim.psf.get_gridded_psf_model(epsf_model)
    pixelscale = rsim_parameters.pixel_scale / gridded_psf.meta['epsf_oversample']
    imwcs = romanisim.wcs.GWCS(dm.meta.wcs)
    return GriddedEPSF(gridded_psf, pixelscale, wcs=imwcs)


def _make_injection_catalog(ra, dec, filter_name, fluxes_maggies):
    """Build an astropy Table in the format expected by inject_sources_into_l2.

    Parameters
    ----------
    ra, dec : array-like
        Sky coordinates in degrees.
    filter_name : str
        Roman filter name (e.g. 'F062').
    fluxes_maggies : array-like
        Source fluxes in maggies (10^(-0.4 * mag_AB)).

    Returns
    -------
    astropy.table.Table
    """
    tab = Table()
    tab['ra'] = np.asarray(ra)
    tab['dec'] = np.asarray(dec)
    tab['type'] = np.array(['PSF'] * len(ra))
    tab[filter_name.upper()] = np.asarray(fluxes_maggies)
    return tab


def _evaluate_catalogs_at_mjd(catalog_list_file, image_mjdobs, image_size, image_wcs, filter_name):
    """Evaluate variable star light curves at the image MJD and return positions and fluxes.

    Reads the same JSON field catalog format used by the existing OU injection
    pipeline. Fluxes are returned in maggies rather than image counts.

    Parameters
    ----------
    catalog_list_file : str
        Text file listing one JSON field catalog path per line.
    image_mjdobs : float
        MJD of the observation mid-time.
    image_size : tuple
        Image shape (ny, nx) in pixels.
    image_wcs : romanisim.wcs.GWCS
        Image WCS for RA/Dec -> pixel conversion.
    filter_name : str
        Roman filter name (e.g. 'F062').

    Returns
    -------
    ra, dec : np.ndarray
        Sky coordinates of sources that fall on the image.
    fluxes_maggies : np.ndarray
        Source fluxes in maggies at the observation epoch.
    xpix, ypix : np.ndarray
        Pixel positions of the sources, from the same image_wcs.toImage()
        call used for the on-image check. Passed on to inject_sources_into_l2
        as x=/y= so it doesn't fall back to its internal
        model.meta.wcs.numerical_inverse(..., with_bounding_box=False) call,
        which can silently return wildly wrong pixel positions (millions of
        pixels off) for some RA/Dec when the iterative solver fails to
        converge -- romanisim.wcs.GWCS itself avoids this same failure mode
        by using .invert() instead (see the commented-out numerical_inverse
        line in romanisim/wcs.py), but inject_sources_into_l2 does not.
    """
    with open(catalog_list_file, 'r') as fh:
        input_catalogs = [line.strip() for line in fh if line.strip()]

    ra_parts, dec_parts, flux_parts, xpix_parts, ypix_parts = [], [], [], [], []
    ny, nx = image_size

    for catalog_path in input_catalogs:
        with open(catalog_path, 'r') as f:
            catalog_sources = list(json.load(f).values())

        ra_coords = np.array([s['ra'] for s in catalog_sources])
        dec_coords = np.array([s['dec'] for s in catalog_sources])

        # Convert to pixel positions; keep sources within 50 px of the image edge
        xpos, ypos = image_wcs.toImage(ra_coords, dec_coords, units='deg')
        on_image = ((xpos >= -50.0) & (xpos < nx + 50.0) &
                    (ypos >= -50.0) & (ypos < ny + 50.0))

        if not np.any(on_image):
            continue

        sources_on = [s for s, m in zip(catalog_sources, on_image) if m]
        x_on = xpos[on_image]
        y_on = ypos[on_image]
        ra_on = ra_coords[on_image]
        dec_on = dec_coords[on_image]

        types = np.array([s['type'] for s in sources_on])
        fluxes = np.empty(len(sources_on))

        # Vectorize sinusoidal sources
        sin_mask = types == 'sinusoidal'
        if np.any(sin_mask):
            sin_src = [s for s, m in zip(sources_on, sin_mask) if m]
            magnitudes = np.array([s['parameters']['magnitude'] for s in sin_src])
            amplitudes = np.array([s['parameters']['amplitude'] for s in sin_src])
            periods = np.array([s['parameters']['period'] for s in sin_src])
            phases = np.array([s['parameters']['phase'] for s in sin_src])
            mags = magnitudes + amplitudes * np.sin(2 * np.pi * (image_mjdobs / periods + phases))
            fluxes[sin_mask] = 10**(-0.4 * mags)

        # Vectorize gaussian sources
        gauss_mask = types == 'gaussian'
        if np.any(gauss_mask):
            gauss_src = [s for s, m in zip(sources_on, gauss_mask) if m]
            magnitudes = np.array([s['parameters']['magnitude'] for s in gauss_src])
            peak_amps = np.array([s['parameters']['peak_amplitude'] for s in gauss_src])
            peak_times = np.array([s['parameters']['peak_time'] for s in gauss_src])
            sigmas = np.array([s['parameters']['sigma'] for s in gauss_src])
            static_flux = 10**(-0.4 * magnitudes)
            peak_amplitude = static_flux * (10**(0.4 * peak_amps) - 1.0)
            gauss_component = peak_amplitude * np.exp(-0.5 * ((image_mjdobs - peak_times) / sigmas) ** 2)
            fluxes[gauss_mask] = static_flux + gauss_component

        unknown = ~(sin_mask | gauss_mask)
        if np.any(unknown):
            raise ValueError(f"Unknown light curve type: {types[unknown][0]}")

        ra_parts.append(ra_on)
        dec_parts.append(dec_on)
        flux_parts.append(fluxes)
        xpix_parts.append(x_on)
        ypix_parts.append(y_on)

    if ra_parts:
        return (np.concatenate(ra_parts), np.concatenate(dec_parts),
                np.concatenate(flux_parts), np.concatenate(xpix_parts),
                np.concatenate(ypix_parts))
    return (np.array([]), np.array([]), np.array([]), np.array([]), np.array([]))


def inject_variable_stars_into_l2(asdf_path, catalog_list_file, output_path,
                                   fix_wcs=False, seed=None, rng=None):
    """Inject variable stars into a Roman L2 ASDF file.

    Loads an L2 calibrated image, optionally corrects the WCS, evaluates
    variable star light curves at the observation epoch, and injects the
    sources using a spatially variable CRDS ePSF and the photometrically
    consistent gain from the photom reference file.

    Parameters
    ----------
    asdf_path : str
        Path or S3 URI of the input L2 ASDF file.
    catalog_list_file : str
        Text file listing one JSON field catalog path per line. Each catalog
        follows the same format as the existing OU injection catalogs
        (sinusoidal or gaussian light curve parameters per source).
    output_path : str
        Local path for the output ASDF file with injected sources.
    fix_wcs : bool
        If True, rerun AssignWcsStep to restore the nominal distortion-model
        WCS before injection. Must be True whenever the WCS in the ASDF is
        not correct, since inject_sources_into_l2 uses the WCS to place
        sources at their catalog RA/Dec positions. Set True for the current
        SOC GBTDS simulations (Gaia alignment fails on simulated data).
        Default False since real Roman data will have a valid pipeline WCS.
    seed : int, optional
        Random seed for reproducibility.
    rng : galsim.BaseDeviate, optional
        GalSim random number generator. If None, one is constructed from seed.

    Returns
    -------
    result : roman_datamodels.datamodels.ImageModel
        ImageModel with injected sources. Also written to output_path.
    injection_catalog : astropy.table.Table
        Catalog of injected sources (ra, dec, filter flux in maggies).
    """
    print(f"Opening {asdf_path}")
    dm = _open_l2_asdf(asdf_path)

    if fix_wcs:
        print("Reassigning WCS (simulation workaround)...")
        _fix_wcs(dm)

    filter_name = dm.meta.instrument.optical_element
    image_size = dm.data.shape
    image_wcs = romanisim.wcs.GWCS(dm.meta.wcs)
    image_mjdobs = dm.meta.exposure.start_time.mjd

    print(f"  Filter: {filter_name}, SCA: {dm.meta.instrument.detector}, MJD: {image_mjdobs:.4f}")

    print("Evaluating light curves...")
    ra, dec, fluxes_maggies, xpix, ypix = _evaluate_catalogs_at_mjd(
        catalog_list_file, image_mjdobs, image_size, image_wcs, filter_name)
    print(f"  {len(ra)} sources on image")

    if len(ra) == 0:
        print("No sources to inject.")
        dm.save(output_path)
        return dm, Table()

    injection_catalog = _make_injection_catalog(ra, dec, filter_name, fluxes_maggies)

    # Ensure romanisim internal defaults are in place before any injection calls
    importlib.reload(rsim_parameters)

    print("Getting gain from photom reference...")
    gain = _get_injection_gain(dm)
    print(f"  gain = {gain:.4f} e-/DN")

    print("Building ePSF from CRDS reference...")
    psf = _build_psf(dm)

    print(f"Injecting {len(ra)} sources...")
    result = inject_sources_into_l2(
        dm.copy(), injection_catalog,
        x=xpix, y=ypix,
        psf=psf, gain=gain, rng=rng, seed=seed)

    print(f"Saving to {output_path}")
    result.save(output_path)

    return result, injection_catalog


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Inject variable stars into a Roman L2 ASDF file.')
    parser.add_argument('asdf_path',
                        help='Path or S3 URI of the input L2 ASDF file.')
    parser.add_argument('catalog_list_file',
                        help='Text file listing one JSON field catalog path per line.')
    parser.add_argument('output_path',
                        help='Output ASDF file path for the image with injected sources.')
    parser.add_argument('--fix-wcs', action='store_true',
                        help='Rerun AssignWcsStep to restore the nominal WCS '
                             '(required for current SOC GBTDS simulations).')
    parser.add_argument('--seed', type=int, default=None,
                        help='Integer random seed for reproducibility.')
    args = parser.parse_args()

    result, catalog = inject_variable_stars_into_l2(
        args.asdf_path,
        args.catalog_list_file,
        args.output_path,
        fix_wcs=args.fix_wcs,
        seed=args.seed)

    print(f"Done. {len(catalog)} sources injected.")
    if len(catalog) > 0:
        catalog.pprint(max_lines=20)
