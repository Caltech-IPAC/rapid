"""Stand-ins for the alerts stage's boundaries: a difference image, an input set, a fake database.

- :func:`write_difference_image` writes a small float32 difference image
  with a TAN WCS (a Gaussian bump at each given pixel position, so stamps
  have structure), and :func:`pixel_to_sky` evaluates that WCS.
- :func:`write_reference_catalog` writes a SExtractor ``ASCII_HEAD`` catalog
  with the columns `dev`'s ``load_refcat`` reads.
- :func:`build_alerts_input_set` writes an input set as the alerts stage
  reads it: a manifest of stage ``input-set`` naming one ZOGY
  ``difference-image`` instance (member ``difference``), optionally one
  ``reference-catalog`` instance, and the three result sets.
- :class:`FakeAlertsDatabase` has :class:`rapidpipe.stages.alerts.PostgresAlertsDatabase`'s
  methods over in-memory seeded rows -- `product_instances` for the sets,
  the difference instance's `diffimages` pid, `sources`, `filters`,
  `exposures`, and the parent tables `merges`, `astroobjects` and
  `astroobjectsmeta` each row carrying its `result_set` -- and records what
  the stage wrote: outbox rows, registered manifests, `nalertpackets`
  updates, commits. :func:`fake_database` is the factory
  ``RAPIDPIPE_ALERTS_DATABASE`` names for a stage run as a subprocess: it
  reads its seed from the JSON file ``RAPIDPIPE_FAKE_ALERTS_SEED`` names,
  restores what earlier invocations committed from the JSON file
  ``RAPIDPIPE_FAKE_ALERTS_STATE`` names, and writes its state there on commit.

Packaged under ``rapidpipe.selftest.support`` (not ``tests/``, which the
pipeline image excludes) so ``rapidpipe selftest --stage alerts`` can import
it inside the image.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

from rapidpipe.db.alerts import OUTBOX_COLUMNS, SOURCE_COLUMNS

DIFFERENCE_INSTANCE = "01J8Y6QZ3M0000000000000D1F"
L2_INSTANCE = "01J8Y6QZ3M00000000000000L2"
REFERENCE_INSTANCE = "01J8Y6QZ3M0000000000000REF"
REFERENCE_CATALOG_INSTANCE = "01J8Y6QZ3M00000000000RCAT1"
SOURCE_SET = "01J8Y6QZ3M00000000000SRCS1"
ASSOCIATION_SET = "01J8Y6QZ3M00000000000ASSC1"
STATISTICS_SET = "01J8Y6QZ3M00000000000STAT1"
UNIT_ID = "e20260821001234/SCA07"

REFCAT_HEADER = """\
#   1 NUMBER                 Running object number
#   2 ALPHAWIN_J2000         Windowed right ascension (J2000)                           [deg]
#   3 DELTAWIN_J2000         windowed declination (J2000)                               [deg]
#   4 FLAGS                  Extraction flags
#   5 CLASS_STAR             S/G classifier output
#   6 MAG_AUTO               Kron-like elliptical aperture magnitude                    [mag]
#   7 MAGERR_AUTO            RMS error for AUTO magnitude                               [mag]
#   8 ELONGATION             A_IMAGE/B_IMAGE
#   9 FWHM_IMAGE             FWHM assuming a gaussian core                              [pixel]
#  10 FLUX_RADIUS            Fraction-of-light radii                                    [pixel]
#  12 KRON_RADIUS            Kron apertures in units of A or B
"""


def image_wcs(naxis: int, crval: Sequence[float], pixel_scale_arcsec: float) -> WCS:
    """A TAN WCS centred on the image, north up, east left."""
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crval = list(crval)
    wcs.wcs.crpix = [(naxis + 1) / 2.0, (naxis + 1) / 2.0]
    scale = pixel_scale_arcsec / 3600.0
    wcs.wcs.cd = [[-scale, 0.0], [0.0, scale]]
    return wcs


def pixel_to_sky(wcs: WCS, x0: float, y0: float) -> tuple[float, float]:
    """RA, Dec of a 0-based pixel position (photutils' ``xfit``/``yfit`` convention)."""
    ra, dec = wcs.all_pix2world([[x0, y0]], 0)[0]
    return float(ra) % 360.0, float(dec)


def write_difference_image(path: Path, naxis: int, crval: Sequence[float],
                           pixel_scale_arcsec: float,
                           bumps: Sequence[tuple[float, float]]) -> WCS:
    """A float32 ``naxis`` x ``naxis`` image with a Gaussian bump at each 0-based (x, y)."""
    wcs = image_wcs(naxis, crval, pixel_scale_arcsec)
    yy, xx = np.mgrid[0:naxis, 0:naxis]
    data = np.zeros((naxis, naxis), dtype=np.float64) + 0.01 * (xx + yy)
    for x0, y0 in bumps:
        data += 100.0 * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * 2.0 ** 2))
    header = wcs.to_header()
    header["BUNIT"] = "DN"
    header["FILTER"] = "F184"
    header["MJD-OBS"] = 61273.125
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data=data.astype(np.float32), header=header).writeto(path, overwrite=True)
    return wcs


def write_reference_catalog(path: Path, rows: Sequence[Sequence[Any]]) -> None:
    """A SExtractor catalog; each row is NUMBER, RA, Dec, FLAGS, CLASS_STAR, MAG_AUTO,
    MAGERR_AUTO, ELONGATION, FWHM_IMAGE, FLUX_RADIUS (two values), KRON_RADIUS."""
    lines = [" ".join(str(v) for v in row) for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(REFCAT_HEADER + "\n".join(lines) + "\n")


def _member(role: str, path: Path, root: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"role": role, "path": str(path.relative_to(root)), "bytes": len(data),
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}


def build_alerts_input_set(inputs: Path, *, difference: Path, reference_catalog: Path | None,
                           result_sets: Sequence[str], differencer: str = "zogy",
                           difference_instance: str = DIFFERENCE_INSTANCE) -> Path:
    """Write the input-set manifest over files already under ``inputs``; return its path."""
    outputs = [{
        "kind": "difference-image", "format_version": "1", "instance": difference_instance,
        "key": {"l2": L2_INSTANCE, "reference": REFERENCE_INSTANCE,
                "differencer": differencer, "settings_hash": "sha256:" + "0" * 64},
        "primary": str(difference.relative_to(inputs)),
        "members": [_member("difference", difference, inputs)],
        "registration": {},
    }]
    if reference_catalog is not None:
        outputs.append({
            "kind": "reference-catalog", "format_version": "1",
            "instance": REFERENCE_CATALOG_INSTANCE,
            "key": {"reference": REFERENCE_INSTANCE, "catalog_type": "sextractor"},
            "primary": str(reference_catalog.relative_to(inputs)),
            "members": [_member("catalog", reference_catalog, inputs)],
            "registration": {},
        })
    manifest = {
        "schema_version": "1", "run": "01J8Y6QZ3M00000000000000RN",
        "unit": {"kind": "detector-image", "id": UNIT_ID},
        "stage": "input-set", "attempt": "01J8Y6QZ3M00000000000000AT",
        "execution_record": "exec/01J8Y6QZ3M00000000000000AT.json",
        "inputs": {"manifest": "inputs/manifest.json", "products": {},
                   "result_sets": list(result_sets)},
        "outputs": outputs,
    }
    path = inputs / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


def source_row(sid: int, x: float, y: float, ra: float, dec: float, *, result_set: str,
               pid: int, mjdobs: float, **overrides) -> dict[str, Any]:
    """A `sources` row with every column the stage reads; the rest distinct and recognisable."""
    row = {"sid": sid, "expid": 1234, "sca": 7, "mjdobs": mjdobs, "ra": ra, "dec": dec,
           "xfit": x, "yfit": y, "xerr": 0.0125, "yerr": 0.025, "fluxfit": 101.5 + sid % 100,
           "fluxerr": 1.5, "flags": 0, "field": 5321, "hp6": 28866, "hp9": 1847448,
           "pid": pid, "isdiffpos": True, "qfit": 0.0625, "cfit": 0.03125, "redchi": 1.25,
           "npixfit": 25, "sharpness": 0.5, "roundness1": 0.015625, "roundness2": -0.03125,
           "peak": 12.5, "fid": 3, "result_set": result_set}
    row.update(overrides)
    return row


class FakeAlertsDatabase:
    """In-memory :class:`~rapidpipe.stages.alerts.PostgresAlertsDatabase`."""

    def __init__(self, seed: dict[str, Any]) -> None:
        self.product_instances: dict[str, dict] = dict(seed.get("product_instances", {}))
        self.diffimages: dict[str, dict] = dict(seed.get("diffimages", {}))
        self.sources: list[dict] = list(seed.get("sources", []))
        self.filters: dict[int, str] = {int(k): v for k, v in seed.get("filters", {}).items()}
        self.exposures: dict[int, float] = {int(k): v for k, v in seed.get("exposures", {}).items()}
        self.merges: list[dict] = list(seed.get("merges", []))
        self.astroobjects: list[dict] = list(seed.get("astroobjects", []))
        self.astroobjectsmeta: list[dict] = list(seed.get("astroobjectsmeta", []))
        self.members: dict[str, list[dict]] = dict(seed.get("members", {}))
        self.outbox: list[dict] = list(seed.get("outbox", []))
        self.registered: list[dict] = []
        self.nalertpackets: list[dict] = []
        self.commits = 0
        self.on_commit = None

    # -- reads -----------------------------------------------------------

    def attempt_outputs(self, attempt: str) -> dict[str, str]:
        return {p["kind"]: i for i, p in sorted(self.product_instances.items())
                if p.get("producing_attempt") == attempt
                and p["kind"] in ("alert-container", "alert-set")}

    def product_members(self, instance: str) -> list[dict]:
        return sorted(self.members.get(instance, []), key=lambda m: m["role"])

    def outbox_rows(self, instance: str) -> list[dict]:
        return sorted((r for r in self.outbox if r["instance"] == instance),
                      key=lambda r: r["record_ordinal"])

    def result_set_kinds(self, instances: list[str], run_id: str | None = None) -> dict[str, dict]:
        return {i: {"kind": p["kind"], "complete": p.get("complete"), "key": p.get("key", {})}
                for i, p in self.product_instances.items() if i in instances}

    def difference_pid(self, instance: str) -> int:
        if instance not in self.diffimages:
            raise ValueError(f"no diffimages row for difference instance {instance!r}")
        return int(self.diffimages[instance]["pid"])

    def flagged_sources(self, source_set: str, pid: int) -> list[tuple[int, int]]:
        return sorted((s["sid"], s["flags"]) for s in self.sources
                      if s["result_set"] == source_set and s["pid"] == pid and s["flags"] != 0)

    def _joined(self, s: dict) -> dict:
        row = {c: s[c] for c in SOURCE_COLUMNS}
        row["band"] = self.filters[s["fid"]]
        row["exptime"] = self.exposures[s["expid"]]
        return row

    def alertable_sources(self, source_set: str, pid: int) -> list[dict]:
        return [self._joined(s) for s in sorted(self.sources, key=lambda s: s["sid"])
                if s["result_set"] == source_set and s["pid"] == pid and s["flags"] == 0]

    def association_chain(self, instance: str, run_id: str | None = None) -> list[str]:
        """The set and the bases it extends (``key.base``), newest first."""
        chain: list[str] = []
        current = instance
        while current is not None:
            row = self.product_instances.get(current)
            if current in chain or row is None or row["kind"] != "association-set":
                raise ValueError(f"association chain of {instance!r} is broken at {current!r}")
            chain.append(current)
            current = (row.get("key") or {}).get("base")
        return chain

    def registered_instances(self, instances) -> set[str]:
        return {i for i in instances if i in self.product_instances}

    def associations(self, lineages, fields, statistics_by_association, sids) -> list[dict]:
        """As PostgreSQL does: each named set's field tables only (every seeded
        merges/astroobjects/astroobjectsmeta row names its ``field``)."""
        rows = []
        for root, chain in lineages.items():
            field = fields[root]
            stats_set = statistics_by_association.get(root)
            depth = {member: d for d, member in enumerate(chain)}
            objects: dict[int, dict] = {}
            for o in sorted((o for o in self.astroobjects
                             if o["field"] == field and o["result_set"] in depth),
                            key=lambda o: -depth[o["result_set"]]):
                objects[o["aid"]] = o          # the newest set, written last, wins
            meta = {m["aid"]: m for m in self.astroobjectsmeta
                    if stats_set is not None and m["field"] == field
                    and m["result_set"] == stats_set}
            merges = [m for m in self.merges if m["field"] == field and m["result_set"] in depth]
            seen = set()
            for m in merges:
                if m["sid"] not in sids or (m["sid"], m["aid"]) in seen:
                    continue
                seen.add((m["sid"], m["aid"]))
                obj = objects.get(m["aid"])
                stats = meta.get(m["aid"]) if obj else None
                count = len({m2["sid"] for m2 in merges if obj and m2["aid"] == obj["aid"]})
                rows.append({
                    "sid": m["sid"], "merges_aid": m["aid"], "association_set": root,
                    "aid": obj["aid"] if obj else None,
                    "ra0": obj["ra0"] if obj else None, "dec0": obj["dec0"] if obj else None,
                    "stdevra": stats["stdevra"] if stats else None,
                    "stdevdec": stats["stdevdec"] if stats else None,
                    "nsources": (stats["nsources"] if stats else count) if obj else None,
                })
        return sorted(rows, key=lambda r: (r["sid"], r["merges_aid"], r["association_set"]))

    def history(self, lineages, fields, objects, min_mjd) -> list[dict]:
        by_sid = {s["sid"]: s for s in self.sources}
        rows = {}
        for root, aid in objects:
            members = set(lineages[root])
            for m in self.merges:
                if (m["field"] != fields[root] or m["result_set"] not in members
                        or m["aid"] != aid):
                    continue
                s = by_sid.get(m["sid"])
                if s is None or s["mjdobs"] < min_mjd:
                    continue
                rows[(root, aid, s["sid"])] = {"object_set": root, "object_aid": aid,
                                               **self._joined(s)}
        return sorted(rows.values(), key=lambda r: (r["mjdobs"], r["sid"]))

    # -- writes ----------------------------------------------------------

    def register_outputs(self, manifest: dict, attempt_id: str) -> None:
        self.registered.append({"manifest": copy.deepcopy(manifest), "attempt": attempt_id})
        for entry in manifest["outputs"]:
            self.product_instances[entry["instance"]] = {
                "kind": entry["kind"], "key": entry["key"], "producing_attempt": attempt_id,
                "complete": True if not entry["members"] else None,
                "row_count": entry.get("row_count")}
            self.members[entry["instance"]] = [dict(m) for m in entry["members"]]

    def insert_outbox_rows(self, rows) -> None:
        for row in rows:
            if set(row) != set(OUTBOX_COLUMNS):
                raise ValueError(f"outbox row columns {sorted(row)} != {sorted(OUTBOX_COLUMNS)}")
            for key in ("candidate", "record_ordinal"):
                if any((r["instance"], r[key]) == (row["instance"], row[key]) for r in self.outbox):
                    raise ValueError(f"duplicate (instance, {key}) in alert_outbox")
            self.outbox.append(dict(row))

    def set_nalertpackets(self, instance: str, run: str) -> int:
        self.nalertpackets.append({"instance": instance, "run": run, "value": 1})
        return 1 if instance in self.diffimages else 0

    def commit(self) -> None:
        self.commits += 1
        if self.on_commit is not None:
            self.on_commit(self)

    def state(self) -> dict[str, Any]:
        return {"outbox": copy.deepcopy(self.outbox), "registered": copy.deepcopy(self.registered),
                "nalertpackets": list(self.nalertpackets), "commits": self.commits,
                "product_instances": copy.deepcopy(self.product_instances),
                "members": copy.deepcopy(self.members)}


SEED_ENV = "RAPIDPIPE_FAKE_ALERTS_SEED"
STATE_ENV = "RAPIDPIPE_FAKE_ALERTS_STATE"


#: What a commit made, restored from the state file when one exists, so a
#: second invocation (a rerun of the same attempt) sees the first's rows.
_COMMITTED = ("product_instances", "members", "outbox", "registered", "nalertpackets", "commits")


def fake_database():
    """The ``RAPIDPIPE_ALERTS_DATABASE`` factory: seeded from, and saved to, JSON files.

    When the state file already exists, what earlier invocations committed
    is restored from it on top of the seed.
    """
    db = FakeAlertsDatabase(json.loads(Path(os.environ[SEED_ENV]).read_text()))
    state_path = Path(os.environ[STATE_ENV])
    if state_path.exists():
        state = json.loads(state_path.read_text())
        for name in _COMMITTED:
            setattr(db, name, state[name])

    def save(database: FakeAlertsDatabase) -> None:
        state_path.write_text(json.dumps(database.state(), indent=2, sort_keys=True))

    db.on_commit = save
    return contextlib.nullcontext(db)
