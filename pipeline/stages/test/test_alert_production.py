"""
File:    test_alert_production.py

Tests for the alert-production job type — the step-4 trigger.

**THE DOUBLES CAN REFUSE**, the same discipline `test_post_db` states: a
producer that accepts everything and a watermark that always claims would
pass these tests against code with no emission control at all. So the
watermark double enforces its real primary key (one claim per unit per
release, later claims lose), and the producer double can fail a send and can
fail the flush — because "delivery failure raises loudly" and "a candidate
failure never fails the attempt" are opposite behaviours that only a double
capable of both can distinguish.

The 2026-08-04 Q7 finding is why the flush case is tested at all: every send
failed, `flush()` returned normally, and the run reported publishing alerts
it had not published.
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def _install_third_party_stubs() -> None:
    """Stub only what is genuinely missing from this interpreter."""
    names = [
        "numpy", "numpy.ma", "scipy", "scipy.ndimage",
        "astropy", "astropy.io", "astropy.io.fits", "astropy.io.ascii",
        "astropy.table", "astropy.wcs", "astropy.coordinates", "astropy.units",
        "boto3", "botocore", "botocore.exceptions",
        "dateutil", "dateutil.tz",
        "fastavro", "fastavro.schema", "fastavro.write", "fastavro.types",
        "galsim", "galsim.wcs", "galsim.roman",
        "romanisim", "romanisim.bandpass", "romanisim.catalog",
        "romanisim.image", "romanisim.psf",
        "photutils", "photutils.background", "photutils.segmentation",
        "injectionLightCurveModels",
    ]
    for name in names:
        if name in sys.modules:
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                continue
        except (ImportError, ValueError):
            pass
        sys.modules[name] = types.ModuleType(name)


_install_third_party_stubs()

from database.modules.utils.rapid_db import RAPIDDB        # noqa: E402
from pipeline.runtime.errors import InputError             # noqa: E402
from pipeline.stages import alert_production                # noqa: E402
from submission import payloads                             # noqa: E402
from submission.manifest import ProcessingUnit               # noqa: E402
from submission.routes import JOB_TYPE_ALERT_PRODUCTION      # noqa: E402


class Source:
    """A candidate, with only what the selection reads."""

    def __init__(self, sid, snr=None):
        self.sid = sid
        self.snr = snr


class FakeConn:
    """The borrowed connection `produce_alerts` claims/confirms through.

    `produce_alerts` now writes the CAS claim and confirm through
    `RAPIDDB.borrowing(context.require_connection())` rather than through an
    injected watermark object (migration 037 / integration ruling 3) — so the
    double has to stand in for the psycopg2 connection itself, the same idiom
    `pipeline/registration/test/test_consumer.py`'s own `FakeConn` uses:
    `cursor()` returns something with `execute`/`fetchone`/`close`, `commit`/
    `rollback` are tracked so a test can assert the transaction boundary
    (`transaction(conn)`, not `RAPIDDB.borrowing`'s own suppressed one).

    THE DOUBLE CAN REFUSE. `alert_emissions` is modelled as a real CAS would
    behave: at most one row per (exposure_id, sca, release_identity), a claim
    succeeds only under the real WHERE clause (state='claimed' AND (stale OR
    same claimant OR prior claimant terminal)), and confirm succeeds only
    when the caller's own token still matches. A double that always claimed,
    or always confirmed, could not show the suppression or takeover paths at
    all — the same discipline the old `Watermark` stated for `ON CONFLICT DO
    NOTHING`.
    """

    def __init__(self, failure=0, rows=None, terminal_attempts=()):
        self.exit_code = 0
        self.failure = failure
        #: (exposure_id, sca, release_identity) -> row dict: state,
        #: claim_token, claimed_at (a monotonic counter standing in for
        #: real time — "stale" is modelled by `stale_keys` below instead of
        #: wall-clock math, which a unit test has no business depending on).
        self.rows = dict(rows or {})
        #: Keys whose claim should be treated as PAST the staleness
        #: threshold, for the takeover tests.
        self.stale_keys = set()
        #: attempt ids whose owning claim should be treated as terminal
        #: (parseable claim_token + terminal lifecycle_state), for the
        #: owner-terminal takeover tests.
        self.terminal_attempts = set(terminal_attempts)
        self.commits = 0
        self.rollbacks = 0
        self.statements = []
        self.milestones = []
        #: `ConnectionExecutor.execute` (the milestone writer's path) reads
        #: `cur.description`/`cur.rowcount` — None/1 models "an INSERT with
        #: no RETURNING clause", exactly `record_milestone`'s statement
        #: shape, so the executor takes the `rowcount` branch rather than
        #: trying to `fetchall()` a result set that was never produced.
        self.description = None
        self.rowcount = 1

    # -- psycopg2 connection surface -----------------------------------
    def cursor(self):
        return self

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    # -- cursor surface: dispatches on statement shape, like the
    #    registration consumer's own FakeConn --------------------------
    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        lowered = " ".join(statement.lower().split())
        if self.failure:
            self._last_result = None
            raise RuntimeError(f"stubbed query failure ({self.failure})")
        if "insert into alert_emissions" in lowered and "on conflict" in lowered:
            self._last_result = self._claim(params)
        elif "update alert_emissions" in lowered and "state = 'emitted'" in lowered:
            self._last_result = self._confirm(params)
        elif "insert into milestones" in lowered:
            self.milestones.append(params)
            self._last_result = None
        else:
            self._last_result = None

    def fetchone(self):
        return self._last_result

    def fetchall(self):
        return [] if self._last_result is None else [self._last_result]

    # -- the CAS itself, in Python, matching claim_alert_emission's SQL -
    def _claim(self, params):
        (exposure_id, sca, release_identity, attempt_id, pid,
         claim_token, claiming_attempt_id) = params
        key = (exposure_id, sca, release_identity)
        row = self.rows.get(key)
        if row is None:
            self.rows[key] = {"state": "claimed", "claim_token": claim_token}
            return (claim_token,)
        if row["state"] != "claimed":
            return None  # terminal (watermark_seed / emitted): never touched
        stale = key in self.stale_keys
        same_claimant = row["claim_token"] == claim_token
        prior_terminal = (row["claim_token"].isdigit()
                          and int(row["claim_token"]) in self.terminal_attempts)
        if stale or same_claimant or prior_terminal:
            row["claim_token"] = claim_token
            return (claim_token,)
        return None  # lost the race: fresh, not a retry, not terminal

    def _confirm(self, params):
        (alerts_published, exposure_id, sca, release_identity, claim_token,
         confirmed_token) = params
        key = (exposure_id, sca, release_identity)
        row = self.rows.get(key)
        if row is None or row["state"] != "claimed" or row["claim_token"] != claim_token:
            return None  # taken over, or already confirmed
        row["state"] = "emitted"
        row["alerts_published"] = alerts_published
        return (confirmed_token,)


class Producer:
    """A producer that can fail a send, and can fail the flush."""

    def __init__(self, fail_sids=(), flush_error=None):
        self.fail_sids = set(fail_sids)
        self.flush_error = flush_error
        self.published = []
        self.flushed = False

    def produce(self, topic, value, callback=None):
        self.published.append((topic, value))

    def flush(self):
        self.flushed = True
        if self.flush_error is not None:
            raise self.flush_error


class Context:
    """The stage context surface `produce_alerts` actually uses.

    `attempt_id` and `require_connection()` are new (migration 037 /
    integration ruling 3): the claim/confirm/milestone writes go through the
    ATTEMPT'S OWN borrowed connection now, not through an injected watermark
    object — `attempt_id` is this attempt's OWN identity (the claiming
    attempt, distinct from `unit.payload.promoted_attempt_id`, the registered
    SOURCE attempt the unit declares).
    """

    def __init__(self, unit, parameters, conn=None, attempt_id=99):
        self.unit = unit
        self.parameters = dict(parameters)
        self.provenance = {}
        self.logger = _SilentLogger()
        self.connection = conn
        self.attempt_id = attempt_id

    def parameter(self, name):
        return self.parameters.get(name)

    def require_connection(self):
        if self.connection is None:
            raise RuntimeError("no connection lent to this test context")
        return self.connection

    def record_effect(self, rows_written=0, rows_removed=0, **extra):
        self.provenance["rows_written"] = (
            self.provenance.get("rows_written", 0) + int(rows_written))
        self.provenance.update(extra)


class _SilentLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


PARAMETERS = {
    "kafka/topic": "rapid.internal.alerts.v1",
    "kafka/bootstrap-servers": "b-1:9098",
    "kafka/max-request-bytes": "15728640",
}

#: The claiming attempt's identity in every test below — `context.attempt_id`
#: — distinct from the unit's declared `promoted_attempt_id` component (the
#: registered SOURCE attempt, `SOURCE_ATTEMPT_ID`). Migration 037 keeps these
#: as two different columns (`alert_emissions.attempt_id` vs `.claim_token`)
#: precisely because they can differ; the tests use different values
#: throughout so a test that accidentally conflated them would fail.
CLAIMING_ATTEMPT_ID = 99
SOURCE_ATTEMPT_ID = 6765


def _unit(**overrides):
    """A real alert-production `ProcessingUnit`, exposure=20/sca=7 fixed.

    Built from the TYPED payload (`submission.payloads`) rather than a
    hand-rolled stub with a `.fields` dict: `ProcessingUnit.exposure`/`.sca`
    are properties derived from the payload's declared components now, and
    `AlertProductionPayload` validates its required components at
    construction (rule 11), so a real payload is both simpler and more
    faithful to what `produce_alerts` actually receives than a re-implemented
    stub would be.
    """
    components = {"exposure": 20, "sca": 7,
                  "promoted_attempt_id": SOURCE_ATTEMPT_ID,
                  "release_identity": "rel-1",
                  "difference_image_pid": 1086}
    components.update(overrides)
    return ProcessingUnit(
        payload=payloads.build(JOB_TYPE_ALERT_PRODUCTION, **components))


class SelectionTests(unittest.TestCase):
    """The PLACEHOLDER selection, tested as the placeholder it is."""

    def test_it_takes_the_top_n_by_snr(self):
        sources = [Source(1, 5.0), Source(2, 50.0), Source(3, 20.0)]

        chosen = alert_production.select_candidates(sources, top_n=2)

        self.assertEqual([s.sid for s in chosen], [2, 3])

    def test_an_unknown_snr_sorts_last_rather_than_dropping(self):
        # A missing measurement is not a low one. Dropping on absence would
        # make the placeholder a filter on data completeness too.
        sources = [Source(1, None), Source(2, 1.0)]

        chosen = alert_production.select_candidates(sources, top_n=2)

        self.assertEqual([s.sid for s in chosen], [2, 1])

    def test_the_default_bound_is_the_recorded_placeholder(self):
        self.assertEqual(alert_production.PLACEHOLDER_TOP_N_BY_SNR, 500)

    def test_fewer_candidates_than_the_bound_is_not_padded(self):
        chosen = alert_production.select_candidates([Source(1, 3.0)])

        self.assertEqual(len(chosen), 1)


class TopicGuardTests(unittest.TestCase):
    """Internal topics only — the mission stream is out of scope entirely."""

    def test_the_topic_comes_from_the_parameter_tree(self):
        context = Context(_unit(), PARAMETERS)

        self.assertEqual(alert_production._internal_topic(context),
                         "rapid.internal.alerts.v1")

    def test_a_public_topic_is_refused(self):
        # A parameter edit must not be able to point simulation alerts at a
        # stream consumers trust.
        context = Context(_unit(), dict(PARAMETERS,
                                        **{"kafka/topic": "rapid.alerts.v1"}))

        with self.assertRaises(InputError) as caught:
            alert_production._internal_topic(context)

        self.assertIn("not an internal topic", str(caught.exception))

    def test_a_test_topic_is_allowed(self):
        context = Context(_unit(),
                          dict(PARAMETERS, **{"kafka/topic": "rapid.test.x"}))

        self.assertEqual(alert_production._internal_topic(context),
                         "rapid.test.x")

    def test_a_missing_topic_is_refused_rather_than_defaulted(self):
        context = Context(_unit(), dict(PARAMETERS, **{"kafka/topic": None}))

        with self.assertRaises(InputError):
            alert_production._internal_topic(context)


class UnitFieldTests(unittest.TestCase):
    """The unit is what the manifest says."""

    def test_a_missing_declared_field_fails_naming_it(self):
        # `difference_image_pid` is REQUIRED on an alert-production payload
        # (rule 11) and validated at construction, so it can no longer be
        # deleted off a built unit the way the old open `fields` dict let
        # tests delete a key. `role_resolved_from` is the payload's own
        # genuinely-optional component (see `AlertProductionPayload`'s
        # docstring) and is absent here by simply not passing it — the same
        # "declared but not carried" case `_unit_field` is meant to catch.
        unit = _unit()
        context = Context(unit, PARAMETERS)

        with self.assertRaises(InputError) as caught:
            alert_production._unit_field(context, "role_resolved_from")

        self.assertIn("role_resolved_from", str(caught.exception))


class Provider:
    """The alert data provider, over the CANDIDATE-reading connection only.

    `db` is no longer where the emission CAS writes go (migration 037 /
    integration ruling 3 moved those onto the attempt's own borrowed
    connection — see `Context.require_connection`); this double no longer
    needs one at all, and takes none, so a test that mistakenly wired the
    old `watermark` object here would fail on the missing argument rather
    than silently doing nothing.
    """

    def __init__(self, sources=(), chip_error=None):
        self.sources = list(sources)
        self.chip_error = chip_error

    def iter_sources(self, pid):
        if self.chip_error is not None:
            raise self.chip_error
        return iter(self.sources)


def _run_produce_alerts(context, provider, producer, assemble=None,
                        fail_sids=()):
    """Run `produce_alerts` against doubles, patching the `alerts` package.

    Module-level (not a method) so both `EmissionTests` and
    `WatermarkSeedTests` can drive a real run without duplicating the patch
    list — `produce_alerts` resolves `alerts.cli`/`alerts.produce`/
    `alerts.kafka_producer` through module-level imports, so they are
    patched here rather than injected.
    """
    import alerts.cli
    import alerts.produce
    import alerts.kafka_producer

    def fake_assemble(prov, source):
        if source.sid in fail_sids:
            raise ValueError(f"candidate {source.sid} is unusable")
        return {"sid": source.sid}

    patches = [
        # `db=` is the stage's path (its own connection's borrowing handle);
        # the double accepts and ignores it — the provider under test is
        # the fixture's, and a double that refused the real call shape was
        # exactly how this suite went red in-image while green off-image.
        (alerts.cli, "make_provider", lambda db=None: provider),
        (alerts.produce, "assemble_alert_for_source",
         assemble or fake_assemble),
        (alerts.produce, "load_schema", lambda *a, **k: {"fake": True}),
        (alerts.produce, "serialize_alert",
         lambda alert, schema=None: b"x" * 10),
        (alerts.produce, "publish_alert",
         lambda payload, prod, topic="alerts", flush=False:
             prod.produce(topic, payload)),
        (alerts.kafka_producer, "make_producer",
         lambda *a, **k: producer),
    ]
    saved = [(mod, name, getattr(mod, name, None)) for mod, name, _ in patches]
    for mod, name, value in patches:
        setattr(mod, name, value)
    try:
        alert_production.produce_alerts(context)
    finally:
        for mod, name, value in saved:
            if value is not None:
                setattr(mod, name, value)


class EmissionTests(unittest.TestCase):
    """Ruling 3: claim -> publish/flush -> confirm, and the effect counts.

    `produce_alerts` resolves its collaborators through module-level imports,
    so they are patched here rather than injected — the alternative would be
    widening the stage signature purely for the tests, which the repo's own
    seam discipline argues against. The emission CAS itself is exercised for
    real (against `FakeConn`'s modelled `alert_emissions` table) through the
    real `RAPIDDB.claim_alert_emission` / `confirm_alert_emission` SQL —
    only the connection is a double, never the CAS logic.
    """

    def _run(self, context, provider, producer, assemble=None, fail_sids=()):
        """Run the stage against doubles, patching the alerts package."""
        _run_produce_alerts(context, provider, producer, assemble=assemble,
                            fail_sids=fail_sids)

    def test_a_clean_run_publishes_and_records_the_counts(self):
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0), Source(2, 8.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self._run(context, provider, producer)

        self.assertEqual(len(producer.published), 2)
        self.assertTrue(producer.flushed)
        self.assertEqual(context.provenance["candidates_considered"], 2)
        self.assertEqual(context.provenance["alerts_published"], 2)
        self.assertEqual(context.provenance["emissions_suppressed"], 0)
        self.assertTrue(context.provenance["emission_confirmed"])
        self.assertEqual(context.provenance["alert_topic"],
                         "rapid.internal.alerts.v1")
        # The selection rule names itself as the placeholder it is.
        self.assertIn("PLACEHOLDER", context.provenance["selection_rule"])
        # (a) CONFIRM lands, and (f) the milestone is in the SAME transaction.
        key = (20, 7, "rel-1")
        self.assertEqual(conn.rows[key]["state"], "emitted")
        self.assertEqual(len(conn.milestones), 1)
        self.assertEqual(conn.milestones[0][0], "alert_published")

    def test_an_already_emitted_unit_publishes_nothing(self):
        # "Emission is once per logical unit per release" — a replay is
        # silent, and the suppression is one of the four effect counts.
        conn = FakeConn(rows={(20, 7, "rel-1"): {"state": "emitted",
                                                  "claim_token": None}})
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self._run(context, provider, producer)

        self.assertEqual(producer.published, [])
        self.assertEqual(context.provenance["emissions_suppressed"], 1)
        self.assertEqual(context.provenance["alerts_published"], 0)

    def test_a_candidate_failure_drops_only_that_candidate(self):
        # Gate 3: candidate failures never fail the attempt.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0), Source(2, 8.0),
                                     Source(3, 7.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self._run(context, provider, producer, fail_sids={2})

        self.assertEqual(len(producer.published), 2)
        self.assertEqual(context.provenance["alerts_published"], 2)
        self.assertEqual(context.provenance["candidates_dropped"], 1)
        self.assertEqual(context.provenance["dropped_by_reason"],
                         {"ValueError": 1})
        # The per-candidate disposition names which one and why.
        self.assertEqual(
            [d["sid"] for d in context.provenance["drop_dispositions"]], [2])

    def test_a_chip_level_failure_fails_the_attempt(self):
        # The one case that does: the provider cannot read the image at all.
        conn = FakeConn()
        provider = Provider(chip_error=OSError("no such image"))
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        with self.assertRaises(RuntimeError):
            self._run(context, provider, producer)

        # (b) claim-then-crash-before-publish: the claim is intact, untouched.
        self.assertEqual(conn.rows[(20, 7, "rel-1")]["state"], "claimed")

    def test_a_delivery_failure_raises_loudly_and_the_claim_survives_intact(self):
        # The 2026-08-04 Q7 finding: a run that reports published alerts
        # while publishing nothing is worse than one that crashes.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer(flush_error=RuntimeError("1 of 1 alert failed"))
        context = Context(_unit(), PARAMETERS, conn=conn)

        with self.assertRaises(RuntimeError):
            self._run(context, provider, producer)

        # (b) publish failure is chip-level: no confirm ran, claim intact.
        key = (20, 7, "rel-1")
        self.assertEqual(conn.rows[key]["state"], "claimed")
        self.assertEqual(conn.rows[key]["claim_token"],
                         str(CLAIMING_ATTEMPT_ID))
        self.assertEqual(conn.milestones, [])

    def test_an_unclaimable_emission_refuses_to_publish(self):
        # A claim that could not be RECORDED must not publish: an emission
        # that is not recorded can be emitted again.
        conn = FakeConn(failure=67)
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        with self.assertRaises(RuntimeError):
            self._run(context, provider, producer)

        self.assertEqual(producer.published, [])

    def test_a_stale_claim_from_a_terminal_attempt_is_taken_over(self):
        # (b) continued: a crashed claimant's stale claim is retaken by a
        # later attempt, per the CAS's own staleness-OR-terminal-prior arm.
        conn = FakeConn(
            rows={(20, 7, "rel-1"): {"state": "claimed", "claim_token": "1"}},
            terminal_attempts={1})
        conn.stale_keys.add((20, 7, "rel-1"))
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self._run(context, provider, producer)

        self.assertEqual(len(producer.published), 1)
        self.assertEqual(context.provenance["emissions_suppressed"], 0)
        self.assertTrue(context.provenance["emission_confirmed"])

    def test_confirm_nulls_the_claim_fields(self):
        # (d) CHECK-shape compliance: migration 037's alert_emissions_claim_
        # shape_ck forbids a non-'claimed' row carrying claim fields, so the
        # CONFIRM statement text itself must NULL both — asserted against the
        # statement FakeConn actually received, not just the modelled result.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self._run(context, provider, producer)

        confirm_statements = [
            statement for statement, _ in conn.statements
            if "state = 'emitted'" in statement.lower()]
        self.assertEqual(len(confirm_statements), 1)
        lowered = " ".join(confirm_statements[0].lower().split())
        self.assertIn("claim_token = null", lowered)
        self.assertIn("claimed_at = null", lowered)

    def test_a_takeover_between_publish_and_confirm_is_a_recorded_no_op(self):
        # (c) takeover: publish is gated on RETURNING this attempt's own
        # token (already covered above); this is the CONFIRM-side half — a
        # foreign token at confirm time is a no-op, never a failure, and
        # never re-publishes on this attempt's behalf.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        # Claim as usual, then simulate another attempt taking the claim over
        # before this attempt's confirm runs.
        original_confirm = conn._confirm

        def confirm_after_takeover(params):
            key = (20, 7, "rel-1")
            conn.rows[key]["claim_token"] = "12345"  # a different claimant
            return original_confirm(params)

        conn._confirm = confirm_after_takeover

        self._run(context, provider, producer)

        # The alert still went out (publish already happened) but this
        # attempt's own confirmation did not land.
        self.assertEqual(len(producer.published), 1)
        self.assertFalse(context.provenance["emission_confirmed"])
        self.assertEqual(conn.rows[(20, 7, "rel-1")]["claim_token"], "12345")

    def test_duplicate_tolerant_retry_republishes_an_unconfirmed_claim(self):
        # (e) duplicate-tolerance: a retry finding its OWN unconfirmed claim
        # (same claim_token — this attempt crashed after claiming but before
        # confirming, and is now retried under the SAME attempt identity)
        # republishes rather than suppressing.
        conn = FakeConn(rows={(20, 7, "rel-1"):
                              {"state": "claimed",
                               "claim_token": str(CLAIMING_ATTEMPT_ID)}})
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self._run(context, provider, producer)

        self.assertEqual(len(producer.published), 1)
        self.assertEqual(context.provenance["emissions_suppressed"], 0)


class WatermarkSeedTests(unittest.TestCase):
    """(g) seed rows carry `watermark_seed`, never the live claim state."""

    def test_seed_alert_emission_watermark_writes_the_seed_state(self):
        self.assertIn(
            "'watermark_seed'",
            _method_source(RAPIDDB.seed_alert_emission_watermark))

    def test_the_claim_and_confirm_never_write_watermark_seed(self):
        # The live CAS path must never be able to (re)mint a seed row — only
        # `seed_alert_emission_watermark` does, and only at initialization.
        # Checked against a real CAS run's tracked statements (the SQL that
        # actually executes), not the method source — the docstrings above
        # legitimately discuss `watermark_seed` in prose (explaining why the
        # CAS's WHERE clause never matches one), which a source-text search
        # cannot tell apart from the executable statement.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        _run_produce_alerts(context, provider, producer)

        executed = " ".join(
            statement.lower() for statement, _ in conn.statements)
        self.assertNotIn("watermark_seed", executed)


def _method_source(method):
    import inspect
    return inspect.getsource(method)


if __name__ == "__main__":
    unittest.main()
