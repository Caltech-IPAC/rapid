/* The k-th smallest of data[0..n-1], k counted from 0, by quickselect
   (Hoare, 1961) with Hoare's partition: two indices close in from the
   ends of the live range, swapping pairs on the wrong side of a
   median-of-three pivot value, and only the side holding k is kept.
   The pivot is never the strict maximum of data[lo..hi-1] (one of
   data[lo] and data[mid] is at least the median of three), so the split
   point j satisfies lo <= j < hi and the range shrinks every pass.
   Reorders data; every caller passes a scratch copy. */

#include "numericalrecipes.h"

static double medianofthree(double a, double b, double c)
{
    if (a < b) {
        if (b < c) return b;
        return (a < c) ? c : a;
    }
    if (a < c) return a;
    return (b < c) ? c : b;
}

double selectkthvalue(int k, double data[], int n)
{
    int lo = 0, hi = n - 1;

    while (lo < hi) {
        double pivot = medianofthree(data[lo], data[lo + (hi - lo) / 2], data[hi]);
        int i = lo - 1, j = hi + 1;

        for (;;) {
            do i++; while (data[i] < pivot);
            do j--; while (data[j] > pivot);
            if (i >= j) break;
            swapvaluesinarray(data, i, j);
        }

        /* data[lo..j] <= pivot <= data[j+1..hi] */
        if (k <= j) hi = j;
        else lo = j + 1;
    }
    return data[k];
}
