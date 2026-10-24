"""scripts/inject_dataset.py: an injected dataset written outside the pipeline.

Contracts: the catalog is the same for a seed and differs for another;
files before the bound are copied byte for byte and files after it
injected, under their own names; the truth table has one row per source
per injected epoch, the same sources in every epoch of a pointing; an
object already under the destination is skipped, never rewritten; the
dry run writes nothing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

from tests.unit.fakes3 import FakeClientError, FakeS3

_SPEC = importlib.util.spec_from_file_location(
    "inject_dataset", Path(__file__).resolve().parents[2] / "scripts" / "inject_dataset.py")
inject_dataset = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inject_dataset)

SRC = "s3://pub/l2/"
DEST = "s3://inputs/r00342-injt/"
STEMS = [f"r00342010{p:02d}001004001_0001_wfi08_f146" for p in (1, 2, 3, 4, 5, 6)]
MJDS = {s: 61587.6 + 1.5 * i for i, s in enumerate(STEMS)}
BOUND = 61591.4


class OnceS3(FakeS3):
    """FakeS3 whose put_object honours If-None-Match: * as S3 does."""

    def put_object(self, *, Bucket, Key, Body, **kw):
        if kw.get("IfNoneMatch") == "*" and (Bucket, Key) in self._objects:
            raise FakeClientError("PreconditionFailed", "exists")
        return super().put_object(Bucket=Bucket, Key=Key, Body=Body, **kw)


def fake_image(path, stem):
    shift = int(stem[8:11])                      # each pass's WCS a little different
    return inject_dataset.keyed({
        "shape": (4088, 4088), "mjd": MJDS[stem], "zp": 26.861, "context": "roman_0046.pmap",
        "to_sky": lambda x, y: (10.0 + np.asarray(x) * 1e-5, -5.0 + np.asarray(y) * 1e-5),
        "to_pixel": lambda ra, dec: ((np.asarray(ra) - 10.0) / 1e-5 + 0.01 * shift,
                                     (np.asarray(dec) + 5.0) / 1e-5)}, stem)


def fake_inject(path, list_file, out, seed, context):
    out.write_bytes(b"injected:" + Path(path).read_bytes())


def setup(tmp_path, existing=()):
    s3 = OnceS3()
    for s in STEMS:
        s3.seed("pub", f"l2/{s}_cal.asdf", f"bytes of {s}".encode())
    for s in existing:
        s3.seed("inputs", f"r00342-injt/l2/{s}_cal.asdf", b"already")
    actions, groups = inject_dataset.plan(STEMS, MJDS, BOUND)
    ctx = {"source": SRC, "dest": DEST, "source_client": s3, "dest_client": s3,
           "existing": {s + "_cal.asdf" for s in existing}, "work": str(tmp_path), "seed": 7,
           "per_image": 20, "mag_range": (21.0, 27.0), "dataset": "r00342-injt",
           "open_image": fake_image, "inject": fake_inject}
    (group,) = groups.values()
    return s3, actions, group, ctx


def test_catalog_is_deterministic_for_a_seed():
    sky = lambda x, y: (x * 1e-5, y * 1e-5)  # noqa: E731
    a = inject_dataset.draw_catalog(7, (1, 1, 8), 50, (21, 27), (4088, 4088), sky)
    b = inject_dataset.draw_catalog(7, (1, 1, 8), 50, (21, 27), (4088, 4088), sky)
    c = inject_dataset.draw_catalog(8, (1, 1, 8), 50, (21, 27), (4088, 4088), sky)
    assert a == b and a != c
    assert all(21 <= s["mag"] <= 27 for s in a)
    assert all(100 <= s["ra"] / 1e-5 <= 4087 - 100 for s in a)


def test_before_the_bound_copied_after_injected_names_kept(tmp_path):
    s3, actions, group, ctx = setup(tmp_path)
    lines, rows = inject_dataset.process_pointing(group, actions, ctx)
    for s in STEMS:
        body = s3._objects[("inputs", f"r00342-injt/l2/{s}_cal.asdf")]
        if MJDS[s] > BOUND:
            assert body == b"injected:" + f"bytes of {s}".encode()
        else:
            assert body == f"bytes of {s}".encode()
    assert {k for (b, k) in s3._objects if b == "inputs"} == {f"r00342-injt/l2/{s}_cal.asdf" for s in STEMS}
    assert sorted(line.split()[1] for line in lines) == ["copied"] * 3 + ["injected"] * 3


def test_truth_one_row_per_source_per_injected_epoch(tmp_path):
    s3, actions, group, ctx = setup(tmp_path)
    _, rows = inject_dataset.process_pointing(group, actions, ctx)
    injected = [s for s in STEMS if MJDS[s] > BOUND]
    assert len(rows) == 20 * len(injected)
    by_epoch = {}
    for r in rows:
        by_epoch.setdefault(r["expid"], set()).add((r["src_id"], r["ra"], r["dec"], r["mag_ab"]))
    assert len(by_epoch) == len(injected)
    assert len({frozenset(v) for v in by_epoch.values()}) == 1      # the same sources in every epoch
    r = rows[0]
    assert np.isclose(r["dnps"], 10 ** (-0.4 * (r["mag_ab"] - 26.861)))


def test_existing_object_skipped_not_rewritten(tmp_path):
    s3, actions, group, ctx = setup(tmp_path, existing=[STEMS[0], STEMS[4]])
    lines, rows = inject_dataset.process_pointing(group, actions, ctx)
    assert s3._objects[("inputs", f"r00342-injt/l2/{STEMS[0]}_cal.asdf")] == b"already"
    assert s3._objects[("inputs", f"r00342-injt/l2/{STEMS[4]}_cal.asdf")] == b"already"
    assert sorted(line.split()[1] for line in lines) == ["copied"] * 2 + ["injected"] * 2 + ["skipped"] * 2
    assert {r["expid"] for r in rows} == {inject_dataset.stem_expid(s) for s in (STEMS[3], STEMS[5])}


def test_put_once_refuses_an_overwrite():
    s3 = OnceS3()
    assert inject_dataset.put_once(s3, "s3://b/k", b"one")
    assert not inject_dataset.put_once(s3, "s3://b/k", b"two")
    assert s3._objects[("b", "k")] == b"one"


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    s3 = OnceS3()
    for s in STEMS:
        s3.seed("pub", f"l2/{s}_cal.asdf", b"x")
    monkeypatch.setattr(inject_dataset, "list_stems", lambda client, root, sel: STEMS)
    monkeypatch.setattr(inject_dataset, "sidecar_mjd", lambda client, root, s: MJDS[s])

    class Paged:
        def paginate(self, **kw):
            return [s3.list_objects_v2(**kw)]

    s3.get_paginator = lambda name: Paged()
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    before = dict(s3._objects)
    assert inject_dataset.main(["--source-root", SRC, "--filter", "F146", "--scas", "8", "--pointings",
                                "001_0001", "--bound-mjd", str(BOUND), "--dest", DEST, "--dataset", "x",
                                "--truth", str(tmp_path / "t.parquet"), "--dry-run"]) == 0
    assert s3._objects == before and not (tmp_path / "t.parquet").exists()
    out = capsys.readouterr().out
    assert out.count(" inject mjd") == 3 and out.count(" copy mjd") == 3
