"""The alert-outbox write repository (rule 14, brief E1/E2).

The two calls the alert-production stage makes on its confirmation path: the
outbox insert, and the identity-basis lookup that decides which basis a chip's
packets are minted under.

**WHY A REPOSITORY AND NOT A `RAPIDDB` METHOD.** `RAPIDDB` is frozen — brief
G's ratified merge decision, restated in D's `products.py` and again in F's
`association.py`: "no new method lands in it". It is the legacy handle whose
methods set an `exit_code` attribute instead of raising, which is the failure
signal `pipeline/repositories/errors.py` exists to replace, and target rule 17
puts new access behind narrow typed repositories. An earlier revision of THIS
work added both queries to `RAPIDDB` and was correctly refused, exactly as F's
was; they belong here, over a connection the caller owns.

The publisher side got this right from the start — `pipeline/publisher/
outbox.py` is a repository over an injected executor — so this module is the
PRODUCER side's equivalent, and the two are deliberately separate: they share a
table and nothing else. The publisher claims and finalizes over its own pooled
connection as `rapid_publisher`; this writes inside the alert-production
attempt's confirmation transaction as the pipeline role, which is INSERT-only
on that table by grant. Merging them would put a role boundary inside one
class.

**THIS REPOSITORY NEVER COMMITS, NEVER OPENS A CONNECTION, AND NEVER ROLLS
BACK.** It takes the connection its caller owns, and its calls run INSIDE the
confirmation transaction that also holds the `alert_emissions` confirm CAS and
the `alert_published` milestone. Rolling back here would discard a transaction
the caller had not finished with — `products.py`'s `_query` records the same
reasoning for the same reason — so a failure is raised and the caller's own
`transaction(conn)` envelope decides what to do with it.

**THE COLLISION RAISE IS NOT CAUGHT.** `insert_alert_outbox_packet` (migration
050) RAISES on a same-`alert_id` insert whose payload checksum, pinned schema
version, or any other envelope field differs. That is a hard invariant
violation — either the digest inputs are incomplete or two different packets
were minted under one identity — and it must reach the caller as an exception
that fails the attempt, not as a typed "query failed" a caller might treat as
retryable. It is therefore re-raised UNWRAPPED, and `_query` below is the only
place in this file that distinguishes the two.
"""

import typing

from pipeline.repositories.errors import RepositoryQueryFailed

#: The probe for DRAFT 050. Asked of the catalog rather than inferred from a
#: failing query: "this schema is not deployed" and "this query is wrong" are
#: two facts a caller must never conflate — F's `association.py` states the
#: same rule for 049, and the cost of conflating them here would be an alert
#: run that silently wrote no packets while reporting success.
_OUTBOX_PROBE = "SELECT to_regclass('public.alert_outbox')"

#: The probe for DRAFT 048's product binding, which the product-key identity
#: basis joins through.
#:
#: PROBED RATHER THAN CATCHING `UndefinedTable`, and the difference matters
#: here more than anywhere else in this file: this call runs inside the
#: caller's confirmation transaction, and a failed statement ABORTS that
#: transaction. The `RAPIDDB` revision of this method recovered by calling
#: `self.conn.rollback()` — safe there, because every read in that class
#: autocommits and owns nothing, and catastrophic here, because it would
#: discard the confirm CAS the caller had already written. Asking the catalog
#: first never puts the transaction in that state.
_PRODUCT_BINDING_PROBE = (
    "SELECT to_regclass('public.products') IS NOT NULL"
    "   AND EXISTS (SELECT 1 FROM information_schema.columns"
    "                WHERE table_schema = 'public'"
    "                  AND table_name = 'diffimages'"
    "                  AND column_name = 'product_id')"
)

_INSERT_PACKET_SQL = (
    "SELECT insert_alert_outbox_packet(%s, %s, %s, %s, %s, %s, %s, %s, %s,"
    "                                  %s, %s)"
)

#: The identity basis's own lookup: a difference image's rule-10 product key,
#: through DRAFT 048's nullable `diffimages.product_id` binding.
_PRODUCT_KEY_FOR_DIFFIMAGE_SQL = (
    "SELECT p.product_key"
    "  FROM DiffImages d"
    "  JOIN Products p ON p.product_id = d.product_id"
    " WHERE d.pid = %s"
)


class OutboxInsertOutcome(typing.NamedTuple):
    """What the insert path did with one packet.

    `outcome` is `'inserted'` (this packet is new) or `'idempotent'` (an
    identical packet was already there — the ordinary re-run after a lost
    response, absorbed rather than treated as an error). A NamedTuple rather
    than a bare string so a caller reading the result cannot mistake the two
    for a boolean.
    """

    alert_id: str
    outcome: str

    @property
    def was_written(self):
        """Did THIS call write the row?

        The effect counts want packets committed by this attempt; an
        idempotent absorption means the packet is in the outbox but this call
        did not put it there.
        """
        return self.outcome == "inserted"


class AlertOutboxRepository:
    """The producer side's outbox writes, over a connection the caller owns."""

    def __init__(self, conn):
        self._conn = conn

    # -- schema probes -------------------------------------------------

    def outbox_schema_present(self):
        """Is DRAFT 050 applied on this database?

        Separate from the writes so a caller can tell "not deployed" from "the
        insert failed" without interpreting an exception.
        """
        rows = self._query("outbox_schema_present", _OUTBOX_PROBE, None)
        return bool(rows) and rows[0][0] is not None

    def product_binding_present(self):
        """Is DRAFT 048's `diffimages.product_id -> products` binding applied?

        Both halves are asked, because 048 adds a table AND a column and a
        database could in principle carry one without the other. Answering
        `False` sends the caller to the `legacy-pid` identity basis, which is
        the ratified degradation for pre-D history.
        """
        rows = self._query("product_binding_present", _PRODUCT_BINDING_PROBE,
                           None)
        return bool(rows) and bool(rows[0][0])

    # -- the identity basis --------------------------------------------

    def product_key_for_difference_image(self, pid):
        """The `products.product_key` bound to a difference image, or None.

        `pid` is a LOOKUP HANDLE here and never enters a digest — exactly as
        `ProductRepository.product_key_for_reference` uses `rfid`. What this
        returns is the image's own product key, which is what the packet
        identity's `product-key` basis is composed from.

        RETURNS None IN TWO DIFFERENT CASES, and the caller must not be able to
        confuse either with a failure:

          * DRAFT 048 is not applied — there is no binding to read anywhere on
            this database;
          * the binding exists and this image has none — pre-D history, the
            ordinary state during rollout.

        Both mean "no product key to use", which is the question this answers,
        and both send the caller to the `legacy-pid` basis. What is NOT
        collapsed into them is a genuine query failure: a typo, a permissions
        error, a dropped connection all raise `RepositoryQueryFailed`. That
        distinction is load-bearing — the identity basis is frozen into every
        packet the chip writes and cannot be corrected later, so degrading to
        the legacy basis on a transient fault would mint permanent legacy
        identities for images that have product keys.
        """
        if not self.product_binding_present():
            return None
        rows = self._query("product_key_for_difference_image",
                           _PRODUCT_KEY_FOR_DIFFIMAGE_SQL, (int(pid),))
        return rows[0][0] if rows else None

    # -- the write -----------------------------------------------------

    def insert_packet(self, alert_id, identity_basis, payload,
                      payload_checksum, schema_version_id, topic,
                      release_identity, exposure_id, sca,
                      producing_attempt_id, corrects_alert_id=None):
        """Write one alert packet to the outbox. Returns an `OutboxInsertOutcome`.

        THROUGH MIGRATION 050's FUNCTION, never a bare INSERT: the function
        carries the same-id-different-envelope comparison, and a call site that
        wrote its own statement could forget it. The function absorbs an
        identical re-insert (`'idempotent'`) and RAISES on a genuine collision.

        MUST RUN INSIDE THE CALLER'S CONFIRMATION TRANSACTION and does not
        commit — the same posture `RAPIDDB.confirm_alert_emission` documents
        for the CAS this runs beside. Rule 14's "same transaction as the
        database effect that produced them" is the caller's envelope; this only
        writes inside it.

        `payload` is the schemaless Avro bytes. `psycopg2.Binary` adapts them
        for the `bytea` parameter — a plain `bytes` object is escaped as text
        and will not round-trip through a bytea column intact.
        """
        import psycopg2

        rows = self._query(
            "insert_packet", _INSERT_PACKET_SQL,
            (alert_id, identity_basis, psycopg2.Binary(payload),
             payload_checksum, schema_version_id, topic, release_identity,
             int(exposure_id), int(sca),
             None if producing_attempt_id is None else int(producing_attempt_id),
             corrects_alert_id))
        if not rows:
            raise RepositoryQueryFailed(
                "insert_packet",
                "the insert function returned no row; it returns 'inserted' "
                "or 'idempotent' and cannot return neither")
        return OutboxInsertOutcome(alert_id, rows[0][0])

    # -- plumbing ------------------------------------------------------

    def _query(self, method, sql, params):
        """Execute and fetch, re-typing failures — except a raised invariant.

        NOT ROLLED BACK HERE. These calls run inside the alert-production
        attempt's confirmation transaction, which owns the boundary and whose
        `transaction(conn)` envelope rolls back on the exception this raises.
        Rolling back here would discard the confirm CAS and the milestone of a
        transaction the caller had not finished with, turning one failed
        packet into a silent partial confirmation — the same reasoning
        `products.py`'s `_query` records for the registration transaction.
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                return cur.fetchall()
        except Exception as exc:                      # noqa: BLE001 — re-typed
            if _is_invariant_violation(exc):
                # THE COLLISION RAISE PASSES THROUGH UNWRAPPED. A same-id
                # insert with a different envelope is a defect in what was
                # minted, not a query that failed to run, and wrapping it as
                # `RepositoryQueryFailed` would put it in the same category as
                # a dropped connection — a category callers are entitled to
                # treat as retryable. It is neither retryable nor recoverable:
                # it must fail the attempt loudly.
                raise
            raise RepositoryQueryFailed(method, str(exc)) from exc


def _is_invariant_violation(exc):
    """Is this the migration's own RAISE, rather than a query failure?

    PL/pgSQL's bare `RAISE EXCEPTION` reports SQLSTATE P0001
    (`raise_exception`), which is what 050's collision guard and its two
    write-once triggers all surface. Matched on the SQLSTATE rather than on the
    message text: the message is written for an operator and may be reworded,
    while the code is the database's own classification.
    """
    return getattr(exc, "pgcode", None) == "P0001"
