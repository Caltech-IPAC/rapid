"""Stand-ins for the statistics stage's boundaries: a crossmatch-shaped input manifest, a fake database.

- :func:`build_statistics_input_set` writes a completion manifest shaped
  like ``crossmatch``'s own (step 1 ruling R2, R10): stage ``crossmatch``,
  unit kind ``field``, unit id the rtid, one ``association-set`` output
  entry with key ``{field, base, source_sets, settings_hash}`` and
  ``registration`` naming the two tables and their row counts, no members.
- :class:`FakeStatisticsDatabase` has
  :class:`rapidpipe.stages.statistics.PostgresStatisticsDatabase`'s methods
  over in-memory state: registered association sets (kind, base, source
  sets, completeness), the ``sources`` child table of each source set,
  ``merges_<field>`` rows and ``sources`` rows keyed by ``result_set``, the
  ``astroobjectsmeta_<field>`` rows COPYed, and the registered statistics
  sets. :func:`fake_database` is the factory ``RAPIDPIPE_STATISTICS_DATABASE``
  names for a stage run as a subprocess (``make stage-statistics``): it
  reads its seed from the JSON file ``RAPIDPIPE_FAKE_STATISTICS_SEED``
  names and writes its state, on commit, to the JSON file
  ``RAPIDPIPE_FAKE_STATISTICS_STATE`` names.

Packaged under ``rapidpipe.selftest.support`` (not ``tests/``, which the
pipeline image excludes at build time -- ``containers/rapid-pipeline/
build.sh``) so ``rapidpipe selftest --stage statistics`` can import it
inside the image.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import json
import os
from pathlib import Path
from typing import Any, Sequence

from rapidpipe.db import objects as _objects

FIELD = 4711398
UNIT_ID = str(FIELD)

RUN_ID = "01J8Y6QZ3M00000000000XRUN0"
ATTEMPT_ID = "01J8Y6QZ3M00000000000XATT0"
ASSOCIATION_SET = "01J8Y6QZ3M00000000000ASC02"
BASE_SET = "01J8Y6QZ3M00000000000ASC01"


def build_statistics_input_set(
    inputs: Path,
    *,
    field: int = FIELD,
    instance: str = ASSOCIATION_SET,
    base: str | None = BASE_SET,
    source_sets: Sequence[str] = (),
    row_counts: dict[str, int] | None = None,
    extra_outputs: Sequence[dict[str, Any]] = (),
) -> Path:
    """Write a `crossmatch`-shaped completion manifest naming one `association-set` entry."""
    manifest = {
        "schema_version": "1", "run": RUN_ID,
        "unit": {"kind": "field", "id": str(field)},
        "stage": "crossmatch", "attempt": ATTEMPT_ID,
        "execution_record": f"exec/{ATTEMPT_ID}.json",
        "inputs": {"manifest": "inputs/manifest.json", "products": {},
                   "result_sets": sorted(source_sets) + ([base] if base else [])},
        "outputs": [{
            "kind": "association-set", "format_version": "1", "instance": instance,
            "key": {"field": field, "base": base, "source_sets": sorted(source_sets),
                    "settings_hash": "0" * 64},
            "primary": None, "members": [],
            "registration": {
                "astroobjects_table": f"astroobjects_{field}", "merges_table": f"merges_{field}",
                "row_counts": row_counts or {"astroobjects": 0, "merges": 0, "merges_pass1": 0,
                                             "merges_pass2": 0, "new_objects": 0}},
        }, *extra_outputs],
    }
    inputs.mkdir(parents=True, exist_ok=True)
    path = inputs / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


def seed_from_fixture(spec: dict[str, Any]) -> dict[str, Any]:
    """The fake database's seed from the fixture's ``inputs`` block (``expected.json``).

    Two association sets, the input and its base, each naming one source
    set; each object's sources are ``[set, sid, ra, dec, fluxfit]`` with
    ``set`` ``"base"`` or ``"delta"``, giving a ``merges_<field>`` row under
    that association set and a ``sources`` row under that source set. The
    ``outside_the_chain`` block adds a third association set and source set
    no chain names, whose rows must not be read.
    """
    field = int(spec["field"])
    sets = {"base": spec["base_set"], "delta": spec["association_set"]}
    source_sets = spec["source_sets"]
    instances = {
        spec["base_set"]: {"kind": "association-set", "base": None,
                           "source_sets": [source_sets["base"]["instance"]], "complete": True},
        spec["association_set"]: {"kind": "association-set", "base": spec["base_set"],
                                  "source_sets": [source_sets["delta"]["instance"]],
                                  "complete": True},
    }
    tables = {s["instance"]: s["table"] for s in source_sets.values()}
    merges: list[list] = []
    sources: dict[str, list[list]] = {t: [] for t in tables.values()}
    for obj in spec["objects"]:
        for which, sid, ra, dec, flux in obj["sources"]:
            merges.append([obj["aid"], sid, sets[which]])
            sources[source_sets[which]["table"]].append(
                [sid, ra, dec, flux, source_sets[which]["instance"]])
    outside = spec.get("outside_the_chain")
    if outside:
        instances[outside["association_set"]] = {
            "kind": "association-set", "base": None,
            "source_sets": [outside["source_set"]], "complete": True}
        tables[outside["source_set"]] = outside["table"]
        for obj in outside["objects"]:
            for sid, ra, dec, flux in obj["sources"]:
                merges.append([obj["aid"], sid, outside["association_set"]])
                sources.setdefault(outside["table"], []).append(
                    [sid, ra, dec, flux, outside["source_set"]])
    return {"instances": instances, "source_set_tables": tables,
            "merges": {str(field): merges}, "sources": sources}


class FakeStatisticsDatabase:
    """In-memory :class:`~rapidpipe.stages.statistics.PostgresStatisticsDatabase`.

    ``instances`` maps an instance id to ``{"kind", "base", "source_sets",
    "complete"}``; ``source_set_tables`` a source-set instance to its
    ``sources`` child table; ``merges`` a field (as a string) to rows
    ``[aid, sid, result_set]``; ``sources`` a child table to rows ``[sid,
    ra, dec, fluxfit, result_set]``; ``statistics_sets`` an instance to what
    registered it.
    """

    def __init__(self, instances=None, source_set_tables=None, merges=None, sources=None,
                 statistics_sets=None) -> None:
        self.instances: dict[str, dict] = dict(instances or {})
        self.source_set_tables: dict[str, str] = dict(source_set_tables or {})
        self.merges: dict[str, list[list]] = dict(merges or {})
        self.sources: dict[str, list[list]] = dict(sources or {})
        self.statistics_sets: dict[str, dict] = dict(statistics_sets or {})
        self.tables: dict[str, list[dict]] = {}
        self.made_tables: list[str] = []
        self.queries: list[dict] = []
        self.commits = 0
        self.on_commit = None

    def association_chain(self, instance: str, run_id: str | None = None) -> list[str]:
        chain: list[str] = []
        current: str | None = instance
        while current is not None:
            if current in chain:
                raise ValueError(f"association chain of {instance!r} loops at {current!r}")
            row = self.instances.get(current)
            if row is None:
                raise ValueError(f"association chain of {instance!r}: no instance {current!r}")
            if row.get("kind", "association-set") != "association-set":
                raise ValueError(f"association chain of {instance!r}: {current!r} is a "
                                 f"{row['kind']}, not an association-set")
            chain.append(current)
            current = row.get("base") or None
        return chain

    def chain_source_sets(self, chain: Sequence[str]) -> list[str]:
        found: set[str] = set()
        for instance in chain:
            row = self.instances[instance]
            if not row.get("complete", True):
                raise ValueError(f"association set {instance!r} is not a complete result set")
            found.update(row.get("source_sets") or [])
        return sorted(found)

    def source_set_table(self, instance: str, run_id: str | None = None) -> str:
        if instance not in self.source_set_tables:
            raise ValueError(f"no source-set result set with instance {instance!r}")
        return self.source_set_tables[instance]

    def find_complete_statistics_set(self, run_id, key, attempt_id=None):
        for instance, s in sorted(self.statistics_sets.items()):
            if s["run"] == run_id and s["key"] == key and s["complete"]:
                return instance, s["row_count"]
        return None

    def ensure_astroobjectsmeta_table(self, field: int) -> bool:
        table = _objects.field_table_names(field)["astroobjectsmeta"]
        if table in self.tables:
            return False
        self.tables[table] = []
        self.made_tables.append(table)
        return True

    def _merges_rows(self, field: int) -> list[list]:
        if str(field) not in self.merges:
            raise ValueError(f"merges_{field} does not exist: the association set has no rows table")
        return self.merges[str(field)]

    def member_rows(self, field, chain, source_tables) -> list[tuple]:
        if not source_tables:
            return []
        merges = self._merges_rows(field)
        self.queries.append({"field": field, "chain": list(chain),
                             "source_tables": [list(t) for t in source_tables]})
        out = []
        for table, source_set in source_tables:
            by_sid = {int(r[0]): r for r in self.sources.get(table, []) if r[4] == source_set}
            for aid, sid, result_set in merges:
                if result_set in chain and int(sid) in by_sid:
                    s = by_sid[int(sid)]
                    out.append((int(aid), int(sid), float(s[1]), float(s[2]), float(s[3])))
        return out

    def objects_in_set(self, field, chain) -> int:
        return len({int(aid) for aid, _, rs in self._merges_rows(field) if rs in chain})

    def register_statistics_set(self, manifest, attempt_id) -> None:
        (entry,) = manifest["outputs"]
        self.statistics_sets[entry["instance"]] = {
            "run": manifest["run"], "stage": manifest["stage"], "attempt": attempt_id,
            "kind": entry["kind"], "key": entry["key"], "row_count": entry["row_count"],
            "complete": True, "result_sets": list(manifest["inputs"]["result_sets"])}

    def copy_astroobjectsmeta(self, field: int, csv_path: Path) -> int:
        table = self.tables[_objects.field_table_names(field)["astroobjectsmeta"]]
        existing = {(r["result_set"], r["aid"]) for r in table}
        inserted = 0
        with Path(csv_path).open() as fh:
            for values in csv.reader(fh):
                if len(values) != len(_objects.ASTROOBJECTSMETA_COLUMNS):
                    raise ValueError(f"COPY row has {len(values)} columns, expected "
                                     f"{len(_objects.ASTROOBJECTSMETA_COLUMNS)}")
                row = dict(zip(_objects.ASTROOBJECTSMETA_COLUMNS, values))
                if (row["result_set"], row["aid"]) in existing:
                    continue
                existing.add((row["result_set"], row["aid"]))
                table.append(row)
                inserted += 1
        return inserted

    def count_result_set_rows(self, table: str, result_set: str) -> int:
        return sum(1 for r in self.tables.get(table, []) if r["result_set"] == result_set)

    def commit(self) -> None:
        self.commits += 1
        if self.on_commit is not None:
            self.on_commit(self)

    def state(self) -> dict[str, Any]:
        return {"tables": copy.deepcopy(self.tables), "made_tables": list(self.made_tables),
                "statistics_sets": copy.deepcopy(self.statistics_sets),
                "queries": copy.deepcopy(self.queries), "commits": self.commits}


SEED_ENV = "RAPIDPIPE_FAKE_STATISTICS_SEED"
STATE_ENV = "RAPIDPIPE_FAKE_STATISTICS_STATE"


def fake_database():
    """The ``RAPIDPIPE_STATISTICS_DATABASE`` factory: seeded from, and saved to, JSON files."""
    seed = json.loads(Path(os.environ[SEED_ENV]).read_text())
    db = FakeStatisticsDatabase(
        instances=seed.get("instances"), source_set_tables=seed.get("source_set_tables"),
        merges=seed.get("merges"), sources=seed.get("sources"),
        statistics_sets=seed.get("statistics_sets"))
    state_path = Path(os.environ[STATE_ENV])

    def save(database: FakeStatisticsDatabase) -> None:
        state_path.write_text(json.dumps(database.state(), indent=2, sort_keys=True))

    db.on_commit = save
    return contextlib.nullcontext(db)
