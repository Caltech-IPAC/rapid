/***********************************************
bkgest_exit.c

Purpose

Cleanly exit the program.

Overview
Deallocate memory and print any diagnostics.

Definitions of Variables

See bkgest_defs.h and bkgest_exec.c


***********************************************/

#include <time.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "bkgest.h"
#include "bkgest_defs.h"
#include "bkgest_errcodes.h"


void bkgest_exit(double           **DPP_Data_Image1,
                 int              **DPP_Data_Mask,
                 BKE_Computation  *BKEP_Comp,
                 BKE_FITSinfo     *BKEP_FITS,
                 BKE_Filenames    *BKEP_Fnames,
                 BKE_Status       *BKEP_Stat)
{

   if ((BKEP_Stat->I_status & RIGHT_MASK) >= 64) return;


   /* Allocated in bkgest_read_data or its subfunctions */

   free(*DPP_Data_Image1);

   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Mask,""))
       free(*DPP_Data_Mask);


   /* Allocated in bkgest_compute_results or its subfunctions */

   free(BKEP_Comp->DP_Output_Array);
   free(BKEP_Comp->DP_Output_Array3);

   if (BKEP_FITS->I_Fits_Type == 2 || BKEP_FITS->I_Fits_Type == 3)
      free(BKEP_Comp->DP_Output_Array2);

   free(BKEP_Comp->GlobalClippedMeanValue);
   free(BKEP_Comp->GlobalScaleValue);
   free(BKEP_Comp->NumberBadPixels);

   if(BKEP_Stat->I_Verbose)
      printf("bkgest_exit: Memory Deallocated.\n");

}










