"""Product identifiers, kinds, manifest types and the storage layout.

Holds the four unit-of-work kinds and identifier types (``ids.py``), the
completion-manifest dataclasses and their JSON read/write and validation
(``manifest.py``), the storage layout beneath a run, and the pure spatial
derivations registration needs (``spatial.py``). Above the leaf modules
(``exitcodes``, ``log``, ``revision``, ``seams``) it imports no other part
of ``rapidpipe`` (``tests/unit/test_dependency_direction.py``). Every
module but ``spatial.py`` imports only the standard library;
``spatial.py`` also imports numpy, healpy and the Roman tessellation in
``database.modules.utils`` (``roman_tessellation``,
``roman_tessellation_db.RomanTessellationClosedForm``), and is imported
only by name, so ``import rapidpipe.products`` loads none of them.
"""
