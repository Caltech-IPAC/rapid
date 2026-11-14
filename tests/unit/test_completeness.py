"""scripts/completeness.py: the truth joined to a run's sources and packets.

Contracts: a detected source without a packet stays detected and is not
alerted; a truth row with no source inside the radius is not detected; a
packet on an injected source is classed injected, one on a bright-flagged
source bright, the rest other; the shift through the corrected WCS moves
the truth by the translation; an earlier alerted epoch absent from a later
packet's history is missing when the packet read its source set and late
when it did not; a source in the reference epochs is binned by its change
against their mean, one injected after the bound by its own magnitude; a
truth written with exposure names is matched to the run's ids through the
dataset's exposure_ids, and one written with packed ids through the legacy
mapping, with the same counts; a packed id that names two of the dataset's
exposures is refused, naming both, and nothing is mapped by guess.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "completeness", Path(__file__).resolve().parents[2] / "scripts" / "completeness.py")
completeness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(completeness)

PX = 0.11 / 3600.0       # degrees per pixel


def truth_frame(n_epochs=1, key="expid"):
    """Three sources per epoch; the epoch keyed by ``expid`` (the run's id 4001 + e, a truth already mapped)
    or by ``exposure`` (the name of pass 4 + e, a truth as inject_dataset.py writes it)."""
    rows = []
    for e in range(n_epochs):
        for i, (x, y, mag) in enumerate([(100.0, 100.0, 21.2), (300.0, 200.0, 24.1), (500.0, 900.0, 26.6)]):
            rows.append({"src_id": i, "x": x, "y": y, "ra": 10.0 + x * PX, "dec": y * PX, "mag_ab": mag,
                         "dnps": 10 ** (-0.4 * (mag - 26.861)), "zp": 26.861, "sca": 8, "mjd": 61592.0 + e,
                         **({"expid": 4001 + e} if key == "expid" else {"exposure": NAMES[e]})})
    return pd.DataFrame(rows)


#: The names of pass 4 and pass 5, observation 4, visit 1, exposure 1 of r00342, and the run's ids for them.
NAMES = ["r0034201004001004001_0001", "r0034201005001004001_0001"]
IDS = {NAMES[0]: 4001, NAMES[1]: 4002}
#: The ids the packing of the time before the serial gave those two exposures.
PACKED = {NAMES[0]: 4040101, NAMES[1]: 5040101}


def sources_frame():
    # source 1 at truth 0, source 2 at truth 1 (0.05 arcsec away), nothing near truth 2; source 3 a bright star
    return pd.DataFrame([
        {"job": 50, "sid": 1, "expid": 4001, "sca": 8, "ra": 10.0 + 100 * PX, "dec": 100 * PX, "fluxfit": 100.0,
         "flags": 0, "rb": 0.9, "in_field": True},
        {"job": 50, "sid": 2, "expid": 4001, "sca": 8, "ra": 10.0 + 300 * PX + 0.05 / 3600, "dec": 200 * PX,
         "fluxfit": 10.0, "flags": 0, "rb": 0.2, "in_field": False},
        {"job": 50, "sid": 3, "expid": 4001, "sca": 8, "ra": 11.0, "dec": 0.5, "fluxfit": 5000.0,
         "flags": completeness.BRIGHT_FOOTPRINT, "rb": None, "in_field": True}])


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


def test_a_source_in_the_reference_epochs_is_binned_by_its_change_against_them():
    zp = 26.861
    rows = [{"src_id": s, "mjd": m, "mag_ab": mag, "zp": zp} for s, m, mag in [
        (0, 1.0, 20.0), (0, 2.0, 20.0), (0, 5.0, 19.0),     # in the reference at 20, brightens to 19
        (1, 1.0, 22.0), (1, 5.0, 22.0),                      # static: no change to see
        (2, 5.0, 24.0)]]                                     # injected after the bound only
    t = completeness.difference_flux(pd.DataFrame(rows), 3.0).set_index(["src_id", "mjd"])
    change = 10 ** (-0.4 * 19.0) - 10 ** (-0.4 * 20.0)
    assert np.isclose(t.loc[(0, 5.0), "mag_diff"], -2.5 * np.log10(change))
    assert np.isclose(t.loc[(0, 5.0), "dnps_diff"], change * 10 ** (0.4 * zp))
    assert np.isinf(t.loc[(1, 5.0), "mag_diff"])
    assert np.isclose(t.loc[(2, 5.0), "mag_diff"], 24.0)
    unbounded = completeness.difference_flux(pd.DataFrame(rows), None)
    assert np.allclose(unbounded["mag_diff"], unbounded["mag_ab"])


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


class _Cursor:
    """A psycopg2 cursor stand-in: records each query, serves its rows in fetchmany chunks."""

    def __init__(self, log, rows_for, name=None, **kw):
        self.log, self.rows_for, self.name, self.description = log, rows_for, name, None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.log.append((self.name, sql, params))
        cols, rows = self.rows_for(sql)
        self.description = [(c,) for c in cols]
        self._rows = list(rows)

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchmany(self, n):
        rows, self._rows = self._rows[:n], self._rows[n:]
        return rows


class _Conn:
    def __init__(self, rows_for):
        self.log, self.rows_for = [], rows_for

    def cursor(self, name=None, cursor_factory=None):
        return _Cursor(self.log, self.rows_for, name)

    def commit(self):
        pass


def test_load_restricts_the_catalogs_in_sql_and_reads_them_through_a_named_cursor(monkeypatch):
    import sys, types
    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(extras=types.SimpleNamespace(RealDictCursor=None)))
    monkeypatch.setitem(sys.modules, "psycopg2.extras", sys.modules["psycopg2"].extras)
    monkeypatch.setattr(completeness, "CHUNK", 2)
    settings = {"packet": {"alert_catalog": "xsources"}, "intake": {"source_root": "s3://b/d/l2/"}}

    def rows_for(sql):
        if sql.startswith("SELECT dataset"):
            return ["dataset", "settings"], [{"dataset": "d", "settings": settings}]
        if "FROM exposure_ids" in sql:
            return ["name", "expid"], [{"name": n, "expid": e} for n, e in IDS.items()]
        if "sources_of" in sql:
            cols = ["job", "sid", "expid", "sca", "ra", "dec", "fluxfit", "flags", "rb", "in_field"]
            return cols, [(50, i, 4001, 8, 10.0, 0.0, 1.0, 0, 0.5, True) for i in range(5)]
        if "packets_of" in sql:
            cols = ["job", "source_job", "sid", "object_job", "aid", "expid", "sca", "trigger_mjd",
                    "filename", "byte_offset", "byte_length", "sent"]
            return cols, [(60, 50, 1, 50, 901, 4001, 8, 61592.0, "f", 0, 10, True)]
        return ["expid", "sca", "metric", "value"], []

    conn = _Conn(rows_for)
    data = completeness.load(conn, 187, {4001, 4002})
    assert data["names"] == IDS                        # the dataset's exposure_ids, for the truth's names
    # the two catalogs went through a named (server-side) cursor, assembled from CHUNK-row fetches
    named = {name: (sql, params) for name, sql, params in conn.log if name}
    assert set(named) == {"completeness_sources", "completeness_packets"}
    for sql, params in named.values():
        assert "expid = ANY(%s)" in sql and params == (187, [4001, 4002])
    assert "xsources_of" in named["completeness_sources"][0]
    assert len(data["sources"]) == 5 and list(data["sources"]["sid"]) == [0, 1, 2, 3, 4]
    assert list(data["sources"].columns)[:3] == ["job", "sid", "expid"]
    assert len(data["packets"]) == 1 and bool(data["packets"]["sent"][0])
    # the small queries stay plain; without --expids the catalogs are unrestricted
    assert all(name is None for name, sql, _ in conn.log if "runs WHERE" in sql)
    conn.log.clear()
    completeness.load(conn, 187, None)
    for name, sql, params in conn.log:
        if name:
            assert "ANY" not in sql and params == (187,)


def test_a_truth_with_names_and_one_with_packed_ids_match_the_runs_ids_with_the_same_counts():
    named = completeness.truth_ids(truth_frame(2, key="exposure"), IDS)
    assert named["expid"].tolist() == [4001] * 3 + [4002] * 3 and named["exposure"].tolist()[0] == NAMES[0]
    legacy = truth_frame(2, key="exposure").drop(columns="exposure")
    legacy["expid"] = [PACKED[n] for n in [NAMES[0]] * 3 + [NAMES[1]] * 3]
    packed = completeness.truth_ids(legacy, IDS)
    assert packed["expid"].tolist() == named["expid"].tolist()
    assert packed["exposure"].tolist() == [str(PACKED[NAMES[0]])] * 3 + [str(PACKED[NAMES[1]])] * 3
    s = sources_frame()
    counts = [completeness.join(completeness.match(completeness.corrected_positions(t, (0.0, 0.0)), s, 0.3),
                                s, packets_frame())[["detected", "alerted"]].sum().tolist()
              for t in (named.astype({"expid": int}), packed.astype({"expid": int}))]
    assert counts[0] == counts[1] == [2, 1]
    # A name the dataset has no id for keeps none: its rows are unprocessed, never matched by guess.
    stranger = completeness.truth_ids(pd.DataFrame({"exposure": ["r0034201009001004001_0001"], "sca": [8]}), IDS)
    assert stranger["expid"].isna().all()


def test_a_packed_id_that_names_two_exposures_of_the_dataset_is_refused_naming_both():
    plans = {"r0104301001001001001_0001": 11, "r0104302001001001001_0001": 12, NAMES[0]: 4001}
    assert completeness.legacy_ids(plans, {4040101}) == {4040101: 4001}      # the clash is not asked for
    with pytest.raises(ValueError, match="r0104301001001001001_0001, r0104302001001001001_0001"):
        completeness.legacy_ids(plans, {1010101, 4040101})
    assert completeness.legacy_ids(plans, {7070707}) == {}                  # no exposure packs to it
