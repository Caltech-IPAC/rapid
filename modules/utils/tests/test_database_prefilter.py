"""
Tests of the database-loading helpers for the extra catalog columns (catalog_float_values,
catalog_values_for_copy, prefilter_database_records, and get_database_prefilter_params in
modules/utils/rapid_pipeline_subs.py), used by pipeline/loadPSFCatIntoDBSourcesTable.py and
pipeline/loadSECatIntoDBSourcesTable.py.

Run from the repository root:  python -m pytest modules/utils/tests
"""

import configparser

import numpy as np
from astropy.table import QTable, MaskedColumn
import astropy.units as u

import modules.utils.rapid_pipeline_subs as util


FILL = -999.0


def test_catalog_float_values_missing():
    t = QTable({"sumrat": [0.5, FILL, np.nan, 0.1]})
    v = util.catalog_float_values(t, "sumrat", (FILL,))
    assert np.allclose(v, [0.5, np.nan, np.nan, 0.1], equal_nan=True)
    assert util.catalog_float_values(t, "rb_score", (FILL,)) is None


def test_catalog_float_values_units_and_mask():
    t = QTable()
    t["X"] = [1.0, 2.0] * u.pix
    t["M"] = MaskedColumn([3.0, 4.0], mask=[False, True])
    assert np.allclose(util.catalog_float_values(t, "X"), [1.0, 2.0])
    assert np.allclose(util.catalog_float_values(t, "M"), [3.0, np.nan], equal_nan=True)


def test_catalog_values_for_copy():
    t = QTable({"rb_score": [0.25, FILL], "nneg": [3, FILL], "rb_label": [1, -1]})
    assert list(util.catalog_values_for_copy(t, "rb_score", fill_values=(FILL,))) == ["0.25", "\\N"]
    assert list(util.catalog_values_for_copy(t, "nneg", integer=True, fill_values=(FILL,))) == ["3", "\\N"]
    # -1 is a real label value (not scored), not a fill value.
    assert list(util.catalog_values_for_copy(t, "rb_label", integer=True, fill_values=(FILL,))) == ["1", "-1"]
    # A column the catalog lacks is loaded as NULL.
    assert list(util.catalog_values_for_copy(t, "absent", fill_values=(FILL,))) == ["\\N", "\\N"]


def test_prefilter_cuts():
    sumrat = np.array([0.5, 0.4, 0.3, 0.9, 0.9])
    rb = np.array([0.6, 0.6, 0.6, 0.0, 0.01])
    keep = util.prefilter_database_records(5, sumrat, rb, 0.4, 0.0)
    # sumrat must be > 0.4 and rb > 0.0 (both strict).
    assert list(keep) == [True, False, False, False, True]


def test_prefilter_missing_passes():
    sumrat = np.array([np.nan, 0.1, 0.9])
    rb = np.array([np.nan, np.nan, np.nan])
    assert list(util.prefilter_database_records(3, sumrat, rb, 0.4, 0.0)) == [True, False, True]
    # Columns absent (older catalogs, or RB not run): nothing is rejected for them.
    assert list(util.prefilter_database_records(3, None, None, 0.4, 0.0)) == [True, True, True]
    assert list(util.prefilter_database_records(3, sumrat, None, 0.4, 0.0)) == [True, False, True]
    # No thresholds (section missing): nothing is rejected.
    assert list(util.prefilter_database_records(3, sumrat, rb, None, None)) == [True, True, True]


def test_get_database_prefilter_params():
    c = configparser.ConfigParser()
    c.read_string("[DATABASE_PREFILTER]\nsumrat_prefilter_threshold = 0.4\nrb_prefilter_threshold = 0.0\n"
                  "[SUMRAT]\nfill_value = -999.0\n[RUBRAT]\nfill_value = -99.0\n")
    s, r, fills = util.get_database_prefilter_params(c)
    assert (s, r) == (0.4, 0.0)
    assert fills == (-999.0, -99.0)

    s, r, fills = util.get_database_prefilter_params(configparser.ConfigParser())
    assert (s, r, fills) == (None, None, (-999.0,))


def test_repo_ini_prefilter_section():
    c = configparser.ConfigParser()
    c.read("cdf/awsBatchSubmitJobs_launchSingleSciencePipeline.ini")
    s, r, fills = util.get_database_prefilter_params(c)
    assert (s, r) == (0.4, 0.0)
    assert fills == (-999.0,)
