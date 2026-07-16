"""Shared fixtures for the rapid_alerts test suite.

The strategy: DatabaseProvider only needs (a) an object exposing
.conn.cursor() and (b) product files it can reach with a plain path (its
_stage() passes non-s3:// paths straight through). So a FakeDB routing
canned rows plus a synthetic on-disk "job directory" exercise the entire
real production path -- SQL -> Source records -> assembly -> cutouts ->
Avro -- with no network and no database.

Fixture map (all function-scoped unless noted):

    tpv_header    dict of WCS cards: realistic TPV solution (linear terms
                  from a real chip, plus nonzero distortion terms so that
                  CRPIX-shift bugs cannot hide behind a linear WCS)
    chip_image    301x301 float32 base image; every pixel value is unique
                  (row*1000 + col) so pixel-identity asserts are exact
    job_dir       tmp dir with the four pipeline products the cutout path
                  knows about (sfft/zogy diff, science, template), all on
                  the tpv_header grid, each offset by a distinct constant
                  so tests can tell which file a stamp came from
    chip_data     in-memory "database" for one chip: sources (with ra/dec
                  computed from the TPV WCS at xfit+1/yfit+1, exactly the
                  invariant the real DB satisfies), objects, associations,
                  and detection history
    make_provider factory -> DatabaseProvider(FakeDB(chip_data), ...);
                  a factory so tests can build independent providers
"""

import sys
from pathlib import Path

# make `rapid_alerts` and the test helpers importable no matter where
# pytest is invoked from
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fitsio
import numpy as np
import pytest

from wcs_eval import tpv_pixel_to_sky

# per-product DC offsets added to chip_image, so a stamp's values identify
# its source file (see job_dir)
PRODUCT_OFFSETS = {
    "sfftdiffimage_masked.fits": 0.0,
    "zogy_diffimage_masked.fits": 50_000.0,
    "bkg_subbed_science_image.fits": 100_000.0,
    "awaicgen_output_mosaic_image_resampled_gainmatched.fits": 200_000.0,
}

CHIP_PID = 99          # the fake chip's diffimages.pid
CHIP_FIELD = 3         # all fake sources live in Roman field 3


@pytest.fixture(scope="session")
def tpv_header():
    # Linear terms lifted from a real RAPID chip; distortion terms are
    # invented but of realistic magnitude. Tests use the same evaluator
    # on both sides, so only self-consistency matters -- but the nonzero
    # PV terms are load-bearing: with a purely linear WCS, a wrong CRPIX
    # shift in the clip header could cancel out and go undetected.
    return {
        "CTYPE1": "RA---TPV", "CTYPE2": "DEC--TPV",
        "CRVAL1": 268.08938824487, "CRVAL2": -29.873768896269,
        "CRPIX1": 150.5, "CRPIX2": 150.5,
        "CD1_1": -2.6070967584643e-05, "CD1_2": 1.5264631506377e-05,
        "CD2_1": 1.464455793042e-05, "CD2_2": 2.4763608881277e-05,
        "PV1_0": 1.1e-06, "PV1_1": 1.00021, "PV1_2": -3.2e-05,
        "PV1_4": 2.1e-04, "PV1_5": -8.6e-05, "PV1_7": 1.9e-04,
        "PV2_0": -0.9e-06, "PV2_1": 1.00018, "PV2_2": 2.7e-05,
        "PV2_4": 1.8e-04, "PV2_6": 7.3e-05, "PV2_10": -1.2e-04,
    }


@pytest.fixture(scope="session")
def chip_image():
    rows, cols = np.mgrid[0:301, 0:301]
    return (rows * 1000 + cols).astype(np.float32)


@pytest.fixture()
def job_dir(tmp_path, tpv_header, chip_image):
    """A synthetic pipeline job directory with all four cutout products."""
    for name, offset in PRODUCT_OFFSETS.items():
        fitsio.write(str(tmp_path / name), chip_image + offset,
                     header=dict(tpv_header), clobber=True)
    return tmp_path


# ---------------------------------------------------------------------------
# The fake chip: sources, objects, associations, history
# ---------------------------------------------------------------------------

def make_source_row(sid, xfit, yfit, mjdobs, tpv_header, pid=CHIP_PID):
    """One sources-table row with every column Source.from_row(strict=True)
    demands. ra/dec are computed from the TPV WCS at (xfit+1, yfit+1) --
    xfit/yfit are 0-based, the WCS is 1-based -- which is exactly the
    relation the real database satisfies (measured at 0.0 mas over real
    sources; see test_live_db.py)."""
    ra, dec = tpv_pixel_to_sky(tpv_header, xfit + 1.0, yfit + 1.0)
    return {
        "sid": sid, "expid": 42, "sca": 7, "mjdobs": mjdobs,
        "ra": ra, "dec": dec, "xfit": xfit, "yfit": yfit,
        "filter_name": "F158",             # provider derives band from this
        "xerr": 0.01, "yerr": 0.02, "fluxfit": 1234.5, "fluxerr": 56.7,
        "flags": 0, "field": CHIP_FIELD, "hp6": 123, "hp9": 4567,
        "pid": pid, "isdiffpos": True, "qfit": 0.1, "cfit": 0.05,
        "redchi": 1.2, "npixfit": 25, "sharpness": 0.4,
        "roundness1": 0.1, "roundness2": -0.05, "peak": 321.0,
    }


class ChipData:
    """In-memory stand-in for the database tables behind one chip."""

    def __init__(self, tpv_header, job_dir):
        # diffimages.filename: the DB stores the zogy path; the provider
        # derives the job directory from it and picks the flavored file
        self.diff_filename = str(job_dir / "zogy_diffimage_masked.fits")

        # diffimages rows for resolve_pid(expid, sca): reprocessing
        # campaigns leave several rows per (expid, sca), more than one
        # of them with vbest=1 -- mirroring the real database. The
        # newest vbest>0 row is CHIP_PID, the one the chip's sources
        # belong to; pid 100 is newer still but vbest=0 (not best).
        self.campaigns = [
            {"pid": 77, "expid": 42, "sca": 7, "vbest": 1},
            {"pid": CHIP_PID, "expid": 42, "sca": 7, "vbest": 1},
            {"pid": 100, "expid": 42, "sca": 7, "vbest": 0},
        ]

        # Three on-chip detections: two associated with objects (one shared
        # object would also be legal; kept distinct for clarity), one
        # unassociated. Positions include a fractional part so rounding is
        # always exercised.
        self.sources = [
            make_source_row(9001, 150.3, 200.6, 60500.5, tpv_header),
            make_source_row(9002, 40.9, 60.2, 60500.5, tpv_header),
            make_source_row(9003, 260.1, 111.7, 60500.5, tpv_header),
        ]
        # merges_<field>: sid -> aid (9003 stays unassociated)
        self.merges = {9001: 777, 9002: 888}
        # astroobjects_<field>: aid -> object row (the full storage row;
        # the SELECT-list projection in FakeCursor trims it per query)
        self.objects = {
            777: {"aid": 777, "ra0": 268.09, "dec0": -29.88,
                  "stdevra": 1.5e-05, "stdevdec": 1.2e-05, "nsources": 3,
                  "meanra": 268.09, "meandec": -29.88, "flux0": 1200.0},
            888: {"aid": 888, "ra0": 268.10, "dec0": -29.87,
                  "stdevra": 2.5e-05, "stdevdec": 2.1e-05, "nsources": 1,
                  "meanra": 268.10, "meandec": -29.87, "flux0": 800.0},
        }
        # Prior detections (other sids of the same objects, earlier mjd,
        # different pid -- they belong to older chips). Object 777 has two,
        # one of them older than typical look-back windows tests may use;
        # object 888 has none.
        self.history = [
            make_source_row(1001, 150.0, 200.0, 60480.5, tpv_header, pid=42),
            make_source_row(1002, 150.1, 200.2, 60490.5, tpv_header, pid=43),
        ]
        self.history_merges = {1001: 777, 1002: 777}

    def _all_detections(self):
        return self.sources + self.history

    def _aid_of(self, sid):
        return {**self.merges, **self.history_merges}.get(sid)


def _selected_columns(sql):
    """The column names a query's SELECT list actually asks for, stripped
    of table aliases ("a.ra0" -> "ra0", "x AS y" -> "y"); None when the
    list contains a '*' (no projection possible)."""
    select_list = sql.split("FROM")[0].split("SELECT")[1]
    columns = []
    for item in select_list.split(","):
        item = item.strip()
        if "*" in item:
            return None
        columns.append(item.split()[-1].split(".")[-1].lower()
                       if " as " in item.lower()
                       else item.split(".")[-1].lower())
    return columns


class FakeCursor:
    """Answers DatabaseProvider._query()'s SQL from a ChipData.

    Routing is by SQL substring -- brittle on purpose: if the provider's
    queries change shape, the KeyError here says "teach the fake about the
    new query" rather than silently returning wrong rows.

    Results are projected onto the query's actual SELECT list (see
    execute), so a provider bug where a set-based prefetch forgets to
    SELECT a newly added column fails here, in the fast suite, instead of
    only in a live batch run. (SELECT-* queries can't be projected and
    return the full fake row, just like the real database.)
    """

    def __init__(self, data):
        self.data = data
        self._rows = []

    # -- the dispatch table ------------------------------------------------
    def execute(self, sql, params):
        d = self.data
        if "vbest" in sql:                            # resolve_pid(expid, sca)
            expid, sca = params
            best = sorted((c for c in d.campaigns
                           if c["expid"] == expid and c["sca"] == sca
                           and c["vbest"] > 0),
                          key=lambda c: -c["pid"])
            self._rows = [{"pid": c["pid"]} for c in best]
        elif "FROM diffimages" in sql:
            self._rows = ([{"filename": d.diff_filename}]
                          if d.diff_filename else [])
        elif "WHERE s.sid" in sql:                    # get_detection(sid)
            self._rows = [dict(r) for r in d._all_detections()
                          if r["sid"] == params[0]]
        elif "WHERE s.pid" in sql:                    # iter_sources(pid)
            self._rows = sorted((dict(r) for r in d.sources
                                 if r["pid"] == params[0]),
                                key=lambda r: r["sid"])
        elif "object_aid" in sql:                     # batch history prefetch
            aids, cutoff = params
            self._rows = sorted(
                ({"object_aid": d._aid_of(r["sid"]), **r}
                 for r in d._all_detections()
                 if d._aid_of(r["sid"]) in aids and r["mjdobs"] >= cutoff),
                key=lambda r: r["mjdobs"])
        elif "m.sid = ANY" in sql:                    # batch object prefetch
            self._rows = [{"sid": sid, **d.objects[d._aid_of(sid)]}
                          for sid in params[0] if d._aid_of(sid) is not None]
        elif "WHERE m.sid" in sql:                    # single-alert object
            aid = d._aid_of(params[0])
            self._rows = [dict(d.objects[aid])] if aid is not None else []
        elif "m.aid = %s" in sql:                     # single-alert prv
            aid, trigger_sid, cutoff = params
            self._rows = sorted(
                (dict(r) for r in d._all_detections()
                 if d._aid_of(r["sid"]) == aid and r["sid"] != trigger_sid
                 and r["mjdobs"] >= cutoff),
                key=lambda r: r["mjdobs"])
        else:
            raise KeyError(f"FakeCursor has no route for query: {sql}")

        # deliver only the columns the SQL asked for -- a KeyError here
        # means the query names a column the fake rows don't carry (teach
        # ChipData about it)
        columns = _selected_columns(sql)
        if columns is not None:
            self._rows = [{c: row[c] for c in columns} for row in self._rows]

    # -- cursor protocol used by _query -------------------------------------
    @property
    def description(self):
        return [(k,) for k in self._rows[0]] if self._rows else []

    def fetchall(self):
        return [tuple(r.values()) for r in self._rows]

    def close(self):
        pass


class FakeDB:
    """Duck-types RAPIDDB: just .conn.cursor()."""

    class _Conn:
        def __init__(self, data):
            self._data = data

        def cursor(self):
            return FakeCursor(self._data)

    def __init__(self, data):
        self.conn = self._Conn(data)


@pytest.fixture()
def chip_data(tpv_header, job_dir):
    return ChipData(tpv_header, job_dir)


@pytest.fixture()
def make_provider(chip_data):
    """Factory for independent DatabaseProviders over the same fake chip."""
    from rapid_alerts.providers import DatabaseProvider

    def _make(diff_flavor="sfft"):
        return DatabaseProvider(FakeDB(chip_data), diff_flavor=diff_flavor)

    return _make
