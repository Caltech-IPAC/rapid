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
        ("scalefacref", float(scalefacref), params["scalefacref_lo"], params["scalefacref_hi"]),
        ("dxrmsfin", float(dxrms), None, params["rms_max"]),
        ("dyrmsfin", float(dyrms), None, params["rms_max"]),
        ("abs_dxmedianfin", abs(float(dxmed)), None, params["median_max"]),
        ("abs_dymedianfin", abs(float(dymed)), None, params["median_max"]),
        ("nsexcatsources", int(nsex), params["n_min"], params["n_max"]),
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
        measurements[name] = value if not (isinstance(value, float) and math.isinf(value)) else "inf"
        bounds[name] = [lo, hi]
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            failing.append(name)
    measurements["dxmedianfin"] = float(dxmed)
    measurements["dymedianfin"] = float(dymed)
    measurements["source_counts"] = source_counts

    if failing:
        parts = []
        for name, value, lo, hi in tests:
            if name not in failing:
                continue
            if lo is None:
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


def _selected_instance_of_run(cur, run_id: str, kind: str, key_json: str) -> list[str]:
    cur.execute(
        """
        SELECT pi.id
        FROM product_instances pi
        JOIN attempts a ON a.id = pi.producing_attempt
        JOIN units u ON u.id = a.unit
        WHERE pi.run = %s AND pi.kind = %s AND pi.logical_key = %s::jsonb
          AND u.selected_attempt = pi.producing_attempt
        ORDER BY pi.id
        """,
        (run_id, kind, key_json),
    )
    return [r[0] for r in cur.fetchall()]


@check("catalog-counts-vs-reference", "1", kind="source-set",
       params=("tolerance", "missing_reference", "reference_run"))
def catalog_counts_vs_reference(conn, instance_id: str, params: dict[str, Any]) -> CheckResult:
    """Result-set row count within a fractional tolerance of the reference's.

    The candidate's ``result_sets.row_count`` against the reference
    instance's for the same kind and logical key. The reference is the
    run ``reference_run``'s instance for the key (selected attempt) when
    that param is non-empty; otherwise the key's current instance, or --
    when the candidate itself is current -- the before-instance of the
    latest promotion that made it current. Passes when |cand - ref| / ref
    <= tolerance; with no usable reference the outcome is
    ``missing_reference`` (``pass`` or ``fail``).
    """
    tolerance = float(params["tolerance"])
    missing = params["missing_reference"]
    reference_run = params.get("reference_run") or None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pi.kind, pi.logical_key, rs.instance IS NOT NULL, rs.complete, rs.row_count
            FROM product_instances pi
            LEFT JOIN result_sets rs ON rs.instance = pi.id
            WHERE pi.id = %s
            """,
            (instance_id,),
        )
        row = cur.fetchone()
        if row is None:
            return CheckResult("failed", {"reason": "instance does not exist"},
                               "instance does not exist")
        kind, logical_key, is_result_set, complete, cand_count = row
        key_json = json.dumps(logical_key, sort_keys=True)
        detail: dict[str, Any] = {"bounds": {"tolerance": tolerance,
                                             "missing_reference": missing,
                                             "reference_run": reference_run},
                                  "measurements": {"candidate_row_count": cand_count},
                                  "failing": []}
        if not is_result_set or not complete or cand_count is None:
            reason = ("not a result set" if not is_result_set
                      else "result set is incomplete" if not complete
                      else "result set has no row count")
            detail.update(reason=reason, failing=["candidate_row_count"])
            return CheckResult("failed", detail, reason)

        reference: str | None = None
        how: str
        if reference_run:
            found = _selected_instance_of_run(cur, reference_run, kind, key_json)
            how = f"run {reference_run}"
            if len(found) > 1:
                detail.update(reason=f"reference run {reference_run} has {len(found)} "
                                     "instances for this key", failing=["reference"])
                return CheckResult("failed", detail, detail["reason"])
            reference = found[0] if found else None
        else:
            cur.execute(
                "SELECT id FROM product_instances WHERE kind = %s AND logical_key = %s::jsonb "
                "AND custody = 'current'",
                (kind, key_json),
            )
            current = cur.fetchone()
            reference = current[0] if current else None
            how = "current"
            if reference == instance_id:
                cur.execute(
                    """
                    SELECT pc.before_instance
                    FROM promotion_changes pc
                    JOIN promotions p ON p.id = pc.promotion
                    WHERE pc.kind = %s AND pc.logical_key = %s::jsonb
                      AND pc.after_instance = %s
                    ORDER BY p.happened_at DESC, p.id DESC
                    LIMIT 1
                    """,
                    (kind, key_json, instance_id),
                )
                previous = cur.fetchone()
                reference = previous[0] if previous else None
                how = "previous current (the candidate is current)"

        ref_count = None
        if reference is not None:
            cur.execute(
                "SELECT complete, row_count FROM result_sets WHERE instance = %s", (reference,))
            ref_row = cur.fetchone()
            if ref_row is not None and ref_row[0] and ref_row[1] is not None:
                ref_count = int(ref_row[1])
    detail["reference"] = {"instance": reference, "chosen_as": how, "row_count": ref_count}
    detail["measurements"]["reference_row_count"] = ref_count

    if ref_count is None:
        reason = (f"no reference ({how}: none)" if reference is None
                  else f"reference {reference} ({how}) has no complete row count")
        outcome = "passed" if missing == "pass" else "failed"
        detail["reason"] = reason
        if outcome == "failed":
            detail["failing"] = ["reference"]
        return CheckResult(outcome, detail,
                           f"{reason}; missing_reference={missing}")

    cand_count = int(cand_count)
    if ref_count == 0:
        relative = 0.0 if cand_count == 0 else math.inf
    else:
        relative = abs(cand_count - ref_count) / ref_count
    detail["measurements"]["relative_difference"] = relative if math.isfinite(relative) else "inf"
    summary = (f"rows={cand_count} reference={ref_count} ({how}) "
               f"relative={relative:.4g} tolerance={tolerance:g}")
    if relative <= tolerance:
        return CheckResult("passed", detail, summary)
    detail["failing"] = ["relative_difference"]
    return CheckResult("failed", detail, summary)
