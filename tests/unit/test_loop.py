"""``rapidpipe loop``: one pass's decisions from a listing and one snapshot of the tables.

Contracts: a listed stem with no published intake and no intake job of the
run is submitted once, and not again while its job is pending; a reference
is submitted only once its field has ``min_frames`` official L2 rows; each
field's associate chain submits one epoch at a time in ``mjdobs`` order and
waits while an earlier exposure of the field is upstream; two fields
advance in the same pass; a pass with nothing new, pending or to submit is
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
a psf job of the run pending for the pair submits nothing more; an intake
names the dataset's official Gaia catalog, and only the PSF without one.
"""

from __future__ import annotations

import contextlib

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
                exposures=set(), l2={}, refs={}, diffs={}, sources={}, objects={}, chain={}, packets={},
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


def _associated(field_jobs: dict) -> list[dict]:
    return [{"id": j, "stage": "associate", "state": "published", "scope": {"field": f}, "sources": s}
            for j, (f, s) in field_jobs.items()]


def test_associate_chain_is_chronological_one_at_a_time_and_waits_for_upstream():
    k1, k2, k3 = ((expid(p, 1, 1), 8, 8) for p in (1, 2, 3))
    l2 = {k1: l2row(7, 1.0, 20), k2: l2row(7, 2.0, 21), k3: l2row(7, 3.0, 22)}
    refs, diffs = {(7, 8): 30}, {k1: 41, k2: 42, k3: 43}

    # The earliest epoch's source set is still upstream: nothing associates.
    subs, _ = loop.plan(snap(l2=l2, refs=refs, diffs=diffs, sources={k2: 42, k3: 43}), [], SEL)
    assert stages(subs, "associate") == []

    # All three official: only the earliest goes, with no predecessor.
    sources = {k1: 41, k2: 42, k3: 43}
    subs, _ = loop.plan(snap(l2=l2, refs=refs, diffs=diffs, sources=sources), [], SEL)
    (a,) = stages(subs, "associate")
    assert a["inputs"] == [{"product": "sources", "key": {"dataset": D, "expid": k1[0], "sca": 8, "fid": 8},
                            "source_job": 41}]
    assert a["expects"][0]["binding"] is None

    # While it is pending, the field waits.
    pend = [{"id": 60, "stage": "associate", "state": "launched", "scope": {"field": 7}, "sources": 41}]
    subs, _ = loop.plan(snap(l2=l2, refs=refs, diffs=diffs, sources=sources, jobs=pend), [], SEL)
    assert stages(subs, "associate") == []

    # Once published, the next epoch goes, naming the field's open object set.
    done = _associated({60: (7, 41)})
    subs, _ = loop.plan(snap(l2=l2, refs=refs, diffs=diffs, sources=sources, jobs=done,
                             objects={7: (60, 900)}, chain={41: 60}), [], SEL)
    (a,) = stages(subs, "associate")
    assert a["inputs"][0]["source_job"] == 42
    assert a["inputs"][1] == {"product": "objects", "key": {"dataset": D, "survey": "HLWAS", "field": 7},
                              "source_job": 60}
    assert a["expects"][0]["binding"] == 900
    # Its packet goes beside it, naming the first object set and no predecessor packet set.
    (pk,) = stages(subs, "packet")
    assert pk["scope"]["expid"] == k1[0]
    assert {"product": "objects", "key": {"dataset": D, "survey": "HLWAS", "field": 7},
            "source_job": 60} in pk["inputs"]
    assert not [i for i in pk["inputs"] if i["product"] == "packets"]
    assert {"product": "catalogs", "key": {"dataset": D, "name": "gaia-x", "version": "DR3"},
            "source_job": 12} in pk["inputs"]


def test_a_run_resumed_in_place_of_another_continues_the_official_chain_without_reassociating():
    k1, k2 = (expid(1, 1, 1), 8, 8), (expid(2, 1, 1), 8, 8)
    l2 = {k1: l2row(7, 1.0, 20), k2: l2row(7, 2.0, 21)}
    # Another run of the dataset associated epoch 1 (source set 41) as object set 60; this run has no jobs.
    subs, _ = loop.plan(snap(l2=l2, refs={(7, 8): 30}, diffs={k1: 41, k2: 42}, sources={k1: 41, k2: 42},
                             objects={7: (60, 900)}, chain={41: 60}), [], SEL)
    (a,) = stages(subs, "associate")
    assert a["inputs"][0]["source_job"] == 42 and a["expects"][0]["binding"] == 900
    (pk,) = stages(subs, "packet")
    assert pk["scope"]["expid"] == k1[0]
    assert {"product": "objects", "key": {"dataset": D, "survey": "HLWAS", "field": 7},
            "source_job": 60} in pk["inputs"]


def test_the_chain_is_walked_back_from_the_official_object_set_and_skips_associates_outside_it():
    links = [{"job": 60, "product": "sources", "source_job": 41},
             {"job": 61, "product": "sources", "source_job": 42}, {"job": 61, "product": "objects", "source_job": 60},
             {"job": 62, "product": "sources", "source_job": 43}, {"job": 62, "product": "objects", "source_job": 61},
             {"job": 70, "product": "sources", "source_job": 41}, {"job": 70, "product": "objects", "source_job": 62}]
    assert loop._chain({7: (62, 900)}, links) == {41: 60, 42: 61, 43: 62}


def test_an_intake_pending_holds_association():
    k1 = (expid(1, 1, 1), 8, 8)
    pend = [{"id": 70, "stage": "intake", "state": "running", "scope": {"dataset": D, "file": stem(2, 1, 1)}}]
    subs, _ = loop.plan(snap(l2={k1: l2row(7, 1.0, 20)}, refs={(7, 8): 30}, diffs={k1: 41},
                             sources={k1: 41}, jobs=pend), [], SEL)
    assert stages(subs, "associate") == []


def test_two_fields_advance_in_the_same_pass():
    a1, a2 = (expid(1, 1, 1), 8, 8), (expid(1, 1, 2), 8, 8)
    l2 = {a1: l2row(7, 1.0, 20), a2: l2row(9, 1.1, 21)}
    subs, _ = loop.plan(snap(l2=l2, refs={(7, 8): 30, (9, 8): 31}, diffs={a1: 41, a2: 42},
                             sources={a1: 41, a2: 42}), [], SEL)
    assert sorted(a["scope"]["field"] for a in stages(subs, "associate")) == [7, 9]


def test_failed_job_is_reported_and_its_key_not_resubmitted():
    k1 = (expid(1, 1, 1), 8, 8)
    failed = [{"id": 80, "stage": "difference", "state": "failed", "scope": {"expid": k1[0], "sca": 8, "fid": 8}}]
    subs, counts = loop.plan(snap(l2={k1: l2row(7, 1.0, 20)}, refs={(7, 8): 30}, jobs=failed), [], SEL)
    assert stages(subs, "difference") == []
    assert counts["failed"] == [{"job": 80, "stage": "difference"}]


def test_drained_pass_exits_zero(monkeypatch, capsys):
    k1 = (expid(1, 1, 1), 8, 8)
    done = snap(published_stems={stem(1, 1, 1)}, l2={k1: l2row(7, 1.0, 20)}, refs={(7, 8): 30},
                diffs={k1: 41}, sources={k1: 41}, packets={k1: 90},
                jobs=_associated({60: (7, 41)}), objects={7: (60, 900)}, chain={41: 60})
    monkeypatch.setattr(loop, "snapshot", lambda conn, run: done)
    monkeypatch.setattr(cli, "connect", lambda **kw: contextlib.nullcontext(None))
    monkeypatch.setattr(cli.foundation, "run_show", lambda conn, run: {"settings": SETTINGS})
    monkeypatch.setattr(cli.storage, "s3_client", lambda unsigned=False: Pages([stem(1, 1, 1) + "_cal.asdf"]))
    monkeypatch.setattr(cli.publish_kafka, "unpublished_sets", lambda conn, run: [])
    monkeypatch.setattr(cli.publish_kafka, "settling_sets", lambda conn, run: [])
    monkeypatch.setattr(cli.launch, "batch_client", lambda: Batch({}))
    monkeypatch.setattr(loop, "reconcile", lambda conn, run, client: {"retrying": []})
    code = cli.main(["loop", "--run", "12", "--filters", "F146", "--scas", "8", "--pointings", "001_0001",
                     "--job-definition", "jd", "--interval", "0"])
    assert code == ExitCode.SUCCESS
    assert '"drained": true' in capsys.readouterr().out
    summary = loop.run_pass(lambda: contextlib.nullcontext(None), 12, SEL, [stem(1, 1, 1)], lambda item: None,
                            lambda: {"packets": 0, "sets": [], "waiting": [90]})
    assert summary["sets_waiting"] == [90] and not summary["drained"]


class Batch:
    """A fake Batch client: ``describe_jobs`` answers from ``{batch id: (status, reason)}``."""

    def __init__(self, jobs: dict):
        self.jobs, self.calls = jobs, []

    def describe_jobs(self, jobs):
        self.calls.append(list(jobs))
        return {"jobs": [{"jobId": j, "status": self.jobs[j][0], "statusReason": self.jobs[j][1]}
                         for j in jobs if j in self.jobs]}


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
        if sql.startswith("UPDATE"):
            self.updates.append(params)
            self.rowcount = 1

    def fetchall(self):
        return self.rows

    def commit(self):
        pass


def test_a_container_that_ended_unrecorded_is_marked_failed_with_batchs_reason():
    conn = Rows([(70, "intake", "launched", "b-70"), (71, "intake", "running", "b-71")])
    batch = Batch({"b-70": ("FAILED", "Terminated by the operator"), "b-71": ("RUNNING", "")})
    checked = loop.reconcile(conn, 12, batch)
    assert checked["ended"] == [{"job": 70, "stage": "intake",
                                 "reason": "Batch FAILED: Terminated by the operator"}]
    assert [u[-1] for u in conn.updates] == [70] and conn.updates[0][1].startswith("Batch FAILED")
    assert checked["retrying"] == []


def test_a_failed_row_under_batchs_retry_is_held_pending_not_failed(monkeypatch):
    k1 = (expid(1, 1, 1), 8, 8)
    job = {"id": 80, "stage": "difference", "state": "failed",
           "scope": {"expid": k1[0], "sca": 8, "fid": 8}}
    monkeypatch.setattr(loop, "snapshot",
                        lambda conn, run: snap(l2={k1: l2row(7, 1.0, 20)}, refs={(7, 8): 30}, jobs=[job]))
    conn = Rows([(80, "difference", "failed", "b-80")])
    summary = loop.run_pass(lambda: contextlib.nullcontext(conn), 12, SEL, [], lambda item: None, None,
                            batch=Batch({"b-80": ("RUNNABLE", "")}))
    assert summary["failed"] == [] and summary["pending"]["difference"] == 1
    assert summary["submitted"]["difference"] == 0 and conn.updates == []


def test_a_local_job_without_a_batch_id_is_left_alone():
    conn, batch = Rows([(90, "import", "running", None)]), Batch({})
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

    # Once those two are associated the chain starts at the bound, not at the reference's rows.
    sources = {k2: 42, k3: 43}
    subs, _ = loop.plan(snap(settings=BOUND, l2=l2, refs={(7, 8): 30}, diffs=sources, sources=sources),
                        [], SEL)
    (a,) = stages(subs, "associate")
    assert a["inputs"][0]["source_job"] == 42


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
