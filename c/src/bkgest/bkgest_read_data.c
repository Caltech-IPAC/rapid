/***********************************************
bkgest_read_data.c

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


***********************************************/

#include <time.h>
#include <stdio.h>
#include "bkgest_errcodes.h"
#include <string.h>
#include <stdlib.h>
#include "fitsio.h"
#include "bkgest.h"
#include "bkgest_defs.h"


void bkgest_read_data(BKE_Filenames  *BKEP_Fnames,
                      BKE_FITSinfo   *BKEP_FITS,
                      double         **DPP_Data_Image1,
                      int            **DPP_Data_Mask,
                      BKE_Status     *BKEP_Stat)
{
    int i, j, k;
    int I_fits_return_status=0, I_ndims_found, I_anynull;
    long LP_naxes[3];
    double D_nullval=0;


    if (BKEP_Stat->I_status) return;

    /* Get the data and keyword values from the input image1 file */
    bkgest_read_image1(BKEP_Fnames, BKEP_FITS, DPP_Data_Image1, BKEP_Stat);

    if (strcmp(BKEP_Fnames->CP_Filename_FITS_Mask,""))
        bkgest_read_mask(BKEP_Fnames, BKEP_FITS, DPP_Data_Mask, BKEP_Stat);
}
