/***********************************************
bkgest_output_keywords.c

Purpose

Write the keyword values to the open fits output file.

Overview


Definitions of Variables

External:
See bkgest_defs.h and bkgest_exec.c


Internal:
I_fits_return_status = Resturn value of fits function.
I_Operation = Which operation was performed.
I_DoConstant = Flags whether operation was writing a constant.
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

void bkgest_output_keywords(fitsfile         *ffp_FITS,
                            BKE_Constants    *BKEP_Const,
                            BKE_Filenames    *BKEP_Fnames,
                            BKE_FITSinfo     *BKEP_FITS,
                            BKE_Computation  *BKEP_Comp,
                            BKE_Status       *BKEP_Stat)
{
    int I_fits_return_status=0, I_Operation;
    char CP_Keyname[FLEN_KEYWORD];
    char    CP_Comment[FLEN_COMMENT];
    char    CP_Keyvalue[FLEN_VALUE];
    double  version;


   if ((BKEP_Stat->I_status & RIGHT_MASK) >= 64) return;


    /* Copy structure info to local variable */

    I_Operation = BKEP_FITS->I_Operation;


    /* Write the keywords that must be in every FITS file */

    if (BKEP_Stat->I_Verbose)
       printf("Writing mandatory header keywords to output FITS file...\n");

    bkgest_output_stdkeywords(ffp_FITS, BKEP_Fnames, BKEP_FITS, BKEP_Stat);
    if(BKEP_Stat->I_status) return;

    if (BKEP_Stat->I_Verbose) {
       printf("Writing date to output FITS file...\n");
       printf("BKE_output_keywords: I_fits_return_status = %d\n",
          I_fits_return_status);
    }

    fits_write_date(ffp_FITS, &I_fits_return_status);

    fits_flush_file(ffp_FITS, &I_fits_return_status);

    if (BKEP_Stat->I_Verbose)
       printf("BKE_output_keywords: I_fits_return_status = %d\n",
          I_fits_return_status);

    if (I_fits_return_status) {
       BKEP_Stat->I_status = DATE|I_fits_return_status;
       return;
    }

    /*
       Description of the data in this FITS file.  Input string to CP_Keyname
       should be no longer than 8 characters.  Otherwise, data in memory will
       be stepped on.
    */

    sprintf(CP_Keyname, "%s", "BKEPROC");
    strcpy(CP_Keyvalue,PROGRAM);
    if (I_Operation==1)
       sprintf(CP_Comment, "%s", "LOCAL MEDIAN IMAGE CALCULATION");
    else
    if (I_Operation==2)
       sprintf(CP_Comment, "%s", "GLOBAL MEDIAN CALCULATION");
    else
    if (I_Operation==3)
       sprintf(CP_Comment, "%s", "GLOBAL AND LOCAL MEDIAN CALCULATION");


    fits_write_key_str(ffp_FITS,
      CP_Keyname, CP_Keyvalue,
      CP_Comment,
      &I_fits_return_status);


    sprintf(CP_Keyname, "%s", PROGRAMABB);
    strcat(CP_Keyname, "VERSN");
    version = BKEVERSN;
    sprintf(CP_Comment, "%s%s%s", "Version number of ",PROGRAM," program");

    fits_write_key_fixflt(ffp_FITS,
      CP_Keyname, version, 2,
      CP_Comment,
      &I_fits_return_status);

    fits_flush_file(ffp_FITS, &I_fits_return_status);

    if (BKEP_Stat->I_Verbose)
       printf("BKE_output_keywords: I_fits_return_status = %d\n",
          I_fits_return_status);

    if (I_fits_return_status) {
       BKEP_Stat->I_status = KEYWORDS|FITS_MSG_MASK|I_fits_return_status;
       return;
    }

    if (BKEP_Stat->I_Verbose)
       printf("Writing constant header keywords to output FITS file...\n");

    bkgest_output_constkeywords(ffp_FITS, BKEP_Fnames, BKEP_Const, BKEP_Comp, BKEP_FITS, BKEP_Stat);

    if (BKEP_Stat->I_Verbose)
       printf("Writing filename header keywords to output FITS file...\n");

    /* Files associated with this one. */
    bkgest_output_filenkeywords(ffp_FITS, BKEP_Fnames, BKEP_FITS, BKEP_Stat);

}





