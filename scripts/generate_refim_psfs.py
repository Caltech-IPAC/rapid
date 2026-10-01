import boto3
import numpy as np
from astropy.io import fits

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util


# The following parameter controls the pixel bin size
# (0 equals 1 pixel, 1 equals 0.1 pixels, 2 equals 0.01 pixels, etc.)

num_dec_places = 1


def average_image_in_radial_bins(nx,ny,data):

    # The formulas below have only been tested for odd values of nx and ny.

    x_cen = float(int((nx - 1) / 2) + 1)
    y_cen = float(int((ny - 1) / 2) + 1)

    pix_sum_dict = {}
    pix_num_dict = {}


    for i in range(0,ny):
        for j in range(0,nx):
            dx = float(j + 1) - x_cen
            dy = float(i + 1) - y_cen
            r = np.sqrt(dx * dx + dy * dy)
            #print("i,j,r =",i,j,r)

            key = str(round(r,num_dec_places))

            try:
                pix_sum_dict[key] += data[i][j]
                pix_num_dict[key] += 1
            except:
                pix_sum_dict[key] = data[i][j]
                pix_num_dict[key] = 1

    for key in pix_sum_dict.keys():
        #print("key,pix_sum_dict[key],pix_num_dict[key] =",key,pix_sum_dict[key],pix_num_dict[key])
        pix_sum_dict[key] = pix_sum_dict[key] / float(pix_num_dict[key])

    for i in range(0,ny):
        for j in range(0,nx):
            dx = float(j + 1) - x_cen
            dy = float(i + 1) - y_cen
            r = np.sqrt(dx * dx + dy * dy)
            #print("i,j,r =",i,j,r)

            key = str(round(r,num_dec_places))

            data[i][j] = pix_sum_dict[key]


    return data


if __name__ == '__main__':


    # Initialize S3 client.

    s3_client = boto3.client('s3')


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    fid_sca_dict = {}

    # Select all distinct fid, sca pairs in PSFs database table.

    records = dbh.get_distinct_fid_sca_from_psfs()

    for r in records:
        fid = int(r[0])
        sca = int(r[1])
        print("fid,sca =",fid,sca)


        # Query database for associated L2FileMeta record.

        psfid,s3_full_name_psf = dbh.get_best_psf(sca,fid)

        print("psfid,s3_full_name_psf =",psfid,s3_full_name_psf)


        # Download PSF from S3 bucket.

        filename_psf,subdirs_psf = util.download_file_from_s3_bucket(s3_client,s3_full_name_psf)

        print("s3_full_name_psf = ",s3_full_name_psf)
        print("filename_psf = ",filename_psf)


        try:
            fid_sca_dict[str(fid)].append([sca,psfid,filename_psf])
        except:
            fid_sca_dict[str(fid)] = []
            fid_sca_dict[str(fid)].append([sca,psfid,filename_psf])

    print("fid_sca_dict =",fid_sca_dict)

    for fid_str in fid_sca_dict.keys():
        fid = int(fid_str)
        print("fid =",fid)

        sca_list = fid_sca_dict[fid_str]

        n = len(sca_list)
        print("n =",n)
        print("sca_list =",sca_list)

        frames_data = []
        sumflux = []

        for i in range(len(sca_list)):
            sca = sca_list[i][0]
            psf_fname = sca_list[i][2]

            print ("i,sca,psf_fname =",i,sca,psf_fname)

            hdul = fits.open(psf_fname)
            hdr = hdul[0].header
            data_input = hdul[0].data


            # This code assumes all PSFs to be averaged are the same size.

            nx = hdr["NAXIS1"]
            ny = hdr["NAXIS2"]


            # Replace each pixel in the PSF image with the average
            # of all pixels at the same radius from the image center.

            data = average_image_in_radial_bins(nx,ny,data_input)


            # Accumulate image data in list for stack-averaging.

            np_data = np.array(data)

            sum = np.sum(np_data)

            sumflux.append(sum)

            print("sum =",sum)

            frames_data.append(np_data)


        # Compute pixel-by-pixel average.

        avg_image = np.mean(frames_data, axis=0)


        # Replace each pixel in the PSF image with the average
        # of all pixels at the same radius from the image center.

        data_output = average_image_in_radial_bins(nx,ny,avg_image)



#####################################3#####################################################
#
#    The following fattens the PSF and therefore deweights the central pixel,
#    which may not be desirable.  Omit for now.
#
#        data_intermediate = average_image_in_radial_bins(nx,ny,avg_image)
#
#
#        # Smooth image to further suppress any remaining artifacts.
#
#
#        data_output = util.smooth_image_by_local_clipped_averaging(nx,ny,data_intermediate)
#
#####################################3#####################################################



        # Renormalize final output image.

        global_sum = np.sum(data_output)
        data_output /= global_sum

        final_sum = np.sum(data_output)
        print("final_sum =",final_sum)


        # Write to FITS output file for given filter.

        fname_output = "refimage_psf_fid" + fid_str + ".fits"

        hdu_list = []
        hdu = fits.PrimaryHDU(data=data_output)

        for i in range(len(sca_list)):
            sca = sca_list[i][0]
            psf_fname = sca_list[i][2]
            keyword = "INPFIL" + str(sca)
            hdu.header[keyword] = psf_fname
            keyword2 = "SUMFLX" + str(sca)
            hdu.header[keyword2] = sumflux[i]

        hdu_list.append(hdu)
        hdu = fits.HDUList(hdu_list)
        hdu.writeto(fname_output,overwrite=True,checksum=True)


        # Upload output FITS file to canonical S3 bucket for pipeline usage.

        product_s3_bucket = 'rapid-pipeline-files'
        s3_object_name = "refimage_psfs" + "/" + fname_output
        filenames = [fname_output]
        objectnames = [s3_object_name]
        util.upload_files_to_s3_bucket(s3_client,product_s3_bucket,filenames,objectnames)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Termination.

    exit(0)
