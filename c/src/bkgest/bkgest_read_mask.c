/***********************************************
bkgest_read_mask.c

Purpose

Read mask image from a FITS file and save it into an array.

***********************************************/

#include <time.h>
#include <stdio.h>
#include "bkgest_errcodes.h"
#include <string.h>
#include <stdlib.h>
#include "fitsio.h"
#include "bkgest.h"
#include "bkgest_defs.h"


void bkgest_read_mask(BKE_Filenames *BKEP_Fnames,
                      BKE_FITSinfo  *BKEP_FITS,
                      int           **DPP_Data_Mask,
                      BKE_Status    *BKEP_Stat)
{
   int i, j, k;
   int I_fits_return_status=0, I_ndims_found, I_anynull;
   long LP_naxes[3], I_Index;
   double D_nullval=0;
   fitsfile *ffp_FITS_In;
   long bitpix;
   double datashift;
   int I_Length_X, I_Length_Y, I_Num_Frames;
   char CP_Keyname[FLEN_KEYWORD];
   char CP_Comment[FLEN_COMMENT];

   if (BKEP_Stat->I_status) return;

   if (BKEP_Stat->I_Verbose)
      printf("bkgest_read_mask: Reading input data from FITS file...\n");

   fits_open_file(&ffp_FITS_In,
                  BKEP_Fnames->CP_Filename_FITS_Mask,
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


   /* Don't try if problem with fits file */

   if (I_fits_return_status) {
      BKEP_Stat->I_status = READ_IMAGE|FITS_MSG_MASK|I_fits_return_status;
      return;
   }

   /* BKEP_FITS->I_Length_X = LP_naxes[0]; */
   /* BKEP_FITS->I_Length_Y = LP_naxes[1]; */
   /*    I_Length_X and I_Length_Y in these two lines should be switched
      because of the way the image is stored in memory.
   */

   I_Length_Y = LP_naxes[0];
   I_Length_X = LP_naxes[1];
   I_Num_Frames = LP_naxes[2];

   BKEP_FITS->I_Length_X = I_Length_X;
   BKEP_FITS->I_Length_Y = I_Length_Y;


   /* The image should have only one data plane. */

   if (I_Num_Frames != 1) {
      BKEP_Stat->I_status = READ_IMAGE|TOO_MANY_DATA_PLANES;
      return;
   }


   /* Get bitpix for proper conversion to unsigned values. */

   sprintf(CP_Keyname, "%s", "BITPIX  ");

   fits_read_key_lng(ffp_FITS_In,
  CP_Keyname,
  &bitpix,
  CP_Comment,
  &I_fits_return_status);

   if (BKEP_Stat->I_Verbose)
       printf("Status after reading BITPIX = %d\n",
              I_fits_return_status);

   /* Allocate input image memory. */

   *DPP_Data_Mask = (int *) calloc(LP_naxes[0]*LP_naxes[1]*LP_naxes[2],
      sizeof(int));


   /* Use I_fits_return_status to store status,
      so if there was a problem with calloc
      subsequent fits functions can detect it */

   if (*DPP_Data_Mask==NULL) I_fits_return_status = MALLOC_FAILED;

   if (BKEP_Stat->I_Verbose)
       printf("Status after malloc = %d\n",
              I_fits_return_status);

   /* Read the data */

   fits_read_img(ffp_FITS_In,
                 TINT,
                 1,
                 LP_naxes[0]*LP_naxes[1]*LP_naxes[2],
                 &D_nullval,
                 *DPP_Data_Mask,
                 &I_anynull,
                 &I_fits_return_status);

   if (BKEP_Stat->I_Verbose)
       printf("Status after reading FITS mask = %d\n",
              I_fits_return_status);


   fits_close_file(ffp_FITS_In,
    &I_fits_return_status);

   if (BKEP_Stat->I_Verbose)
       printf("Status after closing FITS file = %d\n",
              I_fits_return_status);

   /* Return the fits status as bkgest status, with image to say so */
   if (I_fits_return_status)
      BKEP_Stat->I_status = READ_IMAGE|FITS_MSG_MASK|I_fits_return_status;

   if (BKEP_Stat->I_Verbose) {
      printf("bkgest_read_mask: Input FITS file: %s\n",
         BKEP_Fnames->CP_Filename_FITS_Mask);
      printf("bkgest_read_mask: I_Length_X = %d\n", I_Length_X);
      printf("bkgest_read_mask: I_Length_Y = %d\n", I_Length_Y);
      printf("bkgest_read_mask: I_Num_Frames = %d\n",
         I_Num_Frames);
      printf("bkgest_read_mask: bitpix = %ld\n",
         bitpix);
   }


}







