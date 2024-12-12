/***********************************************
bkgest_output.c

Purpose

Save the bkgest outputs to files.

Overview
Creates output files per command-input control.
Append the first layer of the input FITS file onto
the data of the output FITS file.

Definitions of Variables

External:
See bkgest_defs.h and bkgest_exec.c


Internal:

I_Length_X = Length of first dimension of data.
I_Length_Y = Length of second dimension of data.
I_Num_Frames  = Number of Frames of data.
I_Tot_Pixels = Total number of pixels in image.
I_fits_return_status = Return value of fits function.

ffp_FITS = Handle to output FITS file.

ffp_DATA = Handle to output IPAC-table.

***********************************************/

#include <stdio.h>
#include "bkgest_errcodes.h"
#include <string.h>
#include <stdlib.h>
#include <time.h>
#include "fitsio.h"
#include "bkgest.h"
#include "bkgest_defs.h"


void bkgest_output(BKE_Constants   *BKEP_Const,
                   BKE_Filenames   *BKEP_Fnames,
                   BKE_FITSinfo    *BKEP_FITS,
                   BKE_Computation *BKEP_Comp,
                   BKE_Status      *BKEP_Stat)
{
    int i, j, k;
    int I_Length_X, I_Length_Y, I_Num_Frames;
    int I_Tot_Pixels;
    int I_fits_return_status=0;
    int morekeys = 0;

    fitsfile  *ffp_FITS;
    fitsfile  *ffp_FITS_In;

    char *tblfile;
    int nlen;

    FILE *ffp_DATA, *fopen();

    time_t  currenttime;
    char    *datetime;

    if (BKEP_Stat->I_Verbose) {
        printf("bkgest_output: status = %d\n", BKEP_Stat->I_status);
        printf("bkgest_output: status & RIGHT_MASK = %d\n", BKEP_Stat->I_status & RIGHT_MASK);
    }

    if ((BKEP_Stat->I_status & RIGHT_MASK) >= 64) return;

    /* Copy structure info to local variables */
    I_Length_X = BKEP_FITS->I_Length_X;
    I_Length_Y = BKEP_FITS->I_Length_Y;
    I_Num_Frames = BKEP_FITS->I_Num_Frames;
    I_Tot_Pixels = I_Length_X*I_Length_Y*I_Num_Frames;


    /* Output global clippedmean value to IPAC-table if available. */

    if (BKEP_FITS->I_Operation == 2 || BKEP_FITS->I_Operation == 3) {

       tblfile = BKEP_Fnames->CP_Filename_Data_Out;
       if (BKEP_Fnames->CP_Filename_Data_Out[0] == '!')
          tblfile = strtok(BKEP_Fnames->CP_Filename_Data_Out,"!");

       if (BKEP_Stat->I_Verbose)
          printf("Output IPAC-table file = %s\n",tblfile);

       if ((ffp_DATA = fopen(tblfile, "w")) == NULL) {

          BKEP_Stat->I_status = OUTPUT_MAGS|FOPEN_FAILED; return;

       } else {

          time(&currenttime);
          datetime = ctime(&currenttime);

          if (BKEP_Stat->I_Verbose) {
             printf("\\character comment = Output from %s, version %2.1f\n",
                PROGRAM, BKEVERSN);

             printf("\\character Date-Time = %s",datetime);

             printf("\\character inputFITSfile = %s\n",
                BKEP_Fnames->CP_Filename_FITS_Image1);

             for (k=0;k<I_Num_Frames;k++) {
                printf("\\real globalclippedmean%d = %f\n",
                   k,BKEP_Comp->GlobalClippedMeanValue[k]);
                printf("\\real globalscale%d = %f\n",
                   k,BKEP_Comp->GlobalScaleValue[k]);
                printf("\\real numberbadpixels%d = %lu\n",
                   k,BKEP_Comp->NumberBadPixels[k]);
             }
          }

          fprintf(ffp_DATA,"\\character comment = Output from %s, version %5.2f\n",
             PROGRAM, BKEVERSN);

          fprintf(ffp_DATA,"\\character Date-Time = %s",datetime);

          fprintf(ffp_DATA,"\\character inputFITSfile = %s\n",
             BKEP_Fnames->CP_Filename_FITS_Image1);

          fprintf(ffp_DATA,"\\real maskmask = %d\n",
             BKEP_Const->I_MaskMask);

          for (k=0;k<I_Num_Frames;k++) {
             fprintf(ffp_DATA,"\\real globalclippedmean%d = %f\n",
                k,BKEP_Comp->GlobalClippedMeanValue[k]);
             fprintf(ffp_DATA,"\\real globalscale%d = %f\n",
                k,BKEP_Comp->GlobalScaleValue[k]);
             fprintf(ffp_DATA,"\\real numberbadpixels%d = %lu\n",
                k,BKEP_Comp->NumberBadPixels[k]);
          }

       }

       fclose(ffp_DATA);

    }


    if (BKEP_FITS->I_Fits_Type == 1 || BKEP_FITS->I_Fits_Type == 3) {


       /* OUTPUT IMAGE #1. */

       /* Open the FITS file for writing. */

       if (BKEP_Stat->I_Verbose)
          printf("Opening output FITS file #1...\n");

       fits_create_file(&ffp_FITS,
       BKEP_Fnames->CP_Filename_FITS_Out,
       &I_fits_return_status);

       if (BKEP_Stat->I_Verbose)
          printf("I_fits_return_status = %d\n",I_fits_return_status);

       if (I_fits_return_status) {
          BKEP_Stat->I_status = OUTPUT_MAGS|FITS_MSG_MASK|I_fits_return_status;
          return;
       }


       /*
          Copy input-file header to output file.
       */

       if (BKEP_Stat->I_Verbose)
          printf("Copying input-file FITS keywords to output file...\n");

       fits_open_file(&ffp_FITS_In,
                      BKEP_Fnames->CP_Filename_FITS_Image1,
                      READONLY,
                      &I_fits_return_status);

       fits_copy_hdu (ffp_FITS_In, ffp_FITS, morekeys, &I_fits_return_status);

       fits_close_file(ffp_FITS_In, &I_fits_return_status);

       fits_flush_file(ffp_FITS, &I_fits_return_status);

       if (BKEP_Stat->I_Verbose)
          printf("I_fits_return_status = %d\n",I_fits_return_status);

       if (I_fits_return_status) {
          BKEP_Stat->I_status = KEYWORDS|FITS_MSG_MASK|I_fits_return_status;
          return;
       }


       /* Write header keywords FITS file. */

       if (BKEP_Stat->I_Verbose)
          printf("Outputting FITS keywords...\n");

       bkgest_output_keywords(ffp_FITS,
                              BKEP_Const,
                              BKEP_Fnames,
                              BKEP_FITS,
                              BKEP_Comp,
                              BKEP_Stat);

       /* Don't return unless error, not warning. */
       if (((RIGHT_MASK&BKEP_Stat->I_status)&FITS_MSG_MASK) == 4096) return;


       /* Write image-data results to the output FITS file. */

       if (BKEP_Stat->I_Verbose)
          printf("Outputting FITS image #1 data...\n");

       fits_write_img(ffp_FITS,
                      TDOUBLE,
                      1,
                      I_Tot_Pixels,
                      BKEP_Comp->DP_Output_Array,
                      &I_fits_return_status);

       fits_flush_file(ffp_FITS, &I_fits_return_status);

       if (BKEP_Stat->I_Verbose)
          printf("I_fits_return_status = %d\n",I_fits_return_status);

       if (I_fits_return_status) {
          BKEP_Stat->I_status = OUTPUT_MAGS|FITS_MSG_MASK|I_fits_return_status;
          return;
       }


       /* Close the FITS file. */

       if (BKEP_Stat->I_Verbose)
          printf("Closing output FITS file...\n");

       fits_close_file(ffp_FITS, &I_fits_return_status);

       if (BKEP_Stat->I_Verbose)
          printf("I_fits_return_status = %d\n",I_fits_return_status);

       if (I_fits_return_status) {
          BKEP_Stat->I_status = OUTPUT_MAGS|FITS_MSG_MASK|I_fits_return_status;
          return;
       }

       if(BKEP_Stat->I_Verbose)
          printf("bkgest_output: Result written to file:\n%s.\n",
             &BKEP_Fnames->CP_Filename_FITS_Out[1]);


       /* OUTPUT IMAGE #3. */

       /* Open the FITS file for writing. */

       if (BKEP_Stat->I_Verbose)
          printf("Opening output FITS file #3...\n");

       fits_create_file(&ffp_FITS,
                        BKEP_Fnames->CP_Filename_FITS_Out3,
                        &I_fits_return_status);

       if (BKEP_Stat->I_Verbose)
          printf("I_fits_return_status = %d\n",I_fits_return_status);

       if (I_fits_return_status) {
          BKEP_Stat->I_status = OUTPUT_MAGS|FITS_MSG_MASK|I_fits_return_status;
          return;
       }


       /*
          Copy input-file header to output file.
       */

       if (BKEP_Stat->I_Verbose)
          printf("Copying input-file FITS keywords to output file...\n");

       fits_open_file(&ffp_FITS_In,
                      BKEP_Fnames->CP_Filename_FITS_Image1,
                      READONLY,
                      &I_fits_return_status);

       fits_copy_hdu (ffp_FITS_In, ffp_FITS, morekeys, &I_fits_return_status);

       fits_close_file(ffp_FITS_In, &I_fits_return_status);

       fits_flush_file(ffp_FITS, &I_fits_return_status);

       if (BKEP_Stat->I_Verbose)
          printf("I_fits_return_status = %d\n",I_fits_return_status);

       if (I_fits_return_status) {
          BKEP_Stat->I_status = KEYWORDS|FITS_MSG_MASK|I_fits_return_status;
          return;
       }


       /* Write header keywords FITS file. */

       if (BKEP_Stat->I_Verbose)
          printf("Outputting FITS keywords...\n");

       bkgest_output_keywords(ffp_FITS,
                              BKEP_Const,
                              BKEP_Fnames,
                              BKEP_FITS,
                              BKEP_Comp,
                              BKEP_Stat);

       /* Don't return unless error, not warning. */
       if (((RIGHT_MASK&BKEP_Stat->I_status)&FITS_MSG_MASK) == 4096) return;


       /* Write image-data results to the output FITS file. */

       if (BKEP_Stat->I_Verbose)
          printf("Outputting FITS image #3 data...\n");

       fits_write_img(ffp_FITS,
                      TDOUBLE,
                      1,
                      I_Tot_Pixels,
                      BKEP_Comp->DP_Output_Array3,
                      &I_fits_return_status);

       fits_flush_file(ffp_FITS, &I_fits_return_status);

       if (BKEP_Stat->I_Verbose)
          printf("I_fits_return_status = %d\n",I_fits_return_status);

       if (I_fits_return_status) {
          BKEP_Stat->I_status = OUTPUT_MAGS|FITS_MSG_MASK|I_fits_return_status;
          return;
       }


       /* Close the FITS file. */

       if (BKEP_Stat->I_Verbose)
          printf("Closing output FITS file...\n");

       fits_close_file(ffp_FITS, &I_fits_return_status);

       if (BKEP_Stat->I_Verbose)
          printf("I_fits_return_status = %d\n",I_fits_return_status);

       if (I_fits_return_status) {
          BKEP_Stat->I_status = OUTPUT_MAGS|FITS_MSG_MASK|I_fits_return_status;
          return;
       }

       if(BKEP_Stat->I_Verbose)
          printf("bkgest_output: Result written to file:\n%s.\n",
             &BKEP_Fnames->CP_Filename_FITS_Out3[1]);
    }



    if (strcmp(BKEP_Fnames->CP_Filename_FITS_Out2, "") == 0) return;


    if (BKEP_FITS->I_Fits_Type == 2 || BKEP_FITS->I_Fits_Type == 3) {

       /* OUTPUT IMAGE #2. */

       /* Open the FITS file for writing. */

       if (BKEP_Stat->I_Verbose)
          printf("Opening output FITS file #2...\n");

       fits_create_file(&ffp_FITS,
                        BKEP_Fnames->CP_Filename_FITS_Out2,
                        &I_fits_return_status);

       if (BKEP_Stat->I_Verbose)
          printf("I_fits_return_status = %d\n",I_fits_return_status);

       if(I_fits_return_status) {
          BKEP_Stat->I_status = OUTPUT_MAGS|FITS_MSG_MASK|I_fits_return_status;
          return;
       }


       /*
          Copy input-file header to output file.
       */

       if (BKEP_Stat->I_Verbose)
          printf("Copying input-file FITS keywords to output file...\n");

       fits_open_file(&ffp_FITS_In,
                      BKEP_Fnames->CP_Filename_FITS_Image1,
                      READONLY,
                      &I_fits_return_status);

       fits_copy_hdu (ffp_FITS_In, ffp_FITS, morekeys, &I_fits_return_status);

       fits_close_file(ffp_FITS_In, &I_fits_return_status);

       fits_flush_file(ffp_FITS, &I_fits_return_status);

       if (BKEP_Stat->I_Verbose)
          printf("I_fits_return_status = %d\n",I_fits_return_status);

       if (I_fits_return_status) {
          BKEP_Stat->I_status = KEYWORDS|FITS_MSG_MASK|I_fits_return_status;
          return;
       }


       /* Write header keywords FITS file. */

       if (BKEP_Stat->I_Verbose)
          printf("Outputting FITS keywords...\n");

       bkgest_output_keywords(ffp_FITS,
                              BKEP_Const,
                              BKEP_Fnames,
                              BKEP_FITS,
                              BKEP_Comp,
                              BKEP_Stat);

       /* Don't return unless error, not warning. */
       if (((RIGHT_MASK&BKEP_Stat->I_status)&FITS_MSG_MASK) == 4096) return;


       /* Write image-data results to the output FITS file. */

       if (BKEP_Stat->I_Verbose)
          printf("Outputting FITS image #2 data...\n");

       fits_write_img(ffp_FITS,
                      TDOUBLE,
                      1,
                      I_Tot_Pixels,
                      BKEP_Comp->DP_Output_Array2 ,
                      &I_fits_return_status);

       fits_flush_file(ffp_FITS, &I_fits_return_status);

       if (BKEP_Stat->I_Verbose)
          printf("I_fits_return_status = %d\n",I_fits_return_status);

       if (I_fits_return_status) {
          BKEP_Stat->I_status = OUTPUT_MAGS|FITS_MSG_MASK|I_fits_return_status;
          return;
       }


       /* Close the FITS file. */

       if (BKEP_Stat->I_Verbose)
          printf("Closing output FITS file...\n");

       fits_close_file(ffp_FITS, &I_fits_return_status);

       if(I_fits_return_status)
          BKEP_Stat->I_status = OUTPUT_MAGS|FITS_MSG_MASK|I_fits_return_status;

       if(BKEP_Stat->I_Verbose)
          printf("bkgest_output: Result written to file:\n%s.\n",
          &BKEP_Fnames->CP_Filename_FITS_Out2[1]);

    }


}





