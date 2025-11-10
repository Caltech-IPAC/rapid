#########################################################
# Generate PhotUtils catalogs for many different cases,
# for a multitude of ZOGY difference image to get
# statistically meaningful results.
#
# To be run outside container, after running related
# scripts/generate_sexcats_with_custom_config.py
# inside container.
#########################################################

import os
import subprocess
import numpy as np
import time

start_time_benchmark_at_start = time.time()

input_file = "diffimage_masked.fits"
filename_diffimage_sextractor_catalog = "diffimage_masked_with_ztf_config.txt"


# Cases: fwhm,sharplo,sharphi,roundlo,roundhi,min_separation.


cases_dict = {}
cases_dict[0]=[2.0,0.2,1.0,-1.0,1.0,0.0]
cases_dict[1]=[2.0,-1.0,10.0,-1.0,1.0,0.0]
cases_dict[2]=[2.0,-1.0,10.0,-1.0,1.0,1.0]
cases_dict[3]=[2.0,-1.0,10.0,-1.0,1.0,2.0]
cases_dict[4]=[2.0,-1.0,1.0,-1.0,1.0,1.0]
cases_dict[5]=[2.0,-1.0,10.0,-2.0,2.0,1.0]
cases_dict[6]=[1.4,0.2,1.0,-1.0,1.0,0.0]
cases_dict[7]=[1.4,-1.0,10.0,-1.0,1.0,0.0]
cases_dict[8]=[1.0,0.2,1.0,-1.0,1.0,0.0]
cases_dict[9]=[1.0,-1.0,10.0,-1.0,1.0,0.0]

cases = list(cases_dict.keys())
num_cases = len(cases)

os.environ['DISPLAYPLOT'] = "False"


# Get a list of entries in the current directory

directory_paths = os.listdir('.')
#print(directory_paths)

main_path = "/Users/laher/Folks/rapid/download_files_20250927"

nsources_sexcat_list = []
ns_true_list = []

nsources_psfcat_list = []
np_true_list = []


i = 0
j = 0
for directory_path in directory_paths:

    if os.path.isdir(directory_path):
        print(f"'{directory_path}' is a directory.")
    else:
        continue

    specific_directory_path = f"{main_path}/{directory_path}"
    os.chdir(specific_directory_path)

    # List contents of a specific directory
    if os.path.exists(specific_directory_path) and os.path.isdir(specific_directory_path):
        folder_contents = os.listdir(specific_directory_path)
        print(f"Contents of '{specific_directory_path}': {folder_contents}")
    else:
        print(f"Directory '{specific_directory_path}' does not exist or is not a directory.")

    if  os.path.exists(input_file) and os.path.exists(filename_diffimage_sextractor_catalog):
        # and (directory_path == "jid1456" or directory_path == "jid503"):

        for test_file in folder_contents:
            if "inject" in test_file:
                inject_file = test_file
                print(f"inject_file={inject_file}")
                break

        nsources_psfcat_cases_list = []
        np_true_cases_list = []

        for case in cases:

            print(f"case = {case}")

            params = cases_dict[case]
            fwhm = params[0]
            sharplo = params[1]
            sharphi = params[2]
            roundlo = params[3]
            roundhi = params[4]
            min_separation = params[5]

            os.environ['CASENUM'] = f"{case}"
            os.environ['INJECTFILE'] = inject_file
            os.environ['FWHM'] = str(fwhm)
            os.environ['SHARPLO'] = str(sharplo)
            os.environ['SHARPHI'] = str(sharphi)
            os.environ['ROUNDLO'] = str(roundlo)
            os.environ['ROUNDHI'] = str(roundhi)
            os.environ['MINSEP'] = str(min_separation)
            os.environ['INPUTSEXCATFNAME'] = filename_diffimage_sextractor_catalog

            try:
                code_to_execute_object = subprocess.run(['python', '/Users/laher/git/rapid/scripts/generate_psfcat.py'], capture_output=True, text=True, check=True)

                code_to_execute_stdout = code_to_execute_object.stdout

                code_to_execute_stderr = code_to_execute_object.stderr

            except subprocess.CalledProcessError as e:
                print(f"Error executing command: {e}")
                print(f"Stderr: {e.stderr}")

            output_file = f"generate_psfcat_case{case}.out"

            with open(output_file, "w") as f:
                f.write("STDOUT:\n")
                f.write(code_to_execute_stdout)
                f.write("STDERR:\n")
                if code_to_execute_stderr == "" or code_to_execute_stderr is None:
                    f.write("None\n")
                else:
                    f.write(code_to_execute_stderr)


            try:
                code_to_execute_object = subprocess.run(['python', '/Users/laher/git/rapid/scripts/plot_detections.py'], capture_output=True, text=True, check=True)

                code_to_execute_stdout = code_to_execute_object.stdout

                code_to_execute_stderr = code_to_execute_object.stderr

            except subprocess.CalledProcessError as e:
                print(f"Error executing command: {e}")
                print(f"Stderr: {e.stderr}")

            output_file = f"plot_detections_case{case}.out"

            with open(output_file, "w") as f:
                f.write("STDOUT:\n")
                f.write(code_to_execute_stdout)
                f.write("STDERR:\n")
                if code_to_execute_stderr == "" or code_to_execute_stderr is None:
                    f.write("None\n")
                else:
                    f.write(code_to_execute_stderr)


            # Rename catalogs with canonical names to have suffix with case number,
            # so that a downstream process does not overwrite them.

            old_path = "diffimage_masked_psfcat.txt"
            if  os.path.exists(old_path):
                new_path = old_path.replace(".txt",f"_case{case}.txt")
                try:
                    os.rename(old_path, new_path)
                    print(f"File '{old_path}' renamed to '{new_path}'")
                except FileNotFoundError:
                    print(f"Error: Source file '{old_path}' to be renamed not found.")
                except Exception as e:
                    print(f"A renaming error occurred: {e}")

            old_path = "diffimage_masked_psfcat_finder.txt"
            if  os.path.exists(old_path):
                new_path = old_path.replace(".txt",f"_case{case}.txt")
                try:
                    os.rename(old_path, new_path)
                    print(f"File '{old_path}' renamed to '{new_path}'")
                except FileNotFoundError:
                    print(f"Error: Source file '{old_path}' to be renamed not found.")
                except Exception as e:
                    print(f"A renaming error occurred: {e}")


            try:
                results_psfcat = 'results_psfcat.txt'

                with open(results_psfcat, "r") as file:
                    lines = file.readlines()
                    line = lines[0]
                    #print(f"line = {line}")
                    n_psfcat_sources = float(line.strip())
                    print(f"n_psfcat_sources = {n_psfcat_sources}")
                    line = lines[1]
                    #print(f"line = {line}")
                    np_true = float(line.strip())
                    print(f"np_true = {np_true}")

                nsources_psfcat_cases_list.append(n_psfcat_sources)
                np_true_cases_list.append(np_true)

            except FileNotFoundError:
                print(f"Error: The file {results_psfcat} was not found.")
            except Exception as e:
                print(f"An error occurred reading {results_psfcat}: {e}")

            # End of cases loop.


        try:
            results_sexcat = 'results_sexcat.txt'
            with open(results_sexcat, "r") as file:
                lines = file.readlines()
                line = lines[0]
                n_sexcat_sources = float(line.strip())
                line = lines[1]
                ns_true = float(line.strip())

            nsources_sexcat_list.append(n_sexcat_sources)
            ns_true_list.append(ns_true)

        except FileNotFoundError:
            print(f"Error: The file {results_sexcat} was not found.")
        except Exception as e:
            print(f"An error occurred reading {results_sexcat}: {e}")

        number_of_cases_covered = len(nsources_psfcat_cases_list)
        print(f"For {directory_path}, number_of_cases_covered={number_of_cases_covered}")

        if number_of_cases_covered == num_cases:
            nsources_psfcat_list.append(nsources_psfcat_cases_list)
            np_true_list.append(np_true_cases_list)
            i += 1
            print("i =",i)
        else:
            j += 1
            print(f"Error: number_of_cases_covered ({number_of_cases_covered}) not equal to number of cases ({num_cases}): j={j}")

        if i >= 1000:
            break


    os.chdir("..")


# Code-timing benchmark overall.

end_time_benchmark = time.time()
print("\nElapsed execution time in seconds =",end_time_benchmark - start_time_benchmark_at_start)


# Compute statistics and print results.
# Define numpy arrays and compute means and standard deviations.
# Compute margin of error for 95% confidence level (z-value = 1.96).

sample_size = i

print(f"\nSamples: nsources_sexcat_list={nsources_sexcat_list}")
print(f"Samples: ns_true_list={ns_true_list}")

print(f"Samples: nsources_psfcat_list={nsources_psfcat_list}")
print(f"Samples: np_true_list={np_true_list}")


print("\nStatistical results:")
print(f"sample_size={sample_size}")

numpy_nsources_sexcat = np.array(nsources_sexcat_list)
numpy_ns_true = np.array(ns_true_list)

numpy_nsources_psfcat = np.array(nsources_psfcat_list)
numpy_np_true = np.array(np_true_list)

avg_numpy_nsources_sexcat = np.mean(numpy_nsources_sexcat)
std_numpy_nsources_sexcat = np.std(numpy_nsources_sexcat)
margin_of_error_nsources_sexcat = 1.96 * std_numpy_nsources_sexcat / np.sqrt(float(sample_size))

print(f"avg_numpy_nsources_sexcat={avg_numpy_nsources_sexcat}")
print(f"std_numpy_nsources_sexcat={std_numpy_nsources_sexcat}")
print(f"margin_of_error_nsources_sexcat={margin_of_error_nsources_sexcat}")

avg_numpy_ns_true = np.mean(numpy_ns_true)
std_numpy_ns_true = np.std(numpy_ns_true)
margin_of_error_ns_true = 1.96 * std_numpy_ns_true / np.sqrt(float(sample_size))

print(f"avg_numpy_ns_true={avg_numpy_ns_true}")
print(f"std_numpy_ns_true={std_numpy_ns_true}")
print(f"margin_of_error_ns_true={margin_of_error_ns_true}")

avg_numpy_nsources_psfcat = np.mean(numpy_nsources_psfcat,axis=0)
std_numpy_nsources_psfcat = np.std(numpy_nsources_psfcat,axis=0)
margin_of_error_nsources_psfcat = 1.96 * std_numpy_nsources_psfcat / np.sqrt(float(sample_size))

print(f"avg_numpy_nsources_psfcat={avg_numpy_nsources_psfcat}")
print(f"std_numpy_nsources_psfcat={std_numpy_nsources_psfcat}")
print(f"margin_of_error_nsources_psfcat={margin_of_error_nsources_psfcat}")

avg_numpy_np_true = np.mean(numpy_np_true,axis=0)
std_numpy_np_true = np.std(numpy_np_true,axis=0)
margin_of_error_np_true = 1.96 * std_numpy_np_true / np.sqrt(float(sample_size))

print(f"avg_numpy_np_true={avg_numpy_np_true}")
print(f"std_numpy_np_true={std_numpy_np_true}")
print(f"margin_of_error_np_true={margin_of_error_np_true}")


exit(0)
