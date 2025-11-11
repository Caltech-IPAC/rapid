import os
import matplotlib.pyplot as plt
import numpy as np
from astropy.io import ascii
from astropy.table import Table

import modules.utils.rapid_pipeline_subs as util


# Input SExtractor and PhotUtils-finder catalog filenames, along with fake sources injected.

filename_diffimage_sextractor_catalog = os.getenv('INPUTSEXCATFNAME')
if filename_diffimage_sextractor_catalog is None:
    filename_diffimage_sextractor_catalog = "diffimage_masked_original.txt"
print(f"filename_diffimage_sextractor_catalog = {filename_diffimage_sextractor_catalog}")

output_psfcat_filename = "diffimage_masked_psfcat.txt"
output_psfcat_finder_filename = "diffimage_masked_psfcat_finder.txt"

fake_sources_filename = os.getenv('INJECTFILE')
if fake_sources_filename is None:
    fake_sources_filename = "Roman_TDS_simple_model_Y106_124_5_lite_inject.txt"
print(f"fake_sources_filename = {fake_sources_filename}")


# Define environment.

rapid_sw = os.getenv('RAPID_SW')

if rapid_sw is None:

    print("*** Error: Env. var. RAPID_SW not set; quitting...")
    exit(64)

rapid_work = os.getenv('RAPID_WORK')

if rapid_work is None:

    print("*** Error: Env. var. RAPID_WORK not set; quitting...")
    exit(64)

cfg_path = rapid_sw + "/cdf"

print("rapid_sw =",rapid_sw)
print("cfg_path =",cfg_path)


# Define SExtractor parameters in catalog.

sextractor_diffimage_paramsfile = cfg_path + "/rapidSexParamsDiffImage.inp";


# Read SExtractor detections.

params_to_get_diffimage = ["XWIN_IMAGE","YWIN_IMAGE"]

vals_diffimage = util.parse_ascii_text_sextractor_catalog(filename_diffimage_sextractor_catalog,
                                                          sextractor_diffimage_paramsfile,
                                                          params_to_get_diffimage)

nsexcatsources_diffimage = len(vals_diffimage)

print("nsexcatsources_diffimage =",nsexcatsources_diffimage)

#print(vals_diffimage)


# Extract SExtractor-detection positions.

xwin_image = []
ywin_image = []
i=0
for vals in vals_diffimage:
    #if i == 0:
    #print("i,vals =",i,vals)
    x = float(vals[0])                         # Already one-based pixel coordinates.
    y = float(vals[1])
    #print("i,x,y =",i,x,y)
    xwin_image.append(x)
    ywin_image.append(y)
    #print("i,xwin_image,ywin_image =",i,xwin_image,ywin_image)
    i += 1

x1 = np.array(xwin_image)
y1 = np.array(ywin_image)


# Read photutils finder detections.

table_from_file = ascii.read(output_psfcat_filename)

table_type = type(table_from_file)
print("table_type =",table_type)

npsfcatsources_diffimage = len(table_from_file)
print("npsfcatsources_diffimage =",npsfcatsources_diffimage)


x_psfcat = []
y_psfcat = []
i = 0
for line in table_from_file:
    #print("line =",line)
    x_fit= float(line[7]) + 1                # One-based pixel coordinates.
    y_fit= float(line[8]) + 1
    x_psfcat.append(x_fit)
    y_psfcat.append(y_fit)
    #print("i,x_fit,y_fit =",i,x_fit,y_fit)
    i += 1

x2 = np.array(x_psfcat)
y2 = np.array(y_psfcat)


# Read fake sources injected.

fake_sources_from_file = ascii.read(fake_sources_filename)

table_type = type(fake_sources_from_file)
print("table_type =",table_type)

nfakeources_diffimage = len(fake_sources_from_file)
print("nfakeources_diffimage =",nfakeources_diffimage)


x_fakesrc = []
y_fakesrc = []
i = 0
for line in fake_sources_from_file:
    #print("line =",line)
    x_pix = float(line[0]) + 1                # One-based pixel coordinates.
    y_pix = float(line[1]) + 1
    flux = float(line[2])
    x_fakesrc.append(x_pix)
    y_fakesrc.append(y_pix)
    #print("i,x_pix,y_pix,flux =",i,x_pix,y_pix,flux)
    i += 1

x3 = np.array(x_fakesrc)
y3 = np.array(y_fakesrc)


# Count numbers of fake sources matched to SExtractor and PhotUtils.

match_radius_pixels = 1.5
j = 0
ns_true = 0
ns_false = 0
np_true = 0
np_false = 0
ns_true_np_false = 0
ns_false_np_true = 0
for xf,yf in zip(x_fakesrc,y_fakesrc):

    # SExtractor
    idxs = 999999
    dmins = 999999.9
    i = 0
    for xs,ys in zip(xwin_image,ywin_image):
        d = np.sqrt((xf - xs) * (xf - xs) + (yf - ys) * (yf - ys))
        if d < dmins:
            dmins = d
            idxs = i
        i += 1

    # PhotUtils
    idxp = 999999
    dminp = 999999.9
    i = 0
    for xp,yp in zip(x_psfcat,y_psfcat):
        d = np.sqrt((xf - xp) * (xf - xp) + (yf - yp) * (yf - yp))
        if d < dminp:
            dminp = d
            idxp = i
        i += 1

    matchs = False
    if dmins < match_radius_pixels:
        matchs = True
    matchp = False
    if dminp < match_radius_pixels:
        matchp = True

    if matchs is True:
        if matchp is False:
            print("j,xf,yf,dmins,idxs,dminp,idxp,matchs,matchp =",j,xf,yf,dmins,idxs,dminp,idxp,matchs,matchp)
            ns_true_np_false += 1
    else:
        if matchp is True:
            ns_false_np_true += 1

    if matchs is True:
        ns_true += 1
    else:
        ns_false += 1

    if matchp is True:
        np_true += 1
    else:
        np_false += 1

    j += 1

print("ns_true,ns_false,np_true,np_false,ns_true_np_false,ns_false_np_true =",
    ns_true,ns_false,np_true,np_false,ns_true_np_false,ns_false_np_true)


# Create the scatter plot
plt.figure(figsize=(8, 8))
plt.scatter(x1, y1, marker='o', facecolors='none', edgecolors='blue', s=50)
plt.scatter(x2, y2, marker='s', facecolors='none', edgecolors='red',s=10)
plt.scatter(x3, y3, marker='d', facecolors='none', edgecolors='cyan',s=200)

# Add labels and a title
plt.xlabel("X (pixels)")
plt.ylabel("Y (pixels)")
plt.title("Scatter Plot of SExtractor (blue) vs. PhotUtils (red) vs. Fake Sources (cyan)")


nsources = npsfcatsources_diffimage
if nsources is None:
    nsources = 3381
fwhm = float(os.getenv('FWHM'))
if fwhm is None:
    fwhm = 2.0
sharplo = float(os.getenv('SHARPLO'))
if sharplo is None:
    sharplo = -1.0
sharphi = float(os.getenv('SHARPHI'))
if sharphi is None:
    sharphi = 10.0
roundlo = float(os.getenv('ROUNDLO'))
if roundlo is None:
    roundlo = -2.0
roundhi = float(os.getenv('ROUNDHI'))
if roundhi is None:
    roundhi = 2.0
min_separation = float(os.getenv('MINSEP'))
if min_separation is None:
    min_separation = 1.0          # pixels


plt.suptitle(f"SExtractor set up like ZTF: nsexcatsources_diffimage={nsexcatsources_diffimage}")
#plt.text(-100, -100, f'Photutils: ', bbox=dict(facecolor='lightblue', alpha=0.7, pad=5))
plt.figtext(0.05,
            0.93,
            f'Photutils: fwhm={fwhm},nsources={nsources},sharplo={sharplo},sharphi={sharphi},roundlo={roundlo},roundhi={roundhi},min_sep={min_separation}',
            bbox=dict(facecolor='lightblue', alpha=0.7, pad=2))

# Output plot to PNG file.
plt.savefig(f'sex_vs_psf_fwhm={fwhm}_sharplo={sharplo}_sharphi={sharphi}_roundlo={roundlo}_roundhi={roundhi}_min_sep={min_separation}.png')

# Display the plot.
plot_flag = os.getenv('DISPLAYPLOT')
if plot_flag is None:
    plot_flag = True
print("plot_flag =",plot_flag)
if plot_flag == "True":
    plt.show()

with open('results_sexcat.txt', 'w') as f:
    f.write(f'{nsexcatsources_diffimage}\n')
    f.write(f'{ns_true}\n')

with open('results_psfcat.txt', 'w') as f:
    f.write(f'{npsfcatsources_diffimage}\n')
    f.write(f'{np_true}\n')
