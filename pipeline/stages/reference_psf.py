"""
File:    reference_psf.py

Where the reference-image PSF lives, given the science PSF the manifest names.

The monolith read a filename template from the master .ini —
`[JOB_PARAMS] refimage_psf_filename` for the PSF the science pipeline
differences against, `[REF_IMAGE] refimage_psf_filename` for the north-up PSF
the reference-image pipeline fits its PhotUtils catalogue with (dev d11c87d4)
— substituted the filter (`FID`) and, optionally, the detector (`SCAID`) into
it, and fetched the object from a `refimage_psfs/` directory beside the `psfs/`
directory the science PSFs are registered from (dev e3c15953 lineage;
`awsBatchSubmitJobs_runSingleSciencePipeline.py:363-371`).

The first extraction collapsed both onto the science PSF: `download_inputs`
took `psf_uri` for the reference PSF too, so ZOGY and SFFT differenced with
the science image's own PSF and the reference catalogue was fitted with it as
well. That is not the pipeline the 2026-08-21 socsim run was, and it is not
what the reference PSFs carried into the inputs bucket are for
(`rapid_systems/docs/reference/psf-carry-provenance.md`: `refimage_psfs/`
beside `psfs/`, one generation).

**Release content names the file, the manifest names the generation.** The
template is a reference-data version and therefore release content
(`system/security.md`, job configuration's three homes: "anything that can
alter a science product — tuning, reference-data versions — is release
content"). The generation it is read from is the one the manifest's science
PSF belongs to — the `psfs/` directory's parent — so the science PSF and the
reference PSF a unit runs with are always from one sealed generation, and no
bucket name or prefix enters release content. The two tokens are per-unit
facts substituted here; nothing else in the template is interpreted.
"""

import posixpath

from pipeline.runtime.errors import InputError

#: The directory the registered science PSFs live in, under a generation.
SCIENCE_PSF_DIRECTORY = "psfs"
#: Its sibling holding the reference PSFs (the carry runbook's layout).
REFERENCE_PSF_DIRECTORY = "refimage_psfs"

#: The filter token. Substituted as the integer filter id.
FID_TOKEN = "FID"
#: The detector token, zero-padded to two digits. "SCAID" rather than "SCA"
#: because real filenames contain "SCA" (`WFI_SCA07_...`).
SCA_TOKEN = "SCAID"


def substitute_tokens(template: str, fid: int, sca: int) -> str:
    """The filename with `FID` and `SCAID` replaced.

    A template carrying neither token is returned unchanged, which is how the
    single-PSF-per-filter data sets (OpenUniverse, rimtimsim) are configured.
    """
    return (template.replace(FID_TOKEN, str(int(fid)))
                    .replace(SCA_TOKEN, "{:02d}".format(int(sca))))


def reference_psf_uri(psf_uri: str, template: str, fid: int, sca: int) -> str:
    """The reference PSF's S3 URI for a unit.

    Parameters
    ----------
    psf_uri : str
        The science PSF the manifest names (`psfs.filename` for the unit's
        `(fid, sca)`), `s3://<bucket>/<generation>/psfs/<name>`.
    template : str
        The release-content filename template, e.g.
        `refimage_psf_fidFID.fits` or `refimage_psf_f146_scaSCAID.fits`.
    fid, sca : int
        The unit's filter id and detector, from the manifest.

    Raises
    ------
    InputError
        If the template is empty or the science PSF does not sit in a
        `psfs/` directory, which is the convention the generation layout is
        derived from. Loud, because the alternative is differencing with the
        wrong PSF and reporting success.
    """
    if not template or not template.strip():
        raise InputError(
            "the reference-PSF filename template in release content is "
            "empty; it names the reference PSF fetched beside the science "
            "PSF and has no default", uri=psf_uri)

    if not psf_uri.startswith("s3://"):
        raise InputError(
            f"the science PSF {psf_uri!r} is not an s3:// URI, so the "
            "reference PSF's generation cannot be located from it",
            uri=psf_uri)

    directory, _name = posixpath.split(psf_uri)
    parent, leaf = posixpath.split(directory)
    if leaf != SCIENCE_PSF_DIRECTORY:
        raise InputError(
            f"the science PSF {psf_uri!r} is not in a "
            f"{SCIENCE_PSF_DIRECTORY!r} directory; the reference PSF is "
            f"located as its generation's {REFERENCE_PSF_DIRECTORY!r} "
            "sibling and that convention does not hold here",
            uri=psf_uri)

    return posixpath.join(parent, REFERENCE_PSF_DIRECTORY,
                          substitute_tokens(template, fid, sca))
