"""
File:    errors.py

Classifying database errors by SQLSTATE, never by message text.

**WHY THIS MODULE EXISTS** (rule 12, verbatim: "No database error is
classified by message text (use SQLSTATE/typed exceptions; the fake
executors in tests must raise matching shapes)"). Two call sites had grown
message-substring classification:

  * `pipeline.seams._attach_work_unit` recognized a missing
    workflow_definitions row by `"work_units_definition_fk" in str(exc)`,
    and used that to make the entire intent layer optional.
  * the same seam needed, and did not have, a way to recognize the
    unique-violation half of a claim race.

Message text is the wrong key for both. It is a driver-and-locale-dependent
string with no compatibility contract: a psycopg2 upgrade, a PostgreSQL
version bump, or a server running under a non-English locale can all change
it, and the failure mode is silent — the substring simply stops matching and
the branch it guarded stops firing. SQLSTATE is the opposite: a five-character
code fixed by the SQL standard and by PostgreSQL's documented error-code
table, stable across versions and locales.

**WHY NOT `except psycopg2.errors.UniqueViolation` DIRECTLY.** This repo
deliberately keeps the driver out of the layers that must also run where it
is absent — `pipeline.intent.writer` takes an injected `execute(sql, params)`
callable precisely so the whole intent layer is testable and importable with
no psycopg2 present (its own docstring states this rule). Importing
driver-specific exception classes into `seams` would break that, and a bare
`hasattr` probe would drift back toward duck-typing on strings.

So the predicates below read the ONE attribute the DB-API and psycopg2 both
expose for this purpose — `exc.pgcode` — and compare it to a named constant.
That is a typed, documented contract rather than a message match, and it is
satisfiable by a test double without importing a driver: a fake executor
raises any exception object carrying the right `pgcode`, which is what "the
fake executors in tests must raise matching shapes" asks for. `FakePgError`
below is provided so tests raise the real shape rather than each inventing
its own.

The predicates are deliberately conservative — an exception with no
`pgcode`, or a `pgcode` that is not the one asked about, is NOT that error.
An unrecognized failure must propagate, never be absorbed by a branch that
guessed.
"""

#: PostgreSQL SQLSTATE 23505 — unique_violation. Raised by a partial unique
#: index (migration 036's `work_units_current_identity_uq`) exactly as by a
#: table constraint; the index kind is not visible in the code.
UNIQUE_VIOLATION = "23505"

#: PostgreSQL SQLSTATE 23503 — foreign_key_violation. What a missing
#: `workflow_definitions` row raises through `work_units_definition_fk`.
FOREIGN_KEY_VIOLATION = "23503"


def sqlstate_of(exc):
    """The SQLSTATE an exception carries, or None.

    `pgcode` is psycopg2's spelling; `sqlstate` is what some other DB-API
    drivers use for the same value. Both are read so this module does not
    silently become psycopg2-only, and a value that is not a five-character
    code is treated as absent rather than compared.
    """
    for attribute in ("pgcode", "sqlstate"):
        code = getattr(exc, attribute, None)
        if isinstance(code, str) and len(code) == 5:
            return code
    return None


def is_unique_violation(exc):
    """True when `exc` is SQLSTATE 23505.

    The claim-race predicate: for a find-or-create, this specific error means
    another transaction created the row first, which the caller resolves by
    re-reading rather than by failing.
    """
    return sqlstate_of(exc) == UNIQUE_VIOLATION


def is_foreign_key_violation(exc):
    """True when `exc` is SQLSTATE 23503.

    Provided for completeness and for tests that assert a missing definition
    now PROPAGATES: no production call site absorbs this any more (rule 12 —
    a missing definition is a hard error after the loading step exists), so
    a caller reaching for this predicate to swallow one is going against the
    rule this module was written to enforce.
    """
    return sqlstate_of(exc) == FOREIGN_KEY_VIOLATION


class FakePgError(Exception):
    """A driver-shaped error for test doubles: carries a real `pgcode`.

    Tests raise this instead of a bare `RuntimeError` with hand-crafted
    message text, so what they exercise is the same attribute the production
    predicates read. A double that cannot raise the shape production
    classifies is a double that cannot refuse — the exact stub-blindness the
    old message-matching tests suffered from, where a fake raised a
    `RuntimeError` whose message happened to contain a constraint name.
    """

    def __init__(self, pgcode, message=None):
        super().__init__(message or f"SQLSTATE {pgcode}")
        self.pgcode = pgcode
