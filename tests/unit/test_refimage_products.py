"""Tests for rapidpipe.products.refimage: the reference-image and
reference-catalog registration blocks (supervisor step 8, ruling R6).

``reference_image_entry`` and ``reference_catalog_entry`` build valid
entries; tests/db/test_register_reference.py reuses them.
"""

from __future__ import annotations

import copy

import pytest

from rapidpipe.products.refimage import (
    REFERENCE_CATALOG_CATTYPES,
    REFERENCE_CATALOG_KEY_FIELDS,
    REFERENCE_IMAGE_KEY_FIELDS,
    ReferenceCatalogRegistration,
    ReferenceImageRegistration,
    ReferenceRegistrationError,
    rapid_filter_name,
    validate_reference_catalog_entry,
    validate_reference_image_entry,
)

REF_INSTANCE = "01J8Y6QZ3MA2B4C6D8E0F2G2RF"
CONSTITUENTS = ["01J8Y6QZ3MA2B4C6D8E0F2G2C1", "01J8Y6QZ3MA2B4C6D8E0F2G2C2",
                "01J8Y6QZ3MA2B4C6D8E0F2G2C3"]
MD5 = "9e107d9d372bb6826bd81d3542a419d6"


def reference_image_registration(*, constituents=None, field=4711398, filter_="W146",
                                 npucatsources=None) -> dict:
    constituents = list(CONSTITUENTS if constituents is None else constituents)
    return {
        "md5": MD5, "status": 1, "infobits": 0, "field": field, "filter": filter_,
        "ra_center": 268.1, "dec_center": -29.4, "constituents": constituents,
        "nframes": len(constituents), "mjdobs_min": 61679.096, "mjdobs_max": 61679.115,
        "jd_start": 2461679.596, "jd_end": 2461679.615, "total_exptime": 417.0,
        "zero_point": 26.2, "cov5percent": 12.5, "medncov": 3.0, "medpixunc": 0.021,
        "npixnan": 12, "clmean": 0.004, "clstddev": 0.03, "clnoutliers": 118,
        "gmedian": 0.003, "datascale": 0.028, "gmin": -0.4, "gmax": 812.0,
        "fwhmmedpix": 1.9, "fwhmminpix": 1.2, "fwhmmaxpix": 7.5,
        "nsxcatsources": 5321, "npucatsources": npucatsources,
        "settings_hash": "sha256:" + "5" * 64,
    }


def reference_image_entry(*, instance=REF_INSTANCE, constituents=None, field=4711398,
                          filter_="W146", version="0123456789abcdef",
                          npucatsources=None, md5=MD5) -> dict:
    registration = reference_image_registration(
        constituents=constituents, field=field, filter_=filter_, npucatsources=npucatsources)
    registration["md5"] = md5
    files = {"image": "awaicgen_output_mosaic_image.fits",
             "coverage": "awaicgen_output_mosaic_cov_map.fits",
             "uncertainty": "awaicgen_output_mosaic_uncert_image.fits"}
    return {
        "kind": "reference-image", "format_version": "1", "instance": instance,
        "key": {"field": str(field), "filter": filter_, "recipe": "awaicgen", "version": version},
        "primary": f"ref/{files['image']}",
        "members": [{"role": role, "path": f"ref/{name}", "bytes": 1,
                     "sha256": "sha256:" + "a" * 64} for role, name in files.items()],
        "registration": registration,
    }


def reference_catalog_entry(*, instance="01J8Y6QZ3MA2B4C6D8E0F2G2CT", reference=REF_INSTANCE,
                            catalog_type="sextractor", md5=MD5,
                            path="ref/awaicgen_output_mosaic_refimsexcat.txt") -> dict:
    return {
        "kind": "reference-catalog", "format_version": "1", "instance": instance,
        "key": {"reference": reference, "catalog_type": catalog_type},
        "primary": path,
        "members": [{"role": "catalog", "path": path, "bytes": 1,
                     "sha256": "sha256:" + "b" * 64}],
        "registration": {"md5": md5, "status": 1, "catalog_type": catalog_type,
                         "source_count": 5321},
    }


# ----------------------------------------------------------------------
# Key fields and constants
# ----------------------------------------------------------------------

def test_key_fields_and_cattypes():
    assert REFERENCE_IMAGE_KEY_FIELDS == ("field", "filter", "recipe", "version")
    assert REFERENCE_CATALOG_KEY_FIELDS == ("reference", "catalog_type")
    assert REFERENCE_CATALOG_CATTYPES == {"sextractor": 1, "psf": 2}


# ----------------------------------------------------------------------
# reference-image
# ----------------------------------------------------------------------

def test_a_valid_reference_image_entry_returns_its_block():
    registration = validate_reference_image_entry(reference_image_entry())
    assert isinstance(registration, ReferenceImageRegistration)
    assert registration.constituents == CONSTITUENTS
    assert registration.npucatsources is None
    assert ReferenceImageRegistration.from_dict(registration.to_dict()) == registration


def test_filter_spellings_normalise_to_the_rapid_name():
    assert rapid_filter_name("F146") == "W146"
    assert rapid_filter_name(" w146 ") == "W146"
    assert rapid_filter_name("F184") == "F184"
    assert rapid_filter_name("F999") == "F999"
    # The key and the block may spell the filter differently.
    entry = reference_image_entry(filter_="W146")
    entry["key"]["filter"] = "F146"
    assert validate_reference_image_entry(entry).filter == "W146"


def test_npucatsources_may_be_a_count():
    assert validate_reference_image_entry(
        reference_image_entry(npucatsources=4000)).npucatsources == 4000


def _mutated(mutate):
    entry = reference_image_entry()
    mutate(entry)
    return entry


@pytest.mark.parametrize("mutate, message", [
    (lambda e: e.update(kind="psf"), "expected 'reference-image'"),
    (lambda e: e["key"].pop("recipe"), "key must name exactly"),
    (lambda e: e["key"].update(extra="x"), "key must name exactly"),
    (lambda e: e["key"].update(version=""), "'version' must be a non-empty string"),
    (lambda e: e["key"].update(recipe="swarp"), "unknown reference recipe"),
    (lambda e: e["key"].update(field="4711399"), "is not the key's field"),
    (lambda e: e["key"].update(filter="F184"), "is not the key's filter"),
    (lambda e: e["members"].pop(), "exactly the roles"),
    (lambda e: e["members"][1].update(role="image"), "exactly the roles"),
    (lambda e: e.update(primary=e["members"][1]["path"]), "must be its 'image' role"),
    (lambda e: e["registration"].pop("settings_hash"), "missing fields"),
    (lambda e: e["registration"].update(rfid=None), "unknown fields"),
    (lambda e: e["registration"].update(md5="ABC"), "md5 must be 32 lowercase hex"),
    (lambda e: e["registration"].update(md5=MD5.upper()), "md5 must be 32 lowercase hex"),
    (lambda e: e["registration"].update(status=2), "status must be 0 or 1"),
    (lambda e: e["registration"].update(status=True), "status must be 0 or 1"),
    (lambda e: e["registration"].update(infobits=-1), "infobits must be"),
    (lambda e: e["registration"].update(field=0), "field must be a positive integer"),
    (lambda e: e["registration"].update(field="4711398"), "field must be a positive integer"),
    (lambda e: e["registration"].update(ra_center=360.0), "ra_center must be in"),
    (lambda e: e["registration"].update(dec_center=-91.0), "dec_center must be in"),
    (lambda e: e["registration"].update(constituents=[]), "non-empty list"),
    (lambda e: e["registration"].update(constituents="01J8Y6QZ3MA2B4C6D8E0F2G2C1"),
     "non-empty list"),
    (lambda e: e["registration"]["constituents"].__setitem__(0, "not-a-ulid"),
     "constituent must be an instance ULID"),
    (lambda e: e["registration"]["constituents"].__setitem__(1, CONSTITUENTS[0]),
     "constituents repeat"),
    (lambda e: e["registration"].update(nframes=2), "nframes must equal"),
    (lambda e: e["registration"].update(cov5percent=float("nan")), "cov5percent must be a finite"),
    (lambda e: e["registration"].update(cov5percent=101.0), "cov5percent must be a percentage"),
    (lambda e: e["registration"].update(gmax="big"), "gmax must be a finite number"),
    (lambda e: e["registration"].update(fwhmmedpix=float("inf")), "fwhmmedpix must be a finite"),
    (lambda e: e["registration"].update(npixnan=1.5), "npixnan must be a non-negative integer"),
    (lambda e: e["registration"].update(nsxcatsources=-1), "nsxcatsources must be"),
    (lambda e: e["registration"].update(nsexcatsources=1), "unknown fields"),
    (lambda e: e["registration"].update(npucatsources=-1), "npucatsources must be"),
    (lambda e: e["registration"].update(mjdobs_min=61680.0), "is after mjdobs_max"),
    (lambda e: e["registration"].update(jd_end=2461679.0), "is after jd_end"),
    (lambda e: e["registration"].update(total_exptime=-1.0), "total_exptime must be"),
    (lambda e: e["registration"].update(settings_hash="sha256:xyz"), "settings_hash must be"),
])
def test_reference_image_errors(mutate, message):
    with pytest.raises(ReferenceRegistrationError, match=message):
        validate_reference_image_entry(_mutated(mutate))


def test_errors_are_value_errors():
    """`register` maps ValueError to InputRejected (exit 65)."""
    assert issubclass(ReferenceRegistrationError, ValueError)


def test_validation_does_not_mutate_the_entry():
    entry = reference_image_entry()
    before = copy.deepcopy(entry)
    validate_reference_image_entry(entry)
    assert entry == before


# ----------------------------------------------------------------------
# reference-catalog
# ----------------------------------------------------------------------

def test_a_valid_reference_catalog_entry_returns_its_block():
    registration = validate_reference_catalog_entry(reference_catalog_entry())
    assert registration == ReferenceCatalogRegistration(
        md5=MD5, status=1, catalog_type="sextractor", source_count=5321)
    assert validate_reference_catalog_entry(
        reference_catalog_entry(catalog_type="psf")).catalog_type == "psf"


def _mutated_catalog(mutate):
    entry = reference_catalog_entry()
    mutate(entry)
    return entry


@pytest.mark.parametrize("mutate, message", [
    (lambda e: e.update(kind="reference-image"), "expected 'reference-catalog'"),
    (lambda e: e["key"].update(sign="positive"), "key must name exactly"),
    (lambda e: e["key"].update(reference="field-5321"), "reference must be an instance ULID"),
    (lambda e: e["key"].update(catalog_type="photutils"), "unknown catalog_type"),
    (lambda e: e["members"][0].update(role="image"), "exactly one member, role 'catalog'"),
    (lambda e: e["members"].append(dict(e["members"][0], path="ref/other.txt")),
     "exactly one member"),
    (lambda e: e["registration"].pop("source_count"), "missing fields"),
    (lambda e: e["registration"].update(rfid=1), "unknown fields"),
    (lambda e: e["registration"].update(md5="0" * 31), "md5 must be 32"),
    (lambda e: e["registration"].update(status=-1), "status must be 0 or 1"),
    (lambda e: e["registration"].update(source_count=-2), "source_count must be"),
    (lambda e: e["registration"].update(source_count=True), "source_count must be"),
    (lambda e: e["registration"].update(catalog_type="psf"), "is not the key's"),
])
def test_reference_catalog_errors(mutate, message):
    with pytest.raises(ReferenceRegistrationError, match=message):
        validate_reference_catalog_entry(_mutated_catalog(mutate))
