"""``rapidctl`` GC subcommands, under G's full mutation contract.

Every action here is dry-run by default, takes a mandatory reason and an
idempotency key, and writes the audit ledger through DRAFT 047's keyed path —
rule 16 binding on H's new operator surface.

**THE PLAN IS RENDERED FROM THE REAL COMPUTED POPULATION, NEVER COMPOSED FROM
CLI ARGUMENTS.** `pipeline/operatorctl/contract.py`'s governing docstring
states why: "A plan the CLI writes from its own arguments is a plan about what
the CLI intends, and the operator would be reviewing the tool's understanding
rather than the database's." So the dry run computes the real anti-join
against the real inventory and reference set, and prints exactly what an apply
would act on, minus the writing.

**`record_external_action` RECORDS THE RUN-LEVEL ACT; THE PER-ITEM TRUTH LIVES
IN THE PLAN.** That function commits immediately
(`contract.py:107`), so one call cannot carry per-object intent, a truthful
post-delete outcome AND crash-safe recovery. The precedent's own defect is not
copied: `operatorctl/batch.py:80` records BEFORE the AWS action and its prose
claims a later update the code never performs. Here the per-item
intent/outcome protocol on `gc_plan_items` carries that truth, and the
external-action row records that a GC execution happened at all.
"""

from pipeline.gc import horizon as horizon_module
from pipeline.gc import references, reference_sql
from pipeline.gc.inventory import read_inventory
from pipeline.gc.plans import GCPlanRepository, candidate_checksum
from pipeline.operatorctl.contract import call_function


def compute_plan(conn, execute, *, inventory_source, inventory_id,
                 inventory_taken_at, declared_buckets, declared_prefixes,
                 horizons, max_deletions, freshness_seconds, reason,
                 idempotency_key, actor, allowlist=None, dry_run=True,
                 class_of=None, first_seen_absent=None, now=None):
    """Compute (and, unless dry-run, record) one GC plan.

    Returns the rendered result dict either way. The DRY RUN DOES THE REAL
    WORK — real inventory, real reference queries, real anti-join — and simply
    does not write the plan.
    """
    allowlist = (tuple(allowlist) if allowlist is not None
                 else references.DELETABLE_CLASS_ALLOWLIST)

    inventory = read_inventory(
        inventory_source, inventory_id=inventory_id,
        taken_at=inventory_taken_at, freshness_seconds=freshness_seconds,
        now=now, declared_buckets=declared_buckets,
        declared_prefixes=declared_prefixes)

    refs, consulted, absent = reference_sql.collect_references(execute)
    facts = reference_sql.attempt_facts(execute)
    owners = reference_sql.owners(execute)

    effective = horizon_module.effective_horizon(*horizons.values())
    provenance = horizon_module.describe(horizons)

    def elapsed(obj):
        first = (first_seen_absent(obj) if first_seen_absent else None)
        return horizon_module.elapsed_since(first, effective, now=now)

    candidates, retained = references.classify(
        inventory.objects, references=refs, attempt_facts=facts,
        owners=owners, allowlist=allowlist, class_of=class_of,
        horizon_elapsed=(elapsed if effective is not None else None),
        declared_buckets=declared_buckets,
        declared_prefixes=declared_prefixes)

    counts = references.counted(retained)
    result = {
        "action": "gc_compute_plan",
        "inventory_id": inventory.inventory_id,
        "inventory_objects": len(inventory.objects),
        "inventory_complete": inventory.complete,
        "reference_surfaces_consulted": consulted,
        "reference_surfaces_absent": absent,
        "candidates": len(candidates),
        "candidate_checksum": candidate_checksum(candidates),
        "retained_counts": counts,
        "retained_total": len(retained),
        "horizon_seconds": effective,
        "horizon_provenance": provenance,
        "allowlist": list(allowlist),
        "max_deletions": max_deletions,
        "dry_run": dry_run,
    }

    # THE HONEST HEADLINE, PRINTED RATHER THAN INFERRED. With the allowlist
    # empty this is the normal outcome, and an operator should not have to
    # work out from a zero why nothing will be deleted.
    if effective is None:
        result["refusal"] = (
            "no safety horizon is configured, so this plan deletes nothing. "
            "The horizon must exceed the pgBackRest PITR retention and every "
            "real retry/recovery hold; there is no default that permits "
            "deletion.")
    elif not allowlist:
        result["refusal"] = (
            "the deletable-class allowlist is empty, so this plan deletes "
            "nothing. A class joins it only when a ratified proposal names it "
            "together with the durable reference surface that makes its "
            "absence meaningful. This is a conforming outcome: rule 21 "
            "requires that deletion happen only through this mechanism, not "
            "that the mechanism reclaim anything.")

    if dry_run:
        result["rows_affected"] = 0
        return result

    repo = GCPlanRepository(conn)
    plan = repo.record_plan(
        candidates=candidates, retained_counts=counts, inventory=inventory,
        declared_buckets=declared_buckets,
        declared_prefixes=declared_prefixes, horizon_seconds=effective,
        horizon_provenance=provenance, max_deletions=max_deletions,
        allowlist=allowlist, reason=reason,
        idempotency_key=idempotency_key, computed_by=actor)
    result["plan_id"] = plan.plan_id
    result["rows_affected"] = len(candidates)
    return result


def record_execution(conn, idempotency_key, plan_id, reason, outcomes,
                     dry_run, policy_citation=None):
    """Record the run-level operator act in the audit ledger.

    Called AFTER execution with the real outcome counts, deliberately: the
    per-item truth is already durable in `gc_plan_items` (written intent-first
    and outcome-after, so a crash is recoverable), and this row records that
    an operator ran a GC execution and what it did overall. Recording before
    the action — `batch.py:80`'s shape — would put a claim in the ledger that
    nothing later corrects.
    """
    import json

    tally = {}
    for outcome in outcomes:
        tally[outcome.status] = tally.get(outcome.status, 0) + 1
    deleted = tally.get("deleted", 0)

    # THE ARGUMENT ORDER IS 047'S, NOT A GUESS: (key, action_class,
    # target_scope, reason, expected_state, dry_run, rows_affected, detail,
    # policy_citation, dispatcher). `gc_plan_execute` is an enumerated class
    # DRAFT 052 adds — 047 refuses anything outside its literal list, and
    # widening that list rather than the column is what keeps the refusal
    # meaningful for everything else.
    #
    # `rows_affected` is the DELETED count, not the item count: the ledger
    # records what changed in the world, and an item resolved as
    # `already-absent` or `skipped-fenced` changed nothing.
    return call_function(
        conn,
        "SELECT derived.record_external_action(%s, %s, %s, %s, %s, %s, %s,"
        "                                      %s, %s)",
        (idempotency_key, "gc_plan_execute", "gc_plan:%s" % plan_id, reason,
         None, dry_run, deleted, json.dumps(tally), policy_citation))
