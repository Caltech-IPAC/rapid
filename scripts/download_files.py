import os
import boto3
import re

s3_client = boto3.client('s3')
product_s3_bucket = "rapid-product-files"

product_listing = "/Users/laher/Folks/rapid/rapid-product-files_20250927.txt"

filename_diffimage_sextractor_catalog = "diffimage_masked.txt"

output_psfcat_filename = "diffimage_masked_psfcat.txt"
output_psfcat_finder_filename = "diffimage_masked_psfcat_finder.txt"

fake_sources_filename = "Roman_TDS_simple_model_Y106_124_5_lite_inject.txt"


bkg_file = "bkg_subbed_science_image.fits"
filename_diffimage_masked = "diffimage_masked.fits"
filename_diffimage_masked_uncert = "diffimage_uncert_masked.fits"
filename_scorrimage_masked = "scorrimage_masked.fits"
filename_diffpsf = "diffpsf.fits"

try:
    with open(product_listing, "r") as file:
        lines = file.readlines()
except FileNotFoundError:
    print(f"Error: The file {product_listing} was not found.")
except Exception as e:
    print(f"An error occurred: {e}")

continue_flag = True
i = 0
for line in lines:


    s3_bucket_object_name = line.split()[3]
    print(f"s3_bucket_object_name = {s3_bucket_object_name}")

    string_match = re.match(r"(.+?)/(.+?)/(.+)", s3_bucket_object_name)

    try:
        directory_name = string_match.group(2)
        file = string_match.group(3)
        print(f"directory_name = {directory_name}")
        print(f"file = {file}")

    except:
        print("*** Error: Could not parse line; contining...")
        continue




    if directory_name == "jid5560":
        continue_flag = True

    if continue_flag is False:
        continue




    download_flag = False



    if filename_diffpsf == file:
        download_flag = True
    elif bkg_file == file:
        download_flag = True
    elif filename_diffimage_masked == file:
        download_flag = True
    elif filename_diffimage_masked_uncert == file:
        download_flag = True
    elif filename_scorrimage_masked == file:
        download_flag = True
    elif "_inject.txt" in file:
        download_flag = True
    elif filename_diffimage_sextractor_catalog == file:
        download_flag = True
    elif output_psfcat_filename == file:
        download_flag = True
    elif output_psfcat_finder_filename == file:
        download_flag = True



    if download_flag:

        try:
            os.mkdir(directory_name)
            print(f"Directory '{directory_name}' created.")
        except FileExistsError:
            print(f"Directory '{directory_name}' already exists.")

        print("Downloading s3://{}/{} into {}/{}...".\
            format(product_s3_bucket,s3_bucket_object_name,directory_name,file))

        response = s3_client.download_file(product_s3_bucket,s3_bucket_object_name,f"{directory_name}/{file}")

        print("response =",response)

        i += 1


    # Rename catalogs with canonical names to have suffix "_original"
    # so that a downstream process does not overwrite them.

    if filename_diffimage_sextractor_catalog == file:
        old_path = f"{directory_name}/{file}"
        if  os.path.exists(old_path):
            new_path = old_path.replace(".txt","_original.txt")
            try:
                os.rename(old_path, new_path)
                print(f"File '{old_path}' renamed to '{new_path}'")
            except FileNotFoundError:
                print(f"Error: Source file '{old_path}' to be renamed not found.")
            except Exception as e:
                print(f"An error occurred: {e}")

    if output_psfcat_filename == file:
        old_path = f"{directory_name}/{file}"
        if  os.path.exists(old_path):
            new_path = old_path.replace(".txt","_original.txt")
            try:
                os.rename(old_path, new_path)
                print(f"File '{old_path}' renamed to '{new_path}'")
            except FileNotFoundError:
                print(f"Error: Source file '{old_path}' to be renamed not found.")
            except Exception as e:
                print(f"An error occurred: {e}")

    if output_psfcat_finder_filename == file:
        old_path = f"{directory_name}/{file}"
        if  os.path.exists(old_path):
            new_path = old_path.replace(".txt","_original.txt")
            try:
                os.rename(old_path, new_path)
                print(f"File '{old_path}' renamed to '{new_path}'")
            except FileNotFoundError:
                print(f"Error: Source file '{old_path}' to be renamed not found.")
            except Exception as e:
                print(f"An error occurred: {e}")




    #if i > 30:
    #   break


