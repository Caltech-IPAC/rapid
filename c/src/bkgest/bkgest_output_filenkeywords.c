/***********************************************
bkgest_output_filenkeywords.c

Purpose

Write the keyword values for input and output filenames
to the open fits output file.

Overview
Use cfitsio functions.

Definitions of Variables

External:
See bkgest_defs.h and bkgest_exec.c


Internal:
I_fits_return_status = Resturn value of fits function.
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


void bkgest_output_filenkeywords(fitsfile      *ffp_FITS,
                                 BKE_Filenames *BKEP_Fnames,
                                 BKE_FITSinfo  *BKEP_FITS,
                                 BKE_Status    *BKEP_Stat)
{
    int I_fits_return_status=0;
    char CP_Keyname[FLEN_KEYWORD];
    char CP_Keyvalue[MAX_FILENAME_LENGTH];
    char RootFilename[MAX_FILENAME_LENGTH],CompleteFilename[MAX_FILENAME_LENGTH];

    if ((BKEP_Stat->I_status & RIGHT_MASK) >= 64) return;

    sprintf(CP_Keyname, "%s", "BKENLFIL");

    strcpy(CompleteFilename,BKEP_Fnames->CP_Filename_Namelist);
    getRootFilename(RootFilename,CompleteFilename);
    strcpy(CP_Keyvalue,RootFilename);
    fits_update_key(ffp_FITS,
                    TSTRING,
                    CP_Keyname,
                    CP_Keyvalue,
                    "",
                    &I_fits_return_status);

    sprintf(CP_Keyname, "%s", "BKEIFIL ");
    strcpy(CompleteFilename,BKEP_Fnames->CP_Filename_FITS_Image1);
    getRootFilename(RootFilename,CompleteFilename);
    strcpy(CP_Keyvalue,RootFilename);
    fits_update_key(ffp_FITS,
                    TSTRING,
                    CP_Keyname,
                    CP_Keyvalue,
                    "",
                    &I_fits_return_status);

    if (strcmp(BKEP_Fnames->CP_Filename_FITS_Mask, "") != 0) {
            sprintf(CP_Keyname, "%s", "BKEIMASK");
            strcpy(CompleteFilename, BKEP_Fnames->CP_Filename_FITS_Mask);
            getRootFilename(RootFilename,CompleteFilename);
            strcpy(CP_Keyvalue,RootFilename);
            fits_update_key(ffp_FITS,
                            TSTRING,
                            CP_Keyname,
                            CP_Keyvalue,
                            "Mask image file",
                            &I_fits_return_status);
    }



    if (BKEP_FITS->I_Fits_Type == 1 || BKEP_FITS->I_Fits_Type == 3) {

       sprintf(CP_Keyname, "%s", "BKEOFIL1");
       strcpy(CompleteFilename,BKEP_Fnames->CP_Filename_FITS_Out);
       getRootFilename(RootFilename,CompleteFilename);
       strcpy(CP_Keyvalue,RootFilename);
       fits_update_key(ffp_FITS,
                       TSTRING,
                       CP_Keyname,
                       CP_Keyvalue,
                       "",
                       &I_fits_return_status);
    }

    if (BKEP_FITS->I_Fits_Type == 2 || BKEP_FITS->I_Fits_Type == 3) {

       sprintf(CP_Keyname, "%s", "BKEOFIL2");
       strcpy(CompleteFilename,BKEP_Fnames->CP_Filename_FITS_Out2);
       getRootFilename(RootFilename,CompleteFilename);
       strcpy(CP_Keyvalue,RootFilename);
       fits_update_key(ffp_FITS,
                       TSTRING,
                       CP_Keyname,
                       CP_Keyvalue,
                       "",
                       &I_fits_return_status);
    }


    if (BKEP_FITS->I_Fits_Type == 1 || BKEP_FITS->I_Fits_Type == 3) {

       sprintf(CP_Keyname, "%s", "BKEOFIL3");
       strcpy(CompleteFilename,BKEP_Fnames->CP_Filename_FITS_Out3);
       getRootFilename(RootFilename,CompleteFilename);
       strcpy(CP_Keyvalue,RootFilename);
       fits_update_key(ffp_FITS,
                       TSTRING,
                       CP_Keyname,
                       CP_Keyvalue,
                       "",
                       &I_fits_return_status);
    }

    if (BKEP_FITS->I_Operation == 2 || BKEP_FITS->I_Operation == 3) {

       sprintf(CP_Keyname, "%s", "BKETFIL ");
       strcpy(CompleteFilename,BKEP_Fnames->CP_Filename_Data_Out);
       getRootFilename(RootFilename,CompleteFilename);
       strcpy(CP_Keyvalue,RootFilename);
       fits_update_key(ffp_FITS,
                       TSTRING,
                       CP_Keyname,
                       CP_Keyvalue,
                       "",
                       &I_fits_return_status);
    }

    sprintf(CP_Keyname, "%s", "BKELGFIL");
    strcpy(CompleteFilename,BKEP_Fnames->CP_Filename_Log);
    getRootFilename(RootFilename,CompleteFilename);
    strcpy(CP_Keyvalue,RootFilename);
    fits_update_key(ffp_FITS,
                    TSTRING,
                    CP_Keyname,
                    CP_Keyvalue,
                    "",
                    &I_fits_return_status);


    fits_flush_file(ffp_FITS,
      &I_fits_return_status);

    if (BKEP_Stat->I_Verbose)
       printf("BKE_output_filenkeywords:  I_fits_return_status = %d\n",
          I_fits_return_status);

    if (I_fits_return_status)
       BKEP_Stat->I_status = FILEN_KEYWORDS|FITS_MSG_MASK|I_fits_return_status;

}


/* Strip pathname off filename. */

void getRootFilename( char *RootFilename, char *CompleteFilename )

{

   int i,n,si,sf;

   n = strlen(CompleteFilename);

   si = 0;
   sf = n;
   for (i = n - 1; i >= 0; i--) {
      if (CompleteFilename[i] == '/' || CompleteFilename[i] == '!')
      {
         si = i + 1;
         break;
      }
      if (CompleteFilename[i] == ' ') { sf = i; }
   }

   for (i = 0; i < sf - si; i++) {
      RootFilename[i] = CompleteFilename[i + si];
   }

   RootFilename[sf - si] = '\0';

}

