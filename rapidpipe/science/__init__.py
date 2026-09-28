# ported-from: none
"""Algorithms the stages call: referencing, differencing, cataloguing, alerts.

Holds pure functions and wrappers around the C tools that implement the
pipeline's science: reference coaddition, differencing (ZOGY, SFFT),
finalize, load, crossmatch, statistics and alert assembly. This
subpackage does not import ``rapidpipe.stages``, ``rapidpipe.launch`` or
``rapidpipe.cli``.
"""
