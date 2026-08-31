import numpy as np
import modules.utils.rapid_pipeline_subs as util

datafile="/Users/laher/Folks/rapid/sfftdiffimage_masked.txt";
paramsfile="/Users/laher/git/rapid/cdf/rapidSexParamsDiffImage.inp";

params_to_get = ["X_WORLD",
                 "Y_WORLD",
                 "XWIN_IMAGE",
                 "YWIN_IMAGE",
                 "AWIN_WORLD",
                 "BWIN_WORLD",
                 "AWIN_IMAGE",
                 "BWIN_IMAGE",
                 "FWHM_IMAGE",
                 "CLASS_STAR",
                 "FLUX_APER_1",
                 "FLUX_APER_2",
                 "FLUX_APER_3",
                 "FLUX_APER_4",
                 "FLUX_APER_5",
                 "FLUXERR_APER_1",
                 "FLUXERR_APER_2",
                 "FLUXERR_APER_3",
                 "FLUXERR_APER_4",
                 "FLUXERR_APER_5",
                ]

vals = util.parse_ascii_text_sextractor_catalog(datafile,paramsfile,params_to_get)

test_val = vals[2][3]
print("vals[2][3] =",test_val)

num_rows = len(vals)
print("num_rows =",num_rows)

with open('test_parse2.txt', 'w') as csvfile:

    line = ','.join(params_to_get) + ",ratio"
    csvfile.write(f"{line}\n")

    for i in range(num_rows):

        x_world = vals[i][0]
        y_world = vals[i][1]
        xwin_image = vals[i][2]
        ywin_image = vals[i][3]
        awin_world = vals[i][4]
        bwin_world = vals[i][5]
        awin_image = vals[i][6]
        bwin_image = vals[i][7]
        fwhm_image = vals[i][8]
        class_star = vals[i][9]
        flux_aper_1 = vals[i][10]
        flux_aper_2 = vals[i][11]
        flux_aper_3 = vals[i][12]
        flux_aper_4 = vals[i][13]
        flux_aper_5 = vals[i][14]
        fluxerr_aper_1 = vals[i][15]
        fluxerr_aper_2 = vals[i][16]
        fluxerr_aper_3 = vals[i][17]
        fluxerr_aper_4 = vals[i][18]
        fluxerr_aper_5 = vals[i][19]

        ratio = str(float(awin_world) / float(bwin_world))

        print(x_world,y_world,xwin_image,ywin_image,awin_world,bwin_world,awin_image,
              bwin_image,fwhm_image,class_star,flux_aper_1,flux_aper_2,flux_aper_3,
              flux_aper_4,flux_aper_5,fluxerr_aper_1,fluxerr_aper_2,fluxerr_aper_3,
              fluxerr_aper_4,fluxerr_aper_5,ratio)

        my_list = [x_world,y_world,xwin_image,ywin_image,awin_world,bwin_world,awin_image,
                   bwin_image,fwhm_image,class_star,flux_aper_1,flux_aper_2,flux_aper_3,
                   flux_aper_4,flux_aper_5,fluxerr_aper_1,fluxerr_aper_2,fluxerr_aper_3,
                   fluxerr_aper_4,fluxerr_aper_5,ratio]

        line = (", ").join(my_list)
        csvfile.write(f"{line}\n")


exit(0)
