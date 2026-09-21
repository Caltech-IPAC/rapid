"""Persistence: typed repositories, the connection module, migrations.

Holds database access -- typed repositories over PostgreSQL/Q3C, the
connection module, and the migrations applier. This subpackage may import
``rapidpipe.products`` for identifiers and kinds, but provides persistence
without importing ``rapidpipe.runs`` or any stage module.
"""
