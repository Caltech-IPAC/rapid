"""Section C of the test plan: schema registry consistency, alert
assembly semantics, and Avro serialization (migrated from the old
test_rapid_alerts.py script; same coverage, one test per concern).

These run against a minimal hand-rolled AlertDataProvider rather than the
fake database -- assembly semantics don't care where records come from,
and the simpler provider keeps failures here pointing at produce.py, not
at fixture plumbing.

TODO (test plan, not yet implemented):
  C13 load_schema staleness detection: stale latest.txt / drifted file /
      missing file each raise the clear RuntimeError; explicit old
      version still loads without the check
  C14 value-domain round-trip: NaN policy in nullable floats, very
      large/negative fluxes, isdiffpos=False -> isNegative=True
  C15 size guard: an alert with three real-size clips and a fat prv
      history stays well under Kafka's configured message.max.bytes
"""

import io
import time

import fastavro
import pytest

from alerts.gen_schema import generate
from alerts.param_registry import RECORDS, VERSION, Status
from alerts.produce import (assemble_alert, build_dia_source,
                                  build_dia_forced_source, load_schema,
                                  serialize_alert)
from alerts.providers import (Cutouts, ForcedPhot, LvsMatch, NedMatch,
                                    ObjectRecord, RefMatch, Source, SSMatch)


def make_detection(sid, mjd, aid=None):
    return Source(
        sid=sid, expid=42, sca=7, mjdobs=mjd, ra=150.1, dec=2.2,
        xfit=101.5, yfit=202.5, band="F158", aid=aid,
        xerr=0.01, yerr=0.02, fluxfit=1234.5, fluxerr=56.7,
        flags=0, field=3, hp6=123, hp9=4567, pid=99, isdiffpos=True,
        qfit=0.1, cfit=0.05, redchi=1.2, npixfit=25,
        sharpness=0.4, roundness1=0.1, roundness2=-0.05, peak=321.0,
    )


class MinimalProvider:
    """Hand-rolled records; no database, no files, no cutout machinery.

    Duck-typed: assemble_alert() only calls these get_* methods, so this
    needs no provider base class."""

    def get_detection(self, sid):
        return make_detection(sid, mjd=60500.5)

    def get_object_for_source(self, detection):
        return ObjectRecord(aid=777, ra0=150.1, dec0=2.2,
                            stdevra=1.5e-05, stdevdec=1.2e-05, nsources=3)

    def get_prv_detections(self, detection, obj, window_days=365.25):
        return [make_detection(1001, mjd=60480.5, aid=obj.aid),
                make_detection(1002, mjd=60490.5, aid=obj.aid)]

    def get_forced_photometry(self, detection, obj):
        return []

    def get_ss_matches(self, detection):
        # mirror the real provider's contract: return the match list and
        # set the candidate flag on the detection as a side effect
        detection.is_ss_candidate = True
        return [SSMatch(designation="2005 QP87", ra=150.1001, dec=2.2,
                        sep=0.36, pa=90.0, predvmag=21.7)]

    def get_ref_matches(self, detection):
        # (star matches, galaxy matches), mirroring the real provider
        star = RefMatch(source_id="42", ra=150.10005, dec=2.2, sep=0.4,
                        pa=90.0, class_star=0.97, flags=0,
                        mag_auto=18.5, mag_err_auto=0.02, elong=1.05,
                        fwhm=0.31, half_light_radius=0.15, kron_radius=3.5)
        galaxy = RefMatch(source_id="77", ra=150.1004, dec=2.2, sep=1.5,
                          pa=270.0, class_star=0.03, flags=2,
                          mag_auto=21.2, mag_err_auto=0.1, elong=1.6,
                          fwhm=0.8, half_light_radius=0.6, kron_radius=4.1)
        return [star], [galaxy]

    def get_ned_matches(self, detection):
        # nearest first; the second entry is a galaxy NED has no redshift
        # for, so it exercises every nullable nedMatch field
        return [NedMatch(prefname="2MASX J10002400+0212000", ra=150.1002,
                         dec=2.2, sep=0.7, pa=90.0, ptype="G",
                         z=0.0312, zunc=0.0001, zflag="SLS"),
                NedMatch(prefname="SDSS J100024.10+021159.9", ra=150.1008,
                         dec=2.2, sep=2.9, pa=90.0, ptype="G")]

    def get_lvs_matches(self, detection):
        # the first is the same galaxy as nedMatches[0] (joins by prefName)
        # with every field set; the second has only the required ones and
        # an unreliable-redshift flag, exercising every nullable field
        return [LvsMatch(prefname="2MASX J10002400+0212000", ra=150.1002,
                         dec=2.2, sep=0.7, pa=90.0, objtype="G",
                         z=0.0312, z_unc=0.0001, z_tech="SPEC", z_qual=False,
                         DistMpc=135.2,
                         DistMpc_unc=9.5, DistMpc_method="zIndependent",
                         Diam=42.0, Diam_ba=0.6, Diam_pa=45.0, Diam_qual=False,
                         ebv=0.02, m_Ks=11.3, m_Ks_unc=0.05, m_W1=11.9,
                         m_W1_unc=0.02, m_NUV=16.4, m_NUV_unc=0.03,
                         SFR_hybrid=1.5, SFR_hybrid_unc=0.3,
                         Mstar=3.2e10, Mstar_unc=5e9),
                LvsMatch(prefname="UGC 00001", ra=150.1010, dec=2.2,
                         sep=3.6, pa=90.0, z_qual=True)]

    def get_cutouts(self, detection):
        return Cutouts(difference=b"FAKE_DIFF", science=b"FAKE_SCI",
                       template=None)


@pytest.fixture(scope="module")
def alert():
    return assemble_alert(MinimalProvider(), 9999)


def test_committed_avsc_files_match_registry():
    assert generate(check=True), ".avsc files differ from param_registry.py"


def test_assembled_alert_semantics(alert):
    assert alert["diaSourceId"] == 9999
    assert alert["schemaVersion"] == VERSION
    assert alert["diaSource"]["isNegative"] is False   # isdiffpos inverted
    assert alert["diaSource"]["psfNdata"] == 25
    assert alert["diaSource"]["diaObjectId"] == 777
    assert abs(alert["diaSource"]["snr"] - 1234.5 / 56.7) < 1e-6
    assert alert["diaObject"]["nDiaSources"] == 3
    # first/last/validity MJDs are computed from the detection history
    assert alert["diaObject"]["firstDiaSourceMjd"] == 60480.5
    assert alert["diaObject"]["lastDiaSourceMjd"] == 60500.5
    assert alert["diaObject"]["validityStartMjd"] == 60500.5
    assert len(alert["prvDiaSources"]) == 2
    assert alert["prvDiaForcedSources"] is None
    assert alert["cutoutDifference"] == b"FAKE_DIFF"
    assert alert["cutoutReference"] is None
    # solar-system association: matched path
    assert alert["diaSource"]["isSSCandidate"] is True
    assert alert["ssMatches"][0]["designation"] == "2005 QP87"
    assert alert["ssMatches"][0]["predVMag"] == pytest.approx(21.7)
    # prv sources were never associated -> their flag stays null
    assert all(p["isSSCandidate"] is None for p in alert["prvDiaSources"])
    # reference-catalog cross-match: matched path
    assert alert["refStarMatches"][0]["sourceId"] == "42"
    assert alert["refStarMatches"][0]["classStar"] == pytest.approx(0.97)
    assert alert["refGalaxyMatches"][0]["magAuto"] == pytest.approx(21.2)
    # NED cross-match: matched path, nearest first, nullable fields null
    assert alert["nedMatches"][0]["prefName"] == "2MASX J10002400+0212000"
    assert alert["nedMatches"][0]["z"] == pytest.approx(0.0312)
    assert alert["nedMatches"][0]["zFlag"] == "SLS"
    assert alert["nedMatches"][1]["type"] == "G"
    assert alert["nedMatches"][1]["z"] is None
    assert alert["nedMatches"][1]["zUnc"] is None
    assert alert["nedMatches"][1]["zFlag"] is None
    # NED-LVS cross-match: matched path; the first entry is nedMatches[0]
    # seen through NED-LVS, joined by prefName
    assert alert["lvsMatches"][0]["prefName"] == alert["nedMatches"][0]["prefName"]
    assert alert["lvsMatches"][0]["distMpc"] == pytest.approx(135.2)
    assert alert["lvsMatches"][0]["distMethod"] == "zIndependent"
    assert alert["lvsMatches"][0]["diam"] == pytest.approx(42.0)
    assert alert["lvsMatches"][0]["mStar"] == pytest.approx(3.2e10)
    assert alert["lvsMatches"][0]["zQual"] is False
    assert alert["lvsMatches"][1]["zQual"] is True
    assert alert["lvsMatches"][1]["objType"] is None
    assert alert["lvsMatches"][1]["diam"] is None
    assert alert["lvsMatches"][1]["mStar"] is None


def test_time_processed_stamped_at_assembly():
    """timeProcessedMjd is the assembly-time UTC MJD, bracket-checked.

    The bracket converts the Unix clock to MJD independently of the
    astropy path produce.py uses (Unix epoch 1970-01-01 = MJD 40587),
    so a wrong epoch or time scale in the stamping would fail here."""
    def unix_now_mjd():
        return time.time() / 86400.0 + 40587.0

    before = unix_now_mjd()
    alert = assemble_alert(MinimalProvider(), 9999)
    after = unix_now_mjd()

    assert before <= alert["diaSource"]["timeProcessedMjd"] <= after


def test_ref_match_not_run_stays_null():
    """refStarMatches/refGalaxyMatches = None must mean "not run"."""
    provider = MinimalProvider()
    provider.get_ref_matches = lambda detection: None
    alert = assemble_alert(provider, 9999)
    assert alert["refStarMatches"] is None
    assert alert["refGalaxyMatches"] is None


def test_ref_match_ran_clean_is_empty_not_null():
    provider = MinimalProvider()
    provider.get_ref_matches = lambda detection: ([], [])
    alert = assemble_alert(provider, 9999)
    assert alert["refStarMatches"] == []
    assert alert["refGalaxyMatches"] == []


def test_ned_match_not_run_stays_null():
    """nedMatches = None must mean "not run" (disabled or NED unreachable)."""
    provider = MinimalProvider()
    provider.get_ned_matches = lambda detection: None
    alert = assemble_alert(provider, 9999)
    assert alert["nedMatches"] is None


def test_ned_match_ran_clean_is_empty_not_null():
    provider = MinimalProvider()
    provider.get_ned_matches = lambda detection: []
    alert = assemble_alert(provider, 9999)
    assert alert["nedMatches"] == []


def test_lvs_match_not_run_stays_null():
    """lvsMatches = None must mean "not run" (disabled or table unavailable)."""
    provider = MinimalProvider()
    provider.get_lvs_matches = lambda detection: None
    alert = assemble_alert(provider, 9999)
    assert alert["lvsMatches"] is None


def test_lvs_match_ran_clean_is_empty_not_null():
    provider = MinimalProvider()
    provider.get_lvs_matches = lambda detection: []
    alert = assemble_alert(provider, 9999)
    assert alert["lvsMatches"] == []


def test_ss_association_not_run_stays_null():
    """ssMatches=None must mean "not run", never "ran and found nothing"."""
    provider = MinimalProvider()
    provider.get_ss_matches = lambda detection: None
    alert = assemble_alert(provider, 9999)
    assert alert["ssMatches"] is None
    assert alert["diaSource"]["isSSCandidate"] is None


def test_ss_association_ran_clean_is_empty_not_null():
    provider = MinimalProvider()

    def ran_clean(detection):
        detection.is_ss_candidate = False
        return []
    provider.get_ss_matches = ran_clean
    alert = assemble_alert(provider, 9999)
    assert alert["ssMatches"] == []
    assert alert["diaSource"]["isSSCandidate"] is False


def test_stub_params_stay_null(alert):
    by_name = {r.name: r for r in RECORDS}
    for param in by_name["diaSource"].params:
        if param.status is Status.STUB:
            assert alert["diaSource"][param.name] is None, param.name


def test_non_nullable_implemented_param_with_none_raises():
    bad = make_detection(9999, mjd=60500.5)
    bad.sid = None                        # diaSourceId is non-nullable
    with pytest.raises(ValueError, match="diaSourceId"):
        build_dia_source(bad)


def test_stub_params_null_even_with_value_staged():
    fp = ForcedPhot(forced_id=1, aid=777, expid=42, sca=7, ra=150.1,
                    dec=2.2, mjdobs=60500.5, time_proc=60500.6,
                    flux=123.4)
    assert all(v is None for v in build_dia_forced_source(fp).values())


def test_strict_from_row_rejects_incomplete_rows():
    with pytest.raises(KeyError, match="fluxfit"):
        Source.from_row({"sid": 1, "expid": 42}, strict=True)


def test_avro_round_trip(alert):
    schema = load_schema()
    blob = serialize_alert(alert, schema=schema)
    decoded = fastavro.schemaless_reader(io.BytesIO(blob), schema)
    assert decoded["diaSourceId"] == 9999
    assert decoded["diaObject"]["diaObjectId"] == 777
    assert len(decoded["prvDiaSources"]) == 2
    assert decoded["cutoutDifference"] == b"FAKE_DIFF"
