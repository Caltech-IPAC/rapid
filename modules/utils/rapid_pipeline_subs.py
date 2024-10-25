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
    Execute python script.
    '''

    print("execute_command: code_to_execute_args =",code_to_execute_args)


    # Execute code_to_execute.  Not that STDERR and STDOUT are merged into the same data stream.
    # AWS Batch runs Python 3.9.  According to https://docs.python.org/3.9/library/subprocess.html#subprocess.run,
    # if you wish to capture and combine both streams into one, use stdout=PIPE and stderr=STDOUT instead of capture_output.
    # capture_output=False is the default.

    code_to_execute_object = subprocess.run(code_to_execute_args,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)

    returncode = code_to_execute_object.returncode
    print("returncode =",returncode)

    code_to_execute_stdout_stderr = code_to_execute_object.stdout
    print("code_to_execute_stdout_stderr =\n",code_to_execute_stdout_stderr)

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
