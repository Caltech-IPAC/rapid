"""Unified command-line entrypoint for RuBR-AT."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from rubrat.validation import validate_dataset, validate_products, write_report

ROOT = Path(__file__).resolve().parents[2]

FORWARDED = {
    ("labels", "build"): "label_detection_catalogs.py",
    ("labels", "verify"): "verify_hltds_labels.py",
    ("dataset", "hltds"): "build_hltds_rb_npz.py",
    ("dataset", "gbtds"): "build_gbtds_rr_npz.py",
    ("dataset", "scaler"): "compute_feats_scaler.py",
    ("dataset", "manifest"): "write_rb_npz_manifest.py",
    ("dataset", "alignment"): "verify_rb_dataset_alignment.py",
    ("dataset", "rubr"): "build_rubr_from_rb_manifest.py",
    ("train", "supervised"): "train_rb.py",
    ("train", "pu"): "train_rb_pu.py",
    ("train", "ablation"): "train_rb_ablation.py",
    ("train", "rubr"): "train_rubr.py",
    ("evaluate", "pu"): "eval_rb_pu.py",
    ("evaluate", "ablation"): "eval_rb_ablation.py",
    ("compare", "rubr"): "eval_rb.py",
    ("run", "ablations"): "run_rb_ablation_suite.py",
    ("analyze", "ou24-gentype"): "analyze_rb_ou24_gentype.py",
}


def _forward(script: str, args: list[str]) -> int:
    path = ROOT / "scripts" / script
    if not path.is_file():
        raise FileNotFoundError(f"Repository script not found: {path}")
    env = dict(os.environ)
    source = str(ROOT / "src")
    env["PYTHONPATH"] = source + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run([sys.executable, str(path), *args], cwd=ROOT, env=env, check=False).returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rubrat",
        description=__doc__,
        epilog=(
            "Forwarded research commands: labels {build,verify}; dataset "
            "{hltds,gbtds,scaler,manifest,alignment,rubr}; train "
            "{supervised,pu,ablation,rubr}; evaluate {pu,ablation}; compare rubr; "
            "run ablations; analyze ou24-gentype. Pass the underlying command options directly."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Validate products or datasets.")
    validate_sub = validate.add_subparsers(dest="kind", required=True)
    products = validate_sub.add_parser("products")
    products.add_argument("--root", required=True)
    products.add_argument("--survey", choices=["hltds", "gbtds"], required=True)
    products.add_argument("--checksum", action="store_true")
    products.add_argument("--output")
    dataset = validate_sub.add_parser("dataset")
    dataset.add_argument("paths", nargs="+")
    dataset.add_argument("--checksum", action="store_true")
    dataset.add_argument("--output")

    research = sub.add_parser("evaluate-research", help="Validation-threshold evaluation with HLTDS policy metrics.")
    research.add_argument("--checkpoint", required=True)
    research.add_argument("--val", nargs="+", required=True)
    research.add_argument("--test", nargs="+", required=True)
    research.add_argument("--feats-scaler", required=True)
    research.add_argument("--survey", choices=["hltds", "gbtds"], required=True)
    research.add_argument("--output-dir", required=True)
    research.add_argument("--mag-limit-ab", type=float, default=26.0)
    research.add_argument("--no-mag-cut", action="store_true")
    research.add_argument("--batch-size", type=int, default=128)

    infer = sub.add_parser("infer", help="Run a trained RuBR-AT model on NPZ shards.")
    infer.add_argument("--checkpoint", required=True, help="Trained .keras checkpoint.")
    infer.add_argument("--npz", nargs="+", required=True, help="NPZ files, directories, or quoted glob patterns.")
    infer.add_argument("--feats-scaler", required=True, help="Feature-scaler JSON paired with the checkpoint.")
    infer.add_argument("--output", required=True, help="Destination predictions.csv path.")
    infer_threshold = infer.add_mutually_exclusive_group()
    infer_threshold.add_argument("--threshold", type=float, help="Optional real/bogus decision threshold.")
    infer_threshold.add_argument(
        "--threshold-json", help="Optional metrics.json containing a validation-selected threshold."
    )
    infer.add_argument("--batch-size", type=int, default=128)
    infer.add_argument("--chunk-size", type=int, default=1024)
    infer.add_argument("--force-cpu", action="store_true")
    infer.add_argument("--overwrite", action="store_true")

    report = sub.add_parser("report", help="Build publication tables and figures from metrics JSON files.")
    report.add_argument("metrics", nargs="+")
    report.add_argument("--output-dir", required=True)

    rapid = sub.add_parser("rapid", help="Use RuBR-AT as a RAPID real/bogus pipeline stage.")
    rapid_sub = rapid.add_subparsers(dest="rapid_command", required=True)
    score = rapid_sub.add_parser("score", help="Score one RAPID jid product directory.")
    score.add_argument("--job-dir", required=True)
    score.add_argument("--checkpoint", required=True)
    score.add_argument("--feats-scaler", required=True)
    threshold = score.add_mutually_exclusive_group(required=True)
    threshold.add_argument("--threshold", type=float)
    threshold.add_argument("--threshold-json", help="metrics.json emitted by rubrat evaluate-research.")
    score.add_argument("--survey-id", required=True, type=int, choices=[0, 1])
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
    raw = list(sys.argv[1:] if argv is None else argv)
    if len(raw) >= 2 and (raw[0], raw[1]) in FORWARDED:
        return _forward(FORWARDED[(raw[0], raw[1])], raw[2:])
    parser = _parser()
    args = parser.parse_args(raw)
    if args.command == "validate":
        report = (
            validate_products(args.root, args.survey, checksum=args.checksum)
            if args.kind == "products"
            else validate_dataset(args.paths, checksum=args.checksum)
        )
        write_report(report, args.output)
        return 0 if report["valid"] else 1
    if args.command == "evaluate-research":
        from rubrat.evaluate import evaluate

        evaluate(
            args.checkpoint,
            args.val,
            args.test,
            args.feats_scaler,
            args.output_dir,
            survey=args.survey,
            mag_limit_ab=None if args.no_mag_cut else args.mag_limit_ab,
            batch_size=args.batch_size,
        )
        return 0
    if args.command == "infer":
        from rubrat.inference import infer_npz

        threshold = args.threshold
        if args.threshold_json:
            from rubrat.rapid import load_validation_threshold

            threshold = load_validation_threshold(args.threshold_json)
        result = infer_npz(
            args.checkpoint,
            args.npz,
            args.feats_scaler,
            args.output,
            threshold=threshold,
            batch_size=args.batch_size,
            chunk_size=args.chunk_size,
            force_cpu=args.force_cpu,
            overwrite=args.overwrite,
        )
        print(f"Wrote {result['n_rows']} scores to {result['output']}")
        print(f"Wrote provenance to {result['provenance']}")
        return 0
    if args.command == "report":
        from rubrat.reporting import aggregate

        aggregate(args.metrics, args.output_dir)
        return 0
    if args.command == "rapid" and args.rapid_command == "score":
        from rubrat.rapid import (
            DEFAULT_OUTPUT_PRODUCT,
            RAPIDRealBogusClassifier,
            load_validation_threshold,
            write_scored_catalog,
        )

        threshold = args.threshold if args.threshold is not None else load_validation_threshold(args.threshold_json)
        classifier = RAPIDRealBogusClassifier(
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
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
