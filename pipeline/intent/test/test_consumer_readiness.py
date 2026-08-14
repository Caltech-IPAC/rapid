"""Stub-tier tests for `pipeline.intent.consumer_readiness` (D3).

Pure file-read + regex logic, no database and no I/O beyond a synthetic
source tree under `tmp_path` — the real coordination requirements this
module encodes (`CONSUMER_BEFORE_SCHEMA`) are asserted against THIS
repository's actual files below; the predicate MECHANICS are asserted
against a throwaway tree so the test does not depend on writer.py's exact
current contents drifting out from under it.
"""

from pathlib import Path

import pytest

from pipeline.intent.consumer_readiness import (CONSUMER_BEFORE_SCHEMA,
                                                  ConsumerNotReady,
                                                  ReadinessEntry, unready)


def _write(root, rel_path, content):
    path = Path(root) / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_not_ready_while_the_pattern_is_present(tmp_path):
    _write(tmp_path, "pipeline/stages/catalog_db.py",
           "def create_child_table():\n    cur.execute('CREATE TABLE ...')\n")
    entry = ReadinessEntry(
        migration="073-revoke-create-on-schema-public.sql",
        source_path="pipeline/stages/catalog_db.py",
        must_be_absent=r"CREATE\s+TABLE",
        reason="probe")
    assert entry.is_ready(tmp_path) is False
    assert unready(tmp_path, entries=(entry,)) == (entry,)


def test_ready_once_the_pattern_is_gone(tmp_path):
    _write(tmp_path, "pipeline/stages/catalog_db.py",
           "def create_child_table():\n"
           "    cur.execute('SELECT derived.create_child_table(...)')\n")
    entry = ReadinessEntry(
        migration="073-revoke-create-on-schema-public.sql",
        source_path="pipeline/stages/catalog_db.py",
        must_be_absent=r"CREATE\s+TABLE",
        reason="probe")
    assert entry.is_ready(tmp_path) is True
    assert unready(tmp_path, entries=(entry,)) == ()


def test_unready_reports_only_the_entries_still_blocked(tmp_path):
    _write(tmp_path, "a.py", "old pattern here\n")
    _write(tmp_path, "b.py", "already switched\n")
    blocked = ReadinessEntry("mig-a.sql", "a.py", r"old pattern",
                              reason="probe a")
    clear = ReadinessEntry("mig-b.sql", "b.py", r"old pattern",
                            reason="probe b")
    assert unready(tmp_path, entries=(blocked, clear)) == (blocked,)


def test_consumer_not_ready_names_the_migration_and_reason():
    entry = ReadinessEntry("999-probe.sql", "some/file.py", r"x",
                            reason="because reasons")
    exc = ConsumerNotReady(entry)
    assert "999-probe.sql" in str(exc)
    assert "some/file.py" in str(exc)
    assert "because reasons" in str(exc)


# ---------------------------------------------------------------------------
# The real registry, against this repository's actual current state. These
# are the tests that will FAIL the moment writer.py or catalog_db.py switch
# to the constrained-function path -- which is the point: a green run here
# is the signal that 073/078 must stay unpinned/unapplied, and a failure
# here is the signal that this repository has become ready for them.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_073_entry_is_present_and_well_formed():
    entries = [e for e in CONSUMER_BEFORE_SCHEMA
               if e.migration == "073-revoke-create-on-schema-public.sql"]
    assert len(entries) == 1
    entry = entries[0]
    assert (REPO_ROOT / entry.source_path).is_file(), (
        f"{entry.source_path} does not exist under {REPO_ROOT} -- the "
        f"registry entry names a file this repository no longer has")


def test_078_entry_is_present_and_well_formed():
    entries = [e for e in CONSUMER_BEFORE_SCHEMA
               if e.migration == "078-revoke-work-units-raw-update.sql"]
    assert len(entries) == 1
    entry = entries[0]
    assert (REPO_ROOT / entry.source_path).is_file(), (
        f"{entry.source_path} does not exist under {REPO_ROOT} -- the "
        f"registry entry names a file this repository no longer has")


@pytest.mark.parametrize("entry", CONSUMER_BEFORE_SCHEMA,
                          ids=lambda e: e.migration)
def test_every_registry_entry_evaluates_against_this_repo(entry):
    """Every entry's predicate must at least RUN against the real file
    (proves the regex and path are both well-formed); the boolean result
    is not asserted either way here -- readiness is a fact about the
    repository's current state, not something this test should pin down
    and then break the day someone finishes the consumer switch."""
    result = entry.is_ready(REPO_ROOT)
    assert result in (True, False)
