"""
Compute reprojected coadd image, coadd uncertainty, and depth of coverage.

Uses reproject_and_coadd with exact resampling (area-weighted interpolation)
and parallel processing.

Usage
-----
  python coadd.py img1.fits img2.fits img3.fits -o result
  python coadd.py img*.fits -u unc*.fits --combine mean -j 4 -o result
"""

import os
import argparse
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from reproject import reproject_adaptive, reproject_exact
from reproject.mosaicking import reproject_and_coadd, find_optimal_celestial_wcs


# ---------------------------------------------------------------------------
# Create WCS for output coadd.
# ---------------------------------------------------------------------------

def create_output_wcs(ra,dec,n_pixels,cdelt1_refimage,cdelt2_refimage):

    # 1. Initialize the WCS object with the number of dimensions
    w = WCS(naxis=2)

    # 2. Set the reference pixel (CRPIX) - 1-based indexing standard
    # This represents the center or anchor pixel in your image array
    crval = n_pixels / 2 + 0.5
    w.wcs.crpix = [crval, crval]

    # 3. Set the coordinate values at the reference pixel (CRVAL)
    # For example, RA and Dec coordinates in degrees
    w.wcs.crval = [ra,dec]

    # 4. Set the coordinate step/scale per pixel (CDELT)
    # Negative value for RA means RA increases to the left (standard sky view)
    w.wcs.cdelt = np.array([cdelt1_refimage, cdelt2_refimage]) # ~1 arcsec per pixel

    # 5. Set the coordinate projection types (CTYPE)
    # TAN represents a standard Gnomonic (tangent plane) projection
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]

    # Return WCS for output coadd.
    return w


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_hdu(path, hdu_index=0):
    """Return (float64 array, WCS, primary header) from a FITS file."""
    with fits.open(path) as hdul:
        data = hdul[hdu_index].data.astype(np.float64)
        header = hdul[hdu_index].header
        wcs = WCS(header)
    return data, wcs, header


def write_fits(path, data, header, overwrite=True):
    fits.writeto(path, data.astype(np.float32), header=header, overwrite=overwrite, checksum=True)
    print(f"  wrote {path}")


# ---------------------------------------------------------------------------
# Reprojection method
# ---------------------------------------------------------------------------

def _reproject_method(input_data, output_projection, shape_out=None, reprojection_type=None, **kwargs):

    '''
    reproject_exact (drop-in for reproject_and_coadd).
    '''
    print(f"In _reproject_method: reprojection_type={reprojection_type}")

    if reprojection_type == "adaptive":

        return reproject_adaptive(
            input_data,
            output_projection,
            shape_out=shape_out,
            kernel="gaussian",
            **kwargs,
        )

    else:

        return reproject_exact(
            input_data,
            output_projection,
            shape_out=shape_out,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Core coadd routine
# ---------------------------------------------------------------------------

def compute_coadd(
    input_paths,
    uncertainty_paths=None,
    output_wcs=None,
    shape_out=None,
    combine="mean",
    n_jobs=None,
    reprojection_type=None,
):
    """
    Reproject and coadd a list of science FITS images.

    Parameters
    ----------
    input_paths : list[str]
        Ordered list of science image FITS paths.
    uncertainty_paths : list[str] | None
        Ordered list of per-pixel uncertainty FITS paths (1-sigma).
        Must be the same length as *input_paths* when supplied.
    output_wcs : WCS | None
        Target WCS.  Auto-computed from inputs when None.
    shape_out : tuple[int, int] | None
        Output (ny, nx).  Required when *output_wcs* is supplied explicitly.
    combine : str
        How to combine overlapping pixels: 'mean' (default), 'sum',
        'median', 'min', 'max', 'first', 'last'.
    n_jobs : int | None
        Worker processes.  None → use all available CPUs.
        Pass 1 to disable parallelism.

    Returns
    -------
    coadd : np.ndarray
        Coadded science image (NaN where coverage == 0).
    uncertainty : np.ndarray | None
        Propagated 1-sigma uncertainty (None when no uncertainty files given).
    coverage : np.ndarray
        Depth-of-coverage map: effective number of contributing images per pixel.
    output_wcs : WCS
        WCS of the output mosaic.
    """

    # Load science images
    science = [(load_hdu(p)[0], load_hdu(p)[1]) for p in input_paths]

    # Derive optimal output projection when not supplied
    if output_wcs is None:
        output_wcs, shape_out = find_optimal_celestial_wcs(science)

    # parallel argument accepted by reproject_and_coadd:
    #   True  → all CPUs,  int → explicit count,  False → serial
    parallel = True if n_jobs is None else (False if n_jobs == 1 else n_jobs)

    # --- science coadd ---
    print(f"Coadding {len(science)} science image(s) [combine={combine}] ...")
    coadd, coverage = reproject_and_coadd(
        science,
        output_wcs,
        shape_out=shape_out,
        reprojection_type=reprojection_type,
        reproject_function=_reproject_method,
        combine_function=combine,
        parallel=parallel,
    )

    # Mask pixels with no coverage
    coadd[coverage == 0] = np.nan

    # --- uncertainty propagation ---
    uncertainty = None
    if uncertainty_paths is not None:
        print(f"Propagating uncertainty from {len(uncertainty_paths)} file(s) ...")

        # Coadd variance (= sigma^2) images using *sum* so we can divide by N^2
        variance_inputs = [(load_hdu(p)[0] ** 2, load_hdu(p)[1]) for p in uncertainty_paths]

        variance_sum, _ = reproject_and_coadd(
            variance_inputs,
            output_wcs,
            shape_out=shape_out,
            reprojection_type=reprojection_type,
            reproject_function=_reproject_method,
            combine_function="sum",   # always sum variances
            parallel=parallel,
        )

        n = np.maximum(coverage, 1)   # avoid division by zero

        with np.errstate(invalid="ignore", divide="ignore"):
            if combine == "mean":
                # sigma_mean = sqrt(sum_i sigma_i^2) / N
                uncertainty = np.sqrt(variance_sum) / n
            elif combine == "sum":
                # sigma_sum = sqrt(sum_i sigma_i^2)
                uncertainty = np.sqrt(variance_sum)
            else:
                # Reasonable approximation for other combine functions
                uncertainty = np.sqrt(variance_sum) / n

        uncertainty[coverage == 0] = np.nan

    return coadd, uncertainty, coverage, output_wcs


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------

def write_outputs(coadd, uncertainty, coverage, wcs, prefix):
    header = wcs.to_header()
    write_fits(f"{prefix}_coadd.fits",    coadd,       header)
    write_fits(f"{prefix}_coverage.fits", coverage,    header)
    if uncertainty is not None:
        write_fits(f"{prefix}_uncertainty.fits", uncertainty, header)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():

    p = argparse.ArgumentParser(
        description="Reproject and coadd FITS images with Gaussian-kernel adaptive resampling."
    )
    p.add_argument("inputs", nargs="+", help="Science FITS files (ordered)")
    p.add_argument(
        "-u", "--uncertainties", nargs="+", metavar="UNC",
        help="Uncertainty FITS files (same order and count as inputs)",
    )
    p.add_argument(
        "-o", "--output", default="output", metavar="PREFIX",
        help="Output filename prefix  [default: output]",
    )
    p.add_argument(
        "--combine", default="mean",
        choices=["mean", "sum", "median", "min", "max", "first", "last"],
        help="Pixel combine function  [default: mean]",
    )
    p.add_argument(
        "-j", "--jobs", type=int, default=None, metavar="NJOBS",
        help="Parallel worker processes (default: all CPUs; 1 = serial)",
    )
    p.add_argument(
        "-p", "--pixels", type=int, required=True, metavar="NPIXELS",
        help="Number of pixels on a side for the coadd (required)",
    )
    p.add_argument(
        "-r", "--ra", type=float, required=True, metavar="RA",
        help="Right ascension of coadd center (required)",
    )
    p.add_argument(
        "-d", "--dec", type=float, required=True, metavar="DEC",
        help="Declination of coadd center (required)",
    )
    p.add_argument(
        "--cdelt1", type=float, default=-0.000030555555556, metavar="CDELT1",
        help="CDELT1 for WCS (negative value okay for standard sky view)",
    )
    p.add_argument(
        "--cdelt2", type=float, default=0.000030555555556, metavar="CDELT2",
        help="CDELT2 for WCS (negative value okay for sky view)",
    )
    p.add_argument(
        "-t", "--type", type=str, default="exact", metavar="TYPE",
        help="Type of reprojection: exact or adaptive (default: exact)",
    )
    return p.parse_args()


def main():

    args = parse_args()

    if args.uncertainties and len(args.uncertainties) != len(args.inputs):
        raise SystemExit(
            "error: --uncertainties count must match the number of input files "
            f"({len(args.uncertainties)} vs {len(args.inputs)})"
        )


    # Create WCS for output coadd and specify coadd size.

    ra  = args.ra           # RA at coadd image center (deg)
    dec  = args.dec         # Dec at coadd image center (deg)
    n_pixels = args.pixels
    cdelt1_refimage = args.cdelt1
    cdelt2_refimage = args.cdelt2

    reprojection_type = args.type
    if not (reprojection_type == "adaptive" or reprojection_type == "exact"):
        raise SystemExit(
            f"error: Type of reprojection must be exact or adaptive (use -t or --type option) = {reprojection_type}"
        )
    print(f"reprojection_type={reprojection_type}")

    shape_out = (n_pixels,n_pixels)

    output_wcs = create_output_wcs(ra,dec,n_pixels,cdelt1_refimage,cdelt2_refimage)


    # Compute coadd and associated uncertainty and depth-of-coverage maps.

    if args.jobs is None:
        print(f"n_jobs=all_cpus")
    else:
        print(f"n_jobs={args.jobs}")

    coadd, uncertainty, coverage, wcs = compute_coadd(
        args.inputs,
        uncertainty_paths=args.uncertainties,
        combine=args.combine,
        n_jobs=args.jobs,
        output_wcs = output_wcs,
        shape_out=shape_out,
        reprojection_type=reprojection_type,
    )

    print("Writing outputs ...")
    write_outputs(coadd, uncertainty, coverage, wcs, args.output)
    print("Done.")


#################
# Main program.
#################

if __name__ == "__main__":
    main()
