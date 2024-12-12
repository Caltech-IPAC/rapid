/***********************************************
bkgest_init_constants.c

Purpose

Set constant values as defined in the header file bkgest.h.

Overview


Definitions of Variables

See "main" function bkgest_exec.c and bkgest_defs.h

***********************************************/

#include <stdio.h>
#include "bkgest.h"
#include "bkgest_defs.h"
#include "bkgest_errcodes.h"

void bkgest_init_constants(BKE_Constants    *BKEP_Const,
                           BKE_Filenames    *BKEP_Fnames,
                           BKE_Pathnames   *BKEP_Paths,
                           BKE_FITSinfo    *BKEP_FITS,
                           BKE_Computation *BKEP_Comp,
                           BKE_Status      *BKEP_Stat)
{


   /* Initialize all status flags. */

   BKEP_Stat->I_status = 0;
   BKEP_Stat->I_NaNFlag = 0;
   BKEP_Stat->I_Debug = 0;
   BKEP_Stat->I_Verbose = 0;
   BKEP_Stat->I_SuperVerbose = 0;
   BKEP_Stat->I_NaNCount = 0;


   /* Set mask mask value for image-data omission. */

   BKEP_Const->I_MaskMask = 0x00000000;


   /* Set the clippedmean-filter input window size. */

   BKEP_Const->D_Window = 7;


   /* Set the clippedmean-filter grid spacing. */

   BKEP_Const->D_GridSpacing = 16;


   /* Set maximum tolerable percentage of bad pixels. */

   BKEP_Const->D_NBadPixLocTol = 50;
   BKEP_Const->D_NBadPixGloTol = 50;


   /* Set default image pothole value. */

   BKEP_Const->D_Pothole = -1.79769e308;


   /* Set array pointers to NULL in case calloc is never called. */

   BKEP_Comp->DP_Output_Array = NULL;
   BKEP_Comp->DP_Output_Array2 = NULL;
   BKEP_Comp->DP_Output_Array3 = NULL;


   /* Put in stdio defaults in case filenames are not given later. */

   sprintf(BKEP_Fnames->CP_Filename_Namelist, "%s", "");
   sprintf(BKEP_Fnames->CP_Filename_FITS_Image1, "%s", "");
   sprintf(BKEP_Fnames->CP_Filename_FITS_Mask, "%s", "");
   sprintf(BKEP_Fnames->CP_Filename_FITS_Out, "%s", "");
   sprintf(BKEP_Fnames->CP_Filename_FITS_Out2, "%s", "");
   sprintf(BKEP_Fnames->CP_Filename_FITS_Out3, "%s", "");
   sprintf(BKEP_Fnames->CP_Filename_Data_Out, "%s", "");
   sprintf(BKEP_Fnames->CP_Filename_Log, "%s", "stdout");


   /* Put in stdio defaults in case pathnames are not given later. */

   sprintf(BKEP_Paths->CP_Ancillary_File_Path, "%s", ".");


   /* Default operation is global-clippedmean-value calculation. */

   BKEP_FITS->I_Operation = 1;


   /* Default output FITS file is clippedmean image. */

   BKEP_FITS->I_Fits_Type = 1;


   /* Default is compute clippedmean for all planes in data cube of FITS file. */

   BKEP_FITS->I_Data_Plane = 1;

}





