"""
Reformat socsims.  Convert from ASDF format to FITS format, and add required FITS keywords.

% aws s3 ls s3://stpubdata/roman/nexus/soc_simulations/r00340/l2/ | grep cal.asdf | wc
   88038  352152 6778926
"""

import boto3
import re
import subprocess
import numpy as np
import asdf
from astropy.io import fits
from astropy.wcs import WCS, Sip
from astropy.coordinates import SkyCoord
import astropy.units as u
from datetime import datetime
from astropy.time import Time

import modules.utils.rapid_pipeline_subs as util


# Define code.

swname = "convert_socsims.py"
swvers = "1.0"

print("swname =", swname)
print("swvers =", swvers)

# Define input and output S3 buckets.

bucket_name_input = "stpubdata/roman/nexus/soc_simulations/r00340/l2"
bucket_name_output = "socsim-20260427-lite"


# Create S3-client object.

s3_client = boto3.client('s3')


###################################################################
# Methods.
###################################################################

def execute_command_in_shell(bash_command,fname_out=None):

    '''
    Execute a batch command (a string, not a list; can be multiple bash commands connected with &&).
    '''

    print("execute_command: bash_command =",bash_command)


    # Execute code_to_execute.  Note that STDERR and STDOUT are merged into the same data stream.
    # AWS Batch runs Python 3.9.  According to https://docs.python.org/3.9/library/subprocess.html#subprocess.run,
    # if you wish to capture and combine both streams into one, use stdout=PIPE and stderr=STDOUT instead of capture_output.
    # capture_output=False is the default.

    code_to_execute_object = subprocess.run(bash_command,shell=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)

    returncode = code_to_execute_object.returncode
    print("returncode =",returncode)

    code_to_execute_stdout = code_to_execute_object.stdout
    #print("code_to_execute_stdout =\n",code_to_execute_stdout)

    if fname_out is not None:

        try:
            fh = open(fname_out, 'w', encoding="utf-8")
            fh.write(code_to_execute_stdout)
            fh.close()
        except:
            print(f"*** Warning from method execute_command: Could not open output file {fname_out}; quitting...")

    code_to_execute_stderr = code_to_execute_object.stderr
    #print("code_to_execute_stderr (should be empty since STDERR is combined with STDOUT) =\n",code_to_execute_stderr)

    return returncode,code_to_execute_stdout


def extract_gwcs(af):
    """Extract a gwcs.WCS object from an open ASDF file."""
    tree = af.tree
    # Common locations for WCS in ASDF trees (JWST, Roman, etc.)
    for key in ("roman.meta.wcs","roman.meta","wcs", "meta.wcs"):
        parts = key.split(".")
        node = tree
        try:
            for p in parts:
                node = node[p]
            if hasattr(node, "pixel_to_world"):
                return node
        except (KeyError, TypeError):
            continue
    # Walk top-level keys
    for v in tree.values():
        if hasattr(v, "pixel_to_world"):
            return v
    raise ValueError(
        "No gwcs.WCS object found in ASDF tree. "
        "Inspect af.tree manually to locate the WCS key."
    )


def make_pixel_grid(shape, step=32):
    """Return (x, y) pixel coordinate arrays on a regular grid."""
    ny, nx = shape
    y_pts = np.arange(0, ny, step, dtype=float)
    x_pts = np.arange(0, nx, step, dtype=float)
    xg, yg = np.meshgrid(x_pts, y_pts)
    return xg.ravel(), yg.ravel()


def fit_sip(gwcs_obj, shape, sip_degree=4, step=32):
    """
    Fit a FITS WCS + SIP polynomial to a gwcs.WCS object.

    Parameters
    ----------
    gwcs_obj : gwcs.WCS
        The generalized WCS to approximate.
    shape : (ny, nx) tuple
        Image dimensions in pixels.
    sip_degree : int
        Polynomial order for SIP (2–5 is typical).
    step : int
        Pixel grid spacing used for fitting.

    Returns
    -------
    astropy.wcs.WCS
        A FITS WCS with SIP keywords populated.
    """
    px, py = make_pixel_grid(shape, step=step)

    # Transform pixel -> sky using gwcs
    sky = gwcs_obj.pixel_to_world(px, py)
    if isinstance(sky, SkyCoord):
        ra = sky.ra.deg
        dec = sky.dec.deg
    else:
        # Some gwcs objects return (lon, lat) arrays directly
        ra, dec = np.asarray(sky[0]), np.asarray(sky[1])

    # Remove NaN/Inf points (outside valid domain)
    mask = np.isfinite(ra) & np.isfinite(dec)
    px, py, ra, dec = px[mask], py[mask], ra[mask], dec[mask]
    if mask.sum() < 10:
        raise RuntimeError("Too few valid sky coordinates to fit SIP.")

    # --- Linear (CD-matrix) WCS fit ---
    # Reference pixel: image centre
    crpix1 = shape[1] / 2.0
    crpix2 = shape[0] / 2.0

    dpx = px - (crpix1 - 1)   # 0-indexed offset
    dpy = py - (crpix2 - 1)

    # Least-squares fit: [ra, dec] = crval + CD * [dpx, dpy]
    A_lin = np.column_stack([np.ones(len(dpx)), dpx, dpy])
    (crval1, cd1_1, cd1_2), _, _, _ = np.linalg.lstsq(A_lin, ra, rcond=None)
    (crval2, cd2_1, cd2_2), _, _, _ = np.linalg.lstsq(A_lin, dec, rcond=None)

    # Build a plain WCS to compute linear residuals
    w = WCS(naxis=2)
    w.wcs.crpix = [crpix1, crpix2]
    w.wcs.crval = [crval1, crval2]
    w.wcs.cd = np.array([[cd1_1, cd1_2], [cd2_1, cd2_2]])
    w.wcs.ctype = ["RA---TAN-SIP", "DEC--TAN-SIP"]
    w.wcs.set()

    # --- SIP forward coefficients (pixel -> intermediate) ---
    # Residuals in intermediate world coordinates (degrees)
    xi_full, eta_full = w.all_world2pix(ra, dec, 0)  # back to pixel (undistorted)
    # Actual pixel coords minus undistorted: the distortion
    u_dist = px - xi_full   # residual in x
    v_dist = py - eta_full  # residual in y

    # Fit polynomial: distortion = sum_{p+q<=N} a_pq * dpx^p * dpy^q
    def poly_design_matrix(dpx, dpy, degree):
        cols = []
        for p in range(degree + 1):
            for q in range(degree + 1):
                if 1 <= p + q <= degree:   # SIP excludes 0th order & linear handled by CD
                    cols.append(dpx**p * dpy**q)
        return np.column_stack(cols)

    def poly_indices(degree):
        idxs = []
        for p in range(degree + 1):
            for q in range(degree + 1):
                if 1 <= p + q <= degree:
                    idxs.append((p, q))
        return idxs

    # Re-centre on CRPIX for SIP convention (1-indexed)
    dpx1 = px - (crpix1 - 1)
    dpy1 = py - (crpix2 - 1)

    P = poly_design_matrix(dpx1, dpy1, sip_degree)
    idxs = poly_indices(sip_degree)

    coeffs_a, _, _, _ = np.linalg.lstsq(P, u_dist, rcond=None)
    coeffs_b, _, _, _ = np.linalg.lstsq(P, v_dist, rcond=None)

    # Build SIP coefficient arrays (shape [degree+1, degree+1])
    a_arr = np.zeros((sip_degree + 1, sip_degree + 1))
    b_arr = np.zeros((sip_degree + 1, sip_degree + 1))
    for (p, q), ca, cb in zip(idxs, coeffs_a, coeffs_b):
        a_arr[p, q] = ca
        b_arr[p, q] = cb

    # Inverse SIP (approximate via reverse fit)
    # Apply forward SIP to get corrected pixel, then fit inverse
    u_sip = dpx1 + (P @ coeffs_a)
    v_sip = dpy1 + (P @ coeffs_b)

    # Residuals for inverse: given corrected pixel, recover original
    f_dist = dpx1 - u_sip
    g_dist = dpy1 - v_sip

    P_inv = poly_design_matrix(u_sip, v_sip, sip_degree)
    coeffs_ap, _, _, _ = np.linalg.lstsq(P_inv, f_dist, rcond=None)
    coeffs_bp, _, _, _ = np.linalg.lstsq(P_inv, g_dist, rcond=None)

    ap_arr = np.zeros((sip_degree + 1, sip_degree + 1))
    bp_arr = np.zeros((sip_degree + 1, sip_degree + 1))
    for (p, q), ca, cb in zip(idxs, coeffs_ap, coeffs_bp):
        ap_arr[p, q] = ca
        bp_arr[p, q] = cb

    # Attach SIP to the WCS object
    w.sip = Sip(a_arr, b_arr, ap_arr, bp_arr, [crpix1, crpix2])
    w.wcs.ctype = ["RA---TAN-SIP", "DEC--TAN-SIP"]
    w.wcs.set()
    return w


def residual_stats(gwcs_obj, fits_wcs, shape, step=64):
    """Report RMS residual (arcsec) between gwcs and the fitted SIP WCS."""
    px, py = make_pixel_grid(shape, step=step)
    sky = gwcs_obj.pixel_to_world(px, py)
    if isinstance(sky, SkyCoord):
        ra_true, dec_true = sky.ra.deg, sky.dec.deg
    else:
        ra_true, dec_true = np.asarray(sky[0]), np.asarray(sky[1])

    mask = np.isfinite(ra_true) & np.isfinite(dec_true)
    ra_fit, dec_fit = fits_wcs.all_pix2world(px[mask], py[mask], 0)

    d_ra  = (ra_fit  - ra_true[mask])  * np.cos(np.radians(dec_true[mask])) * 3600
    d_dec = (dec_fit - dec_true[mask]) * 3600
    sep   = np.hypot(d_ra, d_dec)
    return sep.mean(), sep.max()


def asdf_to_fits(asdf_path, fits_path, *, shape=None, sip_degree=4,
                 grid_step=32, image_data=None):
    """
    Read *asdf_path*, extract the WCS, fit SIP, and write *fits_path*.

    Parameters
    ----------
    asdf_path : str
        Input ASDF file.
    fits_path : str
        Output FITS file.
    shape : (ny, nx) or None
        Image shape. Inferred from ASDF data array if None.
    sip_degree : int
        SIP polynomial order (default 4).
    grid_step : int
        Pixel spacing for fitting grid (default 32).
    image_data : ndarray or None
        Optional image array to embed in the FITS file.
    """
    with asdf.open(asdf_path, lazy_load=False) as af:

        af.info()

        af_tree_keys = af.tree.keys()
        for key in af_tree_keys:
            value = af.tree[key]
            print(f"=====>af_tree_key = {key}")
            print(f"=====>af_tree_value = {value}")

        #af_tree_wcs = af.tree['meta']
        #print(f"af_tree_wcs = {af_tree_wcs}")

        gwcs_obj = extract_gwcs(af)

        # Determine image shape
        if shape is None:


            '''
            # Try common ASDF tree locations
            for key in ("roman.data", "data", "science", "SCI"):
                arr = af.tree.get(key)
                if arr is not None and hasattr(arr, "shape") and arr.ndim >= 2:
                    shape = arr.shape[-2:]
                    if image_data is None:
                        image_data = np.asarray(arr)
                    break
            '''

            roman_tree = af.tree['roman']

            # get exposure start time from the metadata
            start_time = roman_tree['meta']['exposure']['start_time']

            print(f"=====>start_time = {start_time}")


            # get exposure metadata
            exposure_metadata = roman_tree['meta']['exposure']

            print(f"exposure_metadata = {exposure_metadata}")


            # load the data array
            arr = roman_tree['data']
            if image_data is None:
                image_data = np.asarray(arr)
                image_data_64 = image_data.astype(np.float64)

            shape = arr.shape

            print(f"=====>data_shape = {shape}")


        if shape is None:
            # Fall back to bounding box from gwcs
            try:
                bb = gwcs_obj.bounding_box
                # bounding_box is ((x0,x1),(y0,y1)) for 2D
                (x0, x1), (y0, y1) = bb
                shape = (int(y1 - y0), int(x1 - x0))
            except Exception:
                raise ValueError(
                    "Cannot determine image shape. "
                    "Pass shape=(ny, nx) explicitly."
                )

        print(f"Image shape : {shape}")
        print(f"SIP degree  : {sip_degree}")
        print(f"Grid step   : {grid_step} px")

        fits_wcs = fit_sip(gwcs_obj, shape,
                           sip_degree=sip_degree, step=grid_step)

        rms, mx = residual_stats(gwcs_obj, fits_wcs, shape)
        print(f"Residuals   : RMS={rms*1000:.2f} mas  max={mx*1000:.2f} mas")


    # Build FITS file.

    hdr = fits_wcs.to_header(relax=True)   # relax=True writes SIP keywords
    hdr["NAXIS"]  = 2
    hdr["NAXIS1"] = shape[1]
    hdr["NAXIS2"] = shape[0]


    '''
    Example with required keywords:

    CRPIX1  =               2044.5 / Pixel coordinate of reference point
    CRPIX2  =               2044.5 / Pixel coordinate of reference point
    CD1_1   =  2.6187949489571E-05 / Coordinate transformation matrix element
    CD1_2   =  1.4574831453605E-05 / Coordinate transformation matrix element
    CD2_1   = -1.5281148010609E-05 / Coordinate transformation matrix element
    CD2_2   =  2.5443010447297E-05 / Coordinate transformation matrix element
    CUNIT1  = 'deg'                / Units of coordinate increment and value
    CUNIT2  = 'deg'                / Units of coordinate increment and value
    CTYPE1  = 'RA---TAN-SIP'       / TAN (gnomonic) projection + SIP distortions
    CTYPE2  = 'DEC--TAN-SIP'       / TAN (gnomonic) projection + SIP distortions
    CRVAL1  =      268.49713037465 / [deg] Coordinate value at reference point
    CRVAL2  =      -29.20479624611 / [deg] Coordinate value at reference point
    LONPOLE =                180.0 / [deg] Native longitude of celestial pole
    LATPOLE =      -29.20479624611 / [deg] Native latitude of celestial pole
    WCSNAME = 'wfiwcs_20210204_d2' / Coordinate system title
    MJDREF  =                  0.0 / [d] MJD of fiducial time
    RADESYS = 'FK5'                / Equatorial coordinate system
    EQUINOX =               2000.0 / [yr] Equinox of equatorial coordinates
    FILTER  = 'K213    '           / filter used
    NCOL    =                 4088 / number of columns in image
    NROW    =                 4088 / number of rows in image
    DETECTOR= 'SCA02   '           / detector assembly
    TSTART  =    2461486.094234365 / observation start in Julian Date
    TEND    =   2461486.0948676984 / observation end in Julian Date
    DATE-OBS= '2027-03-21T14:15:41.849' / observation start in UTC Calendar Date
    DATE-END= '2027-03-21T14:16:36.569' / observation end in UTC Calendar Date
    EXPOSURE=                54.72 / time on source in s
    SOFTWARE= 'rimtimsim_v2.0'
    CREATED = '2026-03-05 01:45:23'
    MJD-OBS =    61485.59423436504
    EXPTIME =                54.72
    ZPTMAG  =    25.85726796291789
    SCA_NUM =                    2
    '''


    # Add EXPTIME,DATE-OBS,DATE-END,MJD-OBS keywords.

    exptime = exposure_metadata['exposure_time']
    dateobs = exposure_metadata['start_time']
    dateend = exposure_metadata['end_time']

    print(f"dateobs = {dateobs}")

    t_dateobs = type(dateobs)
    print(f"t_dateobs = {t_dateobs}")


    # %Y = Year, %m = Month, %d = Day, %H = Hour, %M = Minute, %S = Second
    date_object = datetime.strptime(f"{dateobs}", "%Y-%m-%dT%H:%M:%S.%f")

    t = Time(date_object)
    mjd = t.mjd

    hdr["EXPTIME"] = exptime
    hdr["DATE-OBS"] = str(dateobs)
    hdr["DATE-END"] = str(dateend)
    hdr["MJD-OBS"] = mjd


    # Remove CDELT1 and CDELT2 keywords.

    hdr.remove('CDELT1', remove_all=True)
    hdr.remove('CDELT2', remove_all=True)


    # Rename PCi_j keywords to CDi_j keywords.

    hdr.rename_keyword('PC1_1', 'CD1_1', force=False)
    hdr.rename_keyword('PC1_2', 'CD1_2', force=False)
    hdr.rename_keyword('PC2_1', 'CD2_1', force=False)
    hdr.rename_keyword('PC2_2', 'CD2_2', force=False)


    # Add SCA_NUM keyword.

    detector = roman_tree['meta']['instrument']['detector']
    sca_num = int(detector.replace("WFI",""))
    hdr["SCA_NUM"] = sca_num


    # Translater filter names to be similar to Open Universe sims:
    #
    # rimtimsims2db=> select * from filters;
    # fid | filter
    # -----+--------
    #    1 | F184
    #    2 | H158
    #    3 | J129
    #    4 | K213
    #    5 | R062
    #    6 | Y106
    #    7 | Z087
    #    8 | W146
    # (8 rows)

    filter = roman_tree['meta']['instrument']['optical_element']
    if "213" in filter:
        translated_filter = filter.replace("F213","K213").strip()
    elif "184" in filter:
        translated_filter = filter.replace("F184","F184").strip()
    elif "158" in filter:
        translated_filter = filter.replace("F158","H158").strip()
    elif "129" in filter:
        translated_filter = filter.replace("F129","J129").strip()
    elif "062" in filter:
        translated_filter = filter.replace("F062","R062").strip()
    elif "106" in filter:
        translated_filter = filter.replace("F106","Y106").strip()
    elif "087" in filter:
        translated_filter = filter.replace("F087","Z087").strip()
    elif "146" in filter:
        translated_filter = filter.replace("F146","W146").strip()
    else:
        print(f"*** Error: Unexpected filter = {filter}")
        exit(64)

    hdr["FILTER"] = translated_filter


    # Add ZPTMAG keyword.

    '''
    Nominal WFI AB Zero Points
    Filter  Wavelength (m)    AB Zero Point (mag)
    F062    0.48 – 0.76       26.4
    F087    0.76 – 0.98       26.3
    F106    0.93 – 1.19       26.4
    F129    1.13 – 1.45       26.3
    F158    1.38 – 1.77       26.4
    F184    1.68 – 2.00       25.9
    F213    1.95 – 2.30       25.4
    F146    0.93 – 2.00       27.5
    '''

    if "213" in filter:
        zptmag = 25.4
    elif "184" in filter:
        zptmag = 25.9
    elif "158" in filter:
        zptmag = 26.4
    elif "129" in filter:
        zptmag = 26.3
    elif "062" in filter:
        zptmag = 26.4
    elif "106" in filter:
         zptmag = 26.4
    elif "087" in filter:
        zptmag = 26.3
    elif "146" in filter:
        zptmag = 27.5
    else:
        print(f"*** Error: Unexpected filter = {filter}")
        exit(64)

    hdr["ZPTMAG"] = zptmag


    # Multiply by exposure time to convert e-/s into DN (assuming sca_gain = 1.0).

    hdr["BUNIT"] = "DN"

    print(f"exptime = {exptime}")

    np_data = image_data_64 * float(exptime)


    # Create primary and image HDUs, and then output FITS file.

    new_hdu = fits.ImageHDU(header=hdr,data=np_data.astype(np.float32))

    primary_hdu = fits.PrimaryHDU(header=hdr)

    hdul = fits.HDUList([primary_hdu, new_hdu])
    hdul.writeto(fits_path,overwrite=True,checksum=True)
    print(f"Wrote       : {fits_path}")


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    # Parse desired input files in input S3 bucket.

    input_asdf_files = []

    cp_cmd = f"aws s3 ls s3://{bucket_name_input}/ | grep cal.asdf"
    exitcode_from_cp,code_to_execute_stdout = execute_command_in_shell(cp_cmd)
    lines = code_to_execute_stdout.splitlines()

    i = 0
    for line in lines:

        #print(line)

        input_file_metadata = line.strip().split()

        if "cal.asdf" in input_file_metadata[3]:

            input_file = input_file_metadata[3]

            #print(f"input_file = {input_file}")

            input_asdf_files.append(input_file)

        i += 1

        if i > 1:
            break

    print(f"Total number of socsims = {i}")


    # Loop over input FITS files.

    i = 0

    for input_asdf_file in input_asdf_files:

        print(f"i,input_asdf_file = {i},{input_asdf_file}")

        if ".asdf" not in input_asdf_file:
            continue


        # Download file from input S3 bucket to local machine.

        s3_object_input_asdf_file = "s3://" + bucket_name_input + "/" + input_asdf_file
        download_cmd = ['aws','s3','cp',s3_object_input_asdf_file,input_asdf_file]
        exitcode_from_download_cmd = util.execute_command(download_cmd)


        # Create output FITS filename for working directory.

        output_fits_file = input_asdf_file.replace(".asdf","_lite.fits")


        # Convert from ASDF format to FITS format, and add required FITS keywords.
        # Define pixel grid spacing for computing SIP distortion.

        degree = 5
        step = 16
        shape = None                # Method will compute this if None.

        asdf_to_fits(
            input_asdf_file,
            output_fits_file,
            shape=shape,
            sip_degree=degree,
            grid_step=step,
        )


        # Gzip the output FITS file.

        gunzip_cmd = ['gzip', output_fits_file]
        exitcode_from_gunzip = util.execute_command(gunzip_cmd)


        # Upload gzipped file to output S3 bucket.

        gzipped_output_fits_file = output_fits_file + ".gz"

        s3_object_name = gzipped_output_fits_file

        filenames = [gzipped_output_fits_file]

        objectnames = [s3_object_name]

        util.upload_files_to_s3_bucket(s3_client,bucket_name_output,filenames,objectnames)


        # Clean up work directory.

        rm_cmd = ['rm','-f',input_asdf_file]
        exitcode_from_rm = util.execute_command(rm_cmd)

        rm_cmd = ['rm','-f',output_fits_file]
        exitcode_from_rm = util.execute_command(rm_cmd)

        rm_cmd = ['rm','-f',gzipped_output_fits_file]
        exitcode_from_rm = util.execute_command(rm_cmd)

        i += 1


    # Termination.

    exit(0)


