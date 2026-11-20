"""scripts/inspect_run.py's tables and gallery sample, on faked ``job_metrics`` rows.

One table per stage: a row per job labelled by its scope (an image's id and
SCA; an intake's file, though its scope carries the id too), a column per
metric, a metric a job lacks left blank, the run's median and each row's
deviation from it; the sample is the first and last epoch by the
difference's ``jd``, never by the id, and every epoch above twice the median
bright count; the thumbnail keeps each block's
largest-magnitude pixel; the cutouts are the residual stars, or without
them the kept sources of highest S/N inside the image.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "inspect_run", Path(__file__).resolve().parents[2] / "scripts" / "inspect_run.py")
inspect_run = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inspect_run)


def row(job, stage, metric, value, expid=None, field=None, file=None, jd=None):
    return {"job": job, "stage": stage, "expid": expid, "sca": 8 if expid and not file else None, "fid": 8,
            "field": field, "file": file, "metric": metric, "value": value, "jd": jd}


# The ids are serials that carry no meaning: the latest epoch (jd) has the lowest id.
ROWS = [
    row(1, "difference", "n_sources_snr_gt20", 10, expid=104, jd=2461592.5),
    row(1, "difference", "core_fraction", 0.002, expid=104, jd=2461592.5),
    row(2, "difference", "n_sources_snr_gt20", 12, expid=102, jd=2461593.5),
    row(3, "difference", "n_sources_snr_gt20", 40, expid=103, jd=2461594.5),
    row(4, "difference", "n_sources_snr_gt20", 11, expid=101, jd=2461595.5),
    row(5, "intake", "masked_fraction", 0.0004, expid=104, file="r00342_f146"),
    row(6, "reference", "gmax", 9135.0, field=5387158),
]


def test_one_table_per_stage_with_the_median_and_each_rows_deviation():
    out = inspect_run.tables(ROWS)
    assert set(out) == {"difference", "intake", "reference"}
    lines = out["difference"].splitlines()
    assert lines[0] == "| job | scope | core_fraction | n_sources_snr_gt20 |"
    assert lines[2] == "| 4 | 101 sca 8 |  | 11 |"      # rows in label order; absent, never zero
    assert lines[3] == "| 2 | 102 sca 8 |  | 12 |"
    assert lines[5] == "| 1 | 104 sca 8 | 0.002 | 10 |"
    assert lines[6] == "| median | | 0.002 | 11.5 |"
    assert lines[9] == "| 3 − median | 103 sca 8 |  | +28.5 |"
    assert "| 6 | field 5387158 | 9135 |" in out["reference"]
    assert "| 5 | r00342_f146 | 0.0004 |" in out["intake"]


def test_the_gallery_samples_first_last_and_bright_epochs_by_jd():
    assert inspect_run.sample(ROWS) == [1, 3, 4]        # first and last by jd (jobs 1 and 4), the outlier 3
    assert inspect_run.sample([r for r in ROWS if r["stage"] != "difference"]) == []


def test_the_thumbnail_keeps_each_blocks_largest_magnitude_pixel():
    image = np.zeros((16, 16))
    image[1, 2], image[9, 12] = 3.0, -7.0
    thumb = inspect_run.thumbnail(image, 8)
    assert thumb.tolist() == [[3.0, 0.0], [0.0, -7.0]]


def test_a_cutouts_star_flux_is_its_aperture_sum_less_the_sky():
    import numpy as np

    cut = np.full((31, 31), 2.0)
    cut[15, 15] += 500.0
    assert inspect_run.star_flux(cut) == pytest.approx(500.0)


def test_the_cutouts_are_the_residual_stars_else_the_kept_sources_of_highest_snr():
    residual = [{"x": 100.0, "y": 200.0}]
    assert inspect_run.cutout_stars(residual, [(50.0, 50.0, 900.0)], (400, 400)) == (residual, "residual stars")
    kept = [(51.0, 61.0, 30.0), (3.0, 200.0, 999.0), (101.0, 101.0, 80.0)]   # one-based; the second at the edge
    stars, chosen = inspect_run.cutout_stars([], kept, (400, 400))
    assert [(s["x"], s["y"]) for s in stars] == [(100.0, 100.0), (50.0, 60.0)]
    assert chosen.startswith("kept sources")
