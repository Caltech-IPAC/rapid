"""
Reformat rimtimsim FITS files:
1. Remove CDELT1 and CDELT2 keywords
2. Change PC1_1 to CD1_1, ets.
3. Put image data in second HDU with copy of primary header
4. Add MJD-OBS from TSTART converted to MJD
5. Copy EXPOSURE to EXPTIME
6. Rename FITS file from
rimtimsim/rimtimsim_WFI_lvl02_F087_SCA02_field03_rampfitted_exposureno_000017675_sim.fits
to
rimtimsim_lite/rimtimsim_WFI_F087_SCA02_000017675_lite.fits
7. Discard uncertainty HDU
"""

from astropy.io import fits
import glob
import numpy as np

input_fits_files = glob.glob("rimtimsim/rim*.fits")

for input_fits_file in input_fits_files:

    output_fits_file = input_fits_file.replace("_lvl02","").replace("rimtimsim/","rimtimsim_lite/")\
        .replace("_field03_rampfitted_exposureno","").replace("sim.fits","lite.fits")

    hdul = fits.open(input_fits_file)
    hdr = hdul[0].header
    data = hdul[0].data

    hdr.remove('CDELT1', remove_all=True)
    hdr.remove('CDELT2', remove_all=True)

    obsjd = hdr["TSTART"]
    obsmjd = obsjd - 2400000.5
    hdr["MJD-OBS"] = obsmjd

    hdr.rename_keyword('PC1_1', 'CD1_1', force=False)
    hdr.rename_keyword('PC1_2', 'CD1_2', force=False)
    hdr.rename_keyword('PC2_1', 'CD2_1', force=False)
    hdr.rename_keyword('PC2_2', 'CD2_2', force=False)

    exptime = hdr["EXPOSURE"]
    hdr["EXPTIME"] = exptime

    print("input_fits_file =",input_fits_file)
    print("output_fits_file =",output_fits_file)

    # Replace primary HDU with empty image data
    hdul[0] = fits.PrimaryHDU(header=hdr,data=None)

    # Discard uncertainty-image HDU.
    del hdul[1]

    # Create a new ImageHDU with image data
    np_data = np.array(data)
    new_hdu = fits.ImageHDU(header=hdr,data=np_data.astype(np.float32))

    # Append the new HDU to the HDU list
    hdul.append(new_hdu)

    # Write output FITS file.
    hdul.writeto(output_fits_file,overwrite=True,checksum=True)


