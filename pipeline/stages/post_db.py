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

**EFFECT-CLASS, LIKE ALERT PRODUCTION** (ruling R1, extended to the six
post-DB types). These are `submission.subjects.is_product_producing() ==
False` — a database-effect job type, ruling 2's other half — and every
non-product-producing job type now closes through the effect-confirmation
boundary, not through the plain published/none split. Each stage here writes
inside a transaction, then RE-QUERIES after that transaction commits
(`_verify_effect`, `_verify_no_superseded_rows`) to confirm the write is
durably visible, and produces the result as the `effect_outcome` stage fact
(`"confirmed"` or `"unconfirmed"`) the same way alert production's claim/
confirm protocol does. `pipeline.entrypoints.job._execute` maps that fact to
`ProductDisposition.EFFECT_CONFIRMED`/`EFFECT_UNCONFIRMED` and its fail-closed
guard refuses to close `success`+`none` for a database effect this attempt
recorded no verified outcome for — so, UNLIKE THIS MODULE'S EARLIER
DOCSTRING, these attempts ARE registration candidates: `_apply_skip_
disposition` (`pipeline.registration.consumer`) is what disposes an
`effect_confirmed`/`effect_unconfirmed` verdict, not `observability.
registration.decide`'s published/none path.

**The unit is what the manifest says, never what the catalog holds.** Every
one of these scripts discovered its own work at runtime: `to_regclass` probes
across SCAs 1-18, `select distinct field` from tables the previous step had
just written, `pg_tables like 'merges_%'`. Here the unit arrives in the
unit's typed payload (`submission.payloads`), gathered at submission
(`submission.gathering`), and a stage that cannot find its declared target
fails naming it rather than quietly processing nothing.

**Configuration is release content.** `match_radius` and the PSF-catalogue
filenames come from `cdf/science/pipeline.toml` through `context.science`,
not from the master `.ini` these scripts each re-read at import. That is the
W4 re-homing pattern, and it retires the last readers of those keys — which
is what the environment policy's one named temporary exception was waiting
for (code-standards § Environment variables: the orchestrator's environment
interface to its four post-DB subprocesses "expires when those scripts become
bulk-queue job types").
"""

import contextlib
import csv
import logging
import os

from psycopg2 import sql

import modules.utils.rapid_pipeline_subs as util
from database.modules.utils.rapid_db_connect import transaction
from pipeline.association import sets as association_sets
from pipeline.association import watermark as association_watermark
from pipeline.runtime.errors import DBError, InputError
from pipeline.stages import catalog_db

logger = logging.getLogger(__name__)

#: The effect-outcome vocabulary these stages report through `context.produce
#: ("effect_outcome", ...)` — the same two values `RAPIDDB.CONFIRM_OUTCOME_
#: CONFIRMED`/`CONFIRM_OUTCOME_UNCONFIRMED` use (`database.modules.utils.
#: rapid_db`), because `pipeline.entrypoints.job._EFFECT_OUTCOME_TO_
#: DISPOSITION` maps that exact vocabulary to a `ProductDisposition` and this
#: module does not get to invent a second spelling of it. Only these two
#: values are reachable here: a post-DB unit has no concurrent claimant to
#: defer to and nothing pre-existing to find "terminally satisfied" —
#: `held_by_live_owner`/`deferred`/`terminally_satisfied` are alert-
#: production's claim/confirm vocabulary for a shared subject, and these
#: stages each own their table exclusively for the unit's one transaction.
EFFECT_OUTCOME_CONFIRMED = "confirmed"
EFFECT_OUTCOME_UNCONFIRMED = "unconfirmed"


def _verify_effect(conn, context, description: str, query, params,
                   expected: int) -> str:
    """Re-query the database, AFTER the writing transaction has committed,
    to confirm the effect this unit just wrote is durably visible.

    **A REAL POST-WRITE VERIFICATION, NOT A TRUST OF THE WRITE STATEMENT'S
    OWN REPORT.** `cursor.rowcount` on the INSERT/DELETE that just ran is
    the database's word for what one statement did, inside a transaction
    that might still roll back for a reason outside that statement's view
    (a later statement in the same `with transaction(conn)` block, or the
    commit itself). This runs on a FRESH cursor, in a NEW transaction opened
    only after `transaction(conn)`'s own commit has returned, and asks the
    question the effect-lifecycle boundary actually cares about: is this
    fact true of the database right now. `conn.autocommit` is off
    (`database.modules.utils.rapid_db_connect`), so the new cursor's first
    statement opens its own transaction and reads read-committed state —
    which includes this unit's own just-committed write.

    Returns `EFFECT_OUTCOME_CONFIRMED` when the re-query matches what the
    unit expected to find, `EFFECT_OUTCOME_UNCONFIRMED` otherwise — a
    mismatch (or a query that itself fails) means this attempt cannot state
    that its database effect landed, which is exactly the
    `disposition_for_unconfirmed_effect` retry path's job to handle, not
    something this stage papers over with a raised exception.
    """
    try:
        with conn.cursor() as cursor:
            cursor.execute(query, params)
            row = cursor.fetchone()
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - classified by the caller's fail-closed guard
        context.logger.error(
            "%s: post-write verification query failed (%s: %s); the effect "
            "cannot be confirmed", description, type(exc).__name__, exc)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            context.logger.exception(
                "rollback of the failed verification query also failed")
        return EFFECT_OUTCOME_UNCONFIRMED

    found = int(row[0]) if row else 0
    if found != expected:
        context.logger.error(
            "%s: post-write verification found %d, expected %d; the "
            "effect is not confirmed", description, found, expected)
        return EFFECT_OUTCOME_UNCONFIRMED

    context.logger.info("%s: post-write verification confirmed (%d)",
                        description, found)
    return EFFECT_OUTCOME_CONFIRMED


def _verify_no_superseded_rows(conn, context, description: str,
                               tablename: str, join_column: str,
                               identity_table: str,
                               identity_column: str) -> str:
    """Confirm a currency sweep's DELETE landed: re-run its own predicate.

    The same NOT-EXISTS shape `catalog_db.delete_superseded_rows` deletes
    by (`vbest IN (1, 2)` is current), asked AFTER the deleting transaction
    has committed, on a fresh cursor. A currency sweep's effect is not a row
    count — a demotion after this sweep committed legitimately leaves new
    superseded rows behind, and counting them would misread ordinary
    ongoing operation as an unconfirmed effect. What confirms is that the
    rows THIS sweep found superseded, at the moment it deleted them, are no
    longer among the CURRENT set of superseded rows for that predicate
    computed fresh — which a re-run of the identical predicate finding zero
    (immediately after this sweep's own commit, before anything else could
    have written a new demotion) verifies directly.
    """
    query = sql.SQL(
        "SELECT count(*) FROM {child} WHERE NOT EXISTS ("
        "  SELECT 1 FROM {identity} WHERE {identity}.{idcol} = "
        "  {child}.{joincol} AND {identity}.vbest IN (1, 2))").format(
            child=sql.Identifier(tablename),
            identity=sql.Identifier(identity_table),
            idcol=sql.Identifier(identity_column),
            joincol=sql.Identifier(join_column))
    return _verify_effect(conn, context, description, query, (), 0)


def _table_count(cursor, tablename: str) -> int:
    """`SELECT count(*)` on a child table, INSIDE the writing transaction.

    Used to compute a before/after expectation for `_verify_effect`'s
    post-commit re-query — the table is this unit's own (a (date, SCA) or
    (date, field) or field grain owns its child table exclusively for the
    unit's transaction), so a plain `before + written` is a sound
    expectation for what the post-commit count should be.
    """
    cursor.execute(
        sql.SQL("SELECT count(*) FROM {table}").format(
            table=sql.Identifier(tablename)))
    row = cursor.fetchone()
    return int(row[0]) if row else 0


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
    """One declared value from the unit's TYPED payload, required.

    The post-DB units are keyed by processing date, SCA or field rather than
    by exposure, so their real identity rides in the unit's payload
    (`submission.payloads`). A missing value means the manifest did not
    describe this unit, which is `input_missing` — the same classification
    `context.fact` gives for an absent `UnitFacts` entry.

    **READ FROM THE PAYLOAD, NOT AN OPEN DICT** (rule 11). This used to read
    `ProcessingUnit.fields`, an open `dict[str, Any]` — the "parallel
    untyped fact carrier" the rule prohibits. The lookup shape is kept
    deliberately: every call site asks for one named value and wants one
    named failure, and that contract is unchanged. What changed is that the
    name must be one the payload type DECLARES, so a typo is a failure here
    rather than a silently-absent key that the old dict answered with a
    `KeyError` about a name nothing had ever declared.
    """
    payload = getattr(context.unit, "payload", None)
    if payload is None:
        raise InputError(
            "this unit carries no typed payload; units written against the "
            "pre-rule-11 manifest schema carried an open `fields` dict "
            "instead, and manifest schema version 4 refuses them rather "
            "than translating them.")
    declared = tuple(payload.COMPONENTS) + tuple(payload.INVOCATION_FACTS)
    if name not in declared:
        raise InputError(
            f"a {payload.JOB_TYPE!r} unit does not declare {name!r}; it "
            f"declares: {', '.join(declared) or 'nothing'}. Post-DB units "
            f"are enumerated at submission and the job type does not "
            f"discover its own work.")
    value = getattr(payload, name, None)
    if value is None:
        raise InputError(
            f"the manifest does not carry {name!r} for this "
            f"{payload.JOB_TYPE!r} unit, though its payload type declares "
            f"it. Post-DB units are enumerated at submission and the job "
            f"type does not discover its own work.")
    return value


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
        _place_in_data_tablespace(cursor, context.logger)
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

    **THE KEYS ARE DERIVED FROM THE REGISTERED PRODUCT, NOT ASSEMBLED.** This
    built `<proc_date>/jid<N>/<name>` from a `jids` list, and every part of
    that was stale: `Jobs` holds no rows so the list was empty, and the
    product bucket has no such prefix — everything since the submission
    restructure is attempt-scoped. The unit now declares `product_inputs`,
    each carrying the difference image's own S3 URI as registration recorded
    it, and the catalogue is resolved as that object's SIBLING.

    Deriving rather than assembling is what makes this robust to the live
    key-grammar split: newer keys are the zero-padded C-core form
    (`.../000020/07/attempt-0000006765/`) and older ones are not
    (`.../84/8/attempt-6761/`). Nothing here parses either shape — it replaces
    the last path segment and keeps the rest.
    """
    psfcat = (context.science or {}).get("psfcat_diffimage") or {}
    positive = psfcat.get("output_sfft_psfcat_filename")
    if not positive:
        raise InputError(
            "release content does not name output_sfft_psfcat_filename in "
            "[psfcat_diffimage]; the catalog load reads its inputs by that "
            "name and has no default for a science filename")
    # ALL FOUR FILES, because the loader joins each catalogue to its FINDER
    # sibling on `id` — the legacy loader's inner join, which is the science
    # contract, not an implementation choice. Downloading only the two
    # catalogues left `read_psfcat_rows` with no finder to join against, and a
    # missing finder is a documented SKIP, so every source would have been
    # dropped with nothing but a per-file warning to say so.
    #
    # The names come from release content, and `_finder` sits BEFORE
    # `_negative` in them (`..._psfcat_finder_negative.txt`), which is why the
    # finder name is read from the section rather than composed from the
    # positive one.
    finder = psfcat.get("output_sfft_psfcat_finder_filename")
    if not finder:
        raise InputError(
            "release content does not name output_sfft_psfcat_finder_filename "
            "in [psfcat_diffimage]; the catalog load joins each catalogue to "
            "its finder sibling and cannot compose that name itself")
    negative = positive.replace(".txt", "_negative.txt")
    finder_negative = finder.replace(".txt", "_negative.txt")

    proc_date = _unit_field(context, "proc_date")
    sca = int(_unit_field(context, "sca"))

    downloaded = []
    for product in _product_inputs_for_unit(context):
        uri = product.get("difference_image_uri")
        if not uri:
            # A declared input with no URI is a gathering fault, not a
            # missing catalogue: the row it came from had `filename is not
            # null` in its own predicate.
            raise InputError(
                f"unit {proc_date}/{sca} declares a product input with no "
                f"difference-image URI: {product!r}")
        attempt_id = product.get("attempt_id")
        # The two CATALOGUES are what the loader iterates; the two FINDERS are
        # fetched beside them because the reader joins to them, and are not
        # themselves lists of sources to load. Only the catalogues go into
        # `downloaded`, so a finder can never be walked as if it were one.
        for name, is_catalogue in ((positive, True), (negative, True),
                                   (finder, False), (finder_negative, False)):
            bucket, key = _sibling_key(uri, name)
            target = context.scratch(f"attempt{attempt_id}_{name}")
            try:
                context.s3.download_file(bucket, key, target)
            except Exception as exc:  # noqa: BLE001 - triaged below
                # ONLY GENUINE ABSENCE IS A NORMAL OUTCOME (final
                # convergence round, 2026-08-09): this used to swallow
                # EVERY exception as "no catalogue", so an authorization,
                # credential, connectivity, or throttling failure produced
                # a successful zero-row load — and under the resubmission
                # gate that success permanently blocked re-gathering,
                # making an incomplete sources table silently
                # authoritative. A 404/NoSuchKey means the attempt
                # produced no catalogue and contributes nothing; anything
                # else is a real read failure and fails the attempt, which
                # frees the subject for retry.
                code = str(getattr(exc, "response", {}).get(
                    "Error", {}).get("Code", ""))
                if code in ("404", "NoSuchKey", "NotFound"):
                    context.logger.info("no catalogue at s3://%s/%s",
                                        bucket, key)
                    continue
                raise InputError(
                    f"could not read catalogue s3://{bucket}/{key}: "
                    f"{exc}") from exc
            if is_catalogue:
                # THE FILE→PRODUCT PAIRING IS THE PAYLOAD (mission mock,
                # live 2026-08-09): each catalogue's source rows carry ITS
                # difference image's identity (pid, expid, field, fid,
                # mjdobs), which varies per product within one (date, SCA)
                # unit. A flat path list lost the association and the CSV
                # wrote a unit-constant (absent) pid into a NOT NULL column.
                downloaded.append((target, product))

    context.produce("psf_catalogs", downloaded)
    context.record(psf_catalog_files=len(downloaded), sca=sca)
    context.logger.info("unit %s/%s: %d catalogue file(s)",
                        proc_date, sca, len(downloaded))


#: The tablespace the post-DB child tables are placed in where one exists.
#: Named once; the three DDL sites ask for it through `_place_in_data_tablespace`.
DATA_TABLESPACE = "pipeline_data_01"


def _place_in_data_tablespace(cursor, logger=None):
    """Set the data tablespace for this transaction, IF the server has one.

    **A DEPLOY-ONLY DEFECT, found live** (attempt 6771, 2026-08-09). All three
    DDL sites ran `SET LOCAL default_tablespace = pipeline_data_01`
    unconditionally, and rapid-db has only `pg_default` and `pg_global`: the
    named tablespaces are a production-storage arrangement this database does
    not carry. PostgreSQL refuses the SET itself — `InvalidParameterValue`, not
    a warning — so `create_sources_table` died on its first statement and the
    whole catalog-load chain was unreachable. No unit test could see it: the
    statement is valid SQL and only the SERVER knows whether the tablespace
    exists.

    Placement is an optimization, not a correctness property — the child tables
    are identical wherever they live — so its absence must not fail the load.
    Where the tablespace exists the placement is applied exactly as before;
    where it does not, the tables land in `pg_default` and the fact is logged
    rather than assumed. That is the same "absent, not sentinel" rule the
    gathering layer states: a fact that cannot be resolved is left absent, and
    nothing invents a substitute.
    """
    cursor.execute("select 1 from pg_tablespace where spcname = %s;",
                   (DATA_TABLESPACE,))
    if cursor.fetchone() is None:
        if logger is not None:
            logger.info(
                "tablespace %s does not exist on this server; the child "
                "tables are created in the default tablespace. Placement is "
                "an optimization and its absence is not a load failure.",
                DATA_TABLESPACE)
        return False
    cursor.execute("SET LOCAL default_tablespace = " + DATA_TABLESPACE)
    return True


def _product_inputs_for_unit(context):
    """The registered products whose catalogues this unit loads.

    Declared in the manifest by the gatherer; absent means the submission did
    not enumerate them, which is a submission fault rather than something to
    rediscover here.

    An EMPTY list is a legitimate answer and not a fault: a catalog-load
    unit with no product inputs loads nothing and records that through its
    effect counts — the empty-product-set disposition. That is why
    `product_inputs` is the one optional member of `CatalogLoadPayload` and
    why it defaults to empty rather than to None.
    """
    payload = getattr(context.unit, "payload", None)
    if payload is None:
        raise InputError(
            "this unit carries no typed payload; manifest schema version 4 "
            "refuses the pre-rule-11 `fields` shape rather than translating "
            "it")
    return list(getattr(payload, "product_inputs", ()) or ())


def _sibling_key(uri, name):
    """`(bucket, key)` for `name` beside the object `uri` names.

    The URI is `s3://<bucket>/<prefix>/<object>`; the catalogue is
    `<prefix>/<name>`. Written as a last-segment replacement rather than as a
    parse of the prefix's structure, because the structure differs between
    the padded and unpadded key grammars and neither is this function's
    business.
    """
    if not str(uri).startswith("s3://"):
        raise InputError(
            f"declared product input is not an s3 URI and no catalogue can "
            f"be resolved beside it: {uri!r}")
    bucket, _, key = str(uri)[len("s3://"):].partition("/")
    if not bucket or "/" not in key:
        raise InputError(
            f"declared product input {uri!r} names no object under a prefix; "
            f"the catalogue is resolved as that object's sibling")
    prefix = key.rsplit("/", 1)[0]
    return bucket, f"{prefix}/{name}"


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
        # than either a failure or a silent success. Still an effect-class
        # unit, so it still confirms: the table this unit owns must exist
        # and be queryable, even though the row count it confirms is zero.
        outcome = _verify_effect(
            conn, context, f"catalog load {table} (empty product set)",
            sql.SQL("SELECT count(*) FROM {table}").format(
                table=sql.Identifier(table)),
            (), 0)
        context.produce("effect_outcome", outcome)
        context.record_effect(rows_written=0, load_rate_rows_per_second=0.0)
        context.logger.info("no catalogues for %s; nothing loaded", table)
        return

    csv_path = context.scratch(f"{table}.csv")
    rows = _write_sources_csv(context, catalogs, csv_path)

    with transaction(conn) as cursor:
        before = _table_count(cursor, table)
        result = catalog_db.load_through_staging(
            cursor, csv_path, table, "sources", SOURCES_COLUMNS)

    outcome = _verify_effect(
        conn, context, f"catalog load {table}",
        sql.SQL("SELECT count(*) FROM {table}").format(
            table=sql.Identifier(table)),
        (), before + result["rows_written"])
    context.produce("effect_outcome", outcome)

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

    The per-source values the database columns need beyond what the
    catalogue carries — the difference-image identity (pid), exposure,
    field, filter, and MJD — are PER PRODUCT, not per unit (mission mock,
    live 2026-08-09): a (date, SCA) unit loads catalogues from many
    registered products, so `catalogs` is a list of `(path, product)`
    pairs and each file's rows carry its own product's identity. A
    unit-constant fact here wrote NULL pid into a NOT NULL column for
    every source of every production unit.
    """
    # The SCA is the UNIT's, as this docstring has always said: neither the
    # psfcat nor its finder carries an `sca` column, so reading one off the
    # row wrote NULL into a NOT-NULL-in-spirit identity column. Taken from the
    # declared unit field, which is where it actually lives.
    sca = int(_unit_field(context, "sca"))
    written = 0

    with open(csv_path, "w", newline="") as handle:
        writer = csv.writer(handle)
        for path, product in catalogs:
            positive = "_negative" not in os.path.basename(path)
            for row in util.read_psfcat_rows(path):
                writer.writerow(_copy_nulls(
                    _sources_row(row, product, positive, sca)))
                written += 1

    context.logger.info("wrote %d source row(s) to %s", written, csv_path)
    return written


def _copy_nulls(values):
    """Render None as COPY's NULL marker rather than as an empty field.

    **Found live by attempt 6774**, at the very end of a 591-second load: the
    CSV built cleanly, all 282 files parsed, and `COPY` refused the first row
    carrying an absent value with `InvalidTextRepresentation: invalid input
    syntax for type integer: ""`.

    `csv.writer` renders None as an EMPTY FIELD, and `copy_from` is told
    `null="\\N"` — so an empty field is the literal empty string, which is a
    valid text value and an invalid integer. The two conventions disagreed
    about exactly one thing, and only a row with a genuinely absent value
    could show it.

    This is the "absent, not sentinel" rule meeting a format that has its own
    spelling for absent. Nothing is defaulted here; the None is preserved and
    written the way the reader on the other side reads it.
    """
    return ["\\N" if value is None else value for value in values]


def _sources_row(row, product, positive: bool, sca):
    """One catalogue row as the sources column tuple, in COPY order.

    **THE KEYS ARE THE CATALOGUE'S, NOT THE TABLE'S.** Two of them were the
    destination column names — `npixfit` and `npix` — and the catalogue files
    carry neither: the psfcat has `n_pixels_fit` and the finder has
    `n_pixels`. `dict.get` returns None for a name that is not there, so both
    columns would have loaded as NULL for every source ever loaded, silently,
    with no error anywhere. Verified against the real product headers
    (`sfftdiffimage_masked_psfcat.txt` and its `_finder` sibling) rather than
    inferred from the table.

    `product` is the catalogue file's OWN declared product input — the
    gatherer's per-product mapping (pid, expid, field, fid, mjdobs) — never
    a unit-constant: one (date, SCA) unit spans many products.
    """
    return [
        row.get("id"), row.get("ra"), row.get("dec"),
        row.get("x_fit"), row.get("y_fit"), row.get("flux_fit"),
        row.get("x_err"), row.get("y_err"), row.get("flux_err"),
        row.get("n_pixels_fit"), row.get("qfit"), row.get("cfit"),
        row.get("reduced_chi2"), row.get("flags"), row.get("sharpness"),
        row.get("roundness1"), row.get("roundness2"), row.get("n_pixels"),
        row.get("peak"), product.get("pid"), "true" if positive else "false",
        product.get("field"), row.get("hp6"), row.get("hp9"),
        product.get("expid"), product.get("fid"), sca,
        product.get("mjdobs"),
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
        # SET-SCOPED CLONE NAMING (conformance rule 19, brief F1). The live
        # prompt set keeps today's names exactly — `astroobjects_4641773` —
        # so adopting the set model renames nothing, moves no data and needs
        # no backfill. A non-live set materializes its own family under its
        # own prefix, and REPROCESSING ISOLATION IS THEN STRUCTURAL: a
        # reprocessing set cannot mutate the live tables because it never
        # names them. There is no rule to enforce and no grant to withhold —
        # the isolation is a property of the names this computes. The single
        # place that computation lives is `association_sets.table_name`, whose
        # SQL twin is `derived.association_table_name`.
        association_set, set_kind, _lane = _association_scope(cursor, context)
        names = {
            prototype: association_sets.table_name(
                prototype, association_set, field, set_kind)
            for prototype in ("astroobjects", "merges")}

        _place_in_data_tablespace(cursor, context.logger)
        for prototype, name in names.items():
            catalog_db.create_child_table(cursor, name, prototype)

    context.produce("astroobjects_table", names["astroobjects"])
    context.produce("merges_table", names["merges"])
    context.record(field=field, association_set=association_set)


def _association_scope(cursor, context):
    """This unit's `(association_set, kind, lane)`.

    The set comes from the unit when it declares one and from the well-known
    live row otherwise — `pipeline.association.sets.live_association_set`, the
    single lookup the brief allows. Nothing here spells the live set's value,
    which is what keeps "day one there is exactly one set" from becoming "day
    one the live set is hard-coded in the stage".

    One lane per set initially (§2.5), so `lane` is the module's named
    default rather than a literal 0 at this call site.
    """
    declared = getattr(getattr(context.unit, "payload", None),
                       "association_set", None)
    if declared is None:
        association_set = association_sets.live_association_set(
            _ConnLike(cursor))
    else:
        association_set = int(declared)
    kind = association_sets.set_kind(_ConnLike(cursor), association_set)
    return association_set, kind, association_sets.DEFAULT_LANE


class _ConnLike:
    """Adapt an open cursor to the `conn.cursor()` shape the set helpers want.

    `pipeline.association.sets` takes a CONNECTION because its other callers
    (the operator surface, the contract tests) hold one and are not inside a
    transaction. This stage IS inside one, and opening a second cursor on the
    same connection would be harmless but pointless — worse, taking a fresh
    connection here would read the set registry OUTSIDE the transaction whose
    atomicity is the whole point. So the cursor already in hand is lent to
    those helpers instead, which keeps every read in this function inside the
    acceptance transaction's snapshot.
    """

    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return contextlib.nullcontext(self._cursor)


def _advance_association_watermark(cursor, context, association_set, lane,
                                   position, proc_date, field):
    """CAS the lane's watermark to this unit. Returns whether it moved.

    `position` is the POST-LOCK re-read, and comparing against it is what
    tells this unit's turn from a stale one. Three outcomes, all normal:

      * ahead of the frontier and the CAS moves it — the ordinary acceptance;
      * at or behind the frontier — a STALE RETRY landing late (F3). The CAS
        refuses, the watermark does not regress, and the associations this
        unit just wrote are not duplicates because `merges_aid_sid_unique`
        (migration 027, reaching every clone through `INCLUDING INDEXES` in
        `create_field_tables`) makes the merge rows unique on `(aid, sid)`
        and `radec_index` makes the object identity a deterministic function
        of position rather than an assignment. To be exact about which
        mechanism guarantees what, since the brief asks: the UNIQUE INDEX is
        what refuses a second identical merge row, and `radec_index` is what
        makes the second run compute the SAME `aid` for the same source so
        that the index has an identical row to refuse. Neither alone would
        do it — a deterministic aid with no unique index would duplicate
        happily, and a unique index over a nondeterministic aid would admit
        a second row under a different identity;
      * ahead of the frontier but a concurrent duplicate advanced it first —
        the lease serialized them, this one re-read too early to see it, and
        the CAS refuses. Converges on the same state as the first.

    A refusal is logged and reported, never raised. Raising would turn a
    correct convergence into a failed attempt and a retry loop.
    """
    ahead = association_watermark.is_ahead_of(position, proc_date, field)
    if not ahead:
        context.logger.info(
            "field %d of %s is at or behind the association watermark %r; "
            "not advancing (stale retry — the associations converge through "
            "merges_aid_sid_unique and radec_index)",
            field, proc_date, position)
        return False

    advanced = association_watermark.advance(
        cursor, association_set, lane, proc_date, field)
    if not advanced:
        context.logger.info(
            "field %d of %s lost the watermark CAS for set %d lane %d; a "
            "concurrent attempt at the same unit advanced it first",
            field, proc_date, association_set, lane)
    return advanced


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

    # DEFENSE IN DEPTH, NOT THE PRIMARY GUARD: the payload entrypoint already
    # preflighted migration 049 for this route before this unit ran
    # (`pipeline/entrypoints/job.py:_database`, `schema_contract.
    # ROUTE_MIGRATIONS["crossmatch"]`). Checked here, before the acceptance
    # transaction opens and the lane lease is taken, so a schema that
    # regressed between preflight and this unit running fails on one cheap
    # SELECT rather than deep inside the transaction, lease held, on the
    # first UndefinedTable from `read_watermark` or `advance`.
    with conn.cursor() as probe_cursor:
        if not association_watermark.schema_present(probe_cursor):
            raise DBError(
                "migration 049 (association_watermarks) is not applied on "
                "this database; the crossmatch route's schema preflight "
                "should have caught this at startup")

    with transaction(conn) as cursor:
        # THE ACCEPTANCE TRANSACTION (conformance rule 19, brief F3). The
        # associations this unit writes and the watermark saying they are the
        # set's frontier commit together or not at all — that atomicity is the
        # rule's own words, "advanced in the same transaction as the accepted
        # associations", and it is what makes a crashed unit leave the
        # frontier where it was rather than ahead of rows that never landed.
        #
        # The three steps mirror the registrar's discipline
        # (`pipeline/registration/consumer.py:104-171`), in its order:
        #
        #   1. the lane lease as the FIRST statement of the transaction,
        #   2. a post-lock RE-READ of the watermark, because the claim that
        #      brought this unit here was made by an unlocked read in a
        #      gathering pass that may be minutes old,
        #   3. the CAS-guarded advance, at the end, with the rows.
        association_set, set_kind, lane = _association_scope(cursor, context)
        association_watermark.acquire_lane_lease(cursor, association_set, lane)
        position = association_watermark.read_watermark(
            cursor, association_set, lane)

        objects_before = _table_count(cursor, astroobjects)
        merges_before = _table_count(cursor, merges)

        matched, new_objects = _crossmatch_field(
            cursor, context, field, proc_date, float(radius),
            astroobjects, objects_csv, merges_csv)

        objects_result = catalog_db.load_through_staging(
            cursor, objects_csv, astroobjects, "astroobjects",
            ASTROOBJECTS_COLUMNS)
        merges_result = catalog_db.load_through_staging(
            cursor, merges_csv, merges, "merges", MERGES_COLUMNS)

        advanced = _advance_association_watermark(
            cursor, context, association_set, lane, position, proc_date, field)

    objects_outcome = _verify_effect(
        conn, context, f"crossmatch {astroobjects}",
        sql.SQL("SELECT count(*) FROM {table}").format(
            table=sql.Identifier(astroobjects)),
        (), objects_before + objects_result["rows_written"])
    merges_outcome = _verify_effect(
        conn, context, f"crossmatch {merges}",
        sql.SQL("SELECT count(*) FROM {table}").format(
            table=sql.Identifier(merges)),
        (), merges_before + merges_result["rows_written"])
    # BOTH TABLES MUST CONFIRM. This unit's effect is the pair together —
    # objects and their merge rows land in the same transaction — so a
    # verification that only checked one would call the unit confirmed
    # while silently trusting the other's write. `unconfirmed` on either
    # is `unconfirmed` for the unit.
    outcome = (EFFECT_OUTCOME_CONFIRMED
              if (objects_outcome == EFFECT_OUTCOME_CONFIRMED
                  and merges_outcome == EFFECT_OUTCOME_CONFIRMED)
              else EFFECT_OUTCOME_UNCONFIRMED)
    context.produce("effect_outcome", outcome)

    context.record_effect(
        rows_written=objects_result["rows_written"] + merges_result["rows_written"],
        sources_matched=matched,
        astroobjects_written=objects_result["rows_written"],
        merges_written=merges_result["rows_written"],
        new_astroobjects=new_objects,
        # Whether this unit moved the set's frontier. False is a normal
        # outcome — a stale retry, or a duplicate attempt whose twin advanced
        # it first — and recording it is what makes those cases visible in the
        # effect counts rather than indistinguishable from an advance.
        watermark_advanced=advanced,
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
            # CANONICAL DETECTION ORDER (conformance rule 19, brief F3).
            # Rule 19's `(observation_time, detection_id)` at the DETECTION
            # grain is `(mjdobs, sid)`: `sources.mjdobs` is the exposure's MJD
            # (007-sources-family.sql:101, indexed by `sources_mjdobs_idx`) and
            # `sid` is the monotone insert identity, defaulted from the single
            # global `sources_sid_seq` that every child shares. Both halves are
            # real columns, so the mapping's `sid`-alone fallback does not
            # apply here.
            #
            # This is not a performance hint. Within a job the order decides
            # which source is seen FIRST, and `_crossmatch_field`'s `seen` set
            # makes first-seen the winner for a source matched more than once —
            # so an unordered scan makes the association output depend on
            # PostgreSQL's physical row order, which is to say on vacuum
            # timing. Ordering it makes the same inputs give the same
            # associations, which is what pins criterion 6's idempotency to
            # something stronger than luck.
            cursor.execute(
                sql.SQL(
                    "SELECT a.sid, b.aid FROM {sources} AS a, {objects} AS b "
                    "WHERE q3c_join(a.ra, a.dec, b.ra0, b.dec0, %s) "
                    "AND a.field = %s AND a.flags = 0 "
                    "ORDER BY a.mjdobs, a.sid").format(
                        sources=sql.Identifier(sources_table),
                        objects=sql.Identifier(astroobjects)),
                (radius, field))
            for sid, aid in cursor.fetchall():
                merges_writer.writerow([aid, sid])
                seen.add(sid)
                matched_total += 1

            # The same canonical order, for the same reason: this loop MINTS
            # object identities for unmatched sources, and two sources close
            # enough to hash to one `aid` would otherwise be resolved by scan
            # order. `radec_index` is deterministic in position, so ordering
            # this makes the whole assignment deterministic in the input rows.
            cursor.execute(
                sql.SQL(
                    "SELECT sid, ra, dec, fluxfit FROM {sources} "
                    "WHERE field = %s AND flags = 0 "
                    "ORDER BY mjdobs, sid").format(
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

    Declared by the submitter in the manifest, on `CrossmatchPayload.
    source_tables` (`submission.payloads`). Falls back to nothing rather
    than probing the catalog: a unit that names no source tables loaded no
    sources, and the honest outcome is an effect count of zero.

    **STILL A STRUCTURAL NO-OP AS OF THIS WAVE, BUT NOW A NAMED ONE.**
    `source_tables` exists on the typed payload (added this wave,
    optional) so this function has something declared to read, but NO
    GATHERER POPULATES IT YET — `gather_crossmatch_units`
    (`submission/gathering.py`) still constructs a `CrossmatchPayload` with
    only `target_tables`. Until that lands (integration request filed
    against `submission/gathering.py`, this wave's ledger), this returns
    `[]` and `crossmatch_sources` iterates over nothing, exactly as it has
    since the pre-typed-payload representation first surfaced the gap. What
    changed is the shape of the gap: an open dict used to answer `.get()`
    for a key nobody set with a silent None; a closed payload now has a
    declared, typed, always-present-but-still-empty tuple, which is what
    let this function stop reading a key the payload type never declared.
    """
    payload = getattr(context.unit, "payload", None)
    if payload is None:
        raise InputError(
            "this unit carries no typed payload; manifest schema version 4 "
            "refuses the pre-rule-11 `fields` shape rather than translating "
            "it")
    return list(getattr(payload, "source_tables", ()) or ())


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
        _place_in_data_tablespace(cursor, context.logger)
        catalog_db.create_child_table(cursor, target, "astroobjectsmeta")

        # Recomputed wholesale inside the transaction: the delete and the
        # repopulate commit together, so a failure leaves the previous
        # statistics in place rather than an empty table.
        cursor.execute(
            sql.SQL("DELETE FROM {target}").format(
                target=sql.Identifier(target)))
        removed = cursor.rowcount or 0

        # THE LIVE PROTOTYPE'S COLUMNS (mission mock, live 2026-08-09,
        # defect #9): this INSERT named (aid, nobs, mjdobs_first,
        # mjdobs_last, flux_mean, flux_stddev) — a shape no migration ever
        # created — so every statistics attempt failed on UndefinedColumn,
        # and under retry-by-regather that was an unbounded loop (~600
        # children before the hold). astroobjectsmeta's real columns
        # (007-sources-family) are the ones the alert provider reads:
        # position/flux means and stdevs plus nsources.
        cursor.execute(
            sql.SQL(
                "INSERT INTO {target} (aid, meanra, stdevra, meandec, "
                "stdevdec, meanflux, stdevflux, nsources) "
                "SELECT m.aid, avg(s.ra), coalesce(stddev_pop(s.ra), 0), "
                "       avg(s.dec), coalesce(stddev_pop(s.dec), 0), "
                "       avg(s.fluxfit), coalesce(stddev_pop(s.fluxfit), 0), "
                "       count(*) "
                "FROM {merges} AS m JOIN sources AS s ON s.sid = m.sid "
                "GROUP BY m.aid").format(
                    target=sql.Identifier(target),
                    merges=sql.Identifier(f"merges_{field}")))
        written = cursor.rowcount or 0

    # The table is wholesale-rebuilt (DELETE then repopulate in the same
    # transaction), so the post-commit expectation is `written` alone, not
    # `before + written` — there is no "before" left once the delete lands.
    outcome = _verify_effect(
        conn, context, f"statistics {target}",
        sql.SQL("SELECT count(*) FROM {table}").format(
            table=sql.Identifier(target)),
        (), written)
    context.produce("effect_outcome", outcome)

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

    outcome = _verify_no_superseded_rows(
        conn, context, f"merge currency sweep {table}", table,
        join_column="sid", identity_table="diffimages",
        identity_column="pid")
    context.produce("effect_outcome", outcome)

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

    outcome = _verify_no_superseded_rows(
        conn, context, f"source currency sweep {table}", table,
        join_column="sid", identity_table="l2files",
        identity_column="rid")
    context.produce("effect_outcome", outcome)

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
        # A DEFECT REPORT, NOT AN EFFECT TO CONFIRM. This unit writes
        # nothing, so there is no `effect_outcome` to produce on the way
        # out here — `_execute`'s fail-closed guard only asks for one on a
        # SUCCESS close, and raising routes this attempt through the
        # `RuntimeErrorBase` catch in `pipeline.entrypoints.job._execute`
        # instead, which classifies it FAILURE+NONE without ever reaching
        # the effect-class branch.
        raise InputError(
            f"{table} holds {duplicates} duplicate (aid, sid) group(s). "
            f"Migration 027 put a unique index on the merges prototype and "
            f"the clone path carries it, so this is structurally impossible "
            f"on a table created by the converted path — the table predates "
            f"the constraint or was created by something that bypassed it. "
            f"Reported rather than deleted: the rows are the evidence.",
            duplicate_groups=duplicates, table=table)

    # THE CHECK IS ITS OWN CONFIRMATION: a should-find-nothing check's
    # effect is the fact that the invariant held, and re-running the same
    # count on a fresh cursor is what says that is still true right now,
    # not just at the moment the first cursor read it inside the (read-only)
    # transaction above. The identity columns are `count_duplicate_groups`'s
    # own — read from `CONFLICT_TARGETS` rather than restated, so the two
    # queries cannot drift on which columns define a duplicate.
    keys = sql.SQL(", ").join(
        sql.Identifier(c) for c in catalog_db.CONFLICT_TARGETS["merges"])
    query = sql.SQL(
        "SELECT count(*) FROM (SELECT {keys} FROM {child} GROUP BY {keys} "
        "HAVING count(*) > 1) AS duplicates").format(
            keys=keys, child=sql.Identifier(table))
    outcome = _verify_effect(
        conn, context, f"merge dedup check {table}", query, (), 0)
    context.produce("effect_outcome", outcome)

    context.logger.info("%s: no duplicate (aid, sid) groups, as expected",
                        table)


MERGE_DEDUP_SEQUENCE = (
    ("check_merge_duplicates", check_merge_duplicates),
)
