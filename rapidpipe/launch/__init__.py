"""Turning a run into Batch jobs, enforcing dependencies, reading results.

Holds the launcher: it turns a run into AWS Batch jobs, enforces stage
dependencies and retry limits, and reads completed attempts' results back.
This subpackage may import ``rapidpipe.products``, ``rapidpipe.db`` and
``rapidpipe.runs``, and may invoke stage entrypoints, but is never imported
by a stage module.
"""
