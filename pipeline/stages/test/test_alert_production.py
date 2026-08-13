"""
File:    test_alert_production.py

Tests for the alert-production job type — the step-4 trigger.

**THE DOUBLES CAN REFUSE**, the same discipline `test_post_db` states: a
double that accepts everything and a watermark that always claims would pass
these tests against code with no emission control at all. So the watermark
double enforces its real primary key (one claim per unit per release, later
claims lose), and `FakeConn`'s modelled `alert_outbox` table can refuse a
same-`alert_id`-different-envelope insert exactly as the real PL/pgSQL
function does (`insert_alert_outbox_packet`'s own docstring: "one identity,
two different packets ... both are defects that a silent no-op would hide") —
because a double that always accepted could not show that invariant at all.

**THIS JOB TYPE NO LONGER SENDS ANYTHING (brief E, rule 14).** Until package E
this module constructed a live Kafka producer and published in-job; the
2026-08-04 Q7 finding (every send failed, `flush()` returned normally, and the
run reported publishing alerts it had not published) is WHY that path was
tested so carefully here, and it is exactly the class of bug "no producer
exists to call" makes structurally impossible rather than merely untested.
`Producer` is kept, not deleted, and repurposed to FAIL LOUDLY the moment
anything calls `produce()` or `flush()` on it — a double that could record a
send the code should never make again is worth more than one that quietly
vanished, because it turns "the stage regressed to publishing in-job" from a
silent behavioural change back into a hard test failure at the exact call
site the module docstring says must never be reached
(`pipeline/contract/test_alert_send_routes.py` asserts the same thing
repo-wide; this file asserts it locally, against a live run).
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

from database.modules.utils.checked import RapidDBCallFailed  # noqa: E402
from database.modules.utils.rapid_db import RAPIDDB        # noqa: E402
from pipeline.runtime.errors import InputError             # noqa: E402
from pipeline.stages import alert_production                # noqa: E402
from submission import payloads                             # noqa: E402
from submission.manifest import ProcessingUnit               # noqa: E402
from submission.routes import JOB_TYPE_ALERT_PRODUCTION      # noqa: E402


class Source:
    """A candidate, with only what the selection and identity computation
    read.

    `id`/`isdiffpos` are the CATALOG key `alert_identity` hashes
    (`alerts/identity.py`'s module docstring: never `sid`, which is
    DB-generated and realization-local). Defaulted here to values distinct
    from `sid` so a test that accidentally read the wrong attribute would
    fail loudly rather than by coincidence passing.
    """

    def __init__(self, sid, snr=None, id=None, isdiffpos=True):
        self.sid = sid
        self.snr = snr
        self.id = id if id is not None else sid * 100
        self.isdiffpos = isdiffpos


class FakeConn:
    """The borrowed connection `produce_alerts` claims/confirms/outboxes
    through.

    `produce_alerts` writes the CAS claim, the confirm, and (since brief E)
    the outbox rows through `RAPIDDB.borrowing(context.require_connection())`
    rather than through an injected watermark or producer object (migration
    037 / integration ruling 3, extended by rule 14) — so the double has to
    stand in for the psycopg2 connection itself, the same idiom
    `pipeline/registration/test/test_consumer.py`'s own `FakeConn` uses:
    `cursor()` returns something with `execute`/`fetchone`/`close`, `commit`/
    `rollback` are tracked so a test can assert the transaction boundary
    (`transaction(conn)`, not `RAPIDDB.borrowing`'s own suppressed one).

    THE DOUBLE CAN REFUSE, in TWO tables now:

      * `alert_emissions` is modelled as a real CAS would behave: at most one
        row per (exposure_id, sca, release_identity), a claim succeeds only
        under the real WHERE clause (state='claimed' AND (stale OR same
        claimant OR prior claimant terminal)), and confirm succeeds only when
        the caller's own token still matches. A double that always claimed,
        or always confirmed, could not show the suppression or takeover paths
        at all.

      * `alert_outbox` is modelled by RECORDING the calls made through
        `insert_alert_outbox_packet` rather than by running real SQL —
        that function is PL/pgSQL (migration 050) and the stub tier cannot
        execute a server-side function body. What IS kept from the real
        function's contract is the one invariant a test can check without a
        real engine: a second insert under an `alert_id` already on record,
        carrying a DIFFERENT `checksum`, RAISES — modelling "one identity,
        two different packets ... both are defects that a silent no-op would
        hide" (the PL/pgSQL function's own docstring, quoted in
        `RAPIDDB.insert_alert_outbox_packet`). An insert with an IDENTICAL
        checksum is absorbed as 'idempotent', matching the real function's
        documented return values.
    """

    def __init__(self, failure=0, rows=None, terminal_attempts=(),
                product_keys=None, product_key_lookup_error=None):
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
        #: `cur.description` is None for a statement that produced no result
        #: set and non-None for one that did — psycopg2's own contract, and
        #: the discriminator TWO different callers read.
        #:
        #: `ConnectionExecutor.execute` (the milestone writer's path) takes
        #: the `rowcount` branch when it is None, which models "an INSERT with
        #: no RETURNING clause" — exactly `record_milestone`'s statement
        #: shape. `AlertOutboxRepository._query` reads it for the same reason
        #: and returns `[]` rather than calling `fetchall()` on a statement
        #: that produced nothing.
        #:
        #: It is therefore SET PER STATEMENT by `execute` below rather than
        #: fixed at None: a double that always claimed "no result set" would
        #: make the repository's SELECTs return `[]`, and the stage would read
        #: that as "no product binding" and "the insert returned no row" —
        #: two wrong answers that look like data rather than like a broken
        #: double.
        self.description = None
        self.rowcount = 1

        #: pid -> product_key, for `get_difference_image_product_key`. A pid
        #: absent from this dict has no product binding (the ordinary,
        #: pre-D-history case `_image_identity` degrades from) — modelled as
        #: a dict lookup rather than SQL because DRAFT 048's `products`/
        #: `diffimages.product_id` join is exactly the schema-may-be-absent
        #: case `get_difference_image_product_key`'s own docstring says a
        #: stub-tier connection has no business executing.
        self.product_keys = dict(product_keys or {})
        #: Whether DRAFT 048's `products` / `diffimages.product_id` binding
        #: exists AT ALL on this modelled database, as distinct from existing
        #: and being empty for a given pid. The repository probes the catalog
        #: before it joins, so the double has to answer that probe — and the
        #: two answers are different facts the identity basis must not
        #: conflate: "048 is unapplied" and "this image has no binding" both
        #: yield the legacy basis, while a failed LOOKUP must refuse to choose
        #: a basis at all.
        self.product_binding_present = True
        #: Raised (as `RapidDBCallFailed`, via `self.failure`-style exit_code
        #: convention below) when set, to drive the "failed lookup is not an
        #: absent binding" refusal in `_image_identity`.
        self.product_key_lookup_error = product_key_lookup_error
        #: alert_id -> {"checksum", "payload", "basis", "topic",
        #: "release_identity", "exposure_id", "sca", "attempt_id"} — the
        #: modelled `alert_outbox` table. Populated only through
        #: `insert_alert_outbox_packet`, never directly, so a test asserts
        #: the same surface the stage itself writes through.
        self.alert_outbox = {}
        #: alert_ids inserted since the last `commit()` — see `rollback()`,
        #: which discards exactly this set so an aborted confirmation
        #: transaction leaves no trace, matching "a losing claimant commits
        #: NEITHER outbox rows NOR the milestone."
        self._pending_outbox_ids = set()
        #: Calls into `insert_alert_outbox_packet`, in order, exactly as
        #: made — including ones later rolled back by an outer transaction
        #: failure, so a test can distinguish "never called" from "called,
        #: then rolled back" if it needs to (none currently do, but the
        #: rollback-tracked `alert_outbox` dict alone could not tell the
        #: difference).
        self.outbox_calls = []

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
        # Whatever this transaction staged into `alert_outbox` is now
        # durable; nothing left in `_pending_outbox_ids` should be undone by
        # a later rollback (there should not be a later one in these tests —
        # `produce_alerts` runs at most one confirmation transaction per
        # call — but clearing it keeps the double correct if that ever
        # changes).
        self._pending_outbox_ids.clear()

    def rollback(self):
        self.rollbacks += 1
        # A rolled-back transaction must not leave partial outbox rows
        # visible to a later read — the real transaction wraps CONFIRM, the
        # outbox inserts and the milestone as one unit (rule 14), so a
        # rollback undoes all three together. `_pending_outbox_ids` is
        # exactly the set of alert_ids inserted since the last commit, so
        # discarding them (and only them — an id already durable from an
        # EARLIER commit, e.g. a genuine idempotent re-run, must survive)
        # models that atomicity precisely.
        for alert_id in self._pending_outbox_ids:
            self.alert_outbox.pop(alert_id, None)
        self._pending_outbox_ids.clear()

    # -- cursor surface: dispatches on statement shape, like the
    #    registration consumer's own FakeConn --------------------------
    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        lowered = " ".join(statement.lower().split())
        if self.failure:
            self._last_result = None
            raise RuntimeError(f"stubbed query failure ({self.failure})")
        # `description` is set per statement — see its comment above. A
        # statement that produces a result set gets a non-None value; one that
        # does not keeps None. The value's CONTENT is never read by anything,
        # only its None-ness, so a one-column placeholder is enough.
        self.description = [("result",)]
        if "insert into alert_emissions" in lowered and "on conflict" in lowered:
            self._last_result = self._claim(params)
        elif "update alert_emissions" in lowered and "state = 'emitted'" in lowered:
            self._last_result = self._confirm(params)
        elif "insert into milestones" in lowered:
            self.milestones.append(params)
            self._last_result = None
            self.description = None
        elif "insert_alert_outbox_packet" in lowered:
            self._last_result = (self._insert_outbox_packet(params),)
        elif "select p.product_key from diffimages" in lowered:
            self._last_result = self._product_key(params)
        # THE REPOSITORY'S SCHEMA PROBES. `AlertOutboxRepository` asks the
        # catalog whether DRAFT 050 and DRAFT 048 are applied before it reads
        # or writes — that is what keeps "the schema is absent" apart from
        # "the query failed", and on the confirmation path it is what avoids
        # aborting the caller's transaction to find out. The double answers
        # YES to both by default, because these tests exercise the deployed
        # case; `product_binding` is flipped by the tests that want the
        # legacy-pid degradation.
        elif "to_regclass('public.alert_outbox')" in lowered:
            self._last_result = ("alert_outbox",)
        elif "to_regclass('public.products')" in lowered:
            self._last_result = (self.product_binding_present,)
        else:
            self._last_result = None
            self.description = None

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

    # -- the modelled alert_outbox table, matching insert_alert_outbox_
    #    packet's documented contract (RAPIDDB.insert_alert_outbox_packet's
    #    own docstring, migration 050) --------------------------------
    def _insert_outbox_packet(self, params):
        (alert_id, identity_basis, payload, checksum, schema_version_id,
         topic, release_identity, exposure_id, sca, producing_attempt_id,
         corrects_alert_id) = params
        self.outbox_calls.append(params)
        existing = self.alert_outbox.get(alert_id)
        if existing is not None:
            if existing["checksum"] != checksum:
                # A SAME-ID, DIFFERENT-ENVELOPE COLLISION RAISES, uncaught —
                # the real function's documented behaviour: "either the
                # alert_id digest inputs are incomplete or two genuinely
                # different packets were minted under one identity, and both
                # are defects".
                #
                # IT CARRIES `pgcode = 'P0001'`, which is what makes this
                # double faithful rather than merely loud. PL/pgSQL's bare
                # `RAISE EXCEPTION` reports exactly that SQLSTATE, and
                # `AlertOutboxRepository._query` keys on it to decide what to
                # re-raise UNWRAPPED: a collision must escape the repository's
                # typed-error vocabulary and fail the attempt, while a genuine
                # query failure becomes `RepositoryQueryFailed`. A double that
                # raised a bare `RuntimeError` here would be wrapped like any
                # other failure and would prove the opposite of what this test
                # claims.
                error = RuntimeError(
                    f"alert_outbox collision: alert_id {alert_id!r} already "
                    f"recorded with a different payload checksum")
                error.pgcode = "P0001"
                raise error
            return "idempotent"
        # `payload` arrives already wrapped by `psycopg2.Binary()`, matching
        # the real bytea binding path — unwrapped here (`.adapted`, the same
        # attribute psycopg2's own adapter exposes) so `alert_outbox` stores
        # plain bytes a test can compare against what `serialize_alert`
        # returned, the same way a real bytea column would round-trip it.
        raw_payload = getattr(payload, "adapted", payload)
        self.alert_outbox[alert_id] = {
            "identity_basis": identity_basis, "payload": raw_payload,
            "checksum": checksum, "schema_version_id": schema_version_id,
            "topic": topic, "release_identity": release_identity,
            "exposure_id": exposure_id, "sca": sca,
            "producing_attempt_id": producing_attempt_id,
            "corrects_alert_id": corrects_alert_id}
        self._pending_outbox_ids.add(alert_id)
        return "inserted"

    # -- the modelled diffimages/products join -------------------------
    def _product_key(self, params):
        (pid,) = params
        if self.product_key_lookup_error is not None:
            raise self.product_key_lookup_error
        return (self.product_keys[pid],) if pid in self.product_keys else None


class Producer:
    """A producer double that FAILS LOUDLY the moment it is used.

    Brief E's whole point is that `produce_alerts` no longer constructs or
    calls a producer at all — the send moved to `rapid-publisher`, which
    reads `alert_outbox` instead (module docstring: "THIS JOB TYPE NO LONGER
    SENDS ANYTHING"). A double that quietly accepted `produce()`/`flush()`
    calls, the way the pre-E `Producer` did, would keep passing even if a
    regression reintroduced an in-job send — exactly the "double that
    accepts everything" failure mode `test_post_db` and this file's own
    module docstring warn about.

    So this double is not deleted; it is INVERTED. `_run_produce_alerts`
    below still patches `alerts.kafka_producer.make_producer` to return one
    of these — DELIBERATELY, so that if a regression ever made
    `produce_alerts` call `make_producer()` again, the call would succeed
    (there would be something to call it WITH) and the very next line,
    `producer.produce(...)` or `.flush()`, is what actually catches the
    regression, by raising. Any call to `produce()` or `flush()` is
    therefore not a stubbed response — it is a test failure, on purpose.
    """

    def produce(self, topic, value, callback=None):
        raise AssertionError(
            "alert_production.produce_alerts must never construct or use a "
            "Kafka producer (brief E, rule 14): this job type's obligation "
            "ends at the outbox, and rapid-publisher is the only component "
            "that sends")

    def flush(self):
        raise AssertionError(
            "alert_production.produce_alerts must never flush a producer "
            "(brief E, rule 14) — see produce() above")


class Context:
    """The stage context surface `produce_alerts` actually uses.

    `attempt_id` and `require_connection()` (migration 037 / integration
    ruling 3): the claim/confirm/outbox/milestone writes go through the
    ATTEMPT'S OWN borrowed connection, not through an injected watermark or
    producer object — `attempt_id` is this attempt's OWN identity (the
    claiming attempt, distinct from `unit.payload.promoted_attempt_id`, the
    registered SOURCE attempt the unit declares).
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

    def error(self, *args, **kwargs):
        pass


#: `kafka/schema-version-id` is PINNED in every test's parameter tree so
#: `_pinned_schema_version` takes its parameter-tree branch and never
#: reaches the Glue-registry fallback (`GlueSchemaRegistry().
#: schema_version_id(...)`, a real network call this suite has no business
#: making). A fixed UUID-shaped string rather than a real UUID object:
#: `_pinned_schema_version` only ever `str()`s it and stores it, so a
#: plain string exercises the same path a real pinned parameter would.
#:
#: The last group carries a hex LETTER deliberately: a UUID's final group is
#: twelve hex characters, and an all-numeric one is indistinguishable from an
#: AWS account id to `.githooks/pre-push`, which hard-blocks any twelve-digit
#: run in this PUBLIC repository with no allowlist. The fix is to stop writing
#: account-shaped strings, not to weaken a guard protecting a public repo —
#: the same resolution `pipeline/contract/fixture.py` reached for its
#: synthetic ARNs.
PINNED_SCHEMA_VERSION_ID = "11111111-1111-1111-1111-a11111111111"

PARAMETERS = {
    "kafka/topic": "rapid.internal.alerts.v1",
    "kafka/bootstrap-servers": "b-1:9098",
    "kafka/max-request-bytes": "15728640",
    "kafka/schema-version-id": PINNED_SCHEMA_VERSION_ID,
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


class SchemaVersionPinningTests(unittest.TestCase):
    """`_pinned_schema_version`: the parameter tree wins, and is never
    bypassed once it carries a value.

    Only the parameter-tree branch is exercised here — the registry-fallback
    branch calls `GlueSchemaRegistry()`, a real AWS client construction this
    stub-tier suite has no business triggering, and every `EmissionTests`
    run below pins the parameter for exactly that reason.
    """

    def test_a_pinned_parameter_is_used_as_is(self):
        context = Context(_unit(), PARAMETERS)

        version = alert_production._pinned_schema_version(
            context, "rapid.internal.alerts.v1")

        self.assertEqual(version, PINNED_SCHEMA_VERSION_ID)

    def test_the_pinned_value_is_stringified(self):
        # `context.parameter` can return a non-str (a real parameter store
        # may hand back whatever type it stored); `_pinned_schema_version`
        # promises a str because the outbox column and the effect record
        # both store it as text.
        context = Context(_unit(), dict(PARAMETERS,
                                        **{"kafka/schema-version-id": 12345}))

        version = alert_production._pinned_schema_version(
            context, "rapid.internal.alerts.v1")

        self.assertEqual(version, "12345")


class ImageIdentityTests(unittest.TestCase):
    """`_image_identity`: product-key preferred, legacy-pid the ratified
    degradation, and a failed lookup refused rather than degraded.
    """

    # THE COLLABORATOR IS THE CARVED REPOSITORY, not a `RAPIDDB` handle.
    # `RAPIDDB` is frozen (brief G's ratified merge decision, rule 17), so the
    # two calls brief E needs live in `pipeline/repositories/alert_outbox.py`
    # and these tests drive that class over the same `FakeConn`.

    def _repository(self, conn):
        from pipeline.repositories.alert_outbox import AlertOutboxRepository

        return AlertOutboxRepository(conn)

    def test_a_bound_difference_image_uses_the_product_key_basis(self):
        conn = FakeConn(product_keys={1086: "product-key-abc"})
        context = Context(_unit(), PARAMETERS, conn=conn)

        basis = alert_production._image_identity(
            self._repository(conn), 1086, context)

        self.assertEqual(basis, {"basis_name": "product-key",
                                 "product_key": "product-key-abc"})

    def test_an_unbound_difference_image_degrades_to_legacy_pid(self):
        # DRAFT 048 added the binding as nullable; pre-D history has none.
        conn = FakeConn()
        context = Context(_unit(), PARAMETERS, conn=conn)

        basis = alert_production._image_identity(
            self._repository(conn), 1086, context)

        self.assertEqual(basis, {"basis_name": "legacy-pid",
                                 "legacy_pid": 1086})

    def test_an_unapplied_draft_048_also_degrades_to_legacy_pid(self):
        # THE OTHER WAY TO HAVE NO PRODUCT KEY, and it must not look like a
        # failure. A database built from the authoritative stream alone has no
        # `products` table at all — the repository answers that from a CATALOG
        # PROBE rather than by letting a join fail, which is what keeps the
        # caller's confirmation transaction out of an aborted state.
        conn = FakeConn(product_keys={1086: "product-key-abc"})
        conn.product_binding_present = False
        context = Context(_unit(), PARAMETERS, conn=conn)

        basis = alert_production._image_identity(
            self._repository(conn), 1086, context)

        self.assertEqual(basis, {"basis_name": "legacy-pid",
                                 "legacy_pid": 1086})

    def test_a_failed_lookup_is_refused_rather_than_degraded(self):
        # A failed lookup is NOT an absent binding: degrading here would mint
        # a permanent legacy identity on the strength of a transient fault,
        # and identities are immutable once written.
        from pipeline.repositories.errors import RepositoryQueryFailed

        conn = FakeConn(failure=67)
        context = Context(_unit(), PARAMETERS, conn=conn)

        with self.assertRaises((RuntimeError, RepositoryQueryFailed)):
            alert_production._image_identity(
                self._repository(conn), 1086, context)


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

    `db` is no longer where the emission CAS or the outbox writes go
    (migration 037 / integration ruling 3 moved those onto the attempt's own
    borrowed connection — see `Context.require_connection`); this double no
    longer needs one at all, and takes none, so a test that mistakenly wired
    the old `watermark` object here would fail on the missing argument
    rather than silently doing nothing.
    """

    def __init__(self, sources=(), chip_error=None):
        self.sources = list(sources)
        self.chip_error = chip_error

    def iter_sources(self, pid):
        if self.chip_error is not None:
            raise self.chip_error
        return iter(self.sources)


def _run_produce_alerts(context, provider, producer, assemble=None,
                        fail_sids=(), serialize=None):
    """Run `produce_alerts` against doubles, patching the `alerts` package.

    Module-level (not a method) so every test class below can drive a real
    run without duplicating the patch list — `produce_alerts` resolves
    `alerts.cli`/`alerts.produce`/`alerts.kafka_producer` through
    module-level imports, so they are patched here rather than injected.

    `producer` is patched into `alerts.kafka_producer.make_producer`
    ANYWAY, even though brief E means `produce_alerts` should never call it:
    that is exactly what makes `Producer.produce`/`.flush` raising loudly
    (see the class) a meaningful assertion rather than a vacuous one — if
    `make_producer` were left unpatched (or unset), a regression that started
    constructing a producer again might get `None` back and fail somewhere
    unrelated, instead of failing AT the send.
    """
    import alerts.cli
    import alerts.produce
    import alerts.kafka_producer

    def fake_assemble(prov, source):
        if source.sid in fail_sids:
            raise ValueError(f"candidate {source.sid} is unusable")
        return {"sid": source.sid}

    def fake_serialize(alert, schema=None):
        return b"x" * 10

    patches = [
        # `db=` is the stage's path (its own connection's borrowing handle);
        # the double accepts and ignores it — the provider under test is
        # the fixture's, and a double that refused the real call shape was
        # exactly how this suite went red in-image while green off-image.
        (alerts.cli, "make_provider", lambda db=None: provider),
        (alerts.produce, "assemble_alert_for_source",
         assemble or fake_assemble),
        (alerts.produce, "load_schema", lambda *a, **k: {"fake": True}),
        (alerts.produce, "serialize_alert", serialize or fake_serialize),
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
    """Ruling 3 / rule 14: claim -> assemble -> confirm+outbox, and the
    effect counts.

    `produce_alerts` resolves its collaborators through module-level
    imports, so they are patched here rather than injected — the
    alternative would be widening the stage signature purely for the tests,
    which the repo's own seam discipline argues against. The emission CAS
    and the outbox writes are both exercised for real against `FakeConn`'s
    modelled tables through the real `RAPIDDB.claim_alert_emission` /
    `confirm_alert_emission` / `insert_alert_outbox_packet` SQL-building
    code — only the connection is a double, never the CAS or outbox logic.

    NAMED so `-k "outbox or producer or no_send or oversize"` selects the
    ones proving the new contract: at least one test name below carries
    "outbox", and at least one carries "no_send" or "producer".
    """

    def _run(self, context, provider, producer, assemble=None,
            fail_sids=(), serialize=None):
        """Run the stage against doubles, patching the alerts package."""
        _run_produce_alerts(context, provider, producer, assemble=assemble,
                            fail_sids=fail_sids, serialize=serialize)

    def test_a_clean_run_outboxes_packets_and_uses_no_producer(self):
        # Criterion 3 / rule 14's core claim: the job type's obligation ends
        # at the outbox. `producer` is the loud-failing double (see
        # `Producer`'s own docstring) — if `produce_alerts` touched it at
        # all, this test would fail INSIDE the run, at the call site, not
        # by a missed assertion afterward.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0), Source(2, 8.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self._run(context, provider, producer)

        self.assertEqual(len(conn.alert_outbox), 2)
        self.assertEqual(context.provenance["candidates_considered"], 2)
        self.assertEqual(context.provenance["alerts_outboxed"], 2)
        self.assertEqual(context.provenance["emissions_suppressed"], 0)
        self.assertTrue(context.provenance["emission_confirmed"])
        self.assertEqual(context.provenance["alert_topic"],
                         "rapid.internal.alerts.v1")
        # The new per-packet identity bookkeeping the effect record adds.
        self.assertEqual(context.provenance["alert_identity_basis"],
                         "legacy-pid")  # no product binding in this FakeConn
        self.assertEqual(context.provenance["alert_schema_version_id"],
                         PINNED_SCHEMA_VERSION_ID)
        # The selection rule names itself as the placeholder it is.
        self.assertIn("PLACEHOLDER", context.provenance["selection_rule"])
        # (a) CONFIRM lands, and (f) the milestone is in the SAME
        # transaction as the outbox writes.
        key = (20, 7, "rel-1")
        self.assertEqual(conn.rows[key]["state"], "emitted")
        self.assertEqual(len(conn.milestones), 1)
        self.assertEqual(conn.milestones[0][0], "alert_published")

    def test_the_stage_never_constructs_or_calls_a_producer(self):
        # The same run as above, phrased as its own test so `-k producer`
        # catches it even if the "clean run" test's name ever changes: the
        # absence of a send is itself the acceptance criterion, not a detail
        # of the outbox test. `make_producer` is patched to return the
        # loud-failing double; if the stage never calls it, the double is
        # simply never invoked and nothing raises.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        # No AssertionError from Producer.produce/.flush escapes this call —
        # that IS the assertion.
        self._run(context, provider, producer)

        self.assertEqual(context.provenance["alerts_outboxed"], 1)

    def test_an_already_emitted_unit_outboxes_nothing(self):
        # "Emission is once per logical unit per release" — a replay is
        # silent, and the suppression is one of the four effect counts.
        conn = FakeConn(rows={(20, 7, "rel-1"): {"state": "emitted",
                                                  "claim_token": None}})
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self._run(context, provider, producer)

        self.assertEqual(conn.alert_outbox, {})
        self.assertEqual(context.provenance["emissions_suppressed"], 1)
        self.assertEqual(context.provenance["alerts_outboxed"], 0)

    def test_a_candidate_failure_drops_only_that_candidate(self):
        # Gate 3: candidate failures never fail the attempt.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0), Source(2, 8.0),
                                     Source(3, 7.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self._run(context, provider, producer, fail_sids={2})

        self.assertEqual(len(conn.alert_outbox), 2)
        self.assertEqual(context.provenance["alerts_outboxed"], 2)
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

        # (b) claim-then-crash-before-assemble: the claim is intact,
        # untouched, and nothing was outboxed.
        self.assertEqual(conn.rows[(20, 7, "rel-1")]["state"], "claimed")
        self.assertEqual(conn.alert_outbox, {})

    def test_an_unclaimable_emission_refuses_to_outbox(self):
        # A claim that could not be RECORDED must not outbox: an emission
        # that is not recorded can be emitted again.
        conn = FakeConn(failure=67)
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        with self.assertRaises(RuntimeError):
            self._run(context, provider, producer)

        self.assertEqual(conn.alert_outbox, {})

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

        self.assertEqual(len(conn.alert_outbox), 1)
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

    def test_a_losing_claimant_writes_no_outbox_rows(self):
        # (c) takeover, the CONFIRM-side half, re-targeted at the outbox
        # (brief E: the confirm CAS returning zero rows means the insert-
        # outbox-rows arm of the confirmation transaction never runs at
        # all — see `produce_alerts`' "THE ORDER IS FIXED" comment). A
        # foreign token at confirm time is a no-op, never a failure, and
        # never outboxes on this attempt's behalf.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        # Claim as usual, then simulate another attempt taking the claim
        # over before this attempt's confirm runs (inside the same
        # transaction the outbox inserts would otherwise land in).
        original_confirm = conn._confirm

        def confirm_after_takeover(params):
            key = (20, 7, "rel-1")
            conn.rows[key]["claim_token"] = "12345"  # a different claimant
            return original_confirm(params)

        conn._confirm = confirm_after_takeover

        self._run(context, provider, producer)

        # The packet was assembled (STEP 2 ran) but this attempt's own
        # confirmation did not land, so STEP 3's outbox-insert arm never
        # executed: no outbox rows, no milestone.
        self.assertEqual(conn.alert_outbox, {})
        self.assertEqual(conn.milestones, [])
        self.assertFalse(context.provenance["emission_confirmed"])
        self.assertEqual(context.provenance["alerts_outboxed"], 0)
        self.assertEqual(conn.rows[(20, 7, "rel-1")]["claim_token"], "12345")

    def test_duplicate_tolerant_retry_re_outboxes_an_unconfirmed_claim(self):
        # (e) duplicate-tolerance: a retry finding its OWN unconfirmed claim
        # (same claim_token — this attempt crashed after claiming but before
        # confirming, and is now retried under the SAME attempt identity)
        # re-assembles and outboxes rather than suppressing. The alert_id is
        # deterministic (`alert_identity`), so a genuine resend after a lost
        # response would insert the SAME id — absorbed as 'idempotent' by
        # `insert_alert_outbox_packet` — but this is a first outbox for this
        # id within the test, so it lands as an ordinary insert.
        conn = FakeConn(rows={(20, 7, "rel-1"):
                              {"state": "claimed",
                               "claim_token": str(CLAIMING_ATTEMPT_ID)}})
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self._run(context, provider, producer)

        self.assertEqual(len(conn.alert_outbox), 1)
        self.assertEqual(context.provenance["emissions_suppressed"], 0)

    def test_outbox_rows_carry_the_packets_serialized_bytes_and_checksum(self):
        # The outbox row is the packet: a test that only counted rows could
        # not tell "wrote the right bytes" from "wrote a row". Asserts
        # against `alert_id`/checksum computed the SAME way the stage
        # computes them, matching a real reader's own verification path.
        from alerts.identity import alert_identity, payload_checksum

        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0, id=42, isdiffpos=True)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self._run(context, provider, producer)

        expected_id, _ = alert_identity(
            legacy_pid=1086, catalog_id=42, isdiffpos=True,
            release_identity="rel-1")
        self.assertIn(expected_id, conn.alert_outbox)
        row = conn.alert_outbox[expected_id]
        self.assertEqual(row["payload"], b"x" * 10)
        self.assertEqual(row["checksum"], payload_checksum(b"x" * 10))
        self.assertEqual(row["identity_basis"], "legacy-pid")
        self.assertEqual(row["topic"], "rapid.internal.alerts.v1")
        self.assertEqual(row["schema_version_id"], PINNED_SCHEMA_VERSION_ID)


class OversizePacketTests(unittest.TestCase):
    """Criterion 8: an oversize packet is an auditable drop, never a
    stranded outbox row.

    `alert_production.MAX_PACKET_BYTES` is MONKEYPATCHED down to a small
    number rather than actually serializing a 12 MiB+ payload — cheaper,
    faster, and it exercises exactly the comparison the module performs
    (`framed_size = len(payload) + GLUE_HEADER_LEN; if framed_size >
    MAX_PACKET_BYTES`) without needing real megabytes on either side of it.
    The patch is restored in `tearDown` so it cannot leak into another
    test's assertions about the real 12 MiB bound
    (`SelectionTests`/`EmissionTests` never reference the constant, but a
    leaked patch surviving a test failure is exactly the kind of thing that
    silently breaks an unrelated test far below in file order).
    """

    def setUp(self):
        self._real_max_packet_bytes = alert_production.MAX_PACKET_BYTES
        # Small enough that the ordinary `b"x" * 10` fixture payload (plus
        # GLUE_HEADER_LEN, 18 bytes) still fits comfortably under it, so a
        # test can make ONE source oversize by giving it a bigger payload
        # while every other source in the same run stays under the bound
        # unperturbed.
        alert_production.MAX_PACKET_BYTES = 100

    def tearDown(self):
        alert_production.MAX_PACKET_BYTES = self._real_max_packet_bytes

    def test_an_oversize_packet_is_dropped_with_reason_and_not_outboxed(self):
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0, id=1),
                                     Source(2, 8.0, id=2),
                                     Source(3, 7.0, id=3)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        # Source 2's packet alone is oversize: `serialize_alert` returns a
        # payload sized well past the patched 100-byte MAX_PACKET_BYTES for
        # sid 2, and the ordinary 10-byte fixture payload for the others —
        # no real megabyte-scale allocation needed to cross a 100-byte bound.
        def serialize_one_big(alert, schema=None):
            if alert["sid"] == 2:
                return b"y" * 200
            return b"x" * 10

        _run_produce_alerts(context, provider, producer,
                            serialize=serialize_one_big)

        # (criterion 8a) the oversize candidate is a recorded drop, not a
        # crash and not a silent skip.
        self.assertEqual(
            context.provenance["dropped_by_reason"].get(
                alert_production.DROP_REASON_OVERSIZE), 1)
        oversize_dispositions = [
            d for d in context.provenance["drop_dispositions"]
            if d["reason"] == alert_production.DROP_REASON_OVERSIZE]
        self.assertEqual(len(oversize_dispositions), 1)
        self.assertEqual(oversize_dispositions[0]["sid"], 2)
        self.assertIn("bytes", oversize_dispositions[0])

        # (criterion 8b) the OTHER candidates on the same chip still outbox
        # normally — gate 3's drop-and-continue applies to an oversize
        # packet exactly as it does to any other per-candidate drop.
        self.assertEqual(context.provenance["alerts_outboxed"], 2)
        self.assertEqual(len(conn.alert_outbox), 2)

        # (criterion 8c) no outbox row exists for the oversize candidate's
        # alert_id — it was computed (the drop disposition's own "detail"
        # references it), but never written, because the size check runs
        # BEFORE the packet is appended to the in-memory `packets` list that
        # the confirmation transaction commits.
        from alerts.identity import alert_identity

        oversize_alert_id, _ = alert_identity(
            legacy_pid=1086, catalog_id=2, isdiffpos=True,
            release_identity="rel-1")
        self.assertNotIn(oversize_alert_id, conn.alert_outbox)

    def test_an_all_oversize_chip_confirms_with_zero_outboxed(self):
        # The degenerate case: every candidate on the chip is oversize. The
        # claim still confirms (STEP 3 still runs — there is nothing chip-
        # level wrong, only per-candidate drops) but outboxes nothing, which
        # is a materially different effect record from a suppression
        # (emissions_suppressed stays 0; the claim really was won and
        # confirmed, it simply had nothing to write).
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0, id=1)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        _run_produce_alerts(context, provider, producer,
                            serialize=lambda alert, schema=None: b"z" * 200)

        self.assertEqual(context.provenance["alerts_outboxed"], 0)
        self.assertEqual(conn.alert_outbox, {})
        self.assertEqual(
            context.provenance["dropped_by_reason"].get(
                alert_production.DROP_REASON_OVERSIZE), 1)
        self.assertTrue(context.provenance["emission_confirmed"])
        self.assertEqual(context.provenance["emissions_suppressed"], 0)


class OutboxCollisionTests(unittest.TestCase):
    """`FakeConn.alert_outbox` refuses a same-id, different-envelope insert
    — the discipline this file's module docstring states: the double must
    be able to refuse, not just record.
    """

    def _repository(self, conn):
        from pipeline.repositories.alert_outbox import AlertOutboxRepository

        return AlertOutboxRepository(conn)

    def test_a_same_id_different_checksum_insert_raises(self):
        conn = FakeConn()
        outbox = self._repository(conn)

        outbox.insert_packet(
            "sha256:aaaa", "legacy-pid", b"payload-one", "sha256:one",
            PINNED_SCHEMA_VERSION_ID, "rapid.internal.alerts.v1", "rel-1",
            20, 7, CLAIMING_ATTEMPT_ID)

        # AND IT ESCAPES THE REPOSITORY'S TYPED VOCABULARY. The raise is a
        # hard invariant violation, not a query that failed to run, so the
        # repository re-raises it UNWRAPPED rather than as
        # `RepositoryQueryFailed` — which the caller catches and treats as a
        # recoverable database fault. Asserting the negative is the whole
        # point of this test: wrapping it would silently downgrade a
        # corruption signal into a retryable one.
        from pipeline.repositories.errors import RepositoryQueryFailed

        with self.assertRaises(RuntimeError) as caught:
            outbox.insert_packet(
                "sha256:aaaa", "legacy-pid", b"payload-TWO", "sha256:two",
                PINNED_SCHEMA_VERSION_ID, "rapid.internal.alerts.v1",
                "rel-1", 20, 7, CLAIMING_ATTEMPT_ID)
        self.assertNotIsInstance(caught.exception, RepositoryQueryFailed)

    def test_an_ordinary_query_failure_is_typed_as_a_repository_error(self):
        # THE OTHER SIDE OF THE SAME DISCRIMINATOR. A failure that is NOT the
        # migration's own P0001 raise — a dropped connection, a typo, a
        # permissions error — must become `RepositoryQueryFailed`, which the
        # confirmation path catches and reports as "nothing was committed".
        # Without this test the pass-through above could be implemented as
        # "never wrap anything" and still look correct.
        from pipeline.repositories.errors import RepositoryQueryFailed

        conn = FakeConn(failure=67)
        outbox = self._repository(conn)

        with self.assertRaises(RepositoryQueryFailed):
            outbox.insert_packet(
                "sha256:cccc", "legacy-pid", b"payload", "sha256:c",
                PINNED_SCHEMA_VERSION_ID, "rapid.internal.alerts.v1", "rel-1",
                20, 7, CLAIMING_ATTEMPT_ID)

    def test_a_same_id_same_checksum_insert_is_idempotent(self):
        # The ordinary case: a re-run after a lost response recomputes the
        # same digest and the insert path absorbs it rather than raising.
        conn = FakeConn()
        outbox = self._repository(conn)

        first = outbox.insert_packet(
            "sha256:bbbb", "legacy-pid", b"payload", "sha256:same",
            PINNED_SCHEMA_VERSION_ID, "rapid.internal.alerts.v1", "rel-1",
            20, 7, CLAIMING_ATTEMPT_ID)
        second = outbox.insert_packet(
            "sha256:bbbb", "legacy-pid", b"payload", "sha256:same",
            PINNED_SCHEMA_VERSION_ID, "rapid.internal.alerts.v1", "rel-1",
            20, 7, CLAIMING_ATTEMPT_ID)

        self.assertEqual(first.outcome, "inserted")
        self.assertTrue(first.was_written)
        self.assertEqual(second.outcome, "idempotent")
        self.assertFalse(second.was_written)
        self.assertEqual(len(conn.alert_outbox), 1)


class SystemicFailureTests(unittest.TestCase):
    """Finding 10: a systemic failure during assembly fails the chip — it
    must never be absorbed as a per-candidate drop, and a nonempty
    selection that assembles zero packets by ordinary candidate-shaped
    exceptions must not confirm a zero-packet emission as a success.

    Named so `-k systemic` selects this class.
    """

    def test_a_forbidden_identity_input_fails_the_chip_not_the_candidate(self):
        # ForbiddenIdentityInput means the code minting identities put a
        # forbidden key in the payload — a design defect, not a fact about
        # one candidate. It must propagate and fail the attempt rather than
        # being recorded as a drop.
        from alerts.identity import ForbiddenIdentityInput

        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0), Source(2, 8.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        def bad_assemble(prov, source):
            raise ForbiddenIdentityInput("path")

        with self.assertRaises(ForbiddenIdentityInput):
            _run_produce_alerts(context, provider, producer,
                                assemble=bad_assemble)

        # (b)-style: the claim is left intact for later recovery, and
        # nothing was outboxed — the same shape as a chip-level failure.
        self.assertEqual(conn.rows[(20, 7, "rel-1")]["state"], "claimed")
        self.assertEqual(conn.alert_outbox, {})

    def test_a_type_error_during_assembly_fails_the_chip(self):
        # finding 10's own precedent: a TypeError from a programming defect
        # (e.g. spreading the wrong dict into alert_identity's kwargs) must
        # not be swallowed as "this candidate's data was bad".
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        def broken_assemble(prov, source):
            raise TypeError("alert_identity() got multiple values for "
                            "argument 'legacy_pid'")

        with self.assertRaises(TypeError):
            _run_produce_alerts(context, provider, producer,
                                assemble=broken_assemble)

        self.assertEqual(conn.rows[(20, 7, "rel-1")]["state"], "claimed")
        self.assertEqual(conn.alert_outbox, {})

    def test_a_db_call_failure_during_assembly_fails_the_chip(self):
        # RapidDBCallFailed / RepositoryQueryFailed mean the connection or a
        # query is broken — a chip-wide fact, not a per-candidate one.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0), Source(2, 8.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        def broken_assemble(prov, source):
            raise RapidDBCallFailed("get_object_for_source", 67)

        with self.assertRaises(RapidDBCallFailed):
            _run_produce_alerts(context, provider, producer,
                                assemble=broken_assemble)

        self.assertEqual(conn.rows[(20, 7, "rel-1")]["state"], "claimed")
        self.assertEqual(conn.alert_outbox, {})

    def test_every_candidate_failing_identically_fails_the_chip(self):
        # The exact shape finding 10 describes: every selected candidate
        # fails via the ordinary per-candidate catch (a ValueError, still
        # candidate-shaped by type), but ALL of them do, uniformly — which
        # is itself evidence of a systemic problem the old code confirmed
        # as a clean, zero-packet success.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0), Source(2, 8.0),
                                     Source(3, 7.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        self.assertRaises(
            RuntimeError, _run_produce_alerts, context, provider, producer,
            fail_sids={1, 2, 3})

        # The claim is left intact, exactly like a chip-level failure —
        # NOT confirmed with alerts_outboxed=0.
        self.assertEqual(conn.rows[(20, 7, "rel-1")]["state"], "claimed")
        self.assertEqual(conn.alert_outbox, {})

    def test_a_partial_candidate_failure_still_confirms(self):
        # The uniform-failure guard must not fire when only SOME candidates
        # fail — that is ordinary gate-3 drop-and-continue, unchanged.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0), Source(2, 8.0),
                                     Source(3, 7.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        _run_produce_alerts(context, provider, producer, fail_sids={2})

        self.assertEqual(len(conn.alert_outbox), 2)
        self.assertTrue(context.provenance["emission_confirmed"])

    def test_an_all_oversize_chip_still_confirms(self):
        # The uniform-failure guard is scoped to the GENERIC per-candidate
        # catch only — an all-oversize chip stays the ratified "confirms
        # with zero outboxed" outcome (OversizePacketTests), never raised.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0, id=1)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)
        real_max = alert_production.MAX_PACKET_BYTES
        alert_production.MAX_PACKET_BYTES = 100
        try:
            _run_produce_alerts(
                context, provider, producer,
                serialize=lambda alert, schema=None: b"z" * 200)
        finally:
            alert_production.MAX_PACKET_BYTES = real_max

        self.assertTrue(context.provenance["emission_confirmed"])
        self.assertEqual(context.provenance["alerts_outboxed"], 0)


class ConfirmationObservabilityTests(unittest.TestCase):
    """Finding 11: a swallowed DB failure during CONFIRM is recorded and
    logged distinctly from an ordinary takeover — observability only, the
    self-healing (claim-stays-'claimed', later attempt retries) behaviour
    is unchanged.

    Named so `-k confirmation_db_failure` selects this class.
    """

    def test_a_confirm_db_failure_is_flagged_in_provenance(self):
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        # The CONFIRM statement itself raises — modelling a DB fault at
        # exactly the step finding 11 targets.
        original_confirm = conn._confirm

        def failing_confirm(params):
            raise RuntimeError("stubbed confirm failure")

        conn._confirm = failing_confirm

        _run_produce_alerts(context, provider, producer)

        conn._confirm = original_confirm  # restore, though unused after

        self.assertTrue(context.provenance["confirmation_db_failure"])
        self.assertFalse(context.provenance["emission_confirmed"])
        self.assertEqual(context.provenance["alerts_outboxed"], 0)
        # Self-healing UNCHANGED: nothing committed, claim stays 'claimed'.
        self.assertEqual(conn.rows[(20, 7, "rel-1")]["state"], "claimed")
        self.assertEqual(conn.alert_outbox, {})
        self.assertEqual(conn.milestones, [])

    def test_an_ordinary_takeover_is_not_flagged_as_a_db_failure(self):
        # The existing takeover path (no DB fault, just a losing race) must
        # NOT be flagged — the field distinguishes the two, not conflates
        # them.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        original_confirm = conn._confirm

        def confirm_after_takeover(params):
            key = (20, 7, "rel-1")
            conn.rows[key]["claim_token"] = "12345"
            return original_confirm(params)

        conn._confirm = confirm_after_takeover

        _run_produce_alerts(context, provider, producer)

        self.assertFalse(context.provenance["confirmation_db_failure"])
        self.assertFalse(context.provenance["emission_confirmed"])

    def test_a_clean_run_is_not_flagged(self):
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        _run_produce_alerts(context, provider, producer)

        self.assertFalse(context.provenance["confirmation_db_failure"])
        self.assertTrue(context.provenance["emission_confirmed"])


class AggregatePacketBudgetTests(unittest.TestCase):
    """Finding 16: a total-byte budget on accumulated packets, independent
    of `MAX_PACKET_BYTES`'s per-packet cap.

    `alert_production.MAX_TOTAL_PACKET_BYTES` is MONKEYPATCHED down, the
    same idiom `OversizePacketTests` uses for `MAX_PACKET_BYTES` — cheap
    and exercises the real comparison without real gigabytes on either
    side.
    """

    def setUp(self):
        self._real_max_total = alert_production.MAX_TOTAL_PACKET_BYTES

    def tearDown(self):
        alert_production.MAX_TOTAL_PACKET_BYTES = self._real_max_total

    def test_exceeding_the_aggregate_budget_fails_the_chip(self):
        # Three candidates, each individually well under MAX_PACKET_BYTES,
        # but their SUM crosses a small patched aggregate budget.
        alert_production.MAX_TOTAL_PACKET_BYTES = 25

        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0, id=1), Source(2, 8.0, id=2),
                                     Source(3, 7.0, id=3)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        with self.assertRaises(RuntimeError) as caught:
            _run_produce_alerts(
                context, provider, producer,
                serialize=lambda alert, schema=None: b"x" * 10)

        self.assertIn("aggregate budget", str(caught.exception))
        # Chip-level failure: claim left intact, nothing outboxed.
        self.assertEqual(conn.rows[(20, 7, "rel-1")]["state"], "claimed")
        self.assertEqual(conn.alert_outbox, {})

    def test_comfortably_under_the_aggregate_budget_confirms_normally(self):
        # The default budget is untouched here (not patched down), so
        # ordinary small test payloads stay comfortably under it.
        conn = FakeConn()
        provider = Provider(sources=[Source(1, 9.0), Source(2, 8.0)])
        producer = Producer()
        context = Context(_unit(), PARAMETERS, conn=conn)

        _run_produce_alerts(context, provider, producer)

        self.assertTrue(context.provenance["emission_confirmed"])
        self.assertEqual(context.provenance["alerts_outboxed"], 2)

    def test_the_default_budget_is_a_fraction_of_the_documented_worker_memory(self):
        # docs/source/ops/bulk_run.rst documents the shared science job
        # definition's machines as 16 GB; this stage's own budget must stay
        # a bounded fraction of that, not something larger than the worker
        # itself could ever satisfy.
        sixteen_gb = 16 * 1024 * 1024 * 1024
        self.assertGreater(alert_production.MAX_TOTAL_PACKET_BYTES, 0)
        self.assertLess(alert_production.MAX_TOTAL_PACKET_BYTES, sixteen_gb)


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
