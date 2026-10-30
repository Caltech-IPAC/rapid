"""Synthetic catalog copies and in-memory slices for the catalog tests.

`dev`'s ``alerts/test/conftest.py`` helpers (``make_ned_table``,
``make_lvs_table``, ``make_gaia_table``) and the store writers of its
reader tests (``write_hp6_store``, ``write_lvs_store``, ``write_gaia_store``),
ported with the catalogs subpackage (2026-10-29). Each writer lays out
under a directory exactly what the corresponding builder of
:mod:`rapidpipe.science.catalogs.store` would, so a reader test and a
stage test share one fixture shape.
"""

import json
import math

import healpy as hp
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from rapidpipe.science.alerts.crossmatch import (
    GAIA_BOOL_COLUMNS, GAIA_INT_COLUMNS, GAIA_NUMERIC_COLUMNS, GAIA_STRING_COLUMNS)
from rapidpipe.science.catalogs import readers as nr
from rapidpipe.science.catalogs import store as nc

HP6_NSIDE = 2 ** nc.HP6_ORDER


def ra_offset(dec, sep_arcsec):
    """Degrees of RA giving `sep_arcsec` of separation at declination dec."""
    return sep_arcsec / 3600.0 / math.cos(math.radians(dec))


# one star with every Gaia column set, for "does it all get through" tests
FULL_STAR = {
    "source_id": 4057125350474264448, "non_single_star": 1, "ipd_frac_multi_peak": 7,
    "duplicated_source": True, "in_qso_candidates": False, "in_galaxy_candidates": True,
    "phot_variable_flag": "VARIABLE",
    "pmra": -3.2, "pmdec": -5.9, "parallax": 0.41, "parallax_error": 0.05, "ruwe": 1.1,
    "phot_g_mean_mag": 17.9, "phot_bp_mean_mag": 19.1, "phot_rp_mean_mag": 16.8,
    "bp_rp": 2.3, "phot_g_mean_flux_over_error": 310.0,
    "classprob_dsc_combmod_star": 0.99, "classprob_dsc_combmod_galaxy": 0.005,
    "classprob_dsc_combmod_quasar": 0.005,
}

# one galaxy with the LVS columns a match reads
FULL_GALAXY = {
    "objname": "NGC 0001", "objtype": "G", "z_tech": "SPEC", "DistMpc_method": "zIndependent",
    "z_qual": True, "Diam_qual": True,
    "z": 0.015, "z_unc": 0.0001, "DistMpc": 62.0, "DistMpc_unc": 4.0,
    "Diam": 1.6, "Diam_ba": 0.8, "Diam_pa": 110.0, "ebv": 0.03,
    "m_Ks": 10.1, "m_Ks_unc": 0.02, "m_W1": 10.9, "m_W1_unc": 0.02,
    "m_NUV": 15.2, "m_NUV_unc": 0.05, "SFR_hybrid": 3.1, "SFR_hybrid_unc": 0.4,
    "Mstar": 7.9e10, "Mstar_unc": 1.1e10,
}


def make_ned_table(entries):
    """Column arrays keyed by NED_COLUMNS from a list of dicts.

    The in-memory stand-in for a NED slice, as a NedSliceReader would
    return it: missing numerics are NaN, missing strings are None.
    Requires "ra", "dec"; "prefname" defaults to NED0001.., "ptype" to "G",
    "z" and "zunc" to NaN, "zflag" to None.
    """
    n = len(entries)
    return {
        "prefname": np.array([e.get("prefname", f"NED{i:04d}")
                              for i, e in enumerate(entries, start=1)],
                             dtype=object),
        "ra": np.array([e["ra"] for e in entries], dtype=float),
        "dec": np.array([e["dec"] for e in entries], dtype=float),
        "ptype": np.array([e.get("ptype", "G") for e in entries],
                          dtype=object),
        "z": np.array([e.get("z", np.nan) for e in entries], dtype=float),
        "zunc": np.array([e.get("zunc", np.nan) for e in entries],
                         dtype=float),
        "zflag": np.array([e.get("zflag") for e in entries], dtype=object),
    } if n else {k: np.array([], dtype=float if k in ("ra", "dec", "z", "zunc")
                                     else object)
                 for k in nr.NED_COLUMNS}


def make_lvs_table(entries):
    """Column arrays keyed by LVS_COLUMNS from a list of dicts.

    Requires "ra", "dec"; "objname" defaults to LVS0001.., "objtype" to "G",
    the other strings to None, numerics to NaN, the two quality flags to False.
    """
    def column(name, default, dtype):
        return np.array([e.get(name, default(i) if callable(default) else default)
                         for i, e in enumerate(entries, start=1)], dtype=dtype)

    table = {}
    for name in nr.LVS_STRING_COLUMNS:
        default = ((lambda i: f"LVS{i:04d}") if name == "objname"
                   else "G" if name == "objtype" else None)
        table[name] = column(name, default, object)
    for name in nr.LVS_BOOL_COLUMNS:
        table[name] = column(name, False, bool)
    for name in nr.LVS_NUMERIC_COLUMNS:
        table[name] = column(name, np.nan, float)
    return table


def make_gaia_table(entries):
    """Column arrays keyed by GAIA_COLUMNS from a list of dicts.

    The in-memory stand-in for a Gaia slice, as GaiaReader returns it.
    Requires "ra", "dec"; "source_id" defaults to the row number, the
    other ints to 0, bools to False, "phot_variable_flag" to
    "NOT_AVAILABLE" (Gaia's "not analysed"), numerics to NaN.
    """
    def column(name, default, dtype):
        return np.array([e.get(name, default(i) if callable(default) else default)
                         for i, e in enumerate(entries, start=1)], dtype=dtype)

    table = {}
    for name in GAIA_INT_COLUMNS:
        table[name] = column(name, (lambda i: i) if name == "source_id" else 0, np.int64)
    for name in GAIA_BOOL_COLUMNS:
        table[name] = column(name, False, bool)
    for name in GAIA_STRING_COLUMNS:
        table[name] = column(name, "NOT_AVAILABLE", object)
    for name in GAIA_NUMERIC_COLUMNS:
        table[name] = column(name, np.nan, float)
    return table


def write_hp6_store(root, objects, complete=True):
    """Lay out `objects` (dicts: ra, dec, prefname, optional ptype/z/zunc/
    zflag) as order-6 pixel files under `root`, plus a manifest. Returns
    the pixels written."""
    by_pixel = {}
    for obj in objects:
        pix = int(hp.ang2pix(HP6_NSIDE, obj["ra"], obj["dec"], nest=True, lonlat=True))
        by_pixel.setdefault(pix, []).append(obj)
    for pix, objs in by_pixel.items():
        path = root / nc.hp6_file(pix)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({
            "prefname": [o["prefname"] for o in objs],
            "ra": [o["ra"] for o in objs], "dec": [o["dec"] for o in objs],
            "ptype": pa.array([o.get("ptype") for o in objs], pa.string()),
            "z": [o.get("z", np.nan) for o in objs],
            "zunc": [o.get("zunc", np.nan) for o in objs],
            "zflag": pa.array([o.get("zflag") for o in objs], pa.string()),
        }), path)
    (root / nc.MANIFEST_NAME).write_text(json.dumps(
        {"release": "NED_TEST", "hp6": {"complete": complete}}))
    return sorted(by_pixel)


def write_lvs_store(root, entries, release="LVS_TEST"):
    """Write the parquet and info that ``ingest-lvs`` would, from
    make_lvs_table entries (plus the `_hp6` column the reader uses)."""
    table = make_lvs_table(entries)
    hp6 = hp.ang2pix(2 ** nc.HP6_ORDER, table["ra"], table["dec"], nest=True, lonlat=True)
    arrow = pa.table({**{k: pa.array(v) for k, v in table.items()},
                      nc.LVS_HP6_COLUMN: pa.array(hp6.astype("int32"))})
    store = nc.Store(str(root))
    store.write_table(arrow, nc.LVS_PARQUET)
    store.write_text(nc.LVS_INFO, json.dumps({"release": release, "rows": len(entries)}))


def write_gaia_store(root, entries, partition_order=3, complete=True, ref_epoch=2016.0,
                     rows_per_group=2, extra_partitions=()):
    """Write the copy that ``ingest-gaia`` would, from make_gaia_table
    entries: one partition at `partition_order` holding every row (sorted
    by `_healpix_29`, small row groups so the cone read's row-group
    selection is exercised), the metadata files and the ingest record.
    `extra_partitions` are listed but not written."""
    table = make_gaia_table(entries)
    key = hp.ang2pix(2 ** nr.HP29_ORDER, table["ra"], table["dec"], nest=True, lonlat=True)
    order = np.argsort(key)
    arrow = pa.table({nr.GAIA_KEY: pa.array(key[order]),
                      "ref_epoch": pa.array(np.full(len(order), float(ref_epoch))),
                      **{k: pa.array(v[order]) for k, v in table.items()}})
    npix = int(key[0] >> (2 * (nr.HP29_ORDER - partition_order)))
    assert np.all(key >> (2 * (nr.HP29_ORDER - partition_order)) == npix), "one partition"
    store = nc.Store(str(root))
    rel = nc.gaia_leaf_rel(partition_order, npix)
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(arrow, root / rel, row_group_size=rows_per_group)
    leaves = [(partition_order, npix), *extra_partitions]
    store.write_text("partition_info.csv", "Norder,Npix\n" + "".join(f"{o},{p}\n" for o, p in leaves))
    store.write_text("hats.properties", "obs_collection=gaia_test\nhats_npix_suffix=.parquet\n")
    store.write_text(nc.GAIA_INFO, json.dumps({"release": "gaia_test", "complete": complete,
                                               "ref_epoch": ref_epoch}))
    return (partition_order, npix)
