import os
import argparse
import numpy as np
import modules.utils.rapid_pipeline_subs as util

# Repo root, two levels up from this file (scripts/parse_sexcat.py).
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

parser = argparse.ArgumentParser(description="Parse a SExtractor-format catalog.")
parser.add_argument("-d", "--datafile", default="awaicgen_output_mosaic_image_resampled_refgainmatchsexcat.txt",
                     help="Input SExtractor catalog file.")
parser.add_argument("-p", "--paramsfile", default=os.path.join(_repo_root, "cdf", "rapidSexParamsGainMatch.inp"),
                     help="SExtractor params file (default: cdf/rapidSexParamsGainMatch.inp).")
args = parser.parse_args()

datafile = args.datafile
paramsfile = args.paramsfile

params_to_get = ["XWIN_IMAGE","YWIN_IMAGE","FLUX_APER_6","MAG_APER_6",
                 "CLASS_STAR","ISOAREAF_IMAGE","AWIN_WORLD","BWIN_WORLD"]

vals = util.parse_ascii_text_sextractor_catalog(datafile,paramsfile,params_to_get)

test_val = vals[2][3]
print("vals[2][3] =",test_val)

num_rows = len(vals)
print("num_rows =",num_rows)

with open('test_parse2.txt', 'w') as csvfile:

    line = ','.join(params_to_get) + ",ratio"
    csvfile.write(f"{line}\n")

    for i in range(num_rows):
        xwin_image = vals[i][0]
        ywin_image = vals[i][1]
        flux_aper = vals[i][2]
        mag_aper = vals[i][3]
        class_star = float(vals[i][4])
        isoareaf_image = float(vals[i][5])
        awin_world = float(vals[i][6])
        bwin_world = float(vals[i][7])
        ratio = awin_world / bwin_world

        print(xwin_image,ywin_image,flux_aper,mag_aper,class_star,isoareaf_image,awin_world,bwin_world,ratio)

        line = xwin_image + ", " + ywin_image + ", " + flux_aper + ", " + \
               mag_aper + ", " + str(class_star) + ", " + \
               str(isoareaf_image) + ", " + str(awin_world) + ", " + str(bwin_world) + ", " + str(ratio)
        csvfile.write(f"{line}\n")


exit(0)
