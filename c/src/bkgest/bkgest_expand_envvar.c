/***********************************************
bkgest_expand_envvar.c

Purpose
If environmental variables were used in the filename specifications from the
namelist or command line, expand them to string values and rewrite the entry in
the filenames structure.

Overview
Use string functions to search for the separator slash character,
search for the '$' character indicating a variable, then getenv to look BKE the
definition of the variable.

Definitions of Variables


External:
See bkgest_defs.h and bkgest_exec.c

Internal:

CP_Copy = Copy of input string.
CP_Path = Copy of input string, modified by strtok.
CP_Expanded = Holds output expanded string.
CP_Ent = Parsed segment of string.
CPP_TokSlash = Array of string positions where "/" characters were.
CPP_TokDollar = Array of string positions where "$" characters were.
CP_Slash = Marks where "/" characters are.
CP_Dollar = Marks where "$" characters are.

***********************************************/

#include <stdio.h>
#include "bkgest_errcodes.h"
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <fcntl.h>
#include "bkgest.h"
#include "bkgest_defs.h"

void bkgest_expand_envvar(BKE_Filenames *BKEP_Fnames,
     BKE_Status *BKEP_Stat)
{

    if (BKEP_Stat->I_status) return;

    if (strcmp(BKEP_Fnames->CP_Filename_Namelist,"")) {
       expand_envvar_string(BKEP_Fnames->CP_Filename_Namelist);
       if(BKEP_Stat->I_Verbose)
          printf("bkgest_expand_envvar: %s\n",
          BKEP_Fnames->CP_Filename_Namelist);
    }
     if (strcmp(BKEP_Fnames->CP_Filename_FITS_Out,"")) {
       expand_envvar_string(BKEP_Fnames->CP_Filename_FITS_Out);
       if(BKEP_Stat->I_Verbose)
          printf("bkgest_expand_envvar: %s\n",
          BKEP_Fnames->CP_Filename_FITS_Out);
    }
    if (strcmp(BKEP_Fnames->CP_Filename_FITS_Out2,"")) {
       expand_envvar_string(BKEP_Fnames->CP_Filename_FITS_Out2);
       if(BKEP_Stat->I_Verbose)
          printf("bkgest_expand_envvar: %s\n",
          BKEP_Fnames->CP_Filename_FITS_Out2);
    }
    if (strcmp(BKEP_Fnames->CP_Filename_FITS_Out3,"")) {
       expand_envvar_string(BKEP_Fnames->CP_Filename_FITS_Out3);
       if(BKEP_Stat->I_Verbose)
          printf("bkgest_expand_envvar: %s\n",
          BKEP_Fnames->CP_Filename_FITS_Out3);
    }
    if (strcmp(BKEP_Fnames->CP_Filename_Data_Out,"")) {
       expand_envvar_string(BKEP_Fnames->CP_Filename_Data_Out);
       if(BKEP_Stat->I_Verbose)
          printf("bkgest_expand_envvar: %s\n",
          BKEP_Fnames->CP_Filename_Data_Out);
    }

    expand_envvar_string(BKEP_Fnames->CP_Filename_FITS_Image1);
    if (strcmp(BKEP_Fnames->CP_Filename_FITS_Mask,""))
       expand_envvar_string(BKEP_Fnames->CP_Filename_FITS_Mask);
    expand_envvar_string(BKEP_Fnames->CP_Filename_Log);

    if(BKEP_Stat->I_Verbose) {
    printf("bkgest_expand_envvar: %s\n",
       BKEP_Fnames->CP_Filename_FITS_Image1);
    printf("bkgest_expand_envvar: %s\n",
       BKEP_Fnames->CP_Filename_Log);
    }

}

int expand_envvar_string(char *CP_In)
{
  char  CP_Copy[STRING_BUFFER_SIZE];
  char  CP_Path[STRING_BUFFER_SIZE];
  char  CP_Expanded[STRING_BUFFER_SIZE];
  char  CP_Ent[STRING_BUFFER_SIZE];
  char  *CPP_TokSlash[STRING_BUFFER_SIZE];
  char  *CPP_TokDollar[STRING_BUFFER_SIZE];
  char  *CP_Slash, *CP_Dollar;

  int i, j, k, l, I_numchars;


  strcpy(CP_Copy, CP_In);
  strcpy(CP_Path, CP_In);

  /* strtok clobbers a leading "/" */
  if(strncmp(CP_Path, "/", 1))
    strcpy(CP_Expanded, "");
  else
    strcpy(CP_Expanded, "/");

  /* Replace the "/" token with nulls */
  i=0;
  //CPP_TokSlash[i++] = strtok(CP_Path, "/");
  //while (CPP_TokSlash[i++] = strtok(NULL, "/"));

  char* token;
  token = strtok(CP_Path, "/");
  CPP_TokSlash[i++] = token;
  while (token != NULL) {
        //printf("token = %s\n", token);
        token = strtok(NULL, "/");
        CPP_TokSlash[i++] = token;
  }

  for(j=0; j<i-1; j++) {

      /* For each string between the "/"'s put a null at any "$" */
      k=0;
      //CPP_TokDollar[k++] = strtok(CPP_TokSlash[j], "$");
      //while (CPP_TokDollar[k++] = strtok(NULL, "$"));

      token = strtok(CPP_TokSlash[j], "$");
      CPP_TokDollar[k++] = token;
      while (token != NULL) {
            //printf("token = %s\n", token);
            token = strtok(NULL, "$");
            CPP_TokDollar[k++] = token;
      }

      /* Read the resulting parsed strings */
      for(l=0; l<k-1; l++) {
        sscanf(CPP_TokDollar[l], "%s", CP_Ent);
        /* Expand as an environmental variable if there was a "$" */
        if(CPP_TokDollar[l]==CPP_TokSlash[j])
          strcat(CP_Expanded, CP_Ent);
        else
          strcat(CP_Expanded, getenv(CP_Ent));
      }
      if(j<i-2) strcat(CP_Expanded, "/");
  }

  /* strtok clobbers a trailing "/" */
  I_numchars = strlen(CP_Copy);
  if(!strncmp(&CP_Copy[I_numchars-1], "/", 1))
  strcat(CP_Expanded, "/");

  strcpy(CP_In, CP_Expanded);

  return 0;
}




