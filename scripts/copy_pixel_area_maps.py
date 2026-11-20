"""Copy CRDS's pixel area maps, by name, under the prefix a run's [pixel_area_map] store names.

One-time, outside the pipeline, and again after each CRDS delivery of area
references: for each WFI detector, CRDS is asked for its best ``area``
reference at ``--date``; a map not yet under the prefix is fetched
(``<server>/unchecked_get/references/roman/<NAME>``) and written to
``<to>/<NAME>``; and ``<to>/index.json`` gains its detector, useafter date
(the file's own ``meta.useafter``) and name. Entries already in the index
are kept, so a file taken under an earlier map still finds it. The intake
stage reads the index when CRDS does not answer, and the packet stage reads
the maps. Prints one line per detector and a total.

    python scripts/copy_pixel_area_maps.py --to s3://bucket/prefix/pixel-area-maps

TODO(crds-area-keyword): if CRDS fills ``meta.ref_file.area`` in flight L2
files, intake no longer reads ``index.json``; the maps are still copied.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import asdf

from rapidpipe.exitcodes import ArgumentParser
from rapidpipe.products import storage
from rapidpipe.science import crds

INDEX = "index.json"


class Store:
    """The prefix the maps are kept under: an ``s3://`` prefix, or a directory (a path or ``file://``)."""

    def __init__(self, to: str, client=None):
        parsed = urlparse(to)
        self.url = to.rstrip("/")
        if parsed.scheme == "s3":
            self.bucket, self.prefix = parsed.netloc, parsed.path.strip("/")
            self.client = client or storage.s3_client()
            self.directory = None
        else:
            self.directory = Path(parsed.path if parsed.scheme == "file" else to)
            self.directory.mkdir(parents=True, exist_ok=True)

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def has(self, name: str) -> bool:
        if self.directory is not None:
            return (self.directory / name).is_file()
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(name))
            return True
        except Exception as exc:  # noqa: BLE001 - ClientError-shaped
            if not storage.is_not_found(exc):
                raise
            return False

    def read_index(self) -> list[dict]:
        """The index's entries, none when the store has no index yet."""
        if not self.has(INDEX):
            return []
        if self.directory is not None:
            return json.loads((self.directory / INDEX).read_text())["maps"]
        body = self.client.get_object(Bucket=self.bucket, Key=self._key(INDEX))["Body"].read()
        return json.loads(body)["maps"]

    def write(self, path: Path, name: str) -> None:
        if self.directory is not None:
            shutil.copyfile(path, self.directory / name)
        else:
            self.client.upload_file(str(path), self.bucket, self._key(name))


def fetch(server: str, name: str, directory: Path) -> Path:
    """The CRDS reference ``name``, fetched by name into ``directory``."""
    path = directory / name
    url = f"{server.rstrip('/')}/unchecked_get/references/roman/{name}"
    with urllib.request.urlopen(url, timeout=120) as reply, path.open("wb") as stream:
        shutil.copyfileobj(reply, stream, 1 << 20)
    return path


def useafter(path: Path, detector: str) -> str:
    """The map's ``meta.useafter`` as ``YYYY-MM-DDTHH:MM:SS``; ``ValueError`` when it is another detector's."""
    with asdf.open(path, lazy_load=True, memmap=False) as af:
        meta = af["roman"]["meta"]
        if str(meta["instrument"]["detector"]) != detector:
            raise ValueError(f"{path.name} is {meta['instrument']['detector']}'s map, not {detector}'s")
        after = meta["useafter"]
    return str(getattr(after, "isot", after))[:19]


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", required=True,
                        help="the s3:// prefix or directory, the run's [pixel_area_map] store")
    parser.add_argument("--server", default="https://roman-crds.stsci.edu")
    parser.add_argument("--context", default="roman-operational", help="the CRDS context asked under")
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                        help="exposure start the maps are selected for, UTC (default: now)")
    parser.add_argument("--detectors", type=int, nargs="+", default=list(range(1, 19)), metavar="N",
                        help="detector numbers (default: 1 to 18)")
    args = parser.parse_args(argv)
    store, started, copied = Store(args.to), time.monotonic(), 0
    maps = store.read_index()
    with tempfile.TemporaryDirectory(prefix="copy-pixel-area-maps-") as scratch:
        for number in args.detectors:
            detector = f"WFI{number:02d}"
            name = crds.best_reference(args.server, args.context, detector, args.date, "area")
            entry = next((m for m in maps if m["detector"] == detector and m["name"] == name), None)
            if entry is not None and store.has(name):
                print(f"{detector} {name} present {entry['useafter']}", flush=True)
                continue
            path = fetch(args.server, name, Path(scratch))
            entry = {"detector": detector, "useafter": useafter(path, detector), "name": name}
            store.write(path, name)
            path.unlink()
            maps = [m for m in maps if (m["detector"], m["name"]) != (detector, name)] + [entry]
            copied += 1
            print(f"{detector} {name} copied {entry['useafter']}", flush=True)
        index = Path(scratch) / INDEX
        index.write_text(json.dumps(
            {"maps": sorted(maps, key=lambda m: (m["detector"], m["useafter"], m["name"]))}, indent=1) + "\n")
        store.write(index, INDEX)
    print(f"{len(args.detectors)} detectors, {copied} maps copied, {len(maps)} in {store.url}/{INDEX},"
          f" {time.monotonic() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
