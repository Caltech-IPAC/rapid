"""
Print the implementation status of every alert schema field.

Usage:
    python -m rapid_alerts.report            # full field-by-field listing
    python -m rapid_alerts.report --summary  # per-record counts only
"""

import argparse
import sys

from .fields import RECORDS, Status

MARK = {Status.IMPLEMENTED: "x", Status.STUB: " ", Status.NOT_USED: "-"}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Show implemented vs stub alert schema fields")
    parser.add_argument("--summary", action="store_true",
                        help="per-record counts only")
    args = parser.parse_args(argv)

    total_impl = total_stub = 0
    for record in RECORDS:
        counts = {s: sum(1 for f in record.fields if f.status is s)
                  for s in Status}
        total_impl += counts[Status.IMPLEMENTED]
        total_stub += counts[Status.STUB]

        line = (f"{record.name}: {len(record.fields)} fields -- "
                f"{counts[Status.IMPLEMENTED]} implemented, "
                f"{counts[Status.STUB]} stub")
        if counts[Status.NOT_USED]:
            line += f", {counts[Status.NOT_USED]} not used"
        print(f"\n{line}")

        if not args.summary:
            print("-" * len(line))
            for f in record.fields:
                print(f"  [{MARK[f.status]}] {f.name:<26} {f.source or ''}")

    print(f"\nTotal: {total_impl} implemented, {total_stub} stub "
          f"([x] = implemented, [ ] = stub, [-] = not used)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
