"""scripts/completeness_ou.py: an OpenUniverse run's sources and packets joined to the per-image truth.

Contracts: the injected set is the index's transient rows with realised
counts above zero inside the frame, at zero-based ``x - 1, y - 1``, with AB
``mag`` + 2.5 log10(37570 x EXPTIME); a transient with counts in a reference
frame is ``reference`` and one that started before the reference's last
frame is ``early``, whatever else, the rest ``evaluated``; a packet's class
takes a transient with counts first, then the bright-footprint flag, then
a galaxy, then a star, else ``other``.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pandas as pd

_SPEC = importlib.util.spec_from_file_location(
    "completeness_ou", Path(__file__).resolve().parents[2] / "scripts" / "completeness_ou.py")
ou = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ou)

INDEX = """#    object_id  ra  dec  x  y  realized_flux  flux  mag  obj_type
  1  9.30  -44.0  101.0  201.0  500.0  510.0  8.0  transient
  2  9.31  -44.0  50.0   60.0   0.0    0.0    99.0 transient
  3  9.32  -44.0  4100.0 60.0   400.0  400.0  8.5  transient
  4  9.33  -44.0  300.0  300.0  900.0  900.0  7.4  galaxy
  5  9.34  -44.0  400.0  400.0  9e5    9e5    0.1  star
"""


def test_the_injected_set_is_the_lit_transients_in_the_frame_zero_based_in_ab():
    rows = ou.injected(ou.read_index(INDEX), 302.275)
    assert rows["object_id"].tolist() == [1]
    assert (rows["x0"].iloc[0], rows["y0"].iloc[0]) == (100.0, 200.0)
    assert math.isclose(rows["mag_ab"].iloc[0], 8.0 + 2.5 * math.log10(37570 * 302.275))
    assert math.isclose(rows["dnps"].iloc[0], 500.0 / 302.275)


def test_groups_put_reference_light_and_early_starts_apart():
    rows = pd.DataFrame({"object_id": [1, 2, 3, 4]})
    got = ou.groups(rows, {1: 62100.0, 2: 62100.0, 3: 62000.0}, in_reference={2}, reference_end=62026.3)
    assert got.tolist() == ["evaluated", "reference", "early", "early"]   # 4: no start known


def test_a_packets_class_takes_the_transient_then_the_flag_then_galaxy_then_star():
    d = 0.11 / 3600.0
    index = pd.DataFrame({"expid": 7, "sca": 10, "ra_c": [9.0, 9.0, 9.1, 9.2, 9.3],
                          "dec_c": [-44.0, -44.0, -44.0, -44.0, -44.0],
                          "obj_type": ["transient", "galaxy", "galaxy", "star", "transient"],
                          "realized_flux": [10.0, 0.0, 0.0, 0.0, 0.0]})
    packets = pd.DataFrame({"expid": 7, "sca": 10, "ra": [9.0, 9.1, 9.1, 9.2, 9.3, 9.5],
                            "dec": [-44.0 + d / 2, -44.0, -44.0, -44.0, -44.0, -44.0],
                            "flags": [16384, 16384, 0, 0, 0, 0]})
    assert ou.classify(packets, index, 0.165).tolist() == ["transient", "bright", "galaxy", "star", "other",
                                                          "other"]
