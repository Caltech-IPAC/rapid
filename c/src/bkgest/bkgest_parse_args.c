/***********************************************
bkgest_parse_args.c

Purpose
Parse the command line to get input and output
file names constants, and processing flags.

Overview
Loop over the arguments, invoking case statement.

Definitions of Variables

External:
See bkgest_defs.h and bkgest_exec.c


***********************************************/

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "bkgest_errcodes.h"
#include "bkgest.h"
#include "bkgest_defs.h"

void bkgest_parse_args(int             argc,
                       char            **argv,
                       BKE_Constants   *BKEP_Const,
                       BKE_Filenames   *BKEP_Fnames,
                       BKE_Pathnames   *BKEP_Paths,
                       BKE_FITSinfo    *BKEP_FITS,
                       BKE_Status      *BKEP_Stat)
{
    int i=1;
    int I_gotallinputs;
    char CP_Value[STRING_BUFFER_SIZE];

    /* Run regardless of I_bkgest_status so that
       at least log file name will be known */


    while( i < argc )
    {
        if( argv[i][0] == '-' )
        {
           switch( argv[i][1] ) {
              case 'n':      /* -n <input_namelist_fname>  */
                if( ++i >= argc )
                    printf("bkgest_parse_args: -n <input_namelist_fname> missing argument\n" );
                else
                  if(strcmp(BKEP_Fnames->CP_Filename_Namelist,"") == 0) /* Filename can be stored */
                  sscanf(argv[i], "%s", BKEP_Fnames->CP_Filename_Namelist);
                  break;
              case 'i':      /* -i <input_image_fname>  */
                if( ++i >= argc )
                  printf("bkgest_parse_args: -i <input_image_fname> missing argument\n" );
                else
                  if(strcmp(BKEP_Fnames->CP_Filename_FITS_Image1,"") == 0) /* Filename can be stored */
                  sscanf(argv[i], "%s", BKEP_Fnames->CP_Filename_FITS_Image1);
                  break;
              case 'm':      /* -m <input_mask_fname>  */
                if ( ++i >= argc )
                  printf("bkgest_parse_args: -m <input_mask_fname> missing argument\n" );
                else
                  if (argv[i-1][2] == '\0')
                    if (strcmp(BKEP_Fnames->CP_Filename_FITS_Mask,"") == 0)
                      sscanf(argv[i], "%s", BKEP_Fnames->CP_Filename_FITS_Mask);
                      break;
              case 'o':      /* -o <output_fname>  */
                if( ++i >= argc )
                  printf("bkgest_parse_args: -o <fname> missing argument\n" );
                else
                  if(argv[i-1][2] == '1')
                    if(strcmp(BKEP_Fnames->CP_Filename_FITS_Out,"") == 0) {
                      sscanf("!", "%c", BKEP_Fnames->CP_Filename_FITS_Out);  /* clobber */
                      sscanf(argv[i], "%s", &(BKEP_Fnames->CP_Filename_FITS_Out[1]));
                    }
                  if(argv[i-1][2] == '2')
                    if(strcmp(BKEP_Fnames->CP_Filename_FITS_Out2,"") == 0) {
                      sscanf("!", "%c", BKEP_Fnames->CP_Filename_FITS_Out2);  /* clobber */
                      sscanf(argv[i], "%s", &(BKEP_Fnames->CP_Filename_FITS_Out2[1]));
                    }
                  if(argv[i-1][2] == '3')
                    if(strcmp(BKEP_Fnames->CP_Filename_FITS_Out3,"") == 0) {
                      sscanf("!", "%c", BKEP_Fnames->CP_Filename_FITS_Out3);  /* clobber */
                      sscanf(argv[i], "%s", &(BKEP_Fnames->CP_Filename_FITS_Out3[1]));
                    }
                  if(argv[i-1][2] == 't')
                    if(strcmp(BKEP_Fnames->CP_Filename_Data_Out,"") == 0) {
                      sscanf("!", "%c", BKEP_Fnames->CP_Filename_Data_Out);  /* clobber */
                      sscanf(argv[i], "%s", &(BKEP_Fnames->CP_Filename_Data_Out[1]));
                    }
                break;
              case 'l':      /* -l <log_fname>  */
                if( ++i >= argc )
                  printf("bkgest_parse_args: -l <fname> missing argument\n" );
                else
                  if(strcmp(BKEP_Fnames->CP_Filename_Log,"stdout") == 0)
                    sscanf(argv[i], "%s", BKEP_Fnames->CP_Filename_Log);
                  break;
              case 'c':      /* -c <calculation_type>  */
                if( ++i >= argc )
                  printf("bkgest_parse_args: -c <calculation_type> missing argument\n" );
                else
                  sscanf(argv[i], "%d", &(BKEP_FITS->I_Operation));
                break;
              case 'w':      /* -w <window>  */
                if( ++i >= argc )
                  printf("bkgest_parse_args: -w <window> missing argument\n" );
                else
                  sscanf(argv[i], "%d", &(BKEP_Const->D_Window));
                break;
              case 'g':      /* -g <grid_spacing>  */
                if( ++i >= argc )
                  printf("bkgest_parse_args: -g <grid_spacing> missing argument\n" );
                else
                  sscanf(argv[i], "%d", &(BKEP_Const->D_GridSpacing));
                break;
              case 'b':      /* -b <number_bad_pixels_tolerated>  */
                if( ++i >= argc )
                  printf("bkgest_parse_args: -b <mask_mask> missing argument\n" );
                else
                {
                  if (argv[i-1][2] == '\0')
                    sscanf(argv[i], "%d", &(BKEP_Const->I_MaskMask));
                  if(argv[i-1][2] == 'l')
                    sscanf(argv[i], "%d", &(BKEP_Const->D_NBadPixLocTol));
                  if(argv[i-1][2] == 'g')
                    sscanf(argv[i], "%d", &(BKEP_Const->D_NBadPixGloTol));
                }
                break;
              case 'p':      /* -p <data_plane>  */
                if( ++i >= argc )
                  printf("bkgest_parse_args: -p <data_plane> missing argument\n" );
                else
                  sscanf(argv[i], "%d", &(BKEP_FITS->I_Data_Plane));
                break;
              case 'e':      /* -e <pothole>  */
                if ( ++i >= argc )
                  printf("bkgest_parse_args: -e <pothole> missing argument\n" );
                else
                  if (argv[i-1][2] == '\0') {
                    sscanf(argv[i], "%s", CP_Value);
                    BKEP_Const->D_Pothole = atof(CP_Value);
                  }
                break;
              case 'f':      /* -f <output_image_type>  */
                if( ++i >= argc )
                  printf("bkgest_parse_args: -f <output_image_type> missing argument\n" );
                else
                  sscanf(argv[i], "%d", &(BKEP_FITS->I_Fits_Type));
                break;
              case 'a':      /* -a <input_ancillary_file_pathname>  */
                if( ++i >= argc )
                  printf("bkgest_parse_args: -a <input_ancillary_file_pathname> missing argument\n" );
                else
                  if(strcmp(BKEP_Paths->CP_Ancillary_File_Path,".") == 0) /* Filename can be stored */
                    sscanf(argv[i], "%s", BKEP_Paths->CP_Ancillary_File_Path);
                break;
              case 'd':      /* -debug   */
                BKEP_Stat->I_Debug = 1 - BKEP_Stat->I_Debug;
                break;
              case 'v':      /* -debug   */
                if(argv[i][2] != 'v')
                  BKEP_Stat->I_Verbose = 1 - BKEP_Stat->I_Verbose;
                if(argv[i][2] == 'v')
                  BKEP_Stat->I_SuperVerbose = 1 - BKEP_Stat->I_SuperVerbose;
                break;
              default:
                printf("bkgest_parse_args: unknown argument\n");
           }
        }
        else
        {
              printf("bkgest_parse_args: Command line syntax error:\n");
              printf("   Previous argument = %s\n",argv[i - 1]);
              printf("   Current argument = %s\n",argv[i]);
        }
        i++;
    }


    /* Print to stdout the program name and version number */

    if(BKEP_Stat->I_Verbose)
      printf("bkgest_parse_args: Program %s, Version %2.1f\n",
      PROGRAM, BKEVERSN);


   /* Check code inputs. */

   I_gotallinputs = 0;

   if(BKEP_Stat->I_Verbose) {
      if (strcmp(BKEP_Fnames->CP_Filename_Namelist,"") != 0) {
         printf("bkgest_parse_args: Namelist File = %s\n",
            BKEP_Fnames->CP_Filename_Namelist);
      } else {
         printf("bkgest_parse_args: Information only: %s\n",
            "No namelist file specified.");
      }
   }

   if(BKEP_Stat->I_Verbose) {
      if (strcmp(BKEP_Paths->CP_Ancillary_File_Path,"") != 0) {
         printf("bkgest_parse_args: Ancilllary File Pathname = %s\n",
            BKEP_Paths->CP_Ancillary_File_Path);
      } else {
         printf("bkgest_parse_args: Information only: %s %s\n",
            "No ancillary file pathname specified;",
            "defaulting to current directory.");
      }
   }

   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Image1,"") != 0) {
      if(BKEP_Stat->I_Verbose)
         printf("bkgest_parse_args: Input File 1 = %s\n",
         BKEP_Fnames->CP_Filename_FITS_Image1);
   } else {
      printf(
         "bkgest_parse_args: Missing input FITS filename %s\n",
         "(-i <fname>).");
      I_gotallinputs = 1;
   }

   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Mask,"") != 0) {
       printf("bkgest_parse_args: Input Mask = %s\n",
           BKEP_Fnames->CP_Filename_FITS_Mask);
   }

   if (!(BKEP_FITS->I_Operation < 1 ||BKEP_FITS->I_Operation > 3)) {
      if(BKEP_Stat->I_Verbose)
         printf("bkgest_parse_args: %s = %d\n",
            "ClippedMean calculation-type flag",BKEP_FITS->I_Operation);
   } else {
      printf("bkgest_parse_args: %s\n",
         "ClippedMean calculation-type flag out of range (-c <1, 2, or 3>).");
         I_gotallinputs = 2;
   }

   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Out,"")  != 0) {
      if(BKEP_Stat->I_Verbose)
         printf("bkgest_parse_args: Output File 1 = %s\n",
         &BKEP_Fnames->CP_Filename_FITS_Out[1]);
   } else {
      if (BKEP_FITS->I_Fits_Type == 1 ||BKEP_FITS->I_Fits_Type == 3) {
         printf(
            "bkgest_parse_args: Missing output FITS filename %s\n",
            "(-o1 <fname>).");
         I_gotallinputs = 1;
      }
   }

   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Out2,"") != 0) {
      if(BKEP_Stat->I_Verbose)
         printf("bkgest_parse_args: Output File 2 = %s\n",
         &BKEP_Fnames->CP_Filename_FITS_Out2[1]);
   } else {
      if (BKEP_FITS->I_Fits_Type == 2 || BKEP_FITS->I_Fits_Type == 3) {
         printf(
            "bkgest_parse_args: Missing output FITS filename %s\n",
            "(-o2 <fname>).");
         I_gotallinputs = 1;
      }
   }

   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Out3,"") != 0) {
      if(BKEP_Stat->I_Verbose)
         printf("bkgest_parse_args: Output File 3 = %s\n",
         &BKEP_Fnames->CP_Filename_FITS_Out3[1]);
   } else {
      if (BKEP_FITS->I_Fits_Type == 1 ||BKEP_FITS->I_Fits_Type == 3) {
         printf(
            "bkgest_parse_args: Missing output FITS filename %s\n",
            "(-o3 <fname>).");
         I_gotallinputs = 1;
      }
   }

   if (strcmp(BKEP_Fnames->CP_Filename_Data_Out,"")  != 0) {
      if(BKEP_Stat->I_Verbose)
         printf("bkgest_parse_args: Output Table File = %s\n",
         &BKEP_Fnames->CP_Filename_Data_Out[1]);
   } else {
      if (BKEP_FITS->I_Operation == 2 || BKEP_FITS->I_Operation == 3) {
         printf(
            "bkgest_parse_args: Missing output table-data filename %s\n",
            "(-ot <fname>).");
         I_gotallinputs = 1;
      }
   }

   if (BKEP_FITS->I_Operation == 1 || BKEP_FITS->I_Operation == 3) {
      if (BKEP_Const->D_Window > 1 && BKEP_Const->D_Window % 2 == 1) {
         if(BKEP_Stat->I_Verbose)
            printf("bkgest_parse_args: %s = %d\n",
               "Local clippedmean input window (pixels)",BKEP_Const->D_Window);
      } else {
         printf("bkgest_parse_args: %s\n",
            "Local clippedmean input window (pixels) not > 1 or not odd (-w <value>).");
         I_gotallinputs = 2;
      }
      if (BKEP_Const->D_GridSpacing > 1) {
         if(BKEP_Stat->I_Verbose)
            printf("bkgest_parse_args: %s = %d\n",
               "Local clippedmean grid spacing (pixels)",BKEP_Const->D_GridSpacing);
      } else {
         printf("bkgest_parse_args: %s\n",
            "Local clippedmean grid spacing (pixels) not > 1 (-g <value>).");
         I_gotallinputs = 2;
      }
   }

   if ((BKEP_Const->D_NBadPixLocTol >= 0) && (BKEP_Const->D_NBadPixLocTol <= 99)) {
      if(BKEP_Stat->I_Verbose)
         printf("bkgest_parse_args: %s = %d\n",
            "Percentage of bad pixels tolerated for local clippedmean",
            BKEP_Const->D_NBadPixLocTol);
   } else {
      printf("bkgest_parse_args: %s\n",
         "Percentage of bad pixels tolerated for local clippedmean must be an integer in the inclusive range [0, 99] (-bl <value>).");
      I_gotallinputs = 2;
   }

   if ((BKEP_Const->D_NBadPixGloTol >= 0) && (BKEP_Const->D_NBadPixGloTol <= 99)) {
      if(BKEP_Stat->I_Verbose)
         printf("bkgest_parse_args: %s = %d\n",
            "Percentage of bad pixels tolerated for global clippedmean",
            BKEP_Const->D_NBadPixGloTol);
   } else {
      printf("bkgest_parse_args: %s\n",
         "Percentage of bad pixels tolerated for global clippedmean must be an integer in the inclusive range [0, 99] (-bg <value>).");
      I_gotallinputs = 2;
   }

   if (!(BKEP_FITS->I_Data_Plane < 1 || BKEP_FITS->I_Data_Plane > 3)) {
      if(BKEP_Stat->I_Verbose)
         printf("bkgest_parse_args: %s = %d\n",
            "Data-plane calculation flag",BKEP_FITS->I_Data_Plane);
   } else {
      printf("bkgest_parse_args: %s\n",
         "Data-plane calculation flag out of range (-p <1, 2, or 3>).");
      I_gotallinputs = 2;
   }

   if (!(BKEP_FITS->I_Fits_Type < 1 || BKEP_FITS->I_Fits_Type > 4)) {
      if(BKEP_Stat->I_Verbose)
         printf("bkgest_parse_args: %s = %d\n",
            "FITS ftype flag",BKEP_FITS->I_Fits_Type);
   } else {
      printf("bkgest_parse_args: %s\n",
         "FITS ftype flag out of range (-f <1, 2, 3, or 4>).");
      I_gotallinputs = 2;
   }

   if (!(BKEP_Const->I_MaskMask < 0 ||
         BKEP_Const->I_MaskMask > 65535)) {
       printf("bkgest_parse_args: %s = %d\n",
              "Mask mask", BKEP_Const->I_MaskMask);
   } else {
      printf("bkgest_parse_args: %s\n",
        "Mask mask out of range (required to be between 0 and 65535, inclusive>).");
      I_gotallinputs = 2;
   }

   if(BKEP_Stat->I_Verbose) {

      printf("Image Pothole Value = %e\n", BKEP_Const->D_Pothole);

      printf("bkgest_parse_args: Log File = %s\n",
         BKEP_Fnames->CP_Filename_Log);

      printf("bkgest_parse_args: I_Debug = %d\n",
         BKEP_Stat->I_Debug);

      printf("bkgest_parse_args: I_Verbose = %d\n",
         BKEP_Stat->I_Verbose);

      printf("bkgest_parse_args: I_SuperVerbose = %d\n",
         BKEP_Stat->I_SuperVerbose);
   }


   if (BKEP_Stat->I_status) return;

   if (I_gotallinputs == 1) {
      BKEP_Stat->I_status = PARSE_ARGS|MISSING_INPUT;
      return;
   }
   if (I_gotallinputs == 2) {
      BKEP_Stat->I_status = PARSE_ARGS|INPUTS_OUT_OF_RANGE;
      return;
   }



}





