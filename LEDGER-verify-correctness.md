# Adversarial verify: correctness — commits 0c07351..c86bba7

All six attacks investigated against the actual diff (`git diff 3792447..HEAD`),
not the worker's self-report. No defects found; all six HOLD.

## 1. FOUND branch — HOLDS

Both entry routes funnel through `_reconcile_unresolved` unconditionally, and
`_submission_classification` is called first in that method, before any state
inspection (`service.py:1643`). Route (a): `scheduler_job_id IS NULL`
partition at `service.py:479` calls `_reconcile_unresolved(row)` directly.
Route (b): `_reconcile_attempt` (`service.py:747-748`) redirects to the same
method when `observation is None`, on rows that DO carry a `scheduler_job_id`
— `_reconcile_unresolved` never branches on that column, so it cannot
special-case route (b) into skipping the check. `classification == FOUND` at
`service.py:1644` returns `"waiting"` unconditionally, before the
`beyond_submission_horizon` check is even reached. No path bypasses it.

## 2. LOST branch — HOLDS

`mark_lost` (`submission/protocol.py:305-320`) is a CAS: `UPDATE ... WHERE
state = 'unknown'`, and `resolve` (`protocol.py:370-421`) only calls it after
checking `deadline is not None and moment < deadline` first — so LOST is
reachable only past the submission's own `resolution_deadline`, never on a
"stale" or premature row. A LOST submission whose attempt actually ran is not
possible under the protocol's own invariant: `resolve`'s FOUND branch
(positive `describe`) is checked before the deadline logic and returns FOUND
first, so the only way to reach LOST is a negative re-query past deadline —
by definition the job doesn't exist under that name. The
race "job appears after the LOST verdict" cannot cause a wrong classification
here: `_reconcile_unresolved`'s LOST branch just skips the horizon gate and
falls into the same `attempt_lease` + `reread_attempt` + `_attempt_ran`
machinery every other exit from the gate uses (`service.py:1667-1673,
1732`), untouched by this package. `_attempt_ran`'s contradictory-vs-terminal
distinction (`service.py:1017`, called at `:1732`) sits inside the lease,
strictly after the new branch decides only whether to enter — it still runs
exactly as before regardless of which gate route got there.

## 3. Fail-open / rollback — HOLDS

Traced the call sequence in `_reconcile_unresolved`: `_submission_classification`
(`service.py:1608-1624`) is called at line 1643, **before** `attempt_lease` is
entered at line 1667. Its internal `self._safe_rollback()` on a raised lookup
fires only around a bare `submission_for_attempt` SELECT
(`service.py:1614`) that has taken no lease and made no writes — there is
nothing yet for that rollback to discard, and no lease is open at that point
for it to corrupt. Symmetrically, `_resolve_submissions` (`service.py:518-568`)
runs at the very top of `poll_once`, before `open_attempts()`'s lease loop
even starts (`service.py:390-393`); its own `_safe_rollback()` on failure
(`service.py:544`) likewise has nothing else on the connection to discard.
Every other call site into `_reconcile_unresolved`/`_reconcile_attempt` is
itself wrapped in the pre-existing per-row try/except with its own
`_safe_rollback()` (`service.py:462-472`, `:479-483`) — consistent with the
established pattern the whole file already uses for exactly this reason.

## 4. `_OPEN_COLUMNS` gained `submission_id` — HOLDS

`reread_attempt` (`lease.py:120-151`) builds its result via
`dict(zip(names, row))` off `cur.description`, and the SQL is built by
`", ".join(columns)` — no positional indexing anywhere. `open_attempts`
(`service.py:320`, uses `_OPEN_SET_SQL`) is likewise a plain `SELECT
<join> FROM attempts`, consumed as dicts throughout the file (grepped every
`row[...]`/`row.get(...)` site — all keyed access, no `row[N]` integer
indexing found in the diff or its neighbors). Appending a column to a
name-driven, dict-returning tuple is additive and safe; this is not the
same defect class as `open_submissions`/`_OPEN_SQL`'s positional
`zip(columns, row)` in `protocol.py` (still order-sensitive, but unchanged
by this package and matches its existing column list unmodified).

## 5. Commit boundary — HOLDS, with one clarification

`resolve_open` (`protocol.py:429-458`) never raises out of itself — every
row is wrapped in its own try/except (`:447-455`) that counts into
`counts["errors"]` and continues; the "raises midway after some rows already
committed" scenario in the attack framing does not actually arise from
`resolve_open` itself. Since `resolve`/`mark_*` never commit
(`_Executor.execute`, `service.py:1786-1791`, is a bare cursor — no commit
call), no row's write is durable until `_resolve_submissions`'s own
`self.conn.commit()` at `service.py:557` — a single commit that lands the
**entire batch of resolved rows at once**, not per-row despite the comment's
"committed per resolved row" framing (`service.py:551-554`). That comment
somewhat overstates the mechanism — it is "per pass that resolved something,"
not "per row" — but the practical effect claimed (no long-lived transaction
spanning many Batch calls within a single poll cycle, durable before the
next poll's re-query) still holds, since the whole pass happens inside one
`poll_once` call and one commit ends it. The only way `_resolve_submissions`
itself raises past `resolve_open` is `is_available` raising or a bug in
`resolve_open`'s own control flow (e.g. `open_submissions`'s SELECT itself
failing) — caught by the outer try/except (`:537-547`), rolled back, logged,
counted as an error; no partial-commit path exists because nothing commits
until after `resolve_open` returns cleanly.

## 6. Idempotence / re-entrancy — HOLDS

Both `mark_found` and `mark_lost` are CAS UPDATEs (`WHERE state IN ('calling',
'unknown')` / `WHERE state = 'unknown'`, `protocol.py:145-156`), verified by
`_require_one` (`:462-469`) which raises `SubmissionStateConflict` if the
UPDATE's rowcount isn't exactly 1. Two concurrent reconcilers both reading
the same open submission via `open_submissions` (an unlocked SELECT) can both
attempt to resolve it, but only one UPDATE matches the CAS predicate; the
loser's `_require_one` raises, caught by `resolve_open`'s per-row
try/except (`protocol.py:447-455`), counted as an error, row stays open for
next pass — no double-classification, no double-commit, no crash. This
mirrors the CAS discipline already used by `mark_calling`/`mark_bound`/
`mark_unknown`, which this package's read path (`submission_for_attempt`)
does not touch or weaken — it only adds a plain SELECT, taking no lock,
consistent with the protocol's existing "read-only, no transaction of its
own" idiom (`protocol.py:361-362`).

## Summary

| Attack | Verdict |
|---|---|
| 1. FOUND branch bypass | HOLDS |
| 2. LOST branch misclassification | HOLDS |
| 3. Fail-open rollback corrupts lease | HOLDS |
| 4. `_OPEN_COLUMNS` positional break | HOLDS |
| 5. Commit boundary / partial pass | HOLDS (comment slightly overstates "per row" vs. "per pass") |
| 6. Concurrent double-classification | HOLDS |

No defects found. No severity ranking needed.
