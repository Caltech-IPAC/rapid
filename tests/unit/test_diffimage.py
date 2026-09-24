"""Tests for rapidpipe.products.diffimage."""

from __future__ import annotations

import copy

import pytest

from rapidpipe.products.diffimage import (
    CATALOG_OUTCOME_BITS,
    DIFFERENCERS,
    DifferenceImageRegistration,
    DifferenceImageRegistrationError,
    catalog_outcome_bit,
    check_bundle_roles,
    validate_difference_entry,
    validate_source_catalog_entry,
)

SHA = "sha256:" + "0" * 64


def _valid_kwargs() -> dict:
    return dict(
        detection_role="significance",
        centre={"ra": 269.4521, "dec": -28.7710},
        corners=[[269.39, -28.83], [269.51, -28.83], [269.51, -28.71], [269.39, -28.71]],
        catalog_outcome_bits=0,
        infobits_science=0,
        infobits_reference=0,
        source_counts={
            "sextractor": {"positive": 412, "negative": 388},
            "photutils": {"positive": 405, "negative": 391},
        },
        registration_residual={"x_rms": 0.0, "y_rms": 0.0, "x_median": 0.004, "y_median": -0.002},
        reference_scale_factor=0.998,
        md5="9e107d9d372bb6826bd81d3542a419d6",
        reference_rfid=None,
    )


def _zogy_entry(**registration_overrides) -> dict:
    registration = _valid_kwargs()
    registration.update(registration_overrides)
    return {
        "kind": "difference-image",
        "format_version": "1",
        "instance": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "key": {"l2": "L2", "reference": "REF", "differencer": "zogy", "settings_hash": SHA},
        "primary": "diff/zogy_diffimage_masked.fits",
        "members": [
            {"role": "difference", "path": "diff/zogy_diffimage_masked.fits", "bytes": 1, "sha256": SHA},
            {"role": "uncertainty", "path": "diff/zogy_diffimage_uncert_masked.fits", "bytes": 1, "sha256": SHA},
            {"role": "significance", "path": "diff/scorrimage_masked.fits", "bytes": 1, "sha256": SHA},
            {"role": "psf", "path": "diff/diffpsf.fits", "bytes": 1, "sha256": SHA},
        ],
        "registration": registration,
    }


def test_round_trip_through_dict():
    reg = DifferenceImageRegistration(**_valid_kwargs())
    restored = DifferenceImageRegistration.from_dict(reg.to_dict())
    assert restored == reg
    restored.validate("zogy")


def test_from_dict_rejects_unknown_and_missing_fields():
    d = _valid_kwargs()
    d["extra"] = 1
    with pytest.raises(DifferenceImageRegistrationError, match="unknown fields"):
        DifferenceImageRegistration.from_dict(d)
    d = _valid_kwargs()
    del d["md5"]
    with pytest.raises(DifferenceImageRegistrationError, match="missing fields"):
        DifferenceImageRegistration.from_dict(d)


def test_catalog_outcome_bits_match_dev_docstring():
    # dev's awsBatchSubmitJobs_runSingleSciencePipeline.py module docstring.
    assert catalog_outcome_bit("zogy", "positive") == 1
    assert catalog_outcome_bit("zogy", "negative") == 2
    assert catalog_outcome_bit("sfft", "positive") == 4
    assert catalog_outcome_bit("sfft", "negative") == 8
    assert catalog_outcome_bit("naive", "positive") == 16
    assert catalog_outcome_bit("naive", "negative") == 32
    assert len(CATALOG_OUTCOME_BITS) == 6


def test_catalog_outcome_bits_out_of_range_rejected():
    reg = DifferenceImageRegistration(**{**_valid_kwargs(), "catalog_outcome_bits": 64})
    with pytest.raises(DifferenceImageRegistrationError, match="catalog_outcome_bits"):
        reg.validate("zogy")


def test_unavailable_photutils_count_is_null_not_zero():
    kwargs = _valid_kwargs()
    kwargs["source_counts"]["photutils"]["positive"] = None
    kwargs["catalog_outcome_bits"] = 1
    DifferenceImageRegistration(**kwargs).validate("zogy")


def test_sextractor_positive_count_required():
    kwargs = _valid_kwargs()
    kwargs["source_counts"]["sextractor"]["positive"] = None
    with pytest.raises(DifferenceImageRegistrationError, match="nsexcatsources"):
        DifferenceImageRegistration(**kwargs).validate("zogy")


@pytest.mark.parametrize("field,value,match", [
    ("md5", "XYZ", "md5"),
    ("centre", {"ra": 360.0, "dec": 0.0}, "centre ra"),
    ("corners", [[0.0, 0.0]] * 3, "four"),
    ("detection_role", "kernel", "detection_role"),
    ("reference_rfid", 0, "reference_rfid"),
    ("reference_scale_factor", float("nan"), "reference_scale_factor"),
])
def test_invalid_fields_rejected(field, value, match):
    reg = DifferenceImageRegistration(**{**_valid_kwargs(), field: value})
    with pytest.raises(DifferenceImageRegistrationError, match=match):
        reg.validate("zogy")


def test_declared_roles_per_differencer():
    assert DIFFERENCERS["zogy"].required == ("difference", "uncertainty", "significance")
    assert DIFFERENCERS["zogy"].optional == ("psf",)
    assert DIFFERENCERS["sfft"].required == ("difference", "uncertainty")
    assert DIFFERENCERS["sfft"].optional == ("psf", "kernel")
    assert DIFFERENCERS["zogy"].default_detection_role == "significance"
    assert DIFFERENCERS["sfft"].default_detection_role == "difference"
    assert "naive" not in DIFFERENCERS


def test_missing_required_role_rejected():
    with pytest.raises(DifferenceImageRegistrationError, match="missing declared role"):
        check_bundle_roles("zogy", ["difference", "uncertainty"])


def test_optional_role_may_be_absent():
    check_bundle_roles("zogy", ["difference", "uncertainty", "significance"])
    check_bundle_roles("sfft", ["difference", "uncertainty"])


def test_undeclared_role_rejected():
    with pytest.raises(DifferenceImageRegistrationError, match="does not declare"):
        check_bundle_roles("sfft", ["difference", "uncertainty", "significance"])


def test_unknown_differencer_rejected():
    with pytest.raises(DifferenceImageRegistrationError, match="unknown differencer"):
        check_bundle_roles("naive", ["difference"])


def test_valid_zogy_entry():
    reg = validate_difference_entry(_zogy_entry())
    assert reg.detection_role == "significance"


def test_entry_missing_significance_rejected():
    entry = _zogy_entry()
    entry["members"] = [m for m in entry["members"] if m["role"] != "significance"]
    with pytest.raises(DifferenceImageRegistrationError, match="missing declared role"):
        validate_difference_entry(entry)


def test_entry_primary_must_be_difference():
    entry = _zogy_entry()
    entry["primary"] = "diff/scorrimage_masked.fits"
    with pytest.raises(DifferenceImageRegistrationError, match="primary"):
        validate_difference_entry(entry)


def test_entry_key_fields_required():
    entry = _zogy_entry()
    del entry["key"]["reference"]
    with pytest.raises(DifferenceImageRegistrationError, match="reference"):
        validate_difference_entry(entry)
    entry = _zogy_entry()
    entry["key"]["settings_hash"] = "abc"
    with pytest.raises(DifferenceImageRegistrationError, match="settings_hash"):
        validate_difference_entry(entry)


def test_sfft_entry_with_kernel_and_difference_detection():
    entry = copy.deepcopy(_zogy_entry(detection_role="difference"))
    entry["key"]["differencer"] = "sfft"
    entry["members"] = [
        {"role": "difference", "path": "diff/sfftdiffimage_masked.fits", "bytes": 1, "sha256": SHA},
        {"role": "uncertainty", "path": "diff/sfftdiffimage_uncert_masked.fits", "bytes": 1, "sha256": SHA},
        {"role": "kernel", "path": "diff/sfftsoln.fits", "bytes": 1, "sha256": SHA},
    ]
    entry["primary"] = "diff/sfftdiffimage_masked.fits"
    validate_difference_entry(entry)


def _catalog_entry(**overrides):
    entry = {
        "kind": "source-catalog",
        "format_version": "1",
        "instance": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
        "key": {"difference": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "catalog_type": "sextractor", "sign": "positive"},
        "primary": "diff/zogy_diffimage_masked.txt",
        "members": [{"role": "catalog", "path": "diff/zogy_diffimage_masked.txt", "bytes": 1, "sha256": SHA}],
        "registration": {"source_count": 412},
    }
    entry.update(overrides)
    return entry


def test_valid_source_catalog_entry():
    validate_source_catalog_entry(_catalog_entry())


@pytest.mark.parametrize("overrides,match", [
    ({"key": {"difference": "D", "catalog_type": "other", "sign": "positive"}}, "catalog_type"),
    ({"key": {"difference": "D", "catalog_type": "sextractor", "sign": "both"}}, "sign"),
    ({"registration": {"source_count": -1}}, "source_count"),
    ({"registration": {}}, "source_count"),
])
def test_invalid_source_catalog_entry(overrides, match):
    with pytest.raises(DifferenceImageRegistrationError, match=match):
        validate_source_catalog_entry(_catalog_entry(**overrides))


# finalize's provenance fields (supervisor ruling, 2026-09-24).


def test_finalized_entry_with_provenance_fields_validates():
    reg = validate_difference_entry(
        _zogy_entry(finalized_from="01ARZ3NDEKTSV4RRFFQ69G5FAV", revision=2))
    assert reg.md5 == "9e107d9d372bb6826bd81d3542a419d6"
    assert not hasattr(reg, "finalized_from")


@pytest.mark.parametrize("overrides,match", [
    ({"finalized_from": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}, "both of"),
    ({"revision": 2}, "both of"),
    ({"finalized_from": "", "revision": 2}, "finalized_from"),
    ({"finalized_from": "X", "revision": 1}, "revision"),
    ({"finalized_from": "X", "revision": True}, "revision"),
])
def test_finalized_entry_with_bad_provenance_rejected(overrides, match):
    with pytest.raises(DifferenceImageRegistrationError, match=match):
        validate_difference_entry(_zogy_entry(**overrides))


def test_copied_source_catalog_entry_validates():
    validate_source_catalog_entry(_catalog_entry(
        registration={"source_count": 412, "copied_from": "01ARZ3NDEKTSV4RRFFQ69G5FAW"}))


@pytest.mark.parametrize("registration,match", [
    ({"source_count": 412, "copied_from": ""}, "copied_from"),
    ({"source_count": 412, "copied_from": "X", "extra": 1}, "source_count"),
])
def test_copied_source_catalog_entry_with_bad_provenance_rejected(registration, match):
    with pytest.raises(DifferenceImageRegistrationError, match=match):
        validate_source_catalog_entry(_catalog_entry(registration=registration))
