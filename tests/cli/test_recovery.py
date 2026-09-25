"""Behavioural, black-box tests of recovery (supervisor step 6, 2026-09-24,
R7-R9): argv in, exit code / stdout / stderr and database state out,
against a real PostgreSQL with Batch faked -- ``submit_unit`` freezing an
attempt's inputs/settings, a killed job with no container exit code,
``run create --seed <run> --only-failed``, ``run start`` resolving a
seeded unit's inputs, and ``run reconcile --resolve-jobless``.

Every run/unit/attempt here, including a seed run's prior history, is
built through the CLI itself (``run submit``/``run reconcile``/``run
start``), the same convention ``test_run_lifecycle.py``'s module docstring
describes, with one addition: a job-less attempt (a submission whose Batch
call fails after the allocation already committed, R7) is produced by
making the faked ``submit_job`` raise once, mirroring the real failure
this feature recovers from, rather than inserting the attempt row by hand.
``tests/db/test_recovery.py`` covers the same behaviour in detail against
a held, never-committed connection; this module drives it against real,
separately-committed connections per command, as the CLI actually runs.
"""

from __future__ import annotations

from .test_run_lifecycle import _create_run, _kv, _seed_manifest, _submit_and_complete

UNIT = "e001/SCA01"


class _BatchClientError(Exception):
    """Named to match ``rapidpipe.cli.main._BATCH_ERROR_NAMES`` (its class
    name ends in ``ClientError``), so a submission failure here is handled
    the same way a real botocore ``ClientError`` is: attempt/locations
    already committed, exit 75, job-less."""


def _raise_once_then_restore(monkeypatch, fake_batch):
    """The next ``submit_job`` call raises a Batch-shaped error and
    restores the real one, standing in for one submission whose response
    never reached the caller (R7's motivating failure)."""
    original = fake_batch.submit_job

    def _boom(**_kwargs):
        monkeypatch.setattr(fake_batch, "submit_job", original)
        raise _BatchClientError("simulated Batch outage")

    monkeypatch.setattr(fake_batch, "submit_job", _boom)


def _submitted_flags(fake_batch):
    command = fake_batch.submitted[-1]["containerOverrides"]["command"]
    flags = {}
    for i, token in enumerate(command):
        if token.startswith("--") and i + 1 < len(command):
            flags[token] = command[i + 1]
    return command, flags


def _register_admit_and_complete(cli, fake_batch, fake_s3, run_id, *, admit_unit_id):
    """``register``'s own unit id is always derived from the manifest it
    reads (``--unit`` is refused for it); ``--inputs-from-stage admit``
    names the producing unit whose selected output to read, and the
    result is ``admit/<admit_unit_id>`` (``test_run_start.py``'s own
    example: register's derived unit id from admit's manifest)."""
    submitted = cli("run", "submit", run_id, "register", "--unit", admit_unit_id,
                    "--inputs-from-stage", "admit")
    assert submitted.rc == 0, submitted.err
    attempt_id = _kv(submitted.out, "attempt")
    job_id = _kv(submitted.out, "job")
    output_location = _kv(submitted.out, "outputs")
    fake_batch.set_status(job_id, "SUCCEEDED")
    _seed_manifest(fake_s3, output_location, run_id=run_id, stage="register",
                   unit_id=f"admit/{admit_unit_id}", attempt_id=attempt_id)
    reconciled = cli("run", "reconcile", run_id)
    assert reconciled.rc == 0, reconciled.err
    return attempt_id


def _unit_state(db, run_id, stage, unit_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT state FROM units WHERE run = %s AND stage = %s AND unit_id = %s",
            (run_id, stage, unit_id))
        return cur.fetchone()[0]


# ======================================================================
# submit_unit freezes the attempt's inputs and settings (R7)
# ======================================================================

def test_run_start_records_attempt_inputs_and_settings_locations(
        cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", stages="admit")
    result = cli("run", "start", run_id, "--unit", "U", "--inputs",
                "admit=s3://in-bucket/in-prefix", "--settings",
                "admit=s3://in-bucket/admit.toml", "--no-wait")
    assert result.rc == 0, result.err
    attempt_id = _kv(result.out, "attempt")
    with db.cursor() as cur:
        cur.execute(
            "SELECT inputs_location, settings_location FROM attempts WHERE id = %s",
            (attempt_id,))
        assert cur.fetchone() == ("s3://in-bucket/in-prefix", "s3://in-bucket/admit.toml")


# ======================================================================
# A FAILED job with no container exit code becomes 'killed'; the unit is
# failed (terminal), and run start exits 1.
# ======================================================================

def test_batch_failure_with_no_container_exit_code_becomes_killed(
        cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", stages="admit")
    submitted = cli("run", "start", run_id, "--unit", "U", "--inputs",
                    "admit=s3://in-bucket/x", "--no-wait")
    assert submitted.rc == 0, submitted.err
    job_id = _kv(submitted.out, "job")
    fake_batch.set_status(job_id, "FAILED")  # no container_exit_code: "killed" (R9 inventory)

    result = cli("run", "start", run_id, "--unit", "U")
    assert result.rc == 1
    assert f"run={run_id} state=failed" in result.out
    assert _unit_state(db, run_id, "admit", "U") == "failed"
    with db.cursor() as cur:
        cur.execute("SELECT disposition, exit_code FROM attempts WHERE run = %s", (run_id,))
        assert cur.fetchone() == ("killed", None)


# ======================================================================
# run create --seed <run> --only-failed: the new run's id and stage
# suffix, one seeded unit per non-complete seed unit, configuration
# copied; and run start resolving a seeded unit's inputs/settings from
# the seed attempt (R8), with an explicit --inputs winning.
# ======================================================================

def _build_seed_with_failed_difference(cli, db, fake_batch, fake_s3, batch_env, monkeypatch):
    """A P6-shaped seed (tests/db/test_recovery.py's
    ``_seed_with_failed_difference``, built through the CLI): admit and
    register(admit) complete; difference U killed; difference U-jobless
    left with a job-less attempt (its submission failed after allocation);
    difference U-flight running with a job (in flight, not re-run);
    difference U-ok complete."""
    seed = _create_run(
        cli, db, kind="production", purpose="step 6 demo P6",
        stages="admit,register,difference,register,load", owner="cli-recovery-owner",
        extra=("--max-attempts", "3", "--check-policy", "rebuild-strict@1"))

    _submit_and_complete(cli, fake_batch, fake_s3, seed, unit_id=UNIT, stage="admit")
    _register_admit_and_complete(cli, fake_batch, fake_s3, seed, admit_unit_id=UNIT)

    killed = cli("run", "submit", seed, "difference", "--unit", UNIT, "--inputs",
                "s3://scratch/P6/inputs/diff", "--settings", "s3://settings/difference.toml")
    assert killed.rc == 0, killed.err
    fake_batch.set_status(_kv(killed.out, "job"), "FAILED")  # no exit code: killed
    reconciled = cli("run", "reconcile", seed)
    assert reconciled.rc == 0, reconciled.err

    _raise_once_then_restore(monkeypatch, fake_batch)
    jobless = cli("run", "submit", seed, "difference", "--unit", "U-jobless", "--inputs",
                 "s3://scratch/P6/inputs/j")
    assert jobless.rc == 75, jobless.err  # a Batch-shaped error: transient, job-less

    in_flight = cli("run", "submit", seed, "difference", "--unit", "U-flight", "--inputs",
                    "s3://scratch/P6/inputs/f")
    assert in_flight.rc == 0, in_flight.err
    fake_batch.set_status(_kv(in_flight.out, "job"), "RUNNING")

    _submit_and_complete(cli, fake_batch, fake_s3, seed, unit_id="U-ok", stage="difference")
    return seed


def test_only_failed_creates_a_re_run_with_the_copied_configuration_and_seeded_units(
        cli, db, fake_batch, fake_s3, batch_env, monkeypatch):
    seed = _build_seed_with_failed_difference(cli, db, fake_batch, fake_s3, batch_env, monkeypatch)

    created = cli("run", "create", "--seed", seed, "--only-failed")
    assert created.rc == 0, created.err
    new_run = created.out.strip()
    db.track_run(new_run)
    assert "seeded 2 unit(s)" in created.err

    with db.cursor() as cur:
        cur.execute(
            "SELECT kind, owner, selected_stages, max_attempts_per_unit, check_policy_ref, "
            "seed_run FROM runs WHERE id = %s", (new_run,))
        assert cur.fetchone() == (
            "production", "cli-recovery-owner", ["difference", "register", "load"], 3,
            "rebuild-strict@1", seed)

    with db.cursor() as cur:
        cur.execute(
            "SELECT u.stage, u.unit_id, s.unit_id, s.run FROM units u "
            "JOIN units s ON s.id = u.seeded_from_unit "
            "WHERE u.run = %s ORDER BY u.unit_id", (new_run,))
        assert cur.fetchall() == [
            ("difference", UNIT, UNIT, seed),
            ("difference", "U-jobless", "U-jobless", seed)]
        cur.execute("SELECT count(*) FROM units WHERE run = %s", (new_run,))
        assert cur.fetchone() == (2,)

    # run start with no --inputs resolves the seed attempt's own recorded
    # inputs/settings (R8).
    started = cli("run", "start", new_run, "--unit", UNIT, "--no-wait")
    assert started.rc == 0, started.err
    _command, flags = _submitted_flags(fake_batch)
    assert flags["--inputs"] == "s3://scratch/P6/inputs/diff"
    assert flags["--settings"] == "s3://settings/difference.toml"
    with db.cursor() as cur:
        cur.execute(
            "SELECT inputs_location, settings_location FROM attempts WHERE run = %s "
            "AND unit = (SELECT id FROM units WHERE run = %s AND unit_id = %s)",
            (new_run, new_run, UNIT))
        assert cur.fetchone() == ("s3://scratch/P6/inputs/diff", "s3://settings/difference.toml")

    # An explicit --inputs wins over the seed's recorded one.
    explicit = cli("run", "start", new_run, "--unit", "U-jobless", "--inputs",
                   "difference=s3://mine/in", "--no-wait")
    assert explicit.rc == 0, explicit.err
    _command2, flags2 = _submitted_flags(fake_batch)
    assert flags2["--inputs"] == "s3://mine/in"
    assert "--settings" not in flags2  # U-jobless's seed attempt recorded no settings


# ======================================================================
# --only-failed refusals: no --seed; a seed with nothing failed.
# ======================================================================

def test_only_failed_requires_seed(cli):
    result = cli("run", "create", "--only-failed")
    assert result.rc == 64
    assert "--only-failed requires --seed" in result.err


def test_only_failed_refuses_a_seed_with_nothing_failed(cli, db, fake_batch, fake_s3, batch_env):
    clean = _create_run(cli, db, kind="production", stages="admit")
    _submit_and_complete(cli, fake_batch, fake_s3, clean, unit_id=UNIT, stage="admit")
    result = cli("run", "create", "--seed", clean, "--only-failed")
    assert result.rc == 64
    assert "has no non-complete unit" in result.err


# ======================================================================
# run reconcile --resolve-jobless (R9): a job-less attempt is recorded
# lost; the unit returns to ready with allowance left, else failed; run
# start then allocates a new attempt.
# ======================================================================

def test_resolve_jobless_returns_the_unit_to_ready_and_run_start_allocates_a_new_attempt(
        cli, db, fake_batch, fake_s3, batch_env, monkeypatch):
    run_id = _create_run(cli, db, kind="scratch", stages="admit", extra=("--max-attempts", "2"))
    _raise_once_then_restore(monkeypatch, fake_batch)
    failed = cli("run", "submit", run_id, "admit", "--unit", "U", "--inputs", "s3://in-bucket/x")
    assert failed.rc == 75, failed.err

    resolved = cli("run", "reconcile", run_id, "--resolve-jobless", "--older-than", "0")
    assert resolved.rc == 0, resolved.err
    assert "status=NOJOB disposition=lost" in resolved.out
    assert _unit_state(db, run_id, "admit", "U") == "ready"  # allowance remains (1 of 2 used)

    started = cli("run", "start", run_id, "--unit", "U", "--inputs", "s3://in-bucket/x",
                  "--no-wait")
    assert started.rc == 0, started.err
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM attempts WHERE run = %s", (run_id,))
        assert cur.fetchone() == (2,)


def test_resolve_jobless_fails_the_unit_with_no_allowance_left(
        cli, db, fake_batch, fake_s3, batch_env, monkeypatch):
    run_id = _create_run(cli, db, kind="scratch", stages="admit", extra=("--max-attempts", "1"))
    _raise_once_then_restore(monkeypatch, fake_batch)
    failed = cli("run", "submit", run_id, "admit", "--unit", "U", "--inputs", "s3://in-bucket/x")
    assert failed.rc == 75, failed.err

    resolved = cli("run", "reconcile", run_id, "--resolve-jobless", "--older-than", "0")
    assert resolved.rc == 0, resolved.err
    assert "status=NOJOB disposition=lost" in resolved.out
    assert _unit_state(db, run_id, "admit", "U") == "failed"  # no allowance left
    with db.cursor() as cur:
        cur.execute("SELECT reconcile_note FROM attempts WHERE run = %s", (run_id,))
        assert cur.fetchone() == ("no scheduler job after 0 s",)


# ======================================================================
# Help walk: run create's --seed/--only-failed and run reconcile's
# --resolve-jobless are on existing subcommands, already covered by
# test_help.py's walk; nothing new to add here.
# ======================================================================
