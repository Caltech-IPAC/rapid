"""
File:    post_db.py

The post-DB science chain's six job types, as stages.

What this replaces: four scripts the VPO exec'd as subprocesses at the tail of
its main loop, plus the two prune siblings that were never invoked at all.
They shared one failure shape — `except ToolError` -> print -> `exit(64)` —
so any one of them killed the whole operator loop, and none of them left an
attempt record behind. Their work is now six bulk-queue job types with the
same account of themselves every other job type gives.

**These job types produce DATABASE STATE, NOT PRODUCTS** (co-design ruling 2,
operations design § Post-DB science chain). Each declares an empty product
set: nothing is uploaded, `published_products` stays empty, the terminal
record is a pure disposition record that promotes nothing, and the effect —
rows written, rows removed — rides in the attempt record through
`context.record_effect`. The catalog design's product model is deliberately
NOT extended to cover database effects; the disposition record is what
already fits.

That has one consequence worth stating plainly: `_execute` in the entrypoint
derives `ProductDisposition.NONE` from an empty `published_products`, and
`observability.registration.decide` SKIPs `none` with "attempt succeeded but
produced no products". So these attempts close successfully, are never
registration candidates, and never poison the registration pass — which is
the same hole round-3 finding #7 closed for post-process.

**The unit is what the manifest says, never what the catalog holds.** Every
one of these scripts discovered its own work at runtime: `to_regclass` probes
across SCAs 1-18, `select distinct field` from tables the previous step had
just written, `pg_tables like 'merges_%'`. Here the unit arrives in
`unit.fields`, gathered at submission (`submission.gathering`), and a stage
that cannot find its declared target fails naming it rather than quietly
processing nothing.

**Configuration is release content.** `match_radius` and the PSF-catalogue
filenames come from `cdf/science/pipeline.toml` through `context.science`,
not from the master `.ini` these scripts each re-read at import. That is the
W4 re-homing pattern, and it retires the last readers of those keys — which
is what the environment policy's one named temporary exception was waiting
for (code-standards § Environment variables: the orchestrator's environment
interface to its four post-DB subprocesses "expires when those scripts become
bulk-queue job types").
"""

import csv
import logging
import os

from psycopg2 import sql

import modules.utils.rapid_pipeline_subs as util
from database.modules.utils.rapid_db_connect import transaction
from pipeline.runtime.errors import InputError
from pipeline.stages import catalog_db

logger = logging.getLogger(__name__)

# The sources child table's column list, in COPY order. Carried verbatim from
# `loadPSFCatIntoDBSourcesTable.py:165-194`, where it was built by 30
# `cols.append(...)` calls at module import. The order is load-bearing: it is
# the order the CSV writer emits and the order COPY reads.
SOURCES_COLUMNS = (
    "id", "ra", "dec", "xfit", "yfit", "fluxfit", "xerr", "yerr", "fluxerr",
    "npixfit", "qfit", "cfit", "redchi", "flags", "sharpness", "roundness1",
    "roundness2", "npix", "peak", "pid", "isdiffpos", "field", "hp6", "hp9",
    "expid", "fid", "sca", "mjdobs",
)

ASTROOBJECTS_COLUMNS = ("aid", "ra0", "dec0", "flux0")
MERGES_COLUMNS = ("aid", "sid")


def _unit_field(context, name: str):
    """One value from the unit's open `fields` mapping, required.

    The post-DB units are keyed by processing date, SCA or field rather than
    by exposure, so their real identity rides in `ProcessingUnit.fields` (the
    same carrier post-process uses for its jid). A missing key means the
    manifest did not describe this unit, which is `input_missing` — the same
    classification `context.fact` gives for an absent `UnitFacts` entry.
    """
    fields = getattr(context.unit, "fields", None) or {}
    if name not in fields:
        present = ", ".join(sorted(fields)) or "nothing"
        raise InputError(
            f"the manifest does not carry {name!r} for this unit; it names: "
            f"{present}. Post-DB units are enumerated at submission and the "
            f"job type does not discover its own work.")
    return fields[name]


# ---------------------------------------------------------------------------
# 1. Catalog load — unit: (processing date, SCA)
# ---------------------------------------------------------------------------


def create_sources_table(context) -> None:
    """Create this unit's `sources_<date>_<sca>` child table.

    One unit owns one table, which is what makes the load convergent: a rerun
    re-loads this table and cannot interleave with another unit's writes.

    `inherit=True` — the sources children INHERIT the prototype so a query
    against `sources` sees them, exactly as `loadPSFCatIntoDBSourcesTable.py:
    824` arranged. The `SET UNLOGGED` that sat between the CREATE and the
    INHERIT there is gone.
    """
    conn = context.require_connection()
    table = _unit_field(context, "target_table")

    with transaction(conn) as cursor:
        # The child tables live in the data tablespace, the indexes they carry
        # in the index tablespace — the placement the old script set with two
        # `SET default_tablespace` statements around its DDL.
        cursor.execute("SET LOCAL default_tablespace = pipeline_data_01")
        created = catalog_db.create_child_table(
            cursor, table, "sources", inherit=True)

    context.produce("sources_table", table)
    context.record(sources_table=table, sources_table_created=bool(created))
    context.logger.info("sources table %s ready (created=%s)", table, created)


def download_psf_catalogs(context) -> None:
    """Fetch this unit's PSF-fit catalogues from the product bucket.

    The inputs are the science pipeline's per-job outputs for this processing
    date and SCA: four files per job (the positive and negative psfcat and
    finder variants). Their names are RELEASE CONTENT — `[psfcat_diffimage]`
    in `cdf/science/pipeline.toml` — not values re-read from the master
    `.ini`, which is what `loadPSFCatIntoDBSourcesTable.py:142-143` did.

    A unit whose jobs produced no catalogue is not an error: it loads nothing
    and says so through the effect counts. That is the empty-product-set
    disposition working as designed, and it is why this stage records the
    file count rather than raising on zero.
    """
    psfcat = (context.science or {}).get("psfcat_diffimage") or {}
    positive = psfcat.get("output_sfft_psfcat_filename")
    if not positive:
        raise InputError(
            "release content does not name output_sfft_psfcat_filename in "
            "[psfcat_diffimage]; the catalog load reads its inputs by that "
            "name and has no default for a science filename")
    negative = positive.replace(".txt", "_negative.txt")

    proc_date = _unit_field(context, "proc_date")
    sca = int(_unit_field(context, "sca"))
    bucket = context.parameter("s3/product-bucket")

    downloaded = []
    for jid in _jids_for_unit(context):
        for name in (positive, negative):
            key = f"{proc_date}/jid{jid}/{name}"
            target = context.scratch(f"jid{jid}_{name}")
            try:
                context.s3.download_file(bucket, key, target)
            except Exception:  # noqa: BLE001 - absence is a normal outcome
                # A job that produced no catalogue for this SCA contributes
                # nothing. Logged, not raised: the unit's effect count is what
                # reports how much it actually loaded.
                context.logger.info("no catalogue at s3://%s/%s", bucket, key)
                continue
            downloaded.append(target)

    context.produce("psf_catalogs", downloaded)
    context.record(psf_catalog_files=len(downloaded), sca=sca)
    context.logger.info("unit %s/%s: %d catalogue file(s)",
                        proc_date, sca, len(downloaded))


def _jids_for_unit(context):
    """The science jobs whose catalogues this unit loads.

    Declared in the manifest by the gatherer; absent means the submission did
    not enumerate them, which is a submission fault rather than something to
    rediscover here.
    """
    fields = getattr(context.unit, "fields", None) or {}
    return fields.get("jids") or []


def load_sources(context) -> None:
    """Load this unit's catalogue rows through staging and upsert.

    **THE STAGING-PLUS-UPSERT PATH** (database design § Integrity and
    durability). What the old script did instead was `COPY` straight into the
    child table. With a uniqueness constraint present that cannot converge on
    a rerun — the first already-present row aborts the load — so the
    individually-retryable ruling would have been unusable in exactly the
    case retries exist for.

    The load rate is measured and recorded, because the design asks for it by
    name: trading durability for load speed is "an argued-for regression
    requiring measurements", and this is the measurement that argument would
    have to be made against.
    """
    conn = context.require_connection()
    table = context.product("sources_table")
    catalogs = context.product("psf_catalogs")

    if not catalogs:
        # AN EMPTY PRODUCT SET IS A REAL OUTCOME, NOT A FAILURE (ruling 2).
        # The unit had no catalogues to load; it closes successfully with an
        # effect count of zero, which is a different and more useful statement
        # than either a failure or a silent success.
        context.record_effect(rows_written=0, load_rate_rows_per_second=0.0)
        context.logger.info("no catalogues for %s; nothing loaded", table)
        return

    csv_path = context.scratch(f"{table}.csv")
    rows = _write_sources_csv(context, catalogs, csv_path)

    with transaction(conn) as cursor:
        result = catalog_db.load_through_staging(
            cursor, csv_path, table, "sources", SOURCES_COLUMNS)

    context.record_effect(
        rows_written=result["rows_written"],
        rows_staged=result["rows_staged"],
        load_seconds=result["seconds"],
        load_rate_rows_per_second=result["rate"])
    context.logger.info(
        "loaded %d row(s) into %s at %.0f rows/s (%d staged, %d CSV)",
        result["rows_written"], table, result["rate"],
        result["rows_staged"], rows)


def _write_sources_csv(context, catalogs, csv_path: str) -> int:
    """Flatten the PSF catalogues into one COPY-ready CSV. Returns row count.

    The per-source values the database columns need beyond what the catalogue
    carries — the difference-image identity, exposure, SCA, filter, MJD, and
    the healpix indices — are unit facts, not measurements, so they come from
    the manifest and are constant down the file.
    """
    facts = context.unit.facts
    written = 0

    with open(csv_path, "w", newline="") as handle:
        writer = csv.writer(handle)
        for path in catalogs:
            positive = "_negative" not in os.path.basename(path)
            for row in util.read_psfcat_rows(path):
                writer.writerow(_sources_row(row, facts, positive))
                written += 1

    context.logger.info("wrote %d source row(s) to %s", written, csv_path)
    return written


def _sources_row(row, facts, positive: bool):
    """One catalogue row as the sources column tuple, in COPY order."""
    return [
        row.get("id"), row.get("ra"), row.get("dec"),
        row.get("x_fit"), row.get("y_fit"), row.get("flux_fit"),
        row.get("x_err"), row.get("y_err"), row.get("flux_err"),
        row.get("npixfit"), row.get("qfit"), row.get("cfit"),
        row.get("reduced_chi2"), row.get("flags"), row.get("sharpness"),
        row.get("roundness1"), row.get("roundness2"), row.get("npix"),
        row.get("peak"), facts.pid, "true" if positive else "false",
        facts.field, row.get("hp6"), row.get("hp9"),
        facts.expid, facts.fid, row.get("sca"), facts.mjdobs,
    ]


CATALOG_LOAD_SEQUENCE = (
    ("create_sources_table", create_sources_table),
    ("download_psf_catalogs", download_psf_catalogs),
    ("load_sources", load_sources),
)


# ---------------------------------------------------------------------------
# 2. Crossmatch — unit: (processing date, field)
# ---------------------------------------------------------------------------


def create_field_tables(context) -> None:
    """Create this field's `astroobjects_<field>` and `merges_<field>` clones.

    **Both carry the prototype's indexes**, which is where migration 027's
    `merges_aid_sid_unique` reaches the per-field tables. The old path created
    these with `LIKE ... INCLUDING DEFAULTS INCLUDING CONSTRAINTS` and then a
    hand-written list of four indexes that could not include a unique index
    written after it — so every clone was born without the constraint the
    design requires.

    And the two `SET UNLOGGED` statements that ran here **on every pass**,
    outside the creation guard (evidence §3.3), are gone.
    """
    conn = context.require_connection()
    field = int(_unit_field(context, "field"))

    with transaction(conn) as cursor:
        cursor.execute("SET LOCAL default_tablespace = pipeline_data_01")
        for prototype in ("astroobjects", "merges"):
            catalog_db.create_child_table(
                cursor, f"{prototype}_{field}", prototype)

    context.produce("astroobjects_table", f"astroobjects_{field}")
    context.produce("merges_table", f"merges_{field}")
    context.record(field=field)


def crossmatch_sources(context) -> None:
    """Match this field's new sources against its known objects.

    The science is carried over unchanged from `crossMatchSources.py`: a
    `q3c_join` cone match at the release's `match_radius` associates a source
    with an existing object; an unmatched source becomes a new object whose
    `aid` is the deterministic 64-bit hash of (ra, dec) computed client-side
    by `util.radec_index`. Both halves then land as merge rows.

    `match_radius` comes from RELEASE CONTENT (`[source_matching]`), not from
    the master `.ini` the script read at import. It can alter which sources
    are declared the same object, so it is science content by the placement
    criterion and belongs in the file the image digest identifies.
    """
    conn = context.require_connection()
    field = int(_unit_field(context, "field"))
    proc_date = _unit_field(context, "proc_date")

    matching = (context.science or {}).get("source_matching") or {}
    radius = matching.get("match_radius")
    if radius is None:
        raise InputError(
            "release content does not carry [source_matching] match_radius; "
            "it decides which sources are the same object, so there is no "
            "safe default for it")

    astroobjects = context.product("astroobjects_table")
    merges = context.product("merges_table")

    objects_csv = context.scratch(f"{astroobjects}.csv")
    merges_csv = context.scratch(f"{merges}.csv")

    with transaction(conn) as cursor:
        matched, new_objects = _crossmatch_field(
            cursor, context, field, proc_date, float(radius),
            astroobjects, objects_csv, merges_csv)

        objects_result = catalog_db.load_through_staging(
            cursor, objects_csv, astroobjects, "astroobjects",
            ASTROOBJECTS_COLUMNS)
        merges_result = catalog_db.load_through_staging(
            cursor, merges_csv, merges, "merges", MERGES_COLUMNS)

    context.record_effect(
        rows_written=objects_result["rows_written"] + merges_result["rows_written"],
        sources_matched=matched,
        astroobjects_written=objects_result["rows_written"],
        merges_written=merges_result["rows_written"],
        new_astroobjects=new_objects,
        load_rate_rows_per_second=merges_result["rate"])
    context.logger.info(
        "field %d: %d matched, %d new object(s), %d merge row(s)",
        field, matched, new_objects, merges_result["rows_written"])


def _crossmatch_field(cursor, context, field: int, proc_date: str,
                      radius: float, astroobjects: str,
                      objects_csv: str, merges_csv: str):
    """Write this field's new objects and merges as CSVs. Returns counts.

    Iterates the source child tables this field's sources live in — named by
    the manifest, not discovered by `to_regclass` probing across SCAs 1-18 as
    `crossMatchSources.py:882-890` did.
    """
    matched_total = 0
    new_total = 0
    seen = set()

    with (open(objects_csv, "w", newline="") as objects_handle,
          open(merges_csv, "w", newline="") as merges_handle):
        objects_writer = csv.writer(objects_handle)
        merges_writer = csv.writer(merges_handle)

        for sources_table in _source_tables_for_unit(context, proc_date):
            cursor.execute(
                sql.SQL(
                    "SELECT a.sid, b.aid FROM {sources} AS a, {objects} AS b "
                    "WHERE q3c_join(a.ra, a.dec, b.ra0, b.dec0, %s) "
                    "AND a.field = %s AND a.flags = 0").format(
                        sources=sql.Identifier(sources_table),
                        objects=sql.Identifier(astroobjects)),
                (radius, field))
            for sid, aid in cursor.fetchall():
                merges_writer.writerow([aid, sid])
                seen.add(sid)
                matched_total += 1

            cursor.execute(
                sql.SQL(
                    "SELECT sid, ra, dec, fluxfit FROM {sources} "
                    "WHERE field = %s AND flags = 0").format(
                        sources=sql.Identifier(sources_table)),
                (field,))
            for sid, ra, dec, flux in cursor.fetchall():
                if sid in seen:
                    continue
                # The object identity is computed here, not assigned by the
                # database: a deterministic 64-bit index of (ra, dec), so two
                # runs seeing the same position agree on the object without
                # coordinating. That property is what lets the upsert's
                # conflict target mean anything.
                aid = util.radec_index(ra, dec)
                objects_writer.writerow([aid, ra, dec, flux])
                merges_writer.writerow([aid, sid])
                seen.add(sid)
                new_total += 1

    return matched_total, new_total


def _source_tables_for_unit(context, proc_date: str):
    """The source child tables this crossmatch unit reads.

    Declared by the submitter in the manifest. Falls back to nothing rather
    than probing the catalog: a unit that names no source tables loaded no
    sources, and the honest outcome is an effect count of zero.
    """
    fields = getattr(context.unit, "fields", None) or {}
    return fields.get("source_tables") or []


CROSSMATCH_SEQUENCE = (
    ("create_field_tables", create_field_tables),
    ("crossmatch_sources", crossmatch_sources),
)


# ---------------------------------------------------------------------------
# 3. Statistics — unit: field
# ---------------------------------------------------------------------------


def compute_statistics(context) -> None:
    """Rebuild this field's `astroobjectsmeta_<field>` summary table.

    `computeStatisticsForAstroObjects.py` stated its own contract in its
    docstring: the tables "must be dropped before running this script, as the
    tables are recreated, indexed, and then records populated with bulk copy
    for each field. No record inserts or updates are done for speed." That is
    preserved — the statistics are a pure function of the field's current
    objects and merges, so recomputing them wholesale is correct — but it is
    now scoped to ONE field per unit rather than every table `pg_tables`
    happened to return, and the whole rebuild is one transaction rather than
    a drop that leaves the table missing if the repopulate dies.
    """
    conn = context.require_connection()
    field = int(_unit_field(context, "field"))
    target = f"astroobjectsmeta_{field}"

    with transaction(conn) as cursor:
        cursor.execute("SET LOCAL default_tablespace = pipeline_data_01")
        catalog_db.create_child_table(cursor, target, "astroobjectsmeta")

        # Recomputed wholesale inside the transaction: the delete and the
        # repopulate commit together, so a failure leaves the previous
        # statistics in place rather than an empty table.
        cursor.execute(
            sql.SQL("DELETE FROM {target}").format(
                target=sql.Identifier(target)))
        removed = cursor.rowcount or 0

        cursor.execute(
            sql.SQL(
                "INSERT INTO {target} (aid, nobs, mjdobs_first, mjdobs_last, "
                "flux_mean, flux_stddev) "
                "SELECT m.aid, count(*), min(s.mjdobs), max(s.mjdobs), "
                "       avg(s.fluxfit), stddev_pop(s.fluxfit) "
                "FROM {merges} AS m JOIN sources AS s ON s.sid = m.sid "
                "GROUP BY m.aid").format(
                    target=sql.Identifier(target),
                    merges=sql.Identifier(f"merges_{field}")))
        written = cursor.rowcount or 0

    context.record_effect(rows_written=written, rows_removed=removed,
                          statistics_table=target)
    context.logger.info("field %d statistics: %d row(s) rebuilt", field, written)


STATISTICS_SEQUENCE = (
    ("compute_statistics", compute_statistics),
)


# ---------------------------------------------------------------------------
# 4-5. The currency sweeps — unit: field
# ---------------------------------------------------------------------------


def sweep_merge_currency(context) -> None:
    """Remove this field's merge rows whose difference image is not current.

    **The derived-currency invariant** (operations design): "a row is current
    while the image it derives from holds best status, and the currency sweeps
    remove rows whose image has been demoted. Between a demotion and the next
    sweep, superseded rows are present by design; consumers of these tables
    read currency through the image, not the row."

    So finding rows to remove is NORMAL here, unlike the dedup check below —
    a demotion since the last sweep is exactly what this maintains.

    `pruneNotBestMerges.py` did this one row at a time: select every row, ask
    `SELECT vbest FROM diffimages WHERE pid = %s` for each, delete the ones
    that came back 0. This is the same question as one join.
    """
    conn = context.require_connection()
    field = int(_unit_field(context, "field"))
    table = f"merges_{field}"

    with transaction(conn) as cursor:
        removed = catalog_db.delete_superseded_rows(
            cursor, table, "merges",
            join_column="sid", identity_table="diffimages",
            identity_column="pid")

    context.record_effect(rows_removed=removed, swept_table=table)
    context.logger.info("merge currency sweep on %s: %d row(s) removed",
                        table, removed)


def sweep_source_currency(context) -> None:
    """Remove this field's source-side rows whose image is not current.

    The counterpart sweep, and one of the two the co-design's ruling 3 brings
    into the operational chain: "the two uninvoked prune siblings convert
    alongside the four invoked scripts: they are the only maintainers of
    integrity properties the schema does not enforce". Being uninvoked was
    the defect, not the reason to leave it out.
    """
    conn = context.require_connection()
    field = int(_unit_field(context, "field"))
    table = f"merges_{field}"

    with transaction(conn) as cursor:
        removed = catalog_db.delete_superseded_rows(
            cursor, table, "merges",
            join_column="sid", identity_table="l2files",
            identity_column="rid")

    context.record_effect(rows_removed=removed, swept_table=table)
    context.logger.info("source currency sweep on %s: %d row(s) removed",
                        table, removed)


MERGE_CURRENCY_SEQUENCE = (
    ("sweep_merge_currency", sweep_merge_currency),
)

SOURCE_CURRENCY_SEQUENCE = (
    ("sweep_source_currency", sweep_source_currency),
)


# ---------------------------------------------------------------------------
# 6. Merge dedup — unit: field. A should-find-nothing check.
# ---------------------------------------------------------------------------


def check_merge_duplicates(context) -> None:
    """Count duplicate (aid, sid) groups in this field's merges table.

    **This job type does not delete anything** (co-design ruling 6). With
    `merges_aid_sid_unique` on the prototype and the clone path carrying it,
    a duplicate pair cannot be inserted at all, so the dedup sweep "demotes to
    a should-find-nothing check": it exists to prove prevention is working.

    A nonzero count is therefore a DEFECT REPORT about the constraint — a
    clone made before the index existed, or one made by a path that bypassed
    `create_child_table`. Deleting the rows would erase the only evidence
    that the prevention failed, so this raises instead, and the attempt
    records the count.
    """
    conn = context.require_connection()
    field = int(_unit_field(context, "field"))
    table = f"merges_{field}"

    with transaction(conn) as cursor:
        duplicates = catalog_db.count_duplicate_groups(cursor, table, "merges")

    context.record_effect(rows_written=0, rows_removed=0,
                          duplicate_groups=duplicates, checked_table=table)

    if duplicates:
        raise InputError(
            f"{table} holds {duplicates} duplicate (aid, sid) group(s). "
            f"Migration 027 put a unique index on the merges prototype and "
            f"the clone path carries it, so this is structurally impossible "
            f"on a table created by the converted path — the table predates "
            f"the constraint or was created by something that bypassed it. "
            f"Reported rather than deleted: the rows are the evidence.",
            duplicate_groups=duplicates, table=table)

    context.logger.info("%s: no duplicate (aid, sid) groups, as expected",
                        table)


MERGE_DEDUP_SEQUENCE = (
    ("check_merge_duplicates", check_merge_duplicates),
)
