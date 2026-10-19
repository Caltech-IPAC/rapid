"""
Empirical ePSF library model for romanisim L2 injection.

An alternative to the CRDS ePSF reference until CRDS carries well-measured
ePSFs for real, best-focus, well-guided data.  The library holds one
oversampled ePSF per SCA and filter (photutils convention: the stamp sums to
OVERSAMP**2, so sampling it at native-pixel spacing gives the fraction of the
flux in each pixel).  It is measured in detector pixels, has no variation
across the SCA, and already includes the pixel response.

LibraryEPSF.at_position returns a galsim profile that, drawn the way
romanisim's add_objects_to_image draws it (default method, which convolves
with the pixel), reproduces the ePSF pixel for pixel: the stamp carries the
full local Jacobian of the image WCS divided by the oversampling (scale,
rotation, parity and skew), and the pixel response that drawImage adds is
cancelled by a pixel deconvolution.
"""

import galsim
import numpy as np
from astropy.io import fits


def resolve_epsf_library_path(template,filter_name,sca):

    '''
    Substitute FILTER (lower case, e.g. f146) and SCAID (zero-padded) in the
    library filename template, a local path or an s3:// URI.
    '''

    return template.replace("FILTER",filter_name.lower()).replace("SCAID","{:02d}".format(sca))


def read_epsf_library_file(path):

    '''
    Read an oversampled library ePSF from a local path or an s3:// URI.
    Return the stamp, normalized to unit sum, and its oversampling factor.
    '''

    if path.startswith('s3://'):
        import s3fs
        with s3fs.S3FileSystem().open(path,'rb') as f:
            with fits.open(f) as hdul:
                epsf = hdul[0].data.astype(float)
                oversample = int(hdul[0].header['OVERSAMP'])
    else:
        with fits.open(path) as hdul:
            epsf = hdul[0].data.astype(float)
            oversample = int(hdul[0].header['OVERSAMP'])

    return epsf / epsf.sum(),oversample


class LibraryEPSF:
    """Constant-across-the-SCA empirical ePSF with the at_position(x, y)
    interface that romanisim's add_objects_to_image expects."""

    def __init__(self, epsf, oversample, wcs):
        self.epsf = epsf
        self.oversample = oversample
        self.wcs = wcs
        # Keeps the fastpointsources path disabled in add_objects_to_image
        self.psfinterpolators = None

    def at_position(self, x, y):
        local_wcs = self.wcs.local(image_pos=galsim.PositionD(x, y))
        stamp_wcs = galsim.JacobianWCS(*(local_wcs.getMatrix().ravel() / self.oversample))
        epsf = galsim.InterpolatedImage(galsim.Image(self.epsf, wcs=stamp_wcs),
                                        normalization='flux', use_true_center=True)
        pixel = local_wcs.toWorld(galsim.Pixel(1.0))
        return galsim.Convolve(epsf, galsim.Deconvolve(pixel))
