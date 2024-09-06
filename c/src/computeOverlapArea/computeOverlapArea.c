/*******************************************************************************
     computeOverlapArea.c     9/6/24      Russ Laher
                                          laher@ipac.caltech.edu
                                          California Institute of Technology
                                          (c) 2024, All Rights Reserved.

Compute overlap area of intersecting subject and clipping polygons.

Uses GPC, v2.32 (gpc.c and gpc.h).

GPC Licensing: Developers may use GPC for any purpose without paid licensing
restrictions (https://en.wikipedia.org/wiki/General_Polygon_Clipper).
*******************************************************************************/

#include <stdio.h>
#include <stdlib.h>
#include <float.h>
#include <math.h>
#include "gpc.h"
#include "computeOverlapArea.h"

int debug = 0;

int main(void)
{
    gpc_polygon subj, clip, result;
    FILE *sfp, *cfp, *ofp;

    sfp= fopen("subjfile", "r");
    cfp= fopen("clipfile", "r");
    gpc_read_polygon(sfp, 1, &subj);
    gpc_read_polygon(cfp, 1, &clip);

    gpc_polygon_clip(GPC_INT, &subj, &clip, &result);

    ofp= fopen("outfile", "w");
    gpc_write_polygon(ofp, 1, &result);


    /* Compute area of overlap polygon. */

    if (debug == 1) {
        double a_subj,a_clip;
        compute_polygon_area(&subj, &a_subj);
        compute_polygon_area(&clip, &a_clip);
        printf("area of subject polygon = %f\n",a_subj);
        printf("area of clipping polygon = %f\n",a_clip);
    }

    double a_result;
    compute_polygon_area(&result, &a_result);

    printf("area of intersecting polygon = %f\n",a_result);

    gpc_free_polygon(&subj);
    gpc_free_polygon(&clip);
    gpc_free_polygon(&result);

    fclose(sfp);
    fclose(cfp);
    fclose(ofp);

    exit(0);
}


/* Shoelace formula applied to a polygon with multiple contours, which may be holes. */

void compute_polygon_area(gpc_polygon *p, double *area)
{
    /* Test for trivial NULL result case. */
    if (p->num_contours == 0)
    {
        *area = -999999;
        return;
    }

    if (debug == 1) {
        printf("DBL_DIG = %d\n", DBL_DIG);
    }

    *area = 0.0;

    if (debug == 1) {
        printf("p->num_contours = %d\n", p->num_contours);
    }

    for (int c = 0; c < p->num_contours; c++)
    {

        if (debug == 1) {
            printf("c = %d\n", c);
            printf("p->contour[c].num_vertices = %d\n", p->contour[c].num_vertices);
            printf("p->hole[c] = %d\n", p->hole[c]);
        }

        double area_contour = 0.0;

        for (int v = 0; v < p->contour[c].num_vertices; v++)
        {
            if (debug == 1) {
                printf("x y = % .*lf % .*lf\n",
                        DBL_DIG, p->contour[c].vertex[v].x,
                        DBL_DIG, p->contour[c].vertex[v].y);
                }

            /* Connect last vertex to first. */

            int n;

            if (v == p->contour[c].num_vertices - 1)
            {
                n = 0;
            }
            else
            {
                n = v + 1;
            }

            area_contour += (p->contour[c].vertex[v].y + p->contour[c].vertex[n].y) * (p->contour[c].vertex[v].x - p->contour[c].vertex[n].x);
        }

        /* If polygon is negatively oriented, the area will be negative, so take the absolute value. */

        area_contour = fabs(area_contour);

        if  (p->hole[c] == 0)
        {
            *area += area_contour;
        }
        else
        {
            *area -= area_contour;
        }
    }

    *area *= 0.5;

    return;
}





