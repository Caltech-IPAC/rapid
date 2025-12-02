#########################################################
# Breakdown the PhotUtils catalog attributes by filter
# for the many different PhotUtils catalog cases
# for a multitude of ZOGY difference images.
# Make scatter plots of catalog attributes vs. SNR.
#########################################################

import os
import subprocess
import numpy as np
import time
from astropy.io import fits, ascii
from astropy.table import QTable, join
import matplotlib.pyplot as plt


start_time_benchmark_at_start = time.time()


# Define number of catalogs.

n_catalogs = 1000


# Define match_radius_pixels for matching catalog sources with fake-source positions.

match_radius_pixels = 1.0

main_path = "/Users/laher/Folks/rapid/download_files_20250927"
input_fits_file = "diffimage_masked.fits"

case_list = ["PU1","PU2","PU3","PU4","PU5","PU6","PU7","PU8","PU9","PU10"]

photutils_files = ["diffimage_masked_psfcat_case0.txt",
                   "diffimage_masked_psfcat_case1.txt",
                   "diffimage_masked_psfcat_case2.txt",
                   "diffimage_masked_psfcat_case3.txt",
                   "diffimage_masked_psfcat_case4.txt",
                   "diffimage_masked_psfcat_case5.txt",
                   "diffimage_masked_psfcat_case6.txt",
                   "diffimage_masked_psfcat_case7.txt",
                   "diffimage_masked_psfcat_case8.txt",
                   "diffimage_masked_psfcat_case9.txt"]

finder_files = ["diffimage_masked_psfcat_finder_case0.txt",
                "diffimage_masked_psfcat_finder_case1.txt",
                "diffimage_masked_psfcat_finder_case2.txt",
                "diffimage_masked_psfcat_finder_case3.txt",
                "diffimage_masked_psfcat_finder_case4.txt",
                "diffimage_masked_psfcat_finder_case5.txt",
                "diffimage_masked_psfcat_finder_case6.txt",
                "diffimage_masked_psfcat_finder_case7.txt",
                "diffimage_masked_psfcat_finder_case8.txt",
                "diffimage_masked_psfcat_finder_case9.txt"]


number_of_cases_covered = len(photutils_files)
print(f"Number_of_cases_covered={number_of_cases_covered}")


# Define filters and related data dictionaries.

filters = ["F184","H158","J129","K213","R062","Y106","Z087","W146"]

sharpness_true_dict = {}
roundness1_true_dict = {}
roundness2_true_dict = {}
snr_true_dict = {}
sharpness_false_dict = {}
roundness1_false_dict = {}
roundness2_false_dict = {}
snr_false_dict = {}

current_directory = os.getcwd()

for filter in filters:
    for case in case_list:
        filter_case = filter + case
        sharpness_true_dict[filter_case] = []       # Initialize dictionary as empty list.
        roundness1_true_dict[filter_case] = []
        roundness2_true_dict[filter_case] = []
        snr_true_dict[filter_case] = []
        sharpness_false_dict[filter_case] = []
        roundness1_false_dict[filter_case] = []
        roundness2_false_dict[filter_case] = []
        snr_false_dict[filter_case] = []


# Get a list of entries in the current directory

directory_paths = os.listdir('.')
#print(directory_paths)

i = 0
for directory_path in directory_paths:

    if os.path.isdir(directory_path):
        print(f"'{directory_path}' is a directory.")
    else:
        continue

    specific_directory_path = f"{main_path}/{directory_path}"


    # Change to subdirectory.

    os.chdir(specific_directory_path)

    # List contents of a specific directory
    if os.path.exists(specific_directory_path) and os.path.isdir(specific_directory_path):
        folder_contents = os.listdir(specific_directory_path)
        print(f"Contents of '{specific_directory_path}': {folder_contents}")
    else:
        print(f"Directory '{specific_directory_path}' does not exist or is not a directory.")


    # Read filter from header of difference-image FITS file.

    if os.path.exists(input_fits_file):

        with fits.open(input_fits_file) as hdul:

            filter = hdul[0].header["FILTER"].strip()

        print(f"filter={filter}")

        if filter not in filters:
            os.chdir("..")
            continue

    else:
        print("*** Error: Input FITS file does not exist; continuing...")
        os.chdir("..")
        continue

    if os.path.exists(photutils_files[0]):
        print("i = ",i)

        for test_file in folder_contents:
            if "inject" in test_file:
                inject_file = test_file
                print(f"inject_file={inject_file}")
                break

        fake_sources_filename = inject_file


        # Read fake sources injected.

        fake_sources_from_file = ascii.read(fake_sources_filename)

        table_type = type(fake_sources_from_file)
        print("table_type =",table_type)

        nfakeources_diffimage = len(fake_sources_from_file)
        print("nfakeources_diffimage =",nfakeources_diffimage)


        x_fakesrc = []
        y_fakesrc = []
        k = 0
        for line in fake_sources_from_file:
            #print("line =",line)
            x_pix = float(line[0])                    # Already one-based pixel coordinates.
            y_pix = float(line[1])
            flux = float(line[2])
            x_fakesrc.append(x_pix)
            y_fakesrc.append(y_pix)
            #print("k,x_pix,y_pix,flux =",k,x_pix,y_pix,flux)
            k += 1

        x3 = np.array(x_fakesrc)
        y3 = np.array(y_fakesrc)


        # Read PhotUtils catalogs for the many cases.

        for photutils_file,finder_file,case in zip(photutils_files,finder_files,case_list):

                filter_case = filter + case

                if  os.path.exists(photutils_file) and os.path.exists(finder_file):


                    # Join catalogs and extract columns for sources database tables.

                    psfcat_qtable = QTable.read(photutils_file,format='ascii')
                    psfcat_finder_qtable = QTable.read(finder_file,format='ascii')

                    joined_table_inner = join(psfcat_qtable, psfcat_finder_qtable, keys='id', join_type='inner')

                    nrows = len(joined_table_inner)
                    print(f"nrows = {nrows}")


                    # Here are what the columns in the photutils catalogs are called:
                    # Main: id group_id group_size local_bkg x_init y_init flux_init x_fit y_fit flux_fit x_err y_err flux_err npixfit qfit cfit flags ra dec
                    # Finder: id xcentroid ycentroid sharpness roundness1 roundness2 npix peak flux mag daofind_mag

                    x_psfcat = []
                    y_psfcat = []
                    sharpness_psfcat = []
                    roundness1_psfcat = []
                    roundness2_psfcat = []
                    snr_psfcat = []


                    for row in joined_table_inner:

                        #print("line =",line)
                        x_fit= float(row["x_fit"]) + 1            # Convert to one-based pixel coordinates.
                        y_fit= float(row["y_fit"]) + 1
                        sharpness = float(row["sharpness"])
                        roundness1 = float(row["roundness1"])
                        roundness2 = float(row["roundness2"])
                        flux_fit = float(row["flux_fit"])
                        flux_err = float(row["flux_err"])

                        if flux_fit <= 0:
                            continue

                        if flux_err == 0:
                            flux_err = 10000000.0

                        snr = flux_fit / flux_err
                        x_psfcat.append(x_fit)
                        y_psfcat.append(y_fit)
                        sharpness_psfcat.append(sharpness)
                        roundness1_psfcat.append(roundness1)
                        roundness2_psfcat.append(roundness2)
                        snr_psfcat.append(snr)
                        #print("x_fit,y_fit =",x_fit,y_fit)

                    x2 = np.array(x_psfcat)
                    y2 = np.array(y_psfcat)


                    # Count numbers of fake sources matched to PhotUtils.

                    j = 0
                    np_true = 0
                    np_false = 0
                    for xp,yp,sharpness,roundness1,roundness2,snr in zip(x_psfcat,
                                                                         y_psfcat,
                                                                         sharpness_psfcat,
                                                                         roundness1_psfcat,
                                                                         roundness2_psfcat,
                                                                         snr_psfcat):

                        idxp = 999999
                        dminp = 999999.9
                        k = 0
                        for xf,yf in zip(x_fakesrc,y_fakesrc):
                            d = np.sqrt((xf - xp) * (xf - xp) + (yf - yp) * (yf - yp))
                            if d < dminp:
                                dminp = d
                                idxp = k
                            k += 1

                        matchp = False
                        if dminp < match_radius_pixels:
                            matchp = True

                        if matchp is True:
                            sharpness_true_dict[filter_case].append(sharpness)
                            roundness1_true_dict[filter_case].append(roundness1)
                            roundness2_true_dict[filter_case].append(roundness2)
                            snr_true_dict[filter_case].append(snr)
                            np_true += 1
                        else:
                            sharpness_false_dict[filter_case].append(sharpness)
                            roundness1_false_dict[filter_case].append(roundness1)
                            roundness2_false_dict[filter_case].append(roundness2)
                            snr_false_dict[filter_case].append(snr)
                            np_false += 1

                        j += 1

                    print("j,np_true,np_false =",j,np_true,np_false)


                    # End for x_fakesrc,y_fakesrc-matching loop.


            # End of photutils_files,finder_files and case loops.


        i += 1
        print("i =",i)

        if i >= n_catalogs:
            break

    os.chdir("..")

print(f"Done aggregating data across all jobs...")

os.chdir(current_directory)


# Loop over cases.

for case in case_list:

    idx_case_list = case_list.index(case) + 1

    print(f"case={case},idx_case_list={idx_case_list}")


    # Plot sharpness, broken down by filter.

    attribute = 'sharpness'
    for filter in filters:

        filter_case = filter + case

        if len(sharpness_false_dict[filter_case]) == 0:
           continue

        numpy_sharpness_true = np.array(sharpness_true_dict[filter_case])
        numpy_snr_true = np.array(snr_true_dict[filter_case])
        numpy_sharpness_false = np.array(sharpness_false_dict[filter_case])
        numpy_snr_false = np.array(snr_false_dict[filter_case])

        n_numpy_snr_true = len(numpy_snr_true)
        n_numpy_snr_false = len(numpy_snr_false)
        n_numpy_sharpness_true = len(numpy_sharpness_true)
        n_numpy_sharpness_false = len(numpy_sharpness_false)

        #print(f"numpy_snr_true = {numpy_snr_true}")

        print(f"n_numpy_snr_true = {n_numpy_snr_true}")
        print(f"n_numpy_snr_false = {n_numpy_snr_false}")
        print(f"n_numpy_sharpness_true = {n_numpy_sharpness_true}")
        print(f"n_numpy_sharpness_false = {n_numpy_sharpness_false}")


        # Create the scatter plot
        plt.figure(figsize=(8, 8))
        plt.scatter(numpy_snr_false,numpy_sharpness_false,marker='.',facecolors='blue',edgecolors='blue',s=1,alpha=0.1)
        plt.scatter(numpy_snr_true,numpy_sharpness_true,marker='.',facecolors='red',edgecolors='red',s=1,alpha=1.0)


        # Add labels and a title
        plt.xlabel("SNR")
        plt.ylabel("sharpness")
        plt.title("PhotUtils Sources matches Fake Sources (red) vs. not matching (blue)")

        plt.figtext(0.05,
                    0.93,
                    f'case={idx_case_list},filter={filter},n_true={n_numpy_snr_true},n_false={n_numpy_snr_false}',
                    bbox=dict(facecolor='lightblue', alpha=0.7, pad=2))


        # Output plot to PNG file.
        plt.savefig(f'photutils_{attribute}_case={idx_case_list}_filter={filter}.png')

        #plt.show()

        plt.close()


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print("\nElapsed execution time in seconds =",end_time_benchmark - start_time_benchmark_at_start)


    # Plot roundness1, broken down by filter.

    attribute = 'roundness1'
    for filter in filters:

        filter_case = filter + case

        if len(roundness1_false_dict[filter_case]) == 0:
           continue

        numpy_roundness1_true = np.array(roundness1_true_dict[filter_case])
        numpy_snr_true = np.array(snr_true_dict[filter_case])
        numpy_roundness1_false = np.array(roundness1_false_dict[filter_case])
        numpy_snr_false = np.array(snr_false_dict[filter_case])

        n_numpy_snr_true = len(numpy_snr_true)
        n_numpy_snr_false = len(numpy_snr_false)
        n_numpy_roundness1_true = len(numpy_roundness1_true)
        n_numpy_roundness1_false = len(numpy_roundness1_false)

        #print(f"numpy_snr_true = {numpy_snr_true}")

        print(f"n_numpy_snr_true = {n_numpy_snr_true}")
        print(f"n_numpy_snr_false = {n_numpy_snr_false}")
        print(f"n_numpy_roundness1_true = {n_numpy_roundness1_true}")
        print(f"n_numpy_roundness1_false = {n_numpy_roundness1_false}")


        # Create the scatter plot
        plt.figure(figsize=(8, 8))
        plt.scatter(numpy_snr_false,numpy_roundness1_false,marker='.',facecolors='blue',edgecolors='blue',s=1,alpha=0.1)
        plt.scatter(numpy_snr_true,numpy_roundness1_true,marker='.',facecolors='red',edgecolors='red',s=1,alpha=1.0)


        # Add labels and a title
        plt.xlabel("SNR")
        plt.ylabel("roundness1")
        plt.title("PhotUtils Sources matches Fake Sources (red) vs. not matching (blue)")

        plt.figtext(0.05,
                    0.93,
                    f'case={idx_case_list},filter={filter},n_true={n_numpy_snr_true},n_false={n_numpy_snr_false}',
                    bbox=dict(facecolor='lightblue', alpha=0.7, pad=2))


        # Output plot to PNG file.
        plt.savefig(f'photutils_{attribute}_case={idx_case_list}_filter={filter}.png')

        #plt.show()

        plt.close()


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print("\nElapsed execution time in seconds =",end_time_benchmark - start_time_benchmark_at_start)


    # Plot roundness2, broken down by filter.

    attribute = 'roundness2'
    for filter in filters:

        filter_case = filter + case

        if len(roundness2_false_dict[filter_case]) == 0:
           continue

        numpy_roundness2_true = np.array(roundness2_true_dict[filter_case])
        numpy_snr_true = np.array(snr_true_dict[filter_case])
        numpy_roundness2_false = np.array(roundness2_false_dict[filter_case])
        numpy_snr_false = np.array(snr_false_dict[filter_case])

        n_numpy_snr_true = len(numpy_snr_true)
        n_numpy_snr_false = len(numpy_snr_false)
        n_numpy_roundness2_true = len(numpy_roundness2_true)
        n_numpy_roundness2_false = len(numpy_roundness2_false)

        #print(f"numpy_snr_true = {numpy_snr_true}")

        print(f"n_numpy_snr_true = {n_numpy_snr_true}")
        print(f"n_numpy_snr_false = {n_numpy_snr_false}")
        print(f"n_numpy_roundness2_true = {n_numpy_roundness2_true}")
        print(f"n_numpy_roundness2_false = {n_numpy_roundness2_false}")


        # Create the scatter plot
        plt.figure(figsize=(8, 8))
        plt.scatter(numpy_snr_false,numpy_roundness2_false,marker='.',facecolors='blue',edgecolors='blue',s=1,alpha=0.1)
        plt.scatter(numpy_snr_true,numpy_roundness2_true,marker='.',facecolors='red',edgecolors='red',s=1,alpha=1.0)


        # Add labels and a title
        plt.xlabel("SNR")
        plt.ylabel("roundness2")
        plt.title("PhotUtils Sources matches Fake Sources (red) vs. not matching (blue)")

        plt.figtext(0.05,
                    0.93,
                    f'case={idx_case_list},filter={filter},n_true={n_numpy_snr_true},n_false={n_numpy_snr_false}',
                    bbox=dict(facecolor='lightblue', alpha=0.7, pad=2))


        # Output plot to PNG file.
        plt.savefig(f'photutils_{attribute}_case={idx_case_list}_filter={filter}.png')

        #plt.show()

        plt.close()


# Code-timing benchmark overall.

end_time_benchmark = time.time()
print("\nElapsed execution time in seconds =",end_time_benchmark - start_time_benchmark_at_start)


exit(0)
