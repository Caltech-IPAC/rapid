"""The packet stage reads the object set its job names, not the official one.

Contract (foundation plan v5, Part 1 statement 4): a packet job whose
recorded ``objects`` input is a set that is not official builds its
packets from that set; the index names it, the record carries its object
and position, and the official set's object appears nowhere.
"""

from __future__ import annotations

import contextlib
import io
from datetime import datetime

import fastavro
import pyarrow as pa
import pyarrow.parquet as pq

from rapidpipe.foundation import change, create_job, create_run, publish, run_job
from rapidpipe.foundation.local import local_path
from rapidpipe.science.alerts.crossmatch import GAIA_COLUMNS
from rapidpipe.stages import packet
from tests.db.foundation_helpers import write_attempt
from tests.unit.test_packet import MJD, SETTINGS, corners, wcs, write_epoch

SURVEY, DATASET, FIELD = "S", "D", 4658155
IMAGE_KEY = {"dataset": DATASET, "expid": 1, "sca": 8, "fid": 8}
OBJECT_KEY = {"dataset": DATASET, "survey": SURVEY, "field": FIELD}


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
                       "exptime": 60.0}],
        "sources": [{"sid": 1, "dataset": DATASET, "expid": 1, "sca": 8, "fid": 8, "pid": 601,
                     "id": 1, "field": FIELD, "hp6": 0, "hp9": 0, "mjdobs": MJD[1],
                     "ra": ra, "dec": dec, "xfit": 49.0, "yfit": 49.0, "xerr": 0.1, "yerr": 0.1,
                     "fluxfit": 400.0, "fluxerr": 10.0, "flags": 0, "qfit": 0.0, "cfit": 0.0,
                     "npixfit": 49, "sharpness": 0.0, "roundness1": 0.0, "roundness2": 0.0,
                     "peak": 1.0}],
    }


def object_set(conn, run, root, aid, ra0, dec0, source_job, name):
    job = create_job(conn, run, "associate", {"field": FIELD},
                     inputs=[{"product": "sources", "key": IMAGE_KEY, "source_job": source_job}],
                     expects=[{"product": "objects", "key": OBJECT_KEY, "binding": None}])
    publish(conn, job, 1, write_attempt(root / name, {
        "objects": [{"aid": aid, "dataset": DATASET, "survey": SURVEY, "field": FIELD,
                     "ra0": ra0, "dec0": dec0, "nsources": 1, "sumra": 0.0, "sumra2": 0.0,
                     "sumdec": 0.0, "sumdec2": 0.0, "sumflux": 400.0, "sumflux2": 1.6e5}],
        "memberships": [{"aid": aid, "source_job": source_job, "sid": 1}]}))
    return job


def test_a_packet_job_reads_the_object_set_it_names(connect, tmp_path):
    conn = connect()
    live = create_run(conn, "tester", "live", "abc123", {"packet": SETTINGS}, SURVEY, DATASET)
    other = create_run(conn, "tester", "a recipe that is not live", "def456",
                       {"packet": SETTINGS}, SURVEY, DATASET)
    change(conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": live}}], "tester", "live")
    write_epoch(tmp_path / "e1", [(50, 50, 400.0)])
    gaia = tmp_path / "gaia.parquet"  # an empty Gaia product: no matches, not the contract here
    pq.write_table(pa.table({**{name: pa.array([], pa.float64()) for name in GAIA_COLUMNS},
                             "phot_variable_flag": pa.array([], pa.string()),
                             "ref_epoch": pa.array([], pa.float64())}), gaia)

    imported = create_job(conn, live, "import", {}, expects=[
        {"product": p, "key": k, "binding": None} for p, k in (
            ("l2files", IMAGE_KEY), ("diffimages", IMAGE_KEY), ("sources", IMAGE_KEY),
            ("exposures", {"dataset": DATASET, "expid": 1}),
            ("psfs", {"dataset": DATASET, "fid": 8, "sca": 8, "kind": "science"}),
            ("catalogs", {"dataset": DATASET, "name": "gaia", "version": "DR3"}))])
    tables = image_rows(tmp_path / "e1")
    tables["psfs"] = [{"psfid": 701, "dataset": DATASET, "fid": 8, "sca": 8, "kind": "science",
                       "checksum": "0" * 32,
                       "filename": (tmp_path / "e1" / "diffpsf.fits").as_uri()}]
    tables["catalogs"] = [{"dataset": DATASET, "name": "gaia", "version": "DR3", "location": gaia.as_uri()}]
    publish(conn, imported, 1, write_attempt(tmp_path / "import", tables))

    ra, dec = tables["sources"][0]["ra"], tables["sources"][0]["dec"]
    official = object_set(conn, live, tmp_path, 100, ra, dec, imported, "official")
    named = object_set(conn, other, tmp_path, 200, ra + 1e-5, dec, imported, "named")
    assert query(conn, "SELECT job FROM objects_official") == [(official,)]

    inputs = [{"product": p, "key": k, "source_job": j} for p, k, j in (
        ("sources", IMAGE_KEY, imported), ("objects", OBJECT_KEY, named),
        ("diffimages", IMAGE_KEY, imported), ("l2files", IMAGE_KEY, imported),
        ("psfs", {"dataset": DATASET, "fid": 8, "sca": 8, "kind": "science"}, imported),
        ("catalogs", {"dataset": DATASET, "name": "gaia", "version": "DR3"}, imported),
        ("refimages", {"dataset": DATASET, "field": FIELD, "fid": 8}, imported))]
    job = create_job(conn, live, "packet", {"expid": 1, "sca": 8, "fid": 8}, inputs=inputs,
                     expects=[{"product": "packets", "key": IMAGE_KEY, "binding": None}])
    result = run_job(lambda: contextlib.closing(connect()), job, tmp_path.as_uri(), packet.run)
    assert result["published"] and result["refusal"] is None

    index = query(conn, "SELECT object_job, aid, filename, byte_offset, byte_length"
                        " FROM packets_official WHERE job = %s", (job,))
    assert [(o, a) for o, a, *_ in index] == [(named, 200)]
    (_, _, filename, offset, length), = index
    data = local_path(filename).read_bytes()
    with local_path(filename).open("rb") as stream:
        header_end = next(fastavro.block_reader(stream)).offset
    (record,) = list(fastavro.reader(io.BytesIO(data[:header_end] + data[offset:offset + length])))
    assert record["diaObject"]["diaObjectId"] == 200
    assert record["diaObject"]["ra0"] == ra + 1e-5
    assert {r[0] for r in query(conn, "SELECT aid FROM forced WHERE job = %s", (job,))} == {200}
