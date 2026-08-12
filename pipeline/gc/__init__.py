"""Two-pass, database-derived garbage collection (conformance rule 21).

Rule 21, verbatim: "Object deletion happens only through the two-pass
inventory anti-join with a safety horizon exceeding the retry, quarantine and
PITR windows, against a recorded plan."

The governing design is `rapid-clean-sheet-destination.md` §4.11, and this
package implements its seven steps in order:

  1. obtain an S3 inventory                      `inventory.py`
  2. anti-join it against registered artifacts,  `references.py`
     active manifests, quarantined results
     and live attempts
  3. apply an age horizon longer than retry,     `horizon.py`
     quarantine and PITR windows
  4. record a checksummed deletion plan          `plans.py`
  5. wait and recompute                          `plans.py`
  6. delete exact object version identifiers     `execute.py`
     in bounded batches
  7. retain the audit result                     DRAFT 052's triggers

**WHAT THIS PACKAGE HONESTLY DELIVERS, STATED HERE SO NO READER HAS TO INFER
IT.** With the deletable-class allowlist opt-in and effectively empty, this GC
computes plans, records them, and deletes little or nothing. That is the
correct and conforming outcome: rule 21 requires that deletion happen ONLY
through this mechanism, not that the mechanism reclaim anything in particular.

And the exclusivity claim is REPOSITORY-SCOPED, not system-wide. See
`exclusivity.py`, which states its own scope in its failure message.
"""
