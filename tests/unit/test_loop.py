"""``rapidpipe loop``: one pass's decisions from a listing and one snapshot of the tables.

Contracts: a listed stem with no published intake and no intake job of the
run is submitted once, and not again while its job is pending; a reference
is submitted only once its field has ``min_frames`` official L2 rows; every
official source set gets one packet job naming its reference, Gaia, PSF and
source set only, in any order, whatever else of its field is upstream or
pending (association is in the difference job), in every field in the same
pass; a pass with nothing new, pending or to submit is
drained and the verb exits 0, and a packet set still inside its job's
send window keeps it from draining; the listing pages only the observations of
the selected filters; the packet stage's Gaia input names the catalog by
its whole key, dataset included. With ``before_mjd`` set: the reference
names only rows before it and waits while a selected stem is unpublished;
a difference is submitted only at or after it; a field with too few rows
before it is reported held and the pass still drains; absent, nothing
changes. Against Batch: a container that ended without
its row recording it is marked failed with Batch's reason; a row marked
failed whose Batch job is still running is Batch's retry, held pending; a
local job (no Batch id) is left alone. PSFs: a new stem whose filter and
SCA lack an official science PSF gets one ``psf`` job for the
pair, expecting both keys, and no intake; an official pair admits intakes;
a psf job of the run pending for the pair submits nothing more. A container
that died unrecorded is rerun once and held pending, a rerun's death fails
the pass, a job the code failed is not rerun; a pass submits at most its cap,
in stage order, an exposure's expectation kept on its first intake; an intake
names the dataset's official Gaia catalog, and only the PSF without one.
`--passes` selects the survey's passes in stems and rows alike, all by default.
`job wait` returns once every named job is published or failed, holds a
failed row under Batch's retry, gives up at its timeout and names a missing job;
it exits 0 when all published, 1 when one failed, 2 at the timeout. A job
published with its binding refused is reported under ``failed`` with its
reason and its key not resubmitted, and the loop exits 1 for it as for a
failed job once the rest drains.
A pass reports the quality gate's held sets when given the query.
Spot, with ``[batch.spot]``: a stage's submissions go to the Spot queue
while more than ``backlog`` remain, the rest to the prompt queue, and none
without the table; a Spot job that ended unpublished for any reason is
rerun (a reclaim counted), the rerun never again; a Spot job past
``hedge_factor`` times its stage's median since its start or submission is
terminated and rerun, one inside it, a prompt one, or one with no median is
not; a pass reports its submissions by queue.
OpenUniverse (``[intake] reader = "openuniverse"``): the listing pages only
the selected filters' prefixes in the RAPID spelling and keeps
``_simple.asdf`` stems; a pointing is the observing sequence's row, the
intake's expid, in stems and rows alike; a SOC stem is not admitted, a
selection with passes is refused; a missing PSF submits one psf job.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from rapidpipe.cli import main as cli
from rapidpipe.exitcodes import ExitCode
from rapidpipe.foundation import loop

D = "r00342"
SEL = loop.Selection.parse("F146", "8", "all")
SETTINGS = {"intake": {"source_root": "s3://bucket/l2/", "unsigned": True},
            "reference": {"selection": {"min_frames": 2}},
            "packet": {"forced_window_days": 30.0}, "kafka": {}}


def stem(p: int, v: int, e: int, sca: int = 8) -> str:
    return f"r00342010{p:02d}001004{v:03d}_{e:04d}_wfi{sca:02d}_f146"


def expid(p: int, v: int, e: int) -> int:
    return p * 10**6 + 4 * 10**4 + v * 100 + e


class Pages:
    """A fake S3 bucket of ``keys`` under ``l2/``: ``list_objects_v2`` answers ``StartAfter`` and
    ``MaxKeys`` in key order, and its paginator returns a prefix's keys in two pages; every
    paginated prefix is recorded."""

    def __init__(self, keys: list[str]):
        self.keys, self.listed = sorted("l2/" + k for k in keys), []

    def list_objects_v2(self, Bucket, Prefix, StartAfter, MaxKeys):
        found = [k for k in self.keys if k.startswith(Prefix) and k > StartAfter][:MaxKeys]
        return {"Contents": [{"Key": k} for k in found]} if found else {}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        self.listed.append(Prefix)
        keys = [k for k in self.keys if k.startswith(Prefix)]
        half = len(keys) // 2
        return [{"Contents": [{"Key": k} for k in keys[:half]]},
                {"Contents": [{"Key": k} for k in keys[half:]]}]


def snap(**kw) -> loop.Snapshot:
    base = dict(dataset=D, survey="HLWAS", settings=SETTINGS, fids={"W146": 8, "F184": 1},
                published_stems=set(), jobs=[],
                psfs={(8, 8, "science"): (10, 100)},
                exposures=set(), l2={}, refs={}, diffs={}, sources={}, packets={},
                gaia={"key": {"dataset": D, "name": "gaia-x", "version": "DR3"}, "job": 12})
    base.update(kw)
    return loop.Snapshot(**base)


def l2row(field: int, mjd: float, job: int) -> dict:
    return {"field": field, "mjdobs": mjd, "job": job}


def stages(subs: list[dict], name: str) -> list[dict]:
    return [s for s in subs if s["stage"] == name]


def test_listing_reads_every_page_and_keeps_cal_stems():
    keys = [stem(1, 1, 1) + "_cal.asdf", stem(1, 1, 1) + "_wcs.asdf",
            stem(2, 1, 1) + "_cal.asdf", stem(2, 1, 1) + "_thumb.png"]
    assert loop.list_stems(Pages(keys), "s3://bucket/l2/", SEL) == [stem(1, 1, 1), stem(2, 1, 1)]


def test_listing_requests_only_the_selected_filters_observations():
    """F146 is observation 004 in passes 1 and 2 and 001 in pass 6; F087 holds 001 in pass 1."""
    f087 = "r0034201001001001001_0001_wfi08_f087"
    f146 = [stem(1, 1, 1), stem(2, 2, 1), "r0034201006001001001_0001_wfi08_f146"]
    client = Pages([s + "_cal.asdf" for s in [f087, *f146]] + [f087 + "_wcs.asdf"])
    stems = loop.list_stems(client, "s3://bucket/l2/", loop.Selection.parse("F146", "8", "all"))
    assert client.listed == ["l2/r0034201001001004", "l2/r0034201002001004", "l2/r0034201006001001"]
    assert sorted(stems) == sorted(f146)


def test_new_stem_is_submitted_once_and_not_again_while_pending():
    stems = [stem(1, 1, 2), stem(1, 1, 2, sca=9), stem(1, 1, 1)]
    subs, counts = loop.plan(snap(published_stems={stem(1, 1, 1)}), stems, SEL)
    intake = stages(subs, "intake")
    assert [s["scope"]["file"] for s in intake] == [stem(1, 1, 2)]
    assert intake[0]["expects"][1]["key"] == {"dataset": D, "expid": expid(1, 1, 2), "sca": 8, "fid": 8}
    assert counts["new"] == 1

    pending = [{"id": 50, "stage": "intake", "state": "running",
                "scope": {"dataset": D, "file": stem(1, 1, 2)}}]
    subs, counts = loop.plan(snap(published_stems={stem(1, 1, 1)}, jobs=pending), stems, SEL)
    assert stages(subs, "intake") == [] and counts["pending"]["intake"] == 1


def test_reference_waits_for_min_frames():
    one = {(expid(1, 1, 1), 8, 8): l2row(7, 1.0, 20)}
    subs, _ = loop.plan(snap(l2=one), [], SEL)
    assert stages(subs, "reference") == []

    two = {**one, (expid(2, 1, 1), 8, 8): l2row(7, 2.0, 21)}
    subs, _ = loop.plan(snap(l2=two), [], SEL)
    ref = stages(subs, "reference")
    assert len(ref) == 1 and ref[0]["scope"] == {"field": 7, "fid": 8, "sca": 8}
    assert [i["source_job"] for i in ref[0]["inputs"]] == [20, 21]


def test_every_official_source_set_gets_one_packet_in_any_order():
    k1, k2, k3 = ((expid(p, 1, 1), 8, 8) for p in (1, 2, 3))
    l2 = {k1: l2row(7, 1.0, 20), k2: l2row(7, 2.0, 21), k3: l2row(7, 3.0, 22)}
    refs, diffs = {(7, 8): 30}, {k1: 41, k2: 42, k3: 43}

    # The earliest epoch's source set is still upstream: the later two go anyway, together.
    subs, _ = loop.plan(snap(l2=l2, refs=refs, diffs=diffs, sources={k2: 42, k3: 43}), [], SEL)
    packets = stages(subs, "packet")
    assert [p["scope"]["expid"] for p in packets] == [k2[0], k3[0]]
    assert packets[0]["inputs"] == [
        {"product": "refimages", "key": {"dataset": D, "field": 7, "fid": 8}, "source_job": 30},
        {"product": "catalogs", "key": {"dataset": D, "name": "gaia-x", "version": "DR3"},
         "source_job": 12},
        {"product": "psfs", "key": {"dataset": D, "fid": 8, "sca": 8, "kind": "science"},
         "source_job": 10, "binding": 100},
        {"product": "sources", "key": {"dataset": D, "expid": k2[0], "sca": 8, "fid": 8},
         "source_job": 42}]
    assert packets[0]["expects"] == [{"product": "packets", "key": {"dataset": D, "expid": k2[0],
                                                                    "sca": 8, "fid": 8}, "binding": None}]

    # One pending for k2 and k3's official: only k1's goes.
    pend = [{"id": 60, "stage": "packet", "state": "launched", "scope": {"expid": k2[0], "sca": 8, "fid": 8}}]
    subs, _ = loop.plan(snap(l2=l2, refs=refs, diffs=diffs, sources=diffs, packets={k3: 90}, jobs=pend),
                        [], SEL)
    assert [p["scope"]["expid"] for p in stages(subs, "packet")] == [k1[0]]


def test_an_intake_pending_holds_no_packet():
    k1 = (expid(1, 1, 1), 8, 8)
    pend = [{"id": 70, "stage": "intake", "state": "running", "scope": {"dataset": D, "file": stem(2, 1, 1)}}]
    subs, _ = loop.plan(snap(l2={k1: l2row(7, 1.0, 20)}, refs={(7, 8): 30}, diffs={k1: 41},
                             sources={k1: 41}, jobs=pend), [], SEL)
    assert [p["scope"]["expid"] for p in stages(subs, "packet")] == [k1[0]]


def test_two_fields_advance_in_the_same_pass():
    a1, a2 = (expid(1, 1, 1), 8, 8), (expid(1, 1, 2), 8, 8)
    l2 = {a1: l2row(7, 1.0, 20), a2: l2row(9, 1.1, 21)}
    subs, _ = loop.plan(snap(l2=l2, refs={(7, 8): 30, (9, 8): 31}, diffs={a1: 41, a2: 42},
                             sources={a1: 41, a2: 42}), [], SEL)
    assert sorted(p["inputs"][0]["key"]["field"] for p in stages(subs, "packet")) == [7, 9]


def test_failed_job_is_reported_and_its_key_not_resubmitted():
    k1 = (expid(1, 1, 1), 8, 8)
    failed = [{"id": 80, "stage": "difference", "state": "failed", "scope": {"expid": k1[0], "sca": 8, "fid": 8}}]
    subs, counts = loop.plan(snap(l2={k1: l2row(7, 1.0, 20)}, refs={(7, 8): 30}, jobs=failed), [], SEL)
    assert stages(subs, "difference") == []
    assert counts["failed"] == [{"job": 80, "stage": "difference", "scope": failed[0]["scope"],
                                 "refusal": None}]


def test_a_job_published_with_its_binding_refused_is_reported_failed_and_its_key_not_resubmitted():
    k1 = (expid(1, 1, 1), 8, 8)
    refused = [{"id": 81, "stage": "difference", "state": "published", "refusal": "no sources rows",
                "scope": {"expid": k1[0], "sca": 8, "fid": 8}}]
    subs, counts = loop.plan(snap(l2={k1: l2row(7, 1.0, 20)}, refs={(7, 8): 30}, jobs=refused), [], SEL)
    assert stages(subs, "difference") == []
    assert counts["failed"] == [{"job": 81, "stage": "difference", "scope": refused[0]["scope"],
                                 "refusal": "no sources rows"}]


def test_drained_pass_exits_zero(monkeypatch, capsys):
    k1 = (expid(1, 1, 1), 8, 8)
    done = snap(published_stems={stem(1, 1, 1)}, l2={k1: l2row(7, 1.0, 20)}, refs={(7, 8): 30},
                diffs={k1: 41}, sources={k1: 41}, packets={k1: 90})
    monkeypatch.setattr(loop, "snapshot", lambda conn, run: done)
    monkeypatch.setattr(cli, "connect", lambda **kw: contextlib.nullcontext(None))
    monkeypatch.setattr(cli.foundation, "run_show", lambda conn, run: {"settings": SETTINGS})
    monkeypatch.setattr(cli.storage, "s3_client", lambda unsigned=False: Pages([stem(1, 1, 1) + "_cal.asdf"]))
    monkeypatch.setattr(cli.publish_kafka, "unpublished_sets", lambda conn, run: [])
    monkeypatch.setattr(cli.publish_kafka, "settling_sets", lambda conn, run: [])
    monkeypatch.setattr(cli.publish_kafka, "late_sets", lambda conn, run: 1)
    monkeypatch.setattr(cli.launch, "batch_client", lambda: Batch({}))
    monkeypatch.setattr(loop, "reconcile", lambda conn, run, client, *a: {"retrying": []})
    monkeypatch.setattr(loop.attempts, "run_entries", lambda conn, run: [])
    code = cli.main(["loop", "--run", "12", "--filters", "F146", "--scas", "8", "--pointings", "001_0001",
                     "--job-definition", "jd", "--interval", "0"])
    assert code == ExitCode.SUCCESS
    out = capsys.readouterr().out
    assert '"drained": true' in out and '"sets_late": 1' in out     # the run's late sets in the pass line
    summary = loop.run_pass(lambda: contextlib.nullcontext(None), 12, SEL, [stem(1, 1, 1)], lambda item: None,
                            lambda: {"packets": 0, "sets": [], "waiting": [90]})
    assert summary["sets_waiting"] == [90] and not summary["drained"]


@pytest.mark.parametrize("state, refusal", [("failed", None), ("published", "binding refused")])
def test_a_failed_chain_holds_only_its_own_work_and_the_loop_exits_1_once_the_rest_drains(
        monkeypatch, capsys, state, refusal):
    k1, k2 = (expid(1, 1, 1), 8, 8), (expid(2, 1, 1), 8, 8)
    failed = {"id": 80, "stage": "difference", "state": state, "refusal": refusal,
              "scope": {"expid": k1[0], "sca": 8, "fid": 8}}
    rows = {"published_stems": {stem(1, 1, 1), stem(2, 1, 1)}, "l2": {k1: l2row(7, 1.0, 20), k2: l2row(7, 1.1, 21)},
            "refs": {(7, 8): 30}, "diffs": {k2: 42}, "sources": {k2: 42}}
    passes = iter([snap(jobs=[failed], **rows),                                     # k2's packet is due
                   snap(jobs=[failed, {"id": 91, "stage": "packet", "state": "published",
                                       "scope": {"expid": k2[0], "sca": 8, "fid": 8}}],
                        packets={k2: 91}, **rows)])
    submitted = []
    monkeypatch.setattr(loop, "snapshot", lambda conn, run: next(passes))
    monkeypatch.setattr(cli, "connect", lambda **kw: contextlib.nullcontext(None))
    monkeypatch.setattr(cli.foundation, "run_show", lambda conn, run: {"settings": SETTINGS})
    monkeypatch.setattr(cli.foundation, "create_job", lambda conn, run, stage, *a: submitted.append(stage) or 91)
    monkeypatch.setattr(cli.launch, "submit", lambda *a, **kw: "batch-91")
    monkeypatch.setattr(cli.launch, "job_environment", lambda settings: {})
    monkeypatch.setattr(cli.storage, "s3_client", lambda unsigned=False: Pages([]))
    monkeypatch.setattr(cli.publish_kafka, "unpublished_sets", lambda conn, run: [])
    monkeypatch.setattr(cli.publish_kafka, "settling_sets", lambda conn, run: [])
    monkeypatch.setattr(cli.publish_kafka, "late_sets", lambda conn, run: 0)
    monkeypatch.setattr(cli.launch, "batch_client", lambda: Batch({}))
    monkeypatch.setattr(loop, "reconcile", lambda conn, run, client, *a: {"retrying": []})
    monkeypatch.setattr(loop.attempts, "run_entries", lambda conn, run: [])
    code = cli.main(["loop", "--run", "12", "--filters", "F146", "--scas", "8", "--pointings", "001_0001",
                     "--job-definition", "jd", "--interval", "0"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert submitted == ["packet"] and len(lines) == 2           # the other chain went on past the failure
    assert lines[-1]["drained"] and lines[-1]["failed"][0]["job"] == 80
    assert code == ExitCode.FAILURE


class Batch:
    """A fake Batch client: ``describe_jobs`` answers from ``{batch id: (status, reason[, job name[,
    extra fields]])}``, the queue ``prompt-q`` unless the extra fields name one; ``terminate_job`` is
    recorded."""

    def __init__(self, jobs: dict):
        self.jobs, self.calls, self.terminated = jobs, [], []

    def describe_jobs(self, jobs):
        self.calls.append(list(jobs))
        return {"jobs": [{"jobId": j, "status": self.jobs[j][0], "statusReason": self.jobs[j][1],
                          "jobName": self.jobs[j][2] if len(self.jobs[j]) > 2 else f"rapid-x-{j}",
                          "jobQueue": "arn:aws:batch:r:1:job-queue/prompt-q",
                          **(self.jobs[j][3] if len(self.jobs[j]) > 3 else {})}
                         for j in jobs if j in self.jobs]}

    def terminate_job(self, jobId, reason):
        self.terminated.append((jobId, reason))


class Rows:
    """A fake connection: the jobs ``SELECT`` returns ``rows``; each ``UPDATE`` is recorded
    and applies (``rowcount`` 1) while the row is still ``launched`` or ``running``."""

    def __init__(self, rows):
        self.rows, self.updates, self.rowcount = rows, [], 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.sql = sql
        if sql.startswith("UPDATE"):
            self.updates.append(params)
            self.rowcount = 1

    def fetchall(self):
        return [] if "attempts FROM jobs" in self.sql else self.rows

    def commit(self):
        pass


def test_a_container_that_ended_unrecorded_is_marked_failed_with_batchs_reason():
    conn = Rows([(70, "intake", "launched", "b-70", None), (71, "intake", "running", "b-71", None)])
    batch = Batch({"b-70": ("FAILED", "Terminated by the operator"), "b-71": ("RUNNING", "")})
    checked = loop.reconcile(conn, 12, batch)
    assert checked["ended"] == [{"job": 70, "stage": "intake",
                                 "reason": "Batch FAILED: Terminated by the operator"}]
    failed = [u for u in conn.updates if len(u) == 3]
    assert [u[-1] for u in failed] == [70] and failed[0][1].startswith("Batch FAILED")
    assert checked["retrying"] == []


def test_a_failed_row_under_batchs_retry_is_held_pending_not_failed(monkeypatch):
    k1 = (expid(1, 1, 1), 8, 8)
    job = {"id": 80, "stage": "difference", "state": "failed",
           "scope": {"expid": k1[0], "sca": 8, "fid": 8}}
    monkeypatch.setattr(loop, "snapshot",
                        lambda conn, run: snap(l2={k1: l2row(7, 1.0, 20)}, refs={(7, 8): 30}, jobs=[job]))
    conn = Rows([(80, "difference", "failed", "b-80", None)])
    summary = loop.run_pass(lambda: contextlib.nullcontext(conn), 12, SEL, [], lambda item: None, None,
                            batch=Batch({"b-80": ("RUNNABLE", "")}))
    assert summary["failed"] == [] and summary["pending"]["difference"] == 1
    assert summary["submitted"]["difference"] == 0 and conn.updates == []


def test_a_local_job_without_a_batch_id_is_left_alone():
    conn, batch = Rows([(90, "import", "running", None, None)]), Batch({})
    checked = loop.reconcile(conn, 12, batch)
    assert batch.calls == [] and conn.updates == [] and checked["described"] == 0


BOUND = {**SETTINGS, "reference": {"selection": {"min_frames": 2, "before_mjd": 2.5}}}


def test_the_bound_reference_names_only_rows_before_it_and_waits_for_the_selections_intake():
    k1, k2, k3 = ((expid(p, 1, 1), 8, 8) for p in (1, 2, 3))
    l2 = {k1: l2row(7, 1.0, 20), k2: l2row(7, 2.0, 21), k3: l2row(7, 3.0, 22)}
    listed = [stem(p, 1, 1) for p in (1, 2, 3)]
    subs, _ = loop.plan(snap(settings=BOUND, l2=l2, published_stems=set(listed)), listed, SEL)
    (ref,) = stages(subs, "reference")
    assert [i["source_job"] for i in ref["inputs"]] == [20, 21]

    # A selected stem not yet published (listed, beyond max_intake): no intake pending, but no reference.
    subs, counts = loop.plan(snap(settings=BOUND, l2={k1: l2[k1], k3: l2[k3]},
                                  published_stems={listed[0], listed[2]}), listed, SEL, max_intake=0)
    assert stages(subs, "reference") == [] and counts["pending"]["intake"] == 0 and counts["held"] == []


def test_the_bound_differences_only_rows_at_or_after_it():
    k1, k2, k3 = ((expid(p, 1, 1), 8, 8) for p in (1, 2, 3))
    l2 = {k1: l2row(7, 1.0, 20), k2: l2row(7, 2.5, 21), k3: l2row(7, 3.0, 22)}
    subs, _ = loop.plan(snap(settings=BOUND, l2=l2, refs={(7, 8): 30}), [], SEL)
    assert sorted(d["scope"]["expid"] for d in stages(subs, "difference")) == [k2[0], k3[0]]
    # A difference names the science PSF only: it measures the reference's itself.
    assert {(i["key"]["kind"], i["source_job"]) for d in stages(subs, "difference")
            for i in d["inputs"] if i["product"] == "psfs"} == {("science", 10)}

    # Packets start at the bound, not at the reference's rows.
    sources = {k2: 42, k3: 43}
    subs, _ = loop.plan(snap(settings=BOUND, l2=l2, refs={(7, 8): 30}, diffs=sources, sources=sources),
                        [], SEL)
    assert [p["inputs"][-1]["source_job"] for p in stages(subs, "packet")] == [42, 43]


def test_a_field_with_too_few_rows_before_the_bound_is_held_and_the_pass_drains():
    k3, k4 = (expid(3, 1, 1), 8, 8), (expid(4, 1, 2), 8, 8)
    l2 = {k3: l2row(7, 3.0, 22), k4: l2row(9, 1.0, 23)}         # field 7: none before; 9: one
    done = [stem(3, 1, 1), stem(4, 1, 2)]
    subs, counts = loop.plan(snap(settings=BOUND, l2=l2, published_stems=set(done)), done, SEL)
    assert subs == []
    assert [(h["field"], h["reason"].split()[0]) for h in counts["held"]] == [(7, "0"), (9, "1")]
    assert counts["new"] == 0 and not any(counts["pending"].values())


def test_without_the_bound_the_reference_takes_every_row_and_nothing_is_held():
    k1, k3 = (expid(1, 1, 1), 8, 8), (expid(3, 1, 1), 8, 8)
    l2 = {k1: l2row(7, 1.0, 20), k3: l2row(7, 3.0, 22)}
    subs, counts = loop.plan(snap(l2=l2), [stem(5, 1, 1)], SEL, max_intake=0)
    (ref,) = stages(subs, "reference")
    assert [i["source_job"] for i in ref["inputs"]] == [20, 22] and counts["held"] == []


def test_a_missing_psf_submits_one_psf_job_and_no_intake_of_that_sca():
    sel = loop.Selection.parse("F146", "8,9", "all")
    stems = [stem(1, 1, 1), stem(2, 1, 1), stem(1, 1, 1, sca=9), stem(2, 1, 1, sca=9)]
    psfs = {(8, 9, "science"): (12, 102)}
    subs, _ = loop.plan(snap(psfs=psfs), stems, sel)            # SCA 8 lacks its science PSF
    (psf,) = stages(subs, "psf")
    assert psf["scope"] == {"dataset": D, "file": stem(1, 1, 1)}
    assert [(e["product"], e["key"], e["binding"]) for e in psf["expects"]] == [
        ("psfs", {"dataset": D, "fid": 8, "sca": 8, "kind": "science"}, None)]
    assert sorted(s["scope"]["file"] for s in stages(subs, "intake")) == [stem(1, 1, 1, sca=9),
                                                                          stem(2, 1, 1, sca=9)]
    # Neither SCA has a PSF: a job each, and no stem of either is admitted.
    subs, _ = loop.plan(snap(psfs={}), stems, sel)
    assert sorted(s["scope"]["file"] for s in stages(subs, "psf")) == [stem(1, 1, 1), stem(1, 1, 1, sca=9)]
    assert stages(subs, "intake") == []


def test_an_official_science_psf_admits_intakes_and_submits_no_psf_job():
    subs, _ = loop.plan(snap(), [stem(1, 1, 1), stem(2, 1, 1)], SEL)
    assert stages(subs, "psf") == []
    assert [s["scope"]["file"] for s in stages(subs, "intake")] == [stem(1, 1, 1), stem(2, 1, 1)]


def test_a_pending_psf_job_submits_nothing_more():
    pending = [{"id": 60, "stage": "psf", "state": "running", "scope": {"dataset": D, "file": stem(1, 1, 1)}}]
    subs, counts = loop.plan(snap(psfs={}, jobs=pending), [stem(1, 1, 1), stem(2, 1, 1)], SEL)
    assert subs == [] and counts["pending"]["psf"] == 1 and counts["new"] == 2


def test_an_intake_names_the_datasets_gaia_catalog_once_it_is_official():
    named, _ = loop.plan(snap(), [stem(1, 1, 1)], SEL)
    assert [i for i in stages(named, "intake")[0]["inputs"] if i["product"] == "catalogs"] == [
        {"product": "catalogs", "key": {"dataset": D, "name": "gaia-x", "version": "DR3"}, "source_job": 12}]
    alone, _ = loop.plan(snap(gaia=None), [stem(1, 1, 1)], SEL)
    assert [i["product"] for i in stages(alone, "intake")[0]["inputs"]] == ["psfs"]


def test_a_container_that_died_unrecorded_is_rerun_once_and_held_pending(monkeypatch):
    k1 = (expid(1, 1, 1), 8, 8)
    job = {"id": 81, "stage": "difference", "state": "failed", "scope": {"expid": k1[0], "sca": 8, "fid": 8}}
    monkeypatch.setattr(loop, "snapshot",
                        lambda conn, run: snap(l2={k1: l2row(7, 1.0, 20)}, refs={(7, 8): 30}, jobs=[dict(job)]))
    conn, reruns = Rows([(81, "difference", "running", "b-81", None)]), []
    summary = loop.run_pass(lambda: contextlib.nullcontext(conn), 12, SEL, [], lambda item: None, None,
                            batch=Batch({"b-81": ("FAILED", "Host EC2 (instance i-1) terminated.")}),
                            rerun=lambda j, s: reruns.append((j, s)))
    assert reruns == [(81, "difference")] and summary["rerun"] == [81]
    assert summary["failed"] == [] and summary["pending"]["difference"] == 1
    # The rerun's container dies too: no second rerun, and the job fails the pass.
    conn, reruns = Rows([(81, "difference", "failed", "b-82", "Batch FAILED: Task failed to start")]), []
    summary = loop.run_pass(lambda: contextlib.nullcontext(conn), 12, SEL, [], lambda item: None, None,
                            batch=Batch({"b-82": ("FAILED", "Task failed to start", "rapid-difference-81-rerun")}),
                            rerun=lambda j, s: reruns.append((j, s)))
    assert reruns == [] and [(f["job"], f["stage"]) for f in summary["failed"]] == [(81, "difference")]


def test_a_job_the_code_failed_is_not_rerun():
    conn = Rows([(83, "difference", "failed", "b-83", "reference PSF: 4 stars, fewer than 10")])
    checked = loop.reconcile(conn, 12, Batch({"b-83": ("FAILED", "Essential container in task exited")}))
    assert checked["rerun"] == [] and checked["ended"] == []


def test_a_pass_submits_at_most_the_cap_in_stage_order_and_defers_the_rest():
    stems = [stem(1, 1, 1), stem(1, 1, 1, sca=9), stem(2, 1, 1)]
    sel = loop.Selection.parse("F146", "8,9", "all")
    psfs = {(8, 8, "science"): (10, 100), (8, 9, "science"): (11, 101)}
    subs, counts = loop.plan(snap(psfs=psfs), stems, sel, max_submissions=2)
    assert [s["scope"]["file"] for s in subs] == stems[:2] and counts["deferred"] == 1
    assert [e["product"] for e in subs[0]["expects"]] == ["exposures", "l2files"]
    whole, counts = loop.plan(snap(psfs=psfs), stems, sel)
    assert len(whole) == 3 and counts["deferred"] == 0


def test_passes_select_the_surveys_passes_in_stems_and_rows_and_default_to_all():
    sel = loop.Selection.parse("F146", "8", "all", "1,3")
    assert [s for s in (stem(1, 1, 1), stem(2, 1, 1), stem(3, 1, 1)) if sel.admits_stem(s)] == [
        stem(1, 1, 1), stem(3, 1, 1)]
    assert [p for p in (1, 2, 3) if sel.admits_row(expid(p, 1, 1), 8, 8, {8})] == [1, 3]
    subs, _ = loop.plan(snap(), [stem(1, 1, 1), stem(2, 1, 1), stem(3, 1, 1)], sel)
    assert [s["scope"]["file"] for s in stages(subs, "intake")] == [stem(1, 1, 1), stem(3, 1, 1)]
    assert sel.summary()["passes"] == [1, 3] and SEL.passes is None and SEL.summary()["passes"] == "all"


class Polled(Rows):
    """A fake connection whose jobs ``SELECT`` returns the next of ``snapshots`` each check."""

    def __init__(self, snapshots):
        super().__init__(snapshots[0])
        self.snapshots, self.reads = snapshots, 0

    def fetchall(self):
        self.reads += 1
        return self.snapshots[min(self.reads - 1, len(self.snapshots) - 1)]


def test_job_wait_returns_once_every_job_is_published_or_failed():
    conn = Polled([[(1, 12, "running"), (2, 12, "launched")], [(1, 12, "published"), (2, 12, "failed")]])
    slept = []
    result = loop.wait(lambda: contextlib.nullcontext(conn), [1, 2], None, timeout=600, interval=5,
                       sleep=slept.append, clock=lambda: 0.0)
    assert result["ended"] and result["states"] == {1: "published", 2: "failed"} and slept == [5]


def test_job_wait_holds_a_failed_row_under_batchs_retry_and_gives_up_at_the_timeout(monkeypatch):
    monkeypatch.setattr(loop, "reconcile", lambda conn, run, client, *a: {"retrying": [3]})
    conn = Polled([[(3, 12, "failed")]])
    ticks = iter([0.0, 0.0, 30.0, 61.0])
    result = loop.wait(lambda: contextlib.nullcontext(conn), [3], Batch({}), timeout=60, interval=30,
                       sleep=lambda s: None, clock=lambda: next(ticks))
    assert not result["ended"] and result["states"] == {3: "failed"}


@pytest.mark.parametrize("states, ended, code", [
    ({1: "published", 2: "published"}, True, ExitCode.SUCCESS),
    ({1: "published", 2: "failed"}, True, ExitCode.FAILURE),
    ({1: "published", 2: "running"}, False, ExitCode.INCOMPLETE)])
def test_job_wait_exits_0_when_all_published_1_when_one_failed_and_2_at_the_timeout(
        monkeypatch, capsys, states, ended, code):
    monkeypatch.setattr(cli, "connect", lambda **kw: contextlib.nullcontext(None))
    monkeypatch.setattr(cli.launch, "batch_client", lambda: None)
    monkeypatch.setattr(loop, "wait", lambda *a, **kw: {"states": states, "ended": ended, "seconds": 1.0})
    assert cli.main(["job", "wait", "--job", "1", "2", "--timeout", "1"]) == code


def test_job_wait_names_a_job_that_does_not_exist():
    conn = Polled([[(1, 12, "published")]])
    try:
        loop.wait(lambda: contextlib.nullcontext(conn), [1, 9], None, timeout=60)
    except LookupError as exc:
        assert "9" in str(exc)
    else:
        raise AssertionError("no LookupError")


def test_a_pass_reports_the_gates_held_sets_when_given(monkeypatch):
    held = [{"job": 5, "metric": "core_fraction", "value": 0.04}]
    monkeypatch.setattr(loop, "snapshot", lambda conn, run: snap())
    summary = loop.run_pass(lambda: contextlib.nullcontext(None), 12, SEL, [], lambda item: None, None,
                            held_sets=lambda: held)
    assert summary["held_sets"] == held
    assert "held_sets" not in loop.run_pass(lambda: contextlib.nullcontext(None), 12, SEL, [],
                                            lambda item: None, None)


SPOT = {"queue": "spot-q", "backlog": 2, "hedge_factor": 3.0}
ON_SPOT = {"jobQueue": "arn:aws:batch:r:1:job-queue/spot-q"}


def test_a_stages_backlog_goes_to_spot_and_its_last_wave_to_the_prompt_queue():
    stems = [stem(p, 1, 1) for p in range(1, 6)]
    subs, _ = loop.plan(snap(settings={**SETTINGS, "batch": {"spot": SPOT}}), stems, SEL)
    assert [s.get("queue") for s in stages(subs, "intake")] == ["spot-q", "spot-q", "spot-q", None, None]
    subs, _ = loop.plan(snap(), stems, SEL)
    assert not any("queue" in s for s in subs)


def test_a_spot_job_that_ended_unpublished_for_any_reason_is_rerun_and_a_reclaim_counted():
    conn = Rows([(84, "difference", "failed", "b-84", "reference PSF: 4 stars, fewer than 10"),
                 (85, "intake", "running", "b-85", None)])
    checked = loop.reconcile(conn, 12, Batch({
        "b-84": ("FAILED", "Essential container in task exited", "rapid-difference-84", ON_SPOT),
        "b-85": ("FAILED", "Host EC2 (instance i-1) terminated.", "rapid-intake-85", ON_SPOT)}), SPOT)
    assert [r["job"] for r in checked["rerun"]] == [84, 85] and checked["reclaims"] == 1
    # The rerun, on the prompt queue, is not rerun again whatever it dies of.
    conn = Rows([(84, "difference", "failed", "b-86", "reference PSF: 4 stars, fewer than 10")])
    checked = loop.reconcile(conn, 12, Batch({"b-86": ("FAILED", "x", "rapid-difference-84-rerun")}), SPOT)
    assert checked["rerun"] == []


def test_a_spot_straggler_past_the_factor_is_terminated_and_rerun_and_one_inside_it_is_not():
    now = 10_000.0
    conn = Rows([(87, "intake", "running", "b-87", None), (88, "intake", "launched", "b-88", None),
                 (89, "intake", "running", "b-89", None)])
    batch = Batch({"b-87": ("RUNNING", "", "rapid-intake-87", {**ON_SPOT, "startedAt": (now - 400) * 1000}),
                   "b-88": ("RUNNABLE", "", "rapid-intake-88", {**ON_SPOT, "createdAt": (now - 100) * 1000}),
                   "b-89": ("RUNNING", "", "rapid-intake-89", {"startedAt": (now - 400) * 1000})})
    checked = loop.reconcile(conn, 12, batch, SPOT, {"intake": 100.0}, clock=lambda: now)
    assert [t[0] for t in batch.terminated] == ["b-87"] and "hedge" in batch.terminated[0][1]
    assert [h["job"] for h in checked["hedged"]] == [87] and [r["job"] for r in checked["rerun"]] == [87]
    assert loop.reconcile(Rows([(87, "intake", "running", "b-87", None)]), 12, Batch({}), SPOT, None)["hedged"] == []


def test_a_pass_reports_its_submissions_by_queue(monkeypatch):
    one = {"queue": "spot-q", "backlog": 1}
    monkeypatch.setattr(loop, "snapshot", lambda conn, run: snap(settings={**SETTINGS, "batch": {"spot": one}}))
    ids = iter(range(500, 600))
    summary = loop.run_pass(lambda: contextlib.nullcontext(None), 12, SEL, [stem(p, 1, 1) for p in (1, 2, 3)],
                            lambda item: next(ids), None)
    assert summary["queues"] == {"spot-q": [500, 501], "prompt": [502]}


OU = "openuniverse"


def ou(pointing: int, sca: int = 10, band: str = "H158") -> str:
    return f"roman_tds_{band}_{pointing}_wfi{sca:02d}"


def test_an_openuniverse_listing_pages_the_selected_filters_and_keeps_simple_stems():
    keys = [f"H158/1012/{ou(1012)}_simple.asdf", f"H158/1012/{ou(1012, 4)}_simple.asdf",
            f"H158/1012/{ou(1012)}_thumb.png", f"R062/1000/{ou(1000, band='R062')}_simple.asdf"]
    client = Pages(keys)
    sel = loop.Selection.parse("F158", "10", "all", reader=OU)
    assert sorted(loop.list_stems(client, "s3://bucket/l2/", sel)) == [ou(1012, 4), ou(1012)]
    assert client.listed == ["l2/H158/"]


def test_an_openuniverse_pointing_is_the_sequence_row_in_stems_and_rows():
    sel = loop.Selection.parse("F158", "10", "1012,2034", reader=OU)
    stems = [ou(1012), ou(2034), ou(3000), ou(1012, 4), stem(1, 1, 1, sca=10)]
    subs, counts = loop.plan(snap(fids={"H158": 2}, psfs={(2, 10, "science"): (10, 100)}), stems, sel)
    intake = stages(subs, "intake")
    assert [s["scope"]["file"] for s in intake] == [ou(1012), ou(2034)] and counts["admitted"] == 2
    assert intake[0]["expects"][1]["key"] == {"dataset": D, "expid": 1012, "sca": 10, "fid": 2}
    assert sel.admits_row(2034, 10, 2, {2}) and not sel.admits_row(3000, 10, 2, {2})
    assert sel.summary()["pointings"] == [1012, 2034]


def test_an_openuniverse_selection_with_passes_is_refused():
    with pytest.raises(ValueError, match="no passes"):
        loop.Selection.parse("F158", "10", "all", "1,2", reader=OU)


def test_an_openuniverse_stem_without_its_psf_gets_one_psf_job():
    sel = loop.Selection.parse("F158", "10", "all", reader=OU)
    subs, _ = loop.plan(snap(fids={"H158": 2}, psfs={}), [ou(1012), ou(2034)], sel)
    (psf,) = stages(subs, "psf")
    assert psf["scope"] == {"dataset": D, "file": ou(1012)} and stages(subs, "intake") == []
    assert psf["expects"][0]["key"] == {"dataset": D, "fid": 2, "sca": 10, "kind": "science"}
    pending = [{"id": 60, "stage": "psf", "state": "running", "scope": {"dataset": D, "file": ou(1012)}}]
    subs, _ = loop.plan(snap(fids={"H158": 2}, psfs={}, jobs=pending), [ou(1012), ou(2034)], sel)
    assert subs == []
