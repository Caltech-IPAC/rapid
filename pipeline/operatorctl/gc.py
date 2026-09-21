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


def s3_manifest_reader(client=None):
    """Read a manifest body from S3 and parse it.

    The production reader for `expand_manifest_bodies`. It RAISES on a
    manifest it cannot fetch or parse, which is the behaviour the plan-level
    refusal depends on — a reader that returned `{}` on failure would silently
    turn "unreadable" into "protects nothing".
    """
    import json as _json

    def read(uri):
        if not uri.startswith("s3://"):
            raise ValueError("not an S3 URI: %r" % (uri,))
        bucket, _, key = uri[len("s3://"):].partition("/")
        handle = client or __import__("boto3").client("s3")
        body = handle.get_object(Bucket=bucket, Key=key)["Body"].read()
        return _json.loads(body)
    return read


def still_referenced_check(execute, manifest_reader=None):
    """The final re-verification the executor runs INSIDE the fence.

    Layer 3 of the four layered mitigations. It re-reads the live reference
    set — not a snapshot taken at planning time — and answers "has this object
    become referenced since?". Products are uploaded BEFORE they are
    registered (`pipeline/stages/publishing.py:198`), so a registrar that
    committed between planning and execution is exactly the case this catches.

    Returns a callable, so the executor holds no database knowledge of its
    own.
    """
    def is_referenced(item):
        uri = "s3://%s/%s" % (item.bucket, item.object_key)
        references, _consulted, _absent = reference_sql.collect_references(
            execute, manifest_reader=manifest_reader)
        return uri in references or item.object_key in references
    return is_referenced


def references_run_check(execute, run_name, resolved_ref_ids,
                          in_flight_submissions, *, manifest_reader=None):
    """Build 142's caller-evidence envelope for ONE run's own deletion.

    **SCOPED TO ONE RUN, ADDITIVE BESIDE `still_referenced_check`.** That
    function answers the GENERAL collector's question — "is this object
    still referenced by anything, right now" — by re-reading the global
    reference set inside the fence. This function answers 142's DIFFERENT
    question — "do any of OTHER runs' in-flight submissions reference THIS
    run's objects" — which the general collector never asks and
    `still_referenced_check` is not shaped to answer: 142's SQL side
    computes reachability from the database directly for everything except
    the in-flight-manifest case (iii-b in the migration's own header),
    where the manifest lives in S3 and only the caller can read it. Neither
    `still_referenced_check` nor `collect_references`/
    `expand_manifest_bodies` is modified to add this — this is a second,
    parallel function reading `pipeline.gc.reference_sql
    .per_submission_evidence`, the new per-submission-attribution reader.

    `resolved_ref_ids` is the set of URIs (or reference-ids) that identify
    THIS run's own candidate objects — what a foreign manifest's resolved
    references are checked against to decide `references_run`.

    Returns `(evidence, unreadable)`:

      * `evidence` is 142's wire shape for `p_objects.evidence.submissions`:
        a list of `{"submission_id", "manifest_uri", "manifest_checksum",
        "references_run"}` dicts, one per in-flight foreign submission the
        caller named — including the unreadable ones, so the caller can
        decide whether to still send them (142 refuses on any `unreadable`
        entry regardless).
      * `unreadable` is the list of manifest URIs (or a synthesized
        description, for a submission with no manifest_uri at all) that
        could not be read — 142's `evidence.unreadable` array. Non-empty
        `unreadable` means the caller should surface a refusal BEFORE even
        calling `derived.delete_run`, so the operator sees a clean message
        rather than the SQL side's own RA021 text — `contract.py` renders
        this via the `ManifestUnreadable`-family exception the caller
        raises with it.

    `in_flight_submissions` — the rows naming which OTHER runs' submissions
    are in flight — is NOT queried here: that enumeration is 142's own SQL
    predicate (`submissions`/`attempts` joined on `run_key`), so the CALLER
    (`pipeline/operatorctl/main.py`'s `_cmd_run_delete`) queries it directly
    and passes the rows in, keeping this function ignorant of that query's
    exact shape the same way `per_submission_evidence` is.
    """
    from pipeline.gc import reference_sql

    reader = manifest_reader or s3_manifest_reader()
    per_submission = reference_sql.per_submission_evidence(
        execute, run_name, in_flight_submissions, reader)

    evidence = []
    unreadable = []
    for submission_id, entry in per_submission.items():
        if not entry["readable"]:
            unreadable.append(entry["manifest_uri"] or entry["error"])
            references_run = False
        else:
            references_run = bool(
                entry["resolved_refs"] & set(resolved_ref_ids))
        evidence.append({
            "submission_id": submission_id,
            "manifest_uri": entry["manifest_uri"],
            "manifest_checksum": entry["manifest_checksum"],
            "references_run": references_run,
        })
    return evidence, unreadable


def compute_plan(conn, execute, *, inventory_source, inventory_id,
                 inventory_taken_at, declared_buckets, declared_prefixes,
                 horizons, max_deletions, freshness_seconds, reason,
                 idempotency_key, actor, allowlist=None, dry_run=True,
                 class_of=None, first_seen_absent=None, now=None,
                 horizon_provenance=None, manifest_reader=None):
    """Compute (and, unless dry-run, record) one GC plan.

    Returns the rendered result dict either way. The DRY RUN DOES THE REAL
    WORK — real inventory, real reference queries, real anti-join — and simply
    does not write the plan.
    """
    # VALIDATED HERE, NOT JUST INSIDE `classify()`. This is the
    # `--allow-class` path: an operator-supplied allowlist is refused before
    # it is even used to compute anything, rather than only when `classify()`
    # happens to be reached — `validated_allowlist()` is the same guard
    # `classify()` applies, called at THIS read point too so the mechanical
    # refusal in `pipeline/gc/references.py` covers both consumption paths.
    allowlist = references.validated_allowlist(
        allowlist if allowlist is not None
        else references.DELETABLE_CLASS_ALLOWLIST)

    inventory = read_inventory(
        inventory_source, inventory_id=inventory_id,
        taken_at=inventory_taken_at, freshness_seconds=freshness_seconds,
        now=now, declared_buckets=declared_buckets,
        declared_prefixes=declared_prefixes)

    refs, consulted, absent = reference_sql.collect_references(
        execute, manifest_reader=manifest_reader)
    facts = reference_sql.attempt_facts(execute)
    owners = reference_sql.owners(execute)

    effective = horizon_module.effective_horizon(*horizons.values())
    # THE PROVENANCE IS THE OPERATOR'S WHEN THEY GIVE ONE. 052's CHECK
    # requires a horizon and a provenance together or neither, because a
    # horizon without a stated source is a guess wearing a number; the
    # computed description is the fallback so the pair is never half-filled.
    provenance = (horizon_provenance if effective is not None
                  and horizon_provenance else
                  (horizon_module.describe(horizons)
                   if effective is not None else None))

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
