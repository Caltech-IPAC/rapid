"""Behavioural, black-box tests of ``rapidpipe loop run|plan|show`` over a
two-date spec: argv in, exit code / stdout and database state out, against
the CI PostgreSQL with Batch and S3 faked.

The loop waits for each attempt through ``run start``'s walk, which polls
``rapidpipe.cli.runctl._reconcile``. As in ``test_run_start.py``, that
seam is wrapped: before the real reconcile runs, every unresolved attempt
of the run gets a SUCCEEDED job and a manifest shaped like its stage's
(``_FakeStages``): admit an ``l2-image`` bundle, finalize a
``difference-image`` bundle, load a ``source-set`` whose rows the test
writes into a real ``sources_<yyyymmdd>_<sca>`` table (two fields), and
crossmatch an ``association-set``, both registered in ``product_instances``
-- which the real stages do themselves; the loop reads a source set's
fields, and chooses a base, only when its run may read the set
(supervisor step 9 R2), and the association set is how the next date
finds its base (R5). Everything else writes an empty-output manifest or a
result-set entry. Every stage that registers its own outputs (load,
crossmatch, statistics, prune, alerts) registers them here too; register
is not faked, so admit's l2-image and finalize's difference image stay
unregistered.

Every test uses the launcher's real input-manifest read
(``real_input_manifest``; supervisor step 9, R4): the delivery and the
difference template are seeded with manifests, the template registered
under a producer run of its own, so each unit's ``unit_inputs`` rows are
written at submission and asserted.
"""

from __future__ import annotations

import hashlib
import json
import random

import pytest

from rapidpipe.cli import runctl
from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import repository

from .conftest import FAKE_BUCKET, _delete_run_rows

TABLE = "sources_29990101_01"
FIELDS = (101, 102)
TEMPLATE = f"s3://{FAKE_BUCKET}/control/template"
DELIVERY = f"s3://{FAKE_BUCKET}/control/delivery/r0034001002001001001-sca01"
UNIT = "r0034001002001001001/SCA01"
PROD_DEF = "rapid-production:7"
DIGEST = "sha256:" + "b" * 64

pytestmark = pytest.mark.real_input_manifest


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _member(fake_s3, prefix: str, path: str, data: bytes) -> dict:
    fake_s3.seed(FAKE_BUCKET, f"{prefix}/{path}", data)
    return {"role": "primary", "path": path, "bytes": len(data), "sha256": _sha(data)}


def _manifest(run_id, stage, unit_id, attempt_id, outputs, unit_kind="detector-image"):
    return {
        "schema_version": "1", "run": run_id, "unit": {"kind": unit_kind, "id": unit_id},
        "stage": stage, "attempt": attempt_id, "execution_record": f"exec/{attempt_id}.json",
        "inputs": {"manifest": "x", "products": {}, "result_sets": []},
        "outputs": outputs,
    }


#: Stages whose real implementation registers its own outputs
#: (``register_manifest`` in rapidpipe/stages/<stage>.py).
SELF_REGISTERING = ("load", "crossmatch", "statistics", "prune", "alerts")


def _prefix(location: str) -> str:
    assert location.startswith(f"s3://{FAKE_BUCKET}/")
    return location[len(f"s3://{FAKE_BUCKET}/"):]


class _FakeStages:
    """Completes every unresolved attempt with a stage-shaped manifest."""

    def __init__(self, db, fake_batch, fake_s3, *, fail=None, execution_record=None):
        self.db, self.fake_batch, self.fake_s3 = db, fake_batch, fake_s3
        self.fail = fail  # (stage, unit_id) to give a FAILED job
        self.execution_record = execution_record  # written as exec/<attempt>.json
        self.finalized: dict[str, str] = {}  # unit -> finalized difference instance
        self.stop_after = None  # stage whose first completion raises KeyboardInterrupt

    def _outputs(self, run_id, stage, unit_id, attempt_id, prefix):
        s3 = self.fake_s3
        if stage == "admit":
            return [{"kind": "l2-image", "format_version": "1", "instance": new_ulid(),
                     "key": {"unit": unit_id}, "primary": "l2/image.fits",
                     "members": [_member(s3, prefix, "l2/image.fits", b"L2" * 8)]}]
        if stage == "finalize":
            self.finalized[unit_id] = new_ulid()
            return [{"kind": "difference-image", "format_version": "1",
                     "instance": self.finalized[unit_id],
                     "key": {"unit": unit_id}, "primary": "diff/final.fits",
                     "members": [_member(s3, prefix, "diff/final.fits", b"DIFF" * 5)]}]
        if stage == "load":
            instance = new_ulid()
            with self.db.cursor() as cur:
                for f in FIELDS:
                    cur.execute(f"INSERT INTO {TABLE} (field, result_set) VALUES (%s, %s)",
                                (f, instance))
            return [{"kind": "source-set", "format_version": "1", "instance": instance,
                     "key": {"difference": self.finalized[unit_id]}, "primary": None,
                     "members": [],
                     "registration": {"table": TABLE, "row_count": len(FIELDS)}}]
        if stage == "crossmatch":
            return [{"kind": "association-set", "format_version": "1", "instance": new_ulid(),
                     "key": {"field": int(unit_id)}, "primary": None, "members": [],
                     "registration": {"astroobjects_table": f"astroobjects_{unit_id}"}}]
        if stage == "statistics":
            return [{"kind": "statistics-set", "format_version": "1", "instance": new_ulid(),
                     "key": {"field": int(unit_id)}, "primary": None, "members": []}]
        if stage == "prune":
            # prune's key names its base: the field's newest association set.
            with self.db.cursor() as cur:
                cur.execute("SELECT id FROM product_instances WHERE kind = 'association-set' "
                            "AND logical_key ->> 'field' = %s ORDER BY id DESC LIMIT 1",
                            (unit_id,))
                base = cur.fetchone()[0]
            return [{"kind": "pruned-set", "format_version": "1", "instance": new_ulid(),
                     "key": {"base": base, "settings_hash": "sha256:" + "0" * 64},
                     "primary": None, "members": []}]
        if stage == "alerts":
            return [{"kind": "alert-container", "format_version": "1", "instance": new_ulid(),
                     "key": {"unit": unit_id}, "primary": "alerts.avro",
                     "members": [_member(s3, prefix, "alerts.avro", b"AVRO")]}]
        return []

    def install(self, monkeypatch):
        original = runctl._reconcile

        def wrapper(conn, run_id):
            with self.db.cursor() as cur:
                cur.execute(
                    "SELECT u.stage, u.unit_id, u.unit_kind, a.id, a.scheduler_job_id, "
                    "a.output_location FROM attempts a JOIN units u ON u.id = a.unit "
                    "WHERE a.run = %s AND a.disposition IS NULL "
                    "AND a.scheduler_job_id IS NOT NULL", (run_id,))
                rows = cur.fetchall()
            for stage, unit_id, unit_kind, attempt_id, job_id, location in rows:
                if self.fail == (stage, unit_id):
                    self.fake_batch.set_status(job_id, "FAILED", container_exit_code=1)
                    continue
                prefix = _prefix(location)
                outputs = self._outputs(run_id, stage, unit_id, attempt_id, prefix)
                manifest = _manifest(run_id, stage, unit_id, attempt_id, outputs, unit_kind)
                self.fake_s3.seed(FAKE_BUCKET, f"{prefix}/manifest.json",
                                  json.dumps(manifest).encode())
                if self.execution_record is not None:
                    self.fake_s3.seed(FAKE_BUCKET, f"{prefix}/exec/{attempt_id}.json",
                                      json.dumps(self.execution_record).encode())
                self.fake_batch.set_status(job_id, "SUCCEEDED")
                if stage in SELF_REGISTERING:
                    # The real stage registers its own outputs.
                    repository.register_manifest(self.db.connection, manifest,
                                                 registering_attempt_id=attempt_id)
            result = original(conn, run_id)
            if self.stop_after and any(r[0] == self.stop_after for r in rows):
                self.stop_after = None
                raise KeyboardInterrupt("interrupted")
            return result

        monkeypatch.setattr(runctl, "_reconcile", wrapper)
        monkeypatch.setattr(runctl, "sleep", lambda s: None)


@pytest.fixture()
def world(db, fake_batch, fake_s3, batch_env, monkeypatch):
    """A complete release with a production job definition, the template
    and delivery objects, the sources table, and teardown of it all."""
    tag = f"rebuild-v0.{random.randrange(10**6, 10**9)}"
    schedule = f"test-loop-{new_ulid()}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO releases (tag, source_revision, schema_version, image_digest, state, "
            "cut_by) VALUES (%s, %s, '20260924-11-loop-dates.sql', %s, 'complete', 'test')",
            (tag, "a" * 40, DIGEST))
        cur.execute(
            "INSERT INTO release_deployments (release, consumer, job_definition, deployed_by) "
            "VALUES (%s, 'rapid-pipeline-production', %s, 'test')", (tag, PROD_DEF))
        cur.execute("SELECT to_regclass(%s) IS NULL", (TABLE,))
        (made_table,) = cur.fetchone()
        cur.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (field integer, result_set text)")
    fake_batch.job_definitions[PROD_DEF] = "ACTIVE"

    # The difference template is an earlier run's registered product (R4:
    # the difference and alerts units bind its instances at submission).
    conn = db.connection
    template_run = repository.create_run(
        conn, "scratch", "test", "loop template producer", ["difference"], "a" * 40,
        None, "20260924-11-loop-dates.sql", None, None, "prompt", "default", "rapid",
        1, False, None)
    repository.add_unit(conn, template_run, "difference", "detector-image", UNIT)
    template_attempt = repository.allocate_attempt(conn, template_run, "difference", UNIT)
    conn.commit()
    template_prefix = _prefix(TEMPLATE)
    template = _manifest(template_run, "difference", UNIT, template_attempt, [
        {"kind": "l2-image", "format_version": "1", "instance": new_ulid(),
         "key": {"unit": UNIT}, "primary": "l2/old.fits",
         "members": [_member(fake_s3, template_prefix, "l2/old.fits", b"OLD")]},
        {"kind": "reference-catalog", "format_version": "1", "instance": new_ulid(),
         "key": {"field": 101}, "primary": "ref/cat.txt",
         "members": [_member(fake_s3, template_prefix, "ref/cat.txt", b"REFCAT")]},
    ])
    fake_s3.seed(FAKE_BUCKET, f"{template_prefix}/manifest.json", json.dumps(template).encode())
    repository.register_manifest(conn, template, registering_attempt_id=template_attempt)
    conn.commit()

    # admit's input: a delivery manifest naming one raw image that no run
    # registered, so the real reader (R4) accepts it and binds nothing.
    delivery_prefix = _prefix(DELIVERY)
    delivery = _manifest("delivery", "delivery", UNIT, new_ulid(), [
        {"kind": "l1-image", "format_version": "1", "instance": new_ulid(),
         "key": {"unit": UNIT}, "primary": "l1/raw.fits",
         "members": [_member(fake_s3, delivery_prefix, "l1/raw.fits", b"RAW" * 4)]}])
    fake_s3.seed(FAKE_BUCKET, f"{delivery_prefix}/manifest.json", json.dumps(delivery).encode())

    spec_key = f"control/loop/{schedule}.toml"
    fake_s3.seed(FAKE_BUCKET, spec_key, f"""
[loop]
schedule = "{schedule}"
release = "{tag}"
kind = "production"
owner = "scheduler-test"
lane = "prompt"
max_attempts = 2

[[dates]]
processing_date = 2027-10-01
[[dates.detector_images]]
delivery = "{DELIVERY}"
admit_settings = "s3://{FAKE_BUCKET}/settings/admit.toml"
difference_template = "{TEMPLATE}"
difference_settings = "s3://{FAKE_BUCKET}/settings/difference.toml"

[[dates]]
processing_date = 2027-10-02
[[dates.detector_images]]
delivery = "{DELIVERY}"
difference_template = "{TEMPLATE}"
""".encode())

    state = {"tag": tag, "digest": DIGEST, "schedule": schedule,
             "spec": f"s3://{FAKE_BUCKET}/{spec_key}",
             "template": {o["kind"]: o["instance"] for o in template["outputs"]}}
    yield state

    with conn.cursor() as cur:
        cur.execute("SELECT run, promotion FROM loop_dates WHERE schedule = %s", (schedule,))
        rows = cur.fetchall()
        cur.execute("SELECT id FROM runs WHERE release = %s", (tag,))
        runs = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT id FROM promotions WHERE request_context->>'run' = ANY(%s)", (runs,))
        promotions = [r[0] for r in cur.fetchall()]
        cur.execute("DELETE FROM loop_dates WHERE schedule = %s", (schedule,))
        cur.execute("DELETE FROM checks WHERE instance IN "
                    "(SELECT id FROM product_instances WHERE run = ANY(%s))", (runs,))
        cur.execute("DELETE FROM unit_inputs WHERE unit IN "
                    "(SELECT id FROM units WHERE run = ANY(%s))", (runs,))
        cur.execute(f"DELETE FROM {TABLE} WHERE result_set IN "
                    "(SELECT id FROM product_instances WHERE run = ANY(%s))", (runs,))
    _delete_run_rows(conn, runs + [r for r, _ in rows if r not in runs], promotions)
    _delete_run_rows(conn, [template_run], [])
    with conn.cursor() as cur:
        if made_table:
            cur.execute(f"DROP TABLE {TABLE}")
        else:
            cur.execute(f"DELETE FROM {TABLE} WHERE result_set NOT IN "
                        "(SELECT id FROM product_instances)")
        cur.execute("DELETE FROM release_deployments WHERE release = %s", (tag,))
        cur.execute("DELETE FROM releases WHERE tag = %s", (tag,))


def _rows(db, schedule):
    with db.cursor() as cur:
        cur.execute("SELECT processing_date::text, run, state, promotion, record "
                    "FROM loop_dates WHERE schedule = %s ORDER BY processing_date", (schedule,))
        return cur.fetchall()


def _bound(db, run_id, stage, unit_id) -> set[str]:
    """The instances ``unit_inputs`` binds to one unit (R4)."""
    with db.cursor() as cur:
        cur.execute("SELECT ui.producer_instance FROM unit_inputs ui JOIN units u "
                    "ON u.id = ui.unit WHERE u.run = %s AND u.stage = %s AND u.unit_id = %s",
                    (run_id, stage, unit_id))
        return {row[0] for row in cur.fetchall()}


def _read(fake_s3, location):
    body = fake_s3.get_object(Bucket=FAKE_BUCKET, Key=_prefix(location))["Body"].read()
    return json.loads(body)


def test_loop_runs_two_dates_binding_the_first_dates_association_sets(
        cli, db, fake_batch, fake_s3, world, monkeypatch):
    # Every attempt ran the release's image, so the date's run promotes.
    _FakeStages(db, fake_batch, fake_s3, execution_record={
        "image_digest": world["digest"], "release": world["tag"]}).install(monkeypatch)

    planned = cli("loop", "plan", "--spec", world["spec"])
    assert planned.rc == 0, planned.err
    assert "date=2027-10-01 action=create" in planned.out
    assert "base=none (first date)" in planned.out

    result = cli("loop", "run", "--spec", world["spec"], "--interval", "1")
    assert result.rc == 0, result.err + result.out
    assert "date=2027-10-01" in result.out and "date=2027-10-02" in result.out

    rows = _rows(db, world["schedule"])
    assert [(d, s) for d, _, s, _, _ in rows] == [("2027-10-01", "complete"),
                                                  ("2027-10-02", "complete")]
    run1, run2 = rows[0][1], rows[1][1]
    assert run1 != run2

    # Two production runs under the release, with the spec's configuration.
    with db.cursor() as cur:
        cur.execute("SELECT id, kind, owner, lane, release, state, selected_stages, "
                    "input_selection_ref, max_attempts_per_unit, purpose FROM runs "
                    "WHERE release = %s ORDER BY created", (world["tag"],))
        runs = cur.fetchall()
    assert [r[0] for r in runs] == [run1, run2]
    for r in runs:
        assert r[1:6] == ("production", "scheduler-test", "prompt", world["tag"], "finished")
        # A1: the raw difference is never registered.
        assert r[6] == ["admit", "register", "difference", "finalize", "register", "load",
                        "maintain", "crossmatch", "statistics", "prune", "alerts"]
        assert (r[7], r[8]) == (world["spec"], 2)
    assert runs[0][9] == f"processing date 2027-10-01 (schedule {world['schedule']})"

    # The second date's crossmatch input set carries the first date's
    # association set for the same field, and lists both in result_sets.
    record1, record2 = rows[0][4], rows[1][4]
    assert record1["fields"] == list(FIELDS)
    assert record1["base_sets"] == {"101": None, "102": None}
    for f in FIELDS:
        first_set = record1["association_sets"][str(f)]
        assert record2["base_sets"][str(f)] == first_set
        assert record2["bases"][str(f)] == {"run": run1, "processing_date": "2027-10-01",
                                            "base_promoted": True}
        manifest = _read(fake_s3, f"s3://{FAKE_BUCKET}/scratch/runs/{run2}/inputs/"
                                  f"crossmatch/{f}/manifest.json")
        bases = [o for o in manifest["outputs"] if o["kind"] == "association-set"]
        assert [b["instance"] for b in bases] == [first_set]
        assert bases[0]["key"]["field"] == f
        sources = [o for o in manifest["outputs"] if o["kind"] == "source-set"]
        assert len(sources) == 1
        assert set(manifest["inputs"]["result_sets"]) == {sources[0]["instance"], first_set}
        first = _read(fake_s3, f"s3://{FAKE_BUCKET}/scratch/runs/{run1}/inputs/"
                               f"crossmatch/{f}/manifest.json")
        assert [o["kind"] for o in first["outputs"]] == ["source-set"]

    # The alerts input set: the finalized difference image, the template's
    # reference catalog (members copied), and every set as result_sets.
    alerts_in = _read(fake_s3, f"s3://{FAKE_BUCKET}/scratch/runs/{run2}/inputs/alerts/"
                               f"{UNIT}/manifest.json")
    assert [o["kind"] for o in alerts_in["outputs"]] == ["difference-image", "reference-catalog"]
    # R5: per field, the association, statistics and pruned sets.
    assert len(alerts_in["inputs"]["result_sets"]) == 1 + 3 * len(FIELDS)
    for f in FIELDS:
        assert record2["pruned_sets"][str(f)] in alerts_in["inputs"]["result_sets"]
    fake_s3.head_object(Bucket=FAKE_BUCKET,
                        Key=f"scratch/runs/{run2}/inputs/alerts/{UNIT}/diff/final.fits")
    assert record2["alerts"][UNIT]["instance"]

    # R4 under the real manifest reader: every unit binds, at submission,
    # the registered instances its input set names.
    template = world["template"]
    for run in (run1, run2):
        # difference's composed input set is admit's l2-image (not
        # registered by the fakes) in place of the template's, plus the
        # template's reference catalog, an earlier run's registered product.
        assert _bound(db, run, "difference", UNIT) == {template["reference-catalog"]}
        # admit's delivery manifest names nothing registered.
        assert _bound(db, run, "admit", UNIT) == set()
    for f in FIELDS:
        sources = [o["instance"] for o in _read(
            fake_s3, f"s3://{FAKE_BUCKET}/scratch/runs/{run1}/inputs/crossmatch/{f}/"
                     "manifest.json")["outputs"]]
        assert _bound(db, run1, "crossmatch", str(f)) == set(sources)
        # The second date's crossmatch also binds its base: the first
        # date's association set, another run's result set.
        manifest = _read(fake_s3, f"s3://{FAKE_BUCKET}/scratch/runs/{run2}/inputs/"
                                  f"crossmatch/{f}/manifest.json")
        assert _bound(db, run2, "crossmatch", str(f)) == set(manifest["inputs"]["result_sets"])
        assert record1["association_sets"][str(f)] in _bound(db, run2, "crossmatch", str(f))
        # statistics and prune bind the field's association set.
        for stage in ("statistics", "prune"):
            assert _bound(db, run2, stage, str(f)) == {record2["association_sets"][str(f)]}
    # alerts binds its source set, and per field the association, statistics
    # and pruned sets (#144), plus the template's reference catalog; the
    # finalized difference image is not registered by the fakes.
    alerts_bound = _bound(db, run2, "alerts", UNIT)
    assert alerts_bound == set(alerts_in["inputs"]["result_sets"]) | {
        template["reference-catalog"]}
    for f in FIELDS:
        assert {record2["pruned_sets"][str(f)], record2["statistics_sets"][str(f)],
                record2["association_sets"][str(f)]} <= alerts_bound

    # The record's shape.
    for record in (record1, record2):
        assert {"spec", "release", "run", "units", "fields", "base_sets", "alerts",
                "promotion", "promotion_gate"} <= set(record)
        stages = {(u["stage"], u["unit"]) for u in record["units"]}
        assert ("maintain", "29990101/SCA01") in stages
        assert ("crossmatch", "101") in stages and ("prune", "102") in stages
        registers = sorted(u for s, u in stages if s == "register")
        assert registers == [f"admit/{UNIT}", f"finalize/{UNIT}"]
        assert all(u["state"] == "complete" and u["attempt"] and u["job"]
                   for u in record["units"])
        assert record["promotion"]  # a promotion id or "refused: ..."
    for _, _, _, promotion, record in rows:
        assert promotion is not None and record["promotion"] == promotion
        # Step 6's gate: the default policy (the spec names none) checks
        # difference-image and source-set candidates. The fake load registers
        # its source set, as the real stage does (R2: the loop reads only
        # registered, readable sets), so the policy's optional catalog check
        # runs on it; it is not required, and the promotion stands.
        assert record["promotion_gate"] == "check policy rebuild-trial@1"
        assert [(c["check"], c["required"]) for c in record["checks"]] == [
            ("catalog-counts-vs-reference@1", False)]
    with db.cursor() as cur:
        cur.execute("SELECT who, reason, request_context->>'run' FROM promotions "
                    "WHERE id = ANY(%s) ORDER BY happened_at", ([r[3] for r in rows],))
        assert cur.fetchall() == [("scheduler", "processing date 2027-10-01", run1),
                                  ("scheduler", "processing date 2027-10-02", run2)]

    shown = cli("loop", "show", world["schedule"])
    assert shown.rc == 0, shown.err
    lines = shown.out.splitlines()
    assert lines[0].startswith("2027-10-01\tcomplete\trun=" + run1)
    assert lines[1].startswith("2027-10-02\tcomplete\trun=" + run2)
    as_json = cli("loop", "show", world["schedule"], "--json")
    assert json.loads(as_json.out.splitlines()[1])["run"] == run1

    # A rerun does nothing: both dates are complete.
    again = cli("loop", "run", "--spec", world["spec"])
    assert again.rc == 0, again.err
    assert again.out.count("(skipped)") == 2


def test_loop_records_a_refused_promotion_and_still_completes_the_date(
        cli, db, fake_batch, fake_s3, world, monkeypatch):
    # No execution records: no attempt ran a released image, so promote_run
    # refuses -- a science outcome recorded on the row, not a failure (R6).
    _FakeStages(db, fake_batch, fake_s3).install(monkeypatch)
    result = cli("loop", "run", "--spec", world["spec"], "--date", "2027-10-01")
    assert result.rc == 0, result.err + result.out
    ((_, run_id, state, promotion, record),) = _rows(db, world["schedule"])
    assert (state, promotion) == ("complete", None)
    assert record["promotion"].startswith("refused: ")
    assert "not the image of a complete release" in record["promotion"]
    with db.cursor() as cur:
        cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone()[0] == "finished"


def test_loop_resumes_an_interrupted_date_with_the_same_run(
        cli, db, fake_batch, fake_s3, world, monkeypatch):
    stages = _FakeStages(db, fake_batch, fake_s3)
    stages.install(monkeypatch)
    stages.stop_after = "load"
    with pytest.raises(KeyboardInterrupt):
        cli("loop", "run", "--spec", world["spec"], "--date", "2027-10-01")
    (row,) = _rows(db, world["schedule"])
    assert row[2] == "open"
    run_id = row[1]

    resumed = cli("loop", "run", "--spec", world["spec"], "--date", "2027-10-01")
    assert resumed.rc == 0, resumed.err + resumed.out
    assert f"date=2027-10-01 run={run_id} resumed" in resumed.out
    assert "admit r0034001002001001001/SCA01 already complete" in resumed.out
    (row,) = _rows(db, world["schedule"])
    assert (row[1], row[2]) == (run_id, "complete")
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM runs WHERE release = %s", (world["tag"],))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM attempts WHERE run = %s AND stage = 'admit'",
                    (run_id,))
        assert cur.fetchone()[0] == 1
    # R4 across the interruption: difference bound before it, crossmatch and
    # alerts after it, each under the real manifest reader.
    assert _bound(db, run_id, "difference", UNIT) == {world["template"]["reference-catalog"]}
    record = row[4]
    for f in FIELDS:
        assert record["association_sets"][str(f)] in _bound(db, run_id, "alerts", UNIT)
        assert record["pruned_sets"][str(f)] in _bound(db, run_id, "alerts", UNIT)
        assert len(_bound(db, run_id, "crossmatch", str(f))) == 1  # the date's source set


def test_loop_a_failed_unit_fails_the_date_and_leaves_later_dates(
        cli, db, fake_batch, fake_s3, world, monkeypatch):
    _FakeStages(db, fake_batch, fake_s3, fail=("statistics", "102")).install(monkeypatch)
    result = cli("loop", "run", "--spec", world["spec"])
    assert result.rc == 1, result.err + result.out
    rows = _rows(db, world["schedule"])
    assert [(d, s) for d, _, s, _, _ in rows] == [("2027-10-01", "failed")]
    assert "statistics 102" in rows[0][4]["failure"]


def test_loop_refuses_a_release_that_is_not_complete(cli, db, fake_batch, fake_s3, world):
    with db.cursor() as cur:
        cur.execute("UPDATE releases SET state = 'built' WHERE tag = %s", (world["tag"],))
    result = cli("loop", "run", "--spec", world["spec"])
    assert result.rc == 64
    assert "not complete" in result.err
    assert _rows(db, world["schedule"]) == []


def test_loop_exits_75_while_another_loop_holds_the_schedule(
        cli, db, fake_batch, fake_s3, world, monkeypatch):
    _FakeStages(db, fake_batch, fake_s3).install(monkeypatch)
    with db.cursor() as cur:  # db's connection is autocommit: a session lock
        cur.execute("SELECT pg_advisory_lock(hashtext('rapidpipe.loop:' || %s))",
                    (world["schedule"],))
    try:
        result = cli("loop", "run", "--spec", world["spec"])
    finally:
        with db.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext('rapidpipe.loop:' || %s))",
                        (world["schedule"],))
    assert result.rc == 75
    assert f"another loop holds schedule {world['schedule']}" in result.out
    assert _rows(db, world["schedule"]) == []


def test_loop_retry_failed_re_runs_the_failed_units_on_a_seeded_run(
        cli, db, fake_batch, fake_s3, world, monkeypatch):
    stages = _FakeStages(db, fake_batch, fake_s3, fail=("statistics", "102"))
    stages.install(monkeypatch)
    assert cli("loop", "run", "--spec", world["spec"], "--date", "2027-10-01").rc == 1
    ((_, run1, state, _, record),) = _rows(db, world["schedule"])
    assert state == "failed" and "statistics 102" in record["failure"]
    # Codex 7-2: field 101's chain ran to the end although 102 failed.
    with db.cursor() as cur:
        cur.execute("SELECT stage, unit_id, state FROM units WHERE run = %s "
                    "AND stage IN ('statistics', 'prune', 'alerts')", (run1,))
        assert sorted(cur.fetchall()) == [("prune", "101", "complete"),
                                          ("statistics", "101", "complete"),
                                          ("statistics", "102", "failed")]

    stopped = cli("loop", "run", "--spec", world["spec"], "--date", "2027-10-01")
    assert stopped.rc == 1 and "(skipped)" in stopped.out

    stages.fail = None  # the cause is fixed
    retried = cli("loop", "run", "--spec", world["spec"], "--date", "2027-10-01",
                  "--retry-failed")
    assert retried.rc == 0, retried.err + retried.out
    ((_, run2, state, promotion, record),) = _rows(db, world["schedule"])
    assert run2 != run1 and state == "complete"
    assert f"date=2027-10-01 run={run2} reopened (--retry-failed, seeded from {run1})" \
        in retried.out
    assert record["run"] == run2 and record["previous_runs"] == [run1]
    assert record["previous_failures"][0]["run"] == run1
    assert "failure" not in record
    with db.cursor() as cur:
        cur.execute("SELECT seed_run, selected_stages, state FROM runs WHERE id = %s", (run2,))
        assert cur.fetchone() == (run1, ["statistics", "prune", "alerts"], "finished")
        # The seeded run holds only what its seed left: the failed statistics
        # unit (seeded), and the prune and alerts units that never ran.
        cur.execute("SELECT stage, unit_id, state, seeded_from_unit IS NOT NULL FROM units "
                    "WHERE run = %s ORDER BY stage, unit_id", (run2,))
        assert cur.fetchall() == [("alerts", UNIT, "complete", False),
                                  ("prune", "102", "complete", False),
                                  ("statistics", "102", "complete", True)]
    # Inherited results are read from the seed: 101's association set.
    alerts_in = _read(fake_s3, f"s3://{FAKE_BUCKET}/scratch/runs/{run2}/inputs/alerts/"
                               f"{UNIT}/manifest.json")
    assert record["association_sets"]["101"] in alerts_in["inputs"]["result_sets"]
    assert record["promotion"]
    # R4 on the seeded run: statistics 102 (seeded; it re-reads its seed
    # attempt's input location) binds the seed's association set for 102;
    # alerts binds both fields' pruned sets, 101's inherited from the seed.
    assert _bound(db, run2, "statistics", "102") == {record["association_sets"]["102"]}
    alerts_bound = _bound(db, run2, "alerts", UNIT)
    assert alerts_bound == set(alerts_in["inputs"]["result_sets"]) | {
        world["template"]["reference-catalog"]}
    with db.cursor() as cur:
        cur.execute("SELECT id, run FROM product_instances WHERE id = ANY(%s)",
                    ([record["pruned_sets"][str(f)] for f in FIELDS],))
        producers = dict(cur.fetchall())
    assert producers == {record["pruned_sets"]["101"]: run1, record["pruned_sets"]["102"]: run2}
    assert set(producers) <= alerts_bound


def test_loop_a_finished_run_with_units_missing_fails_the_date(
        cli, db, fake_batch, fake_s3, world, monkeypatch):
    stages = _FakeStages(db, fake_batch, fake_s3)
    stages.install(monkeypatch)
    stages.stop_after = "load"
    with pytest.raises(KeyboardInterrupt):
        cli("loop", "run", "--spec", world["spec"], "--date", "2027-10-01")
    ((_, run_id, state, _, _),) = _rows(db, world["schedule"])
    assert state == "open"
    repository.finish_run(db.connection, run_id)  # finished elsewhere, part-way

    result = cli("loop", "run", "--spec", world["spec"], "--date", "2027-10-01")
    assert result.rc == 1, result.err + result.out
    ((_, again, state, promotion, record),) = _rows(db, world["schedule"])
    assert (again, state, promotion) == (run_id, "failed", None)
    reason = record["reason"]
    assert reason.startswith(f"run {run_id} is finished but the date's units are not all "
                             "complete: maintain 29990101/SCA01 (absent), crossmatch 101 "
                             "(absent)")
    assert f"alerts {UNIT} (absent)" in reason
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM promotions WHERE request_context->>'run' = %s",
                    (run_id,))
        assert cur.fetchone()[0] == 0

    # Nothing failed, so the seeded path has nothing to re-run: the date
    # stays failed with its refusal.
    retried = cli("loop", "run", "--spec", world["spec"], "--date", "2027-10-01",
                  "--retry-failed")
    assert retried.rc == 1, retried.err + retried.out
    ((_, again, state, _, record),) = _rows(db, world["schedule"])
    assert (again, state) == (run_id, "failed")
    assert record["reason"].startswith("--retry-failed: ")
    assert "nothing to re-run" in record["reason"]


class _LostResponseClientError(Exception):
    """Named to end in ``ClientError`` (``rapidpipe.cli.main._BATCH_ERROR_NAMES``):
    a Batch-shaped failure, which the loop exits 75 on."""


def test_loop_resolves_a_jobless_attempt_and_keeps_its_bindings(
        cli, db, fake_batch, fake_s3, world, monkeypatch):
    # Batch accepts crossmatch 101's job but the response never arrives:
    # the attempt (and its inputs, bound before allocation) is committed,
    # job-less. loop run exits 75; the rerun resolves it through step 6's
    # resolver (the job is found by name and attached) and completes.
    _FakeStages(db, fake_batch, fake_s3).install(monkeypatch)
    original = fake_batch.submit_job
    lost = []

    def submit_job(**kwargs):
        response = original(**kwargs)
        if not lost and "-crossmatch-" in kwargs["jobName"]:
            lost.append(response["jobId"])
            raise _LostResponseClientError("simulated lost submit response")
        return response

    monkeypatch.setattr(fake_batch, "submit_job", submit_job)
    first = cli("loop", "run", "--spec", world["spec"], "--date", "2027-10-01")
    assert first.rc == 75, first.err + first.out
    ((_, run_id, state, _, _),) = _rows(db, world["schedule"])
    assert state == "open"
    with db.cursor() as cur:
        cur.execute("SELECT a.id FROM attempts a JOIN units u ON u.id = a.unit WHERE "
                    "a.run = %s AND u.stage = 'crossmatch' AND a.scheduler_job_id IS NULL",
                    (run_id,))
        ((jobless,),) = cur.fetchall()
        cur.execute("SELECT unit_id FROM units WHERE run = %s AND stage = 'crossmatch'",
                    (run_id,))
        ((field,),) = cur.fetchall()
    bound = _bound(db, run_id, "crossmatch", field)
    assert len(bound) == 1  # the date's source set, bound before the allocation

    resumed = cli("loop", "run", "--spec", world["spec"], "--date", "2027-10-01")
    assert resumed.rc == 0, resumed.err + resumed.out
    assert f"resolved job-less attempts: {jobless}=REPAIRED" in resumed.out
    ((_, again, state, _, record),) = _rows(db, world["schedule"])
    assert (again, state) == (run_id, "complete")
    assert record["jobless_resolved"] == [{"attempt": jobless, "status": "REPAIRED",
                                           "job": lost[0]}]
    assert _bound(db, run_id, "crossmatch", field) == bound
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM attempts a JOIN units u ON u.id = a.unit "
                    "WHERE a.run = %s AND u.stage = 'crossmatch' AND u.unit_id = %s",
                    (run_id, field))
        assert cur.fetchone()[0] == 1


def test_loop_a_refused_input_manifest_fails_the_date(
        cli, db, fake_batch, fake_s3, world, monkeypatch):
    # The delivery's manifest is gone: run start's launcher refuses admit
    # (InputsRefused, exit 65) before writing anything. The loop records the
    # date failed with the message and does not start later dates.
    _FakeStages(db, fake_batch, fake_s3).install(monkeypatch)
    del fake_s3._objects[(FAKE_BUCKET, f"{_prefix(DELIVERY)}/manifest.json")]
    result = cli("loop", "run", "--spec", world["spec"])
    assert result.rc == 1, result.err + result.out
    ((date, run_id, state, promotion, record),) = _rows(db, world["schedule"])
    assert (date, state, promotion) == ("2027-10-01", "failed", None)
    assert f"admit,register,difference,finalize,register,load {UNIT}: inputs refused: " \
        in record["failure"]
    assert f"{DELIVERY}/manifest.json" in record["failure"]
    assert fake_batch.submitted == []
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM units WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 0
