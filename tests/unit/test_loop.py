"""``rapidpipe loop``: one pass's decisions from a listing and one snapshot of the tables.

Contracts: a listed stem with no published intake and no intake job of the
run is submitted once, and not again while its job is pending; a reference
is submitted only once its field has ``min_frames`` official L2 rows; each
field's associate chain submits one epoch at a time in ``mjdobs`` order and
waits while an earlier exposure of the field is upstream; two fields
advance in the same pass; a pass with nothing new, pending or to submit is
drained and the verb exits 0.
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
    """A fake S3 client whose ``list_objects_v2`` paginator returns two pages."""

    def __init__(self, keys: list[str]):
        self.keys = keys

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        half = len(self.keys) // 2
        return [{"Contents": [{"Key": Prefix + k} for k in self.keys[:half]]},
                {"Contents": [{"Key": Prefix + k} for k in self.keys[half:]]}]


def snap(**kw) -> loop.Snapshot:
    base = dict(dataset=D, survey="HLWAS", settings=SETTINGS, fids={"W146": 8, "F184": 1},
                published_stems=set(), jobs=[],
                psfs={(8, 8, "science"): (10, 100), (8, 8, "reference"): (11, 101)},
                exposures=set(), l2={}, refs={}, diffs={}, sources={}, objects={}, packets={},
                gaia={"key": {"name": "gaia-x", "version": "DR3"}, "job": 12})
    base.update(kw)
    return loop.Snapshot(**base)


def l2row(field: int, mjd: float, job: int) -> dict:
    return {"field": field, "mjdobs": mjd, "job": job}


def stages(subs: list[dict], name: str) -> list[dict]:
    return [s for s in subs if s["stage"] == name]


def test_listing_reads_every_page_and_keeps_cal_stems():
    keys = [stem(1, 1, 1) + "_cal.asdf", stem(1, 1, 1) + "_wcs.asdf",
            stem(2, 1, 1) + "_cal.asdf", stem(2, 1, 1) + "_thumb.png"]
    assert loop.list_stems(Pages(keys), "s3://bucket/l2/") == [stem(1, 1, 1), stem(2, 1, 1)]


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
                             objects={7: (60, 900)}), [], SEL)
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
                jobs=_associated({60: (7, 41)}), objects={7: (60, 900)})
    monkeypatch.setattr(loop, "snapshot", lambda conn, run: done)
    monkeypatch.setattr(cli, "connect", lambda **kw: contextlib.nullcontext(None))
    monkeypatch.setattr(cli.foundation, "run_show", lambda conn, run: {"settings": SETTINGS})
    monkeypatch.setattr(cli.storage, "s3_client", lambda unsigned=False: Pages([stem(1, 1, 1) + "_cal.asdf"]))
    monkeypatch.setattr(cli.publish_kafka, "unpublished_sets", lambda conn, run: [])
    code = cli.main(["loop", "--run", "12", "--filters", "F146", "--scas", "8", "--pointings", "001_0001",
                     "--job-definition", "jd", "--interval", "0"])
    assert code == ExitCode.SUCCESS
    assert '"drained": true' in capsys.readouterr().out
