/***********************************************
bkgest_exec.c

Purpose

Module to compute the local background fast.  Grid of clippedmean points.
Bilinear interpolation.  Similar to SExtractor in some respects.

This program takes as input a FITS file.  Depending on
the command-line option specified, this program will
compute a local clippedmean image, or a global clippedmean value,
or both.  The local clippedmean value is computed over a
window of specified number of odd pixels on a side.
Output is either a FITS file, a global clippedmean value,
or both, again, depending on the command-line option
specified.


Overview
Calls the initialization functions and main loop of program. Calls
output function.


Definitions of Variables
See bkgest_defs.h

DP_Data_Image1 = Array for storing first input image data.
DP_Data_Image2 = Array for storing second input image data.

BKE_Const = Structure that holds constants used in processing.
BKE_Fnames = Structure that holds filenames.
BKE_FITS = Structure that holds fits image dimensions and indices.
BKE_Comp = Structure that holds variables used in processing.
BKE_Stat = Structure that holds status, counters, and flags.

***********************************************/

#include <time.h>
#include <stdio.h>
#include "bkgest_errcodes.h"
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include "fitsio.h"
#include "bkgest.h"
#include "bkgest_defs.h"


int main(int argc, char **argv)
{
    double *DP_Data_Image1=NULL;
    int     *DP_Data_Mask = NULL;

    BKE_Constants  BKE_Const;
    BKE_Filenames  BKE_Fnames;
    BKE_Pathnames  BKE_Paths;
    BKE_FITSinfo  BKE_FITS;
    BKE_Computation  BKE_Comp;
    BKE_Status  BKE_Stat;

    clock();

    /* Set constant default values */
    bkgest_init_constants(&BKE_Const, &BKE_Fnames, &BKE_Paths, &BKE_FITS, &BKE_Comp, &BKE_Stat);

    /* Parse the namelist, replacing default values if specified */
    bkgest_parse_namelist(argc, argv, &BKE_Const, &BKE_Fnames, &BKE_Paths, &BKE_FITS, &BKE_Stat);

    /* Parse the command line, replacing values if specified */
    bkgest_parse_args(argc, argv, &BKE_Const, &BKE_Fnames, &BKE_Paths, &BKE_FITS, &BKE_Stat);

    /* Expand any environmental variables passed into filename defintions */
    bkgest_expand_envvar(&BKE_Fnames, &BKE_Stat);

    /* Get the data and keyword values from the input and output files */
    bkgest_read_data(&BKE_Fnames, &BKE_FITS, &DP_Data_Image1, &DP_Data_Mask, &BKE_Stat);

    /* Perform binary operation on the data*/
    bkgest_compute_results(DP_Data_Image1, DP_Data_Mask, &BKE_Const, &BKE_Fnames, &BKE_FITS, &BKE_Comp, &BKE_Stat);

    /* Write results to output file */
    bkgest_output(&BKE_Const, &BKE_Fnames, &BKE_FITS, &BKE_Comp, &BKE_Stat);

    /* Print filename, error message and timing data */
    bkgest_log_writer(&BKE_Const, &BKE_FITS, &BKE_Stat, &BKE_Fnames, &BKE_Paths);

    /* Deallocate memory */
    bkgest_exit(&DP_Data_Image1, &DP_Data_Mask, &BKE_Comp, &BKE_FITS, &BKE_Fnames, &BKE_Stat);

    /* Send return value */
    exit(BKE_Stat.I_status);

}




