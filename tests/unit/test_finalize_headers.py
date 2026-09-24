"""Tests for rapidpipe.science.finalize.headers: the stamp table and its FITS write."""

from __future__ import annotations

import warnings
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from astropy.io import fits
from astropy.io.fits.verify import VerifyWarning
from astropy.utils.exceptions import AstropyUserWarning

from rapidpipe.science.finalize import headers

ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _values(**overrides) -> headers.StampValues:
    values = dict(
        run=ULID, attempt=ULID, instance=ULID, finalized_from=ULID,
        l2_instance=ULID, reference_instance=ULID, differencer="zogy",
        settings_hash="sha256:" + "0" * 64,
        source_revision="0123456789abcdef0123456789abcdef01234567",
        image_digest="sha256:" + "a" * 64, output_location="s3://bucket/runs/r/finalize/u/a",
        ppid=15, infobits=3, field=4662268, diff_filename="diffimage_masked.fits",
        date="2026-09-24T12:00:00")
    values.update(overrides)
    return headers.StampValues(**values)


def test_the_keyword_table_is_the_ruling_s():
    assert [k for k, _, _ in headers.KEYWORDS] == [
        "RPRUN", "RPATTMPT", "RPINST", "RPSTAGE", "RPFINFRM", "RPL2INST", "RPREFINS",
        "RPDIFFER", "RPSETHSH", "RPSRCREV", "RPIMGDIG", "RPOUTLOC", "PPID", "INFOBITS",
        "FIELD", "DIFFILEN", "DATE"]
    headers.check_keywords()


def test_no_dev_database_or_s3_keyword_is_stamped():
    names = {k for k, _, _ in headers.KEYWORDS}
    assert not names & {"PID", "RID", "EXPID", "FID", "DIFIMVER", "S3BUCKN", "S3OBJPRF"}


def test_stamp_cards_map_values_in_order_with_comments():
    cards = headers.stamp_cards(_values())
    by_name = {k: (v, c) for k, v, c in cards}
    assert by_name["RPSTAGE"][0] == "finalize"
    assert by_name["PPID"][0] == 15
    assert by_name["INFOBITS"][0] == 3
    assert by_name["FIELD"][0] == 4662268
    assert by_name["RPOUTLOC"][0] == "s3://bucket/runs/r/finalize/u/a"
    assert all(comment for _, _, comment in cards)


def test_utc_date_is_iso_to_the_second_in_utc():
    pst = timezone(timedelta(hours=-7))
    assert headers.utc_date(datetime(2026, 9, 24, 5, 6, 7, 999, tzinfo=pst)) == "2026-09-24T12:06:07"
    assert len(headers.utc_date()) == 19


def test_ppid_for_maps_and_refuses_an_unknown_differencer():
    assert headers.ppid_for("sfft", {"zogy": 15, "sfft": 16}) == 16
    with pytest.raises(ValueError, match="naive"):
        headers.ppid_for("naive", {"zogy": 15})


def test_provenance_value_defaults_to_unknown():
    assert headers.provenance_value(None) == "unknown"
    assert headers.provenance_value("") == "unknown"
    assert headers.provenance_value("abc") == "abc"


def test_write_stamped_sets_the_primary_header_and_checksums(tmp_path):
    source = tmp_path / "in.fits"
    data = np.arange(16, dtype=np.float32).reshape(4, 4)
    ext = fits.ImageHDU(data=np.ones((2, 2), dtype=np.float32), name="EXTRA")
    fits.HDUList([fits.PrimaryHDU(data=data, header=fits.Header([("OBJECT", "x")])), ext]
                 ).writeto(source)
    before = source.read_bytes()
    destination = tmp_path / "out.fits"

    with warnings.catch_warnings():
        warnings.simplefilter("error", VerifyWarning)   # no card truncated
        headers.write_stamped(source, destination, headers.stamp_cards(_values()))

    assert source.read_bytes() == before
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", AstropyUserWarning)
        with fits.open(destination, checksum=True) as hdul:
            for hdu in hdul:
                _ = hdu.data
            header = hdul[0].header
            assert header["OBJECT"] == "x"
            assert header["RPSTAGE"] == "finalize"
            assert header["RPSETHSH"] == "sha256:" + "0" * 64
            assert header["RPIMGDIG"] == "sha256:" + "a" * 64
            assert header.comments["RPSRCREV"] == "Difference code revision"
            assert "CHECKSUM" in header and "DATASUM" in header
            np.testing.assert_array_equal(hdul[0].data, data)
            assert (hdul[0].data.dtype.kind, hdul[0].data.dtype.itemsize) == ("f", 4)
            assert hdul["EXTRA"].data.shape == (2, 2)
    assert not [w for w in caught if "checksum" in str(w.message).lower()]


def test_write_stamped_refuses_to_overwrite(tmp_path):
    source = tmp_path / "in.fits"
    fits.PrimaryHDU(data=np.zeros((2, 2), dtype=np.float32)).writeto(source)
    with pytest.raises(OSError):
        headers.write_stamped(source, source, headers.stamp_cards(_values()))


def test_check_readable_refuses_a_non_fits_file(tmp_path):
    path = tmp_path / "bad.fits"
    path.write_bytes(b"not a fits file at all")
    with pytest.raises(Exception):
        headers.check_readable(path)


@pytest.mark.parametrize("length", [1, 59, 60, 64, 68, 69, 150])
def test_fits_card_never_cuts_the_comment(length):
    # astropy truncates the comment of a 60-68 character string; the
    # output location can be any length.
    value = "s3://" + "x" * (length - 5) if length > 5 else "x" * length
    comment = "Finalize attempt output location"
    with warnings.catch_warnings():
        warnings.simplefilter("error", VerifyWarning)
        header = fits.Header()
        header.append(headers.fits_card("RPOUTLOC", value, comment))
        restored = fits.Header.fromstring(header.tostring())
    assert restored["RPOUTLOC"] == value
    assert restored.comments["RPOUTLOC"] == comment
