"""The foundation's operations over ``database/schema.sql``.

Foundation plan v5 (2026-10-20), Part 1: a run is a recipe
(:func:`create_run`), a job is one execution under it with its inputs
recorded at launch (:func:`create_job`), the publisher loads one completed
attempt's declared outputs and tries the automatic binding
(:func:`publish`), and one operation changes official bindings in a
recorded, reversible group (:func:`change`, :func:`undo`). :func:`run_job`
runs one attempt of a job's stage in this process and publishes it.

Each writing function is one transaction on the connection it is given and
ends it: it commits, or rolls back and raises. :class:`NotFound` is a lookup
that found nothing; :class:`Refused` is a change group the official table
refused; :class:`PublishError` is an attempt whose
declared outputs could not be verified.
"""

from rapidpipe.foundation.bookkeeping import create_job, create_run, job_show, run_show
from rapidpipe.foundation.local import JobContext, run_job
from rapidpipe.foundation.official import NotFound, Refused, apply, change, undo
from rapidpipe.foundation.publish import PRODUCT_TABLES, PublishError, publish

__all__ = [
    "JobContext", "NotFound", "PRODUCT_TABLES", "PublishError", "Refused", "apply", "change", "create_job",
    "create_run", "job_show", "publish", "run_job", "run_show", "undo",
]
