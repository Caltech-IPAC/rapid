RAPID Pipeline Design
####################################################

The RAPID pipeline is written in Python, with some system calls to OS commands and C executable binaries.

.. warning::
    The pipeline design described below is evolving and subject to change.

Reference Images
*************************************

Reference images are needed for image differencing.
The reference images will be constructed for sky tiles given by the adopted NSIDE=10 Roman tesselation.
In this gridding scheme, the sky is divided into 2402 tiles that are, in general,
approximately square and with similar area.
The tiles are basically aligned in rows within declination bins, with the most right-ascension
bins at the equator and progressively fewer as
the poles are approached.  There is one circular tile capping each pole.
For NSIDE=10, the tile size is approximately 4 degrees on a side.

The sky tiles are much larger than the Roman WFI focal plane, and, therefore, the reference images are
not constructed to have any overlap regions outside of the sky tile.  Since a given Roman exposure may overlap
a tile boundary, difference-imaging for such an exposure will involve pertinent adjacent reference images.
