"""``l2files.pixel_area_map``: the CRDS name of a file's pixel area map, through the publisher.

Contracts: a published row keeps the name it was written with; a row
written with none, alone in its file so the Parquet column has no type,
loads as null; a file without the column, as a stage before the column
wrote it, loads as null; and the official view of a fresh schema shows the
column.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from rapidpipe.foundation import create_job, publish
from tests.db.foundation_helpers import DATASET, new_run, write_attempt

AREA_MAP = "roman_wfi_area_0085.asdf"


def l2_row(expid: int, **extra) -> dict:
    corners = {f"{a}{i}": 10.0 for i in range(5) for a in ("ra", "dec")}
    return {"rid": 7000 + expid, "dataset": DATASET, "expid": expid, "sca": 16, "fid": 2,
            "field": 1, "hp6": 0, "hp9": 0, "dateobs": datetime(2026, 10, 24),
            "mjdobs": 61337.45, "exptime": 60.0, "filename": f"file:///l2/{expid}.fits.gz",
            "checksum": "0" * 32, "crval1": 10.0, "crval2": 10.0, "crpix1": 2044.5, "crpix2": 2044.5,
            "cd11": -3e-5, "cd12": 0.0, "cd21": 0.0, "cd22": 3e-5, "ra": 10.0, "dec": 10.0,
            **corners, **extra}


@pytest.mark.parametrize("extra, stored", [
    ({"pixel_area_map": AREA_MAP}, AREA_MAP),
    ({"pixel_area_map": None}, None),
    ({}, None),
], ids=["named", "none", "no column"])
def test_a_published_l2files_row_keeps_its_pixel_area_map_name_or_null(connect, tmp_path, extra, stored):
    conn = connect()
    job = create_job(conn, new_run(conn), "intake", {})
    publish(conn, job, 1, write_attempt(tmp_path / "intake", {"l2files": [l2_row(1, **extra)]}))
    with conn.cursor() as cur:
        cur.execute("SELECT pixel_area_map FROM l2files WHERE job = %s", (job,))
        assert cur.fetchall() == [(stored,)]
    conn.commit()


def test_the_official_view_shows_the_column(connect):
    conn = connect()
    with conn.cursor() as cur:
        cur.execute("SELECT pixel_area_map FROM l2files_official LIMIT 0")
        assert [column.name for column in cur.description] == ["pixel_area_map"]
    conn.commit()
