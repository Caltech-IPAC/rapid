/***********************************************
bkgest_read_image1.c

Purpose

Read the image data from a FITS file and save into an array.

Overview

Open the FITS file, allocate memory, read the data into an array,

Definitions of Variables

External:
See bkgest_defs.h and bkgest_exec.c


Internal:
I_fits_return_status = Resturn value of fits function.
I_ndims_found = Number of data dimensions found.
I_anynull = Whether nulls were found in the data.
LP_naxes = Array to store lengths of axes of data.
D_nullval = Set to zero, indicating not to replace nulls.

ffp_FITS_In = Handle to input FITS file.

***********************************************/

#include <time.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "bkgest.h"
#include "bkgest_defs.h"
#include "bkgest_errcodes.h"


void bkgest_read_image1(BKE_Filenames  *BKEP_Fnames,
                        BKE_FITSinfo   *BKEP_FITS,
                        double         **DPP_Data_Image1,
                        BKE_Status     *BKEP_Stat)
{
    int i, j, k;
    int I_fits_return_status=0, I_ndims_found, I_anynull;
    long LP_naxes[3];
    double D_nullval=0;
    fitsfile  *ffp_FITS_In;

    if(BKEP_Stat->I_status) return;

    if(BKEP_Stat->I_Verbose)
    printf("bkgest_read_image1: Reading input data from FITS file...\n");

    fits_open_file( &ffp_FITS_In,
                    BKEP_Fnames->CP_Filename_FITS_Image1,
                    READONLY,
                    &I_fits_return_status);

    /* Read the keywords that tell the dimensions of the data */
    fits_read_keys_lng(ffp_FITS_In,
                       "NAXIS",
                       1,
                       3,
                       LP_naxes,
                       &I_ndims_found,
                       &I_fits_return_status);

    if (!(I_ndims_found > 2)) LP_naxes[2] = 1;

    /* BKEP_FITS->I_Length_X = LP_naxes[0]; */
    /* BKEP_FITS->I_Length_Y = LP_naxes[1]; */
    /* I_Length_X and I_Length_Y in these two lines should be switched
       because of the way the image is stored in memory.
    */

    BKEP_FITS->I_Length_Y = LP_naxes[0];
    BKEP_FITS->I_Length_X = LP_naxes[1];
    BKEP_FITS->I_Num_Frames = LP_naxes[2];


    /* Allocate memory for the data */
    /* Don't try if problem with fits file */
    if(I_fits_return_status)
    {BKEP_Stat->I_status = READ_IMAGE|FITS_MSG_MASK|I_fits_return_status; return;}

    *DPP_Data_Image1 = (double *) calloc(LP_naxes[0]*LP_naxes[1]*LP_naxes[2], sizeof(double));
    /* Use I_fits_return_status to store status,
       so if there was a problem with calloc
       subsequent fits functions can detect it */
    if(*DPP_Data_Image1==NULL) I_fits_return_status = MALLOC_FAILED;

    /* Read the data */
    fits_read_img(ffp_FITS_In,
                  TDOUBLE,
                  1,
                  LP_naxes[0]*LP_naxes[1]*LP_naxes[2],
                  &D_nullval,
                  *DPP_Data_Image1,
                  &I_anynull,
                  &I_fits_return_status);

    fits_close_file(ffp_FITS_In,
      &I_fits_return_status);

    /* Return the fits status as bkgest status, with mask to say so */
    if(I_fits_return_status)
    BKEP_Stat->I_status = READ_IMAGE|FITS_MSG_MASK|I_fits_return_status;


    if(BKEP_Stat->I_Verbose) {
    printf("bkgest_read_image1: Input FITS file: %s\n",
    BKEP_Fnames->CP_Filename_FITS_Image1);
    printf("bkgest_read_image1: I_Length_X = %d\n", BKEP_FITS->I_Length_X);
    printf("bkgest_read_image1: I_Length_Y = %d\n", BKEP_FITS->I_Length_Y);
    printf("bkgest_read_image1: I_Num_Frames = %d\n", BKEP_FITS->I_Num_Frames);
    }


}
