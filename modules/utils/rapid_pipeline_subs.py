import os
import math
from astropy.io import fits
import re


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
