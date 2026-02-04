
#include "fitsio.h"


/* Constants. */

#define CODENAME "cforcepsfaper"
#define CODEVERSION  1.6
#define DEVELOPER  "Russ Laher"

#define TERMINATE_SUCCESS 0
#define TERMINATE_WARNING 32
#define TERMINATE_FAILURE 64
#define LIST_OF_IMAGES_COULD_NOT_BE_OPENED 65
#define MAX_NUM_IMAGES_EXCEEDED 66
#define ERROR_FROM_READALERTSLIST 67

#define NUM_THREADS            1         // Default value.
#define	MAX_NUMBER_IMAGES      15000
#define	MAX_FILENAME_LENGTH    256
#define	MAX_LINE_LENGTH        256
#define	MAX_NUMBER_ALERTS      1500
#define LARGEST_DOUBLE         1.7976931348623157E+308


/* Structs. */

struct arg_struct {
  int tnum;
  int startindex;
  int endindex;
  int numalerts;
  int numrec;
  int stampupsamplefac;
  int stampsz;
  double corrunc;
  double maxbadpixfrac;
  float *gain;
  int verbose;
  int debug;
  char psfimages[MAX_NUMBER_IMAGES][MAX_FILENAME_LENGTH];
  double *vals;
  double *finepsfvals;
  double *alertxposs;
  double *alertyposs;
  int *offimage;
  float *forcPhotFlux;
  float *forcPhotFluxUnc;
  float *forcPhotFluxSnr;
  float *forcPhotFluxChisq;
  float *forcPhotApFlux;
  float *forcPhotApFluxUnc;
  float *forcPhotApFluxSnr;
  float *forcPhotApFluxCorr;
  int *errstatus0;
  int *errstatus2;
};


/* Prototypes. */

int nint(double value);

int debugUpsampledPsf(int k, char outFile[], int nxdim, int nydim, double *finepsfvals, int verbose);
int debugStampImage(long datacubeoffset, char outFile[], int nxdim, int nydim, double *inpdata, int verbose);

int printUsage(char codeName[], char version[], char developer[], int nthreads);

void timeval_print(struct timeval *tv);

int timeval_subtract(struct timeval *result, struct timeval *t2, struct timeval *t1);

void *compute(void *arguments);

int countLinesInImagesList(char listOfImagesFilename[], int verbose, int *numrec);

int readImagesList(char listOfImagesFilename[], int verbose, int *numrec, char images[][MAX_FILENAME_LENGTH], float *gain);

int readHdrInfo(char imagefile[], int verbose, int *naxis1, int *naxis2);

int readPsfData(char images[][MAX_FILENAME_LENGTH], int naxis1, int naxis2, int xpos, int ypos, int stampsz, int verbose, int numrec, int imagesize, double *data);

int readImageData(char images[][MAX_FILENAME_LENGTH], int naxis1, int naxis2, double *xpos, double *ypos, int *offimage, int sz, int verbose, int numalerts, int numrec, int imagesize, double *dataCube);

char* replaceWord(const char* s, const char* oldW, const char* newW);

int countLinesInAlertsList(char listOfAlertPositions[], int verbose, int *numimagepositions);

int bilinearinterp(double *grid, double *outinterp, int c, int k, int numrec, int nxdim, int nydim, int stampupsamplefac);

int writeFloatImageData(char outFile[], int verbose, int naxis1, int naxis2, int iparm, float fparm, float *image);

int readAlertsList(char listOfAlertPositions[],
                   int verbose,
                   int debug,
                   int *numalerts,
                   long *alertpids,
                   double *alertras,
                   double *alertdecs,
                   double *alertxposs,
                   double *alertyposs);

int massagePsfs(char images[][MAX_FILENAME_LENGTH],
                double *psfvals,
                int numrec,
                int stampsz,
                int stampupsamplefac,
                double *finepsfvals,
                int readupsampledpsfs,
                int verbose,
                int debug);

int psffitandapphotom(FILE *fp_data,
                      int c,
                      int k,
                      int numalerts,
                      int numrec,
                      double corrunc,
                      double maxbadpixfrac,
                      char difpsffilename[],
                      double *vals,
                      double *finepsfvals,
                      int stampupsamplefac,
                      int stampsz,
                      double xpos,
                      double ypos,
                      float *gain,
                      double *forcediffimflux,
                      double *forcediffimfluxunc,
                      double *forcediffimfluxsnr,
                      double *forcediffimfluxchisq,
                      double *forcediffimapflux,
                      double *forcediffimapfluxunc,
                      double *forcediffimapfluxsnr,
                      double *forcediffimapfluxcorr,
                      int *exitstatuseph,
                      int verbose,
                      int debug);

int rebin(double *grid, double *outinterp, int nxdim, int nydim, int stampupsamplefac);
int recenter(double *grid, double *outinterp, double xpos, double ypos, int nxdim, int nydim, int stampupsamplefac);

int bicubicinterp(double *grid, double *outinterp, int c, int k, int numrec, int nxdim, int nydim, int stampupsamplefac);

void bcucof(double y[], double y1[], double y2[], double y12[], double d1, double d2, double **c);

void bcuint(double y[],
            double y1[],
            double y2[],
            double y12[],
            double x1l,
            double x1u,
            double x2l,
            double x2u,
            double x1,
            double x2,
            double *ansy,
            double *ansy1,
            double *ansy2);

int readUpsampledPsfDataFromFitsFile(char imagefile[], int naxis1, int naxis2, int verbose, double *dataCube);

char* removePath(const char* s);
