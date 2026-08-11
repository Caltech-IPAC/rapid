"""``rapidctl`` — the operator surface for the audited mutation API.

Conformance rule 16 requires that routine operation go through a
constrained procedure surface rather than handwritten SQL. Migrations
030-032 built the database half of that surface — an append-only ledger
(``derived.mutation_audit``), SECURITY DEFINER mutation functions with
mandatory reason and dry-run-default, the ``rapid_operator`` human tier
and the ``rapid_break_glass`` assumed role. What did not exist was any
way to reach it except by typing SQL at a psql prompt, which is exactly
the practice the rule prohibits: the functions constrain what a mutation
records, but a human composing ``SELECT derived.retry_parked_attempts(...)``
by hand is one typo away from the wrong run_id and has no plan to review
before committing to it.

This package is that missing half. It is deliberately thin — every
subcommand's real work happens inside a database function, and nothing
here composes state-changing SQL. What the CLI adds is the operator
ergonomics the contract implies but SQL cannot enforce: the dry-run plan
printed before the apply, ``--apply`` as an explicit act, a reason that
cannot be omitted, and an idempotency key that makes a re-run after an
ambiguous failure safe rather than a second mutation.

Layout follows the rest of ``pipeline/``: a thin ``main`` that owns
process exit, a ``session`` module that owns connection and role
assumption, and one module per action family.
"""
