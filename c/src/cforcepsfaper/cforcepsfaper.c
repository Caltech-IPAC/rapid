/*******************************************************************************
     cforcepsfaper.c        2/4/6            Russ Laher
                                             laher@ipac.caltech.edu
                                             California Institute of Technology
                                             (c) 2026, All Rights Reserved.

     This C code was lifted from the ZTF git repo, and updated for the RAPID project.
     The ZTF-related comments below were retained for traceability.

     This C code replaces sub psffitandapphotom in forcedphotometry_trim.pl,
     used for the ZTF forced-photometry service, in which both PSF and
     aperture photometry are computed.

     Note on variable names: Prefix "alerts" comes from cforcepsffit.c,
     from which this C code was derived.  For the present application,
     "fps" for forced-photometry service is a more apt prefix, but was
     not changed to minimize the introduction of new bugs.

     Utilizes multi-threading for fast PSF and aperture forced photometry.
     Reads list of ZTF-pipeline difference images via -i option
     (ascending time order, indexed by k in function psffitandapphotom
     below).  Reads corresponding DAOPHOT PSF files by inferring filenames.
     Reads "alerts" from alert list via -a option, where multiple alerts
     are handeled.  Alert list includes indices, processed-image IDs,
     sky coordinates, and image coordindates, with alerts indexed by c.
     This code is initially intended for the case of just one "alert".
     Output file is specified via -o option, and gives table of fluxes
     and other results as functions of indexes c and k.

     Added -r switch to read upsampled and renormalized PSFs from
     ztf*rebinpsf.fits in current directory.
     E.g., ztf_20220811235069_000635_zr_c07_o_q1_rebinpsf.fits
     New Perl script forcedphotometry_trim_cforcepsfaper.pl
     generates the upsampled and renormalized PSFs prior to the
     execution of this C code.
*******************************************************************************/

#include <stdio.h>
#include <stdlib.h>
#include <sys/time.h>
#include <string.h>
#include <math.h>
#include <pthread.h>

#include "nanvalue.h"
#include "numericalrecipes.h"
#include "fitsio.h"
#include "cforcepsfaper.h"


int nint(double value) {
  int final;
  if (value >= 0.0) {
    final = (int) (value + 0.5);
  } else {
    final = (int) (value - 0.5);
  }
  return final;
}


int main(int argc, char **argv) {

    int i, status, verbose = 0;
    int readupsampledpsfs = 0;
    int debug = 0;
    int numrec = 0;
    int numalerts = 0;
    int numimagepositions = 0;
    char version[10];
    char codeName[FLEN_FILENAME];
    char developer[80];
    char inFile[FLEN_FILENAME];
    char inFile2[FLEN_FILENAME];
    char outFile[FLEN_FILENAME];
    char images[MAX_NUMBER_IMAGES][MAX_FILENAME_LENGTH];
    char psfimages[MAX_NUMBER_IMAGES][MAX_FILENAME_LENGTH];
    char fval[64];
    int nthreads = NUM_THREADS;

    int stampsz = 25;
    int stampupsamplefac = 5;
    double corrunc = 1.0;
    double maxbadpixfrac = 0.5;

    int pos = 0.5 * (double) (stampsz + 1);

    float *gain;
    int *offimage;
    long *alertpids;
    double *alertras, *alertdecs;
    double *alertxposs, *alertyposs;


    /* Set code name and software version number. */

    strcpy(codeName, CODENAME);
    sprintf(version, "%.2f", CODEVERSION);
    strcpy(developer, DEVELOPER);


    /* Initialize parameters */

    status = TERMINATE_SUCCESS;


    /* Get command-line arguments. */

    strcpy(inFile, "");
    strcpy(inFile2, "");
    strcpy(outFile, "" );

    if (argc < 2) {
        printUsage(codeName, version, developer, nthreads);
    }

    i = 1;
    while(i < argc) {
        if (argv[i][0] == '-') {
            switch(argv[i][1]) {
                case 'h':      /* -h (help switch) */
                    printUsage(codeName, version, developer, nthreads);
                case 'i':      /* -i <input list filename of difference images> */
                    if (++i >= argc) {
                        printf("-i <input list filename of difference images> missing argument...\n" );
                    } else {
                        if (argv[i-1][2] == '\0')
                            sscanf(argv[i], "%s", inFile);
                    }
                    break;
                 case 'a':      /* -a <input list filename of alert positions> */
                    if (++i >= argc) {
                        printf("-a <input list filename of alert positions> missing argument...\n" );
                    } else {
                        if (argv[i-1][2] == '\0')
                            sscanf(argv[i], "%s", inFile2);
                    }
                    break;
               case 'o':      /* -o <output mean FITS-image file> */
                    if (++i >= argc) {
                        printf("-o <output mean FITS-image file> missing argument...\n" );
                    } else {
                        if (argv[i-1][2] == '\0')
                            sscanf(argv[i], "%s", outFile);
                    }
                    break;
                case 't':      /* -t <number of processing threads> */
                    if (++i >= argc) {
                        printf("-t <number of processing threads> missing argument...\n");
                    } else {
                        if (argv[i-1][2] == '\0')
                            sscanf(argv[i], "%d", &nthreads);
                    }
                    break;
                case 'r':      /* -r (switch to read upsampled and renormalized PSFs from ztf*rebinpsf.fits in current directory) */
                    readupsampledpsfs = 1;
                    break;
                case 'v':      /* -v (verbose switch) */
                    verbose = 1;
                    break;
                default:
                printf("Unknown argument...\n");
            }
        } else {
            printf("Command line syntax error:\n");
            printf("   Previous argument = %s\n",argv[i - 1]);
            printf("   Current argument = %s\n",argv[i]);
        }
        i++;
    }

    if (strcmp(inFile,"") == 0) {
        printf("%s %s %s\n",
                "*** Error: No input difference-image filename specified",
                "(-i <input difference-image filename>);",
                "quitting...");
        exit(TERMINATE_FAILURE);
    }

    if (strcmp(inFile2,"") == 0) {
        printf("%s %s %s\n",
                "*** Error: No input alert-position filename specified",
                "(-i <input alert-position filename>);",
                "quitting...");
        exit(TERMINATE_FAILURE);
    }

    if (strcmp(outFile,"") == 0) {
        printf("%s %s %s\n",
               "*** Error: No output mean FITS-image file specified",
               "(-o <output mean FITS-image file>);",
               "quitting...");
        exit(TERMINATE_FAILURE);
    }

    if ((nthreads != 1) && (nthreads > 16)) {
        printf("nthreads (%d) is greater than 16; quitting...\n", nthreads);
        exit(TERMINATE_FAILURE);
    }

    printf("\n%s, v. %s by %s\n\n", codeName, version, developer);
    printf("Inputs:\n");
    printf("   Input list filename of difference images = %s\n", inFile);
    printf("   Input list filename of alert positions = %s\n", inFile2);
    printf("   Output lightcurve-data filename = %s\n", outFile);
    printf("   Number of processing threads = %d\n", nthreads);
    printf("   Read upsampled and renormalized PSFs from ztf*rebinpsf.fits in current directory = %d\n", readupsampledpsfs);
    printf("   Verbosity = %d\n\n", verbose);


    struct timeval tvBegin, tvEnd, tvDiff;

    // begin
    gettimeofday(&tvBegin, NULL);
    timeval_print(&tvBegin);


    /* Count lines in the list of difference images. */

    printf( "\n%s\n", "Counting lines in the list of difference images..." );

    int countstatus = countLinesInImagesList(inFile, verbose, &numrec);
    if (verbose > 0) {
      printf("countstatus, numrec = %d, %d\n", countstatus, numrec);
    }

    gain = (float *) malloc(numrec * sizeof(float *));


    /* Read in the list of difference images. */

    printf( "\n%s\n", "Reading list of difference images and associated gain values..." );

    int readstatus = readImagesList(inFile, verbose, &numrec, images, gain);
    if (verbose > 0) {
      printf("readstatus, numrec = %d, %d\n", readstatus, numrec);
    }


    /* Count image positions in alerts list. */

    printf( "\n%s\n", "Counting list of alert positions..." );

    int countstatus2 = countLinesInAlertsList(inFile2, verbose, &numimagepositions);
    if (verbose > 0) {
      printf("countstatus2, numimagepositions = %d, %d\n", countstatus2, numimagepositions);
    }

    offimage = (int *) malloc(numimagepositions * sizeof(int *));
    alertpids = (long *) malloc(numimagepositions * sizeof(long *));
    alertras = (double *) malloc(numimagepositions * sizeof(double *));
    alertdecs = (double *) malloc(numimagepositions * sizeof(double *));
    alertxposs = (double *) malloc(numimagepositions * sizeof(double *));
    alertyposs = (double *) malloc(numimagepositions * sizeof(double *));


    /* Read in the list of alert positions. */

    printf( "\n%s\n", "Reading list of alert positions..." );

    int readstatus2 = readAlertsList(inFile2, verbose, debug, &numalerts, alertpids, alertras, alertdecs, alertxposs, alertyposs);
    if (verbose > 0) {
      printf("readstatus2, numalerts = %d, %d\n", readstatus2, numalerts);
    }
    if (readstatus2 != 0) {
      printf("*** Error: Non-zero returned from function readAlertsList (readstatus2, numalerts = %d, %d); quitting...\n", readstatus2, numalerts);
      exit(ERROR_FROM_READALERTSLIST);
    }


    /* Replace string scimrefdiffimg with diffimgpsf to create DAOPHOT PSF filenames. */

    printf( "\n%s\n", "Forming PSF filenames..." );

    char imagefile[MAX_FILENAME_LENGTH];
    char origstr[] = "scimrefdiffimg";
    char replstr[] = "diffimgpsf";

    for (int k = 0; k < numrec; k++) {
      sprintf(imagefile, "%s", images[k]);
      char* psfimagefilename = NULL;
      psfimagefilename = replaceWord(imagefile, origstr, replstr);
      sprintf(psfimages[k], "%s", psfimagefilename);
      printf("k, DAOPHOT PSF file = %d, %s\n", k, psfimages[k]);
      free(psfimagefilename);
    }


    /* Read in the header of the first FITS-image file. */

    int naxis1;
    int naxis2;

    printf( "\n%s %s...\n", "Reading header info of", images[0] );
    int readhdrstatus = readHdrInfo(images[0], verbose, &naxis1, &naxis2);
    if (verbose > 0) {
      printf("readhdrstatus, naxis1, naxis2 = %d, %d, %d\n", readhdrstatus, naxis1, naxis2);
    }


    //end
    gettimeofday(&tvEnd, NULL);
    timeval_print(&tvEnd);

    // diff
    timeval_subtract(&tvDiff, &tvEnd, &tvBegin);
    printf("------------------------------------->Elapsed time (sec) = %ld.%06d\n", tvDiff.tv_sec, tvDiff.tv_usec);

    // begin
    gettimeofday(&tvBegin, NULL);
    timeval_print(&tvBegin);


    /* Read in the PSF data. */

    int imagesize = stampsz * stampsz;

    double *psfvals;
    psfvals = (double *) malloc(numrec * imagesize * sizeof(double *));

    if (readupsampledpsfs == 0) {
        printf( "\n%s\n", "Reading PSF images..." );
        int readdatastatus2 = readPsfData(psfimages, stampsz, stampsz, pos, pos, stampsz, verbose, numrec, imagesize, psfvals);
        if (verbose > 0) {
            printf("readdatastatus2 = %d\n", readdatastatus2);
        }

        //end
        gettimeofday(&tvEnd, NULL);
        timeval_print(&tvEnd);

        // diff
        timeval_subtract(&tvDiff, &tvEnd, &tvBegin);
        printf("------------------------------------->Elapsed time (sec) = %ld.%06d\n", tvDiff.tv_sec, tvDiff.tv_usec);

        // begin
        gettimeofday(&tvBegin, NULL);
        timeval_print(&tvBegin);
    }


    /* Read in the image data. */

    double *vals;
    long sizeofvals = (long) numalerts * (long) numrec * (long) imagesize * (long) sizeof(double *);
    vals = (double *) malloc(sizeofvals);

    printf( "\n%s\n", "Reading input images..." );

    int readdatastatus = readImageData(images, naxis1, naxis2, alertxposs, alertyposs, offimage, stampsz, verbose, numalerts, numrec, imagesize, vals);
    if (verbose > 0) {
        printf("readdatastatus = %d\n", readdatastatus);
    }


    //end
    gettimeofday(&tvEnd, NULL);
    timeval_print(&tvEnd);

    // diff
    timeval_subtract(&tvDiff, &tvEnd, &tvBegin);
    printf("------------------------------------->Elapsed time (sec) = %ld.%06d\n", tvDiff.tv_sec, tvDiff.tv_usec);

    // begin
    gettimeofday(&tvBegin, NULL);
    timeval_print(&tvBegin);


    /* Massage PSFs: Upsample, replace all negative values with zero, and renormalize. */

    int fineimagesize = stampsz * stampsz * stampupsamplefac * stampupsamplefac;

    double *finepsfvals;
    finepsfvals = (double *) malloc(numrec * fineimagesize * sizeof(double *));

    printf( "\n%s\n", "Massaging PSFs..." );

    int massagepsfsstatus =  massagePsfs(psfimages,
                                         psfvals,
                                         numrec,
                                         stampsz,
                                         stampupsamplefac,
                                         finepsfvals,
                                         readupsampledpsfs,
                                         verbose,
                                         debug);
    if (verbose > 0) {
        printf("massagepsfsstatus = %d\n", massagepsfsstatus);
    }


    /* Process the image data. */

    int numpoints = numalerts * numrec;

    float *forcPhotFlux;
    forcPhotFlux = (float *) malloc(numpoints * sizeof(float *));

    float *forcPhotFluxUnc;
    forcPhotFluxUnc = (float *) malloc(numpoints * sizeof(float *));

    float *forcPhotFluxSnr;
    forcPhotFluxSnr = (float *) malloc(numpoints * sizeof(float *));

    float *forcPhotFluxChisq;
    forcPhotFluxChisq = (float *) malloc(numpoints * sizeof(float *));

    float *forcPhotApFlux;
    forcPhotApFlux = (float *) malloc(numpoints * sizeof(float *));

    float *forcPhotApFluxUnc;
    forcPhotApFluxUnc = (float *) malloc(numpoints * sizeof(float *));

    float *forcPhotApFluxSnr;
    forcPhotApFluxSnr = (float *) malloc(numpoints * sizeof(float *));

    float *forcPhotApFluxCorr;
    forcPhotApFluxCorr = (float *) malloc(numpoints * sizeof(float *));

    int *errstatus0;
    errstatus0 = (int *) malloc(numpoints * sizeof(int *));

    int *errstatus2;
    errstatus2 = (int *) malloc(numpoints * sizeof(int *));


    //end
    gettimeofday(&tvEnd, NULL);
    timeval_print(&tvEnd);

    // diff
    timeval_subtract(&tvDiff, &tvEnd, &tvBegin);
    printf("------------------------------------->Elapsed time (sec) = %ld.%06d\n", tvDiff.tv_sec, tvDiff.tv_usec);

    // begin
    gettimeofday(&tvBegin, NULL);
    timeval_print(&tvBegin);

    printf( "%s %d %s\n", "Computing with", nthreads, "threads..." );

    int ncomputes = numalerts * numrec;

    if (nthreads == 1) {

        struct arg_struct args;
        args.tnum = 0;
        args.startindex = 0;
        args.endindex = ncomputes - 1;
        args.numalerts = numalerts;
        args.numrec = numrec;
        args.stampupsamplefac = stampupsamplefac;
        args.stampsz = stampsz;
        args.corrunc = corrunc;
        args.maxbadpixfrac = maxbadpixfrac;
        args.gain = &(gain[0]);
        args.verbose = verbose;
        args.debug = debug;
        for (int k = 0; k < numrec; k++) {
            strcpy(args.psfimages[k], psfimages[k]);
        }
        args.vals = &(vals[0]);
        args.finepsfvals = &(finepsfvals[0]);
        args.alertxposs = &(alertxposs[0]);
        args.alertyposs = &(alertyposs[0]);
        args.offimage = &(offimage[0]);
        args.forcPhotFlux = &(forcPhotFlux[0]);
        args.forcPhotFluxUnc = &(forcPhotFluxUnc[0]);
        args.forcPhotFluxSnr = &(forcPhotFluxSnr[0]);
        args.forcPhotFluxChisq = &(forcPhotFluxChisq[0]);
        args.forcPhotApFlux = &(forcPhotApFlux[0]);
        args.forcPhotApFluxUnc = &(forcPhotApFluxUnc[0]);
        args.forcPhotApFluxSnr = &(forcPhotApFluxSnr[0]);
        args.forcPhotApFluxCorr = &(forcPhotApFluxCorr[0]);
        args.errstatus0 = &(errstatus0[0]);
        args.errstatus2 = &(errstatus2[0]);
        compute(&args);
        printf( "First computed flux = %f\n", forcPhotFlux[0] );

    } else {


        /* Initialize and set thread detached attribute */

        pthread_t threads[nthreads];
        pthread_attr_t attr;

        pthread_attr_init(&attr);
        pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_JOINABLE);


        struct arg_struct targs[nthreads];
        int sx[nthreads], ex[nthreads];

        int nsegx = ncomputes / nthreads;
        int nremx = ncomputes % nthreads;

        if (verbose > 0)
            printf("ncomputes, nsegx, nremx = %d, %d, %d\n", ncomputes, nsegx, nremx);


        for (int t = 0; t < nthreads; t++) {

            int nsx = t * nsegx;
            int nex = (t + 1) * nsegx - 1;
            if (t == nthreads - 1) nex += nremx;

            if (verbose > 0) printf("t, nsx, nex = %d, %d, %d\n", t, nsx, nex);

            sx[t] = nsx;
            ex[t] = nex;
        }

        for (long t = 0; t < nthreads; t++) {
            targs[t].tnum = t + 1;
            targs[t].startindex = sx[t];
            targs[t].endindex = ex[t];
            targs[t].numalerts = numalerts;
            targs[t].numrec = numrec;
            targs[t].stampupsamplefac = stampupsamplefac;
            targs[t].stampsz = stampsz;
            targs[t].corrunc = corrunc;
            targs[t].maxbadpixfrac = maxbadpixfrac;
            targs[t].gain = &(gain[0]);
            targs[t].verbose = verbose;
            targs[t].debug = debug;
            for (int k = 0; k < numrec; k++) {
                strcpy(targs[t].psfimages[k], psfimages[k]);
            }
            targs[t].vals = &(vals[0]);
            targs[t].finepsfvals = &(finepsfvals[0]);
            targs[t].alertxposs = &(alertxposs[0]);
            targs[t].alertyposs = &(alertyposs[0]);
            targs[t].offimage = &(offimage[0]);
            targs[t].forcPhotFlux = &(forcPhotFlux[0]);
            targs[t].forcPhotFluxUnc = &(forcPhotFluxUnc[0]);
            targs[t].forcPhotFluxSnr = &(forcPhotFluxSnr[0]);
            targs[t].forcPhotFluxChisq = &(forcPhotFluxChisq[0]);
            targs[t].forcPhotApFlux = &(forcPhotApFlux[0]);
            targs[t].forcPhotApFluxUnc = &(forcPhotApFluxUnc[0]);
            targs[t].forcPhotApFluxSnr = &(forcPhotApFluxSnr[0]);
            targs[t].forcPhotApFluxCorr = &(forcPhotApFluxCorr[0]);
            targs[t].errstatus0 = &(errstatus0[0]);
            targs[t].errstatus2 = &(errstatus2[0]);
        }


        /* Create the independent processing threads. */

        for (long t = 0; t < nthreads; t++) {
            int rc = pthread_create(&threads[t], NULL, compute, (void *) &targs[t]);
            if (rc) {
                printf("ERROR; return code from pthread_create() of thread %ld is %d\n", t + 1, rc);
                exit(-1);
            }
        }


        /* Free attribute and wait for the other threads */

        void *threadstatus;
        pthread_attr_destroy(&attr);
        for (long t = 0; t < nthreads; t++) {
            int rc = pthread_join(threads[t], &threadstatus);
            if (rc) {
                printf("ERROR; return code from pthread_join() for thread %ld is %d\n", t + 1, rc);
                exit(-1);
            }

            if (verbose > 0)
                printf("Main: completed join with thread %ld having a status of %ld\n", t + 1, (long) threadstatus);
        }

        if (verbose > 0) {
            for (long t = 0; t < nthreads; t++) {
                printf( "Thread %ld: First computed flux = %f\n", t + 1, forcPhotFlux[t] );
            }
        }
    }


    //end
    gettimeofday(&tvEnd, NULL);
    timeval_print(&tvEnd);

    // diff
    timeval_subtract(&tvDiff, &tvEnd, &tvBegin);
    printf("------------------------------------->Elapsed time (sec) = %ld.%06d\n", tvDiff.tv_sec, tvDiff.tv_usec);

    // begin
    gettimeofday(&tvBegin, NULL);
    timeval_print(&tvBegin);


    /* Write results to output file. */

    printf( "\n%s = %s\n", "Writing results to outFile", outFile );

    FILE *fp_data;

    fp_data = fopen(outFile, "w");

    fprintf(fp_data, "c k pid forcPhotFlux forcPhotFluxUnc forcPhotFluxSnr forcPhotFluxChisq forcPhotApFlux forcPhotApFluxUnc forcPhotApFluxSnr forcPhotApFluxCorr errstatus0 errstatus2\n");

    for (int c = 0; c < numalerts; c++) {
        for (int k = 0; k < numrec; k++) {

            int indexpos = c * numrec + k;

            fprintf(fp_data, "%d %d %ld %f %f %f %f %f %f %f %f %d %d\n",
                    c, k,
                    alertpids[indexpos],
                    forcPhotFlux[indexpos],
                    forcPhotFluxUnc[indexpos],
                    forcPhotFluxSnr[indexpos],
                    forcPhotFluxChisq[indexpos],
                    forcPhotApFlux[indexpos],
                    forcPhotApFluxUnc[indexpos],
                    forcPhotApFluxSnr[indexpos],
                    forcPhotApFluxCorr[indexpos],
                    errstatus0[indexpos],
                    errstatus2[indexpos]);
        }
    }

    fclose(fp_data);


    //end
    gettimeofday(&tvEnd, NULL);
    timeval_print(&tvEnd);

    // diff
    timeval_subtract(&tvDiff, &tvEnd, &tvBegin);
    printf("------------------------------------->Elapsed time (sec) = %ld.%06d\n", tvDiff.tv_sec, tvDiff.tv_usec);


    /* Free memory. */

    free(gain);
    free(vals);
    free(psfvals);
    free(finepsfvals);
    free(offimage);
    free(alertpids);
    free(alertras);
    free(alertdecs);
    free(alertxposs);
    free(alertyposs);
    free(forcPhotFlux);
    free(forcPhotFluxUnc);
    free(forcPhotFluxSnr);
    free(forcPhotFluxChisq);
    free(forcPhotApFlux);
    free(forcPhotApFluxUnc);
    free(forcPhotApFluxSnr);
    free(forcPhotApFluxCorr);
    free(errstatus0);
    free(errstatus2);


    /* Terminate properly. */

    printf("Terminating with exit code = %d\n", status);

    if (nthreads == 1) {
        exit(status);
    } else {
        pthread_exit(NULL);
    }
}


void *compute(void *arguments) {

    FILE *fp_data;

    char psfimages[MAX_NUMBER_IMAGES][MAX_FILENAME_LENGTH];

    struct arg_struct *args = arguments;

    int tnum = args->tnum;
    int startindex = args->startindex;
    int endindex = args->endindex;
    int numalerts = args->numalerts;
    int numrec = args->numrec;
    int stampupsamplefac = args->stampupsamplefac;
    int stampsz = args->stampsz;
    double corrunc = args->corrunc;
    double maxbadpixfrac = args->maxbadpixfrac;
    float *gain = args->gain;
    int verbose = args->verbose;
    int debug = args->debug;
    for (int k = 0; k < numrec; k++) {
        strcpy(psfimages[k], args->psfimages[k]);
    }
    double *vals = args->vals;
    double *finepsfvals = args->finepsfvals;
    double *alertxposs = args->alertxposs;
    double *alertyposs = args->alertyposs;
    int *offimage = args->offimage;
    float *forcPhotFlux = args->forcPhotFlux;
    float *forcPhotFluxUnc = args->forcPhotFluxUnc;
    float *forcPhotFluxSnr = args->forcPhotFluxSnr;
    float *forcPhotFluxChisq = args->forcPhotFluxChisq;
    float *forcPhotApFlux = args->forcPhotApFlux;
    float *forcPhotApFluxUnc = args->forcPhotApFluxUnc;
    float *forcPhotApFluxSnr = args->forcPhotApFluxSnr;
    float *forcPhotApFluxCorr = args->forcPhotApFluxCorr;
    int *errstatus0 = args->errstatus0;
    int *errstatus2 = args->errstatus2;

    char outfile[256];
    sprintf(outfile, "cforcepsfaper_thread_%d.out", tnum);
    fp_data = fopen(outfile, "w");


    if (verbose > 0)
        fprintf(fp_data, "tnum, startindex, endindex = %d, %d, %d\n", tnum, startindex, endindex);

    int fineimagesize = stampsz * stampsz * stampupsamplefac * stampupsamplefac;


    /* Compute forced photometry. */

    int ncomputes = numalerts * numrec;

    for (int indexpos = startindex; indexpos <= endindex; indexpos++) {

        int c = indexpos / numrec;
        int k = indexpos - c * numrec;

        int *exitstatuseph;
        exitstatuseph = (int *) malloc(3 * sizeof(int *));

        exitstatuseph[0] = 0;
        exitstatuseph[1] = 0;
        exitstatuseph[2] = 0;
        double forcediffimflux = -99999;
        double forcediffimfluxunc = -99999;
        double forcediffimfluxsnr = -99999;
        double forcediffimfluxchisq = -99999;
        double forcediffimapflux = -99999;
        double forcediffimapfluxunc = -99999;
        double forcediffimapfluxsnr = -99999;
        double forcediffimapfluxcorr = -99999;

        if (offimage[indexpos] == 1) {
            if (verbose > 0) printf( "Stamp is off image for c, k = %d, %d\n", c, k );
            exitstatuseph[0] = 61;
        } else {

            double xpos = alertxposs[indexpos];
            double ypos = alertyposs[indexpos];

            int psffitphotomstatus = psffitandapphotom(fp_data,
                                                       c,
                                                       k,
                                                       numalerts,
                                                       numrec,
                                                       corrunc,
                                                       maxbadpixfrac,
                                                       psfimages[k],
                                                       vals,
                                                       finepsfvals,
                                                       stampupsamplefac,
                                                       stampsz,
                                                       xpos,
                                                       ypos,
                                                       gain,
                                                       &forcediffimflux,
                                                       &forcediffimfluxunc,
                                                       &forcediffimfluxsnr,
                                                       &forcediffimfluxchisq,
                                                       &forcediffimapflux,
                                                       &forcediffimapfluxunc,
                                                       &forcediffimapfluxsnr,
                                                       &forcediffimapfluxcorr,
                                                       exitstatuseph,
                                                       verbose,
                                                       debug);

            if (debug > 1) {
                printf("psffitphotomstatus = %d\n", psffitphotomstatus);
            }

        }

        if (verbose > 0) {
            printf("main: c, k, forcediffimflux, forcediffimfluxunc, forcediffimfluxsnr, forcediffimfluxchisq, exitstatuseph[0], exitstatuseph[2] = %d, %d, %f, %f, %f, %f, %d, %d\n\n",
                   c, k, forcediffimflux, forcediffimfluxunc, forcediffimfluxsnr, forcediffimfluxchisq, exitstatuseph[0], exitstatuseph[2]);
            printf("            forcediffimapflux, forcediffimapfluxunc, forcediffimapfluxsnr, forcediffimapfluxcorr  = %f, %f, %f, %f\n\n",
                   forcediffimapflux, forcediffimapfluxunc, forcediffimapfluxsnr, forcediffimapfluxcorr);
        }

        forcPhotFlux[indexpos] = forcediffimflux;
        forcPhotFluxUnc[indexpos] = forcediffimfluxunc;
        forcPhotFluxSnr[indexpos] = forcediffimfluxsnr;
        forcPhotFluxChisq[indexpos] = forcediffimfluxchisq;
        forcPhotApFlux[indexpos] = forcediffimapflux;
        forcPhotApFluxUnc[indexpos] = forcediffimapfluxunc;
        forcPhotApFluxSnr[indexpos] = forcediffimapfluxsnr;
        forcPhotApFluxCorr[indexpos] = forcediffimapfluxcorr;
        errstatus0[indexpos] = exitstatuseph[0];
        errstatus2[indexpos] = exitstatuseph[2];

        free(exitstatuseph);
    }

    fclose(fp_data);

    if (tnum > 0) pthread_exit(NULL);  // If thread number equals zero, then assume no multi-threading is called.

    return NULL;
}


/* Return 1 if the difference is negative, otherwise 0.  */
int timeval_subtract(struct timeval *result, struct timeval *t2, struct timeval *t1) {
    long diff = (t2->tv_usec + 1000000 * t2->tv_sec) - (t1->tv_usec + 1000000 * t1->tv_sec);
    result->tv_sec = diff / 1000000;
    result->tv_usec = diff % 1000000;

    return (diff<0);
}


void timeval_print(struct timeval *tv) {
    char buffer[30];
    time_t curtime;

    printf("%ld.%06d", tv->tv_sec, tv->tv_usec);
    curtime = tv->tv_sec;
    strftime(buffer, 30, "%m-%d-%Y  %T", localtime(&curtime));
    printf(" = %s.%06d\n", buffer, tv->tv_usec);
}


/* Software tutorial. */

int printUsage(char codeName[], char version[], char developer[], int nthreads) {
    printf("\n%s%s %s%s%s\n\n%s\n%s\n%s\n%s\n%s %d%s\n%s\n",
           codeName,
           ", v.", version,
           ", by ", developer,
           "Usage:",
           "-i <input list-of-images file>",
           "-a <input list-of-alert-positions file>",
           "-o <output lightcurve-data file>",
           "-t <number of processing threads> (default =",
           nthreads,
           ")\n[-r (switch to read upsampled PSFs from ztf*rebinpsf.fits in current directory)]",
           "[-v (verbose switch)]");
    exit(TERMINATE_SUCCESS);
}


/* Count lines in list of images. */

int countLinesInImagesList(char listOfImagesFilename[], int verbose, int *numrec) {

    char imagefile[MAX_FILENAME_LENGTH];
    char gainstr[MAX_FILENAME_LENGTH];
    int status = 0;
    FILE *fp_file;

    if ((fp_file = fopen(listOfImagesFilename,"r")) == NULL) {

        printf("countLinesInImagesList: %s\n",
              "List-of-images file could not be opened.");
        status = LIST_OF_IMAGES_COULD_NOT_BE_OPENED;
        exit(status);

    } else {

        int i = 0;
        while (!feof(fp_file)) {
            fscanf(fp_file, "%s", imagefile);
            fscanf(fp_file, "%s", gainstr);
            if (feof(fp_file)) break;
            if (strcmp(imagefile,"")) {

                if (i >= MAX_NUMBER_IMAGES) {
                    printf("countLinesInImagesList: Max. number of input images exceeded; quitting...\n");
                    status = MAX_NUM_IMAGES_EXCEEDED;
                    exit(status);
                }

                i++;
            }
        }

        if (fclose(fp_file)) {
            printf("countLinesInImagesList: %s\n",
                   "Image-data list file could not be closed.");
        }

        *numrec = i;

        printf("countLinesInImagesList: Number of input images = %d\n", *numrec);

    }

    return(status);
}


/* Read in list of images. */

int readImagesList(char listOfImagesFilename[], int verbose, int *numrec, char images[][MAX_FILENAME_LENGTH], float *gain) {

    char imagefile[MAX_FILENAME_LENGTH];
    char gainstr[MAX_FILENAME_LENGTH];
    int status = 0;
    FILE *fp_file;

    if ((fp_file = fopen(listOfImagesFilename,"r")) == NULL) {

        printf("readImagesList: %s\n",
              "List-of-images file could not be opened.");
        status = LIST_OF_IMAGES_COULD_NOT_BE_OPENED;
        exit(status);

    } else {

        int i = 0;
        while (!feof(fp_file)) {
            fscanf(fp_file, "%s", imagefile);
            fscanf(fp_file, "%s", gainstr);
            if (feof(fp_file)) break;
            if (strcmp(imagefile,"")) {

                if (i >= MAX_NUMBER_IMAGES) {
                    printf("readImagesList: Max. number of input images exceeded; quitting...\n");
                    status = MAX_NUM_IMAGES_EXCEEDED;
                    exit(status);
                }

                sprintf(images[i], "%s", imagefile);
                gain[i] = atof(gainstr);
                if (verbose > 0) {
                    printf("readImagesList: i, input file = %d, %s, %f\n", i, images[i], gain[i]);
                }
                i++;
            }
        }

        if (fclose(fp_file)) {
            printf("readImagesList: %s\n",
                   "Image-data list file could not be closed.");
        }

        *numrec = i;

    }

    return(status);
}


/* Read in header of image. */

int readHdrInfo(char imagefile[], int verbose, int *naxis1, int *naxis2) {

    int status = TERMINATE_SUCCESS;
    int I_fits_return_status = 0;
    fitsfile *ffp_FITS_In;


    /* Open input FITS file. */

    fits_open_file(&ffp_FITS_In,
                   imagefile,
                   READONLY,
                   &I_fits_return_status);

    if (verbose > 0)
        printf( "Status after opening image file = %d\n", I_fits_return_status );

    if (I_fits_return_status != 0) {
        printf("%s %s%s %s\n",
                      "*** Error: Could not open",
               imagefile,
               ";",
               "quitting...");
        exit(TERMINATE_FAILURE);
    }


    /* Read the keywords that tell the dimensions of the data */

    long LP_naxes[3];
    int I_ndims_found;
    fits_read_keys_lng(ffp_FITS_In,
                       "NAXIS",
                       1, 3,
                       LP_naxes,
                       &I_ndims_found,
                       &I_fits_return_status);

    if (I_fits_return_status) {
        printf("%s\n",
               "*** Error: Could not read NAXIS keywords; quitting...");
        exit(TERMINATE_FAILURE);
    }

    if (I_ndims_found > 2) {
        printf("%s %d %s\n",
               "*** Error: A single 2-D image plane is expected; found",
               I_ndims_found,
               "image planes; quitting...");
        exit(TERMINATE_FAILURE);
    }

    *naxis1 = (int) LP_naxes[0];
    *naxis2 = (int) LP_naxes[1];

    if (verbose > 0)
        printf("readHdrInfo: naxis1, naxis2 = %d, %d\n", *naxis1, *naxis2);


    /* Close input FITS file. */

    fits_close_file(ffp_FITS_In, &I_fits_return_status);

    if (verbose > 0)
        printf( "Status after closing image file = %d\n", I_fits_return_status );

    return(status);
}


/* Read in PSF images. */

int readPsfData(char images[][MAX_FILENAME_LENGTH], int naxis1, int naxis2, int x, int y, int sz, int verbose, int numrec, int imagesize, double *dataCube) {

    int status = TERMINATE_SUCCESS;
    char imagefile[MAX_FILENAME_LENGTH];
    int anynull;
    int I_fits_return_status = 0;
    double nullval = 0;
    fitsfile *ffp_FITS_In;

    for (int i = 0; i < numrec; i++) {

        /*
             round input position to nearest center-pixel (unit-based)
             and define initial cutout limits along x and y.
        */

        int xi = (float) x + 0.5;
        int yi = (float) y + 0.5;

        int hsz = 0.5 * ((float) sz - 1);
        int xmin = xi - hsz;
        int ymin = yi - hsz;
        int xmax = xi + hsz;
        int ymax = yi + hsz;

        /* reset limits if falls outside input image footprint. */

        if( xmin < 1 ) { xmin = 1; }
        if( xmax > naxis1 ) { xmax = naxis1; }
        if( ymin < 1 ) { ymin = 1; }
        if( ymax > naxis2 ) { ymax = naxis2; }

        int naxis1_stamp = (xmax - xmin + 1);
        int naxis2_stamp = (ymax - ymin + 1);

        sprintf(imagefile, "%s", images[i]);

        int index = i * imagesize;
        double *ptr = &dataCube[index];
        if (verbose > 0) printf("ptr = %p\n", ptr);


        /* Open input FITS file. */

        fits_open_file(&ffp_FITS_In,
                       imagefile,
                       READONLY,
                       &I_fits_return_status);

        if (verbose > 0)
            printf( "Status after opening image file = %d\n", I_fits_return_status );

        if (I_fits_return_status != 0) {
            printf("%s %s%s %s\n",
                          "*** Error: Could not open",
                   imagefile,
                   ";",
                   "quitting...");
            exit(TERMINATE_FAILURE);
        }


        /* Read subset of image. */

        long naxes[2];
        naxes[0] = naxis1;
        naxes[1] = naxis2;

        long fpixel[2];
        fpixel[0] = xmin;
        fpixel[1] = ymin;

        long lpixel[2];
        lpixel[0] = xmax;
        lpixel[1] = ymax;

        long inc[2];
        inc[0] = 1;
        inc[1] = 1;

        fits_read_subset_dbl(ffp_FITS_In,
                             0,
                             2,
                             naxes,
                             fpixel,
                             lpixel,
                             inc,
                             nullval,
                             ptr,
                             &anynull,
                             &I_fits_return_status);

        if (verbose > 0)
            printf( "Status after reading image data = %d\n", I_fits_return_status );

        if (I_fits_return_status != 0) {
            printf("%s %s%s %s\n",
                          "*** Error: Could not read image data from",
                   imagefile,
                   ";",
                   "quitting...");
            exit(TERMINATE_FAILURE);
        }

        if (verbose > 0)
            printf("dataCube 1, 2, 3 = %f, %f, %f\n", *ptr, *(ptr + 1), *(ptr + 2));
        if (verbose > 0)
            printf("dataCube 312, 313, 314 = %f, %f, %f\n", *(ptr + 312), *(ptr + 313), *(ptr + 314));


        /* Close input FITS file. */

        fits_close_file(ffp_FITS_In, &I_fits_return_status);

        if (verbose > 0)
            printf( "Status after closing image file = %d\n", I_fits_return_status );
    }

    return(status);
}


/* Read in image data. */

int readImageData(char images[][MAX_FILENAME_LENGTH], int naxis1, int naxis2, double *xpos, double *ypos, int *offimage, int sz, int verbose, int numalerts, int numrec, int imagesize, double *dataCube) {

    int status = TERMINATE_SUCCESS;
    char imagefile[MAX_FILENAME_LENGTH];
    int anynull;
    int I_fits_return_status = 0;
    double nullval = 0;
    fitsfile *ffp_FITS_In;

    for (int i = 0; i < numrec; i++) {

        sprintf(imagefile, "%s", images[i]);


        /* Open input FITS file. */

        fits_open_file(&ffp_FITS_In,
                       imagefile,
                       READONLY,
                       &I_fits_return_status);

        if (verbose > 0)
            printf( "Status after opening image file = %d\n", I_fits_return_status );

        if (I_fits_return_status != 0) {
            printf("%s %s%s %s\n",
                          "*** Error: Could not open",
                   imagefile,
                   ";",
                   "quitting...");
            exit(TERMINATE_FAILURE);
        }


        for (int c = 0; c < numalerts; c++) {

            //printf( "c = %d\n", c );

            long index = (long) c * (long) numrec * (long) imagesize + (long) i * (long) imagesize;
            double *ptr = &dataCube[index];
            if (verbose > 0) printf("ptr = %p\n", ptr);

            int indexpos = c * numrec + i;
            offimage[indexpos] = 0;                   /* Assume position is on image and away from edges. */
            double x = xpos[indexpos];
            double y = ypos[indexpos];


            /*
                 round input position to nearest center-pixel (unit-based)
                 and define initial cutout limits along x and y.
            */

            int xi = (int) (x + 0.5);
            int yi = (int) (y + 0.5);

            int hsz = 0.5 * ((float) sz - 1);
            int xmin = xi - hsz;
            int ymin = yi - hsz;
            int xmax = xi + hsz;
            int ymax = yi + hsz;

            if ((i==1) && (c==0)) printf( "x, y, xi, yi, hsz = %f, %f, %d, %d, %d\n", x, y, xi, yi, hsz );


            /* reset limits if falls outside input image footprint. */

            if ((xmin < 1) || (xmax > naxis1) || (ymin < 1) || (ymax > naxis2)) {
                offimage[indexpos] = 1;
                if (verbose > 0) printf( "Too close to edge (c=%d, i=%d, naxis1=%d, naxis2=%d, xmin=%d, xmax=%d, ymin=%d, ymax=%d, image=%s); skipping...\n", c, i, naxis1, naxis2, xmin, xmax, ymin, ymax, imagefile );
                continue;
            }

            int naxis1_stamp = (xmax - xmin + 1);
            int naxis2_stamp = (ymax - ymin + 1);


            /* Read subset of image. */

            long naxes[2];
            naxes[0] = naxis1;
            naxes[1] = naxis2;

            long fpixel[2];
            fpixel[0] = xmin;
            fpixel[1] = ymin;

            long lpixel[2];
            lpixel[0] = xmax;
            lpixel[1] = ymax;

            long inc[2];
            inc[0] = 1;
            inc[1] = 1;

            fits_read_subset_dbl(ffp_FITS_In,
                                 0,
                                 2,
                                 naxes,
                                 fpixel,
                                 lpixel,
                                 inc,
                                 nullval,
                                 ptr,
                                 &anynull,
                                 &I_fits_return_status);

            if (verbose > 0)
                printf( "Status after reading image data = %d\n", I_fits_return_status );

            if (I_fits_return_status != 0) {
                printf("%s %s%s %s\n",
                       "*** Error: Could not read image data from",
                       imagefile,
                       ";",
                       "quitting...");
                exit(TERMINATE_FAILURE);
            }

            if (verbose > 0)
                printf("dataCube 1, 2, 3 = %f, %f, %f\n", *ptr, *(ptr + 1), *(ptr + 2));
            if (verbose > 0)
                printf("dataCube 312, 313, 314 = %f, %f, %f\n", *(ptr + 312), *(ptr + 313), *(ptr + 314));
        }


        /* Close input FITS file. */

        fits_close_file(ffp_FITS_In, &I_fits_return_status);

        if (verbose > 0)
            printf( "Status after closing image file = %d\n", I_fits_return_status );
    }

    return(status);
}


char* replaceWord(const char* s, const char* oldW, const char* newW)
{
    char* result;
    int i, cnt = 0;
    int newWlen = strlen(newW);
    int oldWlen = strlen(oldW);

    // Counting the number of times old word
    // occur in the string
    for (i = 0; s[i] != '\0'; i++) {
        if (strstr(&s[i], oldW) == &s[i]) {
            cnt++;

            // Jumping to index after the old word.
            i += oldWlen - 1;
        }
    }

    // Making new string of enough length
    result = (char*)malloc(i + cnt * (newWlen - oldWlen) + 1);

    i = 0;
    while (*s) {
        // compare the substring with the result
        if (strstr(s, oldW) == s) {
            strcpy(&result[i], newW);
            i += newWlen;
            s += oldWlen;
        }
        else
            result[i++] = *s++;
    }

    result[i] = '\0';
    return result;
}


/* Count image positions. */

int countLinesInAlertsList(char listOfAlertPositions[], int verbose, int *numimagepositions) {

    char curr_token[MAX_LINE_LENGTH];
    char *curr_val;

    int status = 0;
    FILE *fp_file;

    if ((fp_file = fopen(listOfAlertPositions,"r")) == NULL) {

        printf("countLinesInAlertsList: %s\n",
              "List-of-images file could not be opened.");
        status = -3;
        return(status);

    } else {

        int i = 0;
        while (!feof(fp_file)) {
            fscanf(fp_file, "%s", curr_token);
            fscanf(fp_file, "%s", curr_token);
            fscanf(fp_file, "%s", curr_token);
            fscanf(fp_file, "%s", curr_token);
            fscanf(fp_file, "%s", curr_token);
            fscanf(fp_file, "%s", curr_token);
            fscanf(fp_file, "%s", curr_token);
            if (feof(fp_file)) break;
            if (strcmp(curr_token,"")) {

                i++;
            }
        }

        if (fclose(fp_file)) {
            printf("countLinesInAlertsList: %s\n",
                   "Image-data list file could not be closed.");
        }

        *numimagepositions = i;

    }

    return(status);
}


/* Read in list of alert positions. */

int readAlertsList(char listOfAlertPositions[],
                   int verbose,
                   int debug,
                   int *numalerts,
                   long *alertpids,
                   double *alertras,
                   double *alertdecs,
                   double *alertxposs,
                   double *alertyposs) {

    char curr_token[MAX_LINE_LENGTH];
    char *curr_val;
    char *eptr;

    int status = 0;
    FILE *fp_file;

    if ((fp_file = fopen(listOfAlertPositions,"r")) == NULL) {

        printf("readAlertsList: %s\n",
              "List-of-images file could not be opened.");
        status = -4;
        return(status);

    } else {

        int i = 0;
        int j = 0;
        while (!feof(fp_file)) {
            fscanf(fp_file, "%s", curr_token);
            if (feof(fp_file)) break;
            if (strcmp(curr_token,"")) {


                /*
                   i is not an array index, but rather counter of alerts.
                 */

                if (i > MAX_NUMBER_ALERTS) {
                    printf("readAlertsList: Max. number of input alerts exceeded; quitting...\n");
                    status = -5;
                    return(status);
                }


                int cindex = atoi(curr_token);
                fscanf(fp_file, "%s", curr_token);
                int iindex = atoi(curr_token);
                fscanf(fp_file, "%s", curr_token);
                long alertpid = atol(curr_token);
                fscanf(fp_file, "%s", curr_token);
                double alertra = strtod(curr_token, &eptr);
                fscanf(fp_file, "%s", curr_token);
                double alertdec = strtod(curr_token, &eptr);
                fscanf(fp_file, "%s", curr_token);
                double x = strtod(curr_token, &eptr);
                fscanf(fp_file, "%s", curr_token);
                double y = strtod(curr_token, &eptr);

                alertpids[j] = alertpid;
                alertras[j] = alertra;
                alertdecs[j] = alertdec;
                alertxposs[j] = x;
                alertyposs[j] = y;

                if (debug > 1) {
                    printf("%d, %d, %d, %ld, %.7f, %.7f, %.11f, %.11f\n", j, cindex, iindex, alertpid, alertra, alertdec, x, y);
                }
                j++;
                if (iindex == 0 ) i++;
            }
        }

        if (fclose(fp_file)) {
            printf("readAlertsList: %s\n",
                   "Image-data list file could not be closed.");
        }

        *numalerts = i;

    }

    return(status);
}


int bilinearinterp(double *grid, double *outinterp, int c, int k, int numrec, int nxdim, int nydim, int stampupsamplefac) {

    int status = 0;


    /* Set up grid on image. */

    int sx, sy;
    int npts = stampupsamplefac;
    double rx = (double) nxdim / (double) npts;
    double ry = (double) nydim / (double) npts;
    sx = nint(rx);
    sy = nint(ry);

    double gx[sx], gy[sy];

    double ratiox = ((double) (nxdim - 1)) / (rx - 1.0);
    double ratioy = ((double) (nydim - 1)) / (ry - 1.0);
    double tol = 1.0e-6;

    for (int j = 0; j < sx; j++) {
      gx[j] = (double) j * ratiox;
    }

    for (int i = 0; i < sy; i++) {
      gy[i] = (double) i * ratioy;
    }

    int gridimagesize = sx * sy;
    int gridindexoffset = c * numrec * gridimagesize + k * gridimagesize;


    /* Bilinear interpolation. */

    for (int ii = 0; ii < nydim; ii++) {
        for (int jj = 0; jj < nxdim; jj++) {

            double xpt = (double) jj;
            double ypt = (double) ii;

            int jp1 = 0;
            for (int j = 0; j < sx; j++) {
                if ( gx[j] >= xpt - tol )
                {
                    jp1 = j;
                    break;
                }
            }
            if ( jp1 == 0 )
            {
                jp1 = 1;
            }

            int ip1 = 0;
            for (int i = 0; i < sy; i++) {
                if ( gy[i] >= ypt - tol )
                {
                     ip1 = i;
                     break;
                }
            }
            if ( ip1 == 0 )
            {
                ip1 = 1;
            }

            int j = jp1 - 1;
            int i = ip1 - 1;
            double x = gx[j];
            double xp1 = gx[jp1];
            double y = gy[i];
            double yp1 = gy[ip1];

            double mapij = grid[gridindexoffset + i * sx + j];
            double mapip1j = grid[gridindexoffset + ip1 * sx + j];
            double mapijp1 = grid[gridindexoffset + i * sx + jp1];
            double mapip1jp1 = grid[gridindexoffset + ip1 * sx + jp1];

            double t = (xpt - x) / (xp1 - x);
            double u = (ypt - y) / (yp1 - y);
            double result = (1 - t) * (1 - u) * mapij +
                            t * (1 - u) * mapijp1 +
                            t * u * mapip1jp1 +
                            (1 - t) * u * mapip1j;

            int index = ii * nxdim + jj;
            outinterp[index] = result;
        }
    }

    return(status);
}


/* Write float image data to output FITS file. */

int writeFloatImageData(char outFile[], int verbose, int naxis1, int naxis2, int iparm, float fparm, float *image) {

    int status = TERMINATE_SUCCESS;
    char CP_Keyname[FLEN_KEYWORD];
    char CP_Comment[FLEN_COMMENT];
    int I_fits_return_status = 0;
    long I_Num_Out;
    fitsfile *ffp_FITS_Out;


    /* Open output FITS file. */

    fits_create_file(&ffp_FITS_Out,
                     outFile,
                     &I_fits_return_status);

    if (verbose > 0)
        printf("status after opening output FITS file = %d\n", I_fits_return_status);

     if (I_fits_return_status != 0) {
         if (I_fits_return_status == 105) {
             printf("%s %s %s %s\n",
                    "*** Error: Could not create",
                    outFile,
                    "(perhaps it already exists or disk quota exceeded?);",
                    "quitting...");
         } else {
             printf("%s %s%s %s\n",
                    "*** Error: Could not create",
                    outFile,
                    ";",
                    "quitting...");
        }
        exit(TERMINATE_FAILURE);
    }


    sprintf(CP_Keyname, "%s", "SIMPLE");
    sprintf(CP_Comment, "%s", "STANDARD FITS FORMAT");
    I_Num_Out = 1;
    fits_update_key(ffp_FITS_Out,
                    TLOGICAL,
                    CP_Keyname,
                    &I_Num_Out,
                    CP_Comment,
                    &I_fits_return_status);

    sprintf(CP_Keyname, "%s", "BITPIX  ");
    sprintf(CP_Comment, "%s", "IMAGE DATA TYPE");
    I_Num_Out = -32;
    fits_update_key(ffp_FITS_Out,
                    TLONG,
                    CP_Keyname,
                    &I_Num_Out,
                    CP_Comment,
                    &I_fits_return_status);

    sprintf(CP_Keyname, "%s", "NAXIS");
    sprintf(CP_Comment, "%s", "STANDARD FITS FORMAT");
    I_Num_Out = 2;
    fits_update_key(ffp_FITS_Out,
                    TLONG,
                    CP_Keyname,
                    &I_Num_Out,
                    CP_Comment,
                    &I_fits_return_status);

    sprintf(CP_Keyname, "%s", "NAXIS1");
    sprintf(CP_Comment, "%s", "STANDARD FITS FORMAT");
    I_Num_Out = naxis1;
    fits_update_key(ffp_FITS_Out,
                    TLONG,
                    CP_Keyname,
                    &I_Num_Out,
                    CP_Comment,
                    &I_fits_return_status);

    sprintf(CP_Keyname, "%s", "NAXIS2");
    sprintf(CP_Comment, "%s", "STANDARD FITS FORMAT");
    I_Num_Out = naxis2;
    fits_update_key(ffp_FITS_Out,
                    TLONG,
                    CP_Keyname,
                    &I_Num_Out,
                    CP_Comment,
                    &I_fits_return_status);

    sprintf(CP_Keyname, "%s", "IPARM");
    sprintf(CP_Comment, "%s", "Context-sensitive integer parameter");
    I_Num_Out = iparm;
    fits_update_key(ffp_FITS_Out,
                    TLONG,
                    CP_Keyname,
                    &I_Num_Out,
                    CP_Comment,
                    &I_fits_return_status);

    sprintf(CP_Keyname, "%s", "FPARM");
    sprintf(CP_Comment, "%s", "Context-sensitive floating-point parameter");
    float R_Num_Out = fparm;
    fits_update_key(ffp_FITS_Out,
                    TFLOAT,
                    CP_Keyname,
                    &R_Num_Out,
                    CP_Comment,
                    &I_fits_return_status);


    fits_flush_file(ffp_FITS_Out, &I_fits_return_status);

    if (verbose == 1)
        printf("Status after creating basic primary HDU = %d\n",
               I_fits_return_status);


    /* Output image data. */

    fits_write_img(ffp_FITS_Out,
                   TFLOAT,
                   1,
                   naxis1 * naxis2,
                   image,
                   &I_fits_return_status);

    if (verbose > 0)
        printf("status after writing image data = %d\n", I_fits_return_status);

    if (I_fits_return_status != 0) {
        printf("%s %s%s %s\n",
                      "*** Error: Could not write image data to",
               outFile,
               ";",
               "quitting...");
        exit(TERMINATE_FAILURE);
    }


    /* Close output FITS file. */

    fits_close_file(ffp_FITS_Out, &I_fits_return_status);

    if (verbose > 0)
        printf( "Status after closing image file = %d\n", I_fits_return_status );


    return(status);
}


int massagePsfs(char images[][MAX_FILENAME_LENGTH],
                double *psfvals,
                int numrec,
                int stampsz,
                int stampupsamplefac,
                double *finepsfvals,
                int flag,
                int verbose,
                int debug) {

    int status = 0;

    int imagesize = stampsz * stampsz;
    int nxdim = stampupsamplefac * stampsz;
    int nydim = nxdim;
    int fineimagesize = nxdim * nydim;

    for (int k = 0; k < numrec; k++) {

        double *interpresult;
        interpresult = (double *) malloc(fineimagesize * sizeof(double *));

        double dsum = 0.0;

        if (flag == 0) {


            /*
               Zero out negative values.
            */

            int indatacubeoffset = k * imagesize;

            for (int i = 0; i < stampsz; i++) {
                int offset = indatacubeoffset + i * stampsz;
                for (int j = 0; j < stampsz; j++) {
                    int index = offset + j;

                    if (psfvals[index] < 0.0) psfvals[index] = 0.0;
                }
            }

            if (debug > 0) {
                printf("\n-----loop start--------------->k = %d\n", k);
                 // Vertical slice.
                int j = 12;
                for (int i = 8; i < 15; i++) {
                    int index = indatacubeoffset + i * stampsz + j;
                    printf("Original PSF: i, j, index, psfvals[index] = %d, %d, %d, %f\n", i, j, index, psfvals[index]);
               }
            }


            /* Upsample PSF. */

            int retfrominterp = bilinearinterp(psfvals, interpresult, 0, k, numrec, nxdim, nydim, stampupsamplefac);
            //int retfrominterp = bicubicinterp(psfvals, interpresult, 0, k, numrec, nxdim, nydim, stampupsamplefac);
            if (verbose > 0) {
                printf("retfrominterp = %d\n", retfrominterp);
            }

            if (debug > 0) {
                printf("Function interp: interpresult[7798] = %f\n", interpresult[7798]);
                printf("Function interp: interpresult[7799] = %f\n", interpresult[7799]);
                printf("Function interp: interpresult[7800] = %f\n", interpresult[7800]);
                printf("Function interp: interpresult[7801] = %f\n", interpresult[7801]);
                printf("Function interp: interpresult[7802] = %f\n", interpresult[7802]);
                printf("Function interp: interpresult[7803] = %f\n", interpresult[7803]);
                printf("Function interp: interpresult[7804] = %f\n", interpresult[7804]);

                printf("Function interp: interpresult[7811] = %f\n", interpresult[7811]);
                printf("Function interp: interpresult[7812] = %f\n", interpresult[7812]);
                printf("Function interp: interpresult[7813] = %f\n", interpresult[7813]);
            }

            for (int i = 0; i < nydim; i++) {
                int offset = i * nxdim;
                for (int j = 0; j < nxdim; j++) {
                    int index = offset + j;

                    dsum += interpresult[index];
                }
            }

            if (debug > 0) printf("k, dsum = %d, %f\n", k, dsum);

        } else {

            char imagefile[MAX_FILENAME_LENGTH];
            char origstr[] = "diffimgpsf";
            char replstr[] = "rebinpsf";

            sprintf(imagefile, "%s", images[k]);

            if (debug > 0) printf( "\n%s = %s\n", "Original PSF filename", imagefile);

            char* psfimagefilename = NULL;
            psfimagefilename = replaceWord(imagefile, origstr, replstr);

            char* inFile = NULL;
            inFile = removePath(psfimagefilename);

            printf( "%s = %s\n", "Upsampled and renormalized PSF filename", inFile);

            int retfromreadUpsampledPsfData = readUpsampledPsfDataFromFitsFile(inFile, nxdim, nydim, verbose, interpresult);
            printf("retfromreadUpsampledPsfData = %d\n", retfromreadUpsampledPsfData);

            dsum = 1.0;        // Already renormalized.

            free(psfimagefilename);
        }






        if ((debug > 5) && (k == 18)) {
            char inFile[FLEN_FILENAME];
            //strcpy(inFile, "rebin/ztf_20220629305868_000795_zr_c07_o_q1_rebinpsf.fits" );
            //strcpy(inFile, "rebin2/ztf_20220815175451_000635_zg_c07_o_q1_rebinpsf.fits" );
            //strcpy(inFile, "rebin2/ztf_20220814214699_000635_zg_c07_o_q1_rebinpsf.fits" );
            strcpy(inFile, "debug2/ztf_20220810327037_000635_zg_c07_o_q1_rebinpsf.fits" );
            printf("Reading upsampled PSF file from %s\n", inFile);
            int retfromreadUpsampledPsfData = readUpsampledPsfDataFromFitsFile(inFile, nxdim, nydim, verbose, interpresult);
            printf("retfromreadUpsampledPsfData = %d\n", retfromreadUpsampledPsfData);

            dsum = 1.0;        // Already renormalized.
        }







        int datacubeoffset = k * fineimagesize;

        for (int i = 0; i < nydim; i++) {
            int offset = i * nxdim;
            for (int j = 0; j < nxdim; j++) {
                int index = offset + j;
                int indexout = datacubeoffset + index;
                finepsfvals[indexout] = interpresult[index] / dsum;
            }
        }





        if (debug > 0) {
            // Vertical slice.
            int j = 62;
            for (int i = 50; i < 70; i++) {
                int offset = i * nxdim;
                int index = offset + j;
                int indexout = datacubeoffset + index;
                printf("After renormalization: i, j, indexout, finepsfvals[indexout] = %d, %d, %d, %f\n", i, j, indexout, finepsfvals[indexout]);
            }
        }




        free(interpresult);
    }


    /* Debug upsampled PSF.  Specify k-index to pick out desired PSF from data cube. */

    if (debug > 0) {
        int k = 18;
        char outFile[FLEN_FILENAME];
        strcpy(outFile, "psf.fits" );
        debugUpsampledPsf(k, outFile, nxdim, nydim, finepsfvals, verbose);
    }

    return(status);
}


int debugUpsampledPsf(int k, char outFile[], int nxdim, int nydim, double *finepsfvals, int verbose) {

    int status = 0;

    int fineimagesize = nxdim * nydim;

    float *data;
    data = (float *) malloc(fineimagesize * sizeof(float *));

    int datacubeoffset = k * fineimagesize;
    for (int i = 0; i < nydim; i++) {
        int offset = i * nxdim;
        for (int j = 0; j < nxdim; j++) {
            int index = offset + j;
            int indexin = datacubeoffset + offset + j;

            data[index] = (float) finepsfvals[indexin];
        }
    }

    int naxis1 = nxdim;
    int naxis2 = nydim;
    int iparm = 5;
    float fparm = 14.3;

    printf( "%s\n", "Writing output FITS image..." );
    int writedatastatus = writeFloatImageData(outFile, verbose, naxis1, naxis2, iparm, fparm, data);
    if (verbose > 0) {
        printf("writedatastatus = %d\n", writedatastatus);
    }

    free(data);

    return(status);
}


int debugStampImage(long datacubeoffset, char outFile[], int nxdim, int nydim, double *inpdata, int verbose) {

    int status = 0;

    int fineimagesize = nxdim * nydim;

    float *data;
    data = (float *) malloc(fineimagesize * sizeof(float *));

    for (int i = 0; i < nydim; i++) {
        int offset = i * nxdim;
        for (int j = 0; j < nxdim; j++) {
            int index = offset + j;
            long indexin = (long) datacubeoffset + (long) offset + (long) j;

            data[index] = (float) inpdata[indexin];
        }
    }

    int naxis1 = nxdim;
    int naxis2 = nydim;
    int iparm = 5;
    float fparm = 14.3;

    printf( "%s\n", "Writing output FITS image..." );
    int writedatastatus = writeFloatImageData(outFile, verbose, naxis1, naxis2, iparm, fparm, data);
    if (verbose > 0) {
        printf("writedatastatus = %d\n", writedatastatus);
    }

    free(data);

    return(status);
}


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
                      int debug) {

    int status = 0;

    double nanvalue = NANVALUE;

    int imagesize = stampsz * stampsz;

    int nxdim = stampupsamplefac * stampsz;
    int nydim = nxdim;
    int fineimagesize = nxdim * nydim;


    /*
       Compute flux in original data image.  Add a minimum background so all
       values are positive, which will be subtracted from the interpolated
       image after scaling the interpolated image to conserve flux.
    */

    long datacubeoffset = (long) c * (long) numrec * (long) imagesize + (long) k * (long) imagesize;

    int orignbadpixels = 0;
    double origdsum = 0.0;
    double origdmin = LARGEST_DOUBLE;

    for (int i = 0; i < stampsz; i++) {
        long offset = datacubeoffset + (long) i * (long) stampsz;
        for (int j = 0; j < stampsz; j++) {
            long index = offset + (long) j;

            if (vals[index] != 0 && iznanorinfd(vals[index])) {
                orignbadpixels++;
            } else {
                if (origdmin > vals[index]) origdmin = vals[index];
            }
        }
    }

    double *posvals;
    posvals = (double *) malloc(imagesize * sizeof(double *));

    for (int i = 0; i < stampsz; i++) {
        int offset = i * stampsz;
        for (int j = 0; j < stampsz; j++) {
            int index = offset + j;
            long indexin = datacubeoffset + (long) offset + (long) j;

            posvals[index] = vals[indexin] - origdmin;
        }
    }

    for (int i = 0; i < stampsz; i++) {
        int offset = i * stampsz;
        for (int j = 0; j < stampsz; j++) {
            int index = offset + j;

            if (posvals[index] != 0 && iznanorinfd(posvals[index])) {

            } else {
                origdsum += posvals[index];
            }
        }
    }

    if (verbose > 0) {
        fprintf(fp_data, "c, k, xpos, ypos, orignbadpixels, origdmin, origdsum = %d, %d, %f, %f, %d, %f, %f\n", c, k, xpos, ypos, orignbadpixels, origdmin, origdsum);
        fprintf(fp_data, "difpsffilename=%s\n", difpsffilename);
    }


    /* Upsample data. */

    double *interpresult;
    interpresult = (double *) malloc(fineimagesize * sizeof(double *));

    //int retfrominterp = bilinearinterp(posvals, interpresult, 0, 0, numrec, nxdim, nydim, stampupsamplefac);
    int retfrominterp = rebin(posvals, interpresult, nxdim, nydim, stampupsamplefac);
    if (verbose > 0) {
        fprintf(fp_data, "retfrominterp = %d\n", retfrominterp);
    }

    if (debug > 1) {
        fprintf(fp_data, "Function interp: interpresult[0] = %f\n", interpresult[0]);
        fprintf(fp_data, "Function interp: interpresult[1] = %f\n", interpresult[1]);
        fprintf(fp_data, "Function interp: interpresult[2] = %f\n", interpresult[2]);
    }

    int badpixels = 0;
    double dsum = 0.0;

    for (int i = 0; i < nydim; i++) {
        int offset = i * nxdim;
        for (int j = 0; j < nxdim; j++) {
            int index = offset + j;

            if (interpresult[index] != 0 && iznanorinfd(interpresult[index])) {
                badpixels++;
            } else {
                dsum += interpresult[index];
            }
        }
    }

    if (verbose > 0) {
        fprintf(fp_data, "c, k, badpixels, dsum = %d, %d, %d, %f\n", c, k, badpixels, dsum);
    }

    for (int i = 0; i < nydim; i++) {
        int offset = i * nxdim;
        for (int j = 0; j < nxdim; j++) {
            int index = offset + j;
            interpresult[index] = (interpresult[index] + origdmin) * origdsum / dsum;
        }
    }


    /* Recenter data more accurately on (x,y). */

    double *recenterresult;
    recenterresult = (double *) malloc(fineimagesize * sizeof(double *));

    int retfromrecenter = recenter(interpresult, recenterresult, xpos, ypos, nxdim, nydim, stampupsamplefac);
    if (verbose > 0) {
        fprintf(fp_data, "retfromrecenter = %d\n", retfromrecenter);
    }


    /* Output debug info. */

    if ((debug > 0) && (c == 3) && (k == 18)) {
        char outFile[FLEN_FILENAME];
        strcpy(outFile, "data.fits" );
        debugStampImage((long) 0, outFile, nxdim, nydim, recenterresult, verbose);
    }


    if ((debug > 0) && (c == 3) && (k == 18)) {
        char outFile[FLEN_FILENAME];
        strcpy(outFile, "data2.fits" );
        debugStampImage(datacubeoffset, outFile, stampsz, stampsz, vals, verbose);
    }


    /*
       Issue warning if bad pixels exist within upsampled stamp but still
       below max tolerable fraction of maxbadpixfrac.
    */

    double badpixfrac = ((double) badpixels) / ((double) fineimagesize);

    fprintf(fp_data, "badpixels, badpixfrac, maxbadpixfrac = %d, %f, %f\n", badpixels, badpixfrac, maxbadpixfrac);

    if ((badpixels > 0) && (badpixfrac <= maxbadpixfrac)) {

        fprintf(fp_data, "%s%d %s%d %s%d %s%f %s%f %s\n",
                "=== Warning: bad pixels were detected within the upsampled (c=",c,"k=",k,"badpixels=", badpixels,
                "badpixfrac=", badpixfrac,"maxbadpixfrac=", maxbadpixfrac,"difference-image cutout; photometry may be impacted; continuing...");

        exitstatuseph[2] = 56;
    }


    /* If fraction of bad pixels exceeds threshold; skip this epoch. */

    if ((badpixels > 0) && (badpixfrac > maxbadpixfrac)) {

        fprintf(fp_data, "=== Warning: bad-pixel fraction exceeded; photometry not possible; skipping...\n");

        exitstatuseph[2] = 55;

        return(exitstatuseph[2]);
    }


    /* Convert to PSF-weight map for use in photometry. */

    int psfindexoffset = k * fineimagesize;

    double psfsum = 0.0;

    for (int i = 0; i < nydim; i++) {
        int offsetin = psfindexoffset + i * nxdim;
        for (int j = 0; j < nxdim; j++) {
            int indexin = offsetin + j;

            if (! (finepsfvals[indexin] != 0 && iznanorinfd(finepsfvals[indexin]))) {
               psfsum += finepsfvals[indexin] * finepsfvals[indexin];
            }
        }
    }

    if (verbose > 0) {
        fprintf(fp_data, "c, k, psfsum = %d, %d, %f\n", c, k, psfsum);
    }

    double *rpsfvals;
    rpsfvals = (double *) malloc(fineimagesize * sizeof(double *));

    for (int i = 0; i < nydim; i++) {
        int offset = i * nxdim;
        int offsetin = psfindexoffset + offset;
        for (int j = 0; j < nxdim; j++) {
            int index = offset + j;
            int indexin = offsetin + j;

            if (finepsfvals[indexin] != 0 && iznanorinfd(finepsfvals[indexin])) {
                rpsfvals[index] = 0.0;
            } else {
                rpsfvals[index] = finepsfvals[indexin] / psfsum;
            }
        }
    }

    if (debug > 1) {
        fprintf(fp_data, "Function interp: rpsfvals[7810] = %f\n", rpsfvals[7810]);
        fprintf(fp_data, "Function interp: rpsfvals[7811] = %f\n", rpsfvals[7811]);
        fprintf(fp_data, "Function interp: rpsfvals[7812] = %f\n", rpsfvals[7812]);
        fprintf(fp_data, "Function interp: rpsfvals[7813] = %f\n", rpsfvals[7813]);
        fprintf(fp_data, "Function interp: rpsfvals[7814] = %f\n", rpsfvals[7814]);
    }


    /*
       Compute robust background dispersion in input original diff-image
       stamp with central maskrad pixel radius (containing purported
       source signal) omitted.
    */

    double maskrad = 5.0;
    int n = 0;

    int stampcen = (int) (0.5 * (double) stampsz);

    for (int i = 0; i < stampsz; i++) {
        long offset = datacubeoffset + (long) i * (long) stampsz;
        double dy = (double) (i - stampcen);
        for (int j = 0; j < stampsz; j++) {
            long index = offset + (long) j;
            double dx = (double) (j - stampcen);
            double radius = sqrt(dx * dx + dy * dy);

            if (radius >= maskrad) {

                if (! (vals[index] != 0 && iznanorinfd(vals[index]))) {
                    n++;
                }
            }
        }
    }

    double *data;
    data = (double *) malloc(n * sizeof(double *));

    n = 0;  // Reset for second pass.

    for (int i = 0; i < stampsz; i++) {
        long offset = datacubeoffset + (long) i * (long) stampsz;
        double dy = (double) (i - stampcen);
        for (int j = 0; j < stampsz; j++) {
            long index = offset + (long) j;
            double dx = (double) (j - stampcen);
            double radius = sqrt(dx * dx + dy * dy);

            if (radius >= maskrad) {

                if (! (vals[index] != 0 && iznanorinfd(vals[index]))) {
                    data[n] = vals[index];
                    n++;
                }
            }
        }
    }


    /*
        If there are not enough background pixels, skip this epoch. Normally,
        there are 544 background pixels in a 25x25 stamp image.
    */

    int nminbckpix = 100;

    if (n < nminbckpix) {

        fprintf(fp_data,
                "=== Warning: insufficient number of background pixels=%d (require at least nminbckpix=%d); photometry not possible; skipping...\n",
                n, nminbckpix);

        exitstatuseph[2] = 54;

        return(exitstatuseph[2]);
    }

    double sigmadiff = computescale(data, n);
    double pct50 = computemedian(data, n);
    double pct50upsamp = pct50 / (((double) stampupsamplefac) * ((double) stampupsamplefac));

    if (verbose > 0) {
        fprintf(fp_data, "c, k, n, sigmadiff, median, pct50upsamp = %d, %d, %d, %f, %f, %f\n", c, k, n, sigmadiff, pct50, pct50upsamp);
    }


    /*  Subtract background of pct50upsamp DN per upsampled pixel. */

    for (int i = 0; i < nydim; i++) {
        int offset = i * nxdim;
        for (int j = 0; j < nxdim; j++) {
            int index = offset + j;

            if (! (recenterresult[index] != 0 && iznanorinfd(recenterresult[index]))) {
                recenterresult[index] -= pct50upsamp;
            }
        }
    }


    /* Compute PSF-fit flux (DN) using the aligned diff-image and PSF-weight map. */

    *forcediffimflux = 0.0;

    for (int i = 0; i < nydim; i++) {
        int offset = i * nxdim;
        for (int j = 0; j < nxdim; j++) {
            int index = offset + j;

            if (! (recenterresult[index] != 0 && iznanorinfd(recenterresult[index]))) {
               *forcediffimflux += recenterresult[index] * rpsfvals[index];
            }
        }
    }


    /* Compute variance map for upsampled diff-image pixels used for photometry. */

    double *posrecenterresult;
    posrecenterresult = (double *) malloc(fineimagesize * sizeof(double *));

    for (int i = 0; i < nydim; i++) {
        int offset = i * nxdim;
        for (int j = 0; j < nxdim; j++) {
            int index = offset + j;

            posrecenterresult[index] = recenterresult[index];

            if (! (posrecenterresult[index] != 0 && iznanorinfd(posrecenterresult[index]))) {
                if (posrecenterresult[index] < 0.0) posrecenterresult[index] = 0.0;
            }
        }
    }

    double *varmap;
    varmap = (double *) malloc(fineimagesize * sizeof(double *));

    for (int i = 0; i < nydim; i++) {
        int offset = i * nxdim;
        for (int j = 0; j < nxdim; j++) {
            int index = offset + j;

            if (posrecenterresult[index] != 0 && iznanorinfd(posrecenterresult[index])) {
                varmap[index] = nanvalue;
            } else {
                varmap[index] = (posrecenterresult[index] / (double) gain[k]) +
                                ((sigmadiff * sigmadiff) / (((double) stampupsamplefac) * ((double) stampupsamplefac)));
            }
        }
    }



    /*
       Compute uncertainty in PSF-fit flux; multiply by a correction factor
       corrunc to obtain approximate consistency with expected sigma_mag
       (~ 1.0857/snr) for the estimated source mag or flux in DN.
    */

    double usum = 0;

    for (int i = 0; i < nydim; i++) {
        int offset = i * nxdim;
        for (int j = 0; j < nxdim; j++) {
            int index = offset + j;

            if (! (varmap[index] != 0 && iznanorinfd(varmap[index]))) {
                usum += varmap[index] * rpsfvals[index] * rpsfvals[index];
            }
        }
    }


    *forcediffimfluxunc = corrunc * sqrt(usum);


    /*
       Compute signal-to-noise ratio in PSF-fit flux.
    */

    *forcediffimfluxsnr = *forcediffimflux / *forcediffimfluxunc;


    /*
       Compute reduced chi-square for PSF-fit.
       Divide by number of independent degrees of freedom (i.e., account for pixel upsampling).
    */

    *forcediffimfluxchisq = 0.0;

    for (int i = 0; i < nydim; i++) {
        int offsetin = psfindexoffset + i * nxdim;
        int offset = i * nxdim;
        for (int j = 0; j < nxdim; j++) {
            int indexin = offsetin + j;
            int index = offset + j;

            if (! (varmap[index] != 0 && iznanorinfd(varmap[index]))) {
                if (varmap[index] != 0.0) {
                    if (! (recenterresult[index] != 0 && iznanorinfd(recenterresult[index]))) {
                        *forcediffimfluxchisq += (recenterresult[index] - *forcediffimflux * finepsfvals[indexin]) *
                                                 (recenterresult[index] - *forcediffimflux * finepsfvals[indexin]) / varmap[index];
                    }
                }
            }
        }
    }

    *forcediffimfluxchisq /= (double) ((stampsz * stampsz) - 1);


    /*
       Print results if verbose.
    */

    if (verbose > 0) {
        fprintf(fp_data, "psffitphotom: c, k, forcediffimflux, forcediffimfluxunc, forcediffimfluxsnr, forcediffimfluxchisq, exitstatuseph[0] = %d, %d, %f, %f, %f, %f, %d\n\n",
               c, k, *forcediffimflux, *forcediffimfluxunc, *forcediffimfluxsnr, *forcediffimfluxchisq, exitstatuseph[0]);
    }


   /*
       Compute aperture-photometry flux and uncertainty within a fixed aperture
       of diameter 9 native pixels on upsampled diff-image stamp; also estimate
       aperture (curve-of-growth) correction from upsampled, unit-normalized PSF;
       multiply uncertainty by correction factor corrunc like above.
    */

    double apdiam = 9.0;
    double aprad = 0.5 * apdiam * stampupsamplefac;
    double apcorrsum = 0.0;
    double appixsum = 0.0;
    double apvarsum = 0.0;
    int apnanpix = 0;

    int finestampden = (int) (0.5 * (double) nxdim);

    for (int i = 0; i < nydim; i++) {
        int offsetin = psfindexoffset + i * nxdim;
        int offset = i * nxdim;
        double dy = (double) (i - finestampden);
        for (int j = 0; j < nxdim; j++) {
            int indexin = offsetin + j;
            int index = offset + j;
            double dx = (double) (j - finestampden);
            double radius = sqrt(dx * dx + dy * dy);

            if (radius <= aprad) {
                apcorrsum += finepsfvals[indexin];
                if (recenterresult[index] != 0 && iznanorinfd(recenterresult[index])) {
                    apnanpix++;
                } else {
                    appixsum += recenterresult[index];
                    apvarsum += varmap[index];
                }
            }
        }
    }

    *forcediffimapfluxcorr = 1.0 / apcorrsum;


    /* Compute aperture-photometry flux. */

    *forcediffimapflux = *forcediffimapfluxcorr * appixsum;


    /* Compute aperture-photometry flux uncertainty. */

    *forcediffimapfluxunc = *forcediffimapfluxcorr * corrunc * sqrt(apvarsum);


    /* Compute signal-to-noise ratio in aperture flux. */

    *forcediffimapfluxsnr = *forcediffimapflux / *forcediffimapfluxunc;


    /*
       Print results if verbose.
    */

    if (verbose > 0) {
        fprintf(fp_data, "psffitphotom: c, k, forcediffimapflux, forcediffimapfluxunc, forcediffimapfluxsnr, forcediffimapfluxcorr = %d, %d, %f, %f, %f, %f\n\n",
               c, k, *forcediffimapflux, *forcediffimapfluxunc, *forcediffimapfluxsnr, *forcediffimapfluxcorr);
    }


    /*
       Debug actions.
    */

    if ((debug > 0) && (c == 3) && (k == 18)) {
        char outFile[FLEN_FILENAME];
        strcpy(outFile, "data3.fits" );
        debugStampImage((long) 0, outFile, nxdim, nydim, rpsfvals, verbose);
    }

    if ((debug > 0) && (c == 3) && (k == 18)) {
        char outFile[FLEN_FILENAME];
        strcpy(outFile, "data4.fits" );
        debugStampImage((long) 0, outFile, nxdim, nydim, varmap, verbose);
    }


    /*
       Free memory.
    */

    free(posvals);
    free(interpresult);
    free(recenterresult);
    free(rpsfvals);
    free(data);
    free(posrecenterresult);
    free(varmap);


    /*
       Return.
    */

    return(status);

}


int rebin(double *grid, double *outinterp, int nxdim, int nydim, int stampupsamplefac) {

    int status = 0;


    /* Set up grid on image. */

    int sx, sy;
    int npts = stampupsamplefac;
    double rx = (double) nxdim / (double) npts;
    double ry = (double) nydim / (double) npts;
    sx = nint(rx);
    sy = nint(ry);


    /* Resample image data. */

    for (int ii = 0; ii < nydim; ii++) {
        for (int jj = 0; jj < nxdim; jj++) {

            double xpt = (double) jj;
            double ypt = (double) ii;

            int i = floor(ypt / (double) npts);
            int j = floor(xpt / (double) npts);

            int index = ii * nxdim + jj;
            outinterp[index] = grid[i * sx + j];
        }
    }

    return(status);
}


int recenter(double *grid, double *outinterp, double xpos, double ypos, int nxdim, int nydim, int stampupsamplefac) {

    int status = 0;

    int xi = nint(xpos);
    int yi = nint(ypos);

    double diffx = ((double) xi) - xpos;
    double diffy = ((double) yi) - ypos;

    double delta = 1.0 / ((double) stampupsamplefac);

    int xshift = nint(diffx / delta);
    int yshift = nint(diffy / delta);

    //printf( "xpos, ypos, xi, yi, xshift, yshift = %f, %f, %d, %d, %d, %d\n", xpos, ypos, xi, yi, xshift, yshift);


    /* Shift image data.  Zero out data that is off grid. */

    for (int ii = 0; ii < nydim; ii++) {
        int i = ii - yshift;
        for (int jj = 0; jj < nxdim; jj++) {

            int j = jj - xshift;
            int index = ii * nxdim + jj;

            if ((j < 0) || (j >= nxdim) || (i < 0) || (i >= nydim)) {
                outinterp[index] = 0.0;
            } else {
                int indexin = i * nxdim + j;
                outinterp[index] = grid[indexin];
            }
        }
    }

    return(status);
}


/*
Bicubic interpolation within a grid square. Input quantities are y,y1,y2,y12 (as described in bcucof); x1l and x1u, the lower and upper coordinates of the grid square in the 1-direction; x2l and x2u likewise for the 2-direction; and x1,x2, the coordinates of the desired point for the interpolation. The interpolated function value is returned as ansy, and the interpolated gradient values as ansy1 and ansy2. This routine calls bcucof.
*/

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
            double *ansy2) {

    int i;
    double t,u,d1,d2;

    double **c;
    c = (double **) malloc(4 * sizeof(double *));
    for (i = 0; i < 4; i++)
        c[i] = (double *) malloc(4 * sizeof(double *));

    d1 = x1u - x1l;
    d2 = x2u - x2l;

    bcucof(y, y1, y2, y12, d1, d2, c);

    if (x1u == x1l || x2u == x2l) printf("Bad input in routine bcuint");
    t = (x1 - x1l) / d1;
    u = (x2 - x2l) / d2;
    *ansy = (*ansy2) = (*ansy1) = 0.0;

    for (i = 3; i >=0; i--) {
        *ansy = t * (*ansy) + ((c[i][3] * u + c[i][2]) * u + c[i][1]) * u + c[i][0];
        *ansy2 = t * (*ansy2) + (3.0 * c[i][3] * u + 2.0 * c[i][2]) * u + c[i][1];
        *ansy1 = u * (*ansy1) + (3.0 * c[3][i] * t + 2.0 * c[2][i]) * t + c[1][i];
    }

    *ansy1 /= d1;
    *ansy2 /= d2;
}


/*
    Given arrays y[1..4], y1[1..4], y2[1..4], and y12[1..4], containing the
    function, gradients, and cross derivative at the four grid points of a rectangular
    grid cell (numbered counterclockwise from the lower left), and given d1 and d2, the
    length of the grid cell in the 1- and 2-directions, this routine returns the
    table c[1..4][1..4] that is used by routine bcuint for bicubic interpolation.
*/

void bcucof(double y[], double y1[], double y2[], double y12[], double d1, double d2, double **c) {

    static int wt[16][16] = { 1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
                              0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,
                              -3,0,0,3,0,0,0,0,-2,0,0,-1,0,0,0,0,
                              2,0,0,-2,0,0,0,0,1,0,0,1,0,0,0,0,
                              0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,
                              0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,
                              0,0,0,0,-3,0,0,3,0,0,0,0,-2,0,0,-1,
                              0,0,0,0,2,0,0,-2,0,0,0,0,1,0,0,1,
                              -3,3,0,0,-2,-1,0,0,0,0,0,0,0,0,0,0,
                              0,0,0,0,0,0,0,0,-3,3,0,0,-2,-1,0,0,
                              9,-9,9,-9,6,3,-3,-6,6,-6,-3,3,4,2,1,2,
                              -6,6,-6,6,-4,-2,2,4,-3,3,3,-3,-2,-1,-1,-2,
                              2,-2,0,0,1,1,0,0,0,0,0,0,0,0,0,0,
                              0,0,0,0,0,0,0,0,2,-2,0,0,1,1,0,0,
                              -6,6,-6,6,-3,-3,3,3,-4,4,2,-2,-2,-2,-1,-1,
                              4,-4,4,-4,2,2,-2,-2,2,-2,-2,2,1,1,1,1 };

    int l, k, j, i;
    double xx, d1d2, cl[16], x[16];

    d1d2 = d1 * d2;

    for (i = 0; i < 4; i++) {
        x[i] = y[i];
        x[i + 4] = y1[i]*d1;
        x[i + 8] = y2[i]*d2;
        x[i + 12] = y12[i]*d1d2;
    }

    for (i = 0; i < 16; i++) {
        xx = 0.0;
        for (k = 0; k < 16; k++) {
            xx += ((double) wt[i][k]) * x[k];
        }
        cl[i] = xx;
    }

    l = 0;

    for (i = 0; i < 4 ; i++) {
        for (j = 0; j < 4; j++) {
            c[i][j] = cl[l];
            l++;
        }
    }
}


int bicubicinterp(double *grid, double *outinterp, int c, int k, int numrec, int nxdim, int nydim, int stampupsamplefac) {

    int status = 0;


    /* Set up grid on image. */

    double rx = (double) nxdim / (double) stampupsamplefac;
    double ry = (double) nydim / (double) stampupsamplefac;

    int ncols = nint(rx);
    int nrows = nint(ry);

    int gridimagesize = ncols * nrows;
    int gridindexoffset = c * numrec * gridimagesize + k * gridimagesize;

    double ratiox = (rx - 1.0) / ((double) (nxdim - 1));
    double ratioy = (ry - 1.0) / ((double) (nydim - 1));

    double **ya;
    ya = (double **) malloc(nrows * sizeof(double *));
    for (int i = 0; i < nrows; i++)
        ya[i] = (double *) malloc(ncols * sizeof(double *));

    double x1a[ncols];
    double x2a[nrows];

    double **y1a;
    y1a = (double **) malloc(nrows * sizeof(double *));
    for (int i = 0; i < nrows; i++)
        y1a[i] = (double *) malloc(ncols * sizeof(double *));

    double **y2a;
    y2a = (double **) malloc(nrows * sizeof(double *));
    for (int i = 0; i < nrows; i++)
        y2a[i] = (double *) malloc(ncols * sizeof(double *));

    double **y12a;
    y12a = (double **) malloc(nrows * sizeof(double *));
    for (int i = 0; i < nrows; i++)
        y12a[i] = (double *) malloc(ncols * sizeof(double *));

    for (int j = 0; j < ncols; j++) {
        x1a[j] = (double) j;
    }
    for (int i = 0; i < nrows; i++) {
        x2a[i] = (double) i;
    }

    for (int i = 0; i < nrows; i++) {
        for (int j = 0; j < ncols; j++) {
            int index = gridindexoffset + i * ncols + j;
            ya[i][j] = grid[index];
        }
    }

    int icenter = nrows / 2;
    int jcenter = ncols / 2;

    for (int i = 0; i < nrows; i++) {
        for (int j = 0; j < ncols; j++) {
            if ((i == icenter) && (j == jcenter)) {
                y1a[i][j] = 0.0;
                y2a[i][j] = 0.0;
                y12a[i][j] = 0.0;
            } else if ((i == 0) || (i == (nrows - 1)) || (j == 0) || (j == (ncols - 1))) {
                y1a[i][j] = 0.0;
                y2a[i][j] = 0.0;
              y12a[i][j] = 0.0;
            } else {
                y1a[i][j]=(ya[i][j+1]-ya[i][j-1])/(x1a[j+1]-x1a[j-1]);
                y2a[i][j]=(ya[i+1][j]-ya[i-1][j])/(x2a[i+1]-x2a[i-1]);
                y12a[i][j]=(ya[i+1][j+1]-ya[i+1][j-1]-ya[i-1][j+1]+ya[i-1][j-1])/((x1a[j+1]-x1a[j-1])*(x2a[i+1]-x2a[i-1]));
            }
        }
    }


    /* Bicubic interpolation. */


    for (int i = 0; i < nydim; i++) {

        double x2 = ((double) i) * ratioy;
        int offset = i * nxdim;

        for (int j = 0; j < nxdim; j++) {

            double x1 = ((double) j) * ratiox;
            int index = offset + j;

            if ((i == 0) || (i == (nydim - 1)) || (j == 0) || (j == (nxdim - 1))) {
                outinterp[index] = 0.0;
            } else {

                int ii = floor(x2);
                int jj = floor(x1);

                double y[4]   = { ya[ii][jj], ya[ii][jj+1], ya[ii+1][jj+1], ya[ii+1][jj] };
                double y1[4]  = { y1a[ii][jj], y1a[ii][jj+1], y1a[ii+1][jj+1], y1a[ii+1][jj] };
                double y2[4]  = { y2a[ii][jj], y2a[ii][jj+1], y2a[ii+1][jj+1], y2a[ii+1][jj] };
                double y12[4] = { y12a[ii][jj], y12a[ii][jj+1], y12a[ii+1][jj+1], y12a[ii+1][jj] };

                double x1l = (double) floor(x1);
                double x1u = (double) (floor(x1)) + 1.0;
                double x2l = (double) floor(x2);
                double x2u = (double) (floor(x2)) + 1.0;

                double ansy, ansy1, ansy2;

                bcuint(y, y1, y2, y12, x1l, x1u, x2l, x2u, x1, x2, &ansy, &ansy1, &ansy2);

                outinterp[index] = ansy;
            }
        }
    }

    return(status);
}


/* Read PSF image made from PDL Gaussian interpolation. */

int readUpsampledPsfDataFromFitsFile(char imagefile[], int naxis1, int naxis2, int verbose, double *dataCube) {

    int status = TERMINATE_SUCCESS;
    int anynull;
    int I_fits_return_status = 0;
    double nullval = 0;
    fitsfile *ffp_FITS_In;

    int xmin = 1;
    int ymin = 1;
    int xmax = naxis1;
    int ymax = naxis2;

    int index = 0;
    double *ptr = &dataCube[index];
    if (verbose > 0) printf("ptr = %p\n", ptr);


    /* Open input FITS file. */

    fits_open_file(&ffp_FITS_In,
                   imagefile,
                   READONLY,
                   &I_fits_return_status);

    if (verbose > 0)
        printf( "Status after opening image file = %d\n", I_fits_return_status );

    if (I_fits_return_status != 0) {
        printf("%s %s%s %s\n",
                      "*** Error: Could not open",
               imagefile,
               ";",
               "quitting...");
        exit(TERMINATE_FAILURE);
    }


    /* Read subset of image. */

    long naxes[2];
    naxes[0] = naxis1;
    naxes[1] = naxis2;

    long fpixel[2];
    fpixel[0] = xmin;
    fpixel[1] = ymin;

    long lpixel[2];
    lpixel[0] = xmax;
    lpixel[1] = ymax;

    long inc[2];
    inc[0] = 1;
    inc[1] = 1;

    fits_read_subset_dbl(ffp_FITS_In,
                         0,
                         2,
                         naxes,
                         fpixel,
                         lpixel,
                         inc,
                         nullval,
                         ptr,
                         &anynull,
                         &I_fits_return_status);

    if (verbose > 0)
        printf( "Status after reading image data = %d\n", I_fits_return_status );

    if (I_fits_return_status != 0) {
        printf("%s %s%s %s\n",
                      "*** Error: Could not read image data from",
               imagefile,
               ";",
               "quitting...");
        exit(TERMINATE_FAILURE);
    }

    if (verbose > 0)
        printf("dataCube 1, 2, 3 = %f, %f, %f\n", *ptr, *(ptr + 1), *(ptr + 2));
    if (verbose > 0)
        printf("dataCube 312, 313, 314 = %f, %f, %f\n", *(ptr + 312), *(ptr + 313), *(ptr + 314));


    /* Close input FITS file. */

    fits_close_file(ffp_FITS_In, &I_fits_return_status);

    if (verbose > 0)
        printf( "Status after closing image file = %d\n", I_fits_return_status );


    return(status);
}


char* removePath(const char* s)
{
    char* result;

    int len = strlen(s);

    //printf("----->len=%d\n", len);

    // Making new strings
    result = (char*) malloc(MAX_FILENAME_LENGTH);
    char* t = (char*) malloc(MAX_FILENAME_LENGTH);

    // March backwards until slash delimiter is found.

    int n = 0;
    for (int i = len - 1; i >= 0; i--) {

      //printf("----->s[i]=%c\n", s[i]);

      if (s[i] == '\0') continue;
      if (s[i] == '/') break;
      t[n] = s[i];
      n++;
    }

    for (int i = 0; i < n; i++) {
      result[i] = t[n - 1 - i];
    }

    result[n] = '\0';
    return result;
}
