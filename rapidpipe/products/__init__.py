"""Product identifiers, kinds and the storage layout.

Holds the four unit-of-work kinds and identifier types (``ids.py``), the
storage layout beneath a run, the difference image's bits
(``diffimage.py``), the filter names (``filters.py``) and the pure spatial
derivations registration needs (``spatial.py``). Above the leaf modules
(``exitcodes``, ``log``, ``revision``) it imports no other part
of ``rapidpipe`` (``tests/unit/test_dependency_direction.py``). Every
module but ``spatial.py`` imports only the standard library;
``spatial.py`` also imports numpy, healpy and the Roman tessellation in
``database.modules.utils`` (``roman_tessellation``,
``roman_tessellation_db.RomanTessellationClosedForm``), and is imported
only by name, so ``import rapidpipe.products`` loads none of them.
"""
