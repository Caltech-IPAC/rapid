"""Copy CRDS ePSF references, by name, under the prefix the psf stage reads first.

One-time, outside the pipeline: each NAME is fetched from the CRDS server
(``<server>/unchecked_get/references/roman/<NAME>``, as the psf stage
fetches it) and uploaded to ``<to>/<NAME>``, the ``[psf] epsf_cache`` a run
names by ``--set``. A name already under the prefix is skipped, so a rerun
copies only what is missing. Prints one line per name and a total.

    python scripts/copy_epsf.py --to s3://bucket/prefix/epsf NAME [NAME ...]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rapidpipe.products import storage


def copy(name: str, server: str, bucket: str, prefix: str, client) -> tuple[str, str, int]:
    key = f"{prefix}/{name}"
    try:
        size = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
        return name, "present", size
    except Exception as exc:  # noqa: BLE001 - ClientError-shaped
        if not storage.is_not_found(exc):
            raise
    url = f"{server.rstrip('/')}/unchecked_get/references/roman/{name}"
    with tempfile.TemporaryDirectory(prefix="copy-epsf-") as scratch:
        path = Path(scratch) / name
        with urllib.request.urlopen(url, timeout=120) as reply, path.open("wb") as stream:
            shutil.copyfileobj(reply, stream, 1 << 20)
        client.upload_file(str(path), bucket, key)
        return name, "copied", path.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", required=True, help="the s3:// prefix, e.g. the run's [psf] epsf_cache")
    parser.add_argument("--server", default="https://roman-crds.stsci.edu")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("names", nargs="+", metavar="NAME")
    args = parser.parse_args(argv)
    bucket, _, prefix = args.to.removeprefix("s3://").partition("/")
    client, started, total = storage.s3_client(), time.monotonic(), 0
    with ThreadPoolExecutor(args.workers) as pool:
        for name, what, size in pool.map(lambda n: copy(n, args.server, bucket, prefix.rstrip("/"), client),
                                         args.names):
            total += size
            print(f"{name} {what} {size}", flush=True)
    print(f"{len(args.names)} references, {total / 1e9:.1f} GB under {args.to},"
          f" {time.monotonic() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
