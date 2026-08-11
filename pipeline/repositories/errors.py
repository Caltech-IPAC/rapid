"""The repository layer's typed errors.

A third family would be a third vocabulary, so this one is deliberately
thin and its place is stated rather than assumed (see
`pipeline/intent/errors.py`'s "two typed-error vocabularies" section):

  * `rapid_db_connect`'s `DBError` family answers "could a connection be
    established?" — raised before there is anything to query.
  * `pipeline.intent.errors` answers "what did the database say about
    this statement?" — SQLSTATE predicates over driver exceptions.
  * This module answers "did this repository's call succeed?" — the
    replacement for `RAPIDDB`'s `exit_code = 67` flag, which is the one
    failure signal neither of the others covers because it is not a
    connection outcome and not a specific SQLSTATE.

`RepositoryQueryFailed` always wraps the driver exception as `__cause__`,
so a caller that wants the SQLSTATE can still classify it with
`pipeline.intent.errors` — the layering adds a type, it does not hide
what the database said.
"""


class RepositoryError(Exception):
    """Base for the repository layer's failures."""

    error_category = "db_error"


class RepositoryQueryFailed(RepositoryError):
    """A repository query did not execute.

    Replaces `RAPIDDB`'s `exit_code = 67` plus bare `return`, which
    produced `None` where callers expected a list — indistinguishable
    from an empty result set until the `len()` three lines later raised
    `TypeError` instead of the intended "nothing found" branch.

    Carries the repository method name, because a caller several frames
    up should not have to re-derive which query failed from a driver
    message.
    """

    error_category = "db_query_failed"

    def __init__(self, method, message):
        super().__init__("%s failed: %s" % (method, message))
        self.method = method
