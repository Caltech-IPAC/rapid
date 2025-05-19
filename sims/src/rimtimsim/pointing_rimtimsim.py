from astropy.io import fits
import glob
import matplotlib.pyplot as plt
import modules.utils.rapid_pipeline_subs as util
import numpy as np


fits_files = glob.glob("rimtimsim/rim*.fits")

crval1_list = []
crval2_list = []

ra1_list = []
dec1_list = []

ra2_list = []
dec2_list = []

ra3_list = []
dec3_list = []

ra4_list = []
dec4_list = []

for fits_file in fits_files:

    hdul = fits.open(fits_file)
    hdr = hdul[0].header
    data = hdul[0].data

    crpix1 = hdr["CRPIX1"]
    crpix2 = hdr["CRPIX2"]

    crval1 = hdr["CRVAL1"]
    crval2 = hdr["CRVAL2"]

    crval1_list.append(crval1)
    crval2_list.append(crval2)

    naxis1 = hdr["NAXIS1"]
    naxis2 = hdr["NAXIS2"]

    x1 = 1
    y1 = 1

    x2 = naxis1
    y2 = 1

    x3 = naxis1
    y3 = naxis2

    x4 = 1
    y4 = naxis2

    pc1_1 = hdr["PC1_1"]
    pc1_2 = hdr["PC1_2"]
    pc2_1 = hdr["PC1_1"]
    pc2_2 = hdr["PC2_2"]

    crota2 = np.arctan2(pc1_2,pc1_1) * 180.0 / np.pi
    cdelt1 = 0.11 / 3600.0
    cdelt2 = cdelt1

    ra1,dec1 = util.tan_proj(x1,y1,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)
    ra2,dec2 = util.tan_proj(x2,y2,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)
    ra3,dec3 = util.tan_proj(x3,y3,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)
    ra4,dec4 = util.tan_proj(x4,y4,crpix1,crpix2,crval1,crval2,cdelt1,cdelt2,crota2)

    ra1_list.append(ra1)
    dec1_list.append(dec1)

    ra2_list.append(ra2)
    dec2_list.append(dec2)

    ra3_list.append(ra3)
    dec3_list.append(dec3)

    ra4_list.append(ra4)
    dec4_list.append(dec4)

    print(fits_file,crval1,crval2,naxis1,naxis2,round(crota2,3))



delra = (max(crval1_list) - min(crval1_list)) * 3600.0
deldec = (max(crval2_list) - min(crval2_list)) * 3600.0
delsca = 4088 * 0.11

print("delsca,delra,deldec [arcsec] =",delsca,delra,deldec)

fig = plt.figure(figsize=(8, 8))  # width=8 inches, height=6 inches
ax = fig.add_axes([0.1, 0.1, 0.8, 0.8])  # covers 80% of the figure area

ax.plot(crval1_list, crval2_list, "o")
ax.plot(ra1_list, dec1_list, "x")
ax.plot(ra2_list, dec2_list, "+")
ax.plot(ra3_list, dec3_list, "d")
ax.plot(ra4_list, dec4_list, "^")

#ax.xlim(min(ra1_list),max(crval1_list))
#ax.ylim(min(crval2_list),max(crval2_list))
plt.xlabel("RA (deg)")
plt.ylabel("Dec (deg)")
plt.title("RIMTIMSIM")
plt.show()
