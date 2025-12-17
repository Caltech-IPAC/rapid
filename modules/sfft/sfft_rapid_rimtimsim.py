#! /usr/bin/env python

import os
import numpy as np

from astropy.io import fits, ascii
from astropy.stats import sigma_clipped_stats
from astropy.convolution import convolve_fft

from sfft.utils.pyAstroMatic.PYSEx import PY_SEx
from sfft.CustomizedPacket import Customized_Packet
from sfft.utils.SFFTSolutionReader import Realize_MatchingKernel
from sfft.utils.DeCorrelationCalculator import DeCorrelation_Calculator

#SFFT kernel-fitting settings, should include these in a config file eventually
ForceConv = 'REF' 
GKerHW = 9
KerPolyOrder=0  #polynomial order for kernel spatial variation, SN PIT currently using KerPolyOrder=2
BGPolyOrder=0   #polynomial order for background spatial variation
ConstPhotRatio=True  #use constant photometric ratio for kernel fitting
#backend for SFFT
backend = 'Numpy'  #use Numpy or CUDA for SFFT
cudadevice = '0'  #CUDA device to use for SFFT
nCPUthreads = 8  #number of CPU threads to use for SFFT
verbose = 0  #verbosity level for SFFT (0=quiet, 1=normal, 2=full)

#sfft functions
def bkg_mask(image, use_segm=True, run_sextractor=True, segm_image=None, bsmask=True, bsmask_cat=None, sat_value=1750.0, satmask_radius1=30.0, satmask_radius2=45.0, npix_seg2=4000.0,  bsmask_value=50.0, bsmask_radius=100.0):
    """
    Create detection mask for sfft to mask background/noise pixels. By default this runs SExtractor using the wrapper within sfft to generate a segmentation image. 
    To DO: could implement input Sextractor parameters.

    Parameters
    ----------
    image (str) : Path to input image file.
    use_segm (bool) : If True, use segmentation image to define background pixels. Otherwise, background pixels are not masked. 
    run_sextractor (bool) : If True, run SExtractor to generate segmentation image. If False, use provided segmentation image.
    segm_image (str) : Path to segmentation image file. Generated on-the-fly if run_sextractor is True. 
    bsmask (bool) : If True, mask bright stars in the image.
    bsmask_cat (str) : Path to S-Extractor catalog for bright star masking. If None, mask bright stars based on pixel values (slow).
    sat_value (float) : Saturation value above which sources are masked. 
    sat_mask_radius1 (float) : Minumum radius around saturated sources to mask.
    sat_mask_radius2 (float) : Larger radius around severely saturated sources to mask.
    npix_seg2 (float) : Area in pixels in segmentation image corresponding to sat_maskradius2, used for scaling.
    bsmask_value (float) : Value above which pixels are considered bright stars.
    bsmask_radius (float) : Radius around bright stars to mask.
    
    Returns
    -------
    bkg_mask (ndarray) : Background mask (and bright/saturated star mask) for image. 1 for masked pixels, 0 for unmasked pixels (i.e., those used in sfft kernel fitting).
    """

    with fits.open(image) as hdu:
        hdudata = hdu[0].data
    #indices for masking
    Y, X = np.indices(hdudata.shape)

    if use_segm:
        if run_sextractor:
            source_ext_params = ['X_IMAGE', 'Y_IMAGE', 'FLUX_AUTO', 'FLUXERR_AUTO', 'MAG_AUTO', 'MAGERR_AUTO', 'FLAGS', \
            'FLUX_RADIUS', 'FWHM_IMAGE', 'A_IMAGE', 'B_IMAGE', 'KRON_RADIUS', 'THETA_IMAGE', 'SNR_WIN', 'ISOAREA_IMAGE', 'FLUX_MAX']

            scatalog = PY_SEx.PS(FITS_obj=image, SExParam=source_ext_params, GAIN_KEY='GAIN', SATUR_KEY='SATURATE', \
                                BACK_TYPE='MANUAL', BACK_VALUE=0.0, BACK_SIZE=64, BACK_FILTERSIZE=3, DETECT_THRESH=1.5, \
                                DETECT_MINAREA=5, DETECT_MAXAREA=0, DEBLEND_MINCONT=0.001, BACKPHOTO_TYPE='LOCAL', \
                                CHECKIMAGE_TYPE='SEGMENTATION', AddRD=True, ONLY_FLAGS=None, XBoundary=0.0, YBoundary=0.0, \
                                MDIR=None, VERBOSE_LEVEL=1)[1][0]
            segm_imdata = scatalog.T
            #write out the segmentation image, if name provide, but don't overwrite
            if (segm_image is not None) and (os.path.exists(segm_image) == False):
                with fits.open(image) as hdu:
                    hdu[0].data = segm_imdata
                    hdu.writeto(segm_image)
            else:
                segm_imdata = fits.getdata(segm_image)
        else:
            segm_image = np.zeros()

        bkg_mask = (segm_imdata == 0)
    else:
        bkg_mask = np.logical_not(np.isfinite(fits.getdata(image, ext=0)))

    #bright star masking, include separate method for saturated sources 
    if bsmask:
        hdudata[bkg_mask] = 0.0

        if bsmask_cat is not None:
            bscat = ascii.read(bsmask_cat)
            
            #first deal with saturated sources
            sat_sources = bscat[bscat['FLUX_MAX'] >= sat_value]
            for source in sat_sources:
                #scale based on "size" of source measured by sextrator 
                source_mask_radius = satmask_radius1 + (satmask_radius2 - satmask_radius1)/(np.sqrt(npix_seg2) - 1) * (np.sqrt(source['ISOAREA_IMAGE']) - 1)
                #mask all pixels within radius
                pix_dist =  np.sqrt((X - source['XWIN_IMAGE'] - 1)**2 + (Y - source['YWIN_IMAGE'] - 1)**2)
                sat_mask = pix_dist <= source_mask_radius
                bkg_mask[sat_mask] = 1
            
            #now mask addtional bright sources below saturation
            bright_sources = bscat[(bscat['FLUX_MAX'] < sat_value) & (bscat['FLUX_MAX'] >= bsmask_value)]
            for source in bright_sources:
                pix_dist = np.sqrt((X - source['XWIN_IMAGE'] - 1)**2 + (Y - source['YWIN_IMAGE'] - 1)**2)
                bs_mask = pix_dist <= bsmask_radius
                bkg_mask[bs_mask] = 1

        else:
            #just mask bright stars based on pixel values (slow, no grouping)
            h, w = np.shape(hdudata)[0], np.shape(hdudata)[1]
            Y, X = np.ogrid[:h, :w]
            bright_star_pix = np.where(hdudata > bsmask_value)
            for bs_y, bs_x in zip(bright_star_pix[0], bright_star_pix[1]):
                pix_dist =  np.sqrt((X - bs_x)**2 + (Y-bs_y)**2)
                bs_mask = pix_dist <= bsmask_radius
                bkg_mask[bs_mask] = 1

    return bkg_mask

def run_sfft_rapid(sciim, refim, mask_image, crossconv=False, scipsf=None,  refpsf=None, scibkgsig=None, refbkgsig=None, \
                   ForceConv='REF', GKerHW=9, KerPolyOrder=0, BGPolyOrder=0, \
                   ConstPhotRatio=True, backend='Numpy', cudadevice='0', nCPUthreads=8, outlabel='sfft', verbose=0):
    """
    Run sfft kernel fitting and subtraction on an input science and reference image, with optional psf-crossconvolution and noise decorrelation. 
    This function currently only works for the Numpy (CPU) backend of sfft.
    The input images are assumed to be astrometricaly aligned and registered. 
    The output is a difference image (with optional cross-convoled and decorrelated diff images) and the matching kernel solution.

    Parameters
    ----------
    sciim (str) : Path to input science image file. Input to SFFT.
    refim (str) : Path to input reference image file. Input to SFFT.
    mask_image (str) : Path to background pixel mask image. Input to SFFT. Defines the pixels in the image used for kernel fitting.
    crossconv (bool) : If True, use PSF cross-convolution prior to subtraction. Default is False.
    scipsf (str) : Path to PSF image for science image. Required if cross-convolution is used.
    refpsf (str) : Path to PSF image for reference image. Required if cross-convolution is used.
    scibkgsig (float) : Background noise estimate for science image. Required if cross-convolution is used.
    refbkgsig (float) : Background noise estimate for reference image. Required if cross-convolution is used.
    ForceConv (str) : Which image to convovle. Default is 'REF'. Options are 'REF' or 'SCI'.
    GKerHW (int) : Half-width of the kernel. Default is 9.
    KerPolyOrder (int) : Polynomial order for kernel spatial variation. Default is 0.
    BGPolyOrder (int) : Polynomial order for background spatial variation. Default is 0.
    ConstPhotRatio (bool) : Use constant photometric ratio for kernel fitting. Default is True.
    backend (str) : Backend for SFFT. Default is 'Numpy'. Options are 'Numpy' or 'CUDA'.    
    cudadevice (str) : CUDA device to use for SFFT. Default is '0'.
    nCPUthreads (int) : Number of CPU threads to use for SFFT. Default is 8.
    outlabel (str) : Label for output file names. Default is 'sfft'.
    verbose (int) : Verbosity level for SFFT. Default is 0. Options are 0 (quiet), 1 (normal), or 2 (full).

    Returns
    -------
    diff (str) : Path to difference image file.
    dcdiff (str) : Path to decorrelated difference image file. Only returned if crossconv is True.
    soln (str) : Path to matching-kernel solution file.
    """

    #read in the image data
    #Lei's sfft code and examples takes the transpose of the data, so we do that too to avoid confusion 
    scidata = fits.getdata(sciim).T
    refdata = fits.getdata(refim).T

    #read in the background pixel mask for sfft
    bkgmask = (fits.getdata(mask_image).T).astype(np.bool_)

    #do an intial PSF cross-convolution 
    if crossconv:
        #psf1 and psf2 requried in this case
        scipsfdata = fits.getdata(scipsf).T
        refpsfdata = fits.getdata(refpsf).T

        #do the cross-convolution
        scidata_convd = convolve_fft(scidata, refpsfdata, boundary='fill', \
                                     nan_treatment='fill', fill_value=0.0, normalize_kernel=True)
        refdata_convd = convolve_fft(refdata, scipsfdata, boundary='fill', \
                                     nan_treatment='fill', fill_value=0.0, normalize_kernel=True)

        #use the cross-convolved images as inputs to sfft
        insciim = sciim.replace('.fits','_cconv.fits')
        inrefim = refim.replace('.fits','_cconv.fits')
        
        #write out cross-convolved images
        with fits.open(sciim) as scihdu:
            scihdu[0].data = scidata_convd.T
            scihdu.writeto(insciim, overwrite=True)
        with fits.open(refim) as refhdu:
            refhdu[0].data = refdata_convd.T
            refhdu.writeto(inrefim, overwrite=True)
        
        #work on convolved data
        scidata = scidata_convd
        refdata = refdata_convd

        #setup the names of the outputs
        diff = os.path.join(os.path.dirname(sciim), f"{outlabel}diffimage_cconv_masked.fits")
        soln = os.path.join(os.path.dirname(sciim), f"{outlabel}soln_cconv.fits")
        
    else:
        insciim = sciim  #input images to sfft are just the original images
        inrefim = refim
        
        #setup the names of the outputs
        diff = os.path.join(os.path.dirname(sciim), f"{outlabel}diffimage_masked.fits")
        soln = os.path.join(os.path.dirname(sciim), f"{outlabel}soln.fits")

    sciimagebase = insciim.split('.fits')[0]
    refimagebase = inrefim.split('.fits')[0]

    #bkg masked images for kernel fitting.
    sci_masked = f"{sciimagebase}_bkgmasked.fits"
    ref_masked = f"{refimagebase}_bkgmasked.fits"

    #make the masked images
    for image,masked in zip([insciim,inrefim], [sci_masked,ref_masked]):
        with fits.open(image) as hdu:
            hdudata = hdu[0].data.T
            hdudata[bkgmask] = 0.0
            hdu[0].data = hdudata.T
            hdu.writeto(masked, overwrite=True)

    #run sfft
    Customized_Packet.CP(FITS_REF=inrefim, FITS_SCI=insciim, FITS_mREF=ref_masked, FITS_mSCI=sci_masked, \
                        ForceConv=ForceConv, GKerHW=GKerHW, FITS_DIFF=diff, FITS_Solution=soln, \
                        KerPolyOrder=KerPolyOrder, BGPolyOrder=BGPolyOrder, ConstPhotRatio=ConstPhotRatio, \
                        BACKEND_4SUBTRACT=backend, CUDA_DEVICE_4SUBTRACT=cudadevice, \
                        NUM_CPU_THREADS_4SUBTRACT=nCPUthreads, VERBOSE_LEVEL=verbose)

    #if cross-convolution is used, we need to do a decorrelation step
    if crossconv:
        N0, N1 = scidata.shape
        #get the matching kernel solution for the center of the image to derive decorrelation kernel. 
        #could implement the decorrelation on image subsections if spatially varying kernels are used. 
        XY_q = np.array([[N0/2. + 0.5, N1/2. + 0.5]])
        MKerStack = Realize_MatchingKernel(XY_q).FromFITS(FITS_Solution=soln)
        MK_Fin = MKerStack[0]
        
        #calculate the decorrelation kernel, based on the input psfs and background noise
        #MK_JLst: kernel for the sci image, i.e. the ref psf 
        #SkySig_JLst: background noise for the sci image
        #MK_ILst: kernel for the ref image, i.e. the sci psf
        #SkySig_ILst: background noise for the ref image
        #MK_Fin: the final kernel solution from sfft
        DCKer = DeCorrelation_Calculator.DCC(MK_JLst=[refpsfdata], SkySig_JLst=[scibkgsig], \
                                             MK_ILst=[scipsfdata], SkySig_ILst=[refbkgsig], MK_Fin=MK_Fin, \
                                             KERatio=2.0, VERBOSE_LEVEL=verbose)
        
        #run the decorrelation on the difference image
        diffdata = fits.getdata(diff, ext=0).T
        dcdiffdata = convolve_fft(diffdata, DCKer, boundary='fill', \
                                  nan_treatment='fill', fill_value=0.0, normalize_kernel=True,
                                  preserve_nan=True)
        
        #write out the decorrelated difference image
        dcdiff = diff.replace("cconv", "dconv")
        with fits.open(diff) as diffhdu:
            diffhdu[0].data = dcdiffdata.T
            diffhdu.writeto(dcdiff, overwrite=True)
        
        return diff, dcdiff, soln
    
    else:
        return diff, soln
    
if __name__ == "__main__":
    #example usage with command line arguments
    import argparse 

    parser = argparse.ArgumentParser(description="""
		Run SFFT subtraction using RAPID pipeline products.
        """)
        
    #Example recommended usage:                                    
    #    For rimtimsim: no background masking, mask saturated stars based on SExtractor catalogs, no psf cross-convolution \n
    #    >>> python sfft_rapid_rimtimsim.py bkg_subbed_science_image.fits awaicgen_output_mosaic_image_resampled_gainmatched.fits  --scicat bkg_subbed_science_image_scigainmatchsexcat.txt --refcat awaicgen_output_mosaic_image_resampled_refgainmatchsexcat.txt --satvalue 1750. --satmaskradius 30,45 --npixseg2 4000.
    #
    #    For OpenUniverse: background masking with segmentation images, psf cross-convolution, mask bright stars based on SExtractor catalogs 
    #    >>> python sfft_rapid_rimtimsim.py bkg_subbed_science_image.fits awaicgen_output_mosaic_image_resampled_gainmatched.fits --scisegm sfftscisegm.fits --refsegm sfftrefsegm.fits --scicat bkg_subbed_science_image_scigainmatchsexcat.txt --refcat awaicgen_output_mosaic_image_resampled_refgainmatchsexcat.txt --crossconv --scipsf WFI_SCA08_F158_PSF_DET_DIST_normalized.fits --refpsf refimage_psf_fid2.fits --bsmaskvalue 50. --bsmaskradius 100.
    #                             
    #Example usage for OpenUniverse to match 20250927 run:
    #    Background masking with segmentation images, psf cross-convolution, mask bright stars based on image pixel values
    #    >>> python sfft_rapid_rimtimsim.py bkg_subbed_science_image.fits awaicgen_output_mosaic_image_resampled_gainmatched.fits --scisegm sfftscisegm.fits --refsegm sfftrefsegm.fits --crossconv --scipsf WFI_SCA08_F158_PSF_DET_DIST_normalized.fits --refpsf refimage_psf_fid2.fits --bsmaskvalue 50. --bsmaskradius 100.
    #
    
    parser.add_argument('scifile', help='path to input science image file. Input to SFFT.')
    parser.add_argument('reffile', help='path to input reference image file. Input to SFFT.')
    parser.add_argument('--scisegm', help='path to segmentation or detection image for science image. Generated on-the-fly if named file does not exist.')
    parser.add_argument('--refsegm', help='path to segmentation or detection image for reference image. Generated on-the-fly if named file does not exist.')
    parser.add_argument('--scicat', help='path to source catalog for science image for bright source masking', default=None)
    parser.add_argument('--refcat', help='path to source catalog for reference image for bright source masking', default=None)
    parser.add_argument('--refcovmap', help='reference coverage map for masking output', default=None)
    parser.add_argument('--crossconv', help='use PSF cross-convolution', action='store_true')
    parser.add_argument('--scipsf', help='path to PSF image for science image. Required if cross-convolution is used.', default=None)
    parser.add_argument('--refpsf', help='path to PSF image for reference image. Required if cross-convolution is used.', default=None)
    parser.add_argument('--satvalue', help='saturation value for images, used to mask saturated pixels', type=float, default=1e6)
    parser.add_argument('--satmaskradius', help='saturation mask radius (in pixels) or two values for scaling based on source size, e.g. 30,45', type=str, default='0')
    parser.add_argument('--npixseg2', help='area in pixels in segmentation image corresponding to larger satmask radius for scaling', type=float, default=4000.)
    parser.add_argument('--bsmaskvalue', help='bright source mask value for images, used to mask bright source pixels', type=float, default=1e6)
    parser.add_argument('--bsmaskradius', help='bright source mask radius (in pixels)', type=float, default=0.)

    args = parser.parse_args()
    sciim = args.scifile
    refim = args.reffile
    scisegm = args.scisegm
    refsegm = args.refsegm
    scicat = args.scicat
    refcat = args.refcat
    refcovmap = args.refcovmap
    crossconv = args.crossconv
    scipsf = args.scipsf
    refpsf = args.refpsf
    sat_value = args.satvalue
    satmask_radius = tuple(map(int, args.satmaskradius.split(',')))
    npix_seg2 = args.npixseg2
    bsmask_value = args.bsmaskvalue
    bsmask_radius = args.bsmaskradius

    if len(satmask_radius) == 1:
        satmask_radius1 = satmask_radius[0]
        satmask_radius2 = satmask_radius[0]
    elif len(satmask_radius) == 2:
        satmask_radius1 = satmask_radius[0]
        satmask_radius2 = satmask_radius[1]


    #logic for masking background pixels with segmentation images
    use_scisegm = scisegm is not None
    run_scisextractor = use_scisegm and not os.path.isfile(scisegm)
    
    sci_bkgmask = bkg_mask(sciim, use_segm=use_scisegm, run_sextractor=run_scisextractor, segm_image=scisegm, bsmask=True, bsmask_cat=scicat, 
                           sat_value=sat_value, satmask_radius1=satmask_radius1, satmask_radius2=satmask_radius2, npix_seg2=npix_seg2, 
                           bsmask_value=bsmask_value, bsmask_radius=bsmask_radius)

    use_refsegm = refsegm is not None
    run_refsextractor = use_refsegm and not os.path.isfile(refsegm)
    
    ref_bkgmask = bkg_mask(refim, use_segm=use_refsegm, run_sextractor=run_refsextractor, segm_image=refsegm, bsmask=True, bsmask_cat=refcat, 
                           sat_value=sat_value, satmask_radius1=satmask_radius1, satmask_radius2=satmask_radius2, npix_seg2=npix_seg2, 
                           bsmask_value=bsmask_value, bsmask_radius=bsmask_radius)
    
    if (refcovmap is not None) and os.path.isfile(refcovmap):
        covmask = fits.getdata(refcovmap) == 0
    else:
        #otherwise use 0 in ref image
        covmask = fits.getdata(refim) == 0

    #read in the image data to make the masks and get the background noise estimates if needed
    scidata = fits.getdata(sciim)
    refdata = fits.getdata(refim)

    #make the combined mask
    _nanmask = np.isnan(scidata) | np.isnan(refdata) 
    _bkgmask = np.logical_or(sci_bkgmask,ref_bkgmask) #only include sources in both images
    bkgmask = np.logical_or(_nanmask, _bkgmask) #nans should be zeros for sfft masks
    bkgmaskim = sciim.replace('.fits','_bkgmask.fits')
    with fits.open(sciim) as hdu:
        hdu[0].data = bkgmask.astype(np.int16)
        hdu.writeto(bkgmaskim, overwrite=True)

    #sigma clipped background std. dev. for background pixels
    if crossconv:
        #sigma clipped background std. dev. for background pixels
        scibkgsig = sigma_clipped_stats(scidata[bkgmask].flatten(), sigma=5.0)[2]
        refbkgsig = sigma_clipped_stats(refdata[bkgmask].flatten(), sigma=5.0)[2]

        #run sfft
        diff, dcdiff, soln = run_sfft_rapid(sciim, refim, mask_image=bkgmaskim, crossconv=crossconv, scipsf=scipsf, refpsf=refpsf, \
                                            scibkgsig=scibkgsig, refbkgsig=refbkgsig, ForceConv=ForceConv, GKerHW=GKerHW, \
                                            KerPolyOrder=KerPolyOrder, BGPolyOrder=BGPolyOrder, ConstPhotRatio=ConstPhotRatio, \
                                            backend=backend, cudadevice=cudadevice, nCPUthreads=nCPUthreads)
        
        #mask 0 coverage pixels if dcdiff image
        with fits.open(dcdiff) as hdu:
            hdudata = hdu[0].data
            hdudata[covmask] = np.nan
            hdu[0].data = hdudata
            hdu.writeto(dcdiff, overwrite=True)

    else:
        diff, soln = run_sfft_rapid(sciim, refim, mask_image=bkgmaskim, \
                                    ForceConv=ForceConv, GKerHW=GKerHW, \
                                    KerPolyOrder=KerPolyOrder, BGPolyOrder=BGPolyOrder, ConstPhotRatio=ConstPhotRatio, \
                                    backend=backend, cudadevice=cudadevice, nCPUthreads=nCPUthreads)
        
    #mask 0 coverage pixels if diff image
    with fits.open(diff) as hdu:
        hdudata = hdu[0].data
        hdudata[covmask] = np.nan
        hdu[0].data = hdudata
        hdu.writeto(diff, overwrite=True)
        