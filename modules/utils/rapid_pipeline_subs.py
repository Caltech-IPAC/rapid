import os
import math
from astropy.io import fits
import re
import subprocess
import numpy as np
import numpy.ma as ma
import boto3
from botocore.exceptions import ClientError

from modules.sip_tpv.sip_tpv.sip_to_pv import sip_to_pv

debug = True

rtd = 180.0 / math.pi
dtr = 1.0 / rtd


def execute_command(code_to_execute_args):

    '''
    Execute a command with options.
    '''

    print("execute_command: code_to_execute_args =",code_to_execute_args)


    # Execute code_to_execute.  Not that STDERR and STDOUT are merged into the same data stream.
    # AWS Batch runs Python 3.9.  According to https://docs.python.org/3.9/library/subprocess.html#subprocess.run,
    # if you wish to capture and combine both streams into one, use stdout=PIPE and stderr=STDOUT instead of capture_output.
    # capture_output=False is the default.

    code_to_execute_object = subprocess.run(code_to_execute_args,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)

    returncode = code_to_execute_object.returncode
    print("returncode =",returncode)

    code_to_execute_stdout = code_to_execute_object.stdout
    print("code_to_execute_stdout =\n",code_to_execute_stdout)

    code_to_execute_stderr = code_to_execute_object.stderr
    print("code_to_execute_stderr (should be empty since STDERR is combined with STDOUT) =\n",code_to_execute_stderr)

    return returncode


def upload_files_to_s3_bucket(s3_client,s3_bucket_name,filenames,s3_object_names):

    '''
    Upload list of files to S3 bucket.  Corresponding list of S3 bucket object names must be provided.
    '''

    uploaded_to_bucket = True

    for filename,s3_object_name in zip(filenames,s3_object_names):

        if not os.path.exists(filename):
            print("*** Warning: File does not exist ({}); skipping...".format(filename))
            continue

        try:
            response = s3_client.upload_file(filename,
                                             s3_bucket_name,
                                             s3_object_name)

            print("response =",response)

        except ClientError as e:
            print("*** Error: Failed to upload {} to s3://{}/{}"\
                .format(filename,s3_bucket_name,s3_object_name))
            uploaded_to_bucket = False
            break

        if uploaded_to_bucket:
            print("Successfully uploaded {} to s3://{}/{}"\
                .format(filename,s3_bucket_name,s3_object_name))

    return uploaded_to_bucket


def download_file_from_s3_bucket(s3_client,s3_full_name):

    '''
    Download file from S3 bucket.
    The full name is assumed to be of the following form: s3://sims-sn-f184-lite/1856/Roman_TDS_simple_model_F184_1856_2_lite.fits.gz
    and will be parsed for the s3 bucket name, object name, and filename.
    '''


    # Parse full name.

    string_match = re.match(r"s3://(.+?)/(.+)", s3_full_name)              # TODO

    try:
        s3_bucket_name = string_match.group(1)
        s3_object_name = string_match.group(2)
        print("s3_bucket_name = {}, s3_s3_object_name = {}".\
            format(s3_bucket_name,s3_object_name))

    except:
        print("*** Error: Could not parse s3_full_name; quitting...")
        exit(64)

    string_match2 = re.match(r"(.+)/(.+)", s3_object_name)                 # TODO

    try:
        subdirs = string_match2.group(1)
        filename = string_match2.group(2)
        print("filename = {}".format(filename))

    except:
        print("*** Error: Could not parse s3_object_name; quitting...")
        exit(64)


    # Download reference-image input from associated S3 bucket.

    print("Downloading s3://{}/{} into {}...".format(s3_bucket_name,s3_object_name,filename))

    response = s3_client.download_file(s3_bucket_name,s3_object_name,filename)

    print("response =",response)

    return filename,subdirs


def compute_clip_corr(n_sigma):

    """
    Compute a correction factor to properly reinflate the variance after it is
    naturally diminished via data-clipping.  Employ a simple Monte Carlo method
    and standard normal deviates to simulate the data-clipping and obtain the
    correction factor.
    """

    var_trials = []
    for x in range(0,10):
        a = np.random.normal(0.0, 1.0, 1000000)
        med = np.median(a, axis=0)
        p16 = np.percentile(a, 16, axis=0)
        p84 = np.percentile(a, 84, axis=0)
        sigma = 0.5 * (p84 - p16)
        mdmsg = med - n_sigma * sigma
        b = np.less(a,mdmsg)
        mdpsg = med + n_sigma * sigma
        c = np.greater(a,mdpsg)
        mask = np.any([b,c],axis=0)
        mx = ma.masked_array(a, mask)
        var = ma.getdata(mx.var(axis=0))
        var_trials.append(var)

    np_var_trials = np.array(var_trials)
    avg_var_trials = np.mean(np_var_trials)
    std_var_trials = np.std(np_var_trials)
    corr_fact = 1.0 / avg_var_trials

    return corr_fact


def avg_data_with_clipping(input_filename,n_sigma = 3.0,hdu_index = 0):

    """
    Statistics with outlier rejection (n-sigma data-trimming), ignoring NaNs, across all data array dimensions.
    Assumes the 2D image data are in the specified HDU of the FITS file.
    """

    hdul = fits.open(input_filename)
    data_array = hdul[hdu_index].data

    cf = compute_clip_corr(n_sigma)
    sqrtcf = np.sqrt(cf)

    a = np.array(data_array)

    med = np.nanmedian(a)
    p16 = np.nanpercentile(a,16)
    p84 = np.nanpercentile(a,84)
    sigma = 0.5 * (p84 - p16)
    mdmsg = med - n_sigma * sigma
    b = np.less(a,mdmsg)
    mdpsg = med + n_sigma * sigma
    c = np.greater(a,mdpsg)
    d = np.where(np.isnan(a),True,False)
    mask = b | c | d
    mx = ma.masked_array(a, mask)
    avg = ma.getdata(mx.mean())
    std = ma.getdata(mx.std()) * sqrtcf
    cnt = ma.getdata(mx.count())

    return avg,std,cnt


#-------------------------------------------------------------------
# Given pixel location (x, y) on a tangent plane, compute the corresponding
# sky position (R.A., Dec.), neglecting geometric distortion.

def tan_proj(x,y,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2):

    if debug:
        print("crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2 =",crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)

    glong  = crval1
    glat   = crval2
    twist = crota2

    fsamp = x - crpix1
    fline = y - crpix2

    rpp1 = cdelt1 * dtr
    rpp2 = cdelt2 * dtr
    xx = -fsamp * rpp1
    yy = -fline * rpp2

    rtwist = twist * dtr
    temp = xx * math.cos(rtwist) - yy * math.sin(rtwist)
    yy = xx * math.sin(rtwist) + yy * math.cos(rtwist)
    xx = temp

    delta = math.atan(math.sqrt( xx * xx + yy * yy ))

    if (xx == 0.0) and (yy == 0.0): yy = 1.0
    beta = math.atan2(-xx, yy)
    glatr = glat * dtr
    glongr = glong * dtr
    lat = math.asin(-math.sin(delta) * math.cos(beta) * math.cos(glatr) + math.cos(delta) * math.sin(glatr))
    xxx = math.sin(glatr) * math.sin(delta) * math.cos(beta) + math.cos(glatr) * math.cos(delta)
    yyy = math.sin(delta) * math.sin(beta)
    lon = glongr + math.atan2(yyy, xxx)

    lat = lat * rtd
    lon = lon * rtd

    return (lon,lat)


#-------------------------------------------------------------------
# Given (x, y, z) on the unit sphere, compute (R.A., Dec.).

def convert_xyz_to_radec(x,y,z):

    zn = z
    if (zn < -1.0):
        zn = -1.0
    elif (zn > 1.0):
        zn = 1.0

    dec = rtd * math.asin(zn)

    denom = math.sqrt(x * x + y * y)

    xn = x / denom

    if (xn < -1.0):
        xn = -1.0
    elif (xn > 1.0):
        xn = 1.0

    ra = rtd * math.acos(xn)

    if (y < 0.0):
        ra = 360.0 - ra

    if math.isnan(ra):
        ra = 0.0

    return ra,dec


#-------------------------------------------------------------------
# Given (R.A., Dec.), compute (x, y, z) on the unit sphere.

def compute_xyz(ra,dec):

    alpha = math.radians(ra);
    delta = math.radians(dec);

    cosalpha = math.cos(alpha);
    sinalpha = math.sin(alpha);
    cosdelta = math.cos(delta);
    sindelta = math.sin(delta);

    x = cosdelta * cosalpha;
    y = cosdelta * sinalpha;
    z = sindelta;

    return x,y,z


#-------------------------------------------------------------------
# Read in Roman tessellation data and return in data structure
# that can be searched quickly in dec bins and then in ra bins.
# This algorithm is generally only appropriate for NSIDE=10 or smaller.

def read_roman_tessellation_nside10():

    rapid_sw = os.getenv("RAPID_SW")

    if rapid_sw is None:
        print("*** Error: Env. var. RAPID_SW is not set; quitting...")
        exit(64)

    roman_tessellation_file = rapid_sw + "/cdf/romantessellation_nside10.fits"

    hdul = fits.open(roman_tessellation_file)

    rt = hdul[1].data     # Table is in second FITS extension.

    roman_tessellation_dict = {}

    i = 0
    for row in rt:
        index = i + 1
        ramin = row[2]
        ramax = row[3]
        decmax = row[4]
        decmin = row[5]

        # The skymap FITS file has decmin and decmax values that are
        # screwed up, hence the following special logic:

        if i == 0 or i == 2401:
            decmax = row[5]
            decmin = row[4]

        key = "decmin" + str(decmin) + "decmax" + str(decmax)
        try:
            roman_tessellation_dict[key].append([ramin,ramax,index])
        except:
            roman_tessellation_dict[key] = []
            roman_tessellation_dict[key].append([ramin,ramax,index])
        i = i + 1

    return roman_tessellation_dict


#-------------------------------------------------------------------
# Get Roman tessellation index for given (ra,dec).
# This algorithm is generally only appropriate for NSIDE=10 or smaller.

def get_roman_tessellation_index(rt_dict,ra,dec):

    keys = rt_dict.keys()

    for key in keys:

        #print("key =",key)
        key_match = re.match(r"decmin(.+)decmax(.+)", key)

        decmin = float(key_match.group(1))
        decmax = float(key_match.group(2))
        #print("decmin,decmax =",decmin,decmax)

        if dec >= decmin and dec < decmax:

            #print("Found key =",key)

            try:
                ra_bin_triplet = rt_dict[key]
                #print("ra_bin_triplet = ",ra_bin_triplet)

                for i in range(len(ra_bin_triplet)):
                    ramin = ra_bin_triplet[i][0]
                    ramax = ra_bin_triplet[i][1]
                    index = ra_bin_triplet[i][2]
                    if ra >= ramin and ra < ramax:
                        return index
            except:
                print("*** Error: dictionary key invalid: key =",key)
                exit(64)

    return None


#-------------------------------------------------------------------
# Build command line for awaicgen.

def build_awaicgen_command_line_args(awaicgen_dict):

    '''
    Build awaicgen command line.
    '''

    software_to_execute = 'awaicgen'

    awaicgen_input_images_list_file = awaicgen_dict["awaicgen_input_images_list_file"]
    awaicgen_input_uncert_list_file = awaicgen_dict["awaicgen_input_uncert_list_file"]
    awaicgen_mosaic_size_x = float(awaicgen_dict["awaicgen_mosaic_size_x"])
    awaicgen_mosaic_size_y = float(awaicgen_dict["awaicgen_mosaic_size_y"])
    awaicgen_RA_center = float(awaicgen_dict["awaicgen_RA_center"])
    awaicgen_Dec_center = float(awaicgen_dict["awaicgen_Dec_center"])
    awaicgen_mosaic_rotation = float(awaicgen_dict["awaicgen_mosaic_rotation"])
    awaicgen_pixelscale_absolute = float(awaicgen_dict["awaicgen_pixelscale_absolute"])
    awaicgen_inv_var_weight_flag = int(awaicgen_dict["awaicgen_inv_var_weight_flag"])
    awaicgen_pixelflux_scale_flag = int(awaicgen_dict["awaicgen_pixelflux_scale_flag"])
    awaicgen_simple_coadd_flag = int(awaicgen_dict["awaicgen_simple_coadd_flag"])
    awaicgen_num_threads = int(awaicgen_dict["awaicgen_num_threads"])
    awaicgen_output_mosaic_image_file = awaicgen_dict["awaicgen_output_mosaic_image_file"]
    awaicgen_output_mosaic_cov_map_file = awaicgen_dict["awaicgen_output_mosaic_cov_map_file"]
    awaicgen_output_mosaic_uncert_image_file = awaicgen_dict["awaicgen_output_mosaic_uncert_image_file"]

    code_to_execute_args = [software_to_execute]
    code_to_execute_args.append("-f1")
    code_to_execute_args.append(awaicgen_input_images_list_file)
    code_to_execute_args.append("-f3")
    code_to_execute_args.append(awaicgen_input_uncert_list_file)
    code_to_execute_args.append("-X")
    code_to_execute_args.append(str(awaicgen_mosaic_size_x))
    code_to_execute_args.append("-Y")
    code_to_execute_args.append(str(awaicgen_mosaic_size_y))
    code_to_execute_args.append("-R")
    code_to_execute_args.append(str(awaicgen_RA_center))
    code_to_execute_args.append("-D")
    code_to_execute_args.append(str(awaicgen_Dec_center))
    code_to_execute_args.append("-C")
    code_to_execute_args.append(str(awaicgen_mosaic_rotation))
    code_to_execute_args.append("-pa")
    code_to_execute_args.append(str(awaicgen_pixelscale_absolute))
    code_to_execute_args.append("-wf")
    code_to_execute_args.append(str(awaicgen_inv_var_weight_flag))
    code_to_execute_args.append("-sf")
    code_to_execute_args.append(str(awaicgen_pixelflux_scale_flag))
    code_to_execute_args.append("-sc")
    code_to_execute_args.append(str(awaicgen_simple_coadd_flag))
    code_to_execute_args.append("-nt")
    code_to_execute_args.append(str(awaicgen_num_threads))
    code_to_execute_args.append("-o1")
    code_to_execute_args.append(awaicgen_output_mosaic_image_file)
    code_to_execute_args.append("-o2")
    code_to_execute_args.append(awaicgen_output_mosaic_cov_map_file)
    code_to_execute_args.append("-o3")
    code_to_execute_args.append(awaicgen_output_mosaic_uncert_image_file)
    code_to_execute_args.append("-v")

    print("code_to_execute_args =",code_to_execute_args)

    return code_to_execute_args


#-------------------------------------------------------------------
# Build command line for swarp.

def build_swarp_command_line_args(swarp_dict):

    '''
    Build swarp command line.
    '''

    software_to_execute = 'swarp'

    swarp_input_image = swarp_dict["swarp_input_image".lower()]
    swarp_IMAGEOUT_NAME = swarp_dict["swarp_IMAGEOUT_NAME".lower()]
    swarp_WEIGHTOUT_NAME = swarp_dict["swarp_WEIGHTOUT_NAME".lower()]
    swarp_HEADER_ONLY = swarp_dict["swarp_HEADER_ONLY".lower()]
    swarp_HEADER_SUFFIX = swarp_dict["swarp_HEADER_SUFFIX".lower()]
    swarp_WEIGHT_TYPE = swarp_dict["swarp_WEIGHT_TYPE".lower()]
    swarp_RESCALE_WEIGHTS = swarp_dict["swarp_RESCALE_WEIGHTS".lower()]
    swarp_WEIGHT_SUFFIX = swarp_dict["swarp_WEIGHT_SUFFIX".lower()]
    swarp_WEIGHT_IMAGE = swarp_dict["swarp_WEIGHT_IMAGE".lower()]
    swarp_WEIGHT_THRESH = swarp_dict["swarp_WEIGHT_THRESH".lower()]
    swarp_COMBINE = swarp_dict["swarp_COMBINE".lower()]
    swarp_COMBINE_TYPE = swarp_dict["swarp_COMBINE_TYPE".lower()]
    swarp_CLIP_AMPFRAC = swarp_dict["swarp_CLIP_AMPFRAC".lower()]
    swarp_CLIP_SIGMA = swarp_dict["swarp_CLIP_SIGMA".lower()]
    swarp_CLIP_WRITELOG = swarp_dict["swarp_CLIP_WRITELOG".lower()]
    swarp_CLIP_LOGNAME = swarp_dict["swarp_CLIP_LOGNAME".lower()]
    swarp_BLANK_BADPIXELS = swarp_dict["swarp_BLANK_BADPIXELS".lower()]
    swarp_CELESTIAL_TYPE = swarp_dict["swarp_CELESTIAL_TYPE".lower()]
    swarp_PROJECTION_TYPE = swarp_dict["swarp_PROJECTION_TYPE".lower()]
    swarp_PROJECTION_ERR = swarp_dict["swarp_PROJECTION_ERR".lower()]
    swarp_CENTER_TYPE = swarp_dict["swarp_CENTER_TYPE".lower()]
    swarp_CENTER = swarp_dict["swarp_CENTER".lower()]
    swarp_PIXELSCALE_TYPE = swarp_dict["swarp_PIXELSCALE_TYPE".lower()]
    swarp_PIXEL_SCALE = swarp_dict["swarp_PIXEL_SCALE".lower()]
    swarp_IMAGE_SIZE = swarp_dict["swarp_IMAGE_SIZE".lower()]
    swarp_RESAMPLE = swarp_dict["swarp_RESAMPLE".lower()]
    swarp_RESAMPLE_DIR = swarp_dict["swarp_RESAMPLE_DIR".lower()]
    swarp_RESAMPLE_SUFFIX = swarp_dict["swarp_RESAMPLE_SUFFIX".lower()]
    swarp_RESAMPLING_TYPE = swarp_dict["swarp_RESAMPLING_TYPE".lower()]
    swarp_OVERSAMPLING = swarp_dict["swarp_OVERSAMPLING".lower()]
    swarp_INTERPOLATE = swarp_dict["swarp_INTERPOLATE".lower()]
    swarp_FSCALASTRO_TYPE = swarp_dict["swarp_FSCALASTRO_TYPE".lower()]
    swarp_FSCALE_KEYWORD = swarp_dict["swarp_FSCALE_KEYWORD".lower()]
    swarp_FSCALE_DEFAULT = swarp_dict["swarp_FSCALE_DEFAULT".lower()]
    swarp_GAIN_KEYWORD = swarp_dict["swarp_GAIN_KEYWORD".lower()]
    swarp_GAIN_DEFAULT = swarp_dict["swarp_GAIN_DEFAULT".lower()]
    swarp_SATLEV_KEYWORD = swarp_dict["swarp_SATLEV_KEYWORD".lower()]
    swarp_SATLEV_DEFAULT = swarp_dict["swarp_SATLEV_DEFAULT".lower()]
    swarp_SUBTRACT_BACK = swarp_dict["swarp_SUBTRACT_BACK".lower()]
    swarp_BACK_TYPE = swarp_dict["swarp_BACK_TYPE".lower()]
    swarp_BACK_DEFAULT = swarp_dict["swarp_BACK_DEFAULT".lower()]
    swarp_BACK_SIZE = swarp_dict["swarp_BACK_SIZE".lower()]
    swarp_BACK_FILTERSIZE = swarp_dict["swarp_BACK_FILTERSIZE".lower()]
    swarp_BACK_FILTTHRESH = swarp_dict["swarp_BACK_FILTTHRESH".lower()]
    swarp_VMEM_DIR = swarp_dict["swarp_VMEM_DIR".lower()]
    swarp_VMEM_MAX = swarp_dict["swarp_VMEM_MAX".lower()]
    swarp_MEM_MAX = swarp_dict["swarp_MEM_MAX".lower()]
    swarp_COMBINE_BUFSIZE  = swarp_dict["swarp_COMBINE_BUFSIZE".lower()]
    swarp_DELETE_TMPFILES = swarp_dict["swarp_DELETE_TMPFILES".lower()]
    swarp_COPY_KEYWORDS = swarp_dict["swarp_COPY_KEYWORDS".lower()]
    swarp_WRITE_FILEINFO = swarp_dict["swarp_WRITE_FILEINFO".lower()]
    swarp_WRITE_XML = swarp_dict["swarp_WRITE_XML".lower()]
    swarp_VERBOSE_TYPE = swarp_dict["swarp_VERBOSE_TYPE".lower()]
    swarp_NNODES = swarp_dict["swarp_NNODES".lower()]
    swarp_NODE_INDEX = swarp_dict["swarp_NODE_INDEX".lower()]
    swarp_NTHREADS = swarp_dict["swarp_NTHREADS".lower()]
    swarp_NOPENFILES_MAX = swarp_dict["swarp_NOPENFILES_MAX".lower()]

    code_to_execute_args = [software_to_execute]
    code_to_execute_args.append(swarp_input_image)
    code_to_execute_args.append("-IMAGEOUT_NAME")
    code_to_execute_args.append(swarp_IMAGEOUT_NAME)
    code_to_execute_args.append("-WEIGHTOUT_NAME")
    code_to_execute_args.append(swarp_WEIGHTOUT_NAME)
    code_to_execute_args.append("-HEADER_ONLY")
    code_to_execute_args.append(swarp_HEADER_ONLY)
    code_to_execute_args.append("-HEADER_SUFFIX")
    code_to_execute_args.append(swarp_HEADER_SUFFIX)
    code_to_execute_args.append("-WEIGHT_TYPE")
    code_to_execute_args.append(swarp_WEIGHT_TYPE)
    code_to_execute_args.append("-RESCALE_WEIGHTS")
    code_to_execute_args.append(swarp_RESCALE_WEIGHTS)
    code_to_execute_args.append("-WEIGHT_SUFFIX")
    code_to_execute_args.append(swarp_WEIGHT_SUFFIX)
    code_to_execute_args.append("-WEIGHT_IMAGE")
    code_to_execute_args.append(swarp_WEIGHT_IMAGE)
    code_to_execute_args.append("-WEIGHT_THRESH")
    code_to_execute_args.append(swarp_WEIGHT_THRESH)
    code_to_execute_args.append("-COMBINE")
    code_to_execute_args.append(swarp_COMBINE)
    code_to_execute_args.append("-COMBINE_TYPE")
    code_to_execute_args.append(swarp_COMBINE_TYPE)
    code_to_execute_args.append("-CLIP_AMPFRAC")
    code_to_execute_args.append(swarp_CLIP_AMPFRAC)
    code_to_execute_args.append("-CLIP_SIGMA")
    code_to_execute_args.append(swarp_CLIP_SIGMA)
    code_to_execute_args.append("-CLIP_WRITELOG")
    code_to_execute_args.append(swarp_CLIP_WRITELOG)
    code_to_execute_args.append("-CLIP_LOGNAME")
    code_to_execute_args.append(swarp_CLIP_LOGNAME)
    code_to_execute_args.append("-BLANK_BADPIXELS")
    code_to_execute_args.append(swarp_BLANK_BADPIXELS)
    code_to_execute_args.append("-CELESTIAL_TYPE")
    code_to_execute_args.append(swarp_CELESTIAL_TYPE)
    code_to_execute_args.append("-PROJECTION_TYPE")
    code_to_execute_args.append(swarp_PROJECTION_TYPE)
    code_to_execute_args.append("-PROJECTION_ERR")
    code_to_execute_args.append(swarp_PROJECTION_ERR)
    code_to_execute_args.append("-CENTER_TYPE")
    code_to_execute_args.append(swarp_CENTER_TYPE)
    code_to_execute_args.append("-CENTER")
    code_to_execute_args.append(swarp_CENTER)
    code_to_execute_args.append("-PIXELSCALE_TYPE")
    code_to_execute_args.append(swarp_PIXELSCALE_TYPE)
    code_to_execute_args.append("-PIXEL_SCALE")
    code_to_execute_args.append(swarp_PIXEL_SCALE)
    code_to_execute_args.append("-IMAGE_SIZE")
    code_to_execute_args.append(swarp_IMAGE_SIZE)
    code_to_execute_args.append("-RESAMPLE")
    code_to_execute_args.append(swarp_RESAMPLE)
    code_to_execute_args.append("-RESAMPLE_DIR")
    code_to_execute_args.append(swarp_RESAMPLE_DIR)
    code_to_execute_args.append("-RESAMPLE_SUFFIX")
    code_to_execute_args.append(swarp_RESAMPLE_SUFFIX)
    code_to_execute_args.append("-RESAMPLING_TYPE")
    code_to_execute_args.append(swarp_RESAMPLING_TYPE)
    code_to_execute_args.append("-OVERSAMPLING")
    code_to_execute_args.append(swarp_OVERSAMPLING)
    code_to_execute_args.append("-INTERPOLATE")
    code_to_execute_args.append(swarp_INTERPOLATE)
    code_to_execute_args.append("-FSCALASTRO_TYPE")
    code_to_execute_args.append(swarp_FSCALASTRO_TYPE)
    code_to_execute_args.append("-FSCALE_KEYWORD")
    code_to_execute_args.append(swarp_FSCALE_KEYWORD)
    code_to_execute_args.append("-FSCALE_DEFAULT")
    code_to_execute_args.append(swarp_FSCALE_DEFAULT)
    code_to_execute_args.append("-GAIN_KEYWORD")
    code_to_execute_args.append(swarp_GAIN_KEYWORD)
    code_to_execute_args.append("-GAIN_DEFAULT")
    code_to_execute_args.append(swarp_GAIN_DEFAULT)
    code_to_execute_args.append("-SATLEV_KEYWORD")
    code_to_execute_args.append(swarp_SATLEV_KEYWORD)
    code_to_execute_args.append("-SATLEV_DEFAULT")
    code_to_execute_args.append(swarp_SATLEV_DEFAULT)
    code_to_execute_args.append("-SUBTRACT_BACK")
    code_to_execute_args.append(swarp_SUBTRACT_BACK)
    code_to_execute_args.append("-BACK_TYPE")
    code_to_execute_args.append(swarp_BACK_TYPE)
    code_to_execute_args.append("-BACK_DEFAULT")
    code_to_execute_args.append(swarp_BACK_DEFAULT)
    code_to_execute_args.append("-BACK_SIZE")
    code_to_execute_args.append(swarp_BACK_SIZE)
    code_to_execute_args.append("-BACK_FILTERSIZE")
    code_to_execute_args.append(swarp_BACK_FILTERSIZE)
    code_to_execute_args.append("-BACK_FILTTHRESH")
    code_to_execute_args.append(swarp_BACK_FILTTHRESH)
    code_to_execute_args.append("-VMEM_DIR")
    code_to_execute_args.append(swarp_VMEM_DIR)
    code_to_execute_args.append("-VMEM_MAX")
    code_to_execute_args.append(swarp_VMEM_MAX)
    code_to_execute_args.append("-MEM_MAX")
    code_to_execute_args.append(swarp_MEM_MAX)
    code_to_execute_args.append("-COMBINE_BUFSIZE ")
    code_to_execute_args.append(swarp_COMBINE_BUFSIZE )
    code_to_execute_args.append("-DELETE_TMPFILES")
    code_to_execute_args.append(swarp_DELETE_TMPFILES)
    code_to_execute_args.append("-COPY_KEYWORDS")
    code_to_execute_args.append(swarp_COPY_KEYWORDS)
    code_to_execute_args.append("-WRITE_FILEINFO")
    code_to_execute_args.append(swarp_WRITE_FILEINFO)
    code_to_execute_args.append("-WRITE_XML")
    code_to_execute_args.append(swarp_WRITE_XML)
    code_to_execute_args.append("-VERBOSE_TYPE")
    code_to_execute_args.append(swarp_VERBOSE_TYPE)
    code_to_execute_args.append("-NNODES")
    code_to_execute_args.append(swarp_NNODES)
    code_to_execute_args.append("-NODE_INDEX")
    code_to_execute_args.append(swarp_NODE_INDEX)
    code_to_execute_args.append("-NTHREADS")
    code_to_execute_args.append(swarp_NTHREADS)
    code_to_execute_args.append("-NOPENFILES_MAX")
    code_to_execute_args.append(swarp_NOPENFILES_MAX)

    print("code_to_execute_args =",code_to_execute_args)

    return code_to_execute_args


#-------------------------------------------------------------------
# Given FITS file with sip distortion, create corresponding new
# FITS file with pv distortion (with image data moved to PRIMARY header).

def convert_from_sip_to_pv(input_fits_file_with_sip,hdu_index,output_fits_file_with_pv):
    '''
    hdu_index is zero-based, and corresponds to HDU with image data.
    '''

    hdul = fits.open(input_fits_file_with_sip)

    sip_header = hdul[hdu_index].header

    if debug:
        print("Type for sip_header =",type(sip_header))

    sip_to_pv(sip_header)

    hdul[hdu_index].header = sip_header

    new_hdu = fits.PrimaryHDU(data=hdul[hdu_index].data,header=hdul[hdu_index].header)

    new_hdu.writeto(output_fits_file_with_pv,overwrite=True,checksum=True)


#-------------------------------------------------------------------
# Resample reference image and its coverage map and uncertainty image,
# with either no distortion or sip distortion to the reference frame
# of thescience FITS file with sip distortion.
# Since swarp is utilized, both output science and reference images
# are converted to pv distortion, as necessary.
# Provide input hdu indices (zero based) for where input image data reside.
# Output images are moved to PRIMARY header.

def resample_reference_image_to_science_image_with_pv_distortion(
    input_science_image,
    hdu_index_for_science_image_data,
    input_reference_image,
    input_reference_cov_map,
    input_reference_uncert_image,
    hdu_index_for_reference_image_data,
    pv_convert_flag_for_reference_image_data,
    swarp_dict):


    # Output resampled reference image.
    output_resampled_reference_image = input_reference_image.replace(".fits","_resampled.fits")
    output_resampled_reference_cov_map = input_reference_cov_map.replace(".fits","_resampled.fits")
    output_resampled_reference_uncert_image = input_reference_uncert_image.replace(".fits","_resampled.fits")

    print("input_science_image =",input_science_image)
    print("input_reference_image =",input_reference_image)
    print("input_reference_cov_map =",input_reference_cov_map)
    print("input_reference_uncert_image =",input_reference_uncert_image)
    print("output_resampled_reference_image =",output_resampled_reference_image)
    print("output_resampled_reference_cov_map =",output_resampled_reference_cov_map)
    print("output_resampled_reference_uncert_image =",output_resampled_reference_uncert_image)


    # Convert sip distortion to pv distortion.

    sci_img_fits_file_with_pv = input_science_image.replace(".fits","_pv.fits")
    ref_img_fits_file_with_pv = input_reference_image.replace(".fits","_pv.fits")
    ref_cov_fits_file_with_pv = input_reference_cov_map.replace(".fits","_pv.fits")
    ref_uncert_fits_file_with_pv = input_reference_uncert_image.replace(".fits","_pv.fits")


    convert_from_sip_to_pv(input_science_image,hdu_index_for_science_image_data,sci_img_fits_file_with_pv)

    if pv_convert_flag_for_reference_image_data:
        convert_from_sip_to_pv(input_reference_image,hdu_index_for_reference_image_data,ref_img_fits_file_with_pv)
        convert_from_sip_to_pv(input_reference_cov_map,hdu_index_for_reference_image_data,ref_cov_fits_file_with_pv)
        convert_from_sip_to_pv(input_reference_uncert_image,hdu_index_for_reference_image_data,ref_uncert_fits_file_with_pv)


    # Output weight files.

    output_weight_file = output_resampled_reference_image.replace(".fits","_wt.fits")
    output_cov_weight_file = output_resampled_reference_cov_map.replace(".fits","_wt.fits")
    output_uncert_weight_file = output_resampled_reference_uncert_image.replace(".fits","_wt.fits")


    # Create symlink to sci_img_fits_file_with_pv from output_resampled_reference_image,
    # which requires the .head filename suffix, in order to be fed implicitly into swarp.

    distort_grid_header_file_symlink = output_resampled_reference_image.replace(".fits",".head")

    print("distort_grid_header_file_symlink =",distort_grid_header_file_symlink)

    if os.path.islink(distort_grid_header_file_symlink):
        os.unlink(distort_grid_header_file_symlink)

    os.symlink(sci_img_fits_file_with_pv, distort_grid_header_file_symlink)


    # Repeat for reference coverage map.

    distort_grid_header_file_symlink = output_resampled_reference_cov_map.replace(".fits",".head")

    print("distort_grid_header_file_symlink =",distort_grid_header_file_symlink)

    if os.path.islink(distort_grid_header_file_symlink):
        os.unlink(distort_grid_header_file_symlink)

    os.symlink(sci_img_fits_file_with_pv, distort_grid_header_file_symlink)


    # Repeat for reference uncertainty image.

    distort_grid_header_file_symlink = output_resampled_reference_uncert_image.replace(".fits",".head")

    print("distort_grid_header_file_symlink =",distort_grid_header_file_symlink)

    if os.path.islink(distort_grid_header_file_symlink):
        os.unlink(distort_grid_header_file_symlink)

    os.symlink(sci_img_fits_file_with_pv, distort_grid_header_file_symlink)


    # Swarp the reference image.

    if pv_convert_flag_for_reference_image_data:
        swarp_dict["swarp_input_image".lower()] = ref_img_fits_file_with_pv
    else:
        swarp_dict["swarp_input_image".lower()] = input_reference_image

    swarp_dict["swarp_IMAGEOUT_NAME".lower()] = output_resampled_reference_image
    swarp_dict["swarp_WEIGHTOUT_NAME".lower()] = output_weight_file


    # Execute swarp for the reference image.

    swarp_cmd = build_swarp_command_line_args(swarp_dict)
    exitcode_from_swarp = execute_command(swarp_cmd)


    # Override swarp-parameter dictionary to disable background subtraction
    # for both reference coverage map and reference uncertainty image.

    swarp_SUBTRACT_BACK = 'N'
    swarp_BACK_TYPE = 'MANUAL'
    swarp_BACK_DEFAULT = '0.0'

    swarp_dict["swarp_SUBTRACT_BACK".lower()] = swarp_SUBTRACT_BACK
    swarp_dict["swarp_BACK_TYPE".lower()] = swarp_BACK_TYPE
    swarp_dict["swarp_BACK_DEFAULT".lower()] = swarp_BACK_DEFAULT


    # Swarp the reference coverage map.

    if pv_convert_flag_for_reference_image_data:
        swarp_dict["swarp_input_image".lower()] = ref_cov_fits_file_with_pv
    else:
        swarp_dict["swarp_input_image".lower()] = input_reference_cov_map

    swarp_dict["swarp_IMAGEOUT_NAME".lower()] = output_resampled_reference_cov_map
    swarp_dict["swarp_WEIGHTOUT_NAME".lower()] = output_cov_weight_file


    # Execute swarp for the reference coverage map.

    swarp_cmd = build_swarp_command_line_args(swarp_dict)
    exitcode_from_swarp = execute_command(swarp_cmd)


    # Swarp the reference uncertainty image.

    if pv_convert_flag_for_reference_image_data:
        swarp_dict["swarp_input_image".lower()] = ref_uncert_fits_file_with_pv
    else:
        swarp_dict["swarp_input_image".lower()] = input_reference_uncert_image

    swarp_dict["swarp_IMAGEOUT_NAME".lower()] = output_resampled_reference_uncert_image
    swarp_dict["swarp_WEIGHTOUT_NAME".lower()] = output_uncert_weight_file


    # Execute swarp for the reference uncertainty image.

    swarp_cmd = build_swarp_command_line_args(swarp_dict)
    exitcode_from_swarp = execute_command(swarp_cmd)


    # Return select filenames (in case the files need to be uploaded to the S3 product bucket for examination).

    return sci_img_fits_file_with_pv,\
        ref_img_fits_file_with_pv,\
        ref_cov_fits_file_with_pv,\
        ref_uncert_fits_file_with_pv,\
        output_resampled_reference_image,\
        output_resampled_reference_cov_map,\
        output_resampled_reference_uncert_image


#-------------------------------------------------------------------
# Build command line for sextractor.

def build_sextractor_command_line_args(sextractor_dict):

    '''
    Build sextractor command line.
    '''

    software_to_execute = 'sex'

    sextractor_detection_image = sextractor_dict["sextractor_detection_image".lower()]
    sextractor_input_image = sextractor_dict["sextractor_input_image".lower()]
    sextractor_CATALOG_NAME = sextractor_dict["sextractor_CATALOG_NAME".lower()]
    sextractor_CATALOG_TYPE = sextractor_dict["sextractor_CATALOG_TYPE".lower()]
    sextractor_PARAMETERS_NAME = sextractor_dict["sextractor_PARAMETERS_NAME".lower()]
    sextractor_DETECT_TYPE = sextractor_dict["sextractor_DETECT_TYPE".lower()]
    sextractor_DETECT_MINAREA = sextractor_dict["sextractor_DETECT_MINAREA".lower()]
    sextractor_DETECT_MAXAREA = sextractor_dict["sextractor_DETECT_MAXAREA".lower()]
    sextractor_THRESH_TYPE = sextractor_dict["sextractor_THRESH_TYPE".lower()]
    sextractor_DETECT_THRESH = sextractor_dict["sextractor_DETECT_THRESH".lower()]
    sextractor_ANALYSIS_THRESH = sextractor_dict["sextractor_ANALYSIS_THRESH".lower()]
    sextractor_FILTER = sextractor_dict["sextractor_FILTER".lower()]
    sextractor_FILTER_NAME = sextractor_dict["sextractor_FILTER_NAME".lower()]
    sextractor_FILTER_THRESH = sextractor_dict["sextractor_FILTER_THRESH".lower()]
    sextractor_DEBLEND_NTHRESH = sextractor_dict["sextractor_DEBLEND_NTHRESH".lower()]
    sextractor_DEBLEND_MINCONT = sextractor_dict["sextractor_DEBLEND_MINCONT".lower()]
    sextractor_CLEAN = sextractor_dict["sextractor_CLEAN".lower()]
    sextractor_CLEAN_PARAM = sextractor_dict["sextractor_CLEAN_PARAM".lower()]
    sextractor_MASK_TYPE = sextractor_dict["sextractor_MASK_TYPE".lower()]
    sextractor_WEIGHT_TYPE = sextractor_dict["sextractor_WEIGHT_TYPE".lower()]
    sextractor_RESCALE_WEIGHTS = sextractor_dict["sextractor_RESCALE_WEIGHTS".lower()]
    sextractor_WEIGHT_IMAGE = sextractor_dict["sextractor_WEIGHT_IMAGE".lower()]
    sextractor_WEIGHT_GAIN = sextractor_dict["sextractor_WEIGHT_GAIN".lower()]
    sextractor_WEIGHT_THRESH = sextractor_dict["sextractor_WEIGHT_THRESH".lower()]
    sextractor_FLAG_IMAGE = sextractor_dict["sextractor_FLAG_IMAGE".lower()]
    sextractor_FLAG_TYPE = sextractor_dict["sextractor_FLAG_TYPE".lower()]
    sextractor_PHOT_APERTURES = sextractor_dict["sextractor_PHOT_APERTURES".lower()]
    sextractor_PHOT_AUTOPARAMS = sextractor_dict["sextractor_PHOT_AUTOPARAMS".lower()]
    sextractor_PHOT_PETROPARAMS = sextractor_dict["sextractor_PHOT_PETROPARAMS".lower()]
    sextractor_PHOT_AUTOAPERS = sextractor_dict["sextractor_PHOT_AUTOAPERS".lower()]
    sextractor_PHOT_FLUXFRAC = sextractor_dict["sextractor_PHOT_FLUXFRAC".lower()]
    sextractor_SATUR_LEVEL = sextractor_dict["sextractor_SATUR_LEVEL".lower()]
    sextractor_SATUR_KEY = sextractor_dict["sextractor_SATUR_KEY".lower()]
    sextractor_MAG_ZEROPOINT = sextractor_dict["sextractor_MAG_ZEROPOINT".lower()]
    sextractor_MAG_GAMMA = sextractor_dict["sextractor_MAG_GAMMA".lower()]
    sextractor_GAIN = sextractor_dict["sextractor_GAIN".lower()]
    sextractor_GAIN_KEY = sextractor_dict["sextractor_GAIN_KEY".lower()]
    sextractor_PIXEL_SCALE = sextractor_dict["sextractor_PIXEL_SCALE".lower()]
    sextractor_SEEING_FWHM = sextractor_dict["sextractor_SEEING_FWHM".lower()]
    sextractor_STARNNW_NAME = sextractor_dict["sextractor_STARNNW_NAME".lower()]
    sextractor_BACK_TYPE = sextractor_dict["sextractor_BACK_TYPE".lower()]
    sextractor_BACK_VALUE = sextractor_dict["sextractor_BACK_VALUE".lower()]
    sextractor_BACK_SIZE = sextractor_dict["sextractor_BACK_SIZE".lower()]
    sextractor_BACK_FILTERSIZE = sextractor_dict["sextractor_BACK_FILTERSIZE".lower()]
    sextractor_BACKPHOTO_TYPE = sextractor_dict["sextractor_BACKPHOTO_TYPE".lower()]
    sextractor_BACKPHOTO_THICK = sextractor_dict["sextractor_BACKPHOTO_THICK".lower()]
    sextractor_BACK_FILTTHRESH = sextractor_dict["sextractor_BACK_FILTTHRESH".lower()]
    sextractor_CHECKIMAGE_TYPE = sextractor_dict["sextractor_CHECKIMAGE_TYPE".lower()]
    sextractor_CHECKIMAGE_NAME = sextractor_dict["sextractor_CHECKIMAGE_NAME".lower()]
    sextractor_MEMORY_OBJSTACK = sextractor_dict["sextractor_MEMORY_OBJSTACK".lower()]
    sextractor_MEMORY_PIXSTACK = sextractor_dict["sextractor_MEMORY_PIXSTACK".lower()]
    sextractor_MEMORY_BUFSIZE = sextractor_dict["sextractor_MEMORY_BUFSIZE".lower()]
    sextractor_ASSOC_NAME = sextractor_dict["sextractor_ASSOC_NAME".lower()]
    sextractor_ASSOC_DATA = sextractor_dict["sextractor_ASSOC_DATA".lower()]
    sextractor_ASSOC_PARAMS = sextractor_dict["sextractor_ASSOC_PARAMS".lower()]
    sextractor_ASSOCCOORD_TYPE = sextractor_dict["sextractor_ASSOCCOORD_TYPE".lower()]
    sextractor_ASSOC_RADIUS = sextractor_dict["sextractor_ASSOC_RADIUS".lower()]
    sextractor_ASSOC_TYPE = sextractor_dict["sextractor_ASSOC_TYPE".lower()]
    sextractor_ASSOCSELEC_TYPE = sextractor_dict["sextractor_ASSOCSELEC_TYPE".lower()]
    sextractor_VERBOSE_TYPE = sextractor_dict["sextractor_VERBOSE_TYPE".lower()]
    sextractor_HEADER_SUFFIX = sextractor_dict["sextractor_HEADER_SUFFIX".lower()]
    sextractor_WRITE_XML = sextractor_dict["sextractor_WRITE_XML".lower()]
    sextractor_NTHREADS = sextractor_dict["sextractor_NTHREADS".lower()]
    sextractor_FITS_UNSIGNED = sextractor_dict["sextractor_FITS_UNSIGNED".lower()]
    sextractor_INTERP_MAXXLAG = sextractor_dict["sextractor_INTERP_MAXXLAG".lower()]
    sextractor_INTERP_MAXYLAG = sextractor_dict["sextractor_INTERP_MAXYLAG".lower()]
    sextractor_INTERP_TYPE = sextractor_dict["sextractor_INTERP_TYPE".lower()]

    code_to_execute_args = [software_to_execute]
    code_to_execute_args.append(sextractor_detection_image + "," + sextractor_input_image)
    code_to_execute_args.append("-CATALOG_NAME")
    code_to_execute_args.append(sextractor_CATALOG_NAME)
    code_to_execute_args.append("-CATALOG_TYPE")
    code_to_execute_args.append(sextractor_CATALOG_TYPE)
    code_to_execute_args.append("-PARAMETERS_NAME")
    code_to_execute_args.append(sextractor_PARAMETERS_NAME)
    code_to_execute_args.append("-DETECT_TYPE")
    code_to_execute_args.append(sextractor_DETECT_TYPE)
    code_to_execute_args.append("-DETECT_MINAREA")
    code_to_execute_args.append(sextractor_DETECT_MINAREA)
    code_to_execute_args.append("-DETECT_MAXAREA")
    code_to_execute_args.append(sextractor_DETECT_MAXAREA)
    code_to_execute_args.append("-THRESH_TYPE")
    code_to_execute_args.append(sextractor_THRESH_TYPE)
    code_to_execute_args.append("-DETECT_THRESH")
    code_to_execute_args.append(sextractor_DETECT_THRESH)
    code_to_execute_args.append("-ANALYSIS_THRESH")
    code_to_execute_args.append(sextractor_ANALYSIS_THRESH)
    code_to_execute_args.append("-FILTER")
    code_to_execute_args.append(sextractor_FILTER)
    code_to_execute_args.append("-FILTER_NAME")
    code_to_execute_args.append(sextractor_FILTER_NAME)
    code_to_execute_args.append("-FILTER_THRESH")
    code_to_execute_args.append(sextractor_FILTER_THRESH)
    code_to_execute_args.append("-DEBLEND_NTHRESH")
    code_to_execute_args.append(sextractor_DEBLEND_NTHRESH)
    code_to_execute_args.append("-DEBLEND_MINCONT")
    code_to_execute_args.append(sextractor_DEBLEND_MINCONT)
    code_to_execute_args.append("-CLEAN")
    code_to_execute_args.append(sextractor_CLEAN)
    code_to_execute_args.append("-CLEAN_PARAM")
    code_to_execute_args.append(sextractor_CLEAN_PARAM)
    code_to_execute_args.append("-MASK_TYPE")
    code_to_execute_args.append(sextractor_MASK_TYPE)
    code_to_execute_args.append("-WEIGHT_TYPE")
    code_to_execute_args.append(sextractor_WEIGHT_TYPE)
    code_to_execute_args.append("-RESCALE_WEIGHTS")
    code_to_execute_args.append(sextractor_RESCALE_WEIGHTS)
    code_to_execute_args.append("-WEIGHT_IMAGE")
    code_to_execute_args.append(sextractor_WEIGHT_IMAGE)
    code_to_execute_args.append("-WEIGHT_GAIN")
    code_to_execute_args.append(sextractor_WEIGHT_GAIN)
    code_to_execute_args.append("-WEIGHT_THRESH")
    code_to_execute_args.append(sextractor_WEIGHT_THRESH)
    ########################################################################### OMIT FLAGS
    #code_to_execute_args.append("-FLAG_IMAGE")
    #code_to_execute_args.append(sextractor_FLAG_IMAGE)
    #code_to_execute_args.append("-FLAG_TYPE")
    #code_to_execute_args.append(sextractor_FLAG_TYPE)
    ###########################################################################
    code_to_execute_args.append("-PHOT_APERTURES")
    code_to_execute_args.append(sextractor_PHOT_APERTURES)
    code_to_execute_args.append("-PHOT_AUTOPARAMS")
    code_to_execute_args.append(sextractor_PHOT_AUTOPARAMS)
    code_to_execute_args.append("-PHOT_PETROPARAMS")
    code_to_execute_args.append(sextractor_PHOT_PETROPARAMS)
    code_to_execute_args.append("-PHOT_AUTOAPERS")
    code_to_execute_args.append(sextractor_PHOT_AUTOAPERS)
    code_to_execute_args.append("-PHOT_FLUXFRAC")
    code_to_execute_args.append(sextractor_PHOT_FLUXFRAC)
    code_to_execute_args.append("-SATUR_LEVEL")
    code_to_execute_args.append(sextractor_SATUR_LEVEL)
    code_to_execute_args.append("-SATUR_KEY")
    code_to_execute_args.append(sextractor_SATUR_KEY)
    code_to_execute_args.append("-MAG_ZEROPOINT")
    code_to_execute_args.append(sextractor_MAG_ZEROPOINT)
    code_to_execute_args.append("-MAG_GAMMA")
    code_to_execute_args.append(sextractor_MAG_GAMMA)
    code_to_execute_args.append("-GAIN")
    code_to_execute_args.append(sextractor_GAIN)
    code_to_execute_args.append("-GAIN_KEY")
    code_to_execute_args.append(sextractor_GAIN_KEY)
    code_to_execute_args.append("-PIXEL_SCALE")
    code_to_execute_args.append(sextractor_PIXEL_SCALE)
    code_to_execute_args.append("-SEEING_FWHM")
    code_to_execute_args.append(sextractor_SEEING_FWHM)
    code_to_execute_args.append("-STARNNW_NAME")
    code_to_execute_args.append(sextractor_STARNNW_NAME)
    code_to_execute_args.append("-BACK_TYPE")
    code_to_execute_args.append(sextractor_BACK_TYPE)
    code_to_execute_args.append("-BACK_VALUE")
    code_to_execute_args.append(sextractor_BACK_VALUE)
    code_to_execute_args.append("-BACK_SIZE")
    code_to_execute_args.append(sextractor_BACK_SIZE)
    code_to_execute_args.append("-BACK_FILTERSIZE")
    code_to_execute_args.append(sextractor_BACK_FILTERSIZE)
    code_to_execute_args.append("-BACKPHOTO_TYPE")
    code_to_execute_args.append(sextractor_BACKPHOTO_TYPE)
    code_to_execute_args.append("-BACKPHOTO_THICK")
    code_to_execute_args.append(sextractor_BACKPHOTO_THICK)
    code_to_execute_args.append("-BACK_FILTTHRESH")
    code_to_execute_args.append(sextractor_BACK_FILTTHRESH)
    code_to_execute_args.append("-CHECKIMAGE_TYPE")
    code_to_execute_args.append(sextractor_CHECKIMAGE_TYPE)
    code_to_execute_args.append("-CHECKIMAGE_NAME")
    code_to_execute_args.append(sextractor_CHECKIMAGE_NAME)
    code_to_execute_args.append("-MEMORY_OBJSTACK")
    code_to_execute_args.append(sextractor_MEMORY_OBJSTACK)
    code_to_execute_args.append("-MEMORY_PIXSTACK")
    code_to_execute_args.append(sextractor_MEMORY_PIXSTACK)
    code_to_execute_args.append("-MEMORY_BUFSIZE")
    code_to_execute_args.append(sextractor_MEMORY_BUFSIZE)
    code_to_execute_args.append("-ASSOC_NAME")
    code_to_execute_args.append(sextractor_ASSOC_NAME)
    code_to_execute_args.append("-ASSOC_DATA")
    code_to_execute_args.append(sextractor_ASSOC_DATA)
    code_to_execute_args.append("-ASSOC_PARAMS")
    code_to_execute_args.append(sextractor_ASSOC_PARAMS)
    code_to_execute_args.append("-ASSOCCOORD_TYPE")
    code_to_execute_args.append(sextractor_ASSOCCOORD_TYPE)
    code_to_execute_args.append("-ASSOC_RADIUS")
    code_to_execute_args.append(sextractor_ASSOC_RADIUS)
    code_to_execute_args.append("-ASSOC_TYPE")
    code_to_execute_args.append(sextractor_ASSOC_TYPE)
    code_to_execute_args.append("-ASSOCSELEC_TYPE")
    code_to_execute_args.append(sextractor_ASSOCSELEC_TYPE)
    code_to_execute_args.append("-VERBOSE_TYPE")
    code_to_execute_args.append(sextractor_VERBOSE_TYPE)
    code_to_execute_args.append("-HEADER_SUFFIX")
    code_to_execute_args.append(sextractor_HEADER_SUFFIX)
    code_to_execute_args.append("-WRITE_XML")
    code_to_execute_args.append(sextractor_WRITE_XML)
    code_to_execute_args.append("-NTHREADS")
    code_to_execute_args.append(sextractor_NTHREADS)
    code_to_execute_args.append("-FITS_UNSIGNED")
    code_to_execute_args.append(sextractor_FITS_UNSIGNED)
    code_to_execute_args.append("-INTERP_MAXXLAG")
    code_to_execute_args.append(sextractor_INTERP_MAXXLAG)
    code_to_execute_args.append("-INTERP_MAXYLAG")
    code_to_execute_args.append(sextractor_INTERP_MAXYLAG)
    code_to_execute_args.append("-INTERP_TYPE")
    code_to_execute_args.append(sextractor_INTERP_TYPE)

    print("code_to_execute_args =",code_to_execute_args)

    return code_to_execute_args


def smooth_image_by_local_clipped_averaging(nx,ny,data,x_window = 3,y_window = 3,n_sigma = 3.0):

    x_hwin = int((x_window - 1) / 2)
    y_hwin = int((y_window - 1) / 2)

    smooth_image = np.zeros(shape=(ny, nx))
    smooth_image[:] = np.nan                                              # Initialize 2-D array of NaNs

    for i in range(0,ny):
        for j in range(0,nx):

            if np.isnan(data[i, j]): continue

            data_list = []
            for ii in range(i - y_hwin, i + y_hwin + 1):
                if ((ii < 0) or (ii >= ny)): continue
                for jj in range(j - x_hwin, j + x_hwin + 1):
                    if ((jj < 0) or (jj >= ny)): continue

                    datum = data[ii, jj]

                    #print(datum)

                    if not np.isnan(datum):
                        data_list.append(datum)

            if len(data_list) > 0:

                a = np.array(data_list)

                med = np.median(a)
                p16 = np.percentile(a,16)
                p84 = np.percentile(a,84)
                sigma = 0.5 * (p84 - p16)
                mdmsg = med - n_sigma * sigma
                b = np.less(a,mdmsg)
                mdpsg = med + n_sigma * sigma
                c = np.greater(a,mdpsg)
                mask = b | c
                mx = ma.masked_array(a, mask)
                avg = ma.getdata(mx.mean())

                smooth_image[i, j] = avg.item()

    return smooth_image
