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

**THIS JOB TYPE NO LONGER SENDS ANYTHING** (brief E, rule 14). Until package E
this module constructed a live Kafka producer and sent in-job, which put
delivery inside the Batch job's lifetime: a broker outage failed science
attempts that had already done their science, and "identical bytes on resend"
could not be true because a resend was whatever the next attempt happened to
re-serialize. The job's obligation now ENDS AT THE OUTBOX — it assembles,
serializes, computes each packet's identity, and commits `alert_outbox` rows in
the confirmation transaction. `rapid-publisher` (`pipeline/publisher/`) owns
the wire from there, and is the only component in the tree that constructs a
producer at all; `pipeline/contract/test_alert_send_routes.py` asserts that
repo-wide.

**EMISSION COMMIT SEMANTICS: CLAIM -> ASSEMBLE -> CONFIRM+OUTBOX** (integration
review 2026-08 composite ruling 3, migration 037, as re-targeted by brief E;
supersedes the earlier claim-before-publish watermark this module described up
to that ruling). The
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
2. **ASSEMBLE** — assembly, serialization and identity computation stay PER
   CANDIDATE, inside the drop-and-continue catch (gate 3: a bad cutout drops
   that candidate, not the chip). Each surviving candidate also has its framed
   size CHECKED against `MAX_PACKET_BYTES` here: an oversize packet is an
   AUDITABLE DROP with its own reason, recorded like any other drop and
   discarded BEFORE the confirmation transaction — never a row committed to
   the outbox that the publisher could not send, and never a stranded claim.
   Nothing in this step touches a network.
3. **CONFIRM + OUTBOX** — the ALERT-EFFECT CONFIRMATION transaction, and the
   fixed order inside it is load-bearing (brief E):

       confirm CAS  ->  verify this attempt still owns the claim  ->
       insert outbox rows  ->  write the milestone

   The confirm CAS can affect ZERO rows without raising — a takeover is a
   recorded no-op — so a losing claimant that inserted outbox rows first
   would leave packets behind for an emission it did not confirm, and the
   publisher would deliver them. A losing claimant therefore commits NEITHER
   outbox rows NOR the milestone. All three effects are one transaction
   (integration ruling 6, extended by rule 14): a crash cannot confirm an
   emission without the packets, or write packets without the milestone.

   This is rule 14's "same transaction as the database effect that produced
   them", and it is deliberately NOT rule 9's result-acceptance transaction —
   the attempt's terminal record and closure happen later, in the termination
   protocol (`pipeline/entrypoints/job.py`, `pipeline/runtime/termination.py`),
   and the registration consumer that owns that transaction cannot construct
   these packets: it has no provider, no cutouts and no schema. The remaining
   rule-9 architectural gap is recorded in DRAFT 050's header rather than
   papered over here.

   TWO LEVELS OF IDEMPOTENCY, NOT CONFLATED. The chip-level
   `alert_emissions` fence (this claim/confirm CAS) decides whether this
   ATTEMPT may emit at all; `alert_outbox.alert_id`'s UNIQUE constraint
   decides whether a PACKET is already there. A re-run after a lost response
   re-claims through the first and writes no duplicate rows through the
   second — the digest is deterministic, so the re-run recomputes the same
   ids and the insert path absorbs them as idempotent. Neither mechanism
   substitutes for the other: the fence cannot tell packets apart, and
   uniqueness cannot tell whether an emission was authorized.

**THE CLAIM, THE CONFIRM, THE OUTBOX ROWS AND THE MILESTONE ALL WRITE THROUGH
THE BORROWED CONNECTION** (`context.require_connection()`), never through
`provider.db` — `provider.db` is `alerts.cli.make_provider()`'s OWN, separate
`RAPIDDB()` connection, opened purely to read candidates and cutouts. Writing
the emission state through it would put the CAS on a connection with no
relation to this attempt's own lifecycle transaction, and — because it
autocommits per call — no way to make CONFIRM, the packets and the milestone
atomic at all.

**THE OUTBOX WRITES GO THROUGH A CARVED REPOSITORY** — `pipeline/repositories/
alert_outbox.py`, over that same borrowed connection, so they participate in
the confirmation transaction exactly as the CAS does. `RAPIDDB` is FROZEN
(brief G's ratified merge decision; target rule 17 puts new access behind
narrow typed repositories), and the emission CAS above is reached through it
only because those methods predate the freeze. Two failure vocabularies
therefore meet in this module — `RapidDBCallFailed` from the legacy handle's
`exit_code` convention, `RepositoryQueryFailed` from the repository — and the
confirmation path catches both, because a failed packet insert and a failed
confirm are the same event to this stage.
"""

import logging

# The framing overhead the size check must account for. Imported rather than
# restated: the header is `alerts/kafka_producer.py`'s definition and a second
# copy of its length here would silently disagree if the format ever changed.
# Module-scope because it is a constant, not a collaborator — every other
# `alerts` import in this module is deliberately lazy (see `produce_alerts`).
from alerts.identity import ForbiddenIdentityInput
from alerts.kafka_producer import GLUE_HEADER_LEN
from database.modules.utils.checked import CheckedHandle, RapidDBCallFailed
from database.modules.utils.rapid_db import RAPIDDB
from database.modules.utils.rapid_db_connect import (ConnectionExecutor,
                                                      DBError, transaction)
from pipeline.repositories.alert_outbox import AlertOutboxRepository
from pipeline.repositories.errors import RepositoryQueryFailed
from pipeline.runtime.errors import InputError

logger = logging.getLogger(__name__)

#: Exceptions the per-candidate catch below must NEVER absorb as a per-
#: candidate drop, because each is a SYSTEMIC signal rather than a fact about
#: one candidate's data: a database fault (`DBError`, `RapidDBCallFailed`,
#: `RepositoryQueryFailed`) means the connection or a query is broken for
#: every candidate on the chip, not just this one; `ForbiddenIdentityInput`
#: means the code minting identities put a forbidden key in the payload — a
#: design defect that reproduces identically for every candidate; and a bare
#: `TypeError`/`AttributeError`/`NameError` is a programming error (finding
#: 10's own precedent: spreading the wrong dict into `alert_identity`'s
#: kwargs raised `TypeError` on every candidate, and the old blanket catch
#: swallowed it, confirming the emission with zero packets outboxed). All of
#: these must fail the chip loudly rather than be recorded as gate 3's
#: per-candidate drop-and-continue.
SYSTEMIC_EXCEPTIONS = (DBError, RapidDBCallFailed, RepositoryQueryFailed,
                       ForbiddenIdentityInput, TypeError, AttributeError,
                       NameError)

#: The claim CAS's own staleness threshold, restated here ONLY as
#: documentation (the actual SQL literal lives in
#: `RAPIDDB.claim_alert_emission` and `get_attempts_awaiting_alert_emission`,
#: matching migration 037's `derived.alert_emission_status` view — "keep the
#: two in sync by inspection until a shared parameter home exists").
CLAIM_STALENESS = "interval '1 hour'"

#: The internal-topic PREFIX an alert may be destined for.
#:
#: THE GUARD MOVED TO THE PUBLISHER (brief E2) and this constant moved with the
#: reason for it. The namespace rule is unchanged — the mission/public stream
#: must not be reachable even by reconfiguration, and the publication policy
#: grants `rapid.internal.alerts.*` (plus `rapid.test.*`) and nothing else —
#: but after E this job type has no send to guard: it writes a `topic` into
#: each outbox row and `rapid-publisher` checks that stored value immediately
#: before every send, which is the last point where the check still means
#: something.
#:
#: It is enforced HERE TOO, at outbox-write time, and deliberately so: the
#: topic is part of the row's write-once envelope, so a packet written for a
#: forbidden topic could never be corrected — only refused. Refusing to WRITE
#: it fails the attempt loudly at the point where the parameter was misread,
#: which is where an operator can act on it. Two checks, one rule, neither
#: redundant: this one prevents the row, the publisher's prevents the send.
INTERNAL_TOPIC_PREFIXES = ("rapid.internal.", "rapid.test.")

#: The largest framed packet this job type will commit to the outbox.
#:
#: `make_transport`'s producer cap is 15728640 (15 MiB) and equals the tree's
#: `kafka/max-request-bytes`; a packet at or over it cannot be sent, so
#: committing one would strand a row the publisher can only ever REFUSE. The
#: bound here is deliberately UNDER that cap — a packet's wire form carries the
#: Glue header on top of what is measured, brokers apply their own per-message
#: overhead, and a packet that is exactly at the limit locally is the one that
#: fails remotely.
#:
#: 12 MiB against a typical ~200 KB packet (cutouts dominate: 129x129 stamps,
#: `alerts/providers.py`) is roughly sixty times the ordinary size, so this
#: bound is not a tuning knob for normal traffic — it is a guard against a
#: pathological packet, and crossing it is an auditable drop with its own
#: recorded reason rather than a silent truncation or a stranded row.
MAX_PACKET_BYTES = 12 * 1024 * 1024

#: The drop reason an oversize packet is recorded under. A NAMED CONSTANT
#: because it is a vocabulary term the effect counts group by, and a reason
#: spelled differently in two places would split one category into two.
DROP_REASON_OVERSIZE = "OversizePacket"

#: The largest TOTAL size the in-memory `packets` accumulator may reach
#: across one chip's assembly loop (finding 16), independent of
#: `MAX_PACKET_BYTES`'s per-packet cap.
#:
#: `MAX_PACKET_BYTES` bounds one packet; nothing previously bounded their
#: SUM. Up to `PLACEHOLDER_TOP_N_BY_SNR` (500) packets accumulate in memory
#: between STEP 2 and the STEP 3 confirmation transaction (by design — see
#: the module docstring's "nothing in this loop writes"), so 500 candidates
#: each near the 12 MiB per-packet cap is ~6 GiB permitted before this
#: guard, on a worker with no budget of its own to appeal to.
#:
#: `JOB_TYPE_ALERT_PRODUCTION` is routed onto `batch/job-definition-science`
#: (`submission/routes.py`), the same job definition `JOB_TYPE_SCIENCE`
#: uses, and `docs/source/ops/bulk_run.rst` documents that job definition's
#: machines as 4 vCPUs / 16 GB memory / 20 GB disk. This bound is set to a
#: fraction of that 16 GB — not a number invented from nothing, but not a
#: number to trust exactly either: it is the ONLY worker-sizing figure this
#: repo documents for the job definition this job type actually runs under,
#: never one written specifically for alert-production's own footprint, so
#: the fraction below is deliberately conservative rather than tuned tight
#: against it. 1/8 of 16 GB leaves the rest for the provider's own
#: connections, fastavro/schema machinery, cutout buffers read and released
#: per candidate, and ordinary Python/interpreter overhead running in the
#: same process — none of which this guard can see or bound.
#:
#: Ordinary traffic is nowhere near this: 500 packets at the ~200 KB typical
#: size the `MAX_PACKET_BYTES` comment cites is ~100 MB, roughly 1/20th of
#: this budget. Like `MAX_PACKET_BYTES`, this is a guard against a
#: pathological run (many packets each near the per-packet cap), not a
#: tuning knob for normal traffic.
MAX_TOTAL_PACKET_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB (1/8 of the 16 GB
                                                  # science job definition)

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
    """One declared component of the unit's TYPED payload, or a failure.

    Same contract as the post-DB chain's `_unit_field`: the unit is what the
    manifest says, and a stage that cannot find its declared input fails
    naming it rather than rediscovering it from the catalog.

    Reads the payload rather than the retired open `fields` dict (rule 11).
    The two helpers stay separate rather than being factored into one,
    matching what was here before: they raise different exceptions with
    different wording for two different chains, and a shared helper would
    have to take both as parameters to say anything useful.
    """
    payload = getattr(context.unit, "payload", None)
    if payload is None:
        raise InputError(
            "the alert-production unit carries no typed payload; manifest "
            "schema version 4 refuses the pre-rule-11 `fields` shape rather "
            "than translating it")
    value = getattr(payload, name, None)
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
    from alerts.identity import alert_identity, payload_checksum
    from alerts.produce import (assemble_alert_for_source, load_schema,
                                serialize_alert)
    # Lazy, matching `pipeline/stages/science.py`'s own milestone-writer
    # import and `pipeline/entrypoints/job.py`'s `_database` — no stage
    # imports `observability.attempts` at module scope.
    from observability.attempts import AttemptWriter

    pid = int(_unit_field(context, "difference_image_pid"))
    # The REGISTERED SOURCE attempt — the promotion that made this unit
    # eligible (migration 037's `alert_emissions.attempt_id`). Distinct from
    # this attempt's OWN identity below, which is the CLAIM identity.
    source_attempt_id = int(_unit_field(context, "promoted_attempt_id"))
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
    # THE OUTBOX WRITES GO THROUGH A CARVED REPOSITORY, not through the handle
    # above. `RAPIDDB` is frozen (brief G's ratified merge decision; target
    # rule 17 puts new access behind narrow typed repositories), so the two
    # calls brief E adds — the packet insert and the identity-basis lookup —
    # live in `pipeline/repositories/alert_outbox.py` beside D's products and
    # F's association repositories. Same connection, so they participate in
    # this attempt's transactions exactly as the emission CAS does; different
    # failure vocabulary, which is the point of the carve.
    outbox = AlertOutboxRepository(conn)

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
            # `alerts_outboxed`, matching the confirmed path's own count — the
            # suppression arm kept the retired `alerts_published` name and so
            # reported this attempt's outcome under a different key from every
            # other outcome, which is exactly the drift the rename was meant to
            # end.
            candidates_considered=0, alerts_outboxed=0,
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
    schema = load_schema()
    # The pinned schema-version UUID, resolved ONCE per chip and stored on
    # every packet this attempt writes. Resolved HERE, at outbox-write time,
    # rather than by the publisher at send time: that is the whole mechanism
    # behind "identical bytes on resend" — see `_pinned_schema_version`.
    schema_version_id = _pinned_schema_version(context, topic)
    # Which identity basis this chip's packets use, decided once: the image is
    # the same for every candidate on it. The NAME (for the outbox column and
    # the effect record) is kept apart from the KWARGS (`alert_identity`'s
    # image argument) — see the call site below for what conflating them cost.
    image_basis = _image_identity(outbox, pid, context)
    image_kwargs = {key: value for key, value in image_basis.items()
                    if key != "basis_name"}

    considered = 0
    outboxed = 0
    dropped_by_reason: dict = {}
    drop_dispositions: list = []
    packets: list = []
    #: Count of candidates dropped by the GENERIC per-candidate catch below
    #: (assembly/serialization/identity raised something candidate-shaped),
    #: as distinct from the oversize-packet drop (its own explicit, sized
    #: check, never a symptom of a broken candidate). Only THIS count feeds
    #: the uniform-failure guard after the loop — an all-oversize chip is a
    #: legitimate, auditable "nothing to write" outcome (criterion 8) and
    #: must keep confirming, exactly as before; an all-*-Exception chip is
    #: the shape finding 10 describes and must not.
    candidate_exception_drops = 0
    #: Running total of `packets`' payload bytes, checked against
    #: `MAX_TOTAL_PACKET_BYTES` as each packet is appended (finding 16).
    #: Tracked incrementally rather than summed at the end so the guard can
    #: stop accumulating AT the crossing, not after 500 candidates' worth of
    #: payloads are already resident.
    total_packet_bytes = 0

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

    # STEP 2: ASSEMBLE. Candidate scope is assembly, serialization, identity
    # and the size check — no network, nothing durable. Each candidate's own
    # catch stops here (gate 3: a bad cutout drops that candidate, not the
    # chip), and NOTHING in this loop writes: the packets accumulate in memory
    # and are committed together, once, by the confirmation transaction below.
    # That is what makes "a crash before CONFIRM leaves no outbox rows" true by
    # construction rather than by care.
    for source in selected:
        sid = getattr(source, "sid", None)
        try:
            alert = assemble_alert_for_source(provider, source)
            payload = serialize_alert(alert, schema=schema)
            # THE CATALOG KEY, NOT `sid` (brief E's fixed reading). `sid` is
            # DB-generated at catalog load and realization-local; `(id,
            # isdiffpos)` is the catalogue's own conflict identity.
            # `basis_name` is THIS module's label for the row's column and the
            # effect record; it is not one of `alert_identity`'s parameters,
            # and the identity's own basis discriminator is derived inside the
            # digest from which image kwarg was given. Spreading the whole dict
            # raised TypeError on every candidate — a SYSTEMIC failure the old
            # blanket catch below swallowed, so every chip outboxed nothing
            # while reporting a clean run. Caught by the stage tests before
            # this branch shipped; the two are kept apart here so the mistake
            # cannot recur silently, and `SYSTEMIC_EXCEPTIONS` (finding 10)
            # is what now keeps a TypeError of this shape from being absorbed
            # a second time by a different bug.
            packet_id, _identity_payload = alert_identity(
                catalog_id=getattr(source, "id", None),
                isdiffpos=getattr(source, "isdiffpos", None),
                release_identity=release_identity,
                **image_kwargs)
        except SYSTEMIC_EXCEPTIONS:
            # NOT A CANDIDATE FAILURE — a database fault, a broken repository
            # call, a design defect in identity minting, or a programming
            # error. Every one of these means the chip's OTHER candidates are
            # in exactly the same jeopardy, so re-raising and failing the
            # attempt is correct; absorbing it here is what let finding 10's
            # defect confirm a zero-packet emission as a clean success.
            raise
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
            candidate_exception_drops += 1
            continue

        # THE SIZE CHECK, BEFORE THE CONFIRM TRANSACTION (brief E2). Measured
        # on the FRAMED size — header included — because that is what the
        # producer's request cap applies to. An oversize packet is an
        # auditable drop with its own reason: the chip's other candidates
        # emit normally, and no row is committed that the publisher could only
        # ever refuse.
        framed_size = len(payload) + GLUE_HEADER_LEN
        if framed_size > MAX_PACKET_BYTES:
            dropped_by_reason[DROP_REASON_OVERSIZE] = (
                dropped_by_reason.get(DROP_REASON_OVERSIZE, 0) + 1)
            drop_dispositions.append({
                "sid": sid, "reason": DROP_REASON_OVERSIZE,
                "detail": (f"framed packet is {framed_size} bytes, over the "
                           f"{MAX_PACKET_BYTES}-byte bound (producer request "
                           f"cap is 15728640)"),
                "alert_id": packet_id, "bytes": framed_size})
            context.logger.error(
                "candidate sid=%s dropped: framed packet %d bytes exceeds the "
                "%d-byte bound; not outboxed (alert_id would have been %s)",
                sid, framed_size, MAX_PACKET_BYTES, packet_id)
            continue

        # THE AGGREGATE BUDGET, BEFORE APPENDING (finding 16). The per-packet
        # check above bounds one packet; nothing previously bounded their
        # SUM, and up to `PLACEHOLDER_TOP_N_BY_SNR` (500) packets accumulate
        # in memory between here and the STEP 3 confirmation transaction by
        # design (see the module docstring). Checked against the SAME framed
        # size the per-packet cap uses, so the two guards agree on what
        # "size" means. A CHIP-LEVEL failure, not a per-candidate drop: the
        # accumulator being over budget is a fact about the SUM, not about
        # this one candidate that happened to cross it, and gate 3's
        # drop-and-continue accounting assumes each drop is independent —
        # which this explicitly is not. The claim is left `claimed`,
        # untouched, exactly like the other chip-level failures above; a
        # later attempt (retried with a narrower selection, or once the
        # pathological chip is understood) can still confirm it.
        if total_packet_bytes + framed_size > MAX_TOTAL_PACKET_BYTES:
            raise RuntimeError(
                f"unit {exposure}/{sca} release {release_identity}: "
                f"assembling difference image pid {pid}'s candidates would "
                f"accumulate {total_packet_bytes + framed_size} bytes of "
                f"in-memory packets ({len(packets) + 1} of {considered} "
                f"candidates so far), over the "
                f"{MAX_TOTAL_PACKET_BYTES}-byte aggregate budget; refusing "
                f"to continue assembling rather than risk exhausting the "
                f"worker's memory. Claim left 'claimed' for later recovery.")
        total_packet_bytes += framed_size

        packets.append({"alert_id": packet_id, "payload": payload,
                        "checksum": payload_checksum(payload), "sid": sid})

    # THE UNIFORM-FAILURE GUARD (finding 10). A nonempty selection that
    # assembled zero packets via the GENERIC per-candidate catch — as
    # opposed to the oversize check, which is its own explicit, legitimate
    # "nothing to write" outcome (criterion 8, `OversizePacketTests`) — means
    # every selected candidate hit the same wall. `SYSTEMIC_EXCEPTIONS`
    # above already re-raises the failures that are ALWAYS a chip-wide
    # signal; this catches the remaining case where a genuinely
    # candidate-shaped exception (by type) nonetheless struck every
    # candidate on the chip, which is itself evidence of a systemic problem
    # gate 3's per-candidate scope was never meant to hide. Raised BEFORE
    # STEP 3, so the claim is left `claimed` for a later attempt exactly as
    # a chip-level failure is, rather than confirming an emission that
    # published nothing.
    if (considered > 0 and not packets
            and candidate_exception_drops == considered):
        raise RuntimeError(
            f"every one of the {considered} selected candidate(s) for "
            f"difference image pid {pid} failed during assembly "
            f"(dropped_by_reason={dropped_by_reason!r}); refusing to "
            f"confirm a zero-packet emission as a success")

    # STEP 3: CONFIRM + THE alert_published MILESTONE, in ONE transaction
    # (integration ruling 3 / 6: "Emission confirmation and the
    # alert-published milestone commit in one transaction, so a crash cannot
    # confirm an emission without the milestone recording it").
    # THE ORDER IS FIXED AND IS THE POINT (brief E): confirm CAS first, then
    # the token check, and ONLY THEN the outbox rows and the milestone. The
    # confirm can affect zero rows without raising, so inserting first would
    # let a LOSING claimant commit packets for an emission it did not confirm
    # — and the publisher would deliver them. Under this order a loser commits
    # neither.
    #: Set True only by the DB-failure catch below (finding 11) — the
    #: observability flag distinguishing "the confirm failed" from an
    #: ordinary takeover, in the provenance record below.
    confirmation_db_failure = False
    try:
        with transaction(conn) as _:
            confirmed_token = emissions.confirm_alert_emission(
                exposure, sca, release_identity, claim_token, len(packets))
            if confirmed_token == claim_token:
                for packet in packets:
                    # Through the migration's function, never a bare INSERT:
                    # the same-id-different-envelope comparison is the
                    # invariant, and a call site that wrote its own statement
                    # could forget it. A re-run after a lost response lands
                    # here with identical digests and is absorbed as
                    # 'idempotent'.
                    #
                    # A GENUINE COLLISION RAISES, AND IS NOT CAUGHT HERE —
                    # deliberately. The `except RepositoryQueryFailed` below
                    # handles ordinary query failure; a same-id-different-
                    # envelope collision is not that. The repository re-raises
                    # the migration's own P0001 UNWRAPPED, so it unwinds this
                    # transaction and FAILS THE ATTEMPT. That is the intended
                    # outcome: it means either the digest inputs are
                    # incomplete or two different packets were minted under
                    # one identity, and both are defects that must stop the
                    # pipeline rather than be recorded as a drop and continued
                    # past.
                    outbox.insert_packet(
                        packet["alert_id"], image_basis["basis_name"],
                        packet["payload"], packet["checksum"],
                        schema_version_id, topic, release_identity,
                        exposure, sca, claiming_attempt_id)
                    outboxed += 1
                writer = AttemptWriter(
                    ConnectionExecutor(conn, autocommit_each=False))
                writer.record_milestone(
                    "alert_published", _utcnow(), exposure_id=exposure,
                    sca=sca, producing_attempt_id=claiming_attempt_id)
    except (RapidDBCallFailed, RepositoryQueryFailed) as exc:
        # TWO VOCABULARIES, ONE OUTCOME. `RapidDBCallFailed` is the emission
        # CAS's (the `CheckedHandle` adapter over frozen `RAPIDDB`);
        # `RepositoryQueryFailed` is the outbox repository's. They are the same
        # event as far as this handler is concerned — a database call in the
        # confirmation transaction did not execute — and both are caught here
        # so a failed outbox insert is not louder than a failed confirm.
        #
        # A COLLISION IS DELIBERATELY NOT IN THIS LIST: the repository lets the
        # migration's own P0001 through unwrapped precisely so it escapes this
        # handler and fails the attempt.
        #
        # NOTHING WAS DELIVERED, so this is materially different from what it
        # meant before brief E. The transaction rolled back: no outbox rows,
        # no milestone, no confirmed emission — and no packets on the wire
        # either, because this job type no longer sends. The claim stays
        # 'claimed' and a later attempt (retry or takeover) redoes the work and
        # confirms. Logged rather than raised for the same reason as before:
        # the attempt's science succeeded and failing it here would invite a
        # retry of the whole job for a database fault at its last step —
        # THE SELF-HEALING BEHAVIOUR ITSELF IS UNCHANGED (finding 11's ruled
        # scope: observability only). What changes is how loud and how
        # identifiable this event is in the meantime: this used to be a
        # bare module-`logger` warning, invisible until the 1-hour staleness
        # window let a later attempt take the claim over — this is now an
        # `context.logger.error` (the attempt-scoped, structured logger
        # every other loud path in this stage already uses, captured to the
        # per-stage diagnostics bundle with this attempt's own job/attempt
        # identity, unlike the plain module logger) and a dedicated
        # `confirmation_db_failure` provenance flag, so the gap between "the
        # confirm silently failed" and "a later attempt confirms" is no
        # longer indistinguishable from an ordinary clean run in either the
        # log stream or the terminal record.
        outboxed = 0
        confirmation_db_failure = True
        context.logger.error(
            "unit %s/%s release %s: assembled %d packet(s) but the "
            "CONFIRMATION TRANSACTION FAILED (%s: %s); NOTHING was "
            "committed and nothing was sent. The claim stays 'claimed' for "
            "a later attempt to retry or take over (self-healing by "
            "design, within CLAIM_STALENESS=%s) — but until then this "
            "unit's alerts are NOT emitted. attempt_id=%s claim_token=%s",
            exposure, sca, release_identity, len(packets),
            type(exc).__name__, exc, CLAIM_STALENESS, claiming_attempt_id,
            claim_token)
        confirmed_token = None

    if confirmed_token != claim_token:
        # A LOSING CLAIMANT COMMITS NOTHING. The transaction above wrote no
        # outbox rows and no milestone, because both are inside the
        # `confirmed_token == claim_token` arm — so unlike the pre-E protocol,
        # there is nothing on the wire to deduplicate: the takeover attempt
        # assembles and outboxes the same packets under the same deterministic
        # ids, and the publisher delivers them once.
        outboxed = 0
        if confirmation_db_failure:
            # Already logged loudly, above, at `error` level with the
            # database exception and this attempt's identity — a second,
            # generic "taken over (or the confirm failed)" warning here
            # would blur the one signal finding 11 asks to make clear, so
            # this arm is skipped for that case specifically.
            pass
        else:
            context.logger.warning(
                "unit %s/%s: claim was taken over before confirm; %d "
                "assembled packet(s) were DISCARDED uncommitted — the "
                "takeover attempt outboxes them under the same "
                "deterministic alert ids", exposure, sca, len(packets))

    context.record_effect(
        candidates_considered=considered,
        # OUTBOX-WRITE ACCOUNTING, NOT DELIVERY ACCOUNTING, and renamed to say
        # so (brief E2 asks for one or the other; this is the choice). The old
        # `alerts_published` counted producer sends from inside this job, which
        # no longer happen — keeping the name would have this attempt claiming
        # a delivery it does not perform and cannot observe. What this job can
        # honestly report is how many packets it COMMITTED; whether they
        # reached the broker is `alert_outbox.state`, and how long that took is
        # the `alert_outbox_health` view. The DB column
        # `alert_emissions.alerts_published` (migration 037) keeps its name —
        # renaming a column is a rapid_systems change this brief does not own —
        # and now counts packets outboxed by the confirming attempt.
        alerts_outboxed=outboxed,
        candidates_dropped=sum(dropped_by_reason.values()),
        dropped_by_reason=dropped_by_reason,
        drop_dispositions=drop_dispositions,
        emissions_suppressed=0,
        emission_confirmed=(confirmed_token == claim_token),
        # FINDING 11: the one bit that told an operator "the confirm failed
        # at the database" apart from an ordinary, healthy takeover was
        # buried in a log line at a level (module-`logger.warning`, no
        # attempt identity) nothing polls. `emission_confirmed=False` alone
        # cannot carry that distinction — a takeover is also
        # `emission_confirmed=False` and is not a defect — so this field
        # says explicitly which of the two happened, readable straight out
        # of the terminal record without correlating log lines.
        confirmation_db_failure=confirmation_db_failure,
        alert_topic=topic,
        alert_identity_basis=image_basis["basis_name"],
        alert_schema_version_id=str(schema_version_id),
        alert_release_identity=release_identity,
        alert_difference_image_pid=pid,
        selection_rule=f"PLACEHOLDER top-{PLACEHOLDER_TOP_N_BY_SNR}-by-snr",
        sources_available=len(sources))
    context.logger.info(
        "unit %s/%s: %d candidate(s) considered, %d outboxed, %d dropped, "
        "confirmed=%s (release %s, topic %s, basis %s)",
        exposure, sca, considered, outboxed,
        sum(dropped_by_reason.values()), confirmed_token == claim_token,
        release_identity, topic, image_basis["basis_name"])


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


def _pinned_schema_version(context, topic):
    """The schema-version UUID every packet this attempt writes is pinned to.

    RESOLVED HERE, AT OUTBOX-WRITE TIME, AND STORED — which is the entire
    mechanism behind rule 14's "identical bytes on resend". The production
    producer resolves the registry's LATEST version at publish time
    (`alerts/kafka_producer.py`, `SchemaVersionNumber={"LatestVersion":
    True}`), so a packet re-framed after a registry bump yields DIFFERENT wire
    bytes. Pinning the UUID onto the row and framing strictly from it is what
    makes a resend byte-identical across a registry change.

    Taken from the parameter tree when it carries one, and otherwise from the
    registry — reading it here, once per chip, rather than per packet: it is
    the same value for every packet on the chip and the lookup is a network
    call.

    NOTE THE DIRECTION OF THE DEPENDENCY. This is the LAST place in the
    pipeline that may consult the registry, and it does so to WRITE a value
    down, never to send. After brief E the send path has no registry access at
    all, which is what the pinning buys.
    """
    pinned = context.parameter("kafka/schema-version-id")
    if pinned:
        return str(pinned)

    from alerts.kafka_producer import GlueSchemaRegistry, schema_name_for_topic

    try:
        return str(GlueSchemaRegistry().schema_version_id(
            schema_name_for_topic(topic)))
    except Exception as exc:  # noqa: BLE001
        # FAIL THE ATTEMPT rather than invent or defer. A packet written
        # without a pinned version could only be framed by a registry lookup
        # at send time, which is exactly the property this design removes; and
        # the column is NOT NULL, so there is no partial answer to record.
        raise InputError(
            f"cannot resolve the schema version to pin onto this chip's "
            f"packets for topic {topic!r} ({type(exc).__name__}: {exc}); the "
            f"outbox row stores the version so a resend reproduces the first "
            f"send's wire bytes, and a packet written without one could only "
            f"be framed by a registry lookup at send time") from exc


def _image_identity(outbox, pid, context):
    """Which identity basis this chip's packets use, and its image component.

    Returns the keyword arguments `alerts.identity.alert_identity` takes, plus
    a `basis_name` for the outbox column and the effect record.

    THE PRODUCT KEY IS PREFERRED AND THE LEGACY TUPLE IS A DEGRADATION. A
    difference image registered under the identity model has a
    `diffimages.product_id` pointing at its `products` row (DRAFT 048), and
    that product key is the identity that survives reprocessing. 048 added the
    column as NULLABLE, so pre-D history has none — and for those rows the
    legacy `pid` is the ONLY identity of record. Mirrors the D worker's
    ratified P8 degradation: legacy-only, logged with the reason, never
    invented.

    The basis is decided ONCE PER CHIP because the image is the same for every
    candidate on it, and — critically — it is decided at outbox-write time and
    then frozen into each row. A difference image that later gains a product
    binding does NOT re-mint identities for packets already outboxed.
    """
    from alerts.identity import BASIS_LEGACY_PID, BASIS_PRODUCT_KEY

    product_key = None
    try:
        product_key = outbox.product_key_for_difference_image(pid)
    except RepositoryQueryFailed as exc:
        # A FAILED LOOKUP IS NOT AN ABSENT BINDING. Degrading to the legacy
        # basis here would mint permanent legacy identities for images that
        # may well have product keys, on the strength of a transient database
        # fault — and the identities are immutable once written.
        #
        # The repository already separates the two: a database with no DRAFT
        # 048 answers None from a catalog PROBE, never from a failed query, so
        # this branch is reached only by a genuine fault.
        raise RuntimeError(
            f"could not determine whether difference image pid {pid} has a "
            f"product binding: {exc}. Refusing to choose an identity basis on "
            f"a failed lookup — the basis is frozen into every packet this "
            f"chip writes and cannot be corrected later") from exc

    if product_key:
        return {"basis_name": BASIS_PRODUCT_KEY, "product_key": product_key}

    context.logger.info(
        "difference image pid %s has no product binding (pre-D history: "
        "DRAFT 048 added diffimages.product_id as nullable), so this chip's "
        "packets take the %s basis with the legacy pid as the image "
        "identity — the ratified degradation, recorded per packet in "
        "alert_outbox.identity_basis", pid, BASIS_LEGACY_PID)
    return {"basis_name": BASIS_LEGACY_PID, "legacy_pid": pid}


#: The job type's sequence — one stage, for the reason `produce_alerts` states.
ALERT_PRODUCTION_SEQUENCE = (
    ("produce_alerts", produce_alerts),
)
