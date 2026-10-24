"""``rapidpipe.foundation.attempts.report``: a run's Batch submissions measured.

Contracts: submissions are counted per queue; a reclaim is a Spot entry
whose status reason is Batch's lost-host reason, and a prompt entry with it
is not one; a hedge is an entry the reconcile hedged; lost job-hours are the
started-to-end hours of every entry not published; the fill time runs from
the first submission to the moment the fill concurrency was running at
once, and is ``None`` until then.
"""

from __future__ import annotations

from rapidpipe.foundation import attempts


def t(s: int) -> str:
    return f"2026-10-24T08:{s // 60:02d}:{s % 60:02d}+00:00"


def test_the_report_counts_queues_reclaims_hedges_lost_hours_and_the_fill(monkeypatch):
    monkeypatch.setattr(attempts, "FILL_VCPUS", 2)
    entries = [
        {"queue": "spot-q", "submitted": t(0), "started": t(60), "stopped": t(660),
         "status_reason": "Host EC2 (instance i-1) terminated."},
        {"queue": "spot-q", "submitted": t(0), "started": t(120), "hedged": t(420), "status_reason": "hedge: x"},
        {"queue": "prompt-q", "submitted": t(30), "started": t(700), "ended": t(800), "outcome": "published"},
        {"queue": "prompt-q", "submitted": t(30), "status_reason": "Host EC2 (instance i-2) terminated."},
    ]
    r = attempts.report(entries, "spot-q")
    assert r["submissions"] == {"spot-q": 2, "prompt-q": 2}
    assert r["reclaims"] == 1 and r["hedges"] == 1
    assert r["lost_job_hours"] == round((600 + 300) / 3600, 3)
    assert r["fill_seconds"] == 120
    monkeypatch.setattr(attempts, "FILL_VCPUS", 3)
    assert attempts.report(entries, "spot-q")["fill_seconds"] is None
