"""The quality gate's read against the schema.

Contract: ``decide`` judges a packet set on the difference of its own
exposure and SCA, the ``sources`` input keyed by its scope, however many
other source sets it names (a packet job records the source sets of the
objects it associated with as ``sources`` inputs too: run 247's 132913 was
held on another exposure's coverage); a set naming one source set is judged
on it.
"""

from __future__ import annotations

from psycopg2.extras import Json

from rapidpipe.foundation import gate

GATE = {"min_epochs": 1}


def _job(cur, run: int, stage: str, scope: dict, metrics: dict | None = None) -> int:
    cur.execute("INSERT INTO jobs (run, stage, scope, state, published_attempt, manifest)"
                " VALUES (%s, %s, %s, 'published', 1, %s) RETURNING id",
                (run, stage, Json(scope), Json({"metrics": metrics or {}})))
    return cur.fetchone()[0]


def _sources(cur, packet: int, difference: int, scope: dict) -> None:
    cur.execute("INSERT INTO job_inputs (job, kind, product, key, source_job)"
                " VALUES (%s, 'input', 'sources', %s, %s)", (packet, Json({**scope, "dataset": "D"}), difference))


def test_a_set_is_judged_on_its_own_difference_whichever_source_set_it_names_first(connect):
    conn = connect()
    own, other = {"expid": 2020101, "sca": 4, "fid": 2}, {"expid": 2020103, "sca": 4, "fid": 2}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO runs (who, why, revision, settings, survey, dataset)"
                    " VALUES ('t', 't', 'r', '{}', 'S', 'D') RETURNING id")
        run = cur.fetchone()[0]
        mine = _job(cur, run, "difference", own, {"usable_fraction": 0.99, "n_sources_snr_gt20": 10})
        theirs = _job(cur, run, "difference", other, {"usable_fraction": 0.80, "n_sources_snr_gt20": 10})
        two, one = _job(cur, run, "packet", own), _job(cur, run, "packet", other)
        _sources(cur, two, theirs, other)
        _sources(cur, two, mine, own)
        _sources(cur, one, theirs, other)
    conn.commit()
    answer = gate.decide(conn, two, GATE)
    assert answer["difference_job"] == mine and answer["send"]
    answer = gate.decide(conn, one, GATE)
    assert answer["difference_job"] == theirs and answer["metric"] == "usable_fraction"
