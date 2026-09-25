"""Stand-ins for the prune stage's boundaries: a crossmatch-shaped input manifest, a fake database.

- :func:`build_prune_input_set` writes a completion manifest shaped like
  `crossmatch`'s own (stage ``crossmatch``, unit kind ``field``, a single
  ``association-set`` output entry) -- the shape ``prune`` reads (step 1
  ruling R2).
- :class:`FakePruneDatabase` has
  :class:`rapidpipe.stages.prune.PostgresPruneDatabase`'s methods over
  in-memory state: seeded `merges` rows, `sources` rows (pid only -- the
  rest of a source row is irrelevant to pruning), `diffimages` rows
  (`vbest`, `run`), and the association chain each association-set
  instance names (its base and its own source sets). Records the
  `prunedmerges` rows inserted and the pruned set registered.
  :func:`fake_database` is the factory ``RAPIDPIPE_PRUNE_DATABASE`` names
  for a stage run as a subprocess (``make stage-prune``): it reads its
  seed from the JSON file ``RAPIDPIPE_FAKE_PRUNE_SEED`` names and writes
  its state, on commit, to the JSON file ``RAPIDPIPE_FAKE_PRUNE_STATE``
  names.

Packaged under ``rapidpipe.selftest.support`` (not ``tests/``, which the
pipeline image excludes at build time -- ``containers/rapid-pipeline/
build.sh``) so ``rapidpipe selftest --stage prune`` can import it inside
the image. ``tests/unit/fakeprunedb.py`` re-exports this module for the
test suite.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

FIELD = 424242
UNIT_ID = str(FIELD)
RUN_ID = "01J8Y6QZ3M00000000000PRUN"
CROSSMATCH_RUN_ID = "01J8Y6QZ3M0000000000CXRUN"
CROSSMATCH_ATTEMPT_ID = "01J8Y6QZ3M00000000CXATT00"
ASSOCIATION_INSTANCE = "01J8Y6QZ3M0000000000ASSOC"
SOURCE_SET_INSTANCE = "01J8Y6QZ3M00000000000SS01"
SOURCES_TABLE = "sources_20260821_7"


def build_prune_input_set(
    inputs: Path, *, field: int = FIELD, association_instance: str = ASSOCIATION_INSTANCE,
    base: str | None = None, source_sets: list[str] | None = None,
    settings_hash: str = "sha256:" + "0" * 64,
) -> Path:
    """Write a `crossmatch`-shaped completion manifest naming one `association-set` entry."""
    key = {"field": field, "base": base, "source_sets": source_sets or [SOURCE_SET_INSTANCE],
           "settings_hash": settings_hash}
    manifest = {
        "schema_version": "1", "run": CROSSMATCH_RUN_ID,
        "unit": {"kind": "field", "id": str(field)},
        "stage": "crossmatch", "attempt": CROSSMATCH_ATTEMPT_ID,
        "execution_record": f"exec/{CROSSMATCH_ATTEMPT_ID}.json",
        "inputs": {"manifest": "inputs/manifest.json", "products": {}, "result_sets": []},
        "outputs": [{
            "kind": "association-set", "format_version": "1", "instance": association_instance,
            "key": key, "primary": None, "members": [], "registration": {},
        }],
    }
    inputs.mkdir(parents=True, exist_ok=True)
    path = inputs / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


class FakePruneDatabase:
    """In-memory :class:`~rapidpipe.stages.prune.PostgresPruneDatabase`.

    ``association_sets`` maps an instance to ``{"base": ..., "source_sets":
    [...]}``; ``source_set_tables`` maps a source-set instance to its
    sources table name; ``sources`` maps a table name to a list of ``{sid,
    pid, result_set}`` rows; ``diffimages`` maps a `pid` to ``{vbest,
    run}``; ``merges`` maps a field to a list of ``{aid, sid, result_set}``
    rows.
    """

    def __init__(self, *, association_sets: dict[str, dict] | None = None,
                source_set_tables: dict[str, str] | None = None,
                sources: dict[str, list[dict]] | None = None,
                diffimages: dict[Any, dict] | None = None,
                merges: dict[str, list[dict]] | None = None,
                pruned_sets: dict[str, dict] | None = None) -> None:
        self.association_sets = copy.deepcopy(association_sets or {})
        self.source_set_tables = dict(source_set_tables or {})
        self.sources = copy.deepcopy(sources or {})
        self.diffimages = copy.deepcopy(diffimages or {})
        self.merges = copy.deepcopy(merges or {})
        self.pruned_sets = copy.deepcopy(pruned_sets or {})
        self.prunedmerges: list[dict] = []
        self.commits = 0
        self.on_commit = None

    def find_complete_pruned_set(self, run_id, key, attempt_id=None):
        for instance, s in sorted(self.pruned_sets.items()):
            if s["run"] == run_id and s["key"] == key and s["complete"]:
                return instance, s["row_count"]
        return None

    def association_chain(self, instance: str, run_id: str | None = None) -> list[str]:
        chain: list[str] = []
        current = instance
        while current is not None:
            if current in chain:
                raise ValueError(f"association chain of {instance!r} loops at {current!r}")
            entry = self.association_sets.get(current)
            if entry is None:
                raise ValueError(f"association chain of {instance!r}: no instance {current!r}")
            chain.append(current)
            current = entry.get("base")
        return chain

    def chain_source_sets(self, chain: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for instance in chain:
            for source_set in self.association_sets[instance]["source_sets"]:
                seen.setdefault(source_set, None)
        return list(seen)

    def source_set_table(self, instance: str, run_id: str | None = None) -> tuple[str, int | None]:
        if instance not in self.source_set_tables:
            raise ValueError(f"no source-set result set with instance {instance!r}")
        return self.source_set_tables[instance], None

    def not_best_pairs(self, field: int, chain: list[str],
                       tables_and_sets: list[tuple[str, str]], run_id: str) -> list[tuple[int, int]]:
        notbest_sids: set[int] = set()
        for table, source_set in tables_and_sets:
            for row in self.sources.get(table, []):
                if row["result_set"] != source_set:
                    continue
                diff = self.diffimages.get(row["pid"], {"vbest": 1, "run": None})
                if diff.get("vbest") == 0 and diff.get("run") != run_id:
                    notbest_sids.add(row["sid"])
        pairs = []
        for row in self.merges.get(str(field), []):
            if row["result_set"] in chain and row["sid"] in notbest_sids:
                pairs.append((row["aid"], row["sid"]))
        return pairs

    def base_row_count(self, field: int, chain: list[str]) -> int:
        return sum(1 for row in self.merges.get(str(field), []) if row["result_set"] in chain)

    def register_pruned_set(self, manifest: dict[str, Any], attempt_id: str) -> None:
        (entry,) = manifest["outputs"]
        self.pruned_sets[entry["instance"]] = {
            "run": manifest["run"], "stage": manifest["stage"], "attempt": attempt_id,
            "key": entry["key"], "row_count": entry["row_count"], "complete": True,
            "inputs_result_sets": list(manifest["inputs"]["result_sets"])}

    def insert_pruned_merges(self, rows: list[tuple[int, int]], result_set: str, base_set: str,
                             run: str, attempt: str) -> int:
        existing = {(r["result_set"], r["aid"], r["sid"]) for r in self.prunedmerges}
        inserted = 0
        for aid, sid in rows:
            key = (result_set, aid, sid)
            if key in existing:
                continue
            existing.add(key)
            self.prunedmerges.append({"result_set": result_set, "base_set": base_set, "aid": aid,
                                      "sid": sid, "run": run, "attempt": attempt})
            inserted += 1
        return inserted

    def commit(self) -> None:
        self.commits += 1
        if self.on_commit is not None:
            self.on_commit(self)

    def state(self) -> dict[str, Any]:
        return {"pruned_sets": copy.deepcopy(self.pruned_sets),
                "prunedmerges": copy.deepcopy(self.prunedmerges), "commits": self.commits}


SEED_ENV = "RAPIDPIPE_FAKE_PRUNE_SEED"
STATE_ENV = "RAPIDPIPE_FAKE_PRUNE_STATE"

#: A packaged fixture cannot know the invocation's run id in advance (it is
#: minted by ``rapidpipe.selftest.runner.run_fixture`` after ``prepare``
#: writes the seed file), but R6's own-run clause needs a `diffimages.run`
#: value that matches it. A seeded row's ``"run"`` of this sentinel is
#: replaced with the running attempt's own ``--run`` argument, read from
#: ``sys.argv`` since :func:`fake_database` executes inside that same
#: subprocess.
OWN_RUN_SENTINEL = "__OWN_RUN__"


def _own_run_id() -> str | None:
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--run" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _decode_pid_key(k: str) -> Any:
    """JSON object keys are always strings; `pid` is stored as an int elsewhere."""
    try:
        return int(k)
    except ValueError:
        return k


def fake_database():
    """The ``RAPIDPIPE_PRUNE_DATABASE`` factory: seeded from, and saved to, JSON files."""
    seed = json.loads(Path(os.environ[SEED_ENV]).read_text())
    own_run = _own_run_id()
    diffimages = {}
    for k, v in seed.get("diffimages", {}).items():
        v = dict(v)
        if v.get("run") == OWN_RUN_SENTINEL:
            v["run"] = own_run
        diffimages[_decode_pid_key(k)] = v
    db = FakePruneDatabase(
        association_sets=seed.get("association_sets"),
        source_set_tables=seed.get("source_set_tables"), sources=seed.get("sources"),
        diffimages=diffimages, merges=seed.get("merges"), pruned_sets=seed.get("pruned_sets"))
    state_path = Path(os.environ[STATE_ENV])

    def save(database: FakePruneDatabase) -> None:
        state_path.write_text(json.dumps(database.state(), indent=2, sort_keys=True))

    db.on_commit = save
    return contextlib.nullcontext(db)
