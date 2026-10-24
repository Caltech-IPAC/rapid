"""database/queries.sql against the procedure proof's synthetic world (foundation plan v5, Part 2).

Contract, per statement: it runs against the official views of a world
built the way the stages build theirs (``tests/db/proof/fixture.py`` plus
the L2, exposure and reference rows it lacks), returns at least one row,
and its columns are the ones its ``-- returns:`` line documents. For the
spatial shapes, ``EXPLAIN`` shows the planner choosing the base table's
q3c or GIN index through the view, on cost: the world carries
:data:`BACKGROUND` official images and objects in other fields, so a cone
or a containment selects a few rows of thousands, as on the real sky.

The q3c statements need q3c: asserted under ``CI=true``, skipped with that
reason otherwise. Their execution times print to the CI log.
"""

from __future__ import annotations

import datetime
import os
import re
import time
from pathlib import Path

import pytest

from rapidpipe.science.crossmatch.catalog import new_object_id
from tests.db.proof.fixture import (
    CURRENT, DATASET, FID, FIELD, SCAS, SURVEY, World, image_key, position,
)

QUERIES = Path(__file__).resolve().parents[2] / "database" / "queries.sql"

#: The statements that call q3c, and the index each must reach.
Q3C = {
    "diffimages_in_cone": {"diffimages_radec_idx"},
    "l2files_in_cone_overlapping_field": {"l2files_radec_idx", "l2files_overlapfields_idx"},
    # Driven from the cone, or from the reference through its field (the
    # tessellation tile) when the planner finds that narrower.
    "diffimages_in_cone_with_reference": {"diffimages_radec_idx", "diffimages_field_idx"},
    "objects_in_cone": {"objects_radec_idx"},
}
#: Spatial statements that need no q3c, and the index each must reach.
GIN = {"l2files_overlapping_fields": {"l2files_overlapfields_idx"}}


def load_queries(path: Path = QUERIES) -> dict[str, tuple[list[str], str]]:
    """``{name: (documented columns, statement)}`` from a queries file, in file order."""
    queries = {}
    for block in re.split(r"^-- name: ", path.read_text(), flags=re.M)[1:]:
        name, _, rest = block.partition("\n")
        returns = re.search(r"^-- returns: (.+)$", rest, flags=re.M).group(1)
        statement = "\n".join(line for line in rest.splitlines()
                              if not line.startswith("--")).strip()
        queries[name.strip()] = ([c.strip() for c in returns.split(",")], statement)
    return queries


def q3c_present(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'q3c'")
        present = cur.fetchone() is not None
    conn.commit()
    if not present and os.environ.get("CI") == "true":
        pytest.fail("q3c is not installed in CI")
    return present


#: Official images and objects elsewhere on the sky, in other fields: enough rows
#: that the planner chooses an index on cost rather than scanning everything.
BACKGROUND = 2000


def _l2file(epoch: int, sca: int, mjd: float, ra: float | None = None, dec: float | None = None,
            field: int = FIELD) -> dict:
    if ra is None:
        ra, dec = position(sca, 0)
    return {**image_key(epoch, sca), "field": field, "hp6": 0, "hp9": 0,
            "dateobs": datetime.datetime(2023, 2, 25) + datetime.timedelta(days=epoch),
            "mjdobs": mjd, "exptime": 60.0, "filename": f"l2-{epoch}-{sca}.fits",
            "checksum": "0" * 32, "status": 1, "crval1": ra, "crval2": dec, "crpix1": 1.0,
            "crpix2": 1.0, "cd11": 1e-5, "cd12": 0.0, "cd21": 0.0, "cd22": 1e-5, "ra": ra,
            "dec": dec, "overlapfields": [field, field + 1],
            **{f"{c}{n}": v for n in range(5) for c, v in (("ra", ra), ("dec", dec))}}


def _background(w: World, run: int) -> None:
    """BACKGROUND L2 and difference images, their fields' references and BACKGROUND objects, bound."""
    l2, diff, expects = [], [], []
    for i in range(BACKGROUND):
        expid, field, ra, dec = 1000 + i, 1000 + i // 10, 100.0 + 0.1 * i, 40.0
        l2.append(_l2file(expid, 1, 60000.0, ra, dec, field))
        diff.append({**image_key(expid, 1), "pid": 100000 + i, "field": field, "jd": 2460000.5,
                     "infobitssci": 0, "infobitsref": 0, "status": 1, "filename": "d.fits",
                     **{f"{c}{n}": v for n in range(5) for c, v in (("ra", ra), ("dec", dec))}})
        expects += [{"product": p, "key": image_key(expid, 1), "binding": None}
                    for p in ("l2files", "diffimages")]
    w.job(run, "import", {"background": BACKGROUND}, {"l2files": l2, "diffimages": diff},
          expects=expects)
    fields = sorted({row["field"] for row in l2})
    w.job(run, "import", {"background": "references"}, {"refimages": [
        {"dataset": DATASET, "field": field, "fid": FID, "filename": "r.fits",
         "checksum": "0" * 32, "status": 1, "nframes": 10, "cov5percent": 50.0}
        for field in fields]}, expects=[
        {"product": "refimages", "key": {"dataset": DATASET, "field": field, "fid": FID},
         "binding": None} for field in fields])
    founder = w.job(run, "difference", {"field": FIELD + 1}, {"objects": [
        {"aid": i, "dataset": DATASET, "survey": SURVEY, "field": FIELD + 1,
         "ra0": 100.0 + 0.1 * i, "dec0": 40.0} for i in range(BACKGROUND)]})
    # Official through a bound source set's memberships, as the view defines them.
    w.job(run, "difference", {"field": FIELD + 1}, {"memberships": [
        {"object_job": founder, "aid": i, "source_job": founder, "sid": i, "dra": 0.0, "ddec": 0.0,
         "flux": 1.0} for i in range(BACKGROUND)]},
        expects=[{"product": "sources", "key": image_key(1000, 1), "binding": None}])


@pytest.fixture()
def world(connect, tmp_path):
    """The proof's two recipes' first one, plus L2, exposures, a reference and a failure.

    Returns ``(world, params)``: the parameters every statement takes.
    """
    w = World(connect(), tmp_path)
    run = w.run("queries", live=True)
    _background(w, run)
    reference = w.job(run, "import", {"reference": FIELD}, {"refimages": [{
        "dataset": DATASET, "field": FIELD, "fid": FID, "filename": "ref.fits",
        "checksum": "0" * 32, "status": 1, "nframes": 10, "cov5percent": 50.0}]},
        expects=[{"product": "refimages", "key": {"dataset": DATASET, "field": FIELD, "fid": FID},
                  "binding": CURRENT}])
    ref_input = [{"product": "refimages", "key": {"dataset": DATASET, "field": FIELD, "fid": FID},
                  "source_job": reference}]
    # Epoch 3 has an L2 file and no difference image: the work list's row.
    for epoch in (1, 2, 3):
        for sca in (SCAS if epoch < 3 else SCAS[:1]):
            outputs = {"l2files": [_l2file(epoch, sca, 60000.0 + epoch)]}
            expects = [{"product": "l2files", "key": image_key(epoch, sca), "binding": CURRENT}]
            if sca == SCAS[0]:
                outputs["exposures"] = [{"dataset": DATASET, "expid": epoch, "exptime": 60.0,
                                         "dateobs": datetime.datetime(2023, 2, 25)}]
                expects.append({"product": "exposures",
                                "key": {"dataset": DATASET, "expid": epoch}, "binding": CURRENT})
            w.job(run, "import", {"epoch": epoch, "sca": sca}, outputs, inputs=ref_input,
                  expects=expects)
    made = w.recipe(run)
    failed = w.job(run, "packet", {"epoch": 2, "sca": 1}, {}, published=False)
    w.rows("UPDATE jobs SET state = 'failed', exit_code = 70 WHERE id = %s RETURNING id", (failed,))
    # The publisher analyses the product tables it loads; autovacuum analyses
    # the rest on a running database. Here, at once, so the planner costs the
    # views' bookkeeping joins from statistics rather than defaults.
    w.conn.autocommit = True
    with w.conn.cursor() as cur:
        cur.execute("ANALYZE")
    w.conn.autocommit = False

    ra, dec = position(SCAS[0], 0)
    params = {
        "dataset": DATASET, "survey": SURVEY, "field": FIELD, "fid": FID, "expid": 1,
        "sca": SCAS[0], "object_job": made["differences"][(1, SCAS[0])], "aid": new_object_id(ra, dec),
        "source_job": made["differences"][(1, SCAS[0])], "sid": 0,
        "packet_job": made["packets"][(2, SCAS[0])],
        "rfid": w.rows("SELECT rfid FROM refimages WHERE job = %s", (reference,))[0][0],
        "group": w.rows("SELECT max(opened_group) FROM official")[0][0],
        "ra": ra, "dec": dec, "radius": 0.1, "mjd_min": 59999.0, "mjd_max": 60010.0,
        "jd_min": 2459999.0, "jd_max": 2460010.0, "exclude_rid": -1, "fields": [FIELD],
        "min_frames": 1, "min_nframes": 5, "min_cov5percent": 10.0,
        "pid": w.rows("SELECT min(pid) FROM diffimages_official")[0][0],
    }
    return w, params


def _assert_shape(world, params, name, columns, statement):
    with world.conn.cursor() as cur:
        cur.execute(statement, params)
        rows = cur.fetchall()
        got = [d.name for d in cur.description]
    world.conn.commit()
    assert got == columns, f"{name}: columns {got}, documented {columns}"
    assert rows, f"{name}: no rows"


def _indexes(world, params, statement) -> set[str]:
    """The index names in the planner's plan of ``statement``."""
    with world.conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + statement, params)
        plan = cur.fetchone()[0]
    world.conn.rollback()
    world.last_plan = plan
    found = set()

    def walk(node):
        if isinstance(node, dict):
            if "Index Name" in node:
                found.add(node["Index Name"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(plan)
    return found


def test_each_plain_statement_returns_its_documented_columns(world):
    w, params = world
    queries = load_queries()
    assert set(Q3C) | set(GIN) <= set(queries)
    for name, (columns, statement) in queries.items():
        if name not in Q3C:
            _assert_shape(w, params, name, columns, statement)


def test_overlapfields_containment_reaches_the_gin_index(world):
    w, params = world
    queries = load_queries()
    for name, expected in GIN.items():
        assert _indexes(w, params, queries[name][1]) & expected, (name, w.last_plan)


def test_q3c_statements_return_their_columns_and_reach_their_index(world, capsys):
    w, params = world
    if not q3c_present(w.conn):
        pytest.skip("q3c is required only in CI; a developer's container may lack it")
    queries = load_queries()
    report, missed = [], []
    for name, expected in Q3C.items():
        columns, statement = queries[name]
        _assert_shape(w, params, name, columns, statement)
        used = _indexes(w, params, statement)
        if not used & expected:
            missed.append(name)
        with w.conn.cursor() as cur:
            cur.execute("EXPLAIN " + statement, params)
            plan = "\n".join(row[0] for row in cur.fetchall())
        w.conn.rollback()
        t0 = time.perf_counter()
        with w.conn.cursor() as cur:
            cur.execute(statement, params)
            cur.fetchall()
        w.conn.commit()
        report.append(f"QUERY-TIME q3c {name} {(time.perf_counter() - t0) * 1000:.2f} ms"
                      f" (proof world, CI); indexes {sorted(used)}\n{plan}")
    with capsys.disabled():
        print("\n" + "\n".join(report))
    assert not missed, f"no expected index in the plans of {missed}"
