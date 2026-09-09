"""The PSF registration repository.

The two writes the science-PSF registration makes, over the schema's own
stored functions: `addPSF` (insert at vbest 0, version max + 1 for the
row's (fid, sca); migration 008, attempt identity threaded by 026) and
`updatePSF` (demote the incumbent, promote this version, refusing to demote
an operator pin at vbest 2; the same migrations). `RAPIDDB` is frozen
against new methods (conformance rule 17; its own docstring points here),
which is why dev's `add_psf`/`update_psf` (70565e0c) land as this
repository rather than as two more methods on that class.

Takes a connection it does not own and never commits: the caller owns the
transaction, which is what lets one registration land every detector's row
or none of them. A failed statement rolls the connection back — psycopg2
leaves the transaction failed otherwise — and raises `RepositoryQueryFailed`.
"""

import typing

from pipeline.repositories.errors import RepositoryQueryFailed

_ADD_SQL = (
    "select * from addPSF("
    "cast(%s as smallint), cast(%s as smallint), "
    "cast(%s as character varying(255)), cast(%s as character varying(32)), "
    "cast(%s as smallint), cast(%s as bigint), cast(%s as integer)) "
    "as (psfid integer, version smallint);")

_UPDATE_SQL = (
    "select * from updatePSF("
    "cast(%s as integer), cast(%s as character varying(255)), "
    "cast(%s as character varying(32)), cast(%s as smallint), "
    "cast(%s as smallint), cast(%s as bigint), cast(%s as integer));")

_VBEST_SQL = "select vbest from psfs where psfid = %s;"


class PsfRow(typing.NamedTuple):
    """What `addPSF` returns: the new row's key and its version."""

    psfid: int
    version: int


class PsfRepository:
    """Registration writes on `PSFs`, through the stored functions only."""

    def __init__(self, conn):
        self._conn = conn

    def add(self, fid, sca, filename, checksum, status=1,
            attempt_id=None, record_sequence=None):
        """Insert one PSF version at vbest 0. Never promotes.

        `attempt_id` and `record_sequence` are the attempt identity a
        registration threads through (migration 026, find-before-mint);
        a standalone registration passes neither and mints a version.
        """
        row = self._one("add", _ADD_SQL,
                        (fid, sca, filename, checksum, status,
                         attempt_id, record_sequence))
        if row is None or row[0] is None:
            raise RepositoryQueryFailed(
                "add", f"addPSF returned no row for fid={fid} sca={sca}")
        return PsfRow(int(row[0]), int(row[1]))

    def promote(self, psfid, filename, checksum, status, version,
                attempt_id=None, record_sequence=None):
        """Ask `updatePSF` to make this version current; return its vbest.

        The returned value is read back from the row, because the stored
        function leaves a new version at 0 when the incumbent is pinned at
        2 — the caller reports what happened, not what it asked for.
        """
        self._one("promote", _UPDATE_SQL,
                  (psfid, filename, checksum, status, version,
                   attempt_id, record_sequence))
        return self.vbest(psfid)

    def vbest(self, psfid):
        """The promotion pointer the row holds now."""
        row = self._one("vbest", _VBEST_SQL, (psfid,))
        if row is None:
            raise RepositoryQueryFailed("vbest", f"no PSFs row psfid={psfid}")
        return int(row[0])

    def _one(self, method, sql, params):
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        except RepositoryQueryFailed:
            raise
        except Exception as exc:                  # noqa: BLE001 — re-typed
            self._conn.rollback()
            raise RepositoryQueryFailed(method, str(exc)) from exc
