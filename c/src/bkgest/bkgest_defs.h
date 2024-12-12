/***********************************************
bkgest_defs.h

Purpose

Define structures.

Overview


Definitions of Variables

D_Window = Number of pixels on a side of a square window for
           local-clippedmean-value inputs.

D_GridSpacing = Number of pixels between grid points where
                local clippedmeans are computed.

CP_Filename_Namelist = Namelist Data File Name.
CP_Filename_FITS_Image1 = First Input Image Data File Name.
CP_Filename_FITS_Out = Output ClippedMean-Value Image Data File Name.
CP_Filename_FITS_Out2 = Output Input-ClippedMean Image Data File Name.
CP_Filename_Data_Out = Output IPAC-Table Data File Name.
CP_Filename_Log = Logfile Name.

I_status = If error occurs, function and error
  code is written here. Nominally is zero. Most functions
  check this variable and will skip processing if it is nonzero.
  Computation loop sets the flag to zero and proceeds
  regardless, so that bad pixels will not prevent the processing of good ones.
I_NaNFlag = Flag to indicate that a non-number was produced
  in the computation loop, since that process does not
  convey error information into I_status.
I_NaNCount = A counter that keeps track of the number of NaNs produced.
I_Debug = A flag to control program for debugging purposes.
I_Verbose = A flag to turn on print statements to show program progress.
I_SuperVerbose = A flag to print results of computations.

I_Length_X = Length of first dimension of data.
I_Length_Y = Length of second dimension of data.
I_Num_Frames  = Number of Frames of data.
I_Operation = Which operation to perform.
I_Fits_Type = Which type of resulting image data to output.
I_Data_Plane = Which data planes in input-image data cube to process.

DP_Output_Array = Output array local clippedmean values.

***********************************************/


typedef struct {
    int     D_Window;
    int     D_NBadPixLocTol;
    int     D_NBadPixGloTol;
    int     D_GridSpacing;
    int     I_MaskMask;
    double  D_Pothole;
} BKE_Constants;


typedef struct {
    char CP_Filename_Namelist[MAX_FILENAME_LENGTH];
    char CP_Filename_FITS_Image1[MAX_FILENAME_LENGTH];
    char CP_Filename_FITS_Mask[MAX_FILENAME_LENGTH];
    char CP_Filename_FITS_Out[MAX_FILENAME_LENGTH];
    char CP_Filename_FITS_Out2[MAX_FILENAME_LENGTH];
    char CP_Filename_FITS_Out3[MAX_FILENAME_LENGTH];
    char CP_Filename_Data_Out[MAX_FILENAME_LENGTH];
    char CP_Filename_Log[MAX_FILENAME_LENGTH];
} BKE_Filenames;


typedef struct {
    char CP_Ancillary_File_Path[MAX_FILENAME_LENGTH];
} BKE_Pathnames;


typedef struct {
    int I_status;
    int I_NaNFlag;
    int I_NaNCount;
    int I_Debug;
    int I_Verbose;
    int I_SuperVerbose;
} BKE_Status;

typedef struct {
    int I_Length_X;
    int I_Length_Y;
    int I_Num_Frames;
    int I_Operation;
    int I_Fits_Type;
    int I_Data_Plane;
} BKE_FITSinfo;


/* Stored Results */

typedef struct {
    double* DP_Output_Array;
    double* DP_Output_Array2;
    double* DP_Output_Array3;
    double* GlobalClippedMeanValue;
    double* GlobalScaleValue;
    unsigned long* NumberBadPixels;
} BKE_Computation;


/* Function prototypes. */

int nint(double value);

void bkgest_parse_args(int argc,
                       char **argv,
                       BKE_Constants *BKEP_Const,
                       BKE_Filenames *BKEP_Fnames,
                       BKE_Pathnames *BKEP_Paths,
                       BKE_FITSinfo *BKEP_FITS,
                       BKE_Status *BKEP_Stat);

void bkgest_parse_namelist(int argc,
                           char **argv,
                           BKE_Constants  *BKEP_Const,
                           BKE_Filenames  *BKEP_Fnames,
                           BKE_Pathnames  *BKEP_Paths,
                           BKE_FITSinfo   *BKEP_FITS,
                           BKE_Status   *BKEP_Stat);

void bkgest_init_constants(BKE_Constants   *BKEP_Const,
                           BKE_Filenames   *BKEP_Fnames,
                           BKE_Pathnames   *BKEP_Paths,
                           BKE_FITSinfo    *BKEP_FITS,
                           BKE_Computation *BKEP_Comp,
                           BKE_Status    *BKEP_Stat);

void bkgest_read_data(BKE_Filenames *BKEP_Fnames,
                      BKE_FITSinfo *BKEP_FITS,
                      double **DPP_Data_Image1,
                      int **DPP_Data_Mask,
                      BKE_Status *BKEP_Stat);

void bkgest_read_image1(BKE_Filenames *BKEP_Fnames,
                        BKE_FITSinfo *BKEP_FITS,
                        double **DPP_Data_Image1,
                        BKE_Status *BKEP_Stat);

void bkgest_read_mask(BKE_Filenames *BKEP_Fnames,
                      BKE_FITSinfo  *BKEP_FITS,
                      int           **DPP_Data_Mask,
                      BKE_Status    *BKEP_Stat);

void bkgest_log_writer(BKE_Constants *BKEP_Const,
         BKE_FITSinfo *BKEP_FITS,
         BKE_Status *BKEP_Stat,
         BKE_Filenames *BKEP_Fnames,
         BKE_Pathnames *BKEP_Paths);

void bkgest_exit(double **DPP_Data_Image1,
                 int **DPP_Data_Mask,
                 BKE_Computation *BKEP_Comp,
                 BKE_FITSinfo *BKEP_FITS,
                 BKE_Filenames *BKEP_Fnames,
                 BKE_Status *BKEP_Stat);

void bkgest_output(BKE_Constants *BKEP_Const,
                   BKE_Filenames *BKEP_Fnames,
                   BKE_FITSinfo *BKEP_FITS,
                   BKE_Computation *BKEP_Comp,
                   BKE_Status *BKEP_Stat);

int expand_envvar_string(char *CP_In);

void bkgest_expand_envvar(BKE_Filenames *BKEP_Fnames,
                          BKE_Status *BKEP_Stat);

void getRootFilename(char *, char *);

void bkgest_compute_results(double *DP_Data_Image1,
                            int *DP_Data_Mask,
                            BKE_Constants *BKEP_Const,
                            BKE_Filenames *BKEP_Fnames,
                            BKE_FITSinfo *BKEP_FITS,
                            BKE_Computation *BKEP_Comp,
                            BKE_Status *BKEP_Stat);

void decode_message(int I_message,
                     int I_root,
                     char *CP_Message,
                     char *CP_Function,
                     BKE_Pathnames *BKEP_Paths,
                     BKE_Status *BKEP_Stat);

void bkgest_output_keywords(fitsfile         *ffp_FITS,
                            BKE_Constants    *BKEP_Const,
                            BKE_Filenames    *BKEP_Fnames,
                            BKE_FITSinfo     *BKEP_FITS,
                            BKE_Computation  *BKEP_Comp,
                            BKE_Status       *BKEP_Stat);

void bkgest_output_stdkeywords(fitsfile *ffp_FITS,
                               BKE_Filenames *BKEP_Fnames,
                               BKE_FITSinfo *BKEP_FITS,
                               BKE_Status *BKEP_Stat);

void bkgest_output_constkeywords(fitsfile   *ffp_FITS,
                                 BKE_Filenames   *BKEP_Fnames,
                                 BKE_Constants   *BKEP_Const,
                                 BKE_Computation   *BKEP_Comp,
                                 BKE_FITSinfo   *BKEP_FITS,
                                 BKE_Status   *BKEP_Stat);

void bkgest_output_filenkeywords(fitsfile *ffp_FITS,
                                 BKE_Filenames *BKEP_Fnames,
                                 BKE_FITSinfo *BKEP_FITS,
                                 BKE_Status *BKEP_Stat);

