from astropy.io import fits
import matplotlib.pyplot as plt
import numpy as np


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


def plot_sca_outlines(ra,dec):
    for i in range(0,len(ra),4):
        ra_sca = []
        dec_sca = []

        ra_sca.append(ra[i])
        dec_sca.append(dec[i])

        ra_sca.append(ra[i+1])
        dec_sca.append(dec[i+1])

        ra_sca.append(ra[i+2])
        dec_sca.append(dec[i+2])

        ra_sca.append(ra[i+3])
        dec_sca.append(dec[i+3])

        ra_sca.append(ra[i])
        dec_sca.append(dec[i])
    
        plt.plot(ra_sca, dec_sca,"s", **marker_style3)


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

            #if i > 50: break

    print("len(ra_cen) =",len(ra_cen))
    print("len(ra) =",len(ra))

    print("filters =",filters)




    plt.figure(figsize=(8, 8))

    #plot_sca_outlines(ra,dec)
        
    plt.plot(ra_cen, dec_cen,"o", **marker_style, label="Centers")
    
    #plt.plot(ra, dec,"o", **marker_style2, label="Corners")
    
    plt.xlabel('Right Ascension (degrees)')
    plt.ylabel('Declination (degrees)')
    
    plt.title("Roman_TDS Sky Coverage (" + filter + " Filter, All Exposures, All SCAs)")
    #plt.title("Roman_TDS Sky Coverage (" + filter + " Filter, 50 Exposures, All SCAs)")
    
    #plt.legend()

    #plt.show()
    #plt.savefig("pngs/" + filter + "_footprints.png")
    plt.savefig("pngs/" + filter + "_scatterplot.png")


    tol = 0.01
    rmin = min(ra)
    rmax = max(ra) + tol
    dmin = min(dec)
    dmax = max(dec) + tol
    nbins = 40
    delr = (rmax - rmin) / float(nbins)
    deld = (dmax - dmin) / float(nbins)


    # Bin width must be greater than one half the chip width.

    chip_width = 0.110 * 4088 / 3600.0

    print("deld, chip_width =",deld,chip_width)

    if deld < 0.5 * chip_width:

        print("Error: Bin width must be greater than one half the chip width; quitting...\n")
        exit(64)
    

    print("rmin,rmax,delr,dmin,dmax,deld=",rmin,rmax,delr,dmin,dmax,deld)

    rbins = []
    for i in range(nbins):
        r = rmin + float(i) * delr
        rbins.append(r)

    dbins = []
    for i in range(nbins):
        d = dmin + float(i) * deld
        dbins.append(d)

    for r in rbins:
        for d in dbins:
            print("r,d=",r,d)

    data2d = np.zeros(shape=(nbins, nbins))

    for (rval,dval) in zip(ra,dec):

        i = int((dval - dmin) / deld)
        j = int((rval - rmin) / delr)

        #print("rval,dval,j,i =",rval,dval,j,i)
        
        data2d[i,j] += 0.25

        #print("==================>rval,dval,i,j,data2d[i][j] =",rval,dval,i,j,data2d)



    fig, ax = plt.subplots(1,1, figsize=(8, 7))
    im = ax.imshow(data2d,extent=[rmin, rmax, dmin, dmax],cmap='gist_rainbow')
    ax.set_title("Roman_TDS Sky Coverage (" + filter + " Filter, All Exposures, All SCAs)")
    plt.xlabel('Right Ascension (degrees)')
    plt.ylabel('Declination (degrees)')

    fig.colorbar(im, ax=ax, label='Number of exposure-SCAs')

    #plt.show()
    plt.savefig("pngs/" + filter + "_colormap.png")





    
if __name__ == '__main__':

    filter = 'F184'
    parseAndPlotData(filter)

    filter = 'Y106'
    parseAndPlotData(filter)

    filter = 'Z087'
    parseAndPlotData(filter)

    filter = 'R062'
    parseAndPlotData(filter)

    filter = 'H158'
    parseAndPlotData(filter)

    filter = 'K213'
    parseAndPlotData(filter)

    filter = 'J129'
    parseAndPlotData(filter)
