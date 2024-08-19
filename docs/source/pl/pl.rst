RAPID Pipeline Design
####################################################

Introduction
************************************
Below describes the current design of the RAPID pipeline and its rationale.

.. warning::
    The pipeline design described below is evolving and subject to change.


Computer Languages
************************************

The RAPID pipeline is written in Python, with some system calls to OS commands and C executable binaries.


Reference Images
************************************

Reference images are needed for image differencing.  The maximum-tolerable number of pixels in a reference image
constrains the footprint of a reference image on the sky.  As will be seen below, it will be necessary to adopt
a sub-tile scheme within the Roman tessellation.

The reference images will be constructed for sky tiles given by the NSIDE=10 Roman tessellation.
In this gridding scheme, the sky is divided into 2402 tiles that are, in general,
approximately square and with similar area.
The tiles are basically aligned in rows within declination bins, with the most right-ascension
bins at the equator and progressively fewer as
the poles are approached (see table below).  There is one circular tile capping each pole.
For NSIDE=10, the tile size near the equator is approximately 3.8 degrees in declination height
and 4.5 degrees in right-ascension width.


Table: Number of right-ascension bins per declination bin for NSIDE=10.

==========   =====      ===========
center_dec   count      dec-bin num
==========   =====      ===========
-90.0        1          1
-85.32052    8          2
-80.63321    16         3
-75.93013    24         4
-71.203094   32         5
-66.443535   40         6
-61.642365   48         7
-56.789783   56         8
-51.875088   64         9
-46.886395   72         10
-41.810314   80         11
-36.869896   80         12
-32.230953   80         13
-27.81814    80         14
-23.578178   80         15
-19.47122    80         16
-15.46601    80         17
-11.536959   80         18
-7.662256    80         19
-3.8225536   80         20
0.0          80         21
3.8225536    80         22
7.662256     80         23
11.536959    80         24
15.46601     80         25
19.47122     80         26
23.578178    80         27
27.81814     80         28
32.230953    80         29
36.869896    80         30
41.810314    80         31
46.886395    72         32
51.875088    64         33
56.789783    56         34
61.642365    48         35
66.443535    40         36
71.203094    32         37
75.93013     24         38
80.63321     16         39
85.32052     8          40
90.0         1          41
==========   =====      ===========


The sky tiles are much larger than the Roman WFI focal plane (which is roughly 0.5 degrees by 1.2 degrees with gaps).
It is an open question at this point whether the reference images are constructed to have any buffer regions
outside of the sky tile (or smaller sub-tile).
Since a given Roman exposure may overlap a tile or sub-tile boundary, difference-imaging for such an exposure
will involve pertinent neighboring reference images.

Reference images will be constructed for different filters.  For a given filter, images from
different SCAs will be stacked to make reference images.

Sub-tile scheme or increase NSIDE?
