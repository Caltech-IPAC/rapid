"""The ported product-registration bodies.

Two things these assert that the consumer suite cannot: that the legacy call
SEQUENCE is preserved (insert, then finalize so vbest points at this version,
then the catalogues/measurements keyed by what the insert returned), and that
every fact comes from the attempt's own terminal record — with an absent fact
raising `MissingRecordFact` rather than being defaulted.
"""

import json
import unittest

from pipeline.registration import products
from pipeline.runtime.boundaries import InMemoryObjectStore, checksum


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


def science_record(**overrides):
    body = {
        "attempt_id": 1,
        "job_type": "reference-image",
        "ppid": 12,
        "products": [
            {"name": "reference_image", "uri": "s3://p/ref.fits",
             "checksum": "ref-sha"},
            {"name": "reference_sexcat", "uri": "s3://p/sex.txt",
             "checksum": "sex-sha"},
        ],
        "science_provenance": {
            "field": 4678636, "fid": 1, "hp6": 100, "hp9": 200,
            "reference_image_infobits": 0,
        },
    }
    body.update(overrides)
    return body


def difference_record(**overrides):
    body = {
        "attempt_id": 2,
        "job_type": "science",
        "ppid": 15,
        "products": [
            {"name": "difference_image", "uri": "s3://p/diff.fits",
             "checksum": "diff-sha"},
        ],
        "science_provenance": {
            "rid": 42, "reference_image_id": 77, "field": 4678636, "fid": 1,
            "sca": 7, "hp6": 100, "hp9": 200,
            "sky_position": {"ra0": 1.0, "dec0": 2.0, "ra1": 1.1, "dec1": 2.1,
                             "ra2": 1.2, "dec2": 2.2, "ra3": 1.3, "dec3": 2.3,
                             "ra4": 1.4, "dec4": 2.4},
            "nsexcatsources": 1234, "scalefacref": 0.98,
            "dxrmsfin": 0.01, "dyrmsfin": 0.02,
            "dxmedianfin": 0.001, "dymedianfin": 0.002,
        },
    }
    body.update(overrides)
    return body


class ReferenceImageBodyTests(unittest.TestCase):
    def test_the_legacy_call_sequence_is_preserved(self):
        # add_refimage inserts and returns the rfid; update_refimage then
        # finalizes so vbest points at this version; the catalogues follow,
        # keyed by that rfid. Splitting insert from finalize was the legacy
        # design — the insert cannot know it is best until it exists.
        dbh = FakeDB()

        result = products.register_reference_image(
            dbh, science_record(), science_record()["science_provenance"], 1)

        self.assertEqual([name for name, _ in dbh.calls],
                         ["add_refimage", "update_refimage",
                          "register_refimcatalog"])
        self.assertEqual(result["rfid"], 77)

    def test_the_uri_and_checksum_come_from_the_published_product(self):
        dbh = FakeDB()

        products.register_reference_image(
            dbh, science_record(), science_record()["science_provenance"], 1)

        _name, args = dbh.calls[0]
        self.assertIn("s3://p/ref.fits", args)
        self.assertIn("ref-sha", args)

    def test_an_unpublished_catalogue_is_skipped_not_invented(self):
        # The legacy body's `if photutils_..._uploaded_to_bucket:` guard. The
        # published list is the authority on what exists.
        dbh = FakeDB()

        result = products.register_reference_image(
            dbh, science_record(), science_record()["science_provenance"], 1)

        self.assertEqual([c["cattype"] for c in result["catalogs"]],
                         [products.CATTYPE_SEXTRACTOR])

    def test_both_catalogues_register_when_both_were_published(self):
        record = science_record()
        record["products"].append(
            {"name": "reference_psfcat", "uri": "s3://p/psf.txt",
             "checksum": "psf-sha"})
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
        record = science_record()
        del record["science_provenance"]["hp6"]

        with self.assertRaises(products.MissingRecordFact) as caught:
            products.register_reference_image(
                FakeDB(), record, record["science_provenance"], 1)

        self.assertEqual(caught.exception.field, "hp6")

    def test_a_missing_product_is_a_finding(self):
        record = science_record(products=[])

        with self.assertRaises(products.MissingRecordFact):
            products.register_reference_image(
                FakeDB(), record, record["science_provenance"], 1)

    def test_a_database_failure_raises_rather_than_exiting_the_process(self):
        # The legacy bodies called exit() from inside the registration, which
        # took the whole pass down and left every later job unregistered with
        # no account of how far it got.
        dbh = FakeDB(exit_code=64)

        with self.assertRaises(products.RegistrationFailed):
            products.register_reference_image(
                dbh, science_record(),
                science_record()["science_provenance"], 1)


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
        for value in (1.0, 2.0, 1.1, 2.1, 1.2, 2.2, 1.3, 2.3, 1.4, 2.4):
            self.assertIn(value, args)

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
        self.body = science_record()
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


class RegistrarDispatchTests(unittest.TestCase):
    def test_the_job_type_in_the_record_selects_the_body(self):
        dbh = FakeDB()
        register = products.registrar(dbh, InMemoryObjectStore())

        register({"attempt_id": 1}, None, record=science_record())
        self.assertEqual(dbh.calls[0][0], "add_refimage")

        dbh.calls.clear()
        register({"attempt_id": 2}, None, record=difference_record())
        self.assertEqual(dbh.calls[0][0], "add_diffimage")

    def test_an_unknown_job_type_is_a_finding(self):
        register = products.registrar(FakeDB(), InMemoryObjectStore())

        with self.assertRaises(products.MissingRecordFact):
            register({"attempt_id": 1}, None,
                     record=science_record(job_type="mystery"))

    def test_the_handle_is_not_opened_until_something_registers(self):
        # A pass with no candidates, or one whose every candidate the taxonomy
        # refuses, must not cost a database connection.
        opened = []

        def factory():
            opened.append(True)
            return FakeDB()

        register = products.registrar(factory, InMemoryObjectStore())
        self.assertEqual([], opened)

        register({"attempt_id": 1}, None, record=science_record())
        self.assertEqual([True], opened)


if __name__ == "__main__":
    unittest.main()
