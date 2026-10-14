"""Score one RAPID science-job directory with the real/bogus classifier, from the command line.

This is the offline counterpart of the pipeline path (rda.compute_rb, called through the
extra-catalog-column machinery in modules/utils/rapid_pipeline_subs.py).  It resolves the
canonical product filenames below a job directory, scores every row of the PSF-fit catalog,
and writes a scored copy of the catalog with a provenance sidecar.  Run with the repository
root on PYTHONPATH:

    python -m modules.rubrat.inference.cli score --job-dir /work/jid90828 \\
        --checkpoint best.keras --feats-scaler canonical_9.json --threshold-json threshold.json \\
        --survey-id 0 --difference sfftdiffimage_masked.fits
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m modules.rubrat.inference.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    score = sub.add_parser("score", help="Score one RAPID job product directory.")
    score.add_argument("--job-dir", required=True)
    score.add_argument("--checkpoint", required=True, help="Keras checkpoint (best.keras).")
    score.add_argument("--feats-scaler", required=True, help="Feature-scaler JSON paired with the checkpoint.")
    threshold = score.add_mutually_exclusive_group(required=True)
    threshold.add_argument("--threshold", type=float, help="Score at or above which a source is labeled real.")
    threshold.add_argument("--threshold-json", help="JSON holding the threshold ({\"threshold\": value} or a metrics.json).")
    score.add_argument("--survey-id", required=True, type=int, choices=[0, 1], help="Survey token: 0 HLTDS, 1 GBTDS.")
    score.add_argument("--filter", dest="filter_name", help="Override a missing/incorrect science FITS FILTER value.")
    score.add_argument("--science", help="Science product path, absolute or relative to --job-dir.")
    score.add_argument("--reference", help="Reference product path, absolute or relative to --job-dir.")
    score.add_argument("--difference", help="Difference product path, absolute or relative to --job-dir.")
    score.add_argument("--psf-catalog", help="PSF-fit catalog path, absolute or relative to --job-dir.")
    score.add_argument("--finder-catalog", help="Finder catalog path, absolute or relative to --job-dir.")
    score.add_argument("--output", help="Output catalog; defaults to a canonical filename below --job-dir.")
    score.add_argument("--batch-size", type=int, default=128)
    score.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)

    # Imported here so that --help does not import TensorFlow.
    from modules.rubrat.inference.classifier import (
        DEFAULT_OUTPUT_PRODUCT,
        RealBogusClassifier,
        load_validation_threshold,
        write_scored_catalog,
    )

    threshold = args.threshold if args.threshold is not None else load_validation_threshold(args.threshold_json)
    classifier = RealBogusClassifier(
        args.checkpoint,
        args.feats_scaler,
        threshold=threshold,
        survey_id=args.survey_id,
        batch_size=args.batch_size,
    )
    rows, provenance = classifier.score_job(
        args.job_dir,
        filter_name=args.filter_name,
        science=args.science,
        reference=args.reference,
        difference=args.difference,
        psf_catalog=args.psf_catalog,
        finder_catalog=args.finder_catalog,
    )
    output = Path(args.output) if args.output else Path(args.job_dir) / DEFAULT_OUTPUT_PRODUCT
    catalog, sidecar = write_scored_catalog(rows, provenance, output, overwrite=args.overwrite)
    print(f"Wrote {int(rows['rb_valid'].sum())}/{len(rows)} scores to {catalog}")
    print(f"Wrote provenance to {sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
