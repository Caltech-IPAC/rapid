"""
Assemble the command line for modules/sfft/sfft_rapid_rimtimsim.py.

Kept free of module-level side effects so that the logic can be imported and tested directly
(see pipeline/test/test_sfft_command_equivalence.py).
"""

import ast


def build_sfft_command_args(python_cmd,
                            sfft_code,
                            filename_scifile,
                            filename_reffile,
                            filename_scicat,
                            filename_refcat,
                            filename_scipsf,
                            filename_refpsf,
                            filename_scisegm,
                            filename_refsegm,
                            science_image_filename,
                            crossconv_flag,
                            sfft_dict):

    """
    Build the argument list for modules/sfft/sfft_rapid_rimtimsim.py.

    Bright-source masking settings and segmentation masking are taken from the [SFFT]
    configuration section.  Both fall back to the historical behaviour when the relevant
    parameters are absent, so pre-existing configuration files produce an identical command:

      * Bright-source masking was previously selected by testing whether the science-image
        filename began with a lower-case "r".  That conflated the socsims and the rimtimsims,
        and it is case-sensitive, so a future lower-case "roman_*" data set would silently
        inherit socsims settings.
      * Segmentation masking was previously appended only inside the cross-convolution branch,
        making it an accidental side effect of crossconv_flag rather than a deliberate choice.
    """

    if 'sfft_bsmask_value' in sfft_dict:

        sfft_bsmask_value = sfft_dict['sfft_bsmask_value']
        sfft_bsmask_radius = sfft_dict['sfft_bsmask_radius']
        sfft_use_gainmatch_catalogs = ast.literal_eval(sfft_dict['sfft_use_gainmatch_catalogs'])

    else:

        print("*** Warning: [SFFT] sfft_bsmask_value not found in the configuration file; " +
              "falling back to the legacy science-image-filename test.  Please add " +
              "sfft_bsmask_value, sfft_bsmask_radius and sfft_use_gainmatch_catalogs to " +
              "the [SFFT] configuration section.")

        if "r" == science_image_filename[0]:

            sfft_bsmask_value = "20000.0"
            sfft_bsmask_radius = "30.0"
            sfft_use_gainmatch_catalogs = False

        else:

            sfft_bsmask_value = "50.0"
            sfft_bsmask_radius = "100.0"
            sfft_use_gainmatch_catalogs = True


    # A quirk in the SFFT software requires prepended "./" to the positional input filenames.

    sfft_cmd = [python_cmd,
                sfft_code,
                "./" + filename_scifile,
                "./" + filename_reffile]

    if sfft_use_gainmatch_catalogs:

        sfft_cmd.append("--scicat")
        sfft_cmd.append(filename_scicat)
        sfft_cmd.append("--refcat")
        sfft_cmd.append(filename_refcat)

    sfft_cmd.append("--bsmaskvalue")
    sfft_cmd.append(str(sfft_bsmask_value))
    sfft_cmd.append("--bsmaskradius")
    sfft_cmd.append(str(sfft_bsmask_radius))


    # If crossconv_flag = False, then the SFFT diffimage PSF is just the science-image PSF.

    sfft_cmd.append("--scipsf")
    sfft_cmd.append(filename_scipsf)

    if crossconv_flag:

        sfft_cmd.append("--crossconv")
        sfft_cmd.append("--refpsf")
        sfft_cmd.append(filename_refpsf)


    # Segmentation-based background masking.  Suits sparse extragalactic fields; in the
    # galactic bulge the SExtractor footprints merge and cover nearly the whole frame, so
    # segm == 0 selects very little and the mask is close to inert.

    if 'sfft_use_segmentation' in sfft_dict:
        sfft_use_segmentation = ast.literal_eval(sfft_dict['sfft_use_segmentation'])
    else:
        sfft_use_segmentation = crossconv_flag

    if sfft_use_segmentation:

        sfft_cmd.append("--scisegm")
        sfft_cmd.append(filename_scisegm)
        sfft_cmd.append("--refsegm")
        sfft_cmd.append(filename_refsegm)

    return sfft_cmd
