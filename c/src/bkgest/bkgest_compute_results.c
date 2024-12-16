/***********************************************
bkgest_compute_results.c

Purpose

Main computational processing loop of program.

Compute either local clippedmean image, where the local clippedmean is
computed over specified square window (containing odd number
of pixels on a side), or global clippedmean over entire image,
or both.

Overview
Loop over pixels, call processing functions for each pixel.

Definitions of Variables

External:
See bkgest_defs.h and bkgest_exec.c


I_Length_X = Length of second dimension of data.
I_Length_Y = Length of first dimension of data.
I_Num_Frames  = Number of Frames of data.
I_Operation = Which operation to perform.
I_Tot_Pixels = Total number of pixels in image.
I_Index = Index of current pixel in loop.

***********************************************/

#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <string.h>
#include "bkgest_errcodes.h"
#include "bkgest.h"
#include "bkgest_defs.h"
#include "nanvalue.h"
#include "numericalrecipes.h"

void bkgest_compute_results(double              *DP_Data_Image1,
                            int                 *DP_Data_Mask,
                            BKE_Constants       *BKEP_Const,
                            BKE_Filenames       *BKEP_Fnames,
                            BKE_FITSinfo        *BKEP_FITS,
                            BKE_Computation     *BKEP_Comp,
                            BKE_Status          *BKEP_Stat)
{
    int i, j, k, ii, jj, kk, kkk, iindex, jindex, I_MaskMask;
    int I_Length_X, I_Length_Y, I_Num_Frames, I_Operation;
    int I_Tot_Pixels, I_Index, I_Tot_PixelsOut;
    int nbadpixels, nwin, nhalfw;
    int kstart, kend, I_Data_Plane;
    int I_Fits_Type;
    int I_Index_Inp;
    int NBadPixGloTol, NBadPixLocTol;
    long l;
    unsigned long mid, *NumberBadPixels;
    double *Input_Array;
    double *DP_Output_Array, *DP_Output_Array2, *DP_Output_Array3, *GlobalClippedMeanValue, *GlobalScaleValue;
    double nanvalue = NANVALUE;






    double sigma = 2.5;





    /* For debug only */
    FILE    *fp_data;

    if (BKEP_Stat->I_status) return;


    /* Copy structure info to local variables. */

    I_Length_X = BKEP_FITS->I_Length_X;
    I_Length_Y = BKEP_FITS->I_Length_Y;
    I_Num_Frames = BKEP_FITS->I_Num_Frames;
    I_Tot_Pixels = I_Length_X*I_Length_Y*I_Num_Frames;
    I_Operation = BKEP_FITS->I_Operation;
    I_Fits_Type = BKEP_FITS->I_Fits_Type;
    I_Data_Plane = BKEP_FITS->I_Data_Plane;
    NBadPixLocTol = BKEP_Const->D_NBadPixLocTol;
    NBadPixGloTol = BKEP_Const->D_NBadPixGloTol;
    I_MaskMask = BKEP_Const->I_MaskMask;


    /* Set up grid on image. */

    int px, py, sx, sy;
    int npts = BKEP_Const->D_GridSpacing;
    double rx = (double) I_Length_Y / (double) npts;
    double ry = (double) I_Length_X / (double) npts;
    px = nint(rx);
    py = nint(ry);
    //printf( "px, rx = %d, %f\n", px, rx);
    //printf( "py, ry = %d, %f\n", py, ry);
    sx = px + 1;    // extra bin for end
    sy = py + 1;    // extra bin for end

    int gx[sx], gy[sy];

    for (j = 0; j < sx; j++) {
        gx[j] = j * npts;
    }
    gx[sx - 1] = I_Length_Y - 1;

    for (i = 0; i < sy; i++) {
        gy[i] = i * npts;
    }
    gy[sy - 1] = I_Length_X - 1;

    /*
    for (j = 0; j < sx; j++) {
        printf("gx[%d] = %d\n", j, gx[j]);
    }
    for (i = 0; i < sy; i++) {
        printf("gy[%d] = %d\n", i, gy[i]);
    }
    */


    /* Create local array. */

    Input_Array = (double *) calloc(I_Tot_Pixels, sizeof(double));
    if (Input_Array==NULL)
    {BKEP_Stat->I_status = COMPUTE_RES|MALLOC_FAILED; return;}


    /* Set controls for number image-data planes to treat. */

    if (I_Data_Plane == 3)
    {
        kstart = I_Num_Frames - 1;
        kend = I_Num_Frames;
        I_Num_Frames = 1;
        BKEP_FITS->I_Num_Frames = I_Num_Frames;
        I_Tot_PixelsOut = I_Length_X*I_Length_Y*I_Num_Frames;
    }
    else if (I_Data_Plane == 2)
    {
        kstart = 0;
        kend = 1;
        I_Num_Frames = 1;
        BKEP_FITS->I_Num_Frames = I_Num_Frames;
    }
    else
    {
        kstart = 0;
        kend = I_Num_Frames;
    }


    /* Create the output arrays for writing to file. */

    I_Tot_Pixels = I_Length_X*I_Length_Y*I_Num_Frames;

    BKEP_Comp->DP_Output_Array = (double *) calloc(I_Tot_Pixels, sizeof(double));
    if(BKEP_Comp->DP_Output_Array==NULL) {
       BKEP_Stat->I_status = COMPUTE_RES|MALLOC_FAILED;
       goto end_compute_results;
    }

    BKEP_Comp->DP_Output_Array3 = (double *) calloc(I_Tot_Pixels, sizeof(double));
    if(BKEP_Comp->DP_Output_Array3==NULL) {
       BKEP_Stat->I_status = COMPUTE_RES|MALLOC_FAILED;
       goto end_compute_results;
    }

    if (I_Fits_Type == 2 || I_Fits_Type == 3) {
       BKEP_Comp->DP_Output_Array2 = (double *) calloc(I_Tot_Pixels, sizeof(double));
       if(BKEP_Comp->DP_Output_Array2==NULL) {
          BKEP_Stat->I_status = COMPUTE_RES|MALLOC_FAILED;
          goto end_compute_results;
       }
    }

    BKEP_Comp->GlobalClippedMeanValue = (double *) calloc(I_Num_Frames, sizeof(double));
    if(BKEP_Comp->GlobalClippedMeanValue==NULL) {
       BKEP_Stat->I_status = COMPUTE_RES|MALLOC_FAILED;
       goto end_compute_results;
    }

    BKEP_Comp->GlobalScaleValue = (double *) calloc(I_Num_Frames, sizeof(double));
    if(BKEP_Comp->GlobalScaleValue==NULL) {
       BKEP_Stat->I_status = COMPUTE_RES|MALLOC_FAILED;
       goto end_compute_results;
    }

    BKEP_Comp->NumberBadPixels =
       (unsigned long *) calloc(I_Num_Frames, sizeof(unsigned long));
    if(BKEP_Comp->NumberBadPixels==NULL) {
       BKEP_Stat->I_status = COMPUTE_RES|MALLOC_FAILED;
       goto end_compute_results;
    }


    /* Copy structure info to local variables */

    DP_Output_Array = BKEP_Comp->DP_Output_Array;
    DP_Output_Array3 = BKEP_Comp->DP_Output_Array3;
    if (I_Fits_Type == 2 || I_Fits_Type == 3) {
       DP_Output_Array2 = BKEP_Comp->DP_Output_Array2;
    }
    GlobalClippedMeanValue = BKEP_Comp->GlobalClippedMeanValue;
    GlobalScaleValue = BKEP_Comp->GlobalScaleValue;
    NumberBadPixels = BKEP_Comp->NumberBadPixels;

    int NumBadPixLocTol = NBadPixLocTol * BKEP_Const->D_Window * BKEP_Const->D_Window * 0.01;
    int NumBadPixGloTol = NBadPixGloTol * I_Length_X * I_Length_Y * 0.01;


   /* Compute global clippedmean value. */

   if (I_Operation == 2 || I_Operation == 3) {

      if (BKEP_Stat->I_Verbose)
         printf("Performing global-clippedmean calculation...\n");

      if (I_Length_X * I_Length_Y <= NumBadPixGloTol) {
         BKEP_Stat->I_status = COMPUTE_RES|PARAM_OUT_OF_RANGE;
         goto end_compute_results;
      }

      kkk = 0;

      for (k = kstart; k < kend; k++) {

         nbadpixels = 0;
         l = 0;

         if (strcmp(BKEP_Fnames->CP_Filename_FITS_Mask,"")) {

            for (i=0;i<I_Length_X;i++)
            for (j=0;j<I_Length_Y;j++) {
               I_Index = k*I_Length_X*I_Length_Y+i*I_Length_Y+j;
               if ((DP_Data_Mask[I_Index] & I_MaskMask) == 0) {
                  if (DP_Data_Image1[I_Index] != 0 &&
                      iznanorinfd(DP_Data_Image1[I_Index])) {
                      nbadpixels++;
                  } else {
                     if (DP_Data_Image1[I_Index] <= BKEP_Const->D_Pothole) {
                         nbadpixels++;
                        continue;
                     }
                     Input_Array[l] = DP_Data_Image1[I_Index];
                     l++;
                  }
              } else {
                  nbadpixels++;
              }
            }

         } else {

            for (i=0;i<I_Length_X;i++)
            for (j=0;j<I_Length_Y;j++) {
               I_Index = k*I_Length_X*I_Length_Y+i*I_Length_Y+j;
               if (DP_Data_Image1[I_Index] != 0 &&
                   iznanorinfd(DP_Data_Image1[I_Index])) {
                   nbadpixels++;
               } else {
                   if (DP_Data_Image1[I_Index] <= BKEP_Const->D_Pothole) {
                       nbadpixels++;
                       continue;
                   }
                   Input_Array[l] = DP_Data_Image1[I_Index];
                   l++;
               }
            }
         }


     /*
         printf("l,nbadpixels = %d %d\n",l,nbadpixels);
         for (i=0;i<l;i++) {
            printf("i,input_array = %d %f\n",i,Input_Array[i]);
         }
     */

         NumberBadPixels[kkk] = nbadpixels;

     if (nbadpixels > NumBadPixGloTol) {
        BKEP_Stat->I_status = COMPUTE_RES|TOO_MANY_BAD_PIXELS;
        GlobalClippedMeanValue[kkk] = nanvalue;
        GlobalScaleValue[kkk] = nanvalue;

        if (BKEP_Stat->I_Verbose)
            printf("Too many bad pixels for global-clippedmean calculation.\n");
    }
    else
    {
        double clippedmean, clippedmeanunc;
        int nsamps, nrejects;
        computeclippedmean(Input_Array, l, sigma, &clippedmean, &clippedmeanunc, &nsamps, &nrejects);

        GlobalClippedMeanValue[kkk] = clippedmean;
        GlobalScaleValue[kkk] = computescale(Input_Array, l);
    }


         if (BKEP_Stat->I_Verbose) {
            printf("Image plane = %d\n",kkk);
            printf("Global clippedmean value = %f\n",
               BKEP_Comp -> GlobalClippedMeanValue[kkk]);
            printf("Global scale value = %f\n",
               BKEP_Comp -> GlobalScaleValue[kkk]);
            printf("Number of bad pixels = %ld\n",
               BKEP_Comp -> NumberBadPixels[kkk]);
         }

         kkk++;

      }

   }


    /* Compute local clippedmean image. */

   if (I_Operation==1 || I_Operation==3) {

      nwin = BKEP_Const->D_Window;

      if (BKEP_Stat->I_Verbose)
         printf("Window size = %d\n",nwin);

      if (nwin % 2 != 1) {
         BKEP_Stat->I_status = COMPUTE_RES|NOT_ODD;
         goto end_compute_results;
      }

      nhalfw = nwin / 2;

      if (nwin * nwin <= NumBadPixLocTol) {
         BKEP_Stat->I_status = COMPUTE_RES|PARAM_OUT_OF_RANGE;
         goto end_compute_results;

      }


      /* Compute local clippedmean. */

      kkk = 0;
      for (k = kstart; k < kend; k++) {

         double *grid;
         grid = (double *) calloc(sx * sy, sizeof(double));
         if (grid == NULL) {
             BKEP_Stat->I_status = COMPUTE_RES|MALLOC_FAILED;
             goto end_compute_results;
         }

         for (i = 0; i < sy; i++)
         for (j = 0; j < sx; j++) {

            nbadpixels = 0;
            l = 0;

            if (strcmp(BKEP_Fnames->CP_Filename_FITS_Mask,"")) {

                 /*
                printf("nhalfw,i,j = %d %d %d \n",nhalfw,i,j);
                */

                for (ii = gy[i] - nhalfw; ii <= gy[i] + nhalfw; ii++)
                for (jj = gx[j] - nhalfw; jj <= gx[j] + nhalfw; jj++) {

                   /*
                   printf("BEFORE: ii,jj = %d %d \n",ii,jj);
                   */

                   iindex = ii;
                   jindex = jj;

                   if (ii < 0) iindex = -ii;
                   else if (ii >= I_Length_X) iindex = 2 * I_Length_X - ii - 2;

                   if (jj < 0) jindex = -jj;
                   else if (jj >= I_Length_Y) jindex = 2 * I_Length_Y - jj - 2;

                   /*
                   printf("AFTER: ii,jj = %d %d \n",ii,jj);
                   */

                   I_Index = k * I_Length_X * I_Length_Y + iindex * I_Length_Y + jindex;


                    /* Check that input is a valid number. */

                    if ((DP_Data_Mask[I_Index] & I_MaskMask) == 0) {
                        if (DP_Data_Image1[I_Index] != 0 &&
                            iznanorinfd(DP_Data_Image1[I_Index])) {
                            nbadpixels++;
                        } else {
                    if (DP_Data_Image1[I_Index] <= BKEP_Const->D_Pothole) {
                                nbadpixels++;
                              continue;
                    }
                            Input_Array[l++] = DP_Data_Image1[I_Index];
                }
            } else {
                        nbadpixels++;
            }
                }

            } else {

                /*
                printf("nhalfw,i,j = %d %d %d \n",nhalfw,i,j);
                */

                for (ii = gy[i] - nhalfw; ii <= gy[i] + nhalfw; ii++)
                for (jj = gx[j] - nhalfw; jj <= gx[j] + nhalfw; jj++) {

                   /*
                   printf("BEFORE: ii,jj = %d %d \n",ii,jj);
                   */

                   iindex = ii;
                   jindex = jj;

                   if (ii < 0) iindex = -ii;
                   else if (ii >= I_Length_X) iindex = 2 * I_Length_X - ii - 2;

                   if (jj < 0) jindex = -jj;
                   else if (jj >= I_Length_Y) jindex = 2 * I_Length_Y - jj - 2;

                   /*
                   printf("AFTER: ii,jj = %d %d \n",ii,jj);
                   */

                   I_Index = k * I_Length_X * I_Length_Y + iindex * I_Length_Y + jindex;


                    /* Check that input is a valid number. */

                    if (DP_Data_Image1[I_Index] != 0 &&
                        iznanorinfd(DP_Data_Image1[I_Index])) {
                        nbadpixels++;
                    } else {
                if (DP_Data_Image1[I_Index] <= BKEP_Const->D_Pothole) {
                            nbadpixels++;
                          continue;
                }
                        Input_Array[l++] = DP_Data_Image1[I_Index];
            }
                }

        }

            I_Index = kkk * I_Length_X * I_Length_Y + gy[i] * I_Length_Y + gx[j];

            if (nbadpixels > NumBadPixLocTol) {

                // Lots of NaNs are expected in data, so do not set this status.
                //BKEP_Stat->I_status = COMPUTE_RES|TOO_MANY_BAD_PIXELS;

                if (I_Operation == 3) {
                    DP_Output_Array[I_Index] =  GlobalClippedMeanValue[kkk];
                    grid[i * sx + j] = GlobalClippedMeanValue[kkk];
                } else {
                    DP_Output_Array[I_Index] =  nanvalue;
                    grid[i * sx + j] = nanvalue;
                }
            } else {

                double clippedmean, clippedmeanunc;
                int nsamps, nrejects;
                computeclippedmean(Input_Array, l, sigma, &clippedmean, &clippedmeanunc, &nsamps, &nrejects);

                DP_Output_Array[I_Index] = clippedmean;
                grid[i * sx + j] = DP_Output_Array[I_Index];
            }


            if (BKEP_Stat->I_Verbose) {
           printf("Image plane, gx, gy = %d, %d, %d\n", kkk, gx[j], gy[i]);
               printf("Local clippedmean value = %f\n",
                      DP_Output_Array[I_Index]);
               printf("Number of bad pixels = %d\n",
                      nbadpixels);
            }


            /* Super-verbose diagnostic output. */

            if (i==1 && j==1) {
               if (BKEP_Stat->I_SuperVerbose)
               {
                  printf("%d %d %d\t", k, i, j);
                  printf("0x%x\t", BKEP_Stat->I_status);

                  if (DP_Data_Image1[I_Index] != 0 &&
                      iznanorinfd(DP_Data_Image1[I_Index])) {
                    printf("%s\t", "NaN");
          } else {
                     printf("%f\t", DP_Data_Image1[I_Index]);
          }
                  if (DP_Output_Array[I_Index] != 0 &&
                      iznanorinfd(DP_Output_Array[I_Index]))
                     printf("%s\n", "NaN");
                  else
                     printf("%f\n", DP_Output_Array[I_Index]);
           }
            }

         }


         /* Bilinear interpolation. */

         for (ii = 0; ii < I_Length_X; ii++)
         for (jj = 0; jj < I_Length_Y; jj++) {

         int xpt = jj;
             int ypt = ii;

             int jp1 = 0;
             for ( j = 0; j < sx; j++)
             {
                 if ( gx[j] >= xpt )
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
             for ( i = 0; i < sy; i++)
             {
                 if ( gy[i] >= ypt )
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
             int x = gx[j];
             int xp1 = gx[jp1];
             int y = gy[i];
             int yp1 = gy[ip1];

             double mapij = grid[i * sx + j];
             double mapip1j = grid[ip1 * sx + j];
             double mapijp1 = grid[i * sx + jp1];
             double mapip1jp1 = grid[ip1 * sx + jp1];

             double t = (double) (xpt - x) / (double) (xp1 - x);
             double u = (double) (ypt - y) / (double) (yp1 - y);
             double localBackgroundValue = (1 - t) * (1 - u) * mapij +
                                       t * (1 - u) * mapijp1 +
                                           t * u * mapip1jp1 +
                                           (1 - t) * u * mapip1j;

             I_Index = k * I_Length_X * I_Length_Y + ii * I_Length_Y + jj;
         DP_Output_Array[I_Index] = localBackgroundValue;
     }

         free(grid);
         kkk++;
      }


      /* Compute local sky scale. */

      kkk = 0;
      for (k = kstart; k < kend; k++) {

         double *grid;
         grid = (double *) calloc(sx * sy, sizeof(double));
         if (grid == NULL) {
             BKEP_Stat->I_status = COMPUTE_RES|MALLOC_FAILED;
             goto end_compute_results;
         }

         for (i = 0; i < sy; i++)
         for (j = 0; j < sx; j++) {

            nbadpixels = 0;
            l = 0;

            if (strcmp(BKEP_Fnames->CP_Filename_FITS_Mask,"")) {

                /*
                printf("nhalfw,i,j = %d %d %d \n",nhalfw,i,j);
                */

                for (ii = gy[i] - nhalfw; ii <= gy[i] + nhalfw; ii++)
                for (jj = gx[j] - nhalfw; jj <= gx[j] + nhalfw; jj++) {

                   /*
                   printf("BEFORE: ii,jj = %d %d \n",ii,jj);
                   */

                   iindex = ii;
                   jindex = jj;

                   if (ii < 0) iindex = -ii;
                   else if (ii >= I_Length_X) iindex = 2 * I_Length_X - ii - 2;

                   if (jj < 0) jindex = -jj;
                   else if (jj >= I_Length_Y) jindex = 2 * I_Length_Y - jj - 2;

                   /*
                   printf("AFTER: ii,jj = %d %d \n",ii,jj);
                   */

                   I_Index = k * I_Length_X * I_Length_Y + iindex * I_Length_Y + jindex;


                    /* Check that input is a valid number. */

                    if ((DP_Data_Mask[I_Index] & I_MaskMask) == 0) {
                        if (DP_Data_Image1[I_Index] != 0 &&
                            iznanorinfd(DP_Data_Image1[I_Index])) {
                            nbadpixels++;
                        } else {
                    if (DP_Data_Image1[I_Index] <= BKEP_Const->D_Pothole) {
                                nbadpixels++;
                              continue;
                    }
                            Input_Array[l++] = DP_Data_Image1[I_Index];
                }
            } else {
                        nbadpixels++;
            }
                }

            } else {

                /*
                printf("nhalfw,i,j = %d %d %d \n",nhalfw,i,j);
                */

                for (ii = gy[i] - nhalfw; ii <= gy[i] + nhalfw; ii++)
                for (jj = gx[j] - nhalfw; jj <= gx[j] + nhalfw; jj++) {

                   /*
                   printf("BEFORE: ii,jj = %d %d \n",ii,jj);
                   */

                   iindex = ii;
                   jindex = jj;

                   if (ii < 0) iindex = -ii;
                   else if (ii >= I_Length_X) iindex = 2 * I_Length_X - ii - 2;

                   if (jj < 0) jindex = -jj;
                   else if (jj >= I_Length_Y) jindex = 2 * I_Length_Y - jj - 2;

                   /*
                   printf("AFTER: ii,jj = %d %d \n",ii,jj);
                   */

                   I_Index = k * I_Length_X * I_Length_Y + iindex * I_Length_Y + jindex;


                    /* Check that input is a valid number. */

                    if (DP_Data_Image1[I_Index] != 0 &&
                        iznanorinfd(DP_Data_Image1[I_Index])) {
                        nbadpixels++;
                    } else {
                if (DP_Data_Image1[I_Index] <= BKEP_Const->D_Pothole) {
                          nbadpixels++;
              continue;
                }
                        Input_Array[l++] = DP_Data_Image1[I_Index];
            }
                }

        }

            I_Index = kkk * I_Length_X * I_Length_Y + gy[i] * I_Length_Y + gx[j];

            if (nbadpixels > NumBadPixLocTol) {

                // Lots of NaNs are expected in data, so do not set this status.
                //BKEP_Stat->I_status = COMPUTE_RES|TOO_MANY_BAD_PIXELS;

                if (I_Operation == 3) {
                    DP_Output_Array3[I_Index] =  GlobalScaleValue[kkk];
                    grid[i * sx + j] = GlobalScaleValue[kkk];
        } else {
                    DP_Output_Array3[I_Index] =  nanvalue;
                    grid[i * sx + j] = nanvalue;
        }
            } else  {
            DP_Output_Array3[I_Index] = computescale(Input_Array, l);
                grid[i * sx + j] = DP_Output_Array3[I_Index];
            }


            /* Super-verbose diagnostic output. */

            if (i==1 && j==1) {
               if (BKEP_Stat->I_SuperVerbose)
               {
                  printf("%d %d %d\t", k, i, j);
                  printf("0x%x\t", BKEP_Stat->I_status);

                  if (DP_Data_Image1[I_Index] != 0 &&
                      iznanorinfd(DP_Data_Image1[I_Index])) {
                    printf("%s\t", "NaN");
          } else {
                     printf("%f\t", DP_Data_Image1[I_Index]);
          }
                  if (DP_Output_Array3[I_Index] != 0 &&
                      iznanorinfd(DP_Output_Array3[I_Index]))
                     printf("%s\n", "NaN");
                  else
                     printf("%f\n", DP_Output_Array3[I_Index]);
           }
            }
         }


         /* Bilinear interpolation. */

         for (ii = 0; ii < I_Length_X; ii++)
         for (jj = 0; jj < I_Length_Y; jj++) {

         int xpt = jj;
             int ypt = ii;

             int jp1 = 0;
             for ( j = 0; j < sx; j++)
             {
                 if ( gx[j] >= xpt )
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
             for ( i = 0; i < sy; i++)
             {
                 if ( gy[i] >= ypt )
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
             int x = gx[j];
             int xp1 = gx[jp1];
             int y = gy[i];
             int yp1 = gy[ip1];

             double mapij = grid[i * sx + j];
             double mapip1j = grid[ip1 * sx + j];
             double mapijp1 = grid[i * sx + jp1];
             double mapip1jp1 = grid[ip1 * sx + jp1];

             double t = (double) (xpt - x) / (double) (xp1 - x);
             double u = (double) (ypt - y) / (double) (yp1 - y);
             double localScaleValue = (1 - t) * (1 - u) * mapij +
                                       t * (1 - u) * mapijp1 +
                                           t * u * mapip1jp1 +
                                           (1 - t) * u * mapip1j;

             I_Index = k * I_Length_X * I_Length_Y + ii * I_Length_Y + jj;
         DP_Output_Array3[I_Index] = localScaleValue;
     }

         free(grid);
         kkk++;
      }

   }


    /* Compute clippedmean-background-subtracted image, using either local clippedmean,
       or global clippedmean if local clippedmean is unavailable. */


    if (I_Fits_Type == 2 || I_Fits_Type == 3) {

       if (BKEP_Stat->I_Verbose)
          printf("Computing background-subtracted image...\n");

       kkk = 0;

       for (k = kstart; k < kend; k++) {
          for (i=0;i<I_Length_X;i++)
          for (j=0;j<I_Length_Y;j++) {

             I_Index_Inp = k*I_Length_X*I_Length_Y+i*I_Length_Y+j;
             I_Index = kkk*I_Length_X*I_Length_Y+i*I_Length_Y+j;

             if (! (DP_Data_Image1[I_Index_Inp] != 0 &&
             iznanorinfd(DP_Data_Image1[I_Index_Inp]))) {

                if (I_Operation==1 || I_Operation==3) {

                   if (DP_Output_Array[I_Index] != 0 &&
                      iznanorinfd(DP_Output_Array[I_Index])) {

                      // Lots of NaNs are expected in data, so do not set this status.
                      //BKEP_Stat->I_status = COMPUTE_RES|NOT_A_NUMBER;
                      DP_Output_Array2[I_Index] = nanvalue;

                   } else {

                      DP_Output_Array2[I_Index] =
                         DP_Data_Image1[I_Index_Inp] - DP_Output_Array[I_Index];

                   }

                } else {

                   DP_Output_Array2[I_Index] =
                      DP_Data_Image1[I_Index_Inp] - GlobalClippedMeanValue[kkk];

                }

             } else {

                // Lots of NaNs are expected in data, so do not set this status.
                //BKEP_Stat->I_status = COMPUTE_RES|NOT_A_NUMBER;
                DP_Output_Array2[I_Index] = nanvalue;

             }

          }

          kkk++;
       }

    }


    /* Optional debug output. */

    if (BKEP_Stat->I_Debug)
    {
       fp_data = fopen("bkgest_data.dump", "w");
       for(k=0;k<I_Num_Frames;k++)
       for(i=0;i<I_Length_X;i++)
       for(j=0;j<I_Length_Y;j++) {
          I_Index = k*I_Length_X*I_Length_Y+i*I_Length_Y+j;
          fprintf(fp_data, "%f\n", DP_Output_Array3[I_Index]);
       }
       fclose(fp_data);
    }

   if (BKEP_Stat->I_Verbose) {
      if (I_Operation == 1 || I_Operation == 3) {
         printf("bkgest_compute_results: ");
         printf("Local-clippedmean-image calculation completed.\n");
      }
      if (I_Operation == 2 || I_Operation == 3) {
         printf("bkgest_compute_results: ");
         printf("Global clippedmean-value calculation completed.\n");
      }
   }


end_compute_results:

   free(Input_Array);
   return;
}

int nint(double value) {
  int final;
  if (value >= 0.0) {
    final = (int) (value + 0.5);
  } else {
    final = (int) (value - 0.5);
  }
  return final;
}

