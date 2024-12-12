/***********************************************
bkgest_output_constkeywords.c

Purpose

Write the keyword values for computation constants
to the open fits output file.

Overview
Use cfitsio functions.

Definitions of Variables

External:
See bkgest_defs.h and bkgest_exec.c


Internal:
I_fits_return_status = Resturn value of fits function.
D_Num_In = Floating point value to be written to header.
CP_Keyname = name of Key to be written.
CP_Comment = String value to be written in comment field of header.

***********************************************/

#include <stdio.h>
#include "bkgest_errcodes.h"
#include <string.h>
#include <stdlib.h>
#include "fitsio.h"
#include "bkgest.h"
#include "bkgest_defs.h"
#include "nanvalue.h"


void bkgest_output_constkeywords(fitsfile        *ffp_FITS,
                                 BKE_Filenames   *BKEP_Fnames,
                                 BKE_Constants   *BKEP_Const,
                                 BKE_Computation  *BKEP_Comp,
                                 BKE_FITSinfo   *BKEP_FITS,
                                 BKE_Status   *BKEP_Stat)
{
   int k,I_fits_return_status=0,I_Num_Frames,I_Num_In;
   double D_Num_In;
   char CP_Keyname[FLEN_KEYWORD], CP_Comment[FLEN_COMMENT];
   char    CP_Keyvalue[FLEN_VALUE];


   if ((BKEP_Stat->I_status & RIGHT_MASK) >= 64) return;

   I_Num_Frames = BKEP_FITS->I_Num_Frames;

   sprintf(CP_Keyname, "%s", "INBKEWIN");
   sprintf(CP_Comment, "%s",
      "ClippedMean-value input-grid width (pixels)");
   I_Num_In = BKEP_Const->D_Window;
   fits_write_key(ffp_FITS,
                  TINT,
                  CP_Keyname,
                  &I_Num_In,
                  CP_Comment,
                  &I_fits_return_status);

   fits_flush_file(ffp_FITS,
                   &I_fits_return_status);

   if (BKEP_Stat->I_Verbose)
      printf("BKE_output_constkeywords:  I_fits_return_status = %d\n",
         I_fits_return_status);

   if (I_fits_return_status) {
      BKEP_Stat->I_status =
         CONST_KEYWORDS|FITS_MSG_MASK|I_fits_return_status;
      return;
   }

   sprintf(CP_Keyname, "%s", "INBKEGRD");
   sprintf(CP_Comment, "%s", "ClippedMean-value grid spacing (pixels)");
   I_Num_In = BKEP_Const->D_GridSpacing;
   fits_write_key(ffp_FITS,
                  TINT,
                  CP_Keyname,
                  &I_Num_In,
                  CP_Comment,
                  &I_fits_return_status);

   fits_flush_file(ffp_FITS,
                   &I_fits_return_status);

   if (BKEP_Stat->I_Verbose)
      printf("BKE_output_constkeywords:  I_fits_return_status = %d\n",
         I_fits_return_status);

   if (I_fits_return_status) {
      BKEP_Stat->I_status =
         CONST_KEYWORDS|FITS_MSG_MASK|I_fits_return_status;
      return;
   }

   sprintf(CP_Keyname, "%s", "BKELOCBP");
   sprintf(CP_Comment, "%s", "Percentage local-clippedmean bad pixels tolerated");
   I_Num_In = BKEP_Const->D_NBadPixLocTol;
   fits_write_key(ffp_FITS,
                  TINT,
                  CP_Keyname,
                  &I_Num_In,
                  CP_Comment,
                  &I_fits_return_status);

   fits_flush_file(ffp_FITS,
                   &I_fits_return_status);

   if (BKEP_Stat->I_Verbose)
      printf("BKE_output_constkeywords:  I_fits_return_status = %d\n",
         I_fits_return_status);

   if (I_fits_return_status) {
      BKEP_Stat->I_status =
         CONST_KEYWORDS|FITS_MSG_MASK|I_fits_return_status;
      return;
   }

   sprintf(CP_Keyname, "%s", "BKEGLOBP");
   sprintf(CP_Comment, "%s", "Percentage global-clippedmean bad pixels tolerated");
   I_Num_In = BKEP_Const->D_NBadPixGloTol;
   fits_write_key(ffp_FITS,
                  TINT,
                  CP_Keyname,
                  &I_Num_In,
                  CP_Comment,
                  &I_fits_return_status);

   fits_flush_file(ffp_FITS,
                   &I_fits_return_status);

   if (BKEP_Stat->I_Verbose)
      printf("BKE_output_constkeywords:  I_fits_return_status = %d\n",
         I_fits_return_status);

   if (I_fits_return_status) {
      BKEP_Stat->I_status =
         CONST_KEYWORDS|FITS_MSG_MASK|I_fits_return_status;
      return;
   }

   sprintf(CP_Keyname, "%s", "DPLANEFL");
   sprintf(CP_Comment, "%s", "Data planes processed: 1=All, 2=First, 3=Last");
   I_Num_In = BKEP_FITS->I_Data_Plane;
   fits_write_key(ffp_FITS,
                  TINT,
                  CP_Keyname,
                  &I_Num_In,
                  CP_Comment,
                  &I_fits_return_status);

   fits_flush_file(ffp_FITS,
                   &I_fits_return_status);

   if (BKEP_Stat->I_Verbose)
      printf("BKE_output_constkeywords:  I_fits_return_status = %d\n",
         I_fits_return_status);

   if (I_fits_return_status) {
      BKEP_Stat->I_status =
         CONST_KEYWORDS|FITS_MSG_MASK|I_fits_return_status;
      return;
   }



   if (BKEP_FITS->I_Operation == 2 || BKEP_FITS->I_Operation == 3) {

      for(k=0;k<I_Num_Frames;k++) {

         sprintf(CP_Keyname, "%s%d", "GLOBKEV",k);
         sprintf(CP_Comment, "%s", "Global clippedmean value");

         if( BKEP_Comp->GlobalClippedMeanValue[k] != 0 &&
            iznanorinfd(BKEP_Comp->GlobalClippedMeanValue[k])) {

            strcpy(CP_Keyvalue,"NaN");
            fits_update_key(ffp_FITS,
                            TSTRING,
                            CP_Keyname,
                            CP_Keyvalue,
                            CP_Comment,
                            &I_fits_return_status);

         } else {

            D_Num_In = BKEP_Comp->GlobalClippedMeanValue[k];
            fits_write_key(ffp_FITS,
                           TDOUBLE,
                           CP_Keyname,
                           &D_Num_In,
                           CP_Comment,
                           &I_fits_return_status);

         }

         sprintf(CP_Keyname, "%s%d", "GLOSCLV",k);
         sprintf(CP_Comment, "%s", "Global scale value");

         if( BKEP_Comp->GlobalScaleValue[k] != 0 &&
            iznanorinfd(BKEP_Comp->GlobalScaleValue[k])) {

            strcpy(CP_Keyvalue,"NaN");
            fits_update_key(ffp_FITS,
                            TSTRING,
                            CP_Keyname,
                            CP_Keyvalue,
                            CP_Comment,
                            &I_fits_return_status);

         } else {

            D_Num_In = BKEP_Comp->GlobalScaleValue[k];
            fits_write_key(ffp_FITS,
                           TDOUBLE,
                           CP_Keyname,
                           &D_Num_In,
                           CP_Comment,
                           &I_fits_return_status);

         }

      }

      fits_flush_file(ffp_FITS,
                      &I_fits_return_status);

      if (BKEP_Stat->I_Verbose)
         printf("BKE_output_constkeywords:  I_fits_return_status = %d\n",
            I_fits_return_status);

         if (I_fits_return_status)
         {
            BKEP_Stat->I_status =
               CONST_KEYWORDS|FITS_MSG_MASK|I_fits_return_status;
            return;
         }

   }

   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Mask, "") != 0) {

        sprintf(CP_Keyname, "%s", "BKEMMSK");
        sprintf(CP_Comment, "%s", "Mask mask");
        I_Num_In = BKEP_Const->I_MaskMask;
        fits_write_key(ffp_FITS,
                       TINT,
                       CP_Keyname,
                       &I_Num_In,
                       CP_Comment,
                       &I_fits_return_status);

        fits_flush_file(ffp_FITS,
                        &I_fits_return_status);

        if (BKEP_Stat->I_Verbose)
            printf("bkgest_output_constkeywords (%s):  I_fits_return_status = %d\n",
                   CP_Keyname,
                   I_fits_return_status);

        if (I_fits_return_status) {
            BKEP_Stat->I_status =
                CONST_KEYWORDS|FITS_MSG_MASK|I_fits_return_status;
            return;
        }

   }

}
