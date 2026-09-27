"""One input-binding primitive for both input-set composers.

:func:`bind_input_set` is the only path by which a composed input set is
admitted, bound and written. Its callers are the two composers:
``rapidpipe.cli.runctl.compose_inputs`` (``run inputs``, ``run start``)
and ``rapidpipe.launch.loop.process_date``'s maintain, crossmatch and
alerts sites. What it guarantees, in this fixed order:

- an existing ``<dest>/manifest.json`` is refused (:class:`InputSetExists`)
  before any write, unless ``reuse_existing``;
- admission first: ``repository.add_unit`` refuses a finished, deleting or
  deleted run before anything is composed or copied;
- the id rule is :func:`rapidpipe.runs.inputs.manifest_instances` (output
  entries' instances plus ``inputs.result_sets``), bound through
  :func:`rapidpipe.runs.inputs.bind_registered_inputs` (registered ids
  only; a deleting or deleted producer raises ``InputsRefused``, exit 65);
- the bindings are committed, and only then is the manifest written: a
  manifest on storage means its bindings were committed. A failed write
  leaves committed bindings and no manifest; the next call composes again
  and binds idempotently;
- reuse is a rebind, never a skip: an existing manifest's ids are bound to
  this consumer and committed, and the manifest is never rewritten.

Dependency direction: imports ``rapidpipe.products`` and
``rapidpipe.runs`` only, never ``cli``, ``launch`` or ``stages``.
``repository`` and ``inputs`` are used through their module attributes so
tests that monkeypatch them cover this module too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from rapidpipe.products.manifest import Manifest
from rapidpipe.products.storage import join, parse_location
from rapidpipe.runs import inputs, repository


@dataclass(frozen=True)
class BoundInputSet:
    """The outcome of :func:`bind_input_set`."""

    location: str
    manifest: Manifest
    bound: tuple[str, ...]
    skipped: tuple[str, ...]
    reused: bool


class InputSetExists(Exception):
    """``<dest>/manifest.json`` exists and ``reuse_existing`` is false."""


def bind_input_set(
    conn,
    storage: Any,
    *,
    run_id: str,
    stage: str,
    unit_kind: str,
    unit_id: str,
    dest: str,
    compose: Callable[[], Manifest],
    reuse_existing: bool = True,
) -> BoundInputSet:
    """Admit the consumer unit, compose (or reuse) its input set at
    ``dest``, bind the set's registered instances, commit, then write the
    manifest last.

    ``storage`` is duck-typed: ``exists(location, relative)``,
    ``read_manifest(location_text)``, ``write_manifest(manifest, location)``.
    ``compose`` builds the manifest (copying any members it needs); it is
    called only when no manifest exists, after admission.
    """
    # 1. Where the set lives, and whether it was composed before.
    dest_loc = parse_location(dest)
    existing = storage.exists(dest_loc, "manifest.json")
    # 2. Refuse an overwrite before any write.
    if existing and not reuse_existing:
        raise InputSetExists(f"refusing to overwrite {join(dest_loc, 'manifest.json')}")
    # 3. Admission fence (idempotent): a finished, deleting or deleted run
    #    is refused before anything is read, composed or copied.
    repository.add_unit(conn, run_id, stage, unit_kind, unit_id)
    # 4. Reuse the existing manifest, or compose a new one.
    manifest = storage.read_manifest(dest) if existing else compose()
    # 5. The one id rule.
    ids = inputs.manifest_instances(manifest)
    # 6. Bind the registered subset to this consumer (a rebind on reuse).
    bound, skipped = inputs.bind_registered_inputs(conn, run_id, stage, unit_id, ids)
    # 7. Commit the admission and the bindings.
    conn.commit()
    # 8. The manifest is the last write: one on storage means its bindings
    #    were committed. An existing manifest is never rewritten.
    if not existing:
        storage.write_manifest(manifest, dest_loc)
    # 9. Report.
    return BoundInputSet(dest, manifest, tuple(bound), tuple(skipped), existing)
