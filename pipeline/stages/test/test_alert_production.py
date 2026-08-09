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

from pipeline.runtime.errors import InputError            # noqa: E402
from pipeline.stages import alert_production              # noqa: E402


class Source:
    """A candidate, with only what the selection reads."""

    def __init__(self, sid, snr=None):
        self.sid = sid
        self.snr = snr


class Watermark:
    """The emission watermark, enforcing its real primary key.

    `record_alert_emission` is `INSERT ... ON CONFLICT DO NOTHING` and returns
    whether THIS caller claimed the row. Modelling that faithfully is what
    makes the suppression path testable: a double that always returned True
    could never show the second emitter staying silent.
    """

    def __init__(self, failure=0, already=()):
        self.exit_code = 0
        self.failure = failure
        self.emitted = set(already)
        self.published = {}
        self.cur = self
        self.conn = self
        self.updates = []

    def record_alert_emission(self, exposure_id, sca, release_identity,
                              attempt_id, pid=None, alerts_published=0):
        self.exit_code = self.failure
        if self.failure:
            return None
        key = (exposure_id, sca, release_identity)
        if key in self.emitted:
            return False
        self.emitted.add(key)
        return True

    # The `_update_emission_count` path writes through cur/conn directly.
    def execute(self, statement, params=None):
        self.updates.append((statement, params))

    def commit(self):
        pass


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


class Unit:
    def __init__(self, exposure, sca, fields):
        self.exposure = exposure
        self.sca = sca
        self.fields = fields
        self.facts = None


class Context:
    """The stage context surface `produce_alerts` actually uses."""

    def __init__(self, unit, parameters):
        self.unit = unit
        self.parameters = dict(parameters)
        self.provenance = {}
        self.logger = _SilentLogger()

    def parameter(self, name):
        return self.parameters.get(name)

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


def _unit(**overrides):
    fields = {"attempt_id": 6765, "release_identity": "rel-1",
              "difference_image_pid": 1086,
              "job_type": "alert-production"}
    fields.update(overrides)
    return Unit(exposure=20, sca=7, fields=fields)


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
        unit = _unit()
        del unit.fields["difference_image_pid"]
        context = Context(unit, PARAMETERS)

        with self.assertRaises(InputError) as caught:
            alert_production._unit_field(context, "difference_image_pid")

        self.assertIn("difference_image_pid", str(caught.exception))


class Provider:
    """The alert data provider, over a watermark handle."""

    def __init__(self, db, sources=(), chip_error=None):
        self.db = db
        self.sources = list(sources)
        self.chip_error = chip_error

    def iter_sources(self, pid):
        if self.chip_error is not None:
            raise self.chip_error
        return iter(self.sources)


class EmissionTests(unittest.TestCase):
    """Gates 3 and 4: emission control and the effect counts.

    `produce_alerts` resolves its collaborators through module-level imports,
    so they are patched here rather than injected — the alternative would be
    widening the stage signature purely for the tests, which the repo's own
    seam discipline argues against.
    """

    def _run(self, context, provider, producer, assemble=None, fail_sids=()):
        """Run the stage against doubles, patching the alerts package."""
        import alerts.cli
        import alerts.produce
        import alerts.kafka_producer

        def fake_assemble(prov, source):
            if source.sid in fail_sids:
                raise ValueError(f"candidate {source.sid} is unusable")
            return {"sid": source.sid}

        patches = [
            (alerts.cli, "make_provider", lambda: provider),
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

    def test_a_clean_run_publishes_and_records_the_counts(self):
        watermark = Watermark()
        provider = Provider(watermark, sources=[Source(1, 9.0), Source(2, 8.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS)

        self._run(context, provider, producer)

        self.assertEqual(len(producer.published), 2)
        self.assertTrue(producer.flushed)
        self.assertEqual(context.provenance["candidates_considered"], 2)
        self.assertEqual(context.provenance["alerts_published"], 2)
        self.assertEqual(context.provenance["emissions_suppressed"], 0)
        self.assertEqual(context.provenance["alert_topic"],
                         "rapid.internal.alerts.v1")
        # The selection rule names itself as the placeholder it is.
        self.assertIn("PLACEHOLDER", context.provenance["selection_rule"])

    def test_an_already_emitted_unit_publishes_nothing(self):
        # "Emission is once per logical unit per release" — a replay is
        # silent, and the suppression is one of the four effect counts.
        watermark = Watermark(already={(20, 7, "rel-1")})
        provider = Provider(watermark, sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS)

        self._run(context, provider, producer)

        self.assertEqual(producer.published, [])
        self.assertEqual(context.provenance["emissions_suppressed"], 1)
        self.assertEqual(context.provenance["alerts_published"], 0)

    def test_a_candidate_failure_drops_only_that_candidate(self):
        # Gate 3: candidate failures never fail the attempt.
        watermark = Watermark()
        provider = Provider(watermark,
                            sources=[Source(1, 9.0), Source(2, 8.0),
                                     Source(3, 7.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS)

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
        watermark = Watermark()
        provider = Provider(watermark, chip_error=OSError("no such image"))
        producer = Producer()
        context = Context(_unit(), PARAMETERS)

        with self.assertRaises(RuntimeError):
            self._run(context, provider, producer)

    def test_a_delivery_failure_raises_loudly(self):
        # The 2026-08-04 Q7 finding: a run that reports published alerts
        # while publishing nothing is worse than one that crashes.
        watermark = Watermark()
        provider = Provider(watermark, sources=[Source(1, 9.0)])
        producer = Producer(flush_error=RuntimeError("1 of 1 alert failed"))
        context = Context(_unit(), PARAMETERS)

        with self.assertRaises(RuntimeError):
            self._run(context, provider, producer)

    def test_an_unclaimable_watermark_refuses_to_publish(self):
        # A claim that could not be RECORDED must not publish: an emission
        # that is not recorded can be emitted again.
        watermark = Watermark(failure=67)
        provider = Provider(watermark, sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS)

        with self.assertRaises(RuntimeError):
            self._run(context, provider, producer)

        self.assertEqual(producer.published, [])


if __name__ == "__main__":
    unittest.main()
