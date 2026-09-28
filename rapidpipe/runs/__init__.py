"""Runs, units of work, attempts, the three output states, promotion.

Holds run creation, attempt allocation and the execution record, the three
output states (scratch, candidate, current) and promotion between them,
and running and recording candidate checks (``checking.py``). This
subpackage composes ``rapidpipe.products``, ``rapidpipe.db`` and
``rapidpipe.checks``; it may import those, ``rapidpipe.science`` and the
leaf modules, but no stage module, ``rapidpipe.launch``,
``rapidpipe.selftest`` or ``rapidpipe.cli``
(``tests/unit/test_dependency_direction.py``).
"""
