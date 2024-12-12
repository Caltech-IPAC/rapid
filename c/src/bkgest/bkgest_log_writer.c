/***********************************************
bkgest_log_writer.c

Purpose

Decode error messages and report.

Overview
Append Error Messages to log file.

Definitions of Variables

External:
See bkgest_defs.h and bkgest_exec.c


Internal:
I_Time = Running time of program.
I_bkgest_message = Code of error that occurred.
I_bkgest_root = Code of function in which error occurred.
I_cfitsio_message = Code of cfitsio error returned.

CP_Message = String interpretation of error code part of status word.
CP_Function = String interpretation of function part of status word.

fp_Logfile = File pointer of logfile.

***********************************************/

#include <time.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "bkgest.h"
#include "bkgest_defs.h"
#include "bkgest_errcodes.h"


void bkgest_log_writer(BKE_Constants *BKEP_Const,
                       BKE_FITSinfo  *BKEP_FITS,
                       BKE_Status    *BKEP_Stat,
                       BKE_Filenames *BKEP_Fnames,
                       BKE_Pathnames *BKEP_Paths)
{
   int I_Time;
   int I_bkgest_message;
   int I_bkgest_root;
   int I_cfitsio_message=0;

   char CP_Message[I_MESSAGE_LENGTH];
   char CP_Function[I_FCNNAME_LENGTH];

   FILE *fp_Logfile;

   time_t  currenttime;
   char    *datetime;

   printf("bkgest_log_writer: Log File = %s\n",
            BKEP_Fnames->CP_Filename_Log);

   printf("bkgest_log_writer: Input Image File = %s\n",
            BKEP_Fnames->CP_Filename_FITS_Image1);


   if(strcmp(BKEP_Fnames->CP_Filename_Log, "stdout") == 0) {
     fprintf(stdout, "bkgest_log_writer: Printing Log output to stdout\n");
     fp_Logfile = stdout;
   }
   else
   {
     fp_Logfile = fopen(BKEP_Fnames->CP_Filename_Log, "a");

     if(fp_Logfile==NULL) {
       fprintf(stdout, "bkgest_log_writer: Could not open Log File, using stdout\n");
       fp_Logfile = stdout;
     }
   }


   fprintf(fp_Logfile,"\nProgram %s, Version %2.1f\n",
      PROGRAM, BKEVERSN);

   if (strcmp(BKEP_Fnames->CP_Filename_Namelist,"")) {
      fprintf(fp_Logfile, "Namelist File  = %s\n",
      BKEP_Fnames->CP_Filename_Namelist);
   }
   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Image1,"")) {
      fprintf(fp_Logfile, "Input File = %s\n",
      BKEP_Fnames->CP_Filename_FITS_Image1);
   }
   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Mask,"")) {
      fprintf(fp_Logfile, "Input Mask = %s\n",
      BKEP_Fnames->CP_Filename_FITS_Mask);
   }
   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Out,"")) {
      fprintf(fp_Logfile, "Output File 1 (local clippedmean) = %s\n",
      &(BKEP_Fnames->CP_Filename_FITS_Out[1]));
   }
   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Out2,"")) {
      fprintf(fp_Logfile, "Output File 2 (input - local clippedmean) = %s\n",
      &(BKEP_Fnames->CP_Filename_FITS_Out2[1]));
   }
   if (strcmp(BKEP_Fnames->CP_Filename_FITS_Out3,"")) {
      fprintf(fp_Logfile, "Output File 3 (local sky scale) = %s\n",
      &(BKEP_Fnames->CP_Filename_FITS_Out3[1]));
   }
   if (strcmp(BKEP_Fnames->CP_Filename_Data_Out,"")) {
      fprintf(fp_Logfile, "Output File 3 = %s\n",
      &(BKEP_Fnames->CP_Filename_Data_Out[1]));
   }

   fprintf(fp_Logfile, "Mask mask= %d\n",
      BKEP_Const->I_MaskMask);
   fprintf(fp_Logfile, "Calculation Type = %d\n",
      BKEP_FITS->I_Operation);
   fprintf(fp_Logfile, "Local-ClippedMean Input Window (pixels) = %d\n",
      BKEP_Const->D_Window);
   fprintf(fp_Logfile, "Local-ClippedMean Grid Spacing (pixels) = %d\n",
      BKEP_Const->D_GridSpacing);
   fprintf(fp_Logfile, "Percentage of Bad Pixels Tolerated for Local ClippedMean = %d\n",
      BKEP_Const->D_NBadPixLocTol);
   fprintf(fp_Logfile, "Percentage of Bad Pixels Tolerated for Global ClippedMean = %d\n",
      BKEP_Const->D_NBadPixGloTol);
   fprintf(fp_Logfile, "Image Pothole Value = %e\n",
      BKEP_Const->D_Pothole);
   fprintf(fp_Logfile, "Data-Plane Flag = %d\n",
      BKEP_FITS->I_Data_Plane);
   fprintf(fp_Logfile, "Output Image Type = %d\n",
      BKEP_FITS->I_Fits_Type);
   fprintf(fp_Logfile, "Ancillary Data-File Path = %s\n",
      BKEP_Paths->CP_Ancillary_File_Path);
   fprintf(fp_Logfile, "Verbose flag = %d\n",
      BKEP_Stat->I_Verbose);
   fprintf(fp_Logfile, "Super-verbose flag = %d\n",
      BKEP_Stat->I_SuperVerbose);
   fprintf(fp_Logfile, "Debug flag = %d\n",
      BKEP_Stat->I_Debug);

   if (BKEP_FITS->I_Operation==1)
      fprintf(fp_Logfile, "%s\n", "Set-up to do local-clippedmean image calculation.");
   else if (BKEP_FITS->I_Operation==2)
      fprintf(fp_Logfile, "%s\n", "Set-up to do global clippedmean calculation.");
   else if(BKEP_FITS->I_Operation==3)
      fprintf(fp_Logfile, "%s\n",
         "Set-up to do local and global clippedmean calculations.");

   I_bkgest_message = RIGHT_MASK&(BKEP_Stat->I_status);
   I_bkgest_root = (LEFT_MASK&(BKEP_Stat->I_status))>>16;

   if(I_bkgest_message&FITS_MSG_MASK) {
     I_bkgest_message -= FITS_MSG_MASK;
     ffrprt(fp_Logfile, I_bkgest_message);
   }

   if (BKEP_Stat->I_Verbose)
      printf("Decoding messages...\n");


   decode_message(I_bkgest_message, I_bkgest_root,
                  CP_Message, CP_Function, BKEP_Paths, BKEP_Stat);


   I_bkgest_message = RIGHT_MASK&(BKEP_Stat->I_status);
   I_bkgest_root = (LEFT_MASK&(BKEP_Stat->I_status))>>16;
   if(I_bkgest_message&FITS_MSG_MASK)
      I_bkgest_message -= FITS_MSG_MASK;

   if (I_bkgest_message != 0) {
      fprintf(fp_Logfile,
         "bkgest Status Message      0x%04x\n", I_bkgest_message);
      fprintf(fp_Logfile,
         "%s from Function 0x%04x: %s\n",
         CP_Message,  I_bkgest_root, CP_Function);
   } else {
      fprintf(fp_Logfile,
         "bkgest Status Message      0x%04x\n", I_bkgest_message);
      fprintf(fp_Logfile,
         "%s from Function 0x%04x: %s\n",
         "Normal exit",  I_bkgest_root, CP_Function);
   }

   /* According to John Fowler (8/3/99), there is no need to write anything
      to stderr.

   if (I_bkgest_message != 0) {
      fprintf(stderr,
        ")-: bkgest Status Message      0x%04x: %s from Function 0x%04x: %s.\n",
        I_bkgest_message, CP_Message,  I_bkgest_root, CP_Function);
   }

   */

   if(BKEP_Stat->I_NaNFlag)
   fprintf(fp_Logfile,
      "bkgest Probs Warning: NaN's were produced in the results.\n");
   fprintf(fp_Logfile,
      "A total of %8d   NaN's were produced in the results.\n",
   BKEP_Stat->I_NaNCount);

   I_Time = clock();
   fprintf(fp_Logfile, "Processing time: %f seconds\n", (float)I_Time/CLOCKS_PER_SEC);

   time(&currenttime);
   datetime = ctime(&currenttime);
   fprintf(fp_Logfile,"Current date/time: %s",datetime);
   fprintf(fp_Logfile,"Program %s, version %2.1f, terminated.\n",
      PROGRAM, BKEVERSN);


   if(fp_Logfile != stdout) fclose(fp_Logfile);


   if(BKEP_Stat->I_Verbose)
      printf("bkgest_log_writer: Log Writer finished.\n");


}



void decode_message( int         I_message,
                int         I_root,
  char     *CP_Message,
  char     *CP_Function,
  BKE_Pathnames *BKEP_Paths,
  BKE_Status *BKEP_Stat)
{

char codefile[2*MAX_FILENAME_LENGTH],line[80],mess[80],numstr[80],*rem;
int ifun=0, imess=0;
unsigned long num, nums;

FILE *GETCODEBOOK, *fopen();

   sprintf(CP_Function, "%s", "Unspecified function.");
   sprintf(CP_Message, "%s", "Unknown error");
   strcpy(codefile, BKEP_Paths->CP_Ancillary_File_Path);
   strcat(codefile, "/bkgest_errcodes.h");

   if(BKEP_Stat->I_Verbose)
      printf("Message-code file = %s\n",codefile);

   if ((GETCODEBOOK = fopen(codefile, "r")) == NULL) {

      printf("*** BKE_log_writer: Could not open bkgest_errcodes.h\n");

      BKEP_Stat->I_status = LOG_WRITER|LOG_WRITER;
      sprintf(CP_Message,"%s","ERRCODE_FILE_NOT_FOUND");
      sprintf(CP_Function,"%s","LOG_WRITER");

      return;

   } else {

     while (!feof(GETCODEBOOK)) {
        fscanf(GETCODEBOOK, "%s",line);
 if (line[0]== '#') {
           fscanf(GETCODEBOOK, "%s",mess);
           fscanf(GETCODEBOOK, "%s",numstr);
           num = strtoul(numstr,&rem,16);
           nums = (LEFT_MASK & num) >> 16;

           if ((nums == I_root) && (ifun == 0)) {
      /* printf("%s %X %s\n",mess,nums,rem); */
              sprintf(CP_Function, "%s", mess);
              ifun = 1;
           }

           if ((num == I_message) && (ifun == 1)) {
      /* printf("%s %X %s\n",mess,num,rem); */
              sprintf(CP_Message, "%s", mess);
       imess = 1;
              break;
           }
        }
     }

     fclose(GETCODEBOOK);

   }

   if ((ifun != 1) || (imess != 1)) {
      printf("*** BKE_log_writer: Error in decoding error message.");
   }

   return;

}



