"""The difference-image overlap repository.

Carves `RAPIDDB.get_possible_overlapping_diffimages` and
`RAPIDDB.get_filters` — the two queries `pipeline/forcedPhotometryForField
.py` makes — into a repository returning named records.

WHY THIS FAMILY FIRST. Its result is a 24-column tuple that the caller
unpacks positionally into 24 parallel lists. That is the single most
fragile shape in the surveyed call sites: inserting a column anywhere in
the SELECT list shifts every subsequent field by one, and nothing fails
loudly — `ra1` simply becomes `dec1` and the photometry is silently wrong
about where it looked. A named record makes that class of change a
compile-time-visible edit rather than a silent numerical corruption.

THE SQL IS CARRIED OVER VERBATIM, deliberately. It is already fully
parameterized (`%s` placeholders with a `cast(%s as double precision)`
around the cone-search arguments), it is a q3c radial query whose
behaviour depends on the index it uses, and rewriting it here would make
this a change to the query as well as to its packaging — two changes to
review as one, with the science-affecting half hidden inside the
refactor.
"""

import typing

from pipeline.repositories.errors import RepositoryQueryFailed

# The 25 columns the overlap query returns, named. The order MUST match
# the SELECT list below exactly; it is the one coupling that survives the
# carve, and it survives in one place instead of at every call site.
class OverlappingDiffImage(typing.NamedTuple):
    """One difference image possibly overlapping a sky tile."""

    pid: int
    expid: int
    sca: int
    fid: int
    field: int
    jd: float
    ra0: float
    dec0: float
    ra1: float
    dec1: float
    ra2: float
    dec2: float
    ra3: float
    dec3: float
    ra4: float
    dec4: float
    filename: str
    checksum: str
    infobitssci: int
    infobitsref: int
    rfid: int
    refimfilename: str
    refimchecksum: str
    refim_ppid: int
    #: Angular distance from the tile centre, degrees. Computed by the
    #: query (`q3c_dist`) rather than by the caller, because the caller
    #: would have to re-derive the same spherical geometry the index
    #: already applied.
    dist: float


class Filter(typing.NamedTuple):
    """One row of the Filters reference table."""

    fid: int
    name: str


_OVERLAP_SQL = (
    "select pid,expid,sca,a.fid,a.field,jd,ra0,dec0,ra1,dec1,ra2,dec2,"
    "ra3,dec3,ra4,dec4, "
    "a.filename,a.checksum,infobitssci,infobitsref,a.rfid,b.filename,"
    "b.checksum,b.ppid, "
    "q3c_dist(ra0, dec0, cast(%s as double precision), "
    "cast(%s as double precision)) as dist "
    "from DiffImages a, RefImages b "
    "where a.rfid = b.rfid "
    "and a.ppid = %s "
    "and jd >= %s "
    "and a.status > 0 "
    "and b.status > 0 "
    "and a.vbest > 0 "
    "and b.vbest > 0 "
    "and q3c_radial_query(ra0, dec0, "
    "cast(%s as double precision), "
    "cast(%s as double precision), "
    "cast(%s as double precision)) "
    "order by jd"
)

_FILTERS_SQL = "select fid,filter from Filters order by fid"

#: The default cone-search radius in degrees, as `RAPIDDB` defaulted it.
DEFAULT_SEARCH_RADIUS_DEG = 1.0


class DiffImageRepository:
    """Reads over `DiffImages` joined to `RefImages`, and `Filters`.

    Takes a connection it does not own and never commits: these are
    reads, and a read that committed would end a caller's transaction at
    a point the caller did not choose.
    """

    def __init__(self, conn):
        self._conn = conn

    def possibly_overlapping(self, ppid, jd_earliest, field_ra0, field_dec0,
                             radius_deg=None):
        """Difference images that may overlap a sky tile, ordered by jd.

        "Possibly" is the query's own word and worth keeping: the cone
        search is over image CENTRES, so it is a fast superset — the
        caller still tests real footprint overlap. A repository method
        named `overlapping` would promise a precision this query does
        not have.

        Returns a list, empty when nothing matched. It does not return
        `None`, and a failed query raises rather than looking like an
        empty sky.
        """
        radius = (DEFAULT_SEARCH_RADIUS_DEG if radius_deg is None
                  else radius_deg)
        params = (field_ra0, field_dec0, ppid, jd_earliest,
                  field_ra0, field_dec0, radius)
        rows = self._query("possibly_overlapping", _OVERLAP_SQL, params)
        return [OverlappingDiffImage(*row) for row in rows]

    def filters(self):
        """Every row of the Filters reference table, ordered by fid."""
        rows = self._query("filters", _FILTERS_SQL, ())
        return [Filter(*row) for row in rows]

    def _query(self, method, sql, params):
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except Exception as exc:                  # noqa: BLE001 — re-typed
            # Rolled back because psycopg2 leaves the transaction in a
            # failed state after an error: every subsequent statement on
            # this connection would raise InFailedSqlTransaction, and a
            # caller catching this error to try a different query would
            # get a confusing second failure instead.
            self._conn.rollback()
            raise RepositoryQueryFailed(method, str(exc)) from exc
