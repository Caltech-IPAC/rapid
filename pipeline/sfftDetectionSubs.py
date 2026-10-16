"""
Detection settings for SExtractor on the SFFT difference image.

All three difference-image SExtractor passes (ZOGY positive and negative, SFFT) share the
[SEXTRACTOR_DIFFIMAGE] block.  ZOGY detects on Scorr, which is already a matched-filter
statistic; SFFT without cross-convolution detects on the raw difference image, where a
detection filter and a lower area threshold recover faint transients.  The optional
[SEXTRACTOR_DIFFIMAGE_SFFT] block holds the keys that override [SEXTRACTOR_DIFFIMAGE] for the
SFFT passes only.

On 250 SOC GBTDS SCAs, cut at production SNR > 5, a gaussian FWHM 2.0 px 7x7 filter with
DETECT_MINAREA 1 and DETECT_THRESH 2.0 raised SFFT completeness from 0.297 to 0.572
(brightening) and 0.247 to 0.354 (fading) relative to the production settings (no filter,
minarea 5, thresh 2.5).

The overrides are not applied when SFFT cross-convolution is on: detection then runs on the
cross-convolved image, which is already matched-filtered, and filtering it again would
smooth it twice.
"""

import os


def sfft_sextractor_dict(base_dict,override_dict,crossconv_flag,cfg_path):

    '''
    Return a new dictionary: base_dict, with the keys of override_dict laid over it unless
    override_dict is None or crossconv_flag is set.  base_dict is not modified.  A relative
    sextractor_filter_name is taken to be a file in cfg_path.
    '''

    merged = dict(base_dict)

    if override_dict is None or crossconv_flag:
        return merged

    for key,value in dict(override_dict).items():

        if key == "sextractor_filter_name" and not os.path.isabs(value):
            value = os.path.join(cfg_path,value)

        merged[key] = value

    return merged
