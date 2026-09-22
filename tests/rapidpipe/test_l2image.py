"""Tests for rapidpipe.products.l2image.L2ImageRegistration."""

from __future__ import annotations

import pytest

from rapidpipe.products.l2image import L2ImageRegistration, L2ImageRegistrationError


def _valid_kwargs() -> dict:
    return dict(
        exposure_id="e20260821001234",
        detector=7,
        delivered_version="1",
        filter="F184",
        dateobs="2026-08-21T00:12:34",
        mjdobs=61000.5,
        exptime=140.0,
        infobits=0,
        status=1,
        naxis1=64,
        naxis2=64,
        crval1=269.45,
        crval2=-28.77,
        crpix1=32.0,
        crpix2=32.0,
        cd11=-3.0e-5,
        cd12=0.0,
        cd21=0.0,
        cd22=3.0e-5,
        ctype1="RA---TAN-SIP",
        ctype2="DEC--TAN-SIP",
        cunit1="deg",
        cunit2="deg",
        equinox=2000.0,
        a_order=2,
        a={"2_0": 1e-6},
        b_order=2,
        b={"0_2": 1e-6},
        ra_targ=269.45,
        dec_targ=-28.77,
        pa_obsy=0.0,
        pa_fpa=0.0,
        zptmag=25.5,
        skymean=100.0,
        centre={"ra": 269.45, "dec": -28.77},
        corners=[[269.4, -28.8], [269.5, -28.8], [269.5, -28.7], [269.4, -28.7]],
        md5="9e107d9d372bb6826bd81d3542a419d6",
        hdu=0,
        delivery_instance="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        delivery_source="socsims-fakesrc-fits-20260807-lite",
    )


def test_round_trip_through_dict():
    reg = L2ImageRegistration(**_valid_kwargs())
    restored = L2ImageRegistration.from_dict(reg.to_dict())
    assert restored == reg
    restored.validate()


def test_from_dict_rejects_unknown_key():
    d = _valid_kwargs()
    d["bogus_field"] = 1
    with pytest.raises(L2ImageRegistrationError):
        L2ImageRegistration.from_dict(d)


def test_from_dict_rejects_missing_key():
    d = _valid_kwargs()
    del d["md5"]
    with pytest.raises(L2ImageRegistrationError):
        L2ImageRegistration.from_dict(d)


def test_validate_rejects_out_of_range_dec():
    kwargs = _valid_kwargs()
    kwargs["dec_targ"] = -95.0
    reg = L2ImageRegistration(**kwargs)
    with pytest.raises(L2ImageRegistrationError):
        reg.validate()


def test_validate_rejects_bad_md5():
    kwargs = _valid_kwargs()
    kwargs["md5"] = "not-a-valid-md5"
    reg = L2ImageRegistration(**kwargs)
    with pytest.raises(L2ImageRegistrationError):
        reg.validate()


def test_validate_rejects_bad_status():
    kwargs = _valid_kwargs()
    kwargs["status"] = 2
    reg = L2ImageRegistration(**kwargs)
    with pytest.raises(L2ImageRegistrationError):
        reg.validate()


def test_validate_rejects_negative_exptime():
    kwargs = _valid_kwargs()
    kwargs["exptime"] = -1.0
    reg = L2ImageRegistration(**kwargs)
    with pytest.raises(L2ImageRegistrationError):
        reg.validate()


def test_optional_fields_may_be_none():
    kwargs = _valid_kwargs()
    kwargs.update(pa_obsy=None, pa_fpa=None, zptmag=None, skymean=None,
                  a_order=None, a={}, b_order=None, b={},
                  delivery_source=None)
    reg = L2ImageRegistration(**kwargs)
    reg.validate()
