"""scripts/completeness.py: the truth joined to a run's sources and packets.

Contracts: a detected source without a packet stays detected and is not
alerted; a truth row with no source inside the radius is not detected; a
packet on an injected source is classed injected, one on a bright-flagged
source bright, the rest other; the shift through the corrected WCS moves
the truth by the translation; an earlier alerted epoch absent from a later
packet's history is missing when the packet read its source set and late
when it did not.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

_SPEC = importlib.util.spec_from_file_location(
    "completeness", Path(__file__).resolve().parents[2] / "scripts" / "completeness.py")
completeness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(completeness)

PX = 0.11 / 3600.0       # degrees per pixel


def truth_frame(n_epochs=1):
    rows = []
    for e in range(n_epochs):
        for i, (x, y, mag) in enumerate([(100.0, 100.0, 21.2), (300.0, 200.0, 24.1), (500.0, 900.0, 26.6)]):
            rows.append({"src_id": i, "x": x, "y": y, "ra": 10.0 + x * PX, "dec": y * PX, "mag_ab": mag,
                         "dnps": 10 ** (-0.4 * (mag - 26.861)), "zp": 26.861, "expid": 4001 + e, "sca": 8,
                         "mjd": 61592.0 + e})
    return pd.DataFrame(rows)


def sources_frame():
    # source 1 at truth 0, source 2 at truth 1 (0.05 arcsec away), nothing near truth 2; source 3 a bright star
    return pd.DataFrame([
        {"job": 50, "sid": 1, "expid": 4001, "sca": 8, "ra": 10.0 + 100 * PX, "dec": 100 * PX, "fluxfit": 100.0,
         "flags": 0},
        {"job": 50, "sid": 2, "expid": 4001, "sca": 8, "ra": 10.0 + 300 * PX + 0.05 / 3600, "dec": 200 * PX,
         "fluxfit": 10.0, "flags": 0},
        {"job": 50, "sid": 3, "expid": 4001, "sca": 8, "ra": 11.0, "dec": 0.5, "fluxfit": 5000.0,
         "flags": completeness.BRIGHT_FOOTPRINT}])


def packets_frame(sids=(1, 3)):
    return pd.DataFrame([{"job": 60, "source_job": 50, "sid": s, "object_job": 50, "aid": 900 + s,
                          "expid": 4001, "sca": 8, "trigger_mjd": 61592.0, "sent": True} for s in sids])


def test_detected_without_a_packet_stays_detected_not_alerted():
    truth = completeness.corrected_positions(truth_frame(), (0.0, 0.0))
    s = sources_frame()
    rows = completeness.join(completeness.match(truth, s, 0.3), s, packets_frame()).sort_values("src_id")
    assert rows["detected"].tolist() == [True, True, False]
    assert rows["alerted"].tolist() == [True, False, False]
    assert rows["sent"].tolist() == [True, False, False]
    assert np.isclose(rows["offset_arcsec"].iloc[1], 0.05, atol=1e-3)


def test_purity_classes():
    truth = completeness.corrected_positions(truth_frame(), (0.0, 0.0))
    s = sources_frame()
    rows = completeness.join(completeness.match(truth, s, 0.3), s, packets_frame())
    keys = {(int(j), int(i)) for j, i in zip(rows.loc[rows["detected"], "job"], rows.loc[rows["detected"], "sid"])}
    p = packets_frame((1, 2, 3)).merge(s[["job", "sid", "fluxfit", "flags"]].rename(columns={"job": "source_job"}),
                                       on=["source_job", "sid"])
    p = p.assign(flags=[0, 0, completeness.BRIGHT_FOOTPRINT])
    classed = completeness.purity(p, keys - {(50, 2)}, {(4001, 8): 26.861})
    assert classed.set_index("sid")["class"].to_dict() == {1: "injected", 2: "other", 3: "bright"}


def test_shift_moves_the_truth_by_the_translation():
    truth = truth_frame()
    moved = completeness.corrected_positions(truth, (0.12, -0.99))
    assert np.allclose((moved["ra_c"] - truth["ra"]) / PX, -0.12, atol=1e-6)
    assert np.allclose((moved["dec_c"] - truth["dec"]) / PX, 0.99, atol=1e-6)


def test_history_missing_against_late():
    rows = pd.DataFrame([
        {"src_id": 0, "mjd": 1.0, "detected": True, "alerted": True, "job": 50, "sid": 1, "packet_job": 60,
         "aid": 7, "expid": 1},
        {"src_id": 0, "mjd": 2.0, "detected": True, "alerted": True, "job": 51, "sid": 2, "packet_job": 61,
         "aid": 7, "expid": 2},
        {"src_id": 0, "mjd": 3.0, "detected": True, "alerted": True, "job": 52, "sid": 3, "packet_job": 62,
         "aid": 7, "expid": 3}])
    records = {(61, 2): set(), (62, 3): {1}}
    read = {61: {50}, 62: {50}}            # 62 never read set 51: published after its read
    h = completeness.history(rows, records, read).set_index("packet_job")
    assert h.loc[61, ["expected", "present", "missing", "late"]].tolist() == [1, 0, 1, 0]
    assert h.loc[62, ["expected", "present", "missing", "late"]].tolist() == [2, 1, 0, 1]
