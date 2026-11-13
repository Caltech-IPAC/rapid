"""The packet stage reads the object catalog as it stands at its read and records the read.

Contract (the plan's amendment of 2026-10-24): a packet job naming only its
source set, PSF, Gaia and reference builds each packet from the object its
trigger's difference job made it a member of; the index names that object
(founding job and id), the record carries the object and its position; the
object's members counted are its official ones and the set's own, never a
membership from a source set that is not official; each source's exposure
is the official ``exposures`` row of its key, published by another job than
its source set's (as intake publishes it), and gives the record its
exposure time, Roman observation identifiers and survey (null for a program
with no ``surveys`` row); the reliability fields are null; the source sets,
the exposures and the forced window it read are recorded as the job's
``input`` rows. A
reference with no ``refimcatalogs`` row fails the job: the reference stage
always writes one. The packets are made from the set's xsources, xobjects
and xmemberships (rapid_docs decision-packets-from-xsources); the sources
path was retired with the sources table (2026-11-11), and a run body
still naming it is refused.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
from datetime import datetime

import fastavro
import psycopg2
import psycopg2.extras
import pyarrow as pa
import pytest
import pyarrow.parquet as pq

from rapidpipe.foundation import change, create_job, create_run, publish, run_job
from rapidpipe.foundation.local import local_path
from rapidpipe.science.alerts import kona
from rapidpipe.science.catalogs.store import gaia_from_table
from rapidpipe.stages import packet
from tests.db.foundation_helpers import write_attempt
from tests.db.test_child_reads import _script
from tests.db.test_children import xsource_rows
from tests.support import fakekete
from tests.support.catalogs import ra_offset, write_ss_store
from tests.unit.test_packet import MJD, SETTINGS, corners, wcs, write_epoch, write_l2, write_refcat

SURVEY, DATASET, FIELD = "S", "D", 4658155
#: The run's settings body: the packet table, and the real/bogus model whose version it stamps.
RUN_SETTINGS = {"packet": {**SETTINGS, "ned_match": False, "lvs_match": False, "ss_match": False,  # gaia alone
                           "rb_threshold": 0.5134, "rb_version": ""},
                "difference": {"rubrat": {"model_prefix": "", "model_key": "rubrat/m-1"}}}
IMAGE_KEY = {"dataset": DATASET, "expid": 1, "sca": 8, "fid": 8}
OBJECT_KEY = {"dataset": DATASET, "survey": SURVEY, "field": FIELD}
#: An exposures row's Roman observation identifiers, by column.
IDENTIFIERS = {"execution_plan": 1, "pass": 4, "segment": 1, "observation": 1, "visit": 1,
               "exposure": 1, "visit_id": "0034201004001001001",
               "observation_id": "003420100400100100101101001"}
#: Each column's alert field.
WIRE_NAMES = {"execution_plan": "plan", "pass": "pass", "segment": "segment",
              "observation": "observation", "visit": "visit", "exposure": "exposure",
              "observation_id": "observation_id"}


def query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def image_rows(directory) -> dict[str, list[dict]]:
    """One epoch's l2files, diffimages, exposures and sources rows over the synthetic image."""
    ra, dec = (float(v[0]) for v in wcs().all_pix2world([50.0], [50.0], 1))
    c = corners()
    footprint = {"ra0": ra, "dec0": dec, **{f"{a}{i}": c[2 * (i - 1) + (a == "dec")]
                                             for i in range(1, 5) for a in ("ra", "dec")}}
    image = {"dataset": DATASET, "expid": 1, "sca": 8, "fid": 8, "field": FIELD}
    return {
        "l2files": [{**image, **footprint, "rid": 501, "hp6": 0, "hp9": 0,
                     "dateobs": datetime(2026, 8, 21), "mjdobs": MJD[1], "exptime": 60.0,
                     "filename": (directory / "l2.fits").as_uri(), "checksum": "0" * 32,
                     "crval1": ra, "crval2": dec, "crpix1": 50.0, "crpix2": 50.0,
                     "cd11": 1.0, "cd12": 0.0, "cd21": 0.0, "cd22": 1.0, "ra": ra, "dec": dec}],
        "diffimages": [{**image, **footprint, "pid": 601, "rid": 501, "infobitssci": 0,
                        "infobitsref": 0,
                        "filename": (directory / "zogy_diffimage_masked.fits").as_uri()}],
        "exposures": [{"dataset": DATASET, "expid": 1, "dateobs": datetime(2026, 8, 21),
                       "exptime": 60.0, "program_id": -1, **IDENTIFIERS}],
        "sources": [{"sid": 1, "dataset": DATASET, "expid": 1, "sca": 8, "fid": 8, "pid": 601,
                     "id": 1, "field": FIELD, "hp6": 0, "hp9": 0, "mjdobs": MJD[1],
                     "ra": ra, "dec": dec, "xfit": 49.0, "yfit": 49.0, "xerr": 0.1, "yerr": 0.1,
                     "fluxfit": 400.0, "fluxerr": 10.0, "flags": 0, "qfit": 0.0, "cfit": 0.0,
                     "npixfit": 49, "sharpness": 0.0, "roundness1": 0.0, "roundness2": 0.0,
                     "peak": 1.0}],
    }


def packet_job(conn, tmp_path, refcat_row: bool = True, program_id: int = -1,
               max_alerts: int | None = None, second_source: bool = False,
               xsources: list[dict] | None = None, alert_catalog: str | None = None,
               ss: bool = False, without: tuple[str, ...] = ()):
    """A live run's packet job over one imported epoch, and the import, stray and exposure jobs.

    The set's xsources (rows of ``xsources`` but ``job``) are published with it: by default one
    fitted xsource at (50, 50), S/N 40, founding xobject 201; ``second_source`` adds a weaker one
    (xsid 2, S/N 4) founding xobject 202; ``xsources`` replaces the list. Each row with
    ``"member": True`` founds xobject ``200 + xsid`` holding it, and the stray set's xmembership
    joins xobject 201. ``max_alerts`` overrides the run's ``[packet] max_alerts``;
    ``alert_catalog`` sets the run's ``[packet] alert_catalog``, the retired key, for the test
    that it is refused; None leaves it out, as the shipped settings do. ``ss`` switches the
    solar-system match on: an ``ss`` catalog registered at a root holding the day of the epoch
    with one known object 0.3" from the first xsource, and the epoch's L2 file carrying the
    ephemeris cards (the match runs on tests/support/fakekete.py). ``without`` names
    ``[packet]`` keys left out of the run's body, as a body resolved before they existed."""
    settings = {**RUN_SETTINGS, "associate": {"xsource_flags": 0}}
    if without:
        settings = {**settings, "packet": {k: v for k, v in settings["packet"].items() if k not in without}}
    if ss:
        settings = {**settings, "packet": {**settings["packet"], "ss_match": True}}
    if max_alerts is not None:
        settings = {**settings, "packet": {**settings["packet"], "max_alerts": max_alerts}}
    if alert_catalog is not None:
        settings = {**settings, "packet": {**settings["packet"], "alert_catalog": alert_catalog}}
    if xsources is None:
        xsources = [xsource(1, 50, 50, member=True, fluxfit=400.0, snrfit=40.0, flagsfit=0)]
        if second_source:
            xsources.append(xsource(2, 20, 80, member=True, fluxfit=40.0, snrfit=4.0, flagsfit=0))
    live = create_run(conn, "tester", "live", "abc123", settings, SURVEY, DATASET)
    other = create_run(conn, "tester", "a recipe that is not live", "def456",
                       settings, SURVEY, DATASET)
    change(conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": live}}], "tester", "live")
    write_epoch(tmp_path / "e1", [(50, 50, 400.0)])
    gaia = tmp_path / "gaia"  # an empty Gaia mirror: no matches, not the contract here
    gaia_from_table(str(gaia), pa.table({"source_id": pa.array([], pa.int64()),
                                         "ra": pa.array([], pa.float64()),
                                         "dec": pa.array([], pa.float64())}), "gaia_test")

    catalog_keys = [{"dataset": DATASET, "name": "gaia", "version": "DR3"}]
    if ss:
        catalog_keys.append({"dataset": DATASET, "name": "ss", "version": kona.SS_RELEASE})
    imported = create_job(conn, live, "import", {}, expects=[
        {"product": p, "key": k, "binding": None} for p, k in (
            ("l2files", IMAGE_KEY), ("diffimages", IMAGE_KEY), ("sources", IMAGE_KEY),
            ("psfs", {"dataset": DATASET, "fid": 8, "sca": 8, "kind": "science"}),
            *[("catalogs", k) for k in catalog_keys])])
    tables = image_rows(tmp_path / "e1")
    exposures = [{**tables.pop("exposures")[0], "program_id": program_id}]
    tables["psfs"] = [{"psfid": 701, "dataset": DATASET, "fid": 8, "sca": 8, "kind": "science",
                       "checksum": "0" * 32,
                       "filename": (tmp_path / "e1" / "diffpsf.fits").as_uri()}]
    tables["catalogs"] = [{"dataset": DATASET, "name": "gaia", "version": "DR3",
                           "location": gaia.as_uri() + "/"}]
    if ss:
        write_l2(tmp_path / "e1" / "l2.fits", MJD[1])
        write_ss_store(tmp_path / "ss", kona.day_of(MJD[1]),
                       [("2024 AB", xsources[0]["ra"] - ra_offset(xsources[0]["dec"], 0.3),
                         xsources[0]["dec"], 18.0)])
        tables["catalogs"].append({"dataset": DATASET, "name": "ss", "version": kona.SS_RELEASE,
                                   "location": (tmp_path / "ss").as_uri() + "/"})
    tables["refimages"] = [{"rfid": 801, "dataset": DATASET, "field": FIELD, "fid": 8,
                            "filename": (tmp_path / "e1" / "ref.fits").as_uri(), "checksum": "0" * 32}]
    if refcat_row:
        write_refcat(tmp_path / "e1" / "refcat.txt")
        tables["refimcatalogs"] = [{"rfid": 801, "cattype": packet.REFCAT_CATTYPE, "checksum": "0" * 32,
                                    "filename": (tmp_path / "e1" / "refcat.txt").as_uri()}]
    ra, dec = xsources[0]["ra"], xsources[0]["dec"]
    # The trigger's catalog rows, as its difference job publishes them beside its set: the
    # xsources, and the xobjects and xmemberships its cross-match made. (The set's `sources`
    # rows stay published as the import stage still writes them; the packet stage reads none.)
    members = [x for x in xsources if x.get("member")]
    tables["xsources"] = [{k: v for k, v in x.items() if k != "member"} for x in xsources]
    tables["xobjects"] = [{"xaid": 200 + x["xsid"], "dataset": DATASET, "survey": SURVEY,
                           "field": FIELD, "ra0": x["ra"], "dec0": x["dec"], "flux0": x["fluxap"]}
                          for x in members]
    tables["xmemberships"] = [{"object_job": imported, "xaid": 200 + x["xsid"], "field": FIELD,
                               "source_job": imported, "xsid": x["xsid"], "dra": 0.0, "ddec": 0.0,
                               "flux": x["fluxap"]} for x in members]
    publish(conn, imported, 1, write_attempt(tmp_path / "import", tables))
    # The exposure, published by its own job, as intake publishes it apart from the source set.
    exposure = create_job(conn, live, "intake", {}, expects=[
        {"product": "exposures", "key": {"dataset": DATASET, "expid": 1}, "binding": None}])
    publish(conn, exposure, 1, write_attempt(tmp_path / "intake", {"exposures": exposures}))
    # A recipe that is not live: its unofficial source set joined xobject 201 too; never counted.
    stray = create_job(conn, other, "import", {})
    stray_tables = {"sources": tables["sources"], "xsources": tables["xsources"],
                    "xmemberships": [{**tables["xmemberships"][0], "source_job": stray}]}
    publish(conn, stray, 1, write_attempt(tmp_path / "stray", stray_tables))

    inputs = [{"product": p, "key": k, "source_job": j} for p, k, j in (
        ("sources", IMAGE_KEY, imported),
        ("psfs", {"dataset": DATASET, "fid": 8, "sca": 8, "kind": "science"}, imported),
        *[("catalogs", k, imported) for k in catalog_keys],
        ("refimages", {"dataset": DATASET, "field": FIELD, "fid": 8}, imported))]
    job = create_job(conn, live, "packet", {"expid": 1, "sca": 8, "fid": 8}, inputs=inputs,
                     expects=[{"product": "packets", "key": IMAGE_KEY, "binding": None}])
    return job, imported, stray, ra, dec, exposure


def test_a_packet_job_matches_the_registered_ss_days_predictions_from_the_l2_header(connect, tmp_path,
                                                                                    monkeypatch):
    """The ss catalog registered once at its root: the job reads the day of the trigger, places the
    observer from the L2 file's header, and the trigger 0.3" from the prediction is a candidate."""
    monkeypatch.setattr(kona, "_kete", lambda: fakekete)
    conn = connect()
    job, founder, *_ = packet_job(conn, tmp_path, ss=True)
    result = run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), packet.run)
    assert result["published"] and result["refusal"] is None
    (record,) = _records(conn, job).values()
    assert [(m["designation"], m["predVMag"]) for m in record["ssMatches"]] == [("2024 AB", pytest.approx(23.0))]
    assert record["ssMatches"][0]["sep"] == pytest.approx(0.3, abs=0.01)
    assert record["diaSource"]["isSSCandidate"] is True
    assert query(conn, "SELECT catid, catalog_job FROM external WHERE job = %s", (job,)) == [("2024 AB", founder)]
    recorded = query(conn, "SELECT key ->> 'name' FROM job_inputs WHERE job = %s AND kind = 'input'"
                           " AND product = 'catalogs' ORDER BY 1", (job,))
    assert recorded == [("gaia",), ("ss",)]


def test_a_run_body_without_a_catalog_switch_has_that_match_off(connect, tmp_path):
    """A body resolved before a catalog existed (ss, 2026-11-12) lacks its switch: the job runs,
    the match did not run, and no binding is demanded, as the loop reads the same body."""
    conn = connect()
    job, *_ = packet_job(conn, tmp_path, without=("ss_match", "ss_radius_arcsec", "ss_max_matches",
                                                  "ss_cone_radius_deg"))
    result = run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), packet.run)
    assert result["published"] and result["refusal"] is None
    (record,) = _records(conn, job).values()
    assert record["ssMatches"] is None and record["diaSource"]["isSSCandidate"] is None


def test_a_packet_job_reads_the_catalog_as_it_stands_and_records_the_read(connect, tmp_path):
    conn = connect()
    job, founder, stray, ra, dec, exposure = packet_job(conn, tmp_path)
    result = run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), packet.run)
    assert result["published"] and result["refusal"] is None

    index = query(conn, "SELECT object_job, aid, filename, byte_offset, byte_length"
                        " FROM packets_official WHERE job = %s", (job,))
    assert [(o, a) for o, a, *_ in index] == [(founder, 201)]
    (_, _, filename, offset, length), = index
    data = local_path(filename).read_bytes()
    with local_path(filename).open("rb") as stream:
        header_end = next(fastavro.block_reader(stream)).offset
    (record,) = list(fastavro.reader(io.BytesIO(data[:header_end] + data[offset:offset + length])))
    assert record["diaObject"]["diaObjectId"] == 201
    assert record["diaObject"]["ra0"] == ra
    assert record["diaObject"]["nDiaSources"] == 1
    assert {r[0] for r in query(conn, "SELECT aid FROM forced WHERE job = %s", (job,))} == {201}
    assert record["diaSource"]["psfFitFlags"] == 0
    recorded = query(conn, "SELECT product, source_job FROM job_inputs WHERE job = %s"
                           " AND kind = 'input' ORDER BY product, source_job", (job,))
    assert ("sources", founder) in recorded and ("diffimages", founder) in recorded
    assert ("l2files", founder) in recorded and ("sources", stray) not in recorded
    assert ("exposures", exposure) in recorded
    source = record["diaSource"]
    assert source["exposureTime"] == 60.0
    assert {WIRE_NAMES[k]: v for k, v in IDENTIFIERS.items() if k in WIRE_NAMES} == {
        w: source[w] for w in WIRE_NAMES.values()}
    assert (source["program"], source["survey"]) == (-1, "SOCSIMS GBTDS")
    assert (source["reliability"], source["reliabilityThreshold"], source["reliabilityVersion"]) \
        == (None, None, None)


def test_a_program_with_no_surveys_row_gives_survey_null(connect, tmp_path):
    conn = connect()
    job, *_ = packet_job(conn, tmp_path, program_id=4242)
    result = run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), packet.run)
    assert result["published"]
    ((filename,),) = query(conn, "SELECT DISTINCT filename FROM packets WHERE job = %s", (job,))
    with local_path(filename).open("rb") as stream:
        (record,) = list(fastavro.reader(stream))
    assert (record["diaSource"]["program"], record["diaSource"]["survey"]) == (4242, None)


def test_a_reference_without_its_catalog_row_fails_the_packet_job(connect, tmp_path):
    conn = connect()
    job, *_ = packet_job(conn, tmp_path, refcat_row=False)
    with pytest.raises(ValueError, match="no refimcatalogs row"):
        run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), packet.run)
    assert query(conn, "SELECT state FROM jobs WHERE id = %s", (job,)) == [("failed",)]


#: A statement that reads a parent of child tables (or its official view), not a named child.
PARENT_READ = re.compile(r'\b(?:FROM|JOIN)\s+"?x?sources(?:_official)?"?(?=[\s;)]|$)', re.I)


def test_a_packet_job_reads_its_sources_from_child_tables_never_the_parent(connect, tmp_path,
                                                                          monkeypatch):
    """rapidpipe.foundation.children: the set's own child and the members' children, by name;
    its memberships, objects and forced rows through the field children's functions.

    Every statement the job sends is recorded: the stage's ``RealDictCursor`` and the plain
    cursor ``set_children`` opens.
    """
    statements: list[str] = []

    class RecordingDict(psycopg2.extras.RealDictCursor):
        def execute(self, query, vars=None):
            result = super().execute(query, vars)
            statements.append(self.query.decode())
            return result

    class Recording(psycopg2.extras.LoggingConnection):
        def filter(self, msg, curs):
            statements.append(msg.decode() if isinstance(msg, bytes) else msg)
            return msg

    def recording():
        connection = psycopg2.connect(
            host=os.environ["PGHOST"], port=os.environ.get("PGPORT", "5432"),
            dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
            password=os.environ.get("PGPASSWORD", ""), connection_factory=Recording)
        connection.initialize(logging.getLogger("packet-sql"))
        return contextlib.closing(connection)

    monkeypatch.setattr(packet, "RealDictCursor", RecordingDict)
    conn = connect()
    job, *_ = packet_job(conn, tmp_path)
    result = run_job(recording, job, tmp_path.as_uri(), packet.run)

    assert result["published"]
    for function in ("xmemberships_of(", "packet_membership_rows(", "packet_origin_rows(",
                     "packet_reuse_rows("):
        assert any(f"FROM {function}" in s for s in statements), function  # the stage's reads were seen
    assert [s for s in statements if PARENT_READ.search(s)] == []
    children = [s for s in statements if re.search(r'FROM "xsources_\d{8}_8" s', s)]
    assert len(children) == 2                                     # the set's own, its members'
    assert not any(re.search(r'FROM "sources_\d{8}_8"', s) for s in statements)  # never the sources


@pytest.mark.parametrize("max_alerts, kept", [(1, [201]), (-1, [201, 202])])
def test_the_cap_drops_the_weaker_triggers_before_anything_is_measured(connect, tmp_path,
                                                                       max_alerts, kept):
    """[packet] max_alerts 1 of two triggers: one packet, forced rows for its object only, the
    drop counted in the manifest; -1 keeps both."""
    conn = connect()
    job, *_ = packet_job(conn, tmp_path, max_alerts=max_alerts, second_source=True)
    result = run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), packet.run)
    assert result["published"] and result["refusal"] is None
    assert [a for (a,) in query(conn, "SELECT aid FROM packets_official WHERE job = %s ORDER BY aid",
                                (job,))] == kept
    assert [a for (a,) in query(conn, "SELECT DISTINCT aid FROM forced WHERE job = %s ORDER BY aid",
                                (job,))] == kept
    ((dropped, packets),) = query(
        conn, "SELECT (manifest -> 'packet' ->> 'triggers_dropped_by_cap')::int,"
              " (manifest -> 'packet' ->> 'packets')::int FROM jobs WHERE id = %s", (job,))
    assert (dropped, packets) == (2 - len(kept), len(kept))


def earlier_epoch(conn, live, tmp_path, route: str) -> tuple[int, int | None]:
    """An official difference image of SCA 9 on the trigger's footprint a day before it, its
    science PSF reached by ``route``: ``"input"`` (its job named a psf job's row), ``"official"``
    (its job published and bound the row itself; it named none) or ``"none"`` (no row anywhere).
    Returns the image's job and the psf job."""
    write_epoch(tmp_path / "e2", [(50, 50, 300.0)])
    rows = image_rows(tmp_path / "e2")
    psf_key = {"dataset": DATASET, "fid": 8, "sca": 9, "kind": "science"}
    psf_row = {"psfid": 702, **psf_key, "checksum": "0" * 32,
               "filename": (tmp_path / "e2" / "diffpsf.fits").as_uri()}
    psf_job = None
    inputs, expects, tables = [], [], {}
    if route == "input":
        psf_job = create_job(conn, live, "psf", {}, expects=[{"product": "psfs", "key": psf_key,
                                                             "binding": None}])
        publish(conn, psf_job, 1, write_attempt(tmp_path / "psf9", {"psfs": [psf_row]}))
        inputs = [{"product": "psfs", "key": psf_key, "source_job": psf_job}]
    elif route == "official":
        expects = [{"product": "psfs", "key": psf_key, "binding": None}]
        tables["psfs"] = [psf_row]
    image_key = {"dataset": DATASET, "expid": 2, "sca": 9, "fid": 8}
    expects += [{"product": p, "key": image_key, "binding": None} for p in ("l2files", "diffimages")]
    tables["l2files"] = [{**rows["l2files"][0], "expid": 2, "sca": 9, "rid": 502, "mjdobs": MJD[1] - 1.0}]
    tables["diffimages"] = [{**rows["diffimages"][0], "expid": 2, "sca": 9, "pid": 602, "rid": 502}]
    image_job = create_job(conn, live, "import", {}, inputs=inputs, expects=expects)
    publish(conn, image_job, 1, write_attempt(tmp_path / "import2", tables))
    return image_job, psf_job


@pytest.mark.parametrize("route", ["input", "official", "none"])
def test_each_window_image_is_measured_with_its_own_science_psf(connect, tmp_path, route):
    """dev 01f89299, by the rebuild's record: a window image of another SCA takes the psfs row its
    own job named, else the official one of its key, each read recorded; with no row its science
    fluxes are null and the set still publishes."""
    conn = connect()
    job, founder, _, _, _, _ = packet_job(conn, tmp_path)
    live = query(conn, "SELECT run FROM jobs WHERE id = %s", (job,))[0][0]
    image_job, psf_job = earlier_epoch(conn, live, tmp_path, route)
    result = run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), packet.run)
    assert result["published"] and result["refusal"] is None

    forced = {pid: (sci, diff) for pid, sci, diff in query(
        conn, "SELECT pid, science_flux, psf_flux FROM forced WHERE job = %s", (job,))}
    assert set(forced) == {601, 602}                        # the trigger's image and the earlier one
    assert forced[601][0] is not None and forced[601][1] is not None
    assert forced[602][1] is not None
    assert (forced[602][0] is not None) == (route != "none")
    recorded = query(conn, "SELECT product, key, source_job FROM job_inputs WHERE job = %s"
                           " AND kind = 'input' AND product = 'psfs'", (job,))
    psf_reads = {(k["sca"], j) for _, k, j in recorded}
    expected_read = {"input": {(9, psf_job)}, "official": {(9, image_job)}, "none": set()}[route]
    assert psf_reads == {(8, founder)} | expected_read         # the named input, and the one read
    ((without,),) = query(conn, "SELECT (manifest -> 'packet' ->> 'window_without_science_psf')::int"
                                " FROM jobs WHERE id = %s", (job,))
    assert without == (1 if route == "none" else 0)


def xsource(xsid: int, column: float, line: float, **columns) -> dict:
    """One xsource of the trigger's set at one-based pixel (``column``, ``line``), fitted, flags 0."""
    ra, dec = (float(v[0]) for v in wcs().all_pix2world([column], [line], 1))
    (row,) = [r for r in xsource_rows(MJD[1], 8, n=xsid) if r["xsid"] == xsid]
    return {**row, "expid": 1, "fid": 8, "pid": 601, "field": FIELD, "ra": ra, "dec": dec,
            "x": column, "y": line, "isdiffpos": True, **columns}


def _records(conn, job) -> dict[int, dict]:
    """The packet set's records, by diaSourceId."""
    ((filename,),) = query(conn, "SELECT DISTINCT filename FROM packets WHERE job = %s", (job,))
    with local_path(filename).open("rb") as stream:
        return {r["diaSourceId"]: r for r in fastavro.reader(stream)}


def test_an_xsources_packet_set_is_made_from_the_xsources_their_xobjects_and_xmemberships(
        connect, tmp_path):
    """rapid_docs decision-packets-from-xsources: the triggers are the set's xmember xsources with
    flags = [associate] xsource_flags, the index names their xobjects and xsids, never the sources'
    object 100; a fitted xsource carries its fit in psfFlux, an unfitted one the 3 px aperture,
    both the aperture in apFlux; x and y are zero-based; history counts official xmemberships
    only; the source sets read are recorded as sources reads, and the run's checks pass."""
    conn = connect()
    xsources = [
        xsource(1, 50, 50, member=True, fluxfit=400.0, snrfit=40.0, flagsfit=0, rb=0.875,
                fluxap1=380.0, fluxerrap1=12.0, redchi=1.25, npixfit=49, sharpness=0.25),
        xsource(2, 20, 80, member=True, fluxfit=None, snrfit=None, flagsfit=None,
                fluxap1=60.0, fluxerrap1=10.0),
        xsource(3, 80, 20, member=True, flags=2),        # SExtractor flags: not a trigger
        xsource(4, 80, 80)]                              # in no xobject: not a trigger
    job, founder, stray, *_ = packet_job(conn, tmp_path, xsources=xsources)
    result = run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), packet.run)
    assert result["published"] and result["refusal"] is None

    assert query(conn, "SELECT object_job, aid, sid FROM packets_official WHERE job = %s ORDER BY sid",
                 (job,)) == [(founder, 201, 1), (founder, 202, 2)]
    assert {a for (a,) in query(conn, "SELECT aid FROM forced WHERE job = %s", (job,))} == {201, 202}
    records = _records(conn, job)
    fitted, aperture = records[1], records[2]
    assert fitted["diaObject"]["diaObjectId"] == 201
    assert fitted["diaObject"]["nDiaSources"] == 1                 # the stray set's xmembership not counted
    source = fitted["diaSource"]
    assert (source["psfFlux"], source["psfFluxErr"], source["snr"]) == (400.0, 10.0, 40.0)
    assert (source["apFlux"], source["apFluxErr"]) == (380.0, 12.0)
    assert (source["x"], source["y"], source["psfFitFlags"]) == (49.0, 49.0, 0)
    assert (source["psfRChi2"], source["psfNdata"], source["sharpness"]) == (1.25, 49, 0.25)
    assert (source["psfQfit"], source["peak"], source["xErr"]) == (None, None, None)
    assert source["reliability"] == 0.875
    source = aperture["diaSource"]
    assert (source["psfFlux"], source["psfFluxErr"], source["apFlux"]) == (60.0, 10.0, 60.0)
    assert (source["psfFitFlags"], source["psfRChi2"]) == (0, 1.0)
    # the diaObject's per-filter statistics over the detections read (fid 8 is W146, F146 on the
    # wire) and its mean position from the xobject's sums
    for record, flux, err in ((fitted, 400.0, 10.0), (aperture, 60.0, 10.0)):
        obj = record["diaObject"]
        assert (obj["F146PsfFluxNdata"], obj["F146PsfFluxMean"], obj["F146PsfFluxMeanErr"]) == (1, flux, err)
        assert obj["F146PsfFluxSigma"] == 0.0 and obj["F146PsfFluxMaxSlope"] is None
        assert obj["F184PsfFluxNdata"] is None
        assert obj["meanRa"] == pytest.approx(obj["ra0"], abs=1e-9) and obj["meanDec"] is not None
    recorded = query(conn, "SELECT product, source_job FROM job_inputs WHERE job = %s"
                           " AND kind = 'input' ORDER BY product, source_job", (job,))
    assert ("sources", founder) in recorded and ("sources", stray) not in recorded

    reader = _script("read_packets")
    assert reader.read_set(conn, job)[1] == []
    validate = _script("validate_run")
    validate._s3 = None
    assert validate.packet_set_failures(job)[0] == []
    assert len(validate.members_by_aid(job, [(founder, 201)], "x")[(founder, 201)]) == 1


def test_a_run_body_naming_the_retired_sources_path_is_refused(connect, tmp_path):
    """A body stored with [packet] alert_catalog = "sources" fails its packet job by name, before
    any read: the sources tables are never consulted, and the job is failed."""
    conn = connect()
    job, *_ = packet_job(conn, tmp_path, alert_catalog="sources")
    assert "alert_catalog" not in RUN_SETTINGS["packet"]
    with pytest.raises(ValueError, match="retired with the sources table"):
        run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), packet.run)
    assert query(conn, "SELECT state FROM jobs WHERE id = %s", (job,)) == [("failed",)]
