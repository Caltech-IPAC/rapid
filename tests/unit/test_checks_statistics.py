"""``difference-image-statistics@1`` over a stand-in cursor, and the
``check`` result-line format (supervisor step 6, 2026-09-24, R2, R6)."""

from __future__ import annotations

import pytest

from rapidpipe.checks.builtin import difference_image_statistics
from rapidpipe.checks.policy import load_policy
from rapidpipe.checks.runner import RecordedCheck


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, rows):
        self.cur = _Cursor(rows)

    def cursor(self):
        return self.cur


def _row(**over):
    # The control run's values in rapid_rebuild (live-values correction).
    row = {"scalefacref": 17572.896, "dxrmsfin": 0.25, "dyrmsfin": 0.56, "dxmedianfin": 0.004,
           "dymedianfin": -0.48, "nsexcatsources": 21749,
           "source_counts": {"sextractor": {"positive": 21749, "negative": 55451},
                             "photutils": {"positive": 85079, "negative": 58599}}}
    row.update(over)
    return (row["scalefacref"], row["dxrmsfin"], row["dyrmsfin"], row["dxmedianfin"],
            row["dymedianfin"], row["nsexcatsources"], row["source_counts"])


def _params(policy):
    return load_policy(policy).find_check("difference-image-statistics@1").params


def test_control_like_values_pass_trial_and_fail_strict():
    passed = difference_image_statistics(_Conn([_row()]), "I", _params("rebuild-trial@1"))
    assert passed.outcome == "passed" and passed.detail["failing"] == []
    assert passed.detail["measurements"]["sextractor_pos_neg_ratio"] == pytest.approx(21749 / 55451)
    failed = difference_image_statistics(_Conn([_row()]), "I", _params("rebuild-strict@1"))
    assert failed.outcome == "failed"
    assert failed.detail["failing"] == ["scalefacref", "dxrmsfin", "dyrmsfin", "abs_dymedianfin",
                                        "nsexcatsources", "sextractor_pos_neg_ratio"]
    assert failed.detail["bounds"]["dxrmsfin"] == [None, 0.01]


@pytest.mark.parametrize("override, failing", [
    ({"dxmedianfin": -1.5}, "abs_dxmedianfin"),
    ({"dymedianfin": 1.2}, "abs_dymedianfin"),
    ({"dxrmsfin": 2.5}, "dxrmsfin"),
    ({"nsexcatsources": 999}, "nsexcatsources"),
    ({"nsexcatsources": 1000001}, "nsexcatsources"),
    ({"scalefacref": 2e5}, "scalefacref"),
    ({"scalefacref": 1e-4}, "scalefacref"),
    ({"source_counts": {"sextractor": {"positive": 10, "negative": 0}}},
     "sextractor_pos_neg_ratio"),
    ({"source_counts": {"sextractor": {"positive": 21749, "negative": 1000}}},
     "sextractor_pos_neg_ratio"),
    ({"scalefacref": float("nan")}, "scalefacref"),
    ({"dyrmsfin": float("inf")}, "dyrmsfin"),
    ({"dxmedianfin": None}, "abs_dxmedianfin"),
])
def test_each_bound_can_fail(override, failing):
    result = difference_image_statistics(_Conn([_row(**override)]), "I",
                                         _params("rebuild-trial@1"))
    assert (result.outcome, result.detail["failing"]) == ("failed", [failing])


def test_ratio_is_skipped_without_sextractor_counts():
    for counts in (None, {"photutils": {"positive": 1, "negative": 1}}, '{"sextractor": {}}'):
        result = difference_image_statistics(_Conn([_row(source_counts=counts)]), "I",
                                             _params("rebuild-trial@1"))
        assert result.outcome == "passed"
        assert "sextractor_pos_neg_ratio" not in result.detail["bounds"]
        assert result.detail["notes"]


@pytest.mark.parametrize("rows, reason", [([], "no diffimmeta row"),
                                          ([_row(), _row()], "2 diffimmeta rows")])
def test_missing_or_duplicate_rows_fail(rows, reason):
    result = difference_image_statistics(_Conn(rows), "I", _params("rebuild-trial@1"))
    assert result.outcome == "failed"
    assert result.summary.startswith(reason)


def test_result_line_format():
    row = RecordedCheck("C1", "I1", "difference-image", {"sca": 3, "exposure": 7},
                        "difference-image-statistics", "1", True, "failed",
                        {"summary": "nsexcatsources=5 outside [50000, 500000]"})
    assert row.line() == (
        'instance=I1 kind=difference-image key={"exposure":7,"sca":3} '
        "check=difference-image-statistics@1 required=true outcome=failed "
        "nsexcatsources=5 outside [50000, 500000]")


def test_a_non_finite_measurement_is_recorded_as_text_and_named():
    result = difference_image_statistics(_Conn([_row(scalefacref=float("nan"))]), "I",
                                         _params("rebuild-trial@1"))
    assert result.detail["measurements"]["scalefacref"] == "nan"
    assert result.summary == "scalefacref=nan not finite"


@pytest.mark.parametrize("override", [
    {"dxmedianfin": float("nan")}, {"dymedianfin": float("-inf")},
    {"dxmedianfin": float("inf"), "dymedianfin": float("nan")},
])
def test_non_finite_signed_medians_are_recorded_as_text(override):
    """Codex diff review of step 6: the signed medians reached detail raw,
    so a NaN median made the failed row unrecordable (jsonb has no NaN)."""
    import json

    result = difference_image_statistics(_Conn([_row(**override)]), "I",
                                         _params("rebuild-trial@1"))
    assert result.outcome == "failed"
    for name, value in override.items():
        assert result.detail["measurements"][name] == str(value)
    json.dumps(result.detail, allow_nan=False)  # raises ValueError on a raw NaN
