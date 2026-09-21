"""Runs, units of work, attempts, the three output states, promotion.

Holds run creation, attempt allocation and the execution record, the three
output states (scratch, candidate, current) and promotion between them.
This subpackage composes ``rapidpipe.products`` and ``rapidpipe.db``; it may
import both, but no stage module, ``rapidpipe.launch`` or ``rapidpipe.cli``.
"""
