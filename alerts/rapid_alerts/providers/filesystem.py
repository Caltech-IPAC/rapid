"""
Provider backed by pipeline product files on disk. NOT YET IMPLEMENTED.

The logic to port lives in alerts/roman_rapid_alerts/generate_alerts.py on
the add-alert-generation branch:

    get_detection / iter_detections  <-  parse_sextractor() + load_psf_catalog()
                                         + match_psf() + FITS header parsing
    get_object_for_source            <-  load_lc_tile() + match_lc()
    get_prv_detections               <-  nested_lc_data unpacking in
                                         build_prv_dia_sources()
    get_cutouts                      <-  load_image() + extract_stamp()

Note the flux calibration difference: that script converts SExtractor and
light-curve fluxes to nJy via FILTER_ZP_EFF, while the database flow
currently passes instrumental fluxfit through. Reconcile when porting.
"""

from .base import AlertDataProvider


class FilesystemProvider(AlertDataProvider):

    def __init__(self, data_dir):
        raise NotImplementedError(
            "FilesystemProvider is a placeholder; port the reading logic "
            "from alerts/roman_rapid_alerts/generate_alerts.py "
            "(add-alert-generation branch)")

    def get_detection(self, sid):
        raise NotImplementedError

    def get_object_for_source(self, detection):
        raise NotImplementedError

    def get_prv_detections(self, detection, obj, window_days=365.25):
        raise NotImplementedError

    def get_forced_photometry(self, detection, obj):
        raise NotImplementedError

    def get_cutouts(self, detection):
        raise NotImplementedError
