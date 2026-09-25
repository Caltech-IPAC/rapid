"""The two shipped checks (supervisor step 6, 2026-09-24, R2).

Both read the database only. Each returns a :class:`CheckResult` whose
detail records every measurement, every bound it was held to, and the
names of the failing measurements, so a recorded result can be read
without re-running the check.
"""

from __future__ import annotations

import json
import math
from typing import Any

from rapidpipe.checks.registry import CheckResult, check

_DIFF_PARAMS = ("scalefacref_lo", "scalefacref_hi", "rms_max", "median_max",
                "n_min", "n_max", "ratio_lo", "ratio_hi")


def _num(value: Any) -> float | None:
    """A measurement as a float, ``None`` when null."""
    return None if value is None else float(value)


def _abs(value: float | None) -> float | None:
    return None if value is None else abs(value)


def _recordable(value: float | None) -> float | str | None:
    """A measurement as ``detail`` records it: a finite float or ``None``
    as is, a non-finite one (NaN, Infinity) as text -- jsonb has no NaN,
    so a raw non-finite float would make the row unrecordable."""
    if value is None or math.isfinite(value):
        return value
    return str(value)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


@check("difference-image-statistics", "1", kind="difference-image", params=_DIFF_PARAMS)
def difference_image_statistics(conn, instance_id: str, params: dict[str, Any]) -> CheckResult:
    """Difference-image gain-matching and source-count statistics within bounds.

    Reads the instance's ``diffimmeta`` row through ``diffimages.instance``
    and passes when scalefacref is in [scalefacref_lo, scalefacref_hi],
    dxrmsfin and dyrmsfin are at most rms_max, |dxmedianfin| and
    |dymedianfin| are at most median_max, nsexcatsources is in [n_min,
    n_max], and -- only when ``source_counts`` records SExtractor counts --
    the positive/negative SExtractor ratio is in [ratio_lo, ratio_hi].
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.scalefacref, m.dxrmsfin, m.dyrmsfin, m.dxmedianfin,
                   m.dymedianfin, m.nsexcatsources, m.source_counts
            FROM diffimages d
            JOIN diffimmeta m ON m.pid = d.pid
            WHERE d.instance = %s
            """,
            (instance_id,),
        )
        rows = cur.fetchall()
    if len(rows) != 1:
        reason = ("no diffimmeta row" if not rows
                  else f"{len(rows)} diffimmeta rows") + " for this instance"
        return CheckResult("failed", {"reason": reason, "bounds": dict(params),
                                      "measurements": {}, "failing": ["diffimmeta"]},
                           reason)
    scalefacref, dxrms, dyrms, dxmed, dymed, nsex, source_counts = rows[0]
    if isinstance(source_counts, str):
        source_counts = json.loads(source_counts)

    # (measurement, value, lo, hi); None bounds are open.
    tests: list[tuple[str, Any, Any, Any]] = [
        ("scalefacref", _num(scalefacref), params["scalefacref_lo"], params["scalefacref_hi"]),
        ("dxrmsfin", _num(dxrms), None, params["rms_max"]),
        ("dyrmsfin", _num(dyrms), None, params["rms_max"]),
        ("abs_dxmedianfin", _abs(_num(dxmed)), None, params["median_max"]),
        ("abs_dymedianfin", _abs(_num(dymed)), None, params["median_max"]),
        ("nsexcatsources", None if nsex is None else int(nsex), params["n_min"], params["n_max"]),
    ]
    notes: list[str] = []
    sextractor = (source_counts or {}).get("sextractor") if isinstance(source_counts, dict) else None
    if sextractor and "positive" in sextractor and "negative" in sextractor:
        positive, negative = int(sextractor["positive"]), int(sextractor["negative"])
        ratio = positive / negative if negative else math.inf
        tests.append(("sextractor_pos_neg_ratio", ratio, params["ratio_lo"], params["ratio_hi"]))
    else:
        notes.append("source_counts has no SExtractor positive/negative counts; "
                     "the ratio bound was not applied")

    measurements: dict[str, Any] = {}
    bounds: dict[str, list[Any]] = {}
    failing: list[str] = []
    for name, value, lo, hi in tests:
        # A null or non-finite measurement fails whatever its bounds
        # (supervisor step 6, 2026-09-24, live-values correction).
        finite = value is not None and math.isfinite(value)
        measurements[name] = _recordable(value)
        bounds[name] = [lo, hi]
        if not finite or (lo is not None and value < lo) or (hi is not None and value > hi):
            failing.append(name)
    # The signed medians, sanitised like every other measurement (Codex
    # diff review of step 6: a raw NaN here made the failed row unrecordable).
    measurements["dxmedianfin"] = _recordable(_num(dxmed))
    measurements["dymedianfin"] = _recordable(_num(dymed))
    measurements["source_counts"] = source_counts

    if failing:
        parts = []
        for name, value, lo, hi in tests:
            if name not in failing:
                continue
            if value is None or not math.isfinite(value):
                parts.append(f"{name}={_fmt(value)} not finite")
            elif lo is None:
                parts.append(f"{name}={_fmt(value)} > {_fmt(hi)}")
            elif hi is None:
                parts.append(f"{name}={_fmt(value)} < {_fmt(lo)}")
            else:
                parts.append(f"{name}={_fmt(value)} outside [{_fmt(lo)}, {_fmt(hi)}]")
        summary = "; ".join(parts)
        outcome = "failed"
    else:
        summary = f"{len(tests)} measurements within bounds"
        outcome = "passed"
    detail = {"measurements": measurements, "bounds": bounds, "failing": failing}
    if notes:
        detail["notes"] = notes
    return CheckResult(outcome, detail, summary)


def _identity(cur, source_set_key: dict[str, Any]) -> tuple[int, int, int] | None:
    """``(expid, sca, fid)`` of the exposure a source set's chain reaches:
    key.difference -> that difference-image instance's key.l2 -> the
    ``l2files`` row carrying that instance. ``None`` when any link is
    missing."""
    difference = source_set_key.get("difference")
    if not difference:
        return None
    cur.execute(
        """
        SELECT l.expid, l.sca, l.fid
        FROM product_instances d
        JOIN l2files l ON l.instance::text = d.logical_key->>'l2'
        WHERE d.id = %s AND d.kind = 'difference-image'
        """,
        (difference,),
    )
    rows = cur.fetchall()
    return tuple(rows[0]) if len(rows) == 1 else None


#: Source-set instances of one catalog type, from runs other than the
#: candidate's, whose chain reaches the given (expid, sca, fid).
_SAME_IDENTITY = """
    SELECT s.id, s.run, s.custody
    FROM product_instances s
    JOIN product_instances d ON d.id::text = s.logical_key->>'difference'
                            AND d.kind = 'difference-image'
    JOIN l2files l ON l.instance::text = d.logical_key->>'l2'
    WHERE s.kind = %s
      AND s.logical_key->>'catalog_type' = %s
      AND s.run <> %s
      AND l.expid = %s AND l.sca = %s AND l.fid = %s
"""


@check("catalog-counts-vs-reference", "1", kind="source-set",
       params=("tolerance", "missing_reference", "reference_run"))
def catalog_counts_vs_reference(conn, instance_id: str, params: dict[str, Any]) -> CheckResult:
    """Result-set row count within a fractional tolerance of a reference's.

    Logical keys are per run, so the reference is found by science
    identity (supervisor step 6, 2026-09-24, live-values correction): the
    candidate source set's ``key.difference`` -> that difference image's
    ``key.l2`` -> its ``l2files`` row -> ``(expid, sca, fid)``. The
    reference is a source set of the same ``catalog_type`` from another
    run whose chain reaches the same triple: the ``reference_run``'s
    (selected attempt) when that param is non-empty, else the most
    recently published one with custody ``current``. Passes when
    |cand - ref| / ref <= tolerance; with no reference the outcome is
    ``missing_reference`` (``pass`` or ``fail``); a named reference run
    with no such instance fails.
    """
    tolerance = float(params["tolerance"])
    missing = params["missing_reference"]
    reference_run = params.get("reference_run") or None
    detail: dict[str, Any] = {"bounds": {"tolerance": tolerance, "missing_reference": missing,
                                         "reference_run": reference_run},
                              "measurements": {}, "failing": []}

    def fail(reason: str, failing: str) -> CheckResult:
        detail.update(reason=reason, failing=[failing])
        return CheckResult("failed", detail, reason)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pi.kind, pi.logical_key, pi.run, rs.instance IS NOT NULL, rs.complete,
                   rs.row_count
            FROM product_instances pi
            LEFT JOIN result_sets rs ON rs.instance = pi.id
            WHERE pi.id = %s
            """,
            (instance_id,),
        )
        row = cur.fetchone()
        if row is None:
            return fail("instance does not exist", "candidate")
        kind, logical_key, run, is_result_set, complete, cand_count = row
        detail["measurements"]["candidate_row_count"] = cand_count
        if not is_result_set:
            return fail("not a result set", "candidate_row_count")
        if not complete:
            return fail("result set is incomplete", "candidate_row_count")
        if cand_count is None:
            return fail("result set has no row count", "candidate_row_count")
        catalog_type = (logical_key or {}).get("catalog_type")
        identity = _identity(cur, logical_key or {})
        if catalog_type is None or identity is None:
            return fail("cannot resolve the candidate's science identity (key.catalog_type, "
                        "key.difference -> key.l2 -> l2files)", "identity")
        expid, sca, fid = identity
        detail["identity"] = {"expid": expid, "sca": sca, "fid": fid,
                              "catalog_type": catalog_type}

        query_args = [kind, catalog_type, run, expid, sca, fid]
        if reference_run:
            cur.execute(
                _SAME_IDENTITY + """
                  AND s.run = %s
                  AND EXISTS (SELECT 1 FROM attempts a JOIN units u ON u.id = a.unit
                              WHERE a.id = s.producing_attempt
                                AND u.selected_attempt = s.producing_attempt)
                ORDER BY s.published_at DESC, s.id DESC
                """,
                query_args + [reference_run],
            )
            found = cur.fetchall()
            chosen_as = f"run {reference_run}"
            if len(found) != 1:
                detail["reference"] = {"instance": None, "run": reference_run,
                                       "chosen_as": chosen_as}
                return fail(f"reference run {reference_run} has {len(found)} source sets "
                            "for this identity", "reference")
        else:
            cur.execute(
                _SAME_IDENTITY + """
                  AND s.custody = 'current'
                ORDER BY s.published_at DESC, s.id DESC
                LIMIT 1
                """,
                query_args,
            )
            found = cur.fetchall()
            chosen_as = "current"
        reference, ref_run = (found[0][0], found[0][1]) if found else (None, None)

        ref_count = None
        if reference is not None:
            cur.execute(
                "SELECT complete, row_count FROM result_sets WHERE instance = %s", (reference,))
            ref_row = cur.fetchone()
            if ref_row is not None and ref_row[0] and ref_row[1] is not None:
                ref_count = int(ref_row[1])
    detail["reference"] = {"instance": reference, "run": ref_run, "chosen_as": chosen_as,
                           "row_count": ref_count}
    detail["measurements"]["reference_row_count"] = ref_count

    if ref_count is None:
        if reference is not None and reference_run:
            return fail(f"reference {reference} has no complete row count", "reference")
        reason = ("no reference (no current source set of this identity in another run)"
                  if reference is None
                  else f"reference {reference} has no complete row count")
        detail["reason"] = reason
        if missing == "pass":
            return CheckResult("passed", detail, f"{reason}; missing_reference=pass")
        detail["failing"] = ["reference"]
        return CheckResult("failed", detail, f"{reason}; missing_reference=fail")

    cand_count = int(cand_count)
    if ref_count == 0:
        relative = 0.0 if cand_count == 0 else math.inf
    else:
        relative = abs(cand_count - ref_count) / ref_count
    detail["measurements"]["relative_difference"] = relative if math.isfinite(relative) else "inf"
    summary = (f"rows={cand_count} reference={ref_count} (run {ref_run}) "
               f"relative={relative:.4g} tolerance={tolerance:g}")
    if relative <= tolerance:
        return CheckResult("passed", detail, summary)
    detail["failing"] = ["relative_difference"]
    return CheckResult("failed", detail, summary)
