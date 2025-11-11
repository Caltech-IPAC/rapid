# To be run inside container with sex binary executable under /code/c/bin/sex.

import os
import subprocess
import time

start_time_benchmark_at_start = time.time()

filename_diffimage_sextractor_catalog = 'diffimage_masked.txt'
catalog_suffix = "_with_alice2_config.txt"


# Get a list of entries in the current directory

directory_paths = os.listdir('.')
#print(directory_paths)

main_path = "/work/download_files_20250927"

i = 0
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

    input_file = "diffimage_masked.fits"
    if  os.path.exists(input_file):
        # and (directory_path == "jid1456" or directory_path == "jid503"):

        try:
            code_to_execute_object = subprocess.run(['python3.12', '/debug/scripts/generate_sexcat.py'], capture_output=True, text=True, check=True)

            code_to_execute_stdout = code_to_execute_object.stdout

            code_to_execute_stderr = code_to_execute_object.stderr

        except subprocess.CalledProcessError as e:
            print(f"Error executing command: {e}")
            print(f"Stderr: {e.stderr}")

        output_file = "generate_sexcat.out"

        with open(output_file, "w") as f:
            f.write("STDOUT:\n")
            f.write(code_to_execute_stdout)
            f.write("STDERR:\n")
            if code_to_execute_stderr == "" or code_to_execute_stderr is None:
                f.write("None\n")
            else:
                f.write(code_to_execute_stderr)


        # Rename catalogs with canonical names to have custom suffix
        # so that a downstream process does not overwrite them.

        old_path = filename_diffimage_sextractor_catalog
        if  os.path.exists(old_path):


            # Code to rename catalog with custom suffix.

            new_path = old_path.replace(".txt",catalog_suffix)
            try:
                os.rename(old_path, new_path)
                print(f"File '{old_path}' renamed to '{new_path}'")
            except FileNotFoundError:
                print(f"Error: Source file '{old_path}' to be renamed not found.")
            except Exception as e:
                print(f"An error occurred: {e}")

        i += 1

        print("i =",i)

        if i >= 1025:
            break


    os.chdir("..")


# Code-timing benchmark overall.

end_time_benchmark = time.time()
print("Elapsed execution time in seconds =",end_time_benchmark - start_time_benchmark_at_start)


exit(0)




