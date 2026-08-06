"""The ported product-registration bodies.

Two things these assert that the consumer suite cannot: that the legacy call
SEQUENCE is preserved (insert, then finalize so vbest points at this version,
then the catalogues/measurements keyed by what the insert returned), and that
every fact comes from the attempt's own terminal record — with an absent fact
raising `MissingRecordFact` rather than being defaulted.

**EVERY RECORD HERE IS BUILT BY `termination.build_terminal_record`** (round-3
finding #2). These tests used to hand-construct record bodies as dict literals,
and every one of them was a body production could not author: they carried
`field`, `fid`, `hp6`, `hp9`, `rid`, `sca`, `sky_position` and six ZOGY metrics
in `science_provenance`, when the entrypoint seeded provenance with exactly
three keys — the release digest and two tessellation facts — and no stage
recorded any of the rest under those names. So the suite passed green against a
registrar that could not have registered a single real attempt: the first live
candidate would raise `MissingRecordFact`, stay a candidate, and fail again on
every pass thereafter.

A hand-written dict tests the registrar against a record shape someone
imagined. Going through `build_terminal_record` tests it against the shape the
application actually writes — and the provenance those records carry is built
here by the same `unit_provenance` and stage `record()` calls the pipeline
makes, so a divergence between what a stage records and what the registrar
reads fails HERE rather than in production. That is the property the old suite
lacked, and it is why the fixture below is longer than the literals it
replaces: the cost of the fixture is exactly the coupling that was missing.
"""

import datetime
import importlib.util
import json
import unittest

from pipeline.registration import facts as registration_facts
from pipeline.registration import products
from pipeline.runtime import termination
from pipeline.runtime.boundaries import InMemoryObjectStore, checksum
from pipeline.stages.context import StageContext
from submission.manifest import ProcessingUnit, UnitFacts

# healpy is a container dependency (`docker/Dockerfile_ubuntu*: pip install
# healpy`) and is not in requirements.txt, so it is present in the image and
# usually absent on a laptop. The derivation tests below assert against healpy
# ITSELF — the whole point is that our call matches the legacy convention
# argument for argument — so they are skipped rather than stubbed where it is
# missing. Stubbing would let them pass while proving nothing, which is the
# failure mode this whole round is about.
#
# The RECORD fixtures do not skip: `unit_provenance` yields everything except
# hp6/hp9 without healpy, and `_HAS_HEALPY` gates only the two assertions that
# need the indices to exist. So the bulk of the suite — the call sequences, the
# metric names, the disposition and job-type gates — runs everywhere.
HAS_HEALPY = importlib.util.find_spec("healpy") is not None
_needs_healpy = unittest.skipUnless(
    HAS_HEALPY, "healpy is a container dependency and is not installed here")

#: The science image's centre and corners, as a manifest carries them. The
#: centre drives the healpix derivation, so these are real-ish sky values
#: rather than 1.0/2.0 placeholders — an index derived from a nonsense position
#: is still an integer and would hide a swapped-argument bug.
SKY_POSITION = {
    "ra0": 150.1163, "dec0": 2.2008,
    "ra1": 150.0163, "dec1": 2.1008,
    "ra2": 150.2163, "dec2": 2.1008,
    "ra3": 150.2163, "dec3": 2.3008,
    "ra4": 150.0163, "dec4": 2.3008,
}


class FakeDB:
    """Records the call sequence and hands back the ids the bodies chain on."""

    def __init__(self, exit_code=0):
        self.calls = []
        self.exit_code = exit_code
        self.rfid = 77
        self.pid = 900
        self.version = 3
        self.rfcatid = 11
        self.svid = 22

    def __getattr__(self, name):
        if name.startswith(("add_", "update_", "register_")):
            def record(*args):
                self.calls.append((name, args))
            return record
        raise AttributeError(name)


class _Ownership:
    """The identity fields `build_terminal_record` reads off an ownership."""

    def __init__(self, attempt_id, run_id="run-1"):
        self.attempt_id = attempt_id
        self.run_id = run_id
        self.logical_job_id = f"{run_id}/90000"
        self.scheduler_job_id = "batch-abc"
        self.attempt_index = 1


class _JobEnv:
    """The submission-time binding half of a record."""

    queue_name = "rapid-prompt"
    batch_id = "run-1"
    manifest_uri = "s3://m/manifest.json"
    manifest_checksum = "manifest-sha"
    array_index = 0


def _context(unit, job_type):
    """A `StageContext` with the identity a real one has, and no I/O.

    Constructed rather than faked so `unit_provenance` and the stage `record`
    calls below run against the real object, and so `product_prefix()` — which
    the published URIs are built from — produces the production key shape.
    """
    return StageContext(
        workdir=None, unit=unit, job_type=job_type, science={}, parameters={},
        logger=_QuietLogger(), run_id="run-1", attempt_id=1)


class _QuietLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _reference_unit():
    return ProcessingUnit(
        exposure=90000, sca=7,
        facts=UnitFacts(rid=42, fid=1, field=4678636, rtid=4678636,
                        expid=90000, sky_position=dict(SKY_POSITION),
                        reference_image_infobits=0))


def _science_unit():
    return ProcessingUnit(
        exposure=90000, sca=7,
        facts=UnitFacts(rid=42, fid=1, field=4678636, rtid=4678636,
                        expid=90000, sky_position=dict(SKY_POSITION),
                        reference_image_id=77, reference_image_infobits=0,
                        infobits=0))


def _build(context, published, attempt_id, job_type):
    """Serialize a context's accumulations exactly as the entrypoint does.

    `science_provenance=dict(context.provenance)` and
    `products=dict(context.published_products)` are the entrypoint's own two
    lines (`pipeline/entrypoints/job.py`), and `job_type` is threaded the way
    the FixC round threaded it. Nothing here reaches around the real path.
    """
    for name, entry in published.items():
        context.publish(name, uri=entry["uri"], checksum=entry["checksum"])

    return termination.build_terminal_record(
        _Ownership(attempt_id), _JobEnv(), outcome="success",
        product_disposition="published", intended_exit=0,
        started_at=datetime.datetime(2026, 8, 6, tzinfo=datetime.timezone.utc),
        ended_at=datetime.datetime(2026, 8, 6, 1, tzinfo=datetime.timezone.utc),
        config_digest="cfg-sha", snapshot_key_value="snap/key.json",
        bundle={"key": "b/key.tar.gz", "checksum": "bundle-sha"},
        stages=[], science_provenance=dict(context.provenance),
        products=dict(context.published_products), job_type=job_type)


#: What `healpix_indices` returns for `SKY_POSITION`'s centre at levels 6 and
#: 9. Used ONLY to stand in for the derivation where healpy is not installed,
#: so the registration-body tests below still exercise a complete record. The
#: derivation itself is never asserted against these — `HealpixDerivationTests`
#: checks it against healpy directly and skips without it, because a constant
#: checked against itself would prove nothing.
_HP6_FALLBACK, _HP9_FALLBACK = 21132, 1352509


def _seed_identity(context, unit, job_type):
    """The entrypoint's seeding line, plus the no-healpy stand-in.

    `context.record(**unit_provenance(unit, job_type))` is exactly what
    `pipeline/entrypoints/job.py` does. Where healpy is missing the derivation
    correctly yields nothing — an absent index is a finding, not a zero — so
    the two keys are supplied here to keep the BODY tests testing the bodies
    rather than re-testing the skip.
    """
    provenance = registration_facts.unit_provenance(unit, job_type)
    if "hp6" not in provenance and getattr(unit.facts, "sky_position", None):
        provenance["hp6"] = _HP6_FALLBACK
        provenance["hp9"] = _HP9_FALLBACK
    context.record(**provenance)


def reference_record(with_psfcat=False, **overrides):
    """A reference-image record, authored the way production authors one."""
    unit = _reference_unit()
    context = _context(unit, "reference-image")
    _seed_identity(context, unit, "reference-image")

    published = {
        "reference_image": {"uri": "s3://p/ref.fits", "checksum": "ref-sha"},
        "reference_sexcat": {"uri": "s3://p/sex.txt", "checksum": "sex-sha"},
    }
    if with_psfcat:
        published["reference_psfcat"] = {"uri": "s3://p/psf.txt",
                                         "checksum": "psf-sha"}

    record = _build(context, published, 1, "reference-image")
    record.update(overrides)
    return record


def difference_record(**overrides):
    """A science record carrying what the science stages actually record."""
    unit = _science_unit()
    context = _context(unit, "science")
    _seed_identity(context, unit, "science")

    # The ZOGY measurements, recorded under the names the stages now use.
    # `scalefacref` is the RECIPROCAL of the tool's `scalefac`, which is what
    # `gain_match` records and what the legacy chain registered — see the
    # direction test below.
    scalefac = 1.0204081632653061
    context.record(scalefacref=1. / scalefac, gainmatch_scalefac=scalefac,
                   dxrmsfin=0.01, dyrmsfin=0.02,
                   dxmedianfin=0.001, dymedianfin=0.002,
                   nsexcatsources=1234, diffimage_infobits=0)

    published = {
        "difference_image": {"uri": "s3://p/diff.fits", "checksum": "diff-sha"},
    }
    record = _build(context, published, 2, "science")
    record.update(overrides)
    return record


class RecordsProductionCanAuthorTests(unittest.TestCase):
    """The fixture's own contract: these records are production-shaped.

    If the provenance path ever stops carrying one of these, the failure
    surfaces here — naming the missing fact — rather than as a puzzling
    `MissingRecordFact` from a body three tests down.
    """

    def test_the_reference_record_carries_every_fact_its_body_needs(self):
        # These names are the LIVE `refimages` columns (schema probe,
        # 2026-08-06): rfid, field, hp6, hp9, fid, ppid, ... — so the registrar
        # is speaking the operations schema's own vocabulary, not inventing one.
        science = reference_record()["science_provenance"]
        required = ["field", "fid"] + (["hp6", "hp9"] if HAS_HEALPY else [])
        for name in required:
            self.assertIn(name, science, f"{name} is not in science_provenance")

    def test_the_difference_record_carries_every_fact_its_body_needs(self):
        # And these are the live `diffimmeta` columns: pid, nsexcatsources,
        # scalefacref, field, hp6, hp9, fid, sca, dxrmsfin, dyrmsfin,
        # dxmedianfin, dymedianfin — exactly the required set below.
        science = difference_record()["science_provenance"]
        required = ["rid", "reference_image_id", "field", "fid", "sca",
                    "sky_position", "nsexcatsources", "scalefacref",
                    "dxrmsfin", "dyrmsfin", "dxmedianfin", "dymedianfin"]
        required += ["hp6", "hp9"] if HAS_HEALPY else []
        for name in required:
            self.assertIn(name, science, f"{name} is not in science_provenance")

    def test_sca_comes_from_the_unit_not_from_the_manifest_facts(self):
        # `sca` is half the unit's identity (`exposure/sca`) and is NOT a
        # UnitFacts field — it was the one required registrar fact with nowhere
        # at all to come from, because nothing looked at the unit itself.
        self.assertEqual(7, difference_record()["science_provenance"]["sca"])

    @_needs_healpy
    def test_the_healpix_indices_are_derived_not_carried(self):
        # Nothing in the manifest, in a stage, or in gathering carries hp6/hp9.
        # They exist only because `unit_provenance` derives them — and they are
        # NOT NULL integer columns on the live `refimages` and `diffimmeta`
        # tables, so omitting them is not an option either.
        self.assertIsNone(_science_unit().facts.to_dict().get("hp6"))
        science = difference_record()["science_provenance"]
        expected6, expected9 = registration_facts.healpix_indices(
            SKY_POSITION["ra0"], SKY_POSITION["dec0"])
        self.assertEqual(expected6, science["hp6"])
        self.assertEqual(expected9, science["hp9"])

    @_needs_healpy
    def test_the_fixture_fallback_matches_the_real_derivation(self):
        # Keeps `_HP6_FALLBACK`/`_HP9_FALLBACK` honest. They stand in for the
        # derivation off-image; if they ever stopped matching it, the body
        # tests would be exercising indices production never produces.
        self.assertEqual(
            (_HP6_FALLBACK, _HP9_FALLBACK),
            registration_facts.healpix_indices(SKY_POSITION["ra0"],
                                               SKY_POSITION["dec0"]))

    def test_the_record_is_json_serializable(self):
        # `write_terminal_record` serializes with `json.dumps`; a numpy integer
        # out of `ang2pix` would raise there rather than here, at the one point
        # in the protocol that has nowhere to record an outcome.
        json.dumps(difference_record())
        json.dumps(reference_record())

    def test_the_job_type_and_ppid_ride_the_record(self):
        record = difference_record()
        self.assertEqual("science", record["job_type"])
        self.assertEqual(15, record["ppid"])
        self.assertEqual(12, reference_record()["ppid"])


@_needs_healpy
class HealpixDerivationTests(unittest.TestCase):
    """The derivation is a transcription, and its convention is load-bearing."""

    def test_the_levels_match_the_legacy_loader(self):
        # `loadPSFCatIntoDBSourcesTable.py:26-30`. The hp6/hp9 columns are
        # already populated at these levels; an index computed at another
        # nside is a valid integer naming the wrong patch of sky.
        self.assertEqual(2 ** 6, registration_facts.NSIDE6)
        self.assertEqual(2 ** 9, registration_facts.NSIDE9)

    def test_the_indices_match_a_direct_nested_lonlat_call(self):
        hp = __import__("healpy")
        ra, dec = SKY_POSITION["ra0"], SKY_POSITION["dec0"]

        hp6, hp9 = registration_facts.healpix_indices(ra, dec)

        self.assertEqual(
            int(hp.ang2pix(2 ** 6, ra, dec, nest=True, lonlat=True)), hp6)
        self.assertEqual(
            int(hp.ang2pix(2 ** 9, ra, dec, nest=True, lonlat=True)), hp9)

    def test_nest_and_lonlat_are_not_incidental(self):
        # Guards the two ways this derivation could be silently wrong: the ring
        # scheme numbers the same sphere differently, and without lonlat the
        # angles are read as colatitude/longitude in RADIANS — a position that
        # lands in a different hemisphere, not merely a different pixel.
        hp = __import__("healpy")
        ra, dec = SKY_POSITION["ra0"], SKY_POSITION["dec0"]
        hp6, _hp9 = registration_facts.healpix_indices(ra, dec)

        self.assertNotEqual(
            int(hp.ang2pix(2 ** 6, ra, dec, nest=False, lonlat=True)), hp6)
        self.assertNotEqual(
            int(hp.ang2pix(2 ** 6, ra, dec, nest=True, lonlat=False)), hp6)

    def test_an_absent_centre_derives_nothing_rather_than_zero(self):
        # Zero is a real healpix pixel. Deriving it from an absent position
        # would register the product against a patch of sky it has nothing to
        # do with, which is worse than the registrar naming a missing fact.
        self.assertEqual((None, None), registration_facts.healpix_indices(
            None, 2.2))


class ScaleFactorDirectionTests(unittest.TestCase):
    """`scalefacref` is the reciprocal, and the direction is not cosmetic."""

    def test_the_recorded_scalefacref_is_the_reciprocal_of_scalefac(self):
        # EVIDENCE. `gainMatchScienceAndReferenceImages` returns `scalefac`;
        # the monolith computed `scalefacref = 1./scalefac`
        # (awsBatchSubmitJobs_runSingleSciencePipeline.py:1076) and wrote THAT
        # value into `product_config['ZOGY']['scalefacref']` at line 1802 —
        # which is the exact key the legacy registration body read
        # (registerCompletedJobsInDB.py:688) and passed to
        # `register_diffimmeta`. The DiffImMeta column is therefore already
        # populated with inverted values, and recording the un-inverted
        # `scalefac` would put numbers into it that disagree with every row
        # already there. Both are plausible positive floats, so nothing would
        # ever have noticed.
        science = difference_record()["science_provenance"]

        self.assertAlmostEqual(1. / science["gainmatch_scalefac"],
                               science["scalefacref"])
        self.assertNotAlmostEqual(science["gainmatch_scalefac"],
                                  science["scalefacref"])

    def test_the_reciprocal_is_what_reaches_register_diffimmeta(self):
        dbh = FakeDB()
        record = difference_record()

        products.register_difference_image(
            dbh, record, record["science_provenance"], 2)

        call = [args for name, args in dbh.calls
                if name == "register_diffimmeta"][0]
        # register_diffimmeta(pid, fid, sca, field, hp6, hp9, nsexcatsources,
        #                     scalefacref, dxrmsfin, ...)
        self.assertAlmostEqual(
            record["science_provenance"]["scalefacref"], call[7])


class ReferenceImageBodyTests(unittest.TestCase):
    def test_the_legacy_call_sequence_is_preserved(self):
        # add_refimage inserts and returns the rfid; update_refimage then
        # finalizes so vbest points at this version; the catalogues follow,
        # keyed by that rfid. Splitting insert from finalize was the legacy
        # design — the insert cannot know it is best until it exists.
        dbh = FakeDB()
        record = reference_record()

        result = products.register_reference_image(
            dbh, record, record["science_provenance"], 1)

        self.assertEqual([name for name, _ in dbh.calls],
                         ["add_refimage", "update_refimage",
                          "register_refimcatalog"])
        self.assertEqual(result["rfid"], 77)

    def test_the_uri_and_checksum_come_from_the_published_product(self):
        dbh = FakeDB()
        record = reference_record()

        products.register_reference_image(
            dbh, record, record["science_provenance"], 1)

        _name, args = dbh.calls[0]
        self.assertIn("s3://p/ref.fits", args)
        self.assertIn("ref-sha", args)

    def test_the_derived_healpix_indices_reach_add_refimage(self):
        dbh = FakeDB()
        record = reference_record()
        science = record["science_provenance"]

        products.register_reference_image(dbh, record, science, 1)

        _name, args = dbh.calls[0]
        # add_refimage(ppid, field, fid, hp6, hp9, infobits, status, ...)
        self.assertEqual(science["hp6"], args[3])
        self.assertEqual(science["hp9"], args[4])

    def test_an_unpublished_catalogue_is_skipped_not_invented(self):
        # The legacy body's `if photutils_..._uploaded_to_bucket:` guard. The
        # published list is the authority on what exists.
        dbh = FakeDB()
        record = reference_record()

        result = products.register_reference_image(
            dbh, record, record["science_provenance"], 1)

        self.assertEqual([c["cattype"] for c in result["catalogs"]],
                         [products.CATTYPE_SEXTRACTOR])

    def test_both_catalogues_register_when_both_were_published(self):
        record = reference_record(with_psfcat=True)
        dbh = FakeDB()

        result = products.register_reference_image(
            dbh, record, record["science_provenance"], 1)

        self.assertEqual([c["cattype"] for c in result["catalogs"]],
                         [products.CATTYPE_SEXTRACTOR,
                          products.CATTYPE_PHOTUTILS])

    def test_a_missing_fact_is_a_finding_not_a_default(self):
        # The whole point of the re-keying: the legacy bodies read these from a
        # mutable per-job .ini, and defaulting one silently registers a product
        # under the wrong identity.
        record = reference_record()
        del record["science_provenance"]["hp6"]

        with self.assertRaises(products.MissingRecordFact) as caught:
            products.register_reference_image(
                FakeDB(), record, record["science_provenance"], 1)

        self.assertEqual(caught.exception.field, "hp6")

    def test_a_missing_product_is_a_finding(self):
        record = reference_record(products=[])

        with self.assertRaises(products.MissingRecordFact):
            products.register_reference_image(
                FakeDB(), record, record["science_provenance"], 1)

    def test_a_database_failure_raises_rather_than_exiting_the_process(self):
        # The legacy bodies called exit() from inside the registration, which
        # took the whole pass down and left every later job unregistered with
        # no account of how far it got.
        dbh = FakeDB(exit_code=64)
        record = reference_record()

        with self.assertRaises(products.RegistrationFailed):
            products.register_reference_image(
                dbh, record, record["science_provenance"], 1)


class DifferenceImageBodyTests(unittest.TestCase):
    def test_the_legacy_call_sequence_is_preserved(self):
        dbh = FakeDB()
        record = difference_record()

        result = products.register_difference_image(
            dbh, record, record["science_provenance"], 2)

        self.assertEqual([name for name, _ in dbh.calls],
                         ["add_diffimage", "update_diffimage",
                          "register_diffimmeta"])
        self.assertEqual(result["pid"], 900)

    def test_all_five_corners_reach_add_diffimage(self):
        dbh = FakeDB()
        record = difference_record()

        products.register_difference_image(
            dbh, record, record["science_provenance"], 2)

        _name, args = dbh.calls[0]
        for key in registration_facts.SKY_POSITION_KEYS:
            self.assertIn(SKY_POSITION[key], args)

    def test_the_zogy_metrics_reach_register_diffimmeta_in_order(self):
        # The names the stages record under ARE the names the schema takes;
        # this asserts the whole tuple lands in the legacy argument order.
        dbh = FakeDB()
        record = difference_record()
        science = record["science_provenance"]

        products.register_difference_image(dbh, record, science, 2)

        call = [args for name, args in dbh.calls
                if name == "register_diffimmeta"][0]
        # (pid, fid, sca, field, hp6, hp9, nsexcatsources, scalefacref,
        #  dxrmsfin, dyrmsfin, dxmedianfin, dymedianfin)
        self.assertEqual(science["fid"], call[1])
        self.assertEqual(science["sca"], call[2])
        self.assertEqual(science["field"], call[3])
        self.assertEqual(science["hp6"], call[4])
        self.assertEqual(science["hp9"], call[5])
        self.assertEqual(science["nsexcatsources"], call[6])
        self.assertEqual(science["dxrmsfin"], call[8])
        self.assertEqual(science["dyrmsfin"], call[9])
        self.assertEqual(science["dxmedianfin"], call[10])
        self.assertEqual(science["dymedianfin"], call[11])

    def test_a_partial_sky_position_refuses_to_register(self):
        # A difference image registered with a partial footprint is one that
        # later spatial queries silently mis-match.
        record = difference_record()
        del record["science_provenance"]["sky_position"]["ra3"]

        with self.assertRaises(products.MissingRecordFact):
            products.register_difference_image(
                FakeDB(), record, record["science_provenance"], 2)

    def test_the_zogy_measurements_are_required(self):
        record = difference_record()
        del record["science_provenance"]["scalefacref"]

        with self.assertRaises(products.MissingRecordFact) as caught:
            products.register_difference_image(
                FakeDB(), record, record["science_provenance"], 2)

        self.assertEqual(caught.exception.field, "scalefacref")


class RecordReadingTests(unittest.TestCase):
    """Registration reads the record and verifies its bytes."""

    def setUp(self):
        self.store = InMemoryObjectStore()
        self.body = reference_record()
        self.raw = json.dumps(self.body).encode("utf-8")
        self.key = "attempts/records/run-1/90000_1/attempt-1/seq-0000.json"
        self.store.put_if_absent(self.key, self.raw)

    def _row(self, **overrides):
        row = {"attempt_id": 1, "terminal_record_key": self.key,
               "terminal_record_checksum": checksum(self.raw)}
        row.update(overrides)
        return row

    def test_a_valid_record_is_returned(self):
        self.assertEqual(
            self.body, products.read_record(self.store, self._row()))

    def test_bytes_that_do_not_match_the_cited_checksum_are_refused(self):
        # An object silently replaced at a known key would otherwise be
        # registered as though the attempt had produced it.
        with self.assertRaises(products.RegistrationFailed):
            products.read_record(
                self.store, self._row(terminal_record_checksum="wrong"))

    def test_a_record_for_another_attempt_is_refused(self):
        with self.assertRaises(products.RegistrationFailed):
            products.read_record(self.store, self._row(attempt_id=999))

    def test_a_row_with_no_record_key_is_a_finding(self):
        # NULL here means the reconciler has not materialized the citation
        # yet (review finding #14); the attempt is not ready to register.
        with self.assertRaises(products.MissingRecordFact):
            products.read_record(
                self.store, self._row(terminal_record_key=None))

    def test_a_record_written_through_the_protocol_round_trips(self):
        # The full path: build, WRITE through `write_terminal_record`, then
        # read it back the way the registrar does. This is what proves the
        # record survives its own serialization — a value a stage recorded that
        # `json.dumps` cannot encode fails here, at the boundary, rather than
        # inside the termination protocol where nothing can record it.
        store = InMemoryObjectStore()
        record = difference_record()
        key = termination.terminal_record_key("attempts", "run-1", "90000", 2)

        written = termination.write_terminal_record(store, key, record)
        body = products.read_record(store, {
            "attempt_id": 2, "terminal_record_key": key,
            "terminal_record_checksum": written["checksum"]})

        self.assertEqual(record["science_provenance"],
                         body["science_provenance"])


class RegistrableJobTypeTests(unittest.TestCase):
    """Unregistrable job types must not become permanent candidates."""

    def test_reference_and_science_are_registrable(self):
        self.assertTrue(products.is_registrable(reference_record()))
        self.assertTrue(products.is_registrable(difference_record()))

    def test_registration_and_post_process_are_not(self):
        # THE SELF-POISONING LOOP (round-3 finding #7). A registration job
        # closed `(success, published)`, which is the sole pair `decide`
        # registers on, so a successful registration pass became an
        # unregistrable candidate on the next pass — and the refusal counted as
        # a failure, so the watermark never advanced and it stayed a candidate
        # forever.
        self.assertFalse(
            products.is_registrable(reference_record(job_type="registration")))
        self.assertFalse(
            products.is_registrable(reference_record(job_type="post-process")))

    def test_an_unregistrable_candidate_is_a_no_op_not_a_failure(self):
        # Returning rather than raising is what advances the watermark, which
        # is the correct account: there was nothing to register and the
        # question is settled for this record sequence.
        dbh = FakeDB()
        register = products.registrar(dbh, InMemoryObjectStore())

        result = register({"attempt_id": 9}, None,
                          record=reference_record(job_type="registration"))

        self.assertIsNone(result)
        self.assertEqual([], dbh.calls)


class RegistrarDispatchTests(unittest.TestCase):
    def test_the_job_type_in_the_record_selects_the_body(self):
        dbh = FakeDB()
        register = products.registrar(dbh, InMemoryObjectStore())

        register({"attempt_id": 1}, None, record=reference_record())
        self.assertEqual(dbh.calls[0][0], "add_refimage")

        dbh.calls.clear()
        register({"attempt_id": 2}, None, record=difference_record())
        self.assertEqual(dbh.calls[0][0], "add_diffimage")

    def test_a_record_with_no_job_type_at_all_is_a_missing_fact(self):
        register = products.registrar(FakeDB(), InMemoryObjectStore())

        with self.assertRaises(products.MissingRecordFact) as caught:
            register({"attempt_id": 1}, None,
                     record=reference_record(job_type=None))

        self.assertEqual(caught.exception.field, "job_type")

    def test_the_handle_is_not_opened_until_something_registers(self):
        # A pass with no candidates, or one whose every candidate the taxonomy
        # refuses, must not cost a database connection.
        opened = []

        def factory():
            opened.append(True)
            return FakeDB()

        register = products.registrar(factory, InMemoryObjectStore())
        self.assertEqual([], opened)

        register({"attempt_id": 1}, None, record=reference_record())
        self.assertEqual([True], opened)


class AttemptIdentityThreadingTests(unittest.TestCase):
    """ROUND-3 FINDING #8: the insert knows which registration it belongs to.

    Migration 018 gave `refimages` and `diffimages` an `attempt_id` and a
    `registered_record_sequence`, paired by a CHECK and made unique by a
    partial index, and taught `addRefImage`/`addDiffImage` to find-or-insert on
    that pair BEFORE minting `max(version)+1`. That is what makes a replayed
    registration return the row it already wrote instead of a second copy at a
    new version — but only if the pair actually arrives.

    These tests are the Python half of that contract: the identity reaches the
    call, in the trailing positions the stored function declares, and it is the
    SAME on a replay and HIGHER on a supersession. Without them the two new
    parameters could sit in the signature unpassed and every assertion above
    would still be green, because the legacy argument list is unchanged and
    every one of those tests only ever looked at it.
    """

    def _register(self, row_sequence, attempt_id=2):
        """Register a difference image through the real registrar seam.

        Deliberately NOT by calling `register_difference_image` directly: the
        sequence has to come off the candidate ROW and be threaded down by
        `registrar`, and calling the body directly would let the test supply
        what the production path is supposed to derive. The row is shaped as
        `consumer._COLUMNS` selects it.
        """
        dbh = FakeDB()
        register = products.registrar(dbh, InMemoryObjectStore())
        register({"attempt_id": attempt_id,
                  "terminal_record_sequence": row_sequence},
                 None, record=difference_record())
        return dbh

    def test_the_identity_reaches_add_diffimage_in_the_trailing_positions(self):
        dbh = self._register(1)

        name, args = dbh.calls[0]
        self.assertEqual("add_diffimage", name)
        # addDiffImage's last two declared arguments, so the last two here.
        self.assertEqual((2, 1), args[-2:],
                         "the attempt identity did not reach add_diffimage; "
                         "the stored function cannot deduplicate a replay "
                         "without it")

    def test_the_identity_reaches_add_refimage_in_the_trailing_positions(self):
        dbh = FakeDB()
        register = products.registrar(dbh, InMemoryObjectStore())
        register({"attempt_id": 5, "terminal_record_sequence": 3}, None,
                 record=reference_record())

        name, args = dbh.calls[0]
        self.assertEqual("add_refimage", name)
        self.assertEqual((5, 3), args[-2:])

    def test_a_replay_at_the_same_sequence_presents_the_same_identity(self):
        # Same attempt, same record sequence, twice. The stored function
        # deduplicates on exactly this pair, so presenting the same pair is
        # what makes the second pass write no second set of rows. A pass that
        # sent nothing, or sent something different each time, would mint a new
        # version every time — which is what the unfixed code did.
        first = self._register(1)
        second = self._register(1)

        self.assertEqual(first.calls[0][1][-2:], second.calls[0][1][-2:])
        self.assertEqual((2, 1), second.calls[0][1][-2:])

    def test_a_supersession_at_a_higher_sequence_presents_the_higher_one(self):
        # Idempotence must not swallow a genuine re-registration. A superseding
        # classification raises `terminal_record_sequence`, the pair differs,
        # and the stored function mints a new version as it should.
        first = self._register(1)
        superseding = self._register(2)

        self.assertEqual((2, 1), first.calls[0][1][-2:])
        self.assertEqual((2, 2), superseding.calls[0][1][-2:],
                         "the supersession presented the earlier sequence, so "
                         "the stored function would return the existing row "
                         "and the new products would never be registered")

    def test_the_legacy_arguments_are_undisturbed_ahead_of_the_identity(self):
        # The two new parameters are TRAILING and optional, which is the whole
        # reason nothing else that calls these methods breaks. If they had been
        # inserted anywhere else, every legacy argument after them would shift
        # by two and the stored function would silently reinterpret a checksum
        # as a status.
        dbh = self._register(1)
        _name, args = dbh.calls[0]

        difference = products.published(difference_record(), 2)["difference_image"]
        self.assertEqual(difference["uri"], args[-4])
        self.assertEqual(difference["checksum"], args[-3])

    def test_the_bodies_still_work_with_no_identity_at_all(self):
        # Optional means optional: a caller that passes neither gets the legacy
        # behaviour, which is what keeps every other caller of add_diffimage
        # working. The stored function defaults them too.
        dbh = FakeDB()
        record = difference_record()

        products.register_difference_image(
            dbh, record, record["science_provenance"], 2)

        self.assertEqual((None,), dbh.calls[0][1][-1:])


if __name__ == "__main__":
    unittest.main()
