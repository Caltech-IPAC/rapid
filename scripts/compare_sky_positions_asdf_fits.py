##################################################################################
# Compare distances between sky positions computed from ASDF versus FITS.
##################################################################################

import asdf
from astropy.wcs import WCS, Sip
from astropy.coordinates import SkyCoord
import astropy.units as u
from astropy.io import fits

import modules.utils.rapid_pipeline_subs as util

asdf_file = 'r0034001001001001001_0001_wfi06_f062_cal.asdf'
fits_file = 'r0034001001001001001_0001_wfi06_f062_cal_lite.fits'

def extract_gwcs(af):

    """Extract a gwcs.WCS object from an open ASDF file."""

    tree = af.tree
    for key in ("roman.meta.wcs",):
        parts = key.split(".")
        node = tree
        try:
            for p in parts:
                node = node[p]
            if hasattr(node, "pixel_to_world"):
                print(f"=>key,node = {key},{node}")
                return node
        except (KeyError, TypeError):
            continue
    # Walk top-level keys
    for v in tree.values():
        if hasattr(v, "pixel_to_world"):
            print(f"==>key,v = {key},{v}")
            return v
    raise ValueError(
        "No gwcs.WCS object found in ASDF tree. "
        "Inspect af.tree manually to locate the WCS key."
    )


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    print(f"asdf_file = {asdf_file}")
    print(f"fits_file = {fits_file}")

    af = asdf.open(asdf_file)

    roman_tree = af.tree['roman']


    # get exposure start time from the metadata.

    start_time = roman_tree['meta']['exposure']['start_time']

    print(f"=====>start_time = {start_time}")

    # load the data array
    data = roman_tree['data']

    data_shape = data.shape

    print(f"=====>data_shape = {data_shape}")


    # Extract gWCS object from ASDF file.

    gwcs_obj = extract_gwcs(af)


    # Print bounding box and available frames of reference.
    bb = gwcs_obj.bounding_box
    print(f"bb = {bb}")
    available_frames = gwcs_obj.available_frames
    print(f"available_frames = {available_frames}")


    # Pixel coordinates must be zero-based indices.

    xs = [0,4087,   4087,0,    2043.5]
    ys = [0,0,      4087,4087, 2043.5]


    # Size of a Roman WFI pixel on a side, in arcseconds.

    roman_wfi_pixel_arcseconds = 0.11

    ras_asdf = []
    decs_asdf = []


    # Compute sky positions from ASDF fWCS.

    for x,y in zip(xs,ys):

        # Transform pixel -> sky using gwcs
        sky = gwcs_obj.pixel_to_world(x, y)
        if isinstance(sky, SkyCoord):
            ra = sky.ra.deg
            dec = sky.dec.deg
            print(f"===asdf===>x,y,ra,dec = {x},{y},{ra},{dec}")
        else:
            # Some gwcs objects return (lon, lat) arrays directly
            ra, dec = np.asarray(sky[0]), np.asarray(sky[1])
            print(f"x,y,ra,dec = {x},{y},{ra},{dec}")

        ras_asdf.append(ra)
        decs_asdf.append(dec)


    # Compute sky positions from FITS header with SIP distortion.

    hdu_index = 1
    ras_sip,decs_sip = util.computeSkyCoordsFromPixelCoords(fits_file,xs,ys,hdu_index)


    # Compute distances between sky positions computed from ASDF versus FITS.

    for i in range(len(xs)):
        x = xs[i]
        y = ys[i]
        ra = ras_sip[i]
        dec = decs_sip[i]

        ra_asdf = ras_asdf[i]
        dec_asdf = decs_asdf[i]

        angsep = 3600.0 * util.compute_angular_separation(ra, dec, ra_asdf, dec_asdf)

        pixsep = angsep / roman_wfi_pixel_arcseconds

        print(f"===fits===>x,y,ra,dec,angsep,pixsep = {x},{y},{ra},{dec},{angsep},{pixsep}")


    exit(0)


