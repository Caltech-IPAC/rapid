"""Render one run's quality numbers: a Markdown table per stage and a sampled gallery.

    scripts/inspect_run.py --run <id> --out DIR [--no-gallery]

Outside the pipeline, like ``validate_run.py``: it reads the database's
``job_metrics`` view (one row per job and metric, from the jobs'
manifests) and, for the gallery, the difference jobs' published images;
it writes only under ``DIR``. ``DIR/run-<id>.md`` holds one table per
stage: a row per job (its epoch or field), a column per metric, then the
run's median row and, under it, each row's deviation from that median
(value minus median). The gallery is one PNG per sampled difference job,
``DIR/run-<id>-difference-<job>.png``: the full-frame difference
thumbnail and, for up to eight of the job's residual stars
(``difference.residual_stars`` in its manifest, the stars the core and
ring fractions are measured on), science, reference and difference
cutouts, the difference divided by its star's reference flux (5 px
aperture less the 8 to 12 px annulus median, the core fraction's) and
shown at ±:data:`FLUX_STRETCH` per pixel, so every star's residual
pattern is seen at the scale the core and ring fractions measure it. The sample is the first and last epoch of the run by ``expid``
and every epoch whose ``n_sources_snr_gt20`` is more than twice the run's
median: the images ledger's pictures made by code, not every job.

``check_run.sh`` runs it first, so the numbers are read as soon as a run
drains. The database is named as for every rapidpipe command.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

#: A gallery's residual-star cutouts, at most; their half width, px; the thumbnail's block, px.
GALLERY_STARS, CUTOUT_HALF, THUMB_BLOCK = 8, 15, 8
#: The difference cutout's stretch, a fraction of its star's flux per pixel: a +0.006 core
#: fraction over the 2.5 px core's 20 pixels is 3e-4 per pixel.
FLUX_STRETCH = 1e-3
#: Files beside a difference image that the gallery reads.
SCIENCE, REFERENCE = "bkg_subbed_science_image.fits.gz", "awaicgen_output_mosaic_image_resampled_gainmatched.fits.gz"

QUERY = ("SELECT job, stage, expid, sca, fid, field, file, metric, value FROM job_metrics"
         " WHERE run = %s ORDER BY stage, job, metric")


def label(row: dict) -> str:
    """A job's row label: its epoch, field or file, from its scope columns."""
    if row["expid"] is not None:
        return f"{row['expid']} sca {row['sca']}"
    if row["field"] is not None:
        return f"field {row['field']}"
    return row["file"] or ""


def tables(rows: list[dict]) -> dict[str, str]:
    """One Markdown table per stage from ``job_metrics`` rows."""
    by_stage: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in rows:
        job = by_stage[r["stage"]].setdefault(r["job"], {"label": label(r), "values": {}})
        job["values"][r["metric"]] = r["value"]
    out = {}
    for stage, jobs in by_stage.items():
        metrics = sorted({m for j in jobs.values() for m in j["values"]})
        median = {m: statistics.median(v) for m in metrics
                  if (v := [j["values"][m] for j in jobs.values() if m in j["values"]])}
        lines = ["| job | scope | " + " | ".join(metrics) + " |",
                 "|---|---|" + "---|" * len(metrics)]
        for job, j in sorted(jobs.items(), key=lambda kv: kv[1]["label"]):
            lines.append(f"| {job} | {j['label']} | "
                         + " | ".join(_fmt(j["values"].get(m)) for m in metrics) + " |")
        lines.append("| median | | " + " | ".join(_fmt(median.get(m)) for m in metrics) + " |")
        for job, j in sorted(jobs.items(), key=lambda kv: kv[1]["label"]):
            lines.append(f"| {job} − median | {j['label']} | " + " | ".join(
                _fmt(j["values"][m] - median[m], signed=True) if m in j["values"] else ""
                for m in metrics) + " |")
        out[stage] = "\n".join(lines)
    return out


def sample(rows: list[dict]) -> list[int]:
    """The difference jobs the gallery renders: first and last by expid, and the bright outliers."""
    diffs = {}
    for r in rows:
        if r["stage"] == "difference":
            diffs.setdefault(r["job"], {"expid": r["expid"]})
            diffs[r["job"]][r["metric"]] = r["value"]
    if not diffs:
        return []
    order = sorted(diffs, key=lambda j: (diffs[j]["expid"] or 0, j))
    chosen = {order[0], order[-1]}
    bright = [d["n_sources_snr_gt20"] for d in diffs.values() if "n_sources_snr_gt20" in d]
    if bright:
        median = statistics.median(bright)
        chosen |= {j for j, d in diffs.items() if d.get("n_sources_snr_gt20", 0) > 2 * median}
    return sorted(chosen, key=order.index)


def _fmt(value, signed: bool = False) -> str:
    if value is None:
        return ""
    if float(value).is_integer() and abs(value) >= 10:
        return f"{value:+.0f}" if signed else f"{value:.0f}"
    return f"{value:+.4g}" if signed else f"{value:.4g}"


def thumbnail(image, block: int = THUMB_BLOCK):
    """The largest-magnitude pixel of each ``block x block`` block (the images ledger's thumbnail)."""
    import numpy as np

    ny, nx = (s // block * block for s in image.shape)
    flat = (np.nan_to_num(image[:ny, :nx]).reshape(ny // block, block, nx // block, block)
            .transpose(0, 2, 1, 3).reshape(ny // block, nx // block, block * block))
    return np.take_along_axis(flat, np.abs(flat).argmax(-1)[..., None], -1)[..., 0]


def star_flux(cut) -> float:
    """A centred cutout's star flux: within 5 px less the 8 to 12 px annulus median (the core fraction's)."""
    import numpy as np

    from rapidpipe.science.difference.metrics import APERTURE_PX, SKY_ANNULUS_PX

    centre = (cut.shape[0] - 1) / 2
    rows, cols = np.indices(cut.shape)
    r = np.hypot(cols - centre, rows - centre)
    sky = np.nanmedian(cut[(r >= SKY_ANNULUS_PX[0]) & (r < SKY_ANNULUS_PX[1])])
    return float(np.nansum((cut - sky)[r < APERTURE_PX]))


def gallery(cur, run: int, job: int, out: Path) -> Path:
    """Render one difference job's PNG; return its path."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from astropy.io import fits

    from rapidpipe.foundation.prefix import get

    cur.execute("SELECT d.filename, j.manifest -> 'difference' -> 'residual_stars', d.expid"
                " FROM diffimages d JOIN jobs j ON j.id = d.job WHERE d.job = %s", (job,))
    url, stars, expid = cur.fetchone()
    base = url.rsplit("/", 1)[0] + "/"
    diff = fits.getdata(get(url), 0).astype(float)
    sci = fits.getdata(get(base + SCIENCE), 0).astype(float)
    ref = fits.getdata(get(base + REFERENCE), 0).astype(float)
    stars = (stars or [])[:GALLERY_STARS]
    finite = diff[np.isfinite(diff)]
    sigma = 1.4826 * float(np.median(np.abs(finite - np.median(finite))))
    fig, axes = plt.subplots(1 + len(stars), 3, figsize=(9, 3 * (1 + len(stars))), squeeze=False)
    axes[0][0].imshow(thumbnail(diff), origin="lower", cmap="gray", vmin=-5 * sigma, vmax=5 * sigma)
    axes[0][0].set_title(f"job {job} expid {expid}: difference, {THUMB_BLOCK}x peak, ±5σ", fontsize=8)
    for s in stars:
        axes[0][0].plot(s["x"] / THUMB_BLOCK, s["y"] / THUMB_BLOCK, "o", mfc="none", mec="r", ms=6)
    axes[0][1].axis("off")
    axes[0][2].axis("off")
    for row, s in zip(axes[1:], stars):
        ix, iy, h = int(round(s["x"])), int(round(s["y"])), CUTOUT_HALF
        flux = star_flux(ref[iy - h:iy + h + 1, ix - h:ix + h + 1])
        for ax, (name, image) in zip(row, (("science", sci), ("reference", ref), ("difference", diff))):
            cut = image[iy - h:iy + h + 1, ix - h:ix + h + 1]
            title = f"{name} ({s['x']:.0f}, {s['y']:.0f})"
            if name == "difference" and flux > 0:
                ax.imshow(cut / flux, origin="lower", cmap="gray", vmin=-FLUX_STRETCH,
                          vmax=FLUX_STRETCH)
                title += f" / flux {flux:.3g}, ±{FLUX_STRETCH:g}"
            elif name == "difference":
                ax.imshow(cut, origin="lower", cmap="gray", vmin=-5 * sigma, vmax=5 * sigma)
            else:
                lo, hi = np.nanpercentile(cut, [1, 99.5])
                ax.imshow(cut, origin="lower", cmap="gray", vmin=lo, vmax=hi)
            ax.set_title(title, fontsize=8)
            ax.set_xticks([])
            ax.set_yticks([])
    path = out / f"run-{run}-difference-{job}.png"
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-gallery", action="store_true")
    args = parser.parse_args(argv)
    from rapidpipe.db.connection import connect

    args.out.mkdir(parents=True, exist_ok=True)
    with connect(application_name="inspect_run") as conn, conn.cursor() as cur:
        cur.execute(QUERY, (args.run,))
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
        text = [f"# Run {args.run}: job metrics", ""]
        for stage, table in tables(rows).items():
            text += [f"## {stage}", "", table, ""]
        report = args.out / f"run-{args.run}.md"
        report.write_text("\n".join(text))
        print(report)
        rendered = []
        if not args.no_gallery:
            for job in sample(rows):
                path = gallery(cur, args.run, job, args.out)
                rendered.append(path.name)
                print(path)
    print(f"Inspect run {args.run}: {len({r['job'] for r in rows})} jobs with metrics, "
          f"{len(tables(rows))} stages, gallery {json.dumps(rendered)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
