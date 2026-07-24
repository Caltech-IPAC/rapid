# test_pydantic — three prototypes for pydantic in the alert schema

Throwaway prototypes evaluating pydantic as (or alongside) the schema
source of truth, in the order they were built. None are wired into
`rapid_alerts`. Run the checks from the `alerts/` directory.

## Variant 1 — vanilla pydantic, models generate the schema

`v1_annotated.py` (self-contained; no check script — superseded by v2)

Pydantic models are the single source of truth and a custom walker
generates the `.avsc`. Everything uses public pydantic vocabulary:
`Annotated` metadata carries the Avro width (`LONG`/`FLOAT`...), status,
and provenance; `Field(validation_alias=...)` maps DB columns.

- Pro: fully idiomatic pydantic; best IDE/type-checker support.
- Con: noisy — the registry's at-a-glance layout (type, doc, status,
  source) is buried in `Annotated[...]`/`Field(...)` wrappers, and the
  width markers exist only to work around Python's single `int`/`float`.

## Variant 2 — registry-style DSL, models generate the schema

`v2_registry_style.py` (declarations) + `v2_rapid_pydantic.py` (machinery)
+ `v2_avro.py` (walker). Check: `python -m test_pydantic.v2_check`

Same "models are the truth" design as v1, but declared with
`param_registry.py`'s exact layout via a metaclass (`param()` assignments,
Avro type as a plain string, status/source/attr in registry positions).
The Avro string drives both the `.avsc` and the derived Python type.

- Pro: reads like the registry; single source of truth preserved;
  byte-identical `.avsc` output (subset verified).
- Con: ~180 lines of metaclass magic every maintainer must understand;
  static type checkers can't see the synthesized fields.

## Variant 3 — .avsc-first, models are checked, not generating

`v3_avsc_first.py` (models + consistency checker).
Check: `python -m test_pydantic.v3_check`

The committed `.avsc` files are the wire truth (LSST's arrangement) and
the models are ordinary pydantic verified *compatible* with them by
`schema_consistency_problems()` — the drift gate, same philosophy as
`gen_schema --check` with the arrow reversed. Widths belong to fastavro,
so no markers; docs live only in the `.avsc`; the alias doubles as
provenance; stubs are unaliased null-default fields. Covers the FULL
current schema (all 6 records, 194 fields) and is checked against the
real committed files, real `providers.Source`/`ObjectRecord`, and real
`produce.build_*` output (byte-identical alert).

- Pro: zero custom machinery; most idiomatic; per-packet
  `ValidationError`s naming the field; full IDE support.
- Con: relaxes single-source-of-truth — a schema change edits both the
  `.avsc` and the model, with the consistency check catching drift
  rather than preventing it; `.avsc` is JSON, so the registry's inline
  TODOs/comments need a new home.

## Shared findings (all variants, demonstrated in the check scripts)

- Loud failures: missing/None non-nullables and raising getters are
  `ValidationError`s naming the field.
- The one silent failure mode: a typo'd alias on a *nullable* field
  validates to None — `source_check()` (a port of
  `produce._validate_registry`) must run at import to catch it.
- Known edge: a provider property that raises `AttributeError` looks
  "missing" to pydantic instead of raising; keep provider properties
  trivial.
- `isNegative` (alias + transform) is safe only while
  `populate_by_name` stays False; a model rebuilt from its own
  `model_dump()` fails loudly instead of double-inverting.
- Overhead: ~1.75 µs vs ~0.86 µs per record to build — negligible
  against DB/fastavro/Kafka.

## LSST comparison (verified against Rubin docs, 2026-07-23)

Rubin's chain is: `sdm_schemas/yml/apdb.yaml` (declarative source) ->
`alert_packet`'s `updateSchema.py` (generator) -> committed versioned
`.avsc` -> plain dicts + fastavro at runtime. Pydantic appears only
inside Felis, validating the YAML schema *document* -- there are no
pydantic models of alert packets anywhere at Rubin. Structurally that is
the CURRENT rapid_alerts design (`param_registry.py` -> `gen_schema.py`
-> `.avsc` -> `build_record` + fastavro), not any variant here: variant 3
borrows LSST's "committed .avsc is wire truth" property, but true LSST
parity is what rapid_alerts already does. The Felis pattern would only
transfer if the registry ever moved to a language-neutral file (e.g.
YAML), where a pydantic model validating that file would be exactly
LSST's use of it.

## Recommendation (as of 2026-07-23)

The existing registry remains a strong design; nothing here *demands*
pydantic. If pydantic is adopted, variant 3 is the version to adopt:
it changes the question from "is pydantic worth a metaclass and a
walker?" to "is per-packet validation worth maintaining models alongside
the .avsc under a drift check?".
