"""Stand-ins for the load stage's boundaries: synthetic catalogs, an input set, a fake database.

- :func:`write_photutils_catalogs` writes a Photutils PSF-fit catalog and
  its finder catalog with the columns `dev`'s ``write_psf_catalog_products``
  writes (``astropy.io.ascii.write``, basic format), from row dicts.
- :func:`build_load_input_set` writes a difference attempt's output
  location as the load stage reads it: a completion manifest (stage
  ``difference``) naming one ZOGY ``difference-image`` instance and its
  ``source-catalog`` entries -- SExtractor and Photutils, both signs -- with
  member files whose sizes and SHA-256 match.
- :class:`FakeLoadDatabase` has :class:`rapidpipe.stages.load.PostgresLoadDatabase`'s
  methods over in-memory state: seeded `diffimages`/`l2files` rows, the
  child tables it made, the rows COPY loaded, the source sets registered.
  :func:`fake_database` is the factory ``RAPIDPIPE_LOAD_DATABASE`` names for
  a stage run as a subprocess (``make stage-load``): it reads its seed from
  the JSON file ``RAPIDPIPE_FAKE_LOAD_SEED`` names and writes its state, on
  commit, to the JSON file ``RAPIDPIPE_FAKE_LOAD_STATE`` names.

Packaged under ``rapidpipe.selftest.support`` (not ``tests/``, which the
pipeline image excludes at build time -- ``containers/rapid-pipeline/
build.sh``) so ``rapidpipe selftest --stage load`` can import it inside
the image. ``tests/unit/fakeloaddb.py`` re-exports this module for the
test suite.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import ascii
from astropy.table import Table

from rapidpipe.db import sources as _sources

#: `dev`'s Photutils column names (loadPSFCatIntoDBSourcesTable.py L570-571).
MAIN_COLUMNS = ("id", "group_id", "group_size", "local_bkg", "x_init", "y_init",
                "flux_init", "x_fit", "y_fit", "flux_fit", "x_err", "y_err",
                "flux_err", "n_pixels_fit", "qfit", "cfit", "reduced_chi2", "flags",
                "ra", "dec")
FINDER_COLUMNS = ("id", "x_centroid", "y_centroid", "sharpness", "roundness1",
                  "roundness2", "n_pixels", "peak", "flux", "mag", "daofind_mag")

_INT_COLUMNS = {"id", "group_id", "group_size", "n_pixels_fit", "flags", "n_pixels"}

DIFFERENCE_INSTANCE = "01J8Y6QZ3M0000000000000D1F"
L2_INSTANCE = "01J8Y6QZ3M00000000000000L2"
REFERENCE_INSTANCE = "01J8Y6QZ3M0000000000000REF"
UNIT_ID = "e20260821001234/SCA07"


def main_row(id_: int, x_fit: float, y_fit: float, ra: float, dec: float, **overrides) -> dict:
    """A PSF-fit catalog row; every column not given is a distinct, recognisable value."""
    row = {"id": id_, "group_id": id_, "group_size": 1, "local_bkg": 0.25,
           "x_init": x_fit, "y_init": y_fit, "flux_init": 100.0 + id_,
           "x_fit": x_fit, "y_fit": y_fit, "flux_fit": 101.5 + id_,
           "x_err": 0.0125, "y_err": 0.025, "flux_err": 1.5 + id_ / 10,
           "n_pixels_fit": 25, "qfit": 0.0625, "cfit": 0.03125, "reduced_chi2": 1.25,
           "flags": 0, "ra": ra, "dec": dec}
    row.update(overrides)
    return row


def finder_row(id_: int, **overrides) -> dict:
    row = {"id": id_, "x_centroid": 10.0, "y_centroid": 10.0, "sharpness": 0.5,
           "roundness1": 0.015625, "roundness2": -0.03125, "n_pixels": 9,
           "peak": 12.5 + id_, "flux": 50.0, "mag": -4.25, "daofind_mag": -1.75}
    row.update(overrides)
    return row


def _table(rows: list[dict], columns: tuple[str, ...]) -> Table:
    data = {}
    for name in columns:
        values = [r[name] for r in rows]
        data[name] = np.array(values, dtype=np.int64 if name in _INT_COLUMNS else np.float64)
    return Table(data, names=columns)


def write_photutils_catalogs(catalog: Path, finder: Path, main_rows: list[dict],
                             finder_rows: list[dict]) -> None:
    """Write the two catalogs as `dev`'s ``ascii.write`` does (basic format)."""
    ascii.write(_table(main_rows, MAIN_COLUMNS), catalog, overwrite=True)
    ascii.write(_table(finder_rows, FINDER_COLUMNS), finder, overwrite=True)


def _member(role: str, path: Path, root: Path) -> dict:
    data = path.read_bytes()
    return {"role": role, "path": str(path.relative_to(root)), "bytes": len(data),
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}


def build_load_input_set(inputs: Path, catalogs: dict[str, tuple[list[dict], list[dict]]],
                         *, differencer: str = "zogy",
                         difference_instance: str = DIFFERENCE_INSTANCE) -> Path:
    """Write a difference attempt's output location; return the manifest path.

    ``catalogs`` maps ``positive``/``negative`` to (main rows, finder rows);
    a sign left out has no Photutils entry, as when `dev`'s catalog was not
    produced.
    """
    work = inputs / "work"
    work.mkdir(parents=True, exist_ok=True)
    diff = work / "diffimage_masked.fits"
    diff.write_bytes(b"SIMPLE  =                    T" + b" " * 50)
    outputs = [{
        "kind": "difference-image", "format_version": "1", "instance": difference_instance,
        "key": {"l2": L2_INSTANCE, "reference": REFERENCE_INSTANCE,
                "differencer": differencer, "settings_hash": "sha256:" + "0" * 64},
        "primary": f"work/{diff.name}", "members": [_member("difference", diff, inputs)],
        "registration": {},
    }]
    for i, sign in enumerate(("positive", "negative")):
        sex = work / f"diffimage_masked_{sign}.txt"
        sex.write_text("# SExtractor catalog: never loaded\n")
        outputs.append({
            "kind": "source-catalog", "format_version": "1",
            "instance": f"01J8Y6QZ3M00000000000SEX{i}{i}",
            "key": {"difference": difference_instance, "catalog_type": "sextractor", "sign": sign},
            "primary": f"work/{sex.name}", "members": [_member("catalog", sex, inputs)],
            "registration": {"source_count": 0}})
        if sign not in catalogs:
            continue
        main_rows, finder_rows = catalogs[sign]
        suffix = "" if sign == "positive" else "_negative"
        catalog = work / f"{differencer}_diffimage_masked_psfcat{suffix}.txt"
        finder = work / f"{differencer}_diffimage_masked_psfcat_finder{suffix}.txt"
        write_photutils_catalogs(catalog, finder, main_rows, finder_rows)
        outputs.append({
            "kind": "source-catalog", "format_version": "1",
            "instance": f"01J8Y6QZ3M00000000000PUC{i}{i}",
            "key": {"difference": difference_instance, "catalog_type": "photutils", "sign": sign},
            "primary": f"work/{catalog.name}",
            "members": [_member("catalog", catalog, inputs), _member("finder", finder, inputs)],
            "registration": {"source_count": len(main_rows)}})
    manifest = {
        "schema_version": "1", "run": "01J8Y6QZ3M00000000000000RN",
        "unit": {"kind": "detector-image", "id": UNIT_ID},
        "stage": "difference", "attempt": "01J8Y6QZ3M00000000000000AT",
        "execution_record": "exec/01J8Y6QZ3M00000000000000AT.json",
        "inputs": {"manifest": "inputs/manifest.json", "products": {}, "result_sets": []},
        "outputs": outputs,
    }
    path = inputs / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


def difference_row(**overrides) -> dict[str, Any]:
    """A seeded `diffimages`+`l2files` row, as ``difference_image_row`` returns one."""
    row = {"pid": 4242, "rid": 99, "expid": 1234, "sca": 7, "fid": 3, "field": 5321,
           "hp6": 0, "hp9": 0, "mjdobs": 61273.125, "dateobs": "2026-08-21 03:00:00"}
    row.update(overrides)
    return row


class FakeLoadDatabase:
    """In-memory :class:`~rapidpipe.stages.load.PostgresLoadDatabase`."""

    def __init__(self, differences: dict[str, dict] | None = None,
                 source_sets: dict[str, dict] | None = None) -> None:
        self.differences = dict(differences or {})
        self.source_sets = dict(source_sets or {})   # instance -> {run, key, row_count, ...}
        self.tables: dict[str, list[dict]] = {}
        self.made_tables: list[str] = []
        self.clustered: list[str] = []
        self.commits = 0
        self._pending: dict[str, Any] | None = None
        self.on_commit = None

    def difference_image_row(self, instance: str) -> dict:
        if instance not in self.differences:
            raise ValueError(f"no diffimages row for difference instance {instance!r}")
        return dict(self.differences[instance])

    def find_complete_source_set(self, run_id, key):
        for instance, s in sorted(self.source_sets.items()):
            if s["run"] == run_id and s["key"] == key and s["complete"]:
                return instance, s["row_count"]
        return None

    def ensure_child_table(self, obs_date, sca) -> bool:
        table = _sources.child_table_name(obs_date, sca)
        if table in self.tables:
            return False
        self.tables[table] = []
        self.made_tables.append(table)
        return True

    def register_source_set(self, manifest, attempt_id) -> None:
        (entry,) = manifest["outputs"]
        self.source_sets[entry["instance"]] = {
            "run": manifest["run"], "stage": manifest["stage"], "attempt": attempt_id,
            "kind": entry["kind"], "key": entry["key"], "row_count": entry["row_count"],
            "complete": True, "products": dict(manifest["inputs"]["products"])}

    def copy_sources(self, table: str, csv_path: Path) -> None:
        with Path(csv_path).open() as fh:
            for values in csv.reader(fh):
                if len(values) != len(_sources.COLUMNS):
                    raise ValueError(f"COPY row has {len(values)} columns, "
                                     f"expected {len(_sources.COLUMNS)}")
                self.tables[table].append(dict(zip(_sources.COLUMNS, values)))

    def count_result_set_rows(self, table: str, result_set: str) -> int:
        return sum(1 for r in self.tables[table] if r["result_set"] == result_set)

    def cluster_and_analyze(self, obs_date, sca) -> None:
        self.clustered.append(_sources.child_table_name(obs_date, sca))

    def commit(self) -> None:
        self.commits += 1
        if self.on_commit is not None:
            self.on_commit(self)

    def state(self) -> dict[str, Any]:
        return {"tables": copy.deepcopy(self.tables), "made_tables": list(self.made_tables),
                "source_sets": copy.deepcopy(self.source_sets),
                "clustered": list(self.clustered), "commits": self.commits}


SEED_ENV = "RAPIDPIPE_FAKE_LOAD_SEED"
STATE_ENV = "RAPIDPIPE_FAKE_LOAD_STATE"


def fake_database():
    """The ``RAPIDPIPE_LOAD_DATABASE`` factory: seeded from, and saved to, JSON files."""
    seed = json.loads(Path(os.environ[SEED_ENV]).read_text())
    db = FakeLoadDatabase(differences=seed.get("differences"),
                          source_sets=seed.get("source_sets"))
    state_path = Path(os.environ[STATE_ENV])

    def save(database: FakeLoadDatabase) -> None:
        state_path.write_text(json.dumps(database.state(), indent=2, sort_keys=True))

    db.on_commit = save
    return contextlib.nullcontext(db)
