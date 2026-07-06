"""
RAPID alert production package.

The pieces fit together like this:

    fields.py      THE schema registry: every field of every record, with its
                   Avro type, doc, implementation status, and getter.
                   Edit this file to change the schema or mark a field
                   implemented -- everything below derives from it.
    gen_schema.py  Writes the .avsc files from the registry
                   (replaces generate_schema.sh).
    report.py      Prints the implemented / stub status of every field.
    records.py     Normalized in-memory records (Detection, ObjectRecord, ...)
                   that providers produce and builders consume.
    providers/     Data-access backends. assemble.py only ever talks to the
                   AlertDataProvider interface, so the storage decision
                   (database / file system / sqlite) stays swappable.
    build.py       Registry-driven record builders.
    assemble.py    Puts one alert packet together from provider data.
    serialize.py   Avro serialization and Kafka publishing.
    cli.py         Command-line entry point.
"""

from .assemble import assemble_alert
from .serialize import load_schema, serialize_alert, publish_alert, produce_alert
from .providers.base import AlertDataProvider
from .providers.database import DatabaseProvider

__all__ = [
    "assemble_alert",
    "load_schema",
    "serialize_alert",
    "publish_alert",
    "produce_alert",
    "AlertDataProvider",
    "DatabaseProvider",
]
