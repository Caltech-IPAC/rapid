"""
RAPID alert production package.

    param_registry.py  THE schema registry: every param (= Avro schema field;
                   renamed to avoid clashing with the Roman sky field) of
                   every record, with its Avro type, doc, implementation
                   status, and how to read it from the normalized records.
                   Edit this file to change the schema or mark a param
                   implemented -- everything else derives from it.
                   "python -m alerts.param_registry" prints the
                   implemented/stub report.
    gen_schema.py  Writes the .avsc files from the registry
                   ("python -m alerts.gen_schema [--check]").
    providers.py   Normalized records (Source, ObjectRecord, ...) plus the
                   one AlertDataProvider that produces them, reading tabular
                   data from the DB and pixel/auxiliary products from the
                   pipeline job directory via per-source reader functions.
    produce.py     The runtime path: build records, assemble a packet,
                   serialize to Avro, publish to Kafka.
    cli.py         Command-line entry point
                   ("python -m alerts.cli <sid> [--kafka]").

Import from the submodules directly, e.g.:

    from alerts.produce import produce_alert
    from alerts.providers import AlertDataProvider

(This file deliberately imports nothing, so schema tooling like gen_schema
runs without the serialization dependencies and "python -m" entry points
don't double-import their own module.)
"""
