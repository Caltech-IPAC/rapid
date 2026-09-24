"""Stand-ins for the crossmatch stage's boundaries: an input-set manifest, a fake database.

- :func:`build_crossmatch_input_set` writes an input-set manifest (stage
  ``crossmatch``, unit kind ``field``) naming one or more ``source-set``
  entries and, optionally, one base ``association-set`` entry -- the fan-in
  shape a launcher composes from several ``load`` attempts.
- :class:`FakeCrossmatchDatabase` has
  :class:`rapidpipe.stages.crossmatch.PostgresCrossmatchDatabase`'s methods
  over in-memory tables (lists of dicts). ``q3c_join`` and
  ``q3c_radial_query`` are exact angular-separation tests
  (:func:`rapidpipe.science.spatial.compute_angular_separation`); the
  catalog-visibility rule is the stage's (``result_set`` in the named sets,
  or ``run`` null under ``legacy_catalog``); the copies apply the
  set-scoped UNIQUE keys with ON CONFLICT DO NOTHING semantics and return
  the rows inserted. Source sets resolve to their child table through a
  seeded registry, as ``rapidpipe.db.objects.source_set_table`` does
  through the database. :func:`fake_database` is the factory
  ``RAPIDPIPE_CROSSMATCH_DATABASE`` names for a stage run as a subprocess
  (``make stage-crossmatch``): it reads its seed from the JSON file
  ``RAPIDPIPE_FAKE_CROSSMATCH_SEED`` names and writes its state, on commit,
  to the JSON file ``RAPIDPIPE_FAKE_CROSSMATCH_STATE`` names.

Packaged under ``rapidpipe.selftest.support`` (not ``tests/``, which the
pipeline image excludes at build time -- ``containers/rapid-pipeline/
build.sh``) so ``rapidpipe selftest --stage crossmatch`` can import it
inside the image; the unit tests import it from here too.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
from pathlib import Path
from typing import Any, Sequence

from rapidpipe.db import objects as _objects
from rapidpipe.science.spatial import compute_angular_separation

#: The fixture's field: an interior tile (eight neighbours) holding
#: `load`'s own fixture position (269.45, -28.77).
UNIT_ID = "4662268"
FIELD = 4662268

RUN_ID = "01J8Y6QZ3M00000000000XRUN"
ATTEMPT_ID = "01J8Y6QZ3M0000000000XATT0"

_ASTRO_KEY = ("result_set", "aid")
_MERGES_KEY = ("result_set", "aid", "sid")


def source_set_entry(instance: str, table: str, row_count: int = 0) -> dict[str, Any]:
    """A `load`-shaped ``source-set`` output entry."""
    return {
        "kind": "source-set", "format_version": "1", "instance": instance,
        "key": {"difference": f"difference-of-{instance}", "catalog_type": "photutils"},
        "primary": None, "members": [],
        "registration": {"row_count": row_count, "table": table},
    }


def association_set_entry(instance: str, field: int, base: str | None = None) -> dict[str, Any]:
    """A crossmatch-shaped ``association-set`` output entry, for a base."""
    return {
        "kind": "association-set", "format_version": "1", "instance": instance,
        "key": {"field": field, "base": base, "source_sets": [], "settings_hash": "0" * 64},
        "primary": None, "members": [], "registration": {},
    }


def build_crossmatch_input_set(inputs: Path, entries: Sequence[dict[str, Any]], *,
                               unit_id: str = UNIT_ID) -> Path:
    """Write an input-set manifest listing ``entries`` as its outputs."""
    manifest = {
        "schema_version": "1", "run": RUN_ID,
        "unit": {"kind": "field", "id": unit_id},
        "stage": "crossmatch", "attempt": ATTEMPT_ID,
        "execution_record": f"exec/{ATTEMPT_ID}.json",
        "inputs": {"manifest": "inputs/manifest.json", "products": {}, "result_sets": []},
        "outputs": list(entries),
    }
    inputs.mkdir(parents=True, exist_ok=True)
    path = inputs / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


def _float(value: str) -> float:
    return float(value)


class FakeCrossmatchDatabase:
    """In-memory :class:`~rapidpipe.stages.crossmatch.PostgresCrossmatchDatabase`.

    ``source_sets`` maps a source-set instance to ``{"table": ...,
    "complete": bool}``; ``association_sets`` maps an association-set
    instance to ``{"base": ..., "run": ..., "key": ..., "row_count": ...,
    "complete": bool}``; ``tables`` maps a table name to its rows.
    """

    def __init__(self, source_sets: dict[str, dict] | None = None,
                 association_sets: dict[str, dict] | None = None,
                 tables: dict[str, list[dict]] | None = None) -> None:
        self.source_sets = dict(source_sets or {})
        self.association_sets = dict(association_sets or {})
        self.tables: dict[str, list[dict]] = {k: list(v) for k, v in (tables or {}).items()}
        self.made_tables: list[str] = []
        self.clustered: list[int] = []
        self.locks: list[int] = []
        self.registered: list[dict] = []
        self.commits = 0
        self.on_commit = None

    # -- resolution ------------------------------------------------------

    def lock_field(self, field: int) -> None:
        self.locks.append(int(field))

    def source_set_table(self, instance: str) -> tuple[str, int | None]:
        found = self.source_sets.get(instance)
        if found is None:
            raise ValueError(f"no source-set result set with instance {instance!r}")
        if not found.get("complete", True):
            raise ValueError(f"source-set {instance!r} is not complete and retained")
        return found["table"], found.get("row_count")

    def association_chain(self, instance: str) -> list[str]:
        chain: list[str] = []
        current: str | None = instance
        while current is not None:
            if current in chain:
                raise ValueError(f"association chain of {instance!r} loops at {current!r}")
            found = self.association_sets.get(current)
            if found is None:
                raise ValueError(f"association chain of {instance!r}: no instance {current!r}")
            chain.append(current)
            current = found.get("base") or None
        return chain

    def find_complete_result_set(self, kind: str, run_id: str, key: dict[str, Any]):
        assert kind == "association-set"
        for instance in sorted(self.association_sets):
            found = self.association_sets[instance]
            if found.get("run") == run_id and found.get("key") == key and found.get("complete"):
                return instance, found.get("row_count")
        return None

    def ensure_field_object_tables(self, field: int) -> bool:
        made = False
        for name in (_objects.field_table_names(field)["astroobjects"],
                     _objects.field_table_names(field)["merges"]):
            if name not in self.tables:
                self.tables[name] = []
                self.made_tables.append(name)
                made = True
        return made

    # -- dev's four SELECTs ----------------------------------------------

    def _sources(self, table: str, flags: int, source_set: str) -> list[dict]:
        return [r for r in self.tables.get(table, [])
                if int(r["flags"]) == flags and r["result_set"] == source_set]

    def _catalog(self, field: int, catalog_sets: Sequence[str], legacy_catalog: bool) -> list[dict]:
        rows = self.tables[_objects.field_table_names(field)["astroobjects"]]
        return [b for b in rows if b.get("result_set") in catalog_sets
                or (legacy_catalog and b.get("run") is None)]

    @staticmethod
    def _join(a: dict, catalog: list[dict], match_radius: float) -> list[tuple]:
        return [(a["sid"], b["aid"]) for b in catalog
                if compute_angular_separation(a["ra"], a["dec"], b["ra0"], b["dec0"]) <= match_radius]

    def exposures(self, table: str, field: int, flags: int, source_set: str) -> list[tuple]:
        seen: dict[tuple, None] = {}
        for r in self._sources(table, flags, source_set):
            if int(r["field"]) == field:
                seen[(r["expid"], r["mjdobs"])] = None
        return list(seen)

    def field_matches(self, table, field, expid, flags, source_set, match_radius,
                      catalog_sets, legacy_catalog) -> list[tuple]:
        catalog = self._catalog(field, catalog_sets, legacy_catalog)
        out: list[tuple] = []
        for a in self._sources(table, flags, source_set):
            if int(a["field"]) == field and a["expid"] == expid:
                out.extend(self._join(a, catalog, match_radius))
        return out

    def field_sources(self, table, field, expid, flags, source_set) -> list[tuple]:
        return [(a["sid"], a["ra"], a["dec"], a["fluxfit"])
                for a in self._sources(table, flags, source_set)
                if int(a["field"]) == field and a["expid"] == expid]

    def neighbour_matches(self, table, field, neighbour, flags, source_set, match_radius,
                          catalog_sets, legacy_catalog, cone) -> list[tuple]:
        catalog = self._catalog(field, catalog_sets, legacy_catalog)
        out: list[tuple] = []
        for a in self._sources(table, flags, source_set):
            if int(a["field"]) != neighbour:
                continue
            if cone is not None and compute_angular_separation(
                    a["ra"], a["dec"], cone[0], cone[1]) > cone[2]:
                continue
            out.extend(self._join(a, catalog, match_radius))
        return out

    # -- writes ----------------------------------------------------------

    def _copy(self, table: str, csv_path: Path, columns: Sequence[str],
              key: Sequence[str]) -> int:
        rows = self.tables[table]
        present = {tuple(r[k] for k in key) for r in rows}
        inserted = 0
        with csv_path.open() as fh:
            for values in csv.reader(fh):
                row: dict[str, Any] = dict(zip(columns, values))
                for name in ("aid", "sid"):
                    if name in row:
                        row[name] = int(row[name])
                for name in ("ra0", "dec0", "flux0"):
                    if name in row:
                        row[name] = _float(row[name])
                identity = tuple(row[k] for k in key)
                if identity in present:
                    continue
                present.add(identity)
                rows.append(row)
                inserted += 1
        return inserted

    def copy_astroobjects(self, field: int, csv_path: Path) -> int:
        return self._copy(_objects.field_table_names(field)["astroobjects"], csv_path,
                          _objects.ASTROOBJECTS_COLUMNS, _ASTRO_KEY)

    def copy_merges(self, field: int, csv_path: Path) -> int:
        return self._copy(_objects.field_table_names(field)["merges"], csv_path,
                          _objects.MERGES_COLUMNS, _MERGES_KEY)

    def cluster_field_object_tables(self, field: int) -> None:
        self.clustered.append(int(field))

    def count_result_set_rows(self, table: str, result_set: str) -> int:
        return sum(1 for r in self.tables.get(table, []) if r.get("result_set") == result_set)

    def register_association_set(self, manifest: dict[str, Any], attempt_id: str) -> None:
        for entry in manifest["outputs"]:
            self.association_sets[entry["instance"]] = {
                "base": entry["key"].get("base"), "run": manifest["run"], "key": entry["key"],
                "row_count": entry.get("row_count"), "complete": True,
                "stage": manifest["stage"], "attempt": manifest["attempt"],
                "registering_attempt": attempt_id,
                "result_sets_read": list(manifest["inputs"]["result_sets"]),
            }
        self.registered.append(manifest)

    def commit(self) -> None:
        self.commits += 1
        if self.on_commit is not None:
            self.on_commit(self)

    def state(self) -> dict[str, Any]:
        return {"tables": self.tables, "made_tables": list(self.made_tables),
                "clustered": list(self.clustered), "locks": list(self.locks),
                "association_sets": self.association_sets, "commits": self.commits}


SEED_ENV = "RAPIDPIPE_FAKE_CROSSMATCH_SEED"
STATE_ENV = "RAPIDPIPE_FAKE_CROSSMATCH_STATE"


def fake_database():
    """The ``RAPIDPIPE_CROSSMATCH_DATABASE`` factory: seeded from, and saved to, JSON files."""
    seed = json.loads(Path(os.environ[SEED_ENV]).read_text())
    db = FakeCrossmatchDatabase(source_sets=seed.get("source_sets"),
                                association_sets=seed.get("association_sets"),
                                tables=seed.get("tables"))
    state_path = Path(os.environ[STATE_ENV])

    def save(database: FakeCrossmatchDatabase) -> None:
        state_path.write_text(json.dumps(database.state(), indent=2, sort_keys=True))

    db.on_commit = save
    return contextlib.nullcontext(db)
