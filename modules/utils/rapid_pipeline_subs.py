import os
import math
from astropy.io import fits
import re
import subprocess

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

    code_to_execute_stdout_stdout = code_to_execute_object.stdout
    print("code_to_execute_stdout_stdout =\n",code_to_execute_stdout_stdout)

    code_to_execute_stdout_stderr = code_to_execute_object.stderr
    print("code_to_execute_stdout_stderr (should be empty since STDERR is combined with STDOUT) =\n",code_to_execute_stdout_stderr)

    return returncode


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


def build_awaicgen_command_line_args(awaicgen_dict):

    '''
    Build awaicgen command line.
    '''

    software_to_execute = 'awaicgen'

    awaicgen_input_images_list_file = awaicgen_dict["awaicgen_input_images_list_file"]
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
