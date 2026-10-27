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
always writes one.
"""

from __future__ import annotations

import contextlib
import io
from datetime import datetime

import fastavro
import pyarrow as pa
import pytest
import pyarrow.parquet as pq

from rapidpipe.foundation import change, create_job, create_run, publish, run_job
from rapidpipe.foundation.local import local_path
from rapidpipe.products.diffimage import SOURCE_FLAG_BRIGHT_FOOTPRINT
from rapidpipe.science.alerts.crossmatch import GAIA_COLUMNS
from rapidpipe.stages import packet
from tests.db.foundation_helpers import write_attempt
from tests.unit.test_packet import MJD, SETTINGS, corners, wcs, write_epoch, write_refcat

SURVEY, DATASET, FIELD = "S", "D", 4658155
#: The run's settings body: the packet table, and the real/bogus model whose version it stamps.
RUN_SETTINGS = {"packet": {**SETTINGS, "rb_threshold": 0.5134, "rb_version": ""},
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


def packet_job(conn, tmp_path, flags: int = 0, refcat_row: bool = True, program_id: int = -1):
    """A live run's packet job over one imported epoch, and the import, stray and exposure jobs."""
    live = create_run(conn, "tester", "live", "abc123", RUN_SETTINGS, SURVEY, DATASET)
    other = create_run(conn, "tester", "a recipe that is not live", "def456",
                       RUN_SETTINGS, SURVEY, DATASET)
    change(conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": live}}], "tester", "live")
    write_epoch(tmp_path / "e1", [(50, 50, 400.0)])
    gaia = tmp_path / "gaia.parquet"  # an empty Gaia product: no matches, not the contract here
    pq.write_table(pa.table({**{name: pa.array([], pa.float64()) for name in GAIA_COLUMNS},
                             "phot_variable_flag": pa.array([], pa.string()),
                             "ref_epoch": pa.array([], pa.float64())}), gaia)

    imported = create_job(conn, live, "import", {}, expects=[
        {"product": p, "key": k, "binding": None} for p, k in (
            ("l2files", IMAGE_KEY), ("diffimages", IMAGE_KEY), ("sources", IMAGE_KEY),
            ("psfs", {"dataset": DATASET, "fid": 8, "sca": 8, "kind": "science"}),
            ("catalogs", {"dataset": DATASET, "name": "gaia", "version": "DR3"}))])
    tables = image_rows(tmp_path / "e1")
    tables["sources"][0]["flags"] = flags
    exposures = [{**tables.pop("exposures")[0], "program_id": program_id}]
    tables["psfs"] = [{"psfid": 701, "dataset": DATASET, "fid": 8, "sca": 8, "kind": "science",
                       "checksum": "0" * 32,
                       "filename": (tmp_path / "e1" / "diffpsf.fits").as_uri()}]
    tables["catalogs"] = [{"dataset": DATASET, "name": "gaia", "version": "DR3", "location": gaia.as_uri()}]
    tables["refimages"] = [{"rfid": 801, "dataset": DATASET, "field": FIELD, "fid": 8,
                            "filename": (tmp_path / "e1" / "ref.fits").as_uri(), "checksum": "0" * 32}]
    if refcat_row:
        write_refcat(tmp_path / "e1" / "refcat.txt")
        tables["refimcatalogs"] = [{"rfid": 801, "cattype": packet.REFCAT_CATTYPE, "checksum": "0" * 32,
                                    "filename": (tmp_path / "e1" / "refcat.txt").as_uri()}]
    ra, dec = tables["sources"][0]["ra"], tables["sources"][0]["dec"]
    # The trigger's catalog rows, as its difference job publishes them beside its sources.
    tables["objects"] = [{"aid": 100, "dataset": DATASET, "survey": SURVEY, "field": FIELD,
                          "ra0": ra, "dec0": dec}]
    tables["memberships"] = [{"object_job": imported, "aid": 100, "source_job": imported, "sid": 1,
                              "dra": 0.0, "ddec": 0.0, "flux": 400.0}]
    publish(conn, imported, 1, write_attempt(tmp_path / "import", tables))
    # The exposure, published by its own job, as intake publishes it apart from the source set.
    exposure = create_job(conn, live, "intake", {}, expects=[
        {"product": "exposures", "key": {"dataset": DATASET, "expid": 1}, "binding": None}])
    publish(conn, exposure, 1, write_attempt(tmp_path / "intake", {"exposures": exposures}))
    # A recipe that is not live: its unofficial source set joined object 100 too; never counted.
    stray = create_job(conn, other, "import", {})
    publish(conn, stray, 1, write_attempt(tmp_path / "stray", {
        "sources": tables["sources"],
        "memberships": [{"object_job": imported, "aid": 100, "source_job": stray, "sid": 1,
                         "dra": 0.0, "ddec": 0.0, "flux": 400.0}]}))

    inputs = [{"product": p, "key": k, "source_job": j} for p, k, j in (
        ("sources", IMAGE_KEY, imported),
        ("psfs", {"dataset": DATASET, "fid": 8, "sca": 8, "kind": "science"}, imported),
        ("catalogs", {"dataset": DATASET, "name": "gaia", "version": "DR3"}, imported),
        ("refimages", {"dataset": DATASET, "field": FIELD, "fid": 8}, imported))]
    job = create_job(conn, live, "packet", {"expid": 1, "sca": 8, "fid": 8}, inputs=inputs,
                     expects=[{"product": "packets", "key": IMAGE_KEY, "binding": None}])
    return job, imported, stray, ra, dec, exposure


# A source in a bright footprint is a trigger like a flags = 0 one, its bit in psfFitFlags.
@pytest.mark.parametrize("flags", [0, SOURCE_FLAG_BRIGHT_FOOTPRINT])
def test_a_packet_job_reads_the_catalog_as_it_stands_and_records_the_read(connect, tmp_path, flags):
    conn = connect()
    job, founder, stray, ra, dec, exposure = packet_job(conn, tmp_path, flags)
    result = run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), packet.run)
    assert result["published"] and result["refusal"] is None

    index = query(conn, "SELECT object_job, aid, filename, byte_offset, byte_length"
                        " FROM packets_official WHERE job = %s", (job,))
    assert [(o, a) for o, a, *_ in index] == [(founder, 100)]
    (_, _, filename, offset, length), = index
    data = local_path(filename).read_bytes()
    with local_path(filename).open("rb") as stream:
        header_end = next(fastavro.block_reader(stream)).offset
    (record,) = list(fastavro.reader(io.BytesIO(data[:header_end] + data[offset:offset + length])))
    assert record["diaObject"]["diaObjectId"] == 100
    assert record["diaObject"]["ra0"] == ra
    assert record["diaObject"]["nDiaSources"] == 1
    assert {r[0] for r in query(conn, "SELECT aid FROM forced WHERE job = %s", (job,))} == {100}
    assert record["diaSource"]["psfFitFlags"] == flags
    recorded = query(conn, "SELECT product, source_job FROM job_inputs WHERE job = %s"
                           " AND kind = 'input' ORDER BY product, source_job", (job,))
    assert ("sources", founder) in recorded and ("diffimages", founder) in recorded
    assert ("l2files", founder) in recorded and ("sources", stray) not in recorded
    assert ("exposures", exposure) in recorded
    source = record["diaSource"]
    assert source["exposureTime"] == 60.0
    assert {WIRE_NAMES[k]: v for k, v in IDENTIFIERS.items() if k in WIRE_NAMES} == {
        w: source[w] for w in WIRE_NAMES.values()}
    assert (source["program"], source["survey"]) == (-1, "SOCSIMS")
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
