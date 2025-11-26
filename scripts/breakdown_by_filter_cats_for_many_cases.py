#########################################################
# Breakdown the statistics by filter for the many different
# SExtractor versus PhotUtils catalog cases
# for a multitude of ZOGY difference images.
#
# To be run outside container, after running related
# scripts/generate_sexcats_with_custom_config.py
# inside container, and
# generate_psfcats_for_many_cases.py
# outside container on laptop.
#########################################################

import os
import subprocess
import numpy as np
import time
from astropy.io import fits


start_time_benchmark_at_start = time.time()

main_path = "/Users/laher/Folks/rapid/download_files_20250927"
input_fits_file = "diffimage_masked.fits"

case_list = ["ZTF","AL1","A2","PU1","PU2","PU3","PU4","PU5","PU6","PU7","PU8","PU9","PU10"]

results_files = ["results_sexcat_with_ztf_config.txt",
                 "results_sexcat_with_alice_config.txt",
                 "results_sexcat_with_alice2_config.txt",
                 "results_psfcat_case0.txt",
                 "results_psfcat_case1.txt",
                 "results_psfcat_case2.txt",
                 "results_psfcat_case3.txt",
                 "results_psfcat_case4.txt",
                 "results_psfcat_case5.txt",
                 "results_psfcat_case6.txt",
                 "results_psfcat_case7.txt",
                 "results_psfcat_case8.txt",
                 "results_psfcat_case9.txt"]


number_of_cases_covered = len(results_files)
print(f"Number_of_cases_covered={number_of_cases_covered}")


# Define filters and related data dictionaries.

filters = ["F184","H158","J129","K213","R062","Y106","Z087","W146"]

nsources_cat_dict = {}
nmatches_cat_dict = {}

for filter in filters:
    nsources_cat_dict[filter] = []         # Initialize dictionary value as empty list.
    nmatches_cat_dict[filter] = []


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

    if os.path.exists(results_files[0]):
        print("i = ",i)

        nsources_list = []
        num_matches_with_fake_sources_list = []

        for results_file in results_files:

            if  os.path.exists(results_file):

                try:
                    with open(results_file, "r") as file:
                        lines = file.readlines()
                        line = lines[0]
                        #print(f"line = {line}")
                        num_sources = float(line.strip())
                        print(f"num_sources = {num_sources}")
                        line = lines[1]
                        #print(f"line = {line}")
                        num_matches = float(line.strip())
                        print(f"num_matches = {num_matches}")

                except FileNotFoundError:
                    print(f"Error: The file {results_file} was not found.")
                    num_sources = np.nan
                    num_matches = np.nan
                except Exception as e:
                    print(f"An error occurred reading {results_psfcat}: {e}")
                    num_sources = np.nan
                    num_matches = np.nan

            else:
                print(f"{results_file} does not exist in {directory_path}...")
                num_sources = np.nan
                num_matches = np.nan


            nsources_list.append(num_sources)
            num_matches_with_fake_sources_list.append(num_matches)


            # End of results_file loop.


        nsources_cat_dict[filter].append(nsources_list)
        nmatches_cat_dict[filter].append(num_matches_with_fake_sources_list)

        i += 1
        print("i =",i)

        if i >= 1000:
            break


    os.chdir("..")


print(f"Samples: nsources_cat_dict={nsources_cat_dict}")
print(f"Samples: nmatches_cat_dict={nmatches_cat_dict}")


# Compute statistics and print results, broken  down by filter.
# Define numpy arrays and compute means and standard deviations.
# Compute margin of error for 95% confidence level (z-value = 1.96).

for filter in filters:

    numpy_nsources_cat = np.array(nsources_cat_dict[filter])
    numpy_nmatches_cat = np.array(nmatches_cat_dict[filter])

    print(f"\nStatistical results for filter = {filter}:")

    # Count the number of non-NaN values

    sample_size_list = []

    sample_flag = False
    for i in range(number_of_cases_covered):
        try:
            sliced_arr = numpy_nsources_cat[:, i:i+1]
            #print(sliced_arr)
            sample_size = np.count_nonzero(~np.isnan(sliced_arr))
            #print(f"i={i}, sample_size={sample_size}")
            sample_size_list.append(sample_size)
        except:
            print("No data for filter...")
            sample_flag = True
            break
    if sample_flag:
        continue

    print(f"case_list = {case_list}")
    print(f"sample_size_list = {sample_size_list}")

    avg_numpy_nsources_cat = np.nanmean(numpy_nsources_cat,axis=0)
    std_numpy_nsources_cat = np.nanstd(numpy_nsources_cat,axis=0)
    margin_of_error_nsources_cat = 1.96 * std_numpy_nsources_cat / np.sqrt(float(sample_size))

    print(f"avg_numpy_nsources_cat={avg_numpy_nsources_cat}")
    print(f"std_numpy_nsources_cat={std_numpy_nsources_cat}")
    print(f"margin_of_error_nsources_cat={margin_of_error_nsources_cat}")

    avg_numpy_nmatches_cat = np.nanmean(numpy_nmatches_cat,axis=0)
    std_numpy_nmatches_cat = np.nanstd(numpy_nmatches_cat,axis=0)
    margin_of_error_nmatches_cat = 1.96 * std_numpy_nmatches_cat / np.sqrt(float(sample_size))

    print(f"avg_numpy_nmatches_cat={avg_numpy_nmatches_cat}")
    print(f"std_numpy_nmatches_cat={std_numpy_nmatches_cat}")
    print(f"margin_of_error_nmatches_cat={margin_of_error_nmatches_cat}")


# Code-timing benchmark overall.

end_time_benchmark = time.time()
print("\nElapsed execution time in seconds =",end_time_benchmark - start_time_benchmark_at_start)


exit(0)
