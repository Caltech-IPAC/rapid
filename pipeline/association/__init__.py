"""Association ordering: sets, lanes and the claim watermark (rule 19).

The durable half lives in DRAFT migration 049
(`migrations-draft/049-association-sets-and-watermarks.sql`); this package is
the application side of the same discipline:

* `sets` — the well-known-row lookup for the live association set, and the
  set-scoped clone-family naming that makes reprocessing isolation structural.
* `watermark` — the per-(set, lane) claim lease, the post-lock re-read, and
  the CAS-guarded advance the acceptance transaction commits with its rows.

Nothing here hard-codes the live set. That is the point: day one there is
exactly one set, and every path still keys on it.
"""
