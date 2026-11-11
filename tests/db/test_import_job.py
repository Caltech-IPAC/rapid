"""An import epoch's both-sign source set and its xsources, published into database/schema.sql.

The import stage runs on the unit tests' synthetic source directory (its
one database query, the PSF lookup, faked), and its attempt is published
for real: both signs' sources load into the set's child with their own
``sid``, the xsources into theirs, and both are official through the
set's binding (rapid_docs ``decisions.md``, import-both-signs). Run by
foundation.yml against database/schema.sql.
"""

from __future__ import annotations

from rapidpipe.foundation import create_job, publish
from rapidpipe.stages import import_products
from tests.db.foundation_helpers import DATASET, new_run
from tests.unit.test_import_products import (  # noqa: F401  (source is a fixture)
    EPOCHS, XSOURCES_NEGATIVE, XSOURCES_POSITIVE, catalog, run_import, source, sextractor_catalog,
)


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def test_an_imported_epochs_both_signs_and_xsources_publish_and_are_official(connect, tmp_path, source):
    difference = source / EPOCHS[0]["difference"]
    catalog(difference / import_products.DIFFERENCE_CATALOG_NEGATIVE)
    sextractor_catalog(difference / import_products.XSOURCES_CATALOGS["positive"], XSOURCES_POSITIVE)
    sextractor_catalog(difference / import_products.XSOURCES_CATALOGS["negative"], XSOURCES_NEGATIVE)
    prefix = run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "epoch")

    conn = connect()
    run = new_run(conn, live=True)
    key = {"dataset": DATASET, "expid": EPOCHS[0]["expid"], "sca": 8, "fid": 8}
    job = create_job(conn, run, "import", {}, expects=[{"product": "sources", "key": key,
                                                        "binding": None}])
    result = publish(conn, job, 1, prefix.as_uri())

    assert result["refusal"] is None
    assert _rows(conn, "SELECT isdiffpos, sid FROM sources_official WHERE job = %s ORDER BY 1, 2",
                 (job,)) == [(False, 13), (False, 14), (False, 15), (True, 5), (True, 6), (True, 7)]
    assert _rows(conn, "SELECT xsid, num, isdiffpos, rb FROM xsources_official WHERE job = %s"
                       " ORDER BY xsid", (job,)) == [(1, 1, True, 0.8), (2, 4, True, None),
                                                     (3, 1, False, 0.6)]
    assert _rows(conn, "SELECT count(*) FROM ONLY xsources") == [(0,)]
