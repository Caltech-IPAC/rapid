"""`_clean`: numpy scalar facts must survive JSON encoding.

Observed live 2026-09-09 20:53 UTC (D6 attempt 9, 1,368 files):
`database/sims/db_register_socsim_files.py:854 register_l2file` builds its
`facts` dict from FITS header values and WCS arithmetic (`crval1`, `crval2`,
`ra`, `dec`, `equinox`, `zptmag`, `skymean`, ...) — these come back as numpy
scalar types (`numpy.float64`, `numpy.int64`, ...), not plain Python
`float`/`int`. `admission_bridge.record_l2file_admission` passed them through
`_clean` unchanged into `repo.admit_l2file`, which serializes `facts` to a
JSON column: `TypeError: Object of type int64 is not JSON serializable`.

The round-3 psycopg2 adapters in `rapid_db.py` register numpy scalar types
for query PARAMETERS; they do nothing for values encoded to JSON, which is a
separate encoding path (`json.dumps`, not a DB-API parameter binding).

Stub-tier: exercises `_clean` directly, no database or live connection.
"""

import json

import numpy as np

from database.sims.admission_bridge import _clean


def test_numpy_scalars_are_coerced_to_json_serializable_python_types():
    facts = {
        "crval1": np.float64(123.456),
        "expid": np.int64(7),
        "flagged": np.bool_(True),
        "instrument": "wfi",
    }
    cleaned = _clean(facts)

    # Round-trips through json.dumps without raising.
    json.dumps(cleaned)

    assert cleaned["crval1"] == 123.456
    assert type(cleaned["crval1"]) is float
    assert cleaned["expid"] == 7
    assert type(cleaned["expid"]) is int
    assert cleaned["flagged"] is True
    assert cleaned["instrument"] == "wfi"


def test_unfixed_behaviour_would_raise_on_a_numpy_int64_fact():
    """Documents the live failure this fix removes: a bare numpy scalar,
    passed to json.dumps with no coercion, is not serializable."""
    import pytest

    with pytest.raises(TypeError):
        json.dumps({"expid": np.int64(7)})


def test_none_valued_facts_are_still_dropped():
    """Pre-existing `_clean` behaviour (drop None facts) must survive the
    numpy-coercion change."""
    cleaned = _clean({"a": np.int64(1), "b": None, "c": "kept"})
    assert cleaned == {"a": 1, "c": "kept"}
