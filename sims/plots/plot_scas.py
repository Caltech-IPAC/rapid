from astropy.io import fits
import matplotlib.pyplot as plt
import numpy as np
import sys

import matplotlib.animation as animation

import matplotlib.colors as mcolors

import math

dtr = math.pi / 180.0
rtd = 1.0 / dtr

debug = 1



#-------------------------------------------------------------------
# Given pixel location (x, y) on a tangent plane, compute the corresponding
# sky position (R.A., Dec.), neglecting geometric distortion.
# Requires one-based pixel coordinates (both x,y and crpix1,crpix2).

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


# Roman_TDS_simple_model_F184_10285_11.fits.gz
crpix1 = 2044.0
crpix2 = 2044.0
cd1_1 = -7.2008789991638E-06
cd1_2 = -2.8527273555206E-05
cd2_1 = 2.94433901411525E-05
cd2_2 = -6.7509642552848E-06

cdelt1 = 0.1 / 3600.0
cdelt2 = 0.1 / 3600.0
pa = 0
crota2 = -pa * dtr

cd1_1 = cdelt1 * math.cos(crota2)
cd1_2 = -cdelt2 * math.sin(crota2)
cd2_1 = cdelt1 * math.sin(crota2)
cd2_2 = cdelt2 * math.cos(crota2)

print("cd1_1 =", cd1_1)
print("cd1_2 =", cd1_2)
print("cd2_1 =", cd2_1)
print("cd2_2 =", cd2_2)

# The above formulas approximately reproduce the CD matrix
# for Roman_TDS_simple_model_F184_10285_11.fits.gz
# I'm not sure how the PA was derived, but these results
# indicate that it may be slightly off.
#cd1_1 = -7.200883382546272e-06
#cd1_2 = -2.7975376849487236e-05
#cd2_1 = 2.985183335264445e-05
#cd2_2 = -6.748243027361566e-06


fits_image_filename = "Roman_TDS_obseq_11_6_23.fits"

hdul = fits.open(fits_image_filename)

print(hdul.info())

data = hdul[1].data # assuming the first extension is a table

hdul.close()

fits_image_filename2 = "Roman_TDS_obseq_11_6_23_radec.fits"

hdul = fits.open(fits_image_filename2)

print(hdul.info())

data2 = hdul[1].data # assuming the first extension is a table

hdul.close()



marker_style = dict(linestyle='', color='0.8', markersize=2,
                    markerfacecolor=(1,0,0,0.01), markeredgecolor=(1,0,0,0.01))

marker_style2 = dict(linestyle='', color='0.8', markersize=1,
                    markerfacecolor=(0,0,1,0.01), markeredgecolor=(0,0,1,0.01))

marker_style3 = dict(linestyle='dotted', color='0.5', markersize=0.01,
                    markerfacecolor="tab:blue", markeredgecolor="tab:blue")



def parseAndPlotData(filter):

    print("Parsing...")

    filters = []

    ra_cen = []
    dec_cen = []
    ra = []
    dec = []
    i = 0
    plotFig = True
    for row,row2 in zip(data,data2):

        #print("i,row,row2 =",i,row,row2)

        flag = 0
        for f in filters:
            if f == row[2]: flag = 1

        if flag == 0: filters.append(row[2])

        if row[2] == filter:
        #if row[2] is not None:
            #print(row)

            pa = row[5]

            crota2 = pa

            ra_bore = row[0]
            dec_bore = row[1]

            for j in range(18):

                ra_sca = row2[0][j]
                dec_sca = row2[1][j]

                ra_cen.append(ra_sca)
                dec_cen.append(dec_sca)

                crval1 = ra_sca
                crval2 = dec_sca

                ra1,dec1 = tan_proj(1,1,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)
                ra2,dec2 = tan_proj(4088,1,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)
                ra3,dec3 = tan_proj(4088,4088,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)
                ra4,dec4 = tan_proj(1,4088,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)

                ra.append(ra1)
                dec.append(dec1)

                ra.append(ra2)
                dec.append(dec2)

                ra.append(ra3)
                dec.append(dec3)

                ra.append(ra4)
                dec.append(dec4)

            i += 1

            if i >= 1: break                        # Just plot the first row in each input data file.

    print("len(ra_cen) =",len(ra_cen))
    print("len(ra) =",len(ra))

    print("filters =",filters)


    fig = plt.figure(figsize=(8, 8))
    ax = plt.subplot(111)

    ax.scatter(ra_cen, dec_cen, marker='o', s=12, label="Centers")

    ax.scatter(ra, dec, marker ="x", s=12, label="Corners")

    ax.scatter([ra_bore], [dec_bore], marker ="*", s=36, label="Boresight")

    plt.xlabel('Right Ascension (degrees)')
    plt.ylabel('Declination (degrees)')

    plt.title("Roman WFI Field of View (All SCAs)")

    plt.legend()

    plt.xlim(6.9, 8.4)
    plt.ylim(-46, -45)




    plt.show()


if __name__ == '__main__':

    filters = ['R062', 'Z087', 'Y106', 'J129', 'H158', 'F184', 'K213']

    filter = 'R062'
    parseAndPlotData(filter)

    exit(0)
