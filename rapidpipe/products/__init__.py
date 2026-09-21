"""Product identifiers, kinds, manifest types and the storage layout.

Holds the four unit-of-work kinds and identifier types (``ids.py``), the
completion-manifest dataclasses and their JSON read/write and validation
(``manifest.py``), and the storage layout beneath a run. This subpackage may
import only the standard library: it defines identifiers and manifest types
without importing ``rapidpipe.runs``, ``rapidpipe.db`` or any stage module.
"""
