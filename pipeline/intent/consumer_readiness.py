"""
File:    consumer_readiness.py

D3: the mirror image of `schema_contract.py`, checked at the opposite end of
the pipe.

`schema_contract.py` asks, at service startup: "does the DEPLOYED SCHEMA
satisfy what THIS BUILD's code requires?" — a floor, checked every time a
service starts, because code and schema deploy on separate schedules and a
service must refuse to start against a schema older than what it needs.

This module asks the opposite question, at the opposite time: "is THIS
BUILD's code ready for a SCHEMA CHANGE ABOUT TO BE PINNED?" — checked once,
by whoever is about to advance `RAPID_SYSTEMS_REF`
(`.github/workflows/contract-tests.yml`) or run the applier
(`rapid_systems/cloudformation/apply-db-migrations.sh`) against production,
never by a running service.

**WHY THIS EXISTS.** Migrations 073 and 078 (rapid_systems) each carry a
hand-written "COORDINATION REQUIREMENT" prose paragraph in their own header:
073 must not apply to a database whose running pipeline image still uses the
raw-DDL `create_child_table` path; 078 must not apply while `WorkUnitWriter`
still issues its three raw `UPDATE public.work_units` statements. Both are
CONSUMER-BEFORE-SCHEMA: applying the migration before the rapid-side switch
ships breaks production outright (078's own header: "EVERY work-unit write
in the running system starts failing ... a hard outage of the intent
layer"). Nothing machine-readable enforced either paragraph — an operator
had to have read and remembered the prose at the moment they ran the
applier. `CONSUMER_BEFORE_SCHEMA` below is that prose, restated as a
predicate this repository can evaluate against its OWN source, so "has the
rapid-side switch shipped" becomes a question this code answers about
itself rather than a fact an operator recalls under time pressure.

**SCOPE: CONSUMER-BEFORE-SCHEMA ONLY.** 075 is the opposite direction
(schema-before-consumer: the migration is safe to apply early, and it is the
CONSUMER change that must wait). That direction's risk lives entirely on the
rapid_systems/deploy side — a rapid-side check has nothing to protect here,
because an old, unswitched consumer against a newer schema is exactly the
expand/contract safety `schema_contract.py`'s own docstring describes as
normal. Consumer-before-schema is the direction where THIS repository's
readiness is the fact that matters, which is why it is the direction this
module owns.

**WHAT A "READY" PREDICATE MEANS.** Each entry names a source location and
an ABSENCE this repository's code must exhibit before the paired migration
may be pinned/applied — the raw SQL pattern the coordination paragraph says
must be gone. `grep`-checkable against a plain source read, deliberately not
an AST search: the predicate is meant to be legible in a diff review the
same way the coordination paragraph itself is prose, not a compiler pass.
A false negative (pattern gone for an unrelated reason, migration still
actually unsafe) is possible in principle and is why this is ONE input to
the pin-bump decision, not a replacement for reading the paragraph it
encodes.

**HOW THIS GETS INVOKED.** `is_ready(entry, repo_root)` reads the named file
from a checkout and reports the predicate. A pin-bump check (CI step,
proposed but not yet wired — see the wave-d ledger's D3 rapid_systems-side
specification for where it hooks on the applier side) calls this before
advancing `RAPID_SYSTEMS_REF` past a commit that introduces a
consumer-before-schema migration this repository has not yet satisfied.
"""

import re
from pathlib import Path


class ConsumerNotReady(RuntimeError):
    """This build's code has not yet made the switch a pending
    consumer-before-schema migration requires. Pinning `RAPID_SYSTEMS_REF`
    past that migration, or applying it to a database this image serves,
    would be the exact ordering violation the migration's own
    "COORDINATION REQUIREMENT" paragraph warns against."""

    def __init__(self, entry):
        self.entry = entry
        super().__init__(
            f"{entry.migration} requires {entry.source_path} to no longer "
            f"match {entry.must_be_absent!r} ({entry.reason}), but it still "
            f"does — this repository is not ready for that migration to be "
            f"applied or pinned")


class ReadinessEntry:
    """One consumer-before-schema coordination requirement, restated as a
    predicate over this repository's own source.

    `must_be_absent` is a regular expression: the migration is safe once no
    line in `source_path` matches it. `line_hint` is the line range the
    migration's own header cited when it was written — not re-checked (line
    numbers drift as the file is edited), carried only so a human reading a
    failure knows where to look first.
    """

    def __init__(self, migration, source_path, must_be_absent, reason,
                 line_hint=""):
        self.migration = migration
        self.source_path = source_path
        self.must_be_absent = must_be_absent
        self.reason = reason
        self.line_hint = line_hint

    def is_ready(self, repo_root):
        """True once `source_path` no longer matches `must_be_absent`."""
        text = (Path(repo_root) / self.source_path).read_text()
        return re.search(self.must_be_absent, text, re.MULTILINE) is None


#: rapid_systems migration -> the rapid-side switch it requires first.
#:
#: Each entry is the machine-readable restatement of that migration's own
#: "COORDINATION REQUIREMENT" header paragraph (rapid_systems repo, read in
#: full before editing this list — the prose is the authority; this is a
#: predicate over it, not a replacement for it).
CONSUMER_BEFORE_SCHEMA = (
    ReadinessEntry(
        migration="073-revoke-create-on-schema-public.sql",
        source_path="pipeline/stages/catalog_db.py",
        must_be_absent=r"CREATE\s+TABLE",
        reason="073 revokes rapid_pipeline_write's CREATE ON SCHEMA public; "
               "the consumer must route child-table creation through "
               "derived.create_child_table() instead of raw DDL",
        line_hint="073's own header, lines 17-25",
    ),
    ReadinessEntry(
        migration="078-revoke-work-units-raw-update.sql",
        source_path="pipeline/intent/writer.py",
        must_be_absent=r'UPDATE\s+work_units\s+SET',
        reason="078 revokes rapid_pipeline_write's raw UPDATE on "
               "work_units; WorkUnitWriter's transition_unit, "
               "amend_blocked_reason and supersede_unit must call "
               "derived.transition_work_unit / derived.amend_blocked_reason "
               "/ derived.supersede_unit (077) instead of issuing "
               "UPDATE public.work_units directly",
        line_hint="078's own header, lines 17-23 "
                   "(writer.py:501-508, 550-556, 586-593 as of that header)",
    ),
)


def unready(repo_root, entries=CONSUMER_BEFORE_SCHEMA):
    """The subset of `entries` this repository is NOT yet ready for.

    Returns a tuple of `ReadinessEntry`, empty when every consumer-before-
    schema requirement this repository knows about is satisfied — the state
    a pin-bump check demands before advancing `RAPID_SYSTEMS_REF` past any
    of the named migrations.
    """
    return tuple(e for e in entries if not e.is_ready(repo_root))
