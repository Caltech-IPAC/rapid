/***********************************************
bkgest_parse_namelist.c

Purpose
Read and parse a parameter file to get input and output file
names constants, and processing flags. "Operation" entry tells
which binary operationto perform.

Overview
Use string functions to search for separator characters,
then a series of conditionals to find what variables are
being defined.

Definitions of Variables

Input:

Expects a either a namelist file or command-line inputs.

***********************************************/

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <fcntl.h>
#include "bkgest.h"
#include "bkgest_defs.h"
#include "bkgest_errcodes.h"

#define FILE_BUFFER_SIZE 8192

void bkgest_parse_namelist(int            argc,
                           char           **argv,
                           BKE_Constants  *BKEP_Const,
                           BKE_Filenames  *BKEP_Fnames,
                           BKE_Pathnames  *BKEP_Paths,
                           BKE_FITSinfo   *BKEP_FITS,
                           BKE_Status     *BKEP_Stat)
{


  FILE *fp_namelist;
  /*char CP_buf[FILE_BUFFER_SIZE];*/
  char *CP_buf;
  char CP_NextWord[STRING_BUFFER_SIZE];
  char CP_Equals[STRING_BUFFER_SIZE];
  char CP_Value[STRING_BUFFER_SIZE];
  char CP_Comment[STRING_BUFFER_SIZE];
  char *CP_Pointer, *CP_Comma;

  int i=1, I_Got_Namelist=0, I_SectionsFound=0;
  int I_fildes_nl=0, I_str_length=0;
  int I_past_end=0, I_found=0;

  struct stat  STAT_nlbuf, *STATP_nlbuf;

  if (argc==1) {

    fprintf(stdout, "\nProgram bkgest by Russ Laher (laher@ipac.caltech.edu) 2024 Dec 16\n\n");

    fprintf(stdout, "The bkgest program computes a background map from an input image\n");
    fprintf(stdout, "by segementing the pixel space for the given grid spacing, computing\n");
    fprintf(stdout, "the sigma=2.5 clipped mean over a subimage centered on each grid point\n");
    fprintf(stdout, "of the given window size, and then computing between grid points the\n");
    fprintf(stdout, "background for each pixel via bilinear interpolation.  Grid points that\n");
    fprintf(stdout, "result in NaN are replaced with the global clipped mean.\n\n");

    fprintf(stdout, "Usage: bkgest\n");
    fprintf(stdout, "       -n <input_namelist_fname> (Optional)\n");
    fprintf(stdout, "       -i <input_image_fname> (Namelist or required)\n");
    fprintf(stdout, "       -m <input_mask_fname> (Optional)\n");
    fprintf(stdout, "       -c <clippedmean_calc_type> %s\n",
        "(1=Local, 2=Global, 3=Both, where global is grid filler if local not available due to bad pixels)");
    fprintf(stdout, "       -f <output_image_type>  %s\n",
       "(1=ClippedMean, 2=ClippedMean-Input, 3=Both, 4=None; default is 1)");
    fprintf(stdout, "       -o1 <output_clippedmean_fits_fname> %s\n",
       "(Required if -c 1 or -c 3 and -f 1 or -f 3 are specified, and not applicable if -c 2 is specified)");
    fprintf(stdout, "       -o2 <output_input-clippedmean_fits_fname> %s\n",
       "(Output image depends on -c option; required for -f 2 or -f 3, but not applicable for -f 1)");
    fprintf(stdout, "       -o3 <output_sky_scale_fits_fname> %s\n",
       "(Required if -c 1 or -c 3 and -f 1 or -f 3 are specified, and not applicable if -c 2 is specified)");
    fprintf(stdout, "       -ot <output_global_clippedmean_data_fname> %s\n",
       "(Required if -c 2 or -c 3 is specified)");
    fprintf(stdout, "       -l <log_fname> (Default is stdout)\n");
    fprintf(stdout, "       -w <local_clippedmean_input_window> %s\n",
       "(Depends on -c option; pixels on a side; default is 7 pixels)");
    fprintf(stdout, "       -g <local_clippedmean_grid_spacing> %s\n",
       "(Depends on -c option; computational grid spacing; default is 16 pixels)");
    fprintf(stdout, "       -b <fatal_bit_mask> (Optional, default is zero)\n");
    fprintf(stdout, "       -bl <integer_percent_of_local_clippedmean_number_bad_pixels_tolerated> %s\n",
       "(Optional, must be an integer 99% or less, default is 50%)");
    fprintf(stdout, "       -bg <integer_percent_of_global_clippedmean_number_bad_pixels_tolerated> %s\n",
       "(Optional, must be an integer 99% or less, default is 50%)");
    fprintf(stdout, "       -p <data_plane_to_process> %s\n",
       "(1=All, 2=First, 3=Last; default is 1)");
    fprintf(stdout, "       -e <pothole> (Optional image value at and below which to ignore) \n");
    fprintf(stdout, "       -a <ancillary_file_path> (Optional)\n");
    fprintf(stdout, "       -d (Prints debug statements) \n");
    fprintf(stdout, "       -v (Verbose output)          \n");
    fprintf(stdout, "       -vv (Super-verbose output)\n\n");

    BKEP_Stat->I_status = NAMELIST|NO_ARGS; return;

  }


  /* Get namelist filename from command line */

  while( i < argc ) {
    if ( argv[i][0] == '-' ) {
              switch( argv[i][1] ) {
              case 'n':      /* -n <input_namelist_fname>  */
                if ( ++i >= argc )
                printf("bkgest_parse_namelist: -n <input_namelist_fname> missing argument\n" );
    else if (strcmp(BKEP_Fnames->CP_Filename_Namelist,"") == 0)                                     /* Filename can be stored */
                I_Got_Namelist = sscanf(argv[i], "%s", BKEP_Fnames->CP_Filename_Namelist);
                break;
              default:
                ;
              }
    }
    i++;
  }

  if (!I_Got_Namelist) {
    printf("bkgest_parse_namelist: Information only: %s\n",
           "Namelist not specified.");
    return;
 }



   /* Open the namelist file to get its length */

   if ((I_fildes_nl = open(BKEP_Fnames->CP_Filename_Namelist,O_RDONLY)) == -1)
   {
      printf("bkgest_parse_namelist: Error opening file %s\n",
      BKEP_Fnames->CP_Filename_Namelist);
      BKEP_Stat->I_status = NAMELIST|FOPEN_FAILED; return;
   }

   /* Find number of characters in namelist file */
   STATP_nlbuf = &STAT_nlbuf;
   fstat(I_fildes_nl, STATP_nlbuf);
   I_str_length = STATP_nlbuf->st_size;


   #if 0
   /* Open the namelist file */
   if (strcmp(BKEP_Fnames->CP_Filename_Namelist, "stdin") == 0) {
   fprintf(stdout, "bkgest_parse_namelist: Taking namelist from stdin\n");
   fp_namelist = stdin;
   }
   else {
     if ( (fp_namelist=fopen(BKEP_Fnames->CP_Filename_Namelist,"r"))==NULL) {
       printf("bkgest_parse_namelist: Error opening file %s",
       BKEP_Fnames->CP_Filename_Namelist);
       BKEP_Stat->I_status = NAMELIST|BKEPEN_FAILED; return;
     }
   }
   #endif

   /* Associate a stream with the fildes generated above */
   fp_namelist=fdopen(I_fildes_nl,"r");

   /* Allocate memory for namelist buffer, */
   CP_buf = (char *) calloc(I_str_length+2, sizeof(char));

   /* Read the namelist data */
   /*memset(CP_buf, 0, I_str_length);*/
   if (fread(CP_buf, sizeof(char), I_str_length, fp_namelist) == 0) {
     printf("bkgest_parse_namelist: Could not read from file %s",
     BKEP_Fnames->CP_Filename_Namelist);
     BKEP_Stat->I_status = NAMELIST|COULD_NOT_READ;
     return;
   }
   CP_buf[I_str_length] = '\0';

  /* Close the stream */
    fclose(fp_namelist);

  /* Look for the bkgest section */
  #if 0
   for(CP_Pointer = CP_buf;
   strncmp("&BKGESTIN", CP_Pointer, strlen("&BKGESTIN")) != 0;
   CP_Pointer = strchr(CP_Pointer+1, '&'))
   I_SectionsFound++;
  #endif
  #if 1
  CP_Pointer = CP_buf;
  while(CP_Pointer <= CP_buf + I_str_length && !I_past_end && !I_found)
       {
       CP_Pointer = strchr(CP_Pointer, '&');
       if (CP_Pointer + strlen("&BKGESTIN") >= CP_buf + I_str_length)
          I_past_end = 1;
       else
          {
          if (strncmp("&BKGESTIN", CP_Pointer, strlen("&BKGESTIN")))
             CP_Pointer += strlen("&");
          else
             I_found = 1;
          }
       }

  if (I_past_end)
     {
     BKEP_Stat->I_status = NAMELIST|COULD_NOT_READ;
     return;
     }
  #endif


          CP_Pointer += strlen("&BKGESTIN");

          /* Match the string values read to the variable definitions and
             write the  values into the appropriate variables. */

   for (CP_Comma = strtok(CP_Pointer, ",");
       CP_Comma != NULL && strchr(CP_Comma, '&') == 0;
       CP_Comma = strtok(NULL, ",")) {

       sscanf(CP_Comma, "%s%s%s", CP_NextWord, CP_Equals, CP_Value);
       if (strcmp(CP_NextWord, "Comment")==0)
       strcpy(CP_Comment, CP_Value);
       if (strcmp(CP_NextWord, "FITS_Image_Filename")==0)
       sscanf(&CP_Value[1], "%[^']", BKEP_Fnames->CP_Filename_FITS_Image1);

       if(strcmp(CP_NextWord, "FITS_Mask_Filename")==0)
       sscanf(&CP_Value[1], "%[^']", BKEP_Fnames->CP_Filename_FITS_Mask);

       if (strcmp(CP_NextWord, "FITS_Out_Filename")==0) {
       sscanf("!", "%c", BKEP_Fnames->CP_Filename_FITS_Out);  /* clobber */
       sscanf(&CP_Value[1], "%[^']", &(BKEP_Fnames->CP_Filename_FITS_Out[1]));
       }
       if (strcmp(CP_NextWord, "FITS_Out2_Filename")==0) {
       sscanf("!", "%c", BKEP_Fnames->CP_Filename_FITS_Out2);  /* clobber */
       sscanf(&CP_Value[1], "%[^']", &(BKEP_Fnames->CP_Filename_FITS_Out2[1]));
       }
       if (strcmp(CP_NextWord, "FITS_Out3_Filename")==0) {
       sscanf("!", "%c", BKEP_Fnames->CP_Filename_FITS_Out3);  /* clobber */
       sscanf(&CP_Value[1], "%[^']", &(BKEP_Fnames->CP_Filename_FITS_Out3[1]));
       }
       if (strcmp(CP_NextWord, "Data_Out_Filename")==0) {
       sscanf("!", "%c", BKEP_Fnames->CP_Filename_Data_Out);  /* clobber */
       sscanf(&CP_Value[1], "%[^']", &(BKEP_Fnames->CP_Filename_Data_Out[1]));
       }
       if (strcmp(CP_NextWord, "Log_Filename")==0)
       sscanf(&CP_Value[1], "%[^']", BKEP_Fnames->CP_Filename_Log);
       if (strcmp(CP_NextWord, "Ancillary_File_Path")==0)
       sscanf(&CP_Value[1], "%[^']", BKEP_Paths->CP_Ancillary_File_Path);
       if (strcmp(CP_NextWord, "Operation")==0)
       BKEP_FITS->I_Operation = atoi(CP_Value);
       if (strcmp(CP_NextWord, "Fits_Type")==0)
       BKEP_FITS->I_Fits_Type = atoi(CP_Value);
       if (strcmp(CP_NextWord, "Data_Plane")==0)
       BKEP_FITS->I_Data_Plane = atoi(CP_Value);
       if (strcmp(CP_NextWord, "Window")==0)
       BKEP_Const->D_Window = atoi(CP_Value);
       if (strcmp(CP_NextWord, "GridSpacing")==0)
       BKEP_Const->D_GridSpacing = atoi(CP_Value);
       if (strcmp(CP_NextWord, "NBadPixLocTol")==0)
       BKEP_Const->D_NBadPixLocTol = atoi(CP_Value);
       if (strcmp(CP_NextWord, "NBadPixGloTol")==0)
       BKEP_Const->D_NBadPixGloTol = atoi(CP_Value);
       if(strcmp(CP_NextWord, "Fatal_Bit_Mask")==0)
       BKEP_Const->I_MaskMask = atoi(CP_Value);
       if(strcmp(CP_NextWord, "Pothole")==0)
         BKEP_Const->D_Pothole = atof(CP_Value);
   }

   free(CP_buf);

   if (BKEP_Stat->I_Verbose)
      printf("bkgest_parse_namelist: Namelist File = \t%s\n",
             BKEP_Fnames->CP_Filename_Namelist);
}





