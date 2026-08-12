"""Narrow repositories over the operational tables (conformance rule 17).

Rule 17 asks for typed repositories returning named records and raising
typed errors, with transaction ownership at the use case — against a
present state where `RAPIDDB` is one 5,000-line class with thirty-odd
query methods, returning raw tuples, reporting failure by setting
`exit_code` and returning `None`, and (until brief G) exiting the process
from its constructor.

THE CARVE IS SMALL AND REAL, NOT BROAD AND SHALLOW. It began with two
repositories, chosen as the two highest-traffic live operational families
per the call-site survey: the sky-catalog readers used by the HATS
catalog generators, and the difference-image overlap query used by forced
photometry. Between them they cover the three non-test `pipeline/`
top-level `RAPIDDB()` call sites — the ones still using the legacy
`exit(dbh.exit_code)` idiom. `pipeline/stages/` was already converted to
`RAPIDDB.borrowing(conn)` + `CheckedHandle` by an earlier brief and needs
no carve; it is the template this package generalizes.

IT HAS GROWN ONE PACKAGE AT A TIME, AND EACH ADDITION IS A REFUSAL
RECORDED. Brief D added `products.py`, F added `association.py`, E added
`alert_outbox.py`, H added `admission.py` — and D, F and E each shipped a
first revision that put its queries on `RAPIDDB` instead and was
correctly refused. The freeze now leads every brief's header for that
reason. `admission.py` is the sharpest case of why it matters: admission
ran through `RAPIDDB.add_exposure`, which reports failure by setting
`exit_code = 67` and returning, leaving `expid` as `None` — and not one
of the three ingest scripts had ever checked it, so that `None` flowed on
as the L2 insert's `expid`. A typed raise is the whole difference.

THE ADMISSION CARVE ALSO SETS A PRECEDENT WORTH NAMING: it does not fall
back. Where its DRAFT schema is absent it REFUSES to admit, because the
legacy path it would fall back to mints a duplicate admission per
re-ingest by construction. A degraded path that silently reintroduces the
defect its replacement exists to remove is worse than no path at all.

WHAT A REPOSITORY HERE IS, AND IS NOT:

  * It owns SQL for ONE family of queries over a small set of tables.
  * It takes a connection it does not own. Transaction ownership is the
    use case's — a repository that committed would decide the caller's
    transaction boundary for it, which is the defect `BorrowedConnection`
    was introduced to stop in the registration path.
  * It returns NAMED records (`typing.NamedTuple`), never raw tuples.
    `forcedPhotometryForField.py` unpacks a 24-column tuple positionally
    into 24 parallel lists; a column inserted in the middle of that
    SELECT silently shifts every one of them.
  * It RAISES on failure. There is no `exit_code` to forget to check and
    no `None` return that a caller reads as "no rows" — the distinction
    between "the query found nothing" and "the query failed" is the one
    `RAPIDDB` loses, and `forcedPhotometryForField.py:602` calls
    `len(records)` on a method that returns bare `None` on error.
"""

from pipeline.repositories.errors import (RepositoryError,
                                          RepositoryQueryFailed)

__all__ = ["RepositoryError", "RepositoryQueryFailed"]
