.. _post_db_chain:

The post-DB science chain as bulk-queue job types
##################################################

The conversion record for road-map step 3: what the six post-DB job types
are, why each piece has the shape it has, and what is verified against
what evidence.

Design basis: ``decisions.md`` § Post-DB chain and schema hardening
co-design (the nine rulings), ``design/operations.md`` § Post-DB science
chain, ``design/database.md`` § Integrity and durability and § Access
model, ``design/catalog.md`` § Promotion, and ``design/code-standards.md``
§ Environment variables.

What was converted
******************

Four scripts the VPO exec'd as subprocesses at the tail of its main loop,
plus two prune siblings it never invoked at all:

.. list-table::
   :header-rows: 1
   :widths: 32 26 42

   * - Script
     - Job type
     - Unit
   * - ``loadPSFCatIntoDBSourcesTable.py``
     - ``catalog-load``
     - processing date × SCA
   * - ``crossMatchSources.py``
     - ``crossmatch``
     - processing date × field
   * - ``computeStatisticsForAstroObjects.py``
     - ``statistics``
     - field
   * - ``pruneNotBestMerges.py``
     - ``merge-currency-sweep``
     - field
   * - ``pruneNotBestSources.py``
     - ``source-currency-sweep``
     - field
   * - ``pruneRedundantMerges.py``
     - ``merge-dedup``
     - field

All four invoked ones shared one failure shape — ``except ToolError`` →
print → ``exit(64)`` — so any one of them killed the operator's whole
loop, and none left an attempt record behind. The two uninvoked siblings
convert alongside because they "are the only maintainers of integrity
properties the schema does not enforce" (ruling 3), and an unmaintained
invariant is a defect under the cross-cutting rules.

The work list moves to the submission layer
*******************************************

**ADOPTED:** "A job type never discovers its work by catalog
introspection at runtime; every unit is individually retryable and
individually reconcilable in attempt records."

Every one of the six discovered its own work at runtime: ``to_regclass``
probes across SCAs 1–18, ``select distinct field`` against tables the
previous step had just written, ``pg_tables like 'merges_%'``. Three
consequences, all of which the gathering move ends:

* a unit whose table was missing simply **vanished** from the work list,
  rather than being reported as work not done;
* the work list depended on what a previous run had already created, so
  it was not reconstructible after the fact;
* a catalog-load pass over 18 SCAs was **one process** whose failure lost
  the whole date.

``submission/gathering.py`` now answers those questions from ``Jobs`` —
the rows the science pipeline actually wrote — and each unit becomes its
own array child with its own attempt row.

The corpus-wide sweeps do still ask the catalog which per-field clones
exist, because "which fields have a table" is genuinely their work list
and no processing date names it. That query is **in the submission
layer**, which is what the ruling asks for: the submitter runs it once,
the manifest names each field as a declared unit, and each unit is then
individually retryable.

Database effects, not products
******************************

**ADOPTED:** "These job types produce database state, not stored
products: each declares an empty product set, its terminal record is a
pure disposition record that promotes nothing, and its effect — rows
written, rows removed — is recorded in the attempt record's own fields."

So ``published_products`` stays empty, ``_execute`` derives
``ProductDisposition.NONE``, and ``observability.registration.decide``
SKIPs ``none``. These attempts close successfully and never become
registration candidates — the same self-poisoning shape round-3 finding
#7 closed for post-process.

Three corrections, made once
****************************

The primitives live in ``pipeline/stages/catalog_db.py`` so each
correction is made once rather than six times.

**Clones carry the prototype's indexes.** ``LIKE ... INCLUDING DEFAULTS
INCLUDING CONSTRAINTS`` copies neither indexes nor the unique index
migration 027 added to ``merges (aid, sid)``. The old path then created a
hand-written index list that could not contain an index written after it,
so every per-field clone was born without the uniqueness the design
requires. ``INCLUDING INDEXES`` carries it, and keeps carrying whatever a
later migration adds. Migration 027 states the handoff explicitly: the
per-field constraints "land with the conversion's staging-plus-upsert
load path".

**Loads land through staging and an upsert.** ``COPY`` into an
unconstrained temp table, then ``INSERT ... ON CONFLICT DO NOTHING``.
Against a constrained target the old raw ``COPY`` could not converge on a
rerun — the first already-present row aborts it — so the
individually-retryable ruling would have been unusable in exactly the
case retries exist for. The load rate is measured and recorded, because
trading durability for load speed is "an argued-for regression requiring
measurements" and this is the measurement that argument would be made
against.

**No UNLOGGED anywhere.** All four unconditional ``SET UNLOGGED`` sites
are gone, including the crossmatch pair that ran on *every* pass outside
the creation guard. Unlogged tables lose their contents on crash recovery
and are not replicated; the migration baseline left the prototypes LOGGED
deliberately while the pipeline set every child unlogged at runtime, so
every table holding real data was unlogged anyway.

The dedup sweep demotes to a should-find-nothing check: it **counts and
reports** rather than deletes, because with prevention in place a
duplicate is a defect report about the constraint, and deleting the rows
would erase the evidence.

The two schema write halves
***************************

Both columns had landed in the migration stream with no writer:

* ``retry_policy_version`` (migration 025) is written at ``mark_started``
  — the migration's own CHECK forbids it while the row is ``submitted``,
  so the start transition is the first legal moment. Version 1 is
  park-until-change for every application-failure category.
* the ``abrupt_loss`` closure-record **checksum** (migration 022), whose
  comment said the write site would land separately. A cited key with no
  checksum is a pointer the catalog design tells a reader to distrust and
  gives it no way to verify.

Borrowed connections
********************

The entrypoint already opens exactly one connection per attempt on the
route matrix's lane; the stages write through it. The six scripts made 12
direct ``RAPIDDB()`` constructions between them, each opening a second
connection whose 33 mutating methods commit individually — so a stage
writing through its own handle could not be in one transaction with
anything, and a failure midway left a partial effect beside an attempt
record claiming failure.

Verification
************

Unit suite, ``rapid``::

    $ ./scripts/run-operational-tests.sh <python>
    1161 tests across 38 modules
    RESULT: PASS
    EXIT=0

Baseline before the conversion was 1082 tests across 37 modules. The 12
gathering tests that fail without ``RAPID_SW`` set fail identically on the
unmodified tip — the runner does not set it.

``rapid_systems``: ``validate.sh`` PASS.

Live evidence, 2026-08-09
*************************

Image ``46c73c0-20260809``
(``sha256:dde21a99…``), job definitions ``rapid-pipeline-science`` and
``rapid-pipeline-bulk`` at revision 29. Scan gate diffed by CVE id, both
digests ``inspector2`` coverage ACTIVE/SUCCESSFUL: zero added.

**Full-chain rehearsal**, all six gathering queries against rapid-db as
``rapid_orchestrator``, zero submissions: each enumerated 0 units against
an empty ``Jobs`` table, then 1 unit each for the three per-field job
types once a clone existed. Zero prompt-queue submissions throughout.

**The clone properties, against the real prototype** — not a test
double::

    merges_4641773_aid_sid_idx  unique=True
    RELPERSISTENCE=p

That is migration 027's uniqueness reaching a per-field child, and LOGGED
rather than unlogged.

**One live merge-dedup unit** through the bulk route, attempt 6770::

    rapid_outcome       = success
    product_disposition = none
    retry_policy_version = 1
    products            = None
    rows_written        = 0
    rows_removed        = 0
    duplicate_groups    = 0
    checked_table       = merges_4641773

The disposition record with its effect counts, and the
should-find-nothing check finding nothing — which is what the unique
index earns.

Three defects the live probes found
***********************************

None of these was reachable from the unit suite, which is the argument
for having run the probes at all.

**The field did not fit the column.** ``attempts.sca`` is ``smallint``
because an SCA is 1–18; the per-field units put a seven-digit field
identifier there for key uniqueness, so ``create_submitted`` raised
``NumericValueOutOfRange`` on the first real submission and no per-field
unit could ever have had a row created. The field moved to
``exposure_id`` (integer). The unit tests used single-digit synthetic
fields, which fit smallint and so proved nothing; they now use real field
magnitudes and assert both column domains.

**A missing target read as a crash.** A declared unit whose table does
not exist is an ordinary state — that field has never been crossmatched —
but the bare ``psycopg2.errors.UndefinedTable`` is not in the runtime
taxonomy, so the attempt closed ``internal_error``. ``require_table``
raises ``InputError`` instead, so it classifies ``input_missing``.
Verified live: attempt 6768 closed ``input_invalid`` where 6766 had
closed ``internal_error``.

**A fresh clone was unreadable.** ``LIKE ... INCLUDING INDEXES`` copies
structure, not privileges, and a clone is owned by whichever role created
it — so the merge-dedup unit died with ``permission denied for table
merges_4641773`` against a table that existed and was correctly indexed.
The old ``crossMatchSources.py`` issued a hand-written GRANT block after
each CREATE; the conversion replaced the CREATE and dropped the GRANTs
with it. ``grant_like_prototype`` now copies whatever the prototype
grants — read from the catalog rather than hardcoded, because the old
literal list (``rapidreadrole``, ``rapidporole``) had already drifted
from the live ``rapid_read`` / ``rapid_pipeline_write``.

Open and carried
****************

* **The difference-image product vocabulary** (register: OPEN) — the
  payload publishes ``zogy_diffimage`` / ``sfft_diffimage`` /
  ``naive_diffimage`` while the registration reader requires a product
  named literally ``difference_image``. Registration therefore refuses
  promotion on the live path. Pre-existing, expected, and **not fixed
  here**; ``diffimages`` holds zero rows in consequence, which is why the
  currency sweeps have nothing to sweep.
* **The deployed VPO service cannot submit.** ``submission_env`` requires
  ``RAPID_MANIFEST_BUCKET``, ``RAPID_IMAGE_DIGEST`` and
  ``RAPID_RELEASE_IDENTITY`` with ``os.environ[...]`` — no defaults — and
  the running service's unit sets none of the three (verified inside the
  live container). It has not failed yet only because gathering finds no
  work. Pre-existing from the restructure, outside this job's scope,
  recorded for the owner.
* **The grant fix is not in the pinned image.** It landed after the
  second and final permitted rebuild; its effect was proven live by
  applying ``grant_like_prototype`` to the existing clone. The next
  rebuild carries the code.
* ``job.py:409``'s legacy registration fallback stays
  recorded-as-proposed, not removed, per the task's scope.
