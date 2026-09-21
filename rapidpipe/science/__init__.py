"""Algorithms the stages call: differencing, coaddition, photometry.

Holds pure functions and wrappers around the C tools that implement the
pipeline's science: differencing (ZOGY, SFFT), coaddition, photometry. This
subpackage does not import ``rapidpipe.stages``, ``rapidpipe.launch`` or
``rapidpipe.cli``.
"""
