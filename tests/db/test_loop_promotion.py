"""DB-backed proof of the loop's own promotion gate (loop.md §Promotion;
decision-loop-promotion): under a policy that does not permit automatic
promotion (``rebuild-trial@1``, the default; no shipped policy permits
it), ``loop._promote`` leaves the run's candidates exactly that, a
candidate -- no ``promotions`` row, the run still finishes, and the
record's text says promotion is a person's. A person's own promotion
(what ``rapidpipe run promote`` calls) then promotes it for real.

Everything runs inside the conftest's never-committed outer transaction.
"""

from __future__ import annotations

import datetime as dt

from rapidpipe.launch import loop
from rapidpipe.runs import repository as repo

from .test_checks import _diff_candidate, _source_set
from .test_register_l2 import _NoCloseNoCommitConnProxy
from .test_repository import _make_run

SPEC = """
[loop]
schedule = "test-promotion-loop"
release = "rebuild-v0.1"
kind = "production"
owner = "test"
lane = "prompt"
max_attempts = 2

[[dates]]
processing_date = 2027-10-01
[[dates.detector_images]]
delivery = "s3://b/control/delivery/x"
difference_template = "s3://b/control/step3/x"
"""


def _custody(conn, instance):
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance,))
        return cur.fetchone()[0]


def _promotions_for(conn, run_id):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM promotions WHERE request_context->>'run' = %s",
                    (run_id,))
        return cur.fetchone()[0]


def test_loop_leaves_a_candidate_under_the_trial_policy_then_a_person_promotes(conn):
    run_id = _make_run(conn)
    diff = _diff_candidate(conn, run_id)
    catalog = _source_set(conn, run_id, key={"k": "x"}, rows=10)
    spec = loop.parse_spec(SPEC, "test")

    # _promote commits its recorded checks; the proxy keeps the test inside
    # the conftest's outer transaction.
    promotion_id, text, gate, checks = loop._promote(
        _NoCloseNoCommitConnProxy(conn), run_id, spec, dt.date(2027, 10, 1))

    # rebuild-trial@1 (the spec names none, so the default) does not
    # permit automatic promotion: the loop never calls promote_run.
    assert promotion_id is None
    assert text == "candidate; promotion is a person's (policy rebuild-trial@1)"
    assert gate == "check policy rebuild-trial@1"
    assert checks  # the policy's checks still ran and were recorded

    assert (_custody(conn, diff), _custody(conn, catalog)) == ("candidate", "candidate")
    assert _promotions_for(conn, run_id) == 0

    # The date still finishes (loop.md §Promotion): a candidate is not a
    # failure of the run.
    repo.finish_run(conn, run_id)
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone()[0] == "finished"

    # A person's own promotion (``rapidpipe run promote``) does promote it.
    promotion_id = repo.promote_run(conn, run_id, "a-person", "reviewed and approved")
    assert (_custody(conn, diff), _custody(conn, catalog)) == ("current", "current")
    assert _promotions_for(conn, run_id) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT who, reason FROM promotions WHERE id = %s", (promotion_id,))
        assert cur.fetchone() == ("a-person", "reviewed and approved")
