/***********************************************
bkgest_output_stdkeywords.c

Purpose

Write the standard keyword values to the open fits output file.

Overview
Use cfitsio functions.

Definitions of Variables

External:
See bkgest_defs.h and bkgest_exec.c


Internal:
I_fits_return_status = Resturn value of fits function.
I_Num_In = Integer value to be written to header.
CP_Keyname = name of Key to be written.
CP_Comment = String value to be written in comment field of header.


***********************************************/

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "bkgest.h"
#include "bkgest_defs.h"
#include "bkgest_errcodes.h"


void bkgest_output_stdkeywords(fitsfile         *ffp_FITS,
                               BKE_Filenames    *BKEP_Fnames,
                               BKE_FITSinfo     *BKEP_FITS,
                               BKE_Status       *BKEP_Stat)
{
    int I_fits_return_status=0;
    long I_Num_In,I_Num_Out;
    char CP_Keyname[FLEN_KEYWORD];
    char CP_Comment[FLEN_COMMENT];
    char CP_Keyvalue[FLEN_VALUE];

       if ((BKEP_Stat->I_status & RIGHT_MASK) >= 64) return;

    /* Write the keyword values */


    sprintf(CP_Keyname, "%s", "SIMPLE");
    sprintf(CP_Comment, "%s", "STANDARD FITS FORMAT");
    I_Num_Out = 1;
    fits_update_key(ffp_FITS,
                    TLOGICAL,
                    CP_Keyname,
                    &I_Num_Out,
                    CP_Comment,
                    &I_fits_return_status);


    sprintf(CP_Keyname, "%s", "BITPIX  ");
    sprintf(CP_Comment, "%s", "FOUR-BYTE SINGLE PRECISION FLOATING POINT");
    I_Num_Out = -8*sizeof(float);
    fits_update_key(ffp_FITS,
                    TLONG,
                    CP_Keyname,
                    &I_Num_Out,
                    CP_Comment,
                    &I_fits_return_status);

    sprintf(CP_Keyname, "%s", "NAXIS");
    sprintf(CP_Comment, "%s", "STANDARD FITS FORMAT");

    fits_read_key_lng(ffp_FITS,
                    CP_Keyname,
                    &I_Num_In,
                    CP_Comment,
                    &I_fits_return_status);

    if (BKEP_Stat->I_Verbose)
       printf("Input NAXIS = %ld\n",I_Num_In);

    sprintf(CP_Keyname, "%s", "NAXIS1");
    sprintf(CP_Comment, "%s", "STANDARD FITS FORMAT");
    I_Num_Out = (long) BKEP_FITS->I_Length_Y;
    fits_update_key(ffp_FITS,
                    TLONG,
                    CP_Keyname,
                    &I_Num_Out,
                    CP_Comment,
                    &I_fits_return_status);

    sprintf(CP_Keyname, "%s", "NAXIS2");
    sprintf(CP_Comment, "%s", "STANDARD FITS FORMAT");
    I_Num_Out = (long) BKEP_FITS->I_Length_X;
    fits_update_key(ffp_FITS,
                    TLONG,
                    CP_Keyname,
                    &I_Num_Out,
                    CP_Comment,
                    &I_fits_return_status);


    /* Assume this keyword will be present in the input image, and that
       no change to it is necessary by bkgest.

    sprintf(CP_Keyname, "%s", "BUNIT");
    strcpy(CP_Keyvalue,"DN");
    sprintf(CP_Comment, "%s", "Units of image data");
    fits_update_key(ffp_FITS,
                    TSTRING,
                    CP_Keyname,
                    CP_Keyvalue,
                    CP_Comment,
                    &I_fits_return_status);

    fits_flush_file(ffp_FITS,
                    &I_fits_return_status);

    if (BKEP_Stat->I_Verbose)
       printf("BKE_output_stdkeywords:  I_fits_return_status = %d\n",
          I_fits_return_status);

    */


    if(I_fits_return_status)
        BKEP_Stat->I_status = STD_KEYWORDS|FITS_MSG_MASK|I_fits_return_status;

}





