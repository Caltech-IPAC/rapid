"""Recovery (supervisor step 6, 2026-09-24, R7-R9 and the Codex plan-review
amendments B1-B4) against a real
PostgreSQL with the migrations applied: the 20260924-10 columns,
``submit_unit`` freezing an attempt's inputs and settings, ``run create
--seed <run> --only-failed``, ``run start`` resolving a seeded unit's
inputs, and ``run reconcile --resolve-jobless``.

Batch is ``tests.unit.fakebatch.FakeBatch``. Every call runs on the test's
one connection through :class:`_Held`, whose ``commit``/``rollback`` are
no-ops, so the code under test commits as it always does while the whole
test stays inside the outer transaction conftest rolls back. Skips
cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import contextlib

import pytest

from rapidpipe.cli import main as cli
from rapidpipe.launch import batch as launch_batch
from rapidpipe.runs import repository as repo
from tests.unit.fakebatch import FakeBatch

from .test_repository import _make_run

CHAIN = ["admit", "register", "difference", "register", "load"]
UNIT = "r0034001002001001001/SCA01"
OUTPUTS_ROOT = "s3://test-bucket/prefix"


class _Held:
    """The test connection with commit/rollback held back (see module doc)."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self):
        pass

    def rollback(self):
        pass

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)


@pytest.fixture()
def held(conn):
    return _Held(conn)


@pytest.fixture()
def cli_db(monkeypatch, held):
    """``rapidpipe.cli.main.connect`` yields the held test connection."""

    @contextlib.contextmanager
    def _connect(**_kwargs):
        yield held

    monkeypatch.setattr(cli, "connect", _connect)
    return held


@pytest.fixture()
def fake_batch(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "test-queue")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "test-def")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION", "test-def-prod")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", OUTPUTS_ROOT)
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION", OUTPUTS_ROOT + "-prod")
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_NAME_PREFIX", raising=False)
    fake = FakeBatch()
    monkeypatch.setattr(launch_batch, "batch_client", lambda: fake)
    return fake


def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _all(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _age_attempts(conn, run_id, seconds=3600):
    """Move a run's attempts' ``started`` into the past: rows written in the
    test's one transaction otherwise all start at its ``now()``."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE attempts SET started = started - make_interval(secs => %s) WHERE run = %s",
            (seconds, run_id))


def _attempt(held, run_id, stage, unit_id, *, inputs, settings=None, job="job-x"):
    """Allocate an attempt with recorded locations and (optionally) a job id."""
    repo.add_unit(held, run_id, stage, "detector-image", unit_id)
    attempt_id = repo.allocate_attempt(held, run_id, stage, unit_id, outputs_root=OUTPUTS_ROOT)
    repo.record_attempt_locations(held, attempt_id, inputs, settings)
    if job is not None:
        repo.record_scheduler_job(held, attempt_id, job)
    return attempt_id


def _finish(held, run_id, attempt_id, disposition, *, select=False, exit_code=None):
    repo.record_attempt_result(
        held, attempt_id, exit_code, disposition,
        _one(held, "SELECT output_location FROM attempts WHERE id = %s", (attempt_id,))[0],
        {"source_revision": "abc", "schema_version": "1", "settings_hash": "h"},
        scheduler_job_id=_one(
            held, "SELECT scheduler_job_id FROM attempts WHERE id = %s", (attempt_id,))[0])
    if select:
        repo.select_attempt(held, attempt_id)


def _complete(held, run_id, stage, unit_id, inputs="s3://in/x"):
    attempt_id = _attempt(held, run_id, stage, unit_id, inputs=inputs)
    _finish(held, run_id, attempt_id, "succeeded", select=True, exit_code=0)
    return attempt_id


def _unit_row_id(conn, run_id, stage, unit_id):
    return _one(conn, "SELECT id FROM units WHERE run = %s AND stage = %s AND unit_id = %s",
                (run_id, stage, unit_id))[0]


def _seed_run(held, **overrides):
    kwargs = dict(kind="production", selected_stages=CHAIN, max_attempts=3,
                  settings_overlay_ref="s3://settings/overlay.toml",
                  input_selection_ref="s3://selection/set.json", lane="batch",
                  resource_profile="rebuild", database_target="rapid_rebuild",
                  check_policy_ref="rebuild-trial@1", purpose="step 6 demo P6")
    kwargs.update(overrides)
    return _make_run(held, **kwargs)


# ======================================================================
# The migration
# ======================================================================

def test_migration_adds_the_recovery_columns(conn):
    columns = dict(_all(conn, """
        SELECT table_name || '.' || column_name, data_type || '/' || is_nullable
        FROM information_schema.columns
        WHERE (table_name = 'attempts' AND column_name IN ('inputs_location', 'settings_location'))
           OR (table_name = 'units' AND column_name = 'seeded_from_unit')
    """))
    assert columns == {
        "attempts.inputs_location": "text/YES",
        "attempts.settings_location": "text/YES",
        "units.seeded_from_unit": "text/YES",  # the rapid_ulid domain over text
    }
    (target,) = _one(conn, """
        SELECT ccu.table_name || '.' || ccu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = 'units'
          AND kcu.column_name = 'seeded_from_unit'
    """)
    assert target == "units.id"


# ======================================================================
# submit_unit freezes the attempt's inputs and settings (R7)
# ======================================================================

def test_submit_unit_records_inputs_and_settings_on_the_attempt(held, fake_batch):
    run_id = _make_run(held, kind="scratch")
    with_settings = launch_batch.submit_unit(
        held, run_id=run_id, stage="admit", unit_kind="detector-image", unit_id="u1",
        inputs_location="s3://in/delivery", settings_location="s3://in/admit.toml")
    without = launch_batch.submit_unit(
        held, run_id=run_id, stage="admit", unit_kind="detector-image", unit_id="u2",
        inputs_location="s3://in/delivery2")
    rows = dict((a, (i, s, j)) for a, i, s, j in _all(
        held, "SELECT id, inputs_location, settings_location, scheduler_job_id "
              "FROM attempts WHERE run = %s", (run_id,)))
    assert rows[with_settings.attempt_id] == (
        "s3://in/delivery", "s3://in/admit.toml", with_settings.job_id)
    assert rows[without.attempt_id] == ("s3://in/delivery2", None, without.job_id)


def test_submit_unit_records_locations_even_when_the_submission_fails(held, fake_batch):
    run_id = _make_run(held, kind="scratch")

    def _refuse(**_kwargs):
        raise RuntimeError("Batch said no")

    fake_batch.submit_job = _refuse
    with pytest.raises(RuntimeError):
        launch_batch.submit_unit(
            held, run_id=run_id, stage="admit", unit_kind="detector-image", unit_id="u1",
            inputs_location="s3://in/delivery", settings_location="s3://in/admit.toml")
    assert _one(held, "SELECT inputs_location, settings_location, scheduler_job_id, "
                      "disposition FROM attempts WHERE run = %s", (run_id,)) == (
        "s3://in/delivery", "s3://in/admit.toml", None, None)


# ======================================================================
# run create --seed <run> --only-failed (R7)
# ======================================================================

def _seed_with_failed_difference(held):
    """A P6-shaped seed: admit and register(admit) complete; difference U
    killed (failed); difference U-jobless left running with a job-less
    attempt; difference U-flight running with a job (in flight, not
    re-run); difference U-ok complete."""
    seed = _seed_run(held)
    _complete(held, seed, "admit", UNIT)
    _complete(held, seed, "register", f"admit/{UNIT}")
    killed = _attempt(held, seed, "difference", UNIT, inputs="s3://scratch/P6/inputs/diff",
                      settings="s3://settings/difference.toml")
    _finish(held, seed, killed, "killed")
    _attempt(held, seed, "difference", "U-jobless", inputs="s3://scratch/P6/inputs/j", job=None)
    _attempt(held, seed, "difference", "U-flight", inputs="s3://scratch/P6/inputs/f")
    _complete(held, seed, "difference", "U-ok")
    return seed


def _create(capsys, *argv):
    code = cli.main(["run", "create", *argv])
    out, err = capsys.readouterr()
    return code, out.strip(), err


def test_only_failed_copies_the_non_complete_units_and_the_stage_suffix(held, cli_db, capsys):
    seed = _seed_with_failed_difference(held)
    code, run_id, err = _create(capsys, "--seed", seed, "--only-failed")
    assert code == 0, err
    assert "seeded 2 unit(s)" in err

    row = _one(held, """
        SELECT kind, owner, purpose, selected_stages, code_revision, image_digest,
               release, settings_overlay_ref, input_selection_ref, lane,
               resource_profile, database_target, max_attempts_per_unit,
               check_policy_ref, seed_run, auto_promote
        FROM runs WHERE id = %s""", (run_id,))
    assert row == (
        "production", "brusholme", f"re-run of failed units of {seed}: step 6 demo P6",
        ["difference", "register", "load"], "abc123", "sha256:deadbeef", None,
        "s3://settings/overlay.toml", "s3://selection/set.json", "batch", "rebuild",
        "rapid_rebuild", 3, "rebuild-trial@1", seed, False)

    units = _all(held, """
        SELECT u.stage, u.unit_kind, u.unit_id, u.state, s.unit_id, s.run
        FROM units u JOIN units s ON s.id = u.seeded_from_unit
        WHERE u.run = %s ORDER BY u.unit_id""", (run_id,))
    assert units == [
        ("difference", "detector-image", UNIT, "pending", UNIT, seed),
        ("difference", "detector-image", "U-jobless", "pending", "U-jobless", seed),
    ]
    assert _one(held, "SELECT count(*) FROM units WHERE run = %s", (run_id,)) == (2,)


def test_only_failed_takes_purpose_and_owner_when_given(held, cli_db, capsys):
    seed = _seed_with_failed_difference(held)
    code, run_id, err = _create(capsys, "--seed", seed, "--only-failed", "--purpose", "P6b",
                                "--owner", "rusholme", "--kind", "production")
    assert code == 0, err
    assert _one(held, "SELECT purpose, owner FROM runs WHERE id = %s", (run_id,)) == (
        "P6b", "rusholme")


@pytest.mark.parametrize("failed_register, position_stages", [
    ("admit", ["register", "difference", "register", "load"]),
    ("difference", ["register", "load"]),
])
def test_only_failed_places_a_register_unit_by_its_producing_stage(
        held, cli_db, capsys, failed_register, position_stages):
    seed = _seed_run(held)
    _complete(held, seed, "admit", UNIT)
    if failed_register == "difference":
        _complete(held, seed, "register", f"admit/{UNIT}")
        _complete(held, seed, "difference", UNIT)
    lost = _attempt(held, seed, "register", f"{failed_register}/{UNIT}",
                    inputs=f"s3://scratch/P6/{failed_register}/out")
    _finish(held, seed, lost, "failed", exit_code=1)

    code, run_id, err = _create(capsys, "--seed", seed, "--only-failed")
    assert code == 0, err
    assert _one(held, "SELECT selected_stages FROM runs WHERE id = %s", (run_id,)) == (
        position_stages,)
    assert _all(held, "SELECT stage, unit_id FROM units WHERE run = %s", (run_id,)) == [
        ("register", f"{failed_register}/{UNIT}")]


def test_only_failed_seeds_non_complete_units_at_every_position_from_the_earliest(
        held, cli_db, capsys):
    seed = _seed_run(held)
    _complete(held, seed, "admit", UNIT)
    lost = _attempt(held, seed, "admit", "U-lost", inputs="s3://in/l")
    _finish(held, seed, lost, "lost")  # allowance 3: the unit is ready again
    assert _one(held, "SELECT state FROM units WHERE run = %s AND unit_id = 'U-lost'",
                (seed,)) == ("ready",)
    repo.add_unit(held, seed, "difference", "detector-image", "U-cancel")
    with held.cursor() as cur:
        cur.execute("UPDATE units SET state = 'cancelled' WHERE run = %s AND unit_id = 'U-cancel'",
                    (seed,))
    code, run_id, err = _create(capsys, "--seed", seed, "--only-failed")
    assert code == 0, err
    assert _one(held, "SELECT selected_stages FROM runs WHERE id = %s", (run_id,)) == (CHAIN,)
    # B2: every non-complete unit is seeded, wherever it sits.
    assert _all(held, "SELECT stage, unit_id FROM units WHERE run = %s ORDER BY unit_id",
                (run_id,)) == [("difference", "U-cancel"), ("admit", "U-lost")]


def test_only_failed_refusals(held, cli_db, capsys):
    clean = _seed_run(held)
    _complete(held, clean, "admit", UNIT)
    code, _, err = _create(capsys, "--seed", clean, "--only-failed")
    assert code == 64 and "has no non-complete unit" in err

    code, _, err = _create(capsys, "--only-failed")
    assert code == 64 and "--only-failed requires --seed" in err

    seed = _seed_with_failed_difference(held)
    code, _, err = _create(capsys, "--seed", seed, "--only-failed", "--kind", "scratch")
    assert code == 64 and "differs from seed run" in err

    code, _, err = _create(capsys, "--seed", seed, "--only-failed", "--stages", "difference",
                           "--max-attempts", "2")
    assert code == 64 and "--stages, --max-attempts not accepted with --only-failed" in err

    code, _, err = _create(capsys, "--seed", seed, "--only-failed",
                           "--check-policy", "rebuild-strict@1", "--auto-promote")
    assert code == 64 and "--check-policy, --auto-promote not accepted" in err

    code, _, err = _create(capsys, "--seed", "0" * 26, "--only-failed")
    assert code == 64 and "does not exist" in err

    with held.cursor() as cur:
        cur.execute("UPDATE runs SET state = 'deleting' WHERE id = %s", (seed,))
    code, _, err = _create(capsys, "--seed", seed, "--only-failed")
    assert code == 64 and "'deleting'" in err


def test_run_create_without_only_failed_still_requires_kind_purpose_and_stages(
        cli_db, capsys):
    code, _, err = _create(capsys, "--kind", "scratch")
    assert code == 64 and "--purpose, --stages required" in err


def test_seed_failed_units_refuses_a_run_that_is_not_the_plans(held):
    seed = _seed_with_failed_difference(held)
    other = _make_run(held, kind="production", selected_stages=CHAIN, seed_run=seed)
    with pytest.raises(repo.SeedRefused, match="is not a --only-failed re-run"):
        repo.seed_failed_units(held, seed_run=seed, new_run=other)


# ======================================================================
# run start on a seeded unit (R8)
# ======================================================================

def _start(capsys, run_id, *extra):
    code = cli.main(["run", "start", run_id, "--unit", UNIT, "--no-wait", *extra])
    out, err = capsys.readouterr()
    return code, out, err


def _submitted_flags(fake):
    command = fake.submitted[-1]["containerOverrides"]["command"]
    flags = {}
    for i, token in enumerate(command):
        if token.startswith("--") and i + 1 < len(command):
            flags[token] = command[i + 1]
    return command, flags


def test_start_resolves_a_seeded_units_inputs_and_settings_from_the_seed_attempt(
        held, cli_db, fake_batch, capsys):
    seed = _seed_with_failed_difference(held)
    code, run_id, err = _create(capsys, "--seed", seed, "--only-failed")
    assert code == 0, err

    code, out, err = _start(capsys, run_id)
    assert code == 0, err
    command, flags = _submitted_flags(fake_batch)
    assert command[:2] == ["stage", "difference"]
    assert flags["--inputs"] == "s3://scratch/P6/inputs/diff"
    assert flags["--settings"] == "s3://settings/difference.toml"
    # ... and the new attempt freezes them again.
    assert _one(held, "SELECT inputs_location, settings_location FROM attempts "
                      "WHERE run = %s", (run_id,)) == (
        "s3://scratch/P6/inputs/diff", "s3://settings/difference.toml")


def test_start_explicit_inputs_and_settings_win_over_the_seed(held, cli_db, fake_batch, capsys):
    seed = _seed_with_failed_difference(held)
    _, run_id, _ = _create(capsys, "--seed", seed, "--only-failed")
    code, _, err = _start(capsys, run_id, "--inputs", "difference=s3://mine/in",
                          "--settings", "difference=s3://mine/s.toml")
    assert code == 0, err
    _, flags = _submitted_flags(fake_batch)
    assert (flags["--inputs"], flags["--settings"]) == ("s3://mine/in", "s3://mine/s.toml")


def test_start_seed_attempt_without_recorded_inputs_falls_through(
        held, cli_db, fake_batch, capsys):
    seed = _seed_with_failed_difference(held)
    with held.cursor() as cur:  # a pre-20260924-10 attempt
        cur.execute("UPDATE attempts SET inputs_location = NULL, settings_location = NULL "
                    "WHERE run = %s", (seed,))
    _, run_id, _ = _create(capsys, "--seed", seed, "--only-failed")
    code, _, err = _start(capsys, run_id)
    # Falls through to the preceding stage's output: admit, which the
    # production seed completed (its outputs are project custody).
    assert code == 0, err
    _, flags = _submitted_flags(fake_batch)
    seed_admit = _one(held, """
        SELECT a.output_location FROM units u JOIN attempts a ON a.id = u.selected_attempt
        WHERE u.run = %s AND u.stage = 'admit' AND u.unit_id = %s""", (seed, UNIT))[0]
    assert flags["--inputs"] == seed_admit
    assert "--settings" not in flags


def test_start_seeded_register_keeps_the_seed_unit_id(held, cli_db, fake_batch, capsys):
    seed = _seed_run(held)
    _complete(held, seed, "admit", UNIT)
    _complete(held, seed, "register", f"admit/{UNIT}")
    _complete(held, seed, "difference", UNIT)
    failed = _attempt(held, seed, "register", f"difference/{UNIT}",
                      inputs="s3://products/P6/difference/out")
    _finish(held, seed, failed, "failed", exit_code=1)
    _, run_id, err = _create(capsys, "--seed", seed, "--only-failed")
    assert run_id, err

    code, out, err = _start(capsys, run_id)
    assert code == 0, err
    command, flags = _submitted_flags(fake_batch)
    assert command[:2] == ["stage", "register"]
    assert flags["--unit"] == f"difference/{UNIT}"
    assert flags["--inputs"] == "s3://products/P6/difference/out"
    assert "--settings" not in flags
    assert _one(held, "SELECT count(*) FROM units WHERE run = %s", (run_id,)) == (1,)


def test_seeded_inputs_for_unit_reads_the_latest_seed_attempt(held):
    seed = _seed_run(held)
    first = _attempt(held, seed, "difference", UNIT, inputs="s3://in/first", settings="s3://s/1")
    _finish(held, seed, first, "transient", exit_code=75)
    _age_attempts(held, seed)
    _attempt(held, seed, "difference", UNIT, inputs="s3://in/second")
    new = _make_run(held, kind="production", selected_stages=["difference"], seed_run=seed)
    repo.add_unit(held, new, "difference", "detector-image", UNIT,
                  seeded_from_unit=_unit_row_id(held, seed, "difference", UNIT))
    assert repo.seeded_inputs_for_unit(held, _unit_row_id(held, new, "difference", UNIT)) == (
        "s3://in/second", None)
    repo.add_unit(held, new, "difference", "detector-image", "plain")
    assert repo.seeded_inputs_for_unit(held, _unit_row_id(held, new, "difference", "plain")) == (
        None, None)


# ======================================================================
# run reconcile --resolve-jobless (R9)
# ======================================================================

def test_resolve_jobless_records_lost_and_returns_the_unit_by_allowance(held, fake_batch):
    run_id = _make_run(held, kind="scratch", max_attempts=2)
    first = _attempt(held, run_id, "admit", "u1", inputs="s3://in/1", job=None)
    with_job = _attempt(held, run_id, "admit", "u2", inputs="s3://in/2")
    _age_attempts(held, run_id)
    young = _attempt(held, run_id, "admit", "u3", inputs="s3://in/3", job=None)

    results = launch_batch.resolve_jobless(held, run_id=run_id, older_than_seconds=600)
    assert [(r.attempt_id, r.job_id, r.batch_status, r.disposition) for r in results] == [
        (first, "-", "NOJOB", "lost")]
    # B3: both job-less attempts were looked up by name first.
    assert [c for c in fake_batch.calls if c[0] == "list_jobs"] == [
        ("list_jobs", f"rapid-admit-{first}"), ("list_jobs", f"rapid-admit-{young}")]
    assert _one(held, "SELECT disposition, exit_code, reconcile_note FROM attempts "
                      "WHERE id = %s", (first,)) == ("lost", None, "no scheduler job after 600 s")
    assert _one(held, "SELECT settings_hash, source_revision FROM execution_records "
                      "WHERE attempt = %s", (first,)) == ("unknown", "unknown")
    assert _states(held, run_id) == {"u1": "ready", "u2": "running", "u3": "running"}
    for untouched in (with_job, young):
        assert _one(held, "SELECT disposition FROM attempts WHERE id = %s",
                    (untouched,)) == (None,)

    # The second job-less attempt uses the allowance up: the unit fails.
    second = _attempt(held, run_id, "admit", "u1", inputs="s3://in/1", job=None)
    _age_attempts(held, run_id)
    results = launch_batch.resolve_jobless(held, run_id=run_id, older_than_seconds=600)
    assert {r.attempt_id for r in results} == {second, young}
    assert _states(held, run_id)["u1"] == "failed"


def _states(conn, run_id):
    return dict(_all(conn, "SELECT unit_id, state FROM units WHERE run = %s", (run_id,)))


def test_resolve_jobless_repairs_a_found_job_and_leaves_an_ambiguous_one(held, fake_batch):
    run_id = _make_run(held, kind="scratch", max_attempts=2)
    found = _attempt(held, run_id, "admit", "u1", inputs="s3://in/1", job=None)
    twice = _attempt(held, run_id, "admit", "u2", inputs="s3://in/2", job=None)
    job_id = fake_batch.add_job(f"rapid-admit-{found}")
    ambiguous = [fake_batch.add_job(f"rapid-admit-{twice}") for _ in range(2)]
    _age_attempts(held, run_id)

    results = launch_batch.resolve_jobless(held, run_id=run_id, older_than_seconds=600)
    assert [(r.attempt_id, r.job_id, r.batch_status, r.disposition) for r in results] == [
        (found, job_id, "REPAIRED", None),
        (twice, ",".join(ambiguous), "AMBIGUOUS", None)]
    assert _one(held, "SELECT scheduler_job_id, disposition FROM attempts WHERE id = %s",
                (found,)) == (job_id, None)
    assert _one(held, "SELECT scheduler_job_id, disposition FROM attempts WHERE id = %s",
                (twice,)) == (None, None)
    # The repaired attempt is now the ordinary reconcile's to resolve.
    fake_batch.set_status(job_id, "FAILED", container_exit_code=75)
    reconciled = launch_batch.reconcile(held, run_id=run_id)
    assert [(r.attempt_id, r.disposition) for r in reconciled] == [(found, "transient")]


def test_run_reconcile_resolve_jobless_prints_one_line_per_attempt(
        held, cli_db, fake_batch, capsys):
    run_id = _make_run(held, kind="scratch", max_attempts=2)
    attempt = _attempt(held, run_id, "admit", "u1", inputs="s3://in/1", job=None)
    repaired = _attempt(held, run_id, "admit", "u2", inputs="s3://in/2", job=None)
    job_id = fake_batch.add_job(f"rapid-admit-{repaired}")
    _age_attempts(held, run_id, seconds=120)

    assert cli.main(["run", "reconcile", run_id]) == 0
    assert capsys.readouterr().out == ""

    assert cli.main(["run", "reconcile", run_id, "--resolve-jobless"]) == 0
    # u1: 120 s is younger than the default 600 s; u2's job is found and
    # then reconciled (still RUNNING) in the same command.
    assert capsys.readouterr().out == (
        f"attempt={repaired} job={job_id} status=REPAIRED\n"
        f"attempt={repaired} job={job_id} status=RUNNING disposition=None selected=False\n")

    assert cli.main(["run", "reconcile", run_id, "--resolve-jobless", "--older-than", "60"]) == 0
    assert capsys.readouterr().out == (
        f"attempt={attempt} job=- status=NOJOB disposition=lost\n"
        f"attempt={repaired} job={job_id} status=RUNNING disposition=None selected=False\n")
    assert _one(held, "SELECT reconcile_note FROM attempts WHERE id = %s", (attempt,)) == (
        "no scheduler job after 60 s",)


def test_run_reconcile_older_than_needs_resolve_jobless(cli_db, capsys):
    assert cli.main(["run", "reconcile", "0" * 26, "--older-than", "60"]) == 64
    assert "--older-than needs --resolve-jobless" in capsys.readouterr().err


def test_start_names_resolve_jobless_for_a_jobless_attempt(held, cli_db, fake_batch, capsys):
    run_id = _make_run(held, kind="scratch", selected_stages=["admit"])
    _attempt(held, run_id, "admit", UNIT, inputs="s3://in/1", job=None)
    code, _, err = _start(capsys, run_id, "--inputs", "s3://in/1")
    assert code == 64
    assert f"rapidpipe run reconcile {run_id} --resolve-jobless" in err


# ======================================================================
# Codex amendments B1 (bindings, scratch seeds), B2 (every position,
# inherited stages) and B4 (seeded register keeps its id)
# ======================================================================

def _instance(held, run_id, stage, unit_id):
    """A registered product instance from a completed attempt of ``run_id``."""
    from .test_repository import _register_simple_instance

    attempt_id = _complete(held, run_id, stage, unit_id)
    return _register_simple_instance(held, run_id, stage, attempt_id,
                                     logical_key={"unit": f"{run_id}/{unit_id}"})


def _bindings(held, run_id, stage, unit_id):
    return [r[0] for r in _all(held, """
        SELECT ui.producer_instance FROM unit_inputs ui JOIN units u ON u.id = ui.unit
        WHERE u.run = %s AND u.stage = %s AND u.unit_id = %s ORDER BY 1""",
        (run_id, stage, unit_id))]


def test_production_seed_copies_the_seed_units_input_bindings(held, cli_db, capsys):
    seed = _seed_with_failed_difference(held)
    reference = _instance(held, _make_run(held, kind="production"), "difference", "ref")
    own = _instance(held, seed, "difference", "own")
    repo.bind_unit_inputs(held, seed, "difference", UNIT, [reference, own])
    code, run_id, err = _create(capsys, "--seed", seed, "--only-failed")
    assert code == 0, err
    assert _bindings(held, run_id, "difference", UNIT) == sorted([reference, own])


def test_scratch_seed_reruns_every_stage_carrying_only_first_stage_inputs(
        held, cli_db, fake_batch, capsys):
    seed = _seed_run(held, kind="scratch")
    admit = _attempt(held, seed, "admit", UNIT, inputs="s3://deliveries/U",
                     settings="s3://settings/admit.toml")
    _finish(held, seed, admit, "succeeded", select=True, exit_code=0)
    killed = _attempt(held, seed, "difference", UNIT, inputs="s3://scratch/P/inputs/diff")
    _finish(held, seed, killed, "killed")
    foreign = _instance(held, _make_run(held, kind="production"), "admit", "other")
    own = _instance(held, seed, "admit", "own")
    repo.bind_unit_inputs(held, seed, "admit", UNIT, [foreign, own])
    # A failed difference unit with no admit unit in the seed is not carried.
    lone = _attempt(held, seed, "difference", "U-lone", inputs="s3://scratch/P/x")
    _finish(held, seed, lone, "failed", exit_code=1)

    code, run_id, err = _create(capsys, "--seed", seed, "--only-failed")
    assert code == 0, err
    assert "not carried" in err and "U-lone" in err
    assert _one(held, "SELECT selected_stages, kind FROM runs WHERE id = %s", (run_id,)) == (
        CHAIN, "scratch")
    assert _all(held, "SELECT stage, unit_id FROM units WHERE run = %s", (run_id,)) == [
        ("admit", UNIT)]
    assert _bindings(held, run_id, "admit", UNIT) == [foreign]  # never the seed's own

    code, _, err = _start(capsys, run_id)
    assert code == 0, err
    command, flags = _submitted_flags(fake_batch)
    assert command[:2] == ["stage", "admit"]
    assert (flags["--inputs"], flags["--settings"]) == (
        "s3://deliveries/U", "s3://settings/admit.toml")


def _production_seed_with_later_failures(held):
    """UNIT failed at difference; U2 completed through register(difference)
    and failed at load; U3 completed difference and failed at register."""
    seed = _seed_run(held)
    for unit in (UNIT, "U2", "U3"):
        _complete(held, seed, "admit", unit)
        _complete(held, seed, "register", f"admit/{unit}")
    failed = _attempt(held, seed, "difference", UNIT, inputs="s3://scratch/P6/inputs/diff")
    _finish(held, seed, failed, "killed")
    for unit in ("U2", "U3"):
        _complete(held, seed, "difference", unit, inputs=f"s3://scratch/P6/inputs/{unit}")
    _complete(held, seed, "register", "difference/U2")
    failed = _attempt(held, seed, "load", "U2", inputs="s3://products/P6/difference/U2")
    _finish(held, seed, failed, "failed", exit_code=1)
    failed = _attempt(held, seed, "register", "difference/U3",
                      inputs="s3://products/P6/difference/U3")
    _finish(held, seed, failed, "failed", exit_code=1)
    return seed


def test_only_failed_production_seeds_every_position(held, cli_db, capsys):
    seed = _production_seed_with_later_failures(held)
    code, run_id, err = _create(capsys, "--seed", seed, "--only-failed")
    assert code == 0, err
    assert _one(held, "SELECT selected_stages FROM runs WHERE id = %s", (run_id,)) == (
        ["difference", "register", "load"],)
    assert sorted(_all(held, "SELECT stage, unit_id FROM units WHERE run = %s", (run_id,))) == [
        ("difference", UNIT), ("load", "U2"), ("register", "difference/U3")]


def test_start_inherits_stages_the_seed_completed(held, cli_db, fake_batch, capsys):
    seed = _production_seed_with_later_failures(held)
    _, run_id, _ = _create(capsys, "--seed", seed, "--only-failed")

    code = cli.main(["run", "start", run_id, "--unit", "U2", "--no-wait"])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert f"difference U2 inherited from seed {seed}" in out
    assert f"register U2 inherited from seed {seed}" in out
    command, flags = _submitted_flags(fake_batch)
    assert command[:2] == ["stage", "load"]
    assert flags["--inputs"] == "s3://products/P6/difference/U2"


def test_start_seeded_register_at_a_later_position_keeps_the_seed_unit_id(
        held, cli_db, fake_batch, capsys):
    """B4: register has a producing stage (difference) in the new run, but
    its seeded unit's id and inputs are the seed's, not derived."""
    seed = _production_seed_with_later_failures(held)
    _, run_id, _ = _create(capsys, "--seed", seed, "--only-failed")

    code = cli.main(["run", "start", run_id, "--unit", "U3", "--no-wait"])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert f"difference U3 inherited from seed {seed}" in out
    command, flags = _submitted_flags(fake_batch)
    assert command[:2] == ["stage", "register"]
    assert flags["--unit"] == "difference/U3"
    assert flags["--inputs"] == "s3://products/P6/difference/U3"

    # With register done, load reads difference's output from the seed:
    # the producer was inherited from a production seed.
    attempt = _one(held, "SELECT id FROM attempts WHERE run = %s", (run_id,))[0]
    _finish(held, run_id, attempt, "succeeded", select=True, exit_code=0)
    code = cli.main(["run", "start", run_id, "--unit", "U3", "--no-wait"])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert f"difference U3 inherited from seed {seed}" in out
    assert "register difference/U3 already complete" in out
    command, flags = _submitted_flags(fake_batch)
    seed_difference = _one(held, """
        SELECT a.output_location FROM units u JOIN attempts a ON a.id = u.selected_attempt
        WHERE u.run = %s AND u.stage = 'difference' AND u.unit_id = 'U3'""", (seed,))[0]
    assert command[:2] == ["stage", "load"]
    assert flags["--inputs"] == seed_difference


def test_start_after_a_first_position_register_reads_the_seeds_producer(
        held, cli_db, fake_batch, capsys):
    seed = _seed_run(held)
    _complete(held, seed, "admit", UNIT)
    _complete(held, seed, "register", f"admit/{UNIT}")
    _complete(held, seed, "difference", UNIT)
    failed = _attempt(held, seed, "register", f"difference/{UNIT}",
                      inputs="s3://products/P6/difference/out")
    _finish(held, seed, failed, "failed", exit_code=1)
    _, run_id, _ = _create(capsys, "--seed", seed, "--only-failed")
    _start(capsys, run_id)
    attempt = _one(held, "SELECT id FROM attempts WHERE run = %s", (run_id,))[0]
    _finish(held, run_id, attempt, "succeeded", select=True, exit_code=0)

    code, out, err = _start(capsys, run_id)
    assert code == 0, err
    command, flags = _submitted_flags(fake_batch)
    assert command[:2] == ["stage", "load"]
    assert flags["--inputs"] == _one(held, """
        SELECT a.output_location FROM units u JOIN attempts a ON a.id = u.selected_attempt
        WHERE u.run = %s AND u.stage = 'difference'""", (seed,))[0]


# ======================================================================
# Codex diff review of step 6: the leading register position of a seeded
# run, and an explicit --template on an inheritable stage
# ======================================================================

def _seed_failed_at_both_registers(held):
    """UNIT failed at register(admit); U3 completed difference and failed
    at register(difference). The re-run's stages start at the first
    register: [register, difference, register, load]."""
    seed = _seed_run(held)
    _complete(held, seed, "admit", UNIT)
    failed = _attempt(held, seed, "register", f"admit/{UNIT}", inputs="s3://products/P6/admit/U")
    _finish(held, seed, failed, "failed", exit_code=1)
    _complete(held, seed, "admit", "U3")
    _complete(held, seed, "register", "admit/U3")
    _complete(held, seed, "difference", "U3", inputs="s3://scratch/P6/inputs/U3")
    failed = _attempt(held, seed, "register", "difference/U3",
                      inputs="s3://products/P6/difference/U3")
    _finish(held, seed, failed, "failed", exit_code=1)
    return seed


def test_a_leading_register_position_inherits_register_admit_not_a_later_register_unit(
        held, cli_db, fake_batch, capsys):
    seed = _seed_failed_at_both_registers(held)
    code, run_id, err = _create(capsys, "--seed", seed, "--only-failed")
    assert code == 0, err
    assert _one(held, "SELECT selected_stages FROM runs WHERE id = %s", (run_id,)) == (
        ["register", "difference", "register", "load"],)
    assert sorted(_all(held, "SELECT stage, unit_id FROM units WHERE run = %s", (run_id,))) == [
        ("register", f"admit/{UNIT}"), ("register", "difference/U3")]

    # U3: position 0 stands for register(admit/U3), which the seed completed;
    # it and difference are inherited, and the seeded register(difference/U3)
    # runs at position 2.
    code = cli.main(["run", "start", run_id, "--unit", "U3", "--no-wait"])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert f"register U3 inherited from seed {seed}" in out
    assert f"difference U3 inherited from seed {seed}" in out
    command, flags = _submitted_flags(fake_batch)
    assert command[:2] == ["stage", "register"]
    assert flags["--unit"] == "difference/U3"
    assert flags["--inputs"] == "s3://products/P6/difference/U3"

    # With register(difference/U3) done, the walk goes on to load -- it
    # neither re-runs difference nor skips the registration.
    attempt = _one(held, "SELECT id FROM attempts WHERE run = %s", (run_id,))[0]
    _finish(held, run_id, attempt, "succeeded", select=True, exit_code=0)
    code = cli.main(["run", "start", run_id, "--unit", "U3", "--no-wait"])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "register difference/U3 already complete" in out
    command, flags = _submitted_flags(fake_batch)
    assert command[:2] == ["stage", "load"]
    assert _one(held, "SELECT count(*) FROM units WHERE run = %s AND stage = 'difference'",
                (run_id,)) == (0,)

    # UNIT: its seeded register(admit/UNIT) still runs at position 0.
    code = cli.main(["run", "start", run_id, "--unit", UNIT, "--no-wait"])
    out, err = capsys.readouterr()
    assert code == 0, err
    command, flags = _submitted_flags(fake_batch)
    assert command[:2] == ["stage", "register"]
    assert flags["--unit"] == f"admit/{UNIT}"


def test_an_explicit_template_disables_inheritance(held, cli_db, fake_batch, capsys,
                                                   monkeypatch):
    """U2's difference would be inherited from the seed (a seeded load unit
    follows), but ``--template difference=...`` asks for it to run: the
    template is composed against the seed's producer (admit) and difference
    is submitted with it."""
    from rapidpipe.cli import runctl

    seed = _production_seed_with_later_failures(held)
    _, run_id, _ = _create(capsys, "--seed", seed, "--only-failed")
    calls = []

    def _compose(conn, **kwargs):
        calls.append(kwargs)
        return "s3://composed/U2"

    monkeypatch.setattr(runctl, "compose_inputs", _compose)
    code = cli.main(["run", "start", run_id, "--unit", "U2", "--no-wait",
                     "--template", "difference=s3://tmpl/ref"])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "difference U2 inherited" not in out
    assert [(c["stage"], c["from_stage"], c["producer_run"], c["template"]) for c in calls] == [
        ("difference", "admit", seed, "s3://tmpl/ref")]
    command, flags = _submitted_flags(fake_batch)
    assert command[:2] == ["stage", "difference"]
    assert flags["--inputs"] == "s3://composed/U2"
