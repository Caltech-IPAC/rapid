"""The difference stage through the local runner (`rapidpipe run local`), fake tools.

`run_stage_locally` runs the stage as a subprocess against a real run in
PostgreSQL (tests/db/test_local_runner.py covers admit and register the
same way). The subprocess cannot be monkeypatched, so
RAPIDPIPE_DIFFERENCE_TOOLKIT selects the fake tools. The stage writes no
rows itself; the runner's run, unit, attempt and execution-record rows are
committed by the runner, as in test_local_runner.py.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

from pathlib import Path

from rapidpipe.products.diffimage import validate_difference_entry
from rapidpipe.products.manifest import Manifest
from rapidpipe.runs.local import run_stage_locally
from tests.unit.fakedifftools import CDF_DIR, build_input_set

from .test_repository import _make_run

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_difference_runs_locally_and_is_selected(conn, tmp_path):
    run_id = _make_run(conn, kind="scratch", max_attempts=1)
    conn.commit()

    inputs = tmp_path / "inputs"
    build_input_set(inputs)
    settings = tmp_path / "overlay.toml"
    settings.write_text(
        f'[paths]\ncfg_path = "{CDF_DIR}"\n[statistics]\nclip_correction_seed = 1\n')

    result = run_stage_locally(
        conn,
        run_id=run_id,
        stage="difference",
        unit_kind="detector-image",
        unit_id="e20260821001234/SCA07",
        inputs=str(inputs),
        outputs_root=str(tmp_path / "outputs"),
        settings=str(settings),
        env={"RAPIDPIPE_DIFFERENCE_TOOLKIT": "tests.unit.fakedifftools:fake_toolkit",
             "PYTHONPATH": str(REPO_ROOT)},
    )

    assert result.exit_code == 0
    assert result.disposition == "succeeded"
    assert result.selected
    manifest = Manifest.read(result.manifest_path)
    assert manifest.run == run_id and manifest.attempt == result.attempt_id
    (entry,) = [e for e in manifest.outputs if e.kind == "difference-image"]
    validate_difference_entry(entry.to_dict())

    with conn.cursor() as cur:
        cur.execute("SELECT disposition, exit_code FROM attempts WHERE id = %s",
                    (result.attempt_id,))
        assert cur.fetchone() == ("succeeded", 0)
