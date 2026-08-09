"""
File:    alert_production.py

The alert-production job type — the trigger for the complete-but-unwired
alerts path.

What was already here: `alerts/` builds, serializes, publishes and archives
alerts, and has since it was written. What was missing was anything that
DECIDED to run it. `batch_produce(provider, pid, producer=...)` was the
injection seam with nothing on the injecting side; this module is that side.

**A DATABASE-EFFECT JOB TYPE, NOT A PRODUCT-PRODUCING ONE** (step-4 co-design
gate 3, operations design § Alert production). It declares an empty product
set: the alerts go to the stream, not to the product bucket, and the attempt's
terminal record is a pure disposition record. What the attempt records instead
is the alert-specific effect counts the design names by name — candidates
considered, alerts published, candidates dropped by reason, emissions
suppressed — plus per-candidate drop dispositions.

**CANDIDATE FAILURES NEVER FAIL THE ATTEMPT** (gate 3). A cutout that will not
load, a source whose object lookup returns nothing: these drop that candidate,
recorded with its reason, and the other candidates from the same chip publish
normally. The attempt fails only on CHIP-level failure — the provider cannot
open the difference image at all — and on delivery failure, which raises
loudly because a run that reports published alerts while publishing nothing is
worse than one that crashes (the 2026-08-04 Q7 finding, which is why
`GlueFramingProducer.flush` raises).

**EMISSION COMMIT SEMANTICS: CLAIM -> PUBLISH/FLUSH -> CONFIRM** (integration
review 2026-08 composite ruling 3, migration 037; supersedes the earlier
claim-before-publish watermark this module described up to that ruling). The
previous protocol claimed the watermark, published, and stopped: any failure
between the committed claim and delivery permanently suppressed the unit's
alert, because a claim WAS a confirmed emission — there was no representable
state in between. Migration 037 adds the missing middle state (`claimed`,
transient), and this module now writes it as three separate steps:

1. **CLAIM** — a CAS insert/update against `alert_emissions`' primary key
   (exposure_id, sca, release_identity), committed in its OWN transaction
   BEFORE any publishing starts. Proceeds only when the claim returns THIS
   attempt's own `claim_token`; a claim that returns nothing, or a different
   attempt's token, is a recorded no-op — not a failure — because the unit is
   either terminally suppressed already or genuinely owned by a live
   claimant.
2. **PUBLISH** — assembly and serialization stay PER CANDIDATE, inside the
   drop-and-continue catch (gate 3: a bad cutout drops that candidate, not
   the chip); the producer send and the flush are CHIP-LEVEL, outside every
   per-candidate catch. A candidate failure was always meant to be
   independent of delivery; a delivery failure was always meant to fail the
   attempt (the 2026-08-04 Q7 finding stands). If publishing raises, the
   claim is left `claimed` — untouched — for a later attempt to confirm or
   take over; it is never rolled back to unclaimed, because the at-least-once
   contract's cost is a possible duplicate, never a silently abandoned claim
   nobody retries.
3. **CONFIRM** — a second CAS update, gated on this attempt still owning the
   claim (`claim_token` unchanged since step 1), in the SAME transaction as
   the `alert_published` milestone write (integration ruling 6): a crash
   cannot confirm an emission without the milestone recording it, or vice
   versa. A confirm that finds the claim taken over (zero rows) is a
   recorded no-op: the takeover attempt republishes, and consumers
   deduplicate on alert identity — the at-least-once contract's accepted
   cost, never treated as this attempt's failure.

**THE CLAIM, THE CONFIRM, AND THE MILESTONE ALL WRITE THROUGH THE BORROWED
CONNECTION** (`context.require_connection()`), never through `provider.db` —
`provider.db` is `alerts.cli.make_provider()`'s OWN, separate `RAPIDDB()`
connection, opened purely to read candidates and cutouts. Writing the
emission state through it would put the CAS on a connection with no relation
to this attempt's own lifecycle transaction, and — because it autocommits
per call — no way to make CONFIRM and the milestone atomic at all.
"""

import logging

from database.modules.utils.checked import CheckedHandle, RapidDBCallFailed
from database.modules.utils.rapid_db import RAPIDDB
from database.modules.utils.rapid_db_connect import ConnectionExecutor, transaction
from pipeline.runtime.errors import InputError

logger = logging.getLogger(__name__)

#: The claim CAS's own staleness threshold, restated here ONLY as
#: documentation (the actual SQL literal lives in
#: `RAPIDDB.claim_alert_emission` and `get_attempts_awaiting_alert_emission`,
#: matching migration 037's `derived.alert_emission_status` view — "keep the
#: two in sync by inspection until a shared parameter home exists").
CLAIM_STALENESS = "interval '1 hour'"

#: The internal-topic PREFIX this job type is allowed to publish under.
#:
#: The topic itself is a parameter (`kafka/topic`), not a constant here — one
#: home per fact, and the parameter tree is that home. What is hardcoded is
#: the namespace GUARD: the mission/public stream must not be reachable from
#: this job type even by reconfiguration, and the publication policy grants
#: `rapid.internal.alerts.*` (plus `rapid.test.*`) and nothing else. A
#: parameter edit that pointed this at a public topic would otherwise be a
#: one-line change with no code review of the blast radius.
INTERNAL_TOPIC_PREFIXES = ("rapid.internal.", "rapid.test.")

#: The PLACEHOLDER selection, labelled as one.
#:
#: The co-design records the selection rule as a placeholder — top N candidates
#: per SCA by signal-to-noise — and it is implemented as exactly that, with the
#: name saying so at every reference. Real selection is a science decision
#: (deterministic cuts, real-bogus classification) that the failure-path design
#: already anticipates as a distinct stage; nothing here should be mistaken for
#: it, and a reader who greps for "placeholder" must find this.
PLACEHOLDER_TOP_N_BY_SNR = 500


def _unit_field(context, name):
    """One declared field of the unit, or a named failure.

    Same contract as the post-DB chain's `_unit_field`: the unit is what the
    manifest says, and a stage that cannot find its declared input fails
    naming it rather than rediscovering it from the catalog.
    """
    fields = getattr(context.unit, "fields", None) or {}
    value = fields.get(name)
    if value is None:
        raise InputError(
            f"the alert-production unit does not declare {name!r}; it is a "
            f"submission fact and this stage has no way to derive one")
    return value


def select_candidates(sources, top_n=PLACEHOLDER_TOP_N_BY_SNR):
    """The PLACEHOLDER selection: the top `top_n` candidates by S/N.

    Named `PLACEHOLDER` in its constant, its docstring and the effect counts
    it produces, because that is what the co-design ruled it to be. It is a
    volume bound with a defensible ordering, not a scientific selection: it
    makes the internal phase's packet-count measurement bounded and
    reproducible while the real cuts are designed.

    Sources whose S/N is unknown sort LAST rather than being dropped. A missing
    measurement is not a low one, and dropping on absence would silently make
    the placeholder a filter on data completeness as well as on brightness.
    """
    ordered = sorted(
        sources,
        key=lambda source: (getattr(source, "snr", None) is None,
                            -(getattr(source, "snr", None) or 0.0)))
    return ordered[:top_n]


def produce_alerts(context) -> None:
    """Publish this unit's alerts, once, and record what happened.

    Three steps, three distinct commit points — CLAIM (own transaction,
    first), PUBLISH (no commit of its own; a candidate that assembles cleanly
    is sent but not yet confirmed), CONFIRM+MILESTONE (one transaction,
    last). See the module docstring for why the three cannot collapse back
    into one without reopening the at-most-once loss this protocol replaces.
    """
    from alerts.cli import make_provider
    from alerts.produce import (assemble_alert_for_source, load_schema,
                                publish_alert, serialize_alert)
    # Lazy, matching `pipeline/stages/science.py`'s own milestone-writer
    # import and `pipeline/entrypoints/job.py`'s `_database` — no stage
    # imports `observability.attempts` at module scope.
    from observability.attempts import AttemptWriter

    pid = int(_unit_field(context, "difference_image_pid"))
    # The REGISTERED SOURCE attempt — the promotion that made this unit
    # eligible (migration 037's `alert_emissions.attempt_id`). Distinct from
    # this attempt's OWN identity below, which is the CLAIM identity.
    source_attempt_id = int(_unit_field(context, "attempt_id"))
    release_identity = str(_unit_field(context, "release_identity"))
    exposure = int(context.unit.exposure)
    sca = int(context.unit.sca)

    # THIS ATTEMPT'S OWN IDENTITY — the claiming attempt (migration 037's
    # `alert_emissions.claim_token`), from the entrypoint's own resolved
    # ownership, never from the unit's declared fields (those name the
    # SOURCE attempt, a different fact — see the module docstring).
    claiming_attempt_id = int(context.attempt_id)
    claim_token = str(claiming_attempt_id)

    conn = context.require_connection()

    # The provider reads through THIS attempt's own resolved connection —
    # `make_provider()`'s no-argument path builds an env-configured
    # `RAPIDDB()`, and a Batch payload carries no DBSERVER/DBPORT/DBNAME:
    # every alert job exited 64 at this line on the mock's first wave (the
    # env-only-contract class again; the connection facts live in the
    # parameter tree the entrypoint already resolved).
    provider = make_provider(db=RAPIDDB.borrowing(conn))
    # Adapter-mediated (integration review composite ruling 10): every call
    # below either returns cleanly or raises `RapidDBCallFailed`, so a failed
    # query can never be read as "no row" and fall through to an unguarded
    # publish.
    emissions = CheckedHandle(RAPIDDB.borrowing(conn))

    # STEP 1: THE CLAIM, in its own transaction, committed BEFORE any
    # publishing starts (integration ruling 3: "a crash after a committed
    # claim leaves a stale-recoverable claim, never a suppression").
    #
    # `transaction(conn)` wraps the commit/rollback boundary around whatever
    # runs inside the `with` block; it does not need its own cursor to do
    # that — Postgres transactions are connection-scoped, not cursor-scoped,
    # so `emissions.cur.execute(...)` (via `claim_alert_emission`, on the
    # SAME `conn`) participates in this transaction regardless of which
    # cursor object issued it. The `with` block's own cursor (`_`) is
    # unused; it exists to open and close the block.
    try:
        with transaction(conn) as _:
            won_token = emissions.claim_alert_emission(
                exposure, sca, release_identity, source_attempt_id,
                claiming_attempt_id, claim_token, pid=pid)
    except RapidDBCallFailed as exc:
        raise RuntimeError(
            f"could not claim the emission for unit {exposure}/{sca} "
            f"release {release_identity}: {exc}") from exc

    if won_token != claim_token:
        # A NULL return, or someone else's token: either the unit is already
        # terminally suppressed (watermark_seed/emitted) or a live claimant
        # holds it. Either way this attempt publishes nothing. Recorded as a
        # suppression, one of the four effect counts the design names, and
        # closes successfully — never a failure.
        context.record_effect(
            candidates_considered=0, alerts_published=0,
            emissions_suppressed=1,
            suppression_reason=(
                "already emitted, or claimed by a live attempt, under this "
                "release"),
            alert_release_identity=release_identity,
            alert_difference_image_pid=pid)
        context.logger.info(
            "unit %s/%s not claimed by this attempt under release %s "
            "(won_token=%r); publishing nothing",
            exposure, sca, release_identity, won_token)
        return

    topic = _internal_topic(context)
    producer = _make_internal_producer(context)
    schema = load_schema()

    considered = 0
    published = 0
    dropped_by_reason: dict = {}
    drop_dispositions: list = []

    try:
        sources = list(provider.iter_sources(pid))
    except Exception as exc:  # noqa: BLE001
        # CHIP-LEVEL failure: the provider could not read this difference
        # image at all. This is one of the two cases that fails the attempt
        # per the failure-path design's SCA-level scope (the other being
        # delivery, below) — the claim is left `claimed`, untouched, for a
        # later attempt to confirm or take over.
        raise RuntimeError(
            f"could not read candidates for difference image pid {pid}: "
            f"{exc}") from exc

    selected = select_candidates(sources)
    considered = len(selected)

    # STEP 2: PUBLISH. Candidate scope is assembly + serialization ONLY
    # (integration ruling 3) — each candidate's own catch stops there.
    # Producer construction (above), topic resolution (above), auth, send,
    # and flush are CHIP-LEVEL: outside every per-candidate catch, so a
    # producer/broker failure raises loudly rather than recording as a
    # candidate drop (the 2026-08-04 Q7 finding this module has named since
    # it was written).
    for source in selected:
        sid = getattr(source, "sid", None)
        try:
            alert = assemble_alert_for_source(provider, source)
            payload = serialize_alert(alert, schema=schema)
        except Exception as exc:  # noqa: BLE001 - a candidate, not the chip
            # PER-CANDIDATE DROP (gate 3). The reason is the exception's type
            # rather than its message: the counts are grouped by reason and a
            # message carrying a source id would make every drop its own
            # category.
            reason = type(exc).__name__
            dropped_by_reason[reason] = dropped_by_reason.get(reason, 0) + 1
            drop_dispositions.append({"sid": sid, "reason": reason,
                                      "detail": str(exc)[:200]})
            context.logger.warning("candidate sid=%s dropped (%s): %s",
                                   sid, reason, exc)
            continue
        # CHIP-LEVEL from here: send is not wrapped in the per-candidate
        # catch. A send failure (as opposed to a delayed delivery failure
        # `flush` reports) is a producer/broker fault, not this candidate's.
        publish_alert(payload, producer, topic=topic, flush=False)
        published += 1

    # DELIVERY failure raises loudly — flush() reports what send() deferred.
    # The claim stays `claimed`: this attempt's confirm below never runs, and
    # a later attempt (retry or takeover) republishes and confirms — the
    # at-least-once contract's accepted cost, never a silent loss.
    producer.flush()

    # STEP 3: CONFIRM + THE alert_published MILESTONE, in ONE transaction
    # (integration ruling 3 / 6: "Emission confirmation and the
    # alert-published milestone commit in one transaction, so a crash cannot
    # confirm an emission without the milestone recording it").
    try:
        with transaction(conn) as _:
            confirmed_token = emissions.confirm_alert_emission(
                exposure, sca, release_identity, claim_token, published)
            if confirmed_token == claim_token:
                writer = AttemptWriter(
                    ConnectionExecutor(conn, autocommit_each=False))
                writer.record_milestone(
                    "alert_published", _utcnow(), exposure_id=exposure,
                    sca=sca, producing_attempt_id=claiming_attempt_id)
    except RapidDBCallFailed as exc:
        # The alerts are published; failing the attempt now would misreport a
        # successful emission as a failure and invite a retry that would
        # republish. Logged loudly instead — the attempt's own effect counts
        # still carry the number, and the claim (still 'claimed') is
        # recoverable by a later confirm or takeover.
        logger.warning(
            "published %d alert(s) for %s/%s but could not confirm the "
            "emission (claim left 'claimed' for later recovery): %s",
            published, exposure, sca, exc)
        confirmed_token = None

    if confirmed_token != claim_token:
        context.logger.warning(
            "unit %s/%s: claim was taken over between publish and confirm "
            "(or the confirm failed); %d alert(s) were published but this "
            "attempt's confirmation is a recorded no-op — the takeover "
            "republishes, consumers deduplicate", exposure, sca, published)

    context.record_effect(
        candidates_considered=considered,
        alerts_published=published,
        candidates_dropped=sum(dropped_by_reason.values()),
        dropped_by_reason=dropped_by_reason,
        drop_dispositions=drop_dispositions,
        emissions_suppressed=0,
        emission_confirmed=(confirmed_token == claim_token),
        alert_topic=topic,
        alert_release_identity=release_identity,
        alert_difference_image_pid=pid,
        selection_rule=f"PLACEHOLDER top-{PLACEHOLDER_TOP_N_BY_SNR}-by-snr",
        sources_available=len(sources))
    context.logger.info(
        "unit %s/%s: %d candidate(s) considered, %d published, %d dropped, "
        "confirmed=%s (release %s, topic %s)",
        exposure, sca, considered, published,
        sum(dropped_by_reason.values()), confirmed_token == claim_token,
        release_identity, topic)


def _utcnow():
    """The confirm moment, for the `alert_published` milestone's `reached_at`.

    A fresh timestamp rather than a value threaded through from the CONFIRM
    statement's own `now()`: the milestone and the confirm write in the same
    transaction but not the same statement, and `design/observability.md`
    ties `alert_published` to "emission confirmation" as a moment, not to the
    UPDATE's own server-side clock read specifically — a wall-clock read
    here, a few microseconds before the UPDATE's own `now()`, is close enough
    that no consumer of this milestone could tell the difference, and it
    keeps the milestone writer independent of the confirm statement's return
    shape.
    """
    import datetime
    return datetime.datetime.now(datetime.timezone.utc)


def _internal_topic(context):
    """This deployment's alert topic, refused unless it is an internal one.

    The name comes from the parameter tree (`kafka/topic`), which is its one
    home. The PREFIX check is the guard: the step-4 internal phase publishes
    to internal topics only, and the mission/public stream is out of scope
    entirely. Refusing here means a parameter pointing at a public topic
    fails the attempt loudly rather than publishing simulation alerts onto a
    stream consumers trust.
    """
    topic = context.parameter("kafka/topic")
    if not topic:
        raise InputError(
            "the parameter tree does not carry kafka/topic; the "
            "alert-production job type publishes to the internal topic and "
            "has no default")
    if not any(topic.startswith(prefix) for prefix in INTERNAL_TOPIC_PREFIXES):
        raise InputError(
            f"kafka/topic is {topic!r}, which is not an internal topic. The "
            f"alert-production job type publishes under "
            f"{' or '.join(INTERNAL_TOPIC_PREFIXES)} only; the mission "
            f"stream is not reachable from this job type")
    return topic


def _make_internal_producer(context):
    """The real Kafka producer, on the internal topic.

    THE INJECTION SEAM MADE REAL. `batch_produce`'s `producer=` argument has
    always accepted one; nothing constructed it outside the CLI. The brokers
    are a PARAMETER, not an environment variable read at import: the
    environment policy puts nothing that selects a destination in the
    environment, and a misread broker would publish to the wrong cluster
    silently.

    The request-size cap is NOT set here. `make_transport`'s own default
    (15728640) already equals the tree's `kafka/max-request-bytes`, and
    `make_producer` does not forward the keyword — passing it would be a
    TypeError at the first real publication. If the two ever need to differ,
    the forwarding is the change to make, not a second value invented here.
    """
    from alerts.kafka_producer import make_producer

    brokers = context.parameter("kafka/bootstrap-servers")
    if not brokers:
        raise InputError(
            "the parameter tree does not carry kafka/bootstrap-servers; the "
            "alert-production job type publishes to the internal topic and "
            "has no default broker")
    return make_producer(brokers)


#: The job type's sequence — one stage, for the reason `produce_alerts` states.
ALERT_PRODUCTION_SEQUENCE = (
    ("produce_alerts", produce_alerts),
)
