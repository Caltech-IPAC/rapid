"""The cone readers over synthetic catalog copies in tmp_path (`dev`'s
reader tests from ``alerts/test/test_ned_match.py``, ``test_lvs_match.py``
and ``test_gaia_match.py``, ported 2026-10-29; the matcher and alert
end-to-end tests of those files are not repeated here).

- Hp6NedReader: the reader contract, pixel-edge cones, the cache, the
  missing-pixel rule.
- LvsReader: cone reads, "" -> None, one load per process, the refusal.
- GaiaReader: partition lookup through the ancestor walk, row-group
  selection and caching, the record of reads, the empty cone, the
  missing-partition rule, the ref_epoch hand-off, and the refusal.
"""

import json

import healpy as hp
import numpy as np
import pyarrow as pa
import pytest

from rapidpipe.science.alerts.crossmatch import GAIA_COLUMNS, build_gaiacat, match_gaiacat
from rapidpipe.science.catalogs import readers as nr
from rapidpipe.science.catalogs import store as nc
from tests.support.catalogs import (
    FULL_GALAXY, FULL_STAR, HP6_NSIDE, ra_offset, write_gaia_store, write_hp6_store,
    write_lvs_store)


# ---------------------------------------------------------------------------
# Hp6NedReader
# ---------------------------------------------------------------------------

def test_hp6_table_to_columns_contract():
    t = pa.table({"prefname": ["A", "B"], "ra": [1.0, 2.0], "dec": [3.0, 4.0],
                  "ptype": pa.array(["G", ""], pa.string()), "z": [0.1, None],
                  "zunc": [None, None], "zflag": pa.array([None, "SLS"], pa.string())})
    cols = nr.table_to_columns(t)
    assert set(cols) == set(nr.NED_COLUMNS)
    assert list(cols["ptype"]) == ["G", None]            # "" -> None
    assert list(cols["zflag"]) == [None, "SLS"]
    assert cols["z"][0] == 0.1 and np.isnan(cols["z"][1]) and np.isnan(cols["zunc"]).all()


def test_hp6_cone_returns_only_rows_within_radius(tmp_path):
    ra0, dec0 = 150.0, -20.0
    write_hp6_store(tmp_path, [
        {"ra": ra0, "dec": dec0, "prefname": "AT-CENTRE", "ptype": "G", "z": 0.02},
        {"ra": ra0 + ra_offset(dec0, 6.0), "dec": dec0, "prefname": "AT-6AS", "ptype": ""},
        {"ra": ra0, "dec": dec0 + 15.0 / 3600, "prefname": "AT-15AS"},
        {"ra": ra0 + 1.0, "dec": dec0, "prefname": "FAR"},
    ])
    reader = nr.Hp6NedReader(str(tmp_path))
    assert reader.complete and reader.release == "NED_TEST"
    got = reader(ra0, dec0, 10.0)
    assert sorted(got["prefname"]) == ["AT-6AS", "AT-CENTRE"]
    assert list(got["ptype"][np.argsort(got["prefname"])]) == [None, "G"]   # "" -> None
    assert reader(ra0, dec0, 20.0)["prefname"].tolist().count("AT-15AS") == 1


def test_hp6_cone_crossing_pixel_edges_sees_every_neighbour(tmp_path):
    # centre the cone on a pixel corner: the disc overlaps several pixels,
    # and objects 3" away in each of them must all come back
    pix = int(hp.ang2pix(HP6_NSIDE, 40.0, 10.0, nest=True, lonlat=True))
    corner = hp.boundaries(HP6_NSIDE, pix, step=1, nest=True)[:, 0]
    ra0, dec0 = (float(np.asarray(v).item()) for v in hp.vec2ang(corner, lonlat=True))
    objs = [{"ra": ra0 + ra_offset(dec0, dra), "dec": dec0 + ddec / 3600,
             "prefname": f"N{i}", "ptype": "G"}
            for i, (dra, ddec) in enumerate([(3, 0), (-3, 0), (0, 3), (0, -3)])]
    pixels = write_hp6_store(tmp_path, objs)
    assert len(pixels) >= 2                       # the objects do straddle pixels
    reader = nr.Hp6NedReader(str(tmp_path))
    assert sorted(reader(ra0, dec0, 5.0)["prefname"]) == ["N0", "N1", "N2", "N3"]
    assert set(reader.pixels_for_cone(ra0, dec0, 5.0)) >= set(pixels)


def test_hp6_missing_pixel_is_empty_when_complete_and_none_when_not(tmp_path):
    # X sits at a pixel centre, so a 10" cone around it touches its pixel only
    xra, xdec = (float(v) for v in hp.pix2ang(HP6_NSIDE, 1234, nest=True, lonlat=True))
    write_hp6_store(tmp_path, [{"ra": xra, "dec": xdec, "prefname": "X"}], complete=True)
    reader = nr.Hp6NedReader(str(tmp_path))
    empty = reader(180.0, 45.0, 10.0)             # no file anywhere near
    assert empty is not None and len(empty["prefname"]) == 0
    assert set(empty) == set(nr.NED_COLUMNS)
    (tmp_path / nc.MANIFEST_NAME).unlink()        # no manifest: coverage unknown
    reader = nr.Hp6NedReader(str(tmp_path))
    assert not reader.complete
    assert reader(180.0, 45.0, 10.0) is None      # a touched pixel has no file
    assert reader(xra, xdec, 10.0)["prefname"].tolist() == ["X"]   # present pixel works


def test_hp6_pixel_cache_reads_each_file_once(tmp_path):
    write_hp6_store(tmp_path, [{"ra": 10.0, "dec": 10.0, "prefname": "A"}])
    reader = nr.Hp6NedReader(str(tmp_path), cache_pixels=2)
    reader(10.0, 10.0, 10.0)
    n = reader.n_reads
    reader(10.0, 10.0 + 1 / 3600, 10.0)
    assert reader.n_reads == n >= 1               # second cone served from cache


# ---------------------------------------------------------------------------
# LvsReader
# ---------------------------------------------------------------------------

def test_lvs_reader_cone_and_contract(tmp_path):
    ra0, dec0 = 150.0, -20.0
    write_lvs_store(tmp_path, [
        {**FULL_GALAXY, "ra": ra0, "dec": dec0},
        {"ra": ra0 + ra_offset(dec0, 25.0), "dec": dec0, "objname": "AT-25",
         "z_tech": "", "z_qual": True},
        {"ra": ra0 + ra_offset(dec0, 45.0), "dec": dec0, "objname": "AT-45"},
        {"ra": ra0 + 1.0, "dec": dec0, "objname": "FAR"},
    ])
    reader = nr.LvsReader(str(tmp_path))
    assert reader.release == "LVS_TEST" and reader.n_reads == 0    # lazy

    got = reader(ra0, dec0, 30.0)
    assert set(got) == set(nr.LVS_COLUMNS)
    assert sorted(got["objname"]) == ["AT-25", "NGC 0001"]
    at25 = list(got["objname"]).index("AT-25")
    assert got["z_tech"][at25] is None and got["z_qual"][at25] is np.True_
    assert got["Mstar"][list(got["objname"]).index("NGC 0001")] == pytest.approx(7.9e10)
    assert np.isnan(got["Mstar"][at25])

    assert sorted(reader(ra0, dec0, 50.0)["objname"]) == ["AT-25", "AT-45", "NGC 0001"]
    assert reader(ra0 + 0.5, dec0, 10.0)["objname"].size == 0      # empty, not None
    assert reader.n_reads == 1                                     # one load, reused


def test_lvs_reader_refuses_a_missing_table(tmp_path):
    with pytest.raises(FileNotFoundError, match="ingest-lvs"):
        nr.LvsReader(str(tmp_path))


# ---------------------------------------------------------------------------
# GaiaReader
# ---------------------------------------------------------------------------

def test_gaia_reader_cone_row_groups_cache_and_record(tmp_path):
    ra0, dec0 = 150.0, -20.0
    off = lambda s: ra_offset(dec0, s)   # noqa: E731
    # four near rows, then a crowd 0.3 deg away: in a different order-6
    # pixel, so its keys are all below or all above the near rows' and
    # fill row groups the cone read must skip
    crowd = [{"ra": ra0 + 0.3 + i * 1e-4, "dec": dec0 + 0.3, "source_id": 10_000 + i}
             for i in range(40)]
    partition = write_gaia_store(tmp_path, [
        {**FULL_STAR, "ra": ra0, "dec": dec0},
        {"ra": ra0 + off(0.6), "dec": dec0, "source_id": 6},
        {"ra": ra0 + off(2.0), "dec": dec0, "source_id": 20},
        {"ra": ra0 + off(60.0), "dec": dec0, "source_id": 600},
        *crowd,
    ])
    reader = nr.GaiaReader(str(tmp_path))
    n_groups = reader._footer(partition).metadata.num_row_groups
    assert n_groups >= 20
    assert reader.release == "gaia_test" and reader.complete and reader.ref_epoch == 2016.0
    assert reader.partitions == {partition} and reader.n_reads == 0 and reader.reads == []

    # the ancestor walk: an order-29 key in the cone maps to the order-3 partition
    lo, _ = nr.key_ranges_for_cone(ra0, dec0, 1.0)[0]
    assert reader.partition_for_key(lo) == partition

    got = reader(ra0, dec0, 1.0)
    assert set(got) == set(GAIA_COLUMNS)
    assert sorted(got["source_id"]) == [6, FULL_STAR["source_id"]]
    full = list(got["source_id"]).index(FULL_STAR["source_id"])
    assert got["phot_variable_flag"][full] == "VARIABLE"
    assert got["duplicated_source"][full] == True and got["non_single_star"][full] == 1   # noqa: E712
    assert got["phot_g_mean_mag"][full] == pytest.approx(17.9)
    assert np.isnan(got["phot_g_mean_mag"][list(got["source_id"]).index(6)])
    first_reads = reader.n_reads
    assert 0 < first_reads <= 3                                  # not the whole partition
    assert len(reader.reads) == first_reads                      # the record of what was read
    assert all(r[:2] == partition for r in reader.reads)

    assert sorted(reader(ra0, dec0, 1.0)["source_id"]) == [6, FULL_STAR["source_id"]]
    assert reader.n_reads == first_reads                         # served from the cache
    assert sorted(reader(ra0, dec0, 70.0)["source_id"]) == [6, 20, 600, FULL_STAR["source_id"]]
    assert reader(ra0 + 0.1, dec0 + 0.1, 5.0)["source_id"].size == 0   # empty, not None
    assert reader(ra0 + 20.0, dec0, 5.0)["source_id"].size == 0        # no partition at all


def test_gaia_reader_missing_partition_rule(tmp_path):
    ra0, dec0 = 150.0, -20.0
    write_gaia_store(tmp_path, [{"ra": ra0, "dec": dec0}], complete=False,
                     extra_partitions=[(6, 0)])        # listed, never written
    reader = nr.GaiaReader(str(tmp_path))
    assert reader(ra0, dec0, 1.0)["source_id"].size == 1
    ra_missing, dec_missing = hp.pix2ang(2 ** 6, 0, nest=True, lonlat=True)
    assert reader(float(ra_missing), float(dec_missing), 1.0) is None      # incomplete copy
    (tmp_path / nc.GAIA_INFO).write_text(json.dumps({"complete": True, "ref_epoch": 2016.0}))
    assert nr.GaiaReader(str(tmp_path))(float(ra_missing), float(dec_missing), 1.0)["source_id"].size == 0


def test_gaia_reader_feeds_the_matcher_with_its_epoch(tmp_path):
    # a star 0.4" off at the copy's epoch 2017.5 with a proper motion that
    # carries it to the detection by 2027.5: matched once propagated
    ra0, dec0 = 150.0, -20.0
    dt_years = 10.0
    pmra = -0.4 * 1000.0 / dt_years                  # mas/yr, cos(dec) included
    write_gaia_store(tmp_path, [{**FULL_STAR, "ra": ra0 + ra_offset(dec0, 0.4), "dec": dec0,
                                 "pmra": pmra, "pmdec": 0.0}], ref_epoch=2017.5)
    reader = nr.GaiaReader(str(tmp_path))
    assert reader.ref_epoch == 2017.5                            # taken from the copy
    cat = build_gaiacat(reader(ra0, dec0, 5.0), dt_years=dt_years)
    (match,) = match_gaiacat(ra0, dec0, cat, radius_arcsec=0.1)[0]
    assert match.source_id == FULL_STAR["source_id"] and match.sep == pytest.approx(0.0, abs=0.01)


def test_gaia_reader_refuses_a_missing_copy(tmp_path):
    with pytest.raises(FileNotFoundError, match="ingest-gaia"):
        nr.GaiaReader(str(tmp_path))


def test_gaia_from_table_is_readable(tmp_path):
    # the layout gaia-from-table writes is what the reader opens
    ra0, dec0 = 9.0, -44.0
    table = pa.table({"source_id": pa.array([1, 2, 3], pa.int64()),
                      "ra": [ra0, ra0 + ra_offset(dec0, 0.5), ra0 + 1.0],
                      "dec": [dec0, dec0, dec0], "pmra": [1.0, None, 2.0]})
    nc.gaia_from_table(str(tmp_path), table, "box")
    reader = nr.GaiaReader(str(tmp_path))
    assert reader.complete and reader.ref_epoch == 2016.0 and reader.release == "box"
    got = reader(ra0, dec0, 1.0)
    assert sorted(got["source_id"]) == [1, 2]
    assert np.isnan(got["pmra"][list(got["source_id"]).index(2)])
