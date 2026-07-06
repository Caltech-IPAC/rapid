"""
RAPID alert production package.

    fields.py      THE schema registry: every field of every record, with its
                   Avro type, doc, implementation status, and how to read it
                   from the normalized records. Edit this file to change the
                   schema or mark a field implemented -- everything else
                   derives from it. "python -m rapid_alerts.fields" prints
                   the implemented/stub report.
    gen_schema.py  Writes the .avsc files from the registry
                   ("python -m rapid_alerts.gen_schema [--check]").
    providers.py   Normalized records (Detection, ObjectRecord, ...) plus the
                   data-access backends that produce them, so the storage
                   decision (database / file system / sqlite) stays swappable.
    produce.py     The runtime path: build records, assemble a packet,
                   serialize to Avro, publish to Kafka.
    cli.py         Command-line entry point
                   ("python -m rapid_alerts.cli <sid> [--kafka]").

Import from the submodules directly, e.g.:

    from rapid_alerts.produce import produce_alert
    from rapid_alerts.providers import DatabaseProvider

(This file deliberately imports nothing, so schema tooling like gen_schema
runs without the serialization dependencies and "python -m" entry points
don't double-import their own module.)
"""
