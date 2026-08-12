"""The product / artifact repository.

The write path for the three tables DRAFT migration 048 adds: `products`
(one row per deterministic identity), `artifacts` (one row per published
file per attempt), and `product_artifacts` (which artifact currently
realizes which product).

**WHY A REPOSITORY AND NOT A `RAPIDDB` METHOD.** `RAPIDDB` is frozen —
brief D: "no new method lands in it". It is the legacy handle whose
methods set an `exit_code` attribute instead of raising, which is the
failure signal `pipeline/repositories/errors.py` exists to replace. New
registration writes go through the modern path (carved repositories over a
connection the caller owns), which is G's established pattern and the one
`DiffImageRepository` already follows.

**THIS REPOSITORY NEVER COMMITS AND NEVER OPENS A CONNECTION.** It takes
the connection the registration consumer's per-attempt transaction is
already running on (`pipeline/registration/consumer.py`, `_transaction`),
so product rows, artifact rows, the legacy version rows and the
registration watermark all commit together or roll back together. That
transaction boundary — its lease, its watermark re-read, and C3's unified
lock order — is NOT changed by anything here; D changes what the
transaction records, never the transaction.

A repository that opened its own connection would reproduce round-3
finding #8 exactly: two connections cannot be one transaction, so the
product rows would be durable before the watermark was attempted and a
crash between them would leave rows written with the attempt still a
candidate.

**IDEMPOTENCE IS THE DATABASE'S, NOT THIS MODULE'S.** Every insert here
uses `ON CONFLICT` against a real constraint rather than a
SELECT-then-INSERT: a find-or-insert in Python has a window between the two
statements in which a concurrent registrar can insert the same row, and the
registration consumer is explicitly a concurrent design (per-attempt
leases, watermark re-reads). The constraints doing the work are
`products_product_key_uq` and `artifacts_replay_uq`.
"""

import json
import typing

from pipeline.repositories.errors import RepositoryQueryFailed


class Product(typing.NamedTuple):
    """One canonical product row."""

    product_id: int
    product_key: str
    product_class: str
    role: str


class Artifact(typing.NamedTuple):
    """One published file, as recorded for one attempt."""

    artifact_id: int
    attempt_id: int
    record_sequence: int
    published_name: str
    uri: str
    checksum: str
    checksum_algorithm: str


#: `ON CONFLICT (product_key) DO UPDATE ... RETURNING` rather than
#: `DO NOTHING`: `DO NOTHING` returns no row when the conflict fires, so a
#: second attempt at an existing product would get None back and have to
#: re-SELECT. The `DO UPDATE` is a deliberate no-op write of the key to its
#: own value — the idiom that makes RETURNING fire on both paths — and it
#: touches nothing else, so a re-registration cannot rewrite an existing
#: product's identity payload or its first_seen.
_UPSERT_PRODUCT_SQL = (
    "INSERT INTO products"
    "  (product_key, product_class, role, identity_payload,"
    "   serialization_version, process_family)"
    " VALUES (%s, %s, %s, %s::jsonb, %s, %s)"
    " ON CONFLICT (product_key) DO UPDATE SET product_key = EXCLUDED.product_key"
    " RETURNING product_id, product_key, product_class, role"
)

#: The replay guard, database-side. A replay of the same
#: `(attempt_id, record_sequence, published_name)` conflicts and updates
#: nothing, returning the row that is already there — so a re-run of the
#: registration pass writes no new artifact and the caller still learns the
#: artifact id it needs for the binding.
_UPSERT_ARTIFACT_SQL = (
    "INSERT INTO artifacts"
    "  (attempt_id, record_sequence, published_name, uri,"
    "   checksum_algorithm, checksum, size_bytes, content_type,"
    "   image_digest, source_revision)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (attempt_id, record_sequence, published_name)"
    "   DO UPDATE SET attempt_id = EXCLUDED.attempt_id"
    " RETURNING artifact_id, attempt_id, record_sequence, published_name,"
    "           uri, checksum, checksum_algorithm"
)

#: Supersede every current binding for this product before the new one is
#: written. Two statements rather than one upsert because the partial unique
#: index `product_artifacts_one_current_uq` permits exactly one current row
#: and an INSERT of a second would raise before any DO UPDATE could apply.
_SUPERSEDE_BINDINGS_SQL = (
    "UPDATE product_artifacts SET is_current = false"
    " WHERE product_id = %s AND is_current"
)

_BIND_SQL = (
    "INSERT INTO product_artifacts"
    "  (product_id, artifact_id, legacy_rfid, legacy_pid, legacy_version,"
    "   is_current)"
    " VALUES (%s, %s, %s, %s, %s, true)"
    " ON CONFLICT (product_id, artifact_id) DO UPDATE"
    "   SET is_current = true,"
    "       legacy_rfid = EXCLUDED.legacy_rfid,"
    "       legacy_pid = EXCLUDED.legacy_pid,"
    "       legacy_version = EXCLUDED.legacy_version,"
    "       bound_at = now()"
    " RETURNING product_artifact_id"
)

_LINK_REFIMAGE_SQL = (
    "UPDATE refimages SET product_id = %s WHERE rfid = %s AND version = %s"
)

_LINK_DIFFIMAGE_SQL = (
    "UPDATE diffimages SET product_id = %s WHERE pid = %s AND version = %s"
)

_PRODUCT_BY_KEY_SQL = (
    "SELECT product_id, product_key, product_class, role FROM products"
    " WHERE product_key = %s"
)

_ARTIFACTS_FOR_ATTEMPT_SQL = (
    "SELECT artifact_id, attempt_id, record_sequence, published_name, uri,"
    "       checksum, checksum_algorithm FROM artifacts"
    " WHERE attempt_id = %s ORDER BY artifact_id"
)

_CURRENT_BINDING_SQL = (
    "SELECT artifact_id, legacy_rfid, legacy_pid, legacy_version"
    " FROM product_artifacts WHERE product_id = %s AND is_current"
)

#: The reference image's product key, found from the legacy `rfid` a
#: difference image's record cites. Read through `refimages.product_id`
#: rather than through `product_artifacts.legacy_rfid`, because the FK on the
#: legacy row is the authoritative statement of "this row realizes that
#: product" while a binding row is scoped to one artifact — and a reference
#: image with several versions has several bindings but one product per
#: version.
#:
#: The version predicate is applied only when the caller has a version:
#: `reference_image_version` is an optional manifest fact, and a difference
#: image whose record predates it still names a real `rfid`. Where it is
#: absent the current-best row answers, which is the same row
#: `get_best_reference_image` would have returned.
_REFERENCE_KEY_SQL = (
    "SELECT p.product_key FROM refimages r"
    " JOIN products p ON p.product_id = r.product_id"
    " WHERE r.rfid = %s AND (%s::smallint IS NULL OR r.version = %s::smallint)"
    " ORDER BY r.vbest DESC, r.version DESC LIMIT 1"
)


class ProductRepository:
    """Writes and reads `products`, `artifacts` and `product_artifacts`.

    Takes a connection it does not own and never commits — the caller's
    transaction owns the boundary. See the module docstring.
    """

    def __init__(self, conn):
        self._conn = conn

    # -- writes --------------------------------------------------------

    def upsert_product(self, product_key, product_class, role,
                       identity_payload, serialization_version,
                       process_family):
        """The canonical product row for this identity, created or found.

        Returns a `Product`. Idempotent by `products_product_key_uq`: a
        retry under a new attempt that agrees on all four identity
        components resolves to the SAME row, which is the property rule 10
        exists to give and the reason this is an upsert rather than an
        insert.

        `identity_payload` is the canonical object the key was digested
        over (`pipeline.registration.identity.canonical_payload`), stored
        whole so the identity stays recomputable from the row.
        """
        row = self._one(
            "upsert_product", _UPSERT_PRODUCT_SQL,
            (product_key, product_class, role,
             json.dumps(identity_payload, sort_keys=True,
                        separators=(",", ":")),
             int(serialization_version), int(process_family)))
        return Product(*row)

    def upsert_artifact(self, attempt_id, record_sequence, published_name,
                        uri, checksum, checksum_algorithm="sha256",
                        size_bytes=None, content_type=None,
                        image_digest=None, source_revision=None):
        """One artifact row for one published file of one attempt.

        Returns an `Artifact`. Attempt-scoped: a re-attempt at the same
        science writes a NEW row here even for byte-identical output,
        because these are two publication events. A REPLAY of the same
        `(attempt_id, record_sequence, published_name)` writes none — the
        `artifacts_replay_uq` constraint absorbs it and the existing row
        comes back.

        The checksum is stored WHOLE. The legacy `refimages.checksum` and
        `diffimages.checksum` columns are `varchar(32)` and truncate a
        SHA-256 to half its length; `artifacts.checksum` is CHECK-
        constrained to the full 64 hex characters against the recorded
        algorithm, so a truncated value fails loudly here instead of being
        silently stored and later compared as equal to the wrong bytes.
        """
        row = self._one(
            "upsert_artifact", _UPSERT_ARTIFACT_SQL,
            (int(attempt_id), int(record_sequence), published_name, uri,
             checksum_algorithm, checksum,
             None if size_bytes is None else int(size_bytes),
             content_type, image_digest, source_revision))
        return Artifact(*row)

    def bind(self, product_id, artifact_id, legacy_rfid=None,
             legacy_pid=None, legacy_version=None):
        """Make this artifact the CURRENT realization of this product.

        Supersedes any previous current binding first, so the partial
        unique index `product_artifacts_one_current_uq` sees exactly one
        current row. The superseded rows are kept rather than deleted: a
        product's binding history is the record of which attempts realized
        it, and it is worth having when an operator asks why `vbest` moved.

        `legacy_rfid`/`legacy_pid` plus `legacy_version` record which
        `(rfid|pid, version)` row this binding corresponds to, so today's
        consumers and the identity model name the same object.
        """
        self._execute("supersede_bindings", _SUPERSEDE_BINDINGS_SQL,
                      (int(product_id),))
        row = self._one(
            "bind", _BIND_SQL,
            (int(product_id), int(artifact_id),
             None if legacy_rfid is None else int(legacy_rfid),
             None if legacy_pid is None else int(legacy_pid),
             None if legacy_version is None else int(legacy_version)))
        return row[0]

    def link_reference_image(self, product_id, rfid, version):
        """Point one legacy `refimages` row at its product.

        The legacy row BINDS to the identity without BEING it: `(rfid,
        version)` stays exactly what every current consumer reads, and the
        FK is what says which product it realizes.
        """
        return self._execute("link_reference_image", _LINK_REFIMAGE_SQL,
                             (int(product_id), int(rfid), int(version)))

    def link_difference_image(self, product_id, pid, version):
        """Point one legacy `diffimages` row at its product."""
        return self._execute("link_difference_image", _LINK_DIFFIMAGE_SQL,
                             (int(product_id), int(pid), int(version)))

    # -- reads ---------------------------------------------------------

    def product_by_key(self, product_key):
        """The product with this key, or None.

        Returns None rather than raising for an absent key: "no product has
        this identity yet" is an ordinary answer, and it is how a
        difference image's registration discovers whether its reference
        input has a product row to cite.
        """
        rows = self._query("product_by_key", _PRODUCT_BY_KEY_SQL,
                           (product_key,))
        return Product(*rows[0]) if rows else None

    def artifacts_for_attempt(self, attempt_id):
        """Every artifact this attempt published, oldest first."""
        rows = self._query("artifacts_for_attempt",
                           _ARTIFACTS_FOR_ATTEMPT_SQL, (int(attempt_id),))
        return [Artifact(*row) for row in rows]

    def product_key_for_reference(self, rfid, version=None):
        """The product key of a reference image, by its legacy `rfid`.

        The `rfid` is a LOOKUP HANDLE here and never enters a digest: what
        this returns is the reference's own product key, which is what a
        difference image's identity is composed from. Returns None where
        that reference has no product row yet — the ordinary state during
        rollout, and the caller's signal to register legacy-only rather
        than to invent a key over an unidentified input.
        """
        rows = self._query(
            "product_key_for_reference", _REFERENCE_KEY_SQL,
            (int(rfid),
             None if version is None else int(version),
             None if version is None else int(version)))
        return rows[0][0] if rows else None

    def current_binding(self, product_id):
        """The current `(artifact_id, rfid, pid, version)` binding, or None."""
        rows = self._query("current_binding", _CURRENT_BINDING_SQL,
                           (int(product_id),))
        return rows[0] if rows else None

    # -- plumbing ------------------------------------------------------

    def _query(self, method, sql, params):
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except Exception as exc:                  # noqa: BLE001 — re-typed
            # NOT rolled back here, unlike `DiffImageRepository._query`.
            # That repository's calls are standalone reads whose caller may
            # want to continue on the same connection; these run INSIDE the
            # registration consumer's per-attempt transaction, which owns
            # the boundary and whose `_transaction` context manager rolls
            # back on the exception this raises. Rolling back here would
            # discard the product rows AND the milestone write AND the
            # watermark of a transaction the caller had not finished with,
            # turning one failed registration into a silent partial pass.
            raise RepositoryQueryFailed(method, str(exc)) from exc

    def _one(self, method, sql, params):
        rows = self._query(method, sql, params)
        if not rows:
            # An upsert with RETURNING that produced no row means the
            # ON CONFLICT path did not fire and the insert wrote nothing —
            # a state the constraints make unreachable, so it is a defect
            # rather than a data condition, raised rather than returned.
            raise RepositoryQueryFailed(
                method, "the statement returned no row; an upsert with "
                        "RETURNING must produce exactly one")
        return rows[0]

    def _execute(self, method, sql, params):
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount
        except Exception as exc:                  # noqa: BLE001 — re-typed
            raise RepositoryQueryFailed(method, str(exc)) from exc
