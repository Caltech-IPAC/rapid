#!/usr/bin/env python3
"""Plot a timing summary of a forced-photometry benchmark JSONL file
(the measure tier of benchmark_forced_phot.py).

Produces a two-panel figure:

  1. Wall-clock composition by phase for each fp_run record (stacked
     horizontal bars). Phases are folded into three groups: input-image
     staging (fixed per run), photometry proper (the cforcepsfaper C
     module plus its input/output handling), and lightcurve writing --
     which includes the per-position x per-epoch reference-catalog
     matching that dominates the wall clock.

  2. Projected wall time versus batch size: the linear fit measured
     across runs (wall = fixed + marginal * n_positions), against a
     projection where reference catalogs are parsed once per run
     instead of once per position per epoch.

Usage (from the alerts/ package root):

    python3.11 test/plot_benchmark_forced_phot.py fp.jsonl -o fp_timing.png
"""

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

logger = logging.getLogger(__name__)

# Phase keys written by pipeline/forcedPhotometryForField.py, folded into
# the three groups the architecture decision cares about.
PHASE_GROUPS = {
    "setup": ["to determine input difference images"],
    "photometry": [
        "after generating cforcepsfaper-module inputs",
        "after executing cforcerpsfaper C module",
        "to load output from cforcerpsfaper C module into memory",
    ],
    "write": ["to write final lightcurve files"],
}

# Validated categorical palette (dataviz reference instance, light mode).
COLORS = {"setup": "#2a78d6", "photometry": "#1baf7a", "write": "#eb6834"}
LABELS = {
    "setup": "Determine + stage input images",
    "photometry": "Photometry (C module)",
    "write": "Write lightcurves + catalog matching",
}
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"

# Assumed per-position cost of writing the lightcurve text file itself
# once catalog parsing is cached (the projection's only non-measured
# input; the text write is not separately timed by the FP backend).
CACHED_TEXT_WRITE_S = 0.2


def load_benchmark(jsonl_path):
    """Return (meta, runs, summary) from a benchmark JSONL file."""
    meta, runs, summary = None, [], None
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            kind = rec.get("kind")
            if kind == "meta":
                meta = rec
            elif kind == "fp_run":
                runs.append(rec)
            elif kind == "summary":
                summary = rec
    if not runs:
        raise ValueError(f"no fp_run records in {jsonl_path}; "
                         "was the benchmark run with --run?")
    return meta, runs, summary


def fold_phases(run):
    """Fold a run's phases_s into the three plot groups (seconds)."""
    phases = run.get("phases_s", {})
    return {group: sum(phases.get(k, 0.0) for k in keys)
            for group, keys in PHASE_GROUPS.items()}


def linear_fit(runs, summary):
    """(fixed_s, per_position_s) for wall time vs batch size.

    Prefers the fit already recorded in the summary; falls back to a
    least-squares fit over the fp_run records.
    """
    if summary and "fit_fixed_s" in summary and "fit_per_position_s" in summary:
        return summary["fit_fixed_s"], summary["fit_per_position_s"]
    n = np.array([r["n_positions"] for r in runs], dtype=float)
    wall = np.array([r["wall_s"] for r in runs], dtype=float)
    if len(runs) < 2:
        return 0.0, wall[0] / n[0]
    slope, intercept = np.polyfit(n, wall, 1)
    return intercept, slope


def cached_projection_slope(runs):
    """Estimated per-position cost with per-run catalog caching.

    Everything outside the write phase already scales weakly with batch
    size; measure its marginal directly and add the assumed text-write
    cost that the write phase would retain.
    """
    n = np.array([r["n_positions"] for r in runs], dtype=float)
    non_write = np.array([r["wall_s"] - fold_phases(r)["write"] for r in runs])
    if len(runs) >= 2 and len(set(n)) > 1:
        slope = np.polyfit(n, non_write, 1)[0]
    else:
        slope = 0.0
    return max(slope, 0.0) + CACHED_TEXT_WRITE_S


def _fmt_s(seconds):
    if seconds >= 7200:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 100:
        return f"{seconds:,.0f} s"
    return f"{seconds:.1f} s"


def _style_axis(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelsize=9)


def plot_timing_summary(jsonl_path, out_path, max_positions=100):
    """Render the two-panel timing summary figure and save it."""
    meta, runs, summary = load_benchmark(jsonl_path)
    runs = sorted(runs, key=lambda r: r["n_positions"])
    fixed_s, per_pos_s = linear_fit(runs, summary)
    cached_slope = cached_projection_slope(runs)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(9, 7.2), height_ratios=[1, 1.6],
        facecolor=SURFACE, constrained_layout=True)

    # ---- Panel 1: phase composition per run -------------------------
    _style_axis(ax1)
    bar_h = 0.42
    ypos = np.arange(len(runs))[::-1]
    lefts = np.zeros(len(runs))
    for group in ("setup", "photometry", "write"):
        widths = np.array([fold_phases(r)[group] for r in runs])
        ax1.barh(ypos, widths, bar_h, left=lefts, color=COLORS[group],
                 label=LABELS[group], edgecolor=SURFACE, linewidth=1)
        lefts += widths
    ax1.set_xlim(0, max(lefts) * 1.14)
    for y, run, total in zip(ypos, runs, lefts):
        ax1.text(total + max(lefts) * 0.012, y, _fmt_s(run["wall_s"]),
                 va="center", fontsize=9, color=INK, fontweight="bold")
        write_s = fold_phases(run)["write"]
        if write_s > 0.55 * max(lefts):  # inside-label only when it fits
            ax1.text(run["wall_s"] - write_s / 2, y,
                     f"write + matching · {_fmt_s(write_s)}",
                     va="center", ha="center", fontsize=9, color=INK)
    ax1.set_yticks(ypos)
    ax1.set_yticklabels(
        [f"{r['n_positions']} source{'s' if r['n_positions'] != 1 else ''}"
         for r in runs], fontsize=10, color=INK_2)
    ax1.set_xlabel("wall-clock time (s)", fontsize=9, color=MUTED)
    ax1.grid(axis="x", color=GRID, linewidth=0.8)
    ax1.set_axisbelow(True)
    ax1.legend(loc="upper right", frameon=False, fontsize=8.5,
               labelcolor=INK_2)
    ax1.set_title("Where the wall clock goes", loc="left", fontsize=12,
                  color=INK, fontweight="bold", pad=10)

    # ---- Panel 2: scaling projection ---------------------------------
    _style_axis(ax2)
    n_grid = np.linspace(0, max_positions, 2)
    cur_h = (fixed_s + per_pos_s * n_grid) / 3600
    fix_h = (fixed_s + cached_slope * n_grid) / 3600
    ax2.plot(n_grid, cur_h, color=COLORS["setup"], linewidth=2,
             solid_capstyle="round", label="Current code (linear fit)")
    ax2.plot(n_grid, fix_h, color=COLORS["photometry"], linewidth=2,
             linestyle=(0, (5, 4)),
             label="Projected with catalog caching")
    n_meas = [r["n_positions"] for r in runs]
    wall_meas = [r["wall_s"] / 3600 for r in runs]
    ax2.scatter(n_meas, wall_meas, s=55, color=COLORS["setup"],
                edgecolor=SURFACE, linewidth=1.5, zorder=5,
                label="Measured runs")
    for n, w in zip(n_meas, wall_meas):
        ax2.annotate(f"measured {_fmt_s(w * 3600)}", (n, w),
                     xytext=(8, 8), textcoords="offset points",
                     fontsize=9, color=INK, fontweight="bold")
    ax2.annotate(_fmt_s(cur_h[-1] * 3600),
                 (max_positions, cur_h[-1]), xytext=(8, -3),
                 textcoords="offset points", fontsize=9, color=INK,
                 fontweight="bold")
    ax2.annotate(f"≈ {_fmt_s(fix_h[-1] * 3600)}",
                 (max_positions, fix_h[-1]), xytext=(8, -3),
                 textcoords="offset points", fontsize=9, color=INK,
                 fontweight="bold")
    ax2.set_xlim(0, max_positions * 1.12)
    ax2.set_ylim(0, cur_h[-1] * 1.1)
    ax2.set_xlabel("sources per batched run", fontsize=9, color=MUTED)
    ax2.set_ylabel("wall time (h)", fontsize=9, color=MUTED)
    ax2.grid(axis="y", color=GRID, linewidth=0.8)
    ax2.set_axisbelow(True)
    ax2.legend(loc="upper left", frameon=False, fontsize=8.5,
               labelcolor=INK_2)
    n_epochs = runs[0].get("n_epochs")
    ax2.set_title(
        f"Projected wall time vs. batch size ({n_epochs} epochs per run)",
        loc="left", fontsize=12, color=INK, fontweight="bold", pad=10)

    # ---- Figure title from meta --------------------------------------
    bits = []
    if meta:
        if meta.get("pid"):
            bits.append(f"pid {meta['pid']}")
        if meta.get("git_sha"):
            bits.append(f"git {meta['git_sha']}")
        if meta.get("started_utc"):
            bits.append(meta["started_utc"][:10])
    fields = sorted({r["field"] for r in runs})
    if fields:
        bits.insert(0, "field " + ", ".join(str(f) for f in fields))
    fig.suptitle(
        "Forced-photometry timing benchmark — " + " · ".join(bits),
        fontsize=11.5, color=INK, fontweight="bold", x=0.02, ha="left")

    fig.savefig(out_path, dpi=200, facecolor=SURFACE)
    plt.close(fig)
    logger.info("wrote %s", out_path)
    return out_path


def benchmark_date(jsonl_path):
    """The benchmark's start date as YYYYMMDD, from the meta record
    (today's date if the file has none)."""
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("kind") == "meta":
                started = rec.get("started_utc")
                if started:
                    return started[:10].replace("-", "")
                break
    return date.today().strftime("%Y%m%d")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("jsonl", type=Path,
                        help="benchmark JSONL file (from benchmark_forced_phot.py --run)")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="output image path (default: <jsonl stem>_timing.png)")
    parser.add_argument("--max-positions", type=int, default=100,
                        help="batch size to extrapolate to (default: 100)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = args.output or args.jsonl.with_name(
        f"{args.jsonl.stem}_timing_{benchmark_date(args.jsonl)}.png")
    plot_timing_summary(args.jsonl, out, max_positions=args.max_positions)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
