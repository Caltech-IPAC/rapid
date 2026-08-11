"""AWS-side operator scripts.

This directory has always been a loose collection of one-shot scripts run
by hand (`python aws/terminate_batch_jobs.py --queue ... --dry-run`), with
no `__init__.py` because nothing ever imported it.

Brief G's G3 requires `terminate_batch_jobs` to be WRAPPED rather than
rewritten — `pipeline.operatorctl.batch` imports its `list_jobs` and calls
it — and a module that cannot be imported cannot be wrapped. Hence this
file: it makes the directory a package so the wrapping is a real import of
the real code, rather than a copy of that code under another name which
would drift from it at the first fix applied to only one of the two.

The scripts remain runnable exactly as before; a package `__init__.py`
does not change how `python aws/terminate_batch_jobs.py` behaves. Note
that these scripts are operator tooling rather than pipeline runtime: the
package is included in the distribution so `rapidctl` can reach it, but
nothing in the job payload imports it.
"""
