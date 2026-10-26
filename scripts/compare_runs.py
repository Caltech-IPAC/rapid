"""Compare two runs on the epochs both have official difference images for: the scorecard's columns.

    scripts/compare_runs.py --run-a A --run-b B

Outside the pipeline, like ``inspect_run.py`` and ``completeness.py``, whose
functions compute every column (no metric is defined here): it reads
``job_metrics``, the official difference images and packets and, for an
injected dataset, the truth, and writes nothing. The epochs are the
(expid, SCA) both runs have a row in ``diffimages_official`` for; an epoch in
one run only is named on the ``unshared`` line and never compared.

The null table, one row per epoch and run, then each run's median row (the
scorecard's HLWAS null columns): ``n_candidates`` (before the cap),
``n_sources`` (kept), ``n_sources_snr_gt20``, the epoch's official packets,
``core_fraction``, ``ring_fraction``, the registration offset
``hypot(registration_dx_px, registration_dy_px)`` as the gate reads it, and the
difference job's ``container_seconds``; a metric the job did not record is a
dash, as ``job_metrics`` leaves it absent.

Injected is a property of the dataset: ``completeness.py`` finds each run's
truth under its dataset's ``catalog/`` prefix, and when both runs have one, the
injected tables follow, computed by its ``measure`` over the shared epochs:
per epoch the truth rows, detected and false positives (packets on sources
not in the truth), each run's mean and highest false positives per epoch,
and detected per magnitude bin. Exits 2 when one run's dataset has a truth
and the other's has none, naming them.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import statistics
import sys
from pathlib import Path


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inspect_run = _script("inspect_run")
completeness = _script("completeness")

#: The null table's columns: heading and how a difference job's metric ``values`` give it.
NULL_COLUMNS = [
    ("candidates before the cap", lambda v: v.get("n_candidates")),
    ("kept", lambda v: v.get("n_sources")),
    ("S/N > 20", lambda v: v.get("n_sources_snr_gt20")),
    ("packets", lambda v: v.get("packets")),
    ("core", lambda v: v.get("core_fraction")),
    ("ring", lambda v: v.get("ring_fraction")),
    ("registration px", lambda v: (math.hypot(v["registration_dx_px"], v["registration_dy_px"])
                                   if "registration_dx_px" in v and "registration_dy_px" in v else None)),
    ("difference s", lambda v: v.get("container_seconds")),
]
SIGNED = {"core", "ring"}


def shared(a: dict, b: dict) -> tuple[list, list, list]:
    """The epochs of both, of ``a`` only and of ``b`` only, sorted; ``a`` and ``b`` keyed by (expid, SCA)."""
    return sorted(a.keys() & b.keys()), sorted(a.keys() - b.keys()), sorted(b.keys() - a.keys())


def null_row(values: dict) -> list:
    """One epoch's null columns from its difference job's metric ``values`` and its ``packets`` count."""
    return [column(values) for _, column in NULL_COLUMNS]


def _cell(heading: str, value) -> str:
    return "—" if value is None else inspect_run._fmt(value, signed=heading in SIGNED)


def null_table(runs: dict, epochs: list) -> str:
    """The null table: a row per epoch and run, then each run's median over the epochs."""
    heads = [h for h, _ in NULL_COLUMNS]
    lines = ["| epoch | run | " + " | ".join(heads) + " |", "|---|---|" + "---|" * len(heads)]
    for e in epochs:
        for run, rows in runs.items():
            lines.append(f"| {e[0]} sca {e[1]} | {run} | "
                         + " | ".join(_cell(h, v) for h, v in zip(heads, rows[e])) + " |")
    for run, rows in runs.items():
        medians = []
        for i, h in enumerate(heads):
            got = [rows[e][i] for e in epochs if rows[e][i] is not None]
            medians.append(statistics.median(got) if got else None)
        lines.append(f"| median | {run} | " + " | ".join(_cell(h, v) for h, v in zip(heads, medians)) + " |")
    return "\n".join(lines)


def injected_tables(measured: dict) -> str:
    """Per epoch truth, detected and false positives; each run's mean and highest; detected per bin."""
    lines = ["| epoch | run | truth | detected | false positives |", "|---|---|---|---|---|"]
    eps = {run: m["ep"].assign(fp=completeness.false_positives(m["ep"])).set_index(["expid", "sca"])
           for run, m in measured.items()}
    for e in sorted(set().union(*(ep.index for ep in eps.values()))):
        for run, ep in eps.items():
            if e in ep.index:
                r = ep.loc[e]
                lines.append(f"| {e[0]} sca {e[1]} | {run} | "
                             + " | ".join(_cell("", r[c] if r[c] == r[c] else None)
                                          for c in ("truth", "detected", "fp")) + " |")
    for run, m in measured.items():
        rows, fp = m["rows"], eps[run]["fp"]
        lines.append(f"| all | {run} | {len(rows)} | {int(rows['detected'].sum())} |"
                     f" mean {fp.mean():.1f}, highest {fp.max():.0f} |")
    tables = {run: m["table"].set_index("mag_bin") for run, m in measured.items()}
    names = list(tables)
    bins = sorted(set().union(*(t.index for t in tables.values())))
    lines += ["", "| AB bin | " + " | ".join(f"run {n} detected of injected" for n in names) + " |",
              "|---|" + "---|" * len(names)]
    for b in bins:
        cells = [f"{t.loc[b, 'detected']:.0f} of {t.loc[b, 'injected']:.0f}" if b in t.index else "—"
                 for t in tables.values()]
        lines.append(f"| {b:.1f} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--run-a", type=int, required=True)
    p.add_argument("--run-b", type=int, required=True)
    a = p.parse_args(argv)
    from rapidpipe.db.connection import connect

    jobs, data, rows = {}, {}, {}
    with connect(application_name="compare_runs") as conn:
        for run in (a.run_a, a.run_b):
            with conn.cursor() as cur:
                cur.execute("SELECT expid, sca, job FROM diffimages_official WHERE run = %s", (run,))
                jobs[run] = {(e, s): j for e, s, j in cur.fetchall()}
                cur.execute(inspect_run.QUERY, (run,))
                names = [d[0] for d in cur.description]
                rows[run] = [dict(zip(names, r)) for r in cur.fetchall()]
            conn.commit()
            data[run] = completeness.load(conn, run)
    epochs, only_a, only_b = shared(jobs[a.run_a], jobs[a.run_b])
    print(f"runs {a.run_a} ({data[a.run_a]['run']['dataset']}) and {a.run_b} ({data[a.run_b]['run']['dataset']}):"
          f" {len(epochs)} shared epochs")
    print(f"unshared: run {a.run_a} only {only_a or 'none'}; run {a.run_b} only {only_b or 'none'}")
    if not epochs:
        return 0
    table = {}
    for run in (a.run_a, a.run_b):
        diff = inspect_run.by_stage(rows[run]).get("difference", {})
        packets = data[run]["packets"].groupby(["expid", "sca"]).size().to_dict()
        table[run] = {e: null_row({**diff.get(jobs[run][e], {}).get("values", {}),
                                   "packets": packets.get(e, 0)}) for e in epochs}
    print("\n" + null_table(table, epochs))
    files = {run: completeness.truth_files(data[run]["intake"]) for run in (a.run_a, a.run_b)}
    if not any(files.values()):
        return 0
    if not all(files.values()):
        have = [r for r, f in files.items() if f]
        lack = [r for r, f in files.items() if not f]
        print(f"run {have[0]}'s dataset has a truth and run {lack[0]}'s has none: not an injected pair",
              file=sys.stderr)
        return 2
    measured = {run: completeness.measure(data[run], files[run], epochs=set(epochs), with_history=False)
                for run in (a.run_a, a.run_b)}
    print("\n" + injected_tables(measured))
    return 0


if __name__ == "__main__":
    sys.exit(main())
