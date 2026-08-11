"""The sky-catalog repository: fields, and the per-field object tables.

Carves the `execute_sql_queries` calls that
`pipeline/generateLightCurveHATSCatalog.py` and
`pipeline/generateSourceHATSCatalog.py` make into named methods over the
per-field `astroobjects_<field>` / `merges_<field>` tables and the
`sources` table.

WHY THIS FAMILY. Its call sites carry two defects the carve removes,
both invisible at the call site today:

  (a) `dbh.execute_sql_queries(sql_queries, debug)` passes `debug`
      POSITIONALLY into the method's `params_list` parameter. The method
      then evaluates `params_list[i]` — subscripting the integer 1 —
      because its guard tests `params_list is not None` rather than
      truthiness. All three legacy call sites do this.
  (b) `execute_sql_queries` binds its `records` list inside the loop and
      returns only the LAST query's rows ("code is not generalized", per
      its own comment), so an empty query list raises `UnboundLocalError`
      rather than returning nothing.

Neither is preserved here: each method issues one query and returns its
own rows.

THE TABLE NAMES ARE COMPOSED, AND THAT IS THE ONE UNAVOIDABLE
INTERPOLATION. `astroobjects_<field>` is a real table per sky field, so
the table name genuinely varies per call and cannot be a bound parameter
— SQL has no placeholder for an identifier. The call sites do this with
f-strings. Here it goes through `psycopg2.sql.Identifier`, which quotes
and escapes the identifier properly, and the field is additionally
validated against the pattern the field tables actually use, so a
caller cannot reach the composer with arbitrary text.
"""

import re
import typing

from pipeline.repositories.errors import RepositoryQueryFailed

#: Field names are the suffix of a real table name (`merges_<field>`), so
#: they are drawn from the catalog itself in ordinary use. Validated anyway:
#: the one interpolation in this module is an identifier, and an identifier
#: composed from unvalidated input is how an injection reaches a query that
#: has no placeholder to protect it.
_FIELD_RE = re.compile(r"\A[A-Za-z0-9_]{1,48}\Z")


class AstroObject(typing.NamedTuple):
    """One row of a per-field `astroobjects_<field>` table."""

    aid: int
    ra: float
    dec: float
    nsources: int


class Source(typing.NamedTuple):
    """One row of the `sources` table, as the HATS generator reads it."""

    sid: int
    ra: float
    dec: float


class SkyCatalogRepository:
    """Reads over the per-field catalog tables and `sources`.

    Takes a connection it does not own and never commits.
    """

    def __init__(self, conn):
        self._conn = conn

    def fields(self):
        """Every sky field with a `merges_<field>` table, as a name list.

        Reads the catalog rather than a configuration list, exactly as
        the call sites do: the fields that exist are the fields that have
        tables, and any second list would drift from that one.
        """
        rows = self._query(
            "fields",
            "select tablename from pg_tables where schemaname = %s "
            "and tablename like %s order by tablename",
            ("public", "merges_%"))
        return [row[0].replace("merges_", "", 1) for row in rows]

    def astroobject_ids(self, field):
        """Ordered `aid`s in a field that have at least one source.

        The `nsources > 0` predicate is the call sites' own: an object
        with no sources has no lightcurve to write.
        """
        table = self._field_table("astroobjects", field)
        rows = self._query(
            "astroobject_ids",
            self._compose("SELECT aid FROM {} WHERE nsources > 0 "
                          "ORDER BY aid", table),
            ())
        return [row[0] for row in rows]

    def astroobjects_in_range(self, field, start_aid, end_aid):
        """Objects in a field within an inclusive `aid` range.

        The range is how the generators chunk a field into fixed-size
        parquet files; it is inclusive at both ends because the call
        sites compute `end_aid` as the last element of a slice, not one
        past it.
        """
        table = self._field_table("astroobjects", field)
        rows = self._query(
            "astroobjects_in_range",
            self._compose("SELECT aid, ra, dec, nsources FROM {} "
                          "WHERE aid >= %s AND aid <= %s ORDER BY aid",
                          table),
            (start_aid, end_aid))
        return [AstroObject(*row) for row in rows]

    def source_ids(self):
        """Every `sid` in the `sources` table, ordered."""
        rows = self._query("source_ids",
                           "SELECT sid FROM sources ORDER BY sid", ())
        return [row[0] for row in rows]

    def sources_in_range(self, start_sid, end_sid):
        """Sources within an inclusive `sid` range, ordered.

        The columns are fixed here rather than passed in as a string.
        The call site interpolates a `sources_cols` variable straight
        into the SELECT list, which is a column list in SQL text — the
        thing `rapid_db_connect.select_columns` exists to replace, and a
        place a repository can simply be explicit instead.
        """
        rows = self._query(
            "sources_in_range",
            "SELECT sid, ra, dec FROM sources "
            "WHERE sid >= %s AND sid <= %s ORDER BY sid",
            (start_sid, end_sid))
        return [Source(*row) for row in rows]

    def source_columns_in_range(self, columns, start_sid, end_sid):
        """Chosen columns of `sources` within an inclusive `sid` range.

        The configurable-column sibling of `sources_in_range`, for the
        source-catalog generator, which writes whichever columns its
        science configuration names straight out to CSV. Rows come back
        as raw tuples in the requested order, matching the header the
        caller writes from the same list.
        """
        rows = self._query(
            "source_columns_in_range",
            self._compose_columns(
                "SELECT {cols} FROM {tbl} WHERE sid >= %s AND sid <= %s "
                "ORDER BY sid", "sources", columns),
            (start_sid, end_sid))
        return rows

    def astroobject_columns_in_range(self, field, columns, start_aid,
                                     end_aid):
        """Chosen columns of a field's objects within an inclusive range.

        The column LIST is caller-supplied here, unlike
        `astroobjects_in_range` above, because the HATS generators read
        it from their science configuration — which columns land in the
        catalog is a product decision, not a repository one.

        Caller-supplied columns are still identifiers, so they go through
        `sql.Identifier` exactly as the table name does, and each is
        validated first. The call site this replaced interpolated the
        configuration string straight into the SELECT list.

        Returns raw tuples in the requested column order — deliberately,
        because a NamedTuple cannot have a shape that varies per call.
        The caller zips them against the same column list it passed in,
        which is the only correspondence that can be right by
        construction.
        """
        table = self._field_table("astroobjects", field)
        rows = self._query(
            "astroobject_columns_in_range",
            self._compose_columns(
                "SELECT {cols} FROM {tbl} WHERE aid >= %s AND aid <= %s "
                "ORDER BY aid", table, columns),
            (start_aid, end_aid))
        return rows

    def joined_sources_in_range(self, field, columns, join_index, start_aid,
                                end_aid):
        """Source rows joined to a field's merge table, ordered for nesting.

        The `ORDER BY aid, mjdobs` is load-bearing and carried over
        unchanged: the caller builds a nested lightcurve frame from this
        result, and the per-object time ordering is what makes each
        nested cell a lightcurve rather than a bag of measurements.
        """
        merges = self._field_table("merges", field)
        rows = self._query(
            "joined_sources_in_range",
            self._compose_joined(merges, join_index, columns),
            (start_aid, end_aid))
        return rows

    # -- composition and execution -----------------------------------------
    def _field_table(self, prefix, field):
        if not _FIELD_RE.match(str(field)):
            raise RepositoryQueryFailed(
                "_field_table",
                "field name %r is not a valid table suffix" % (field,))
        return "%s_%s" % (prefix, field)

    def _compose(self, template, table):
        """Compose a statement whose only variable part is a table name."""
        from psycopg2 import sql                  # noqa: PLC0415
        return sql.SQL(template).format(sql.Identifier(table))

    def _identifiers(self, columns):
        """Validate and quote a caller-supplied column list."""
        from psycopg2 import sql                  # noqa: PLC0415
        names = [c.strip() for c in columns]
        for name in names:
            if not _FIELD_RE.match(name):
                raise RepositoryQueryFailed(
                    "_identifiers",
                    "column name %r is not a valid identifier" % (name,))
        return sql.SQL(", ").join(sql.Identifier(n) for n in names)

    def _compose_columns(self, template, table, columns):
        from psycopg2 import sql                  # noqa: PLC0415
        return sql.SQL(template).format(cols=self._identifiers(columns),
                                        tbl=sql.Identifier(table))

    def _compose_joined(self, merges, join_index, columns):
        """The sources-joined-to-merges statement, fully quoted.

        Built separately from `_compose_columns` because the column list
        here is qualified to the `sources` alias while the join index is
        qualified to neither — a shape a single template could express
        only by making the qualification a caller's responsibility, which
        is how the interpolated version got its `a.` prefix glued onto
        only the first column of a comma-separated string.
        """
        from psycopg2 import sql                  # noqa: PLC0415
        names = [c.strip() for c in columns]
        for name in names + [join_index]:
            if not _FIELD_RE.match(name):
                raise RepositoryQueryFailed(
                    "_compose_joined",
                    "column name %r is not a valid identifier" % (name,))
        qualified = sql.SQL(", ").join(
            sql.SQL("a.") + sql.Identifier(n) for n in names)
        return sql.SQL(
            "SELECT {idx}, {cols} FROM sources a, {merges} b "
            "WHERE a.sid = b.sid AND aid >= %s AND aid <= %s "
            "ORDER BY aid, mjdobs").format(
                idx=sql.Identifier(join_index),
                cols=qualified,
                merges=sql.Identifier(merges))

    def _query(self, method, statement, params):
        try:
            with self._conn.cursor() as cur:
                cur.execute(statement, params)
                return cur.fetchall()
        except Exception as exc:                  # noqa: BLE001 — re-typed
            self._conn.rollback()
            raise RepositoryQueryFailed(method, str(exc)) from exc
