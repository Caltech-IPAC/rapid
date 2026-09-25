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
    row = {"scalefacref": 1.07, "dxrmsfin": 0.21, "dyrmsfin": 0.19, "dxmedianfin": 0.02,
           "dymedianfin": -0.03, "nsexcatsources": 123456,
           "source_counts": {"sextractor": {"positive": 1000, "negative": 900}}}
    row.update(over)
    return (row["scalefacref"], row["dxrmsfin"], row["dyrmsfin"], row["dxmedianfin"],
            row["dymedianfin"], row["nsexcatsources"], row["source_counts"])


def _params(policy):
    return load_policy(policy).find_check("difference-image-statistics@1").params


def test_control_like_values_pass_trial_and_fail_strict():
    passed = difference_image_statistics(_Conn([_row()]), "I", _params("rebuild-trial@1"))
    assert passed.outcome == "passed" and passed.detail["failing"] == []
    assert passed.detail["measurements"]["sextractor_pos_neg_ratio"] == pytest.approx(1000 / 900)
    failed = difference_image_statistics(_Conn([_row()]), "I", _params("rebuild-strict@1"))
    assert failed.outcome == "failed"
    assert failed.detail["failing"] == ["scalefacref", "dxrmsfin", "dyrmsfin", "nsexcatsources"]
    assert failed.detail["bounds"]["dxrmsfin"] == [None, 0.01]


@pytest.mark.parametrize("override, failing", [
    ({"dxmedianfin": -0.7}, "abs_dxmedianfin"),
    ({"dymedianfin": 0.6}, "abs_dymedianfin"),
    ({"nsexcatsources": 49999}, "nsexcatsources"),
    ({"scalefacref": 5.5}, "scalefacref"),
    ({"source_counts": {"sextractor": {"positive": 10, "negative": 0}}},
     "sextractor_pos_neg_ratio"),
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
