from astropy.io import fits
import matplotlib.pyplot as plt
import numpy as np
import sys

import matplotlib.animation as animation

import matplotlib.colors as mcolors

import math

dtr = math.pi / 180.0
rtd = 1.0 / dtr


def tan_proj2(x,y,crpix1,crpix2,crval1,crval2,cd1_1,cd1_2,cd2_1,cd2_2):

    #print("crpix1,crpix2,crval1,crval2,cd1_1,cd1_2,cd2_1,cd2_2 =",crpix1,crpix2,crval1,crval2,cd1_1,cd1_2,cd2_1,cd2_2)

    glong  = crval1
    glat   = crval2

    fsamp = x - crpix1
    fline = y - crpix2

    xx = -(cd1_1 * fsamp + cd1_2 * fline) * dtr
    yy = -(cd2_1 * fsamp + cd2_2 * fline) * dtr

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

cdelt1 = 110.549e-3 / 3600.0
cdelt2 = 103.6e-3 / 3600.0
pa = 256.4381474089074
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

            crota2 = -pa * dtr

            cd1_1 = cdelt1 * math.cos(crota2)
            cd1_2 = -cdelt2 * math.sin(crota2)
            cd2_1 = cdelt1 * math.sin(crota2)
            cd2_2 = cdelt2 * math.cos(crota2)


            for j in range(18):

                ra_sca = row2[0][j]
                dec_sca = row2[1][j]

                ra_cen.append(ra_sca)
                dec_cen.append(dec_sca)

                crval1 = ra_sca
                crval2 = dec_sca

                ra1,dec1 = tan_proj2(1,1,crpix1,crpix2,crval1,crval2,cd1_1,cd1_2,cd2_1,cd2_2)
                ra2,dec2 = tan_proj2(4088,1,crpix1,crpix2,crval1,crval2,cd1_1,cd1_2,cd2_1,cd2_2)
                ra3,dec3 = tan_proj2(4088,4088,crpix1,crpix2,crval1,crval2,cd1_1,cd1_2,cd2_1,cd2_2)
                ra4,dec4 = tan_proj2(1,4088,crpix1,crpix2,crval1,crval2,cd1_1,cd1_2,cd2_1,cd2_2)

                ra.append(ra1)
                dec.append(dec1)
            
                ra.append(ra2)
                dec.append(dec2)
            
                ra.append(ra3)
                dec.append(dec3)
            
                ra.append(ra4)
                dec.append(dec4)
            
            i += 1

            #if i > 1000: break

    print("len(ra_cen) =",len(ra_cen))
    print("len(ra) =",len(ra))

    print("filters =",filters)






    pause_time = 0.1

    num_exposures_to_plot_per_frame = 50
    num_sca_footprints = num_exposures_to_plot_per_frame * 4 * 18

    frames = len(ra) // num_sca_footprints
    rem = len(ra) % num_sca_footprints

    print("frames,rem =",frames,rem)


    nlines = 2 * num_exposures_to_plot_per_frame * 18    # This is the maximum that may be needed, depending on remainder.

    fig, ax = plt.subplots(1,1,figsize=(7, 7))

    x_range = [6,13]
    y_range = [-47,-41]

    
    # Create lines initially without data
    lines = [ax.plot([], [], [])[0] for _ in range(nlines)]

   
    ax.set_xlim(x_range)
    ax.set_ylim(y_range)

    ax.set_xlabel('Right Ascension (degrees)')
    ax.set_ylabel('Declination (degrees)')
    
    ax.title.set_text("Roman_TDS Sky Coverage (" + filter + " Filter, All Exposures, All SCAs)")



    #for i in range(0,frames):
    def animate(i):

        start_slice = i * num_sca_footprints
        end_slice = start_slice + num_sca_footprints

        if i == frames - 1: end_slice += rem
    
            
        my_ra = ra[start_slice:end_slice]
        my_dec = dec[start_slice:end_slice]

        colors = mcolors.XKCD_COLORS
        colors_list_keys = list(colors.keys())

        k = 0
        kk = 0

        for j in range(0,len(my_ra),4):

            ra_sca = []
            dec_sca = []
            
            ra_sca.append(my_ra[j])
            dec_sca.append(my_dec[j])

            ra_sca.append(my_ra[j+1])
            dec_sca.append(my_dec[j+1])

            ra_sca.append(my_ra[j+2])
            dec_sca.append(my_dec[j+2])

            ra_sca.append(my_ra[j+3])
            dec_sca.append(my_dec[j+3])

            ra_sca.append(my_ra[j])
            dec_sca.append(my_dec[j])
    
            lines[k].set_data(ra_sca, dec_sca)  # update the data.
            #lines[k].set_color("red")

            #print("kk = ",kk)
            
            lines[k].set_color(colors_list_keys[kk])

            k += 1

            if k % 18 == 0: kk += 1

        return lines

    print("Done.")


    print("Animation ready to begin: Hit return key to continue...")
    print("(After animation has ended, close graphics window to terminate.)")

    for line in sys.stdin:
        if line.rstrip() is not None:
            break
        print(f'Input : {line}')


    ani = animation.FuncAnimation(fig, animate, interval=frames, blit=True, save_count=frames)

    plt.show()


if __name__ == '__main__':

    filters = ['R062', 'Z087', 'Y106', 'J129', 'H158', 'F184', 'K213']

    filter = 'F184'
    parseAndPlotData(filter)

    exit(0)
