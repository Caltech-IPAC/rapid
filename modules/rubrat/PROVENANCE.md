# Source provenance

RuBR-AT was extracted from the real/bogus implementation in Unified-RAPID at
commit `a867f808900e31b4f89305be9d1ce5189ef9cf6a`.

Only files committed at that revision were used as the migration source. Local
uncommitted Unified-RAPID work was deliberately excluded. The neural network
layers retain their registered Keras package name (`UnifiedRAPID`) so existing
`.keras` checkpoints remain loadable.

Intentional correctness changes made during extraction:

- HLTDS truth matching no longer applies a magnitude limit by default.
- The canonical magnitude is explicitly `truth_mag_ab = mag + zpt`.
- The default AB-magnitude 26 policy is evaluation-only.
- HLTDS PU positives are not removed from training because they are faint.
- The package and documentation contain RB functionality only.

The migrated default architecture was checked against that source revision
with a fixed TensorFlow seed and fixed input tensors: both builds contain
879,329 parameters and produce byte-identical initialized weights and
predictions. Keras registration names remain unchanged for checkpoint loading.

# Move into RAPID (2026-10-09)

`modules/rubrat/` was copied from RuBR-AT (https://github.com/Aayush75/RuBR-AT, Aayush
Kuloor) at commit `3e665de` and is maintained in RAPID from then on; the RuBR-AT repository is
no longer developed. The four RuBR-AT packages (`rubrat`, `classification`, `ingestion`,
`rubr_compat`) were reorganized by role:

- `preprocessing/` — cutouts, the nine features, the feature scaler, filter registry, NPZ
  contract (`data_utils`, `npz_builders`, `rubr_adapter` trimmed to its catalog/cutout/feature
  parts, `fits_loader`, `rb_manifest`, `validation`, `phase0_utils`, `compute_feats_scaler`).
- `model/` — the architecture and the custom Keras layers needed to load a checkpoint
  (`model_factory`, `cnn_encoder`, `cross_attention`, `heads`, `gpu_config`, `rb_model.yaml`).
- `training/` — the training loop and its helpers (`train_utils`, formerly `training.py`),
  losses, metrics, positive-unlabeled training, evaluation, threshold selection
  (`select_threshold`, formerly `evaluate.py`), run provenance, the HLTDS magnitude policy.
- `inference/` — `classifier.py` (formerly `rubrat/rapid.py`; the class
  `RAPIDRealBogusClassifier` is now `RealBogusClassifier`) and a scoring CLI.
- `legacy/` — unmaintained reference code and RuBR-AT's documents; see `legacy/README.md`.

Left behind in RuBR-AT: the ablation suite, the legacy-RuBR comparison and `rubr_compat`,
the paper-specific analyses, the Snakemake workflow, and NPZ-only inference. The model
definitions, the Keras registration package name `UnifiedRAPID`, the preprocessing formulas,
and the arcsinh stretch are unchanged, and the move was checked by scoring one synthetic chip
through the RuBR-AT classifier and the moved one: identical scores, labels, and statuses.

The earlier `rapid/RuBR/` directory (Ashish Mahabal, commit `90fbc83b`, 2026-05-29), a
different six-feature model with a DANN experiment, was removed; its DANN experiment and model
package were moved to `legacy/dann/`.
