"""Shared row manifests for RB CNN/RuBR dataset alignment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from classification.data_utils import resolve_npz_paths


def row_id(*, jid: str, row_index: int) -> str:
    return f"{Path(str(jid)).name}:{int(row_index)}"


def row_id_from_metadata(meta: dict[str, Any]) -> str:
    if meta.get("row_id"):
        return str(meta["row_id"])
    jid = meta.get("jid")
    if not jid:
        jid_folder = meta.get("jid_folder", "")
        jid = Path(str(jid_folder)).name if jid_folder else ""
    if not jid:
        raise ValueError(f"Metadata row lacks jid/jid_folder: {meta}")
    if "row_index" in meta:
        idx = int(meta["row_index"])
    elif "source_row_index" in meta:
        idx = int(meta["source_row_index"])
    else:
        idx = int(meta.get("id", meta.get("det_id", -1)))
    if idx < 0:
        raise ValueError(f"Metadata row lacks a usable row index: {meta}")
    return row_id(jid=str(jid), row_index=idx)


def records_from_npz(paths: str | Path | Sequence[str | Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for shard in resolve_npz_paths(paths):
        with np.load(shard, allow_pickle=True) as z:
            y = np.asarray(z["y"], dtype=np.int64)
            metadata = np.asarray(z["metadata"], dtype=object)
            for i, meta_obj in enumerate(metadata):
                meta = dict(meta_obj) if isinstance(meta_obj, dict) else {}
                records.append(
                    {
                        "row_id": row_id_from_metadata(meta),
                        "label": int(y[i]),
                        "xcentroid": float(meta.get("xcentroid", meta.get("x", np.nan))),
                        "ycentroid": float(meta.get("ycentroid", meta.get("y", np.nan))),
                        "metadata": meta,
                        "source_npz": str(shard),
                        "source_npz_row": int(i),
                    }
                )
    return records


def write_jsonl(records: Iterable[dict[str, Any]], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def alignment_report(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    *,
    left_name: str = "left",
    right_name: str = "right",
) -> dict[str, Any]:
    n = min(len(left), len(right))
    mismatches: list[dict[str, Any]] = []
    for i in range(n):
        lrow = left[i]
        rrow = right[i]
        problems = []
        if lrow.get("row_id") != rrow.get("row_id"):
            problems.append("row_id")
        if int(lrow.get("label", -1)) != int(rrow.get("label", -2)):
            problems.append("label")
        for key in ("xcentroid", "ycentroid"):
            lv = float(lrow.get(key, np.nan))
            rv = float(rrow.get(key, np.nan))
            if np.isfinite(lv) and np.isfinite(rv) and abs(lv - rv) > 1e-6:
                problems.append(key)
        if problems:
            mismatches.append({"index": i, "problems": problems, left_name: lrow, right_name: rrow})
            if len(mismatches) >= 20:
                break
    return {
        "schema_version": "rb_dataset_alignment_v1",
        "left_name": left_name,
        "right_name": right_name,
        "left_count": len(left),
        "right_count": len(right),
        "same_count": len(left) == len(right),
        "checked_count": n,
        "mismatch_count": len(mismatches),
        "aligned": len(left) == len(right) and not mismatches,
        "mismatches": mismatches,
    }
