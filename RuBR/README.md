# RuBR: Model + Experiments Repository

RuBR contains reusable model components in `model/` and experiment entry scripts in `experiments/`.

## Directory structure

- `model/`
	- `dann_model.py` (DANN architecture)
	- `data.py` (data loading and normalization helpers)
	- `layers.py` (shared rotation-invariant and GRL layers)
	- `callbacks.py` (training callbacks such as F1-based early stopping)
	- `metadata.py` (metadata helpers used by evaluation/analysis)
- `experiments/`
	- `rotinv/`
		- `train_rotinv_with_features.py`
		- `evaluate_rotinv_with_features.py`
	- `domain_adaption/`
		- `train_dann.py`
		- `test_dann.py`
		- `train_control_baseline.py`
		- `test_control_baseline.py`
	- `analysis/`
		- `compare_magnitude_histograms.py`

## Setup

Requires Python 3.11.

Install dependencies:

```bash
pip install -r requirements.txt
```

## Data download

[RAPID Pipeline Products — Public Access](https://caltech-ipac-rapid.readthedocs.io/en/latest/prod/products.html#public-access)

This page contains instructions to download RAPID pipeline data.

## Dataset format requirements

Required keys in every dataset file:

- `X`
- `feats`
- `y`
- `metadata`

Required labels:

- `y.shape == (N,)`
- `y` contains `0` and `1`

Required image format:

- `X.shape == (N, 64, 64, 3)` or `X.shape == (N, 3, 64, 64)`
- channel order: `ref, sci, diff`

Required metadata:

- `len(metadata) == N`
- for `experiments/rotinv/train_rotinv_with_features.py` and `experiments/rotinv/evaluate_rotinv_with_features.py`:
  - each `metadata[i]` must contain `filter`
  - allowed `filter` values: `F184`, `H158`, `J129`, `K213`, `R062`, `Y106`, `Z087`

Required file format:

- repo-native rotinv scripts accept `.npz` and `.npy`
- repo-native domain-adaptation scripts accept `.npz`
- `X` and `feats` must not contain NaNs

### Per-script feature requirements

| Script / model | Required `feats` shape | Required feature order |
|---|---:|---|
| `experiments/rotinv/train_rotinv_with_features.py` | `(N, F)` | exact column order from input dataset; train/val/test must match |
| `experiments/rotinv/evaluate_rotinv_with_features.py` | `(N, F)` | exact column order used during training |
| `experiments/domain_adaption/train_control_baseline.py` | `(N, F)` | exact column order from input dataset; train/val/test must match |
| `experiments/domain_adaption/test_control_baseline.py` | `(N, F)` | exact column order used during training |
| `experiments/domain_adaption/train_dann.py` | `(N, F)` | exact column order from source/target training datasets; all splits must match |
| `experiments/domain_adaption/test_dann.py` | `(N, F)` | exact column order used during training |
| `scripts/eval_comb_author.py` | `(N, 5)` | `flux, mag, npix, roundness, sharpness` |
| `scripts/run_author_test_compat.py control` | `(N, 6)` | `mag, roundness, sharpness, peak, flux, npix` |
| `scripts/run_author_test_compat.py dann` | `(N, 6)` | `mag, roundness, sharpness, peak, flux, npix` |

### Object-dtype `feats`

If `feats` is stored as an object array of dicts:

- repo-native loaders use `list(f.values())`
- `scripts/eval_comb_author.py` uses sorted keys and expects:
  - `flux`
  - `mag`
  - `npix`
  - `roundness`
  - `sharpness`

## Experiment details and usage

### 1) RotInv training

Script: `experiments/rotinv/train_rotinv_with_features.py`

Purpose:
- Trains rotationally invariant hybrid classifier (image branch + tabular features).

Main input:
- `--data_path`: training dataset (`.npz` or supported format in script)

Main outputs:
- Saved model checkpoints/final model
- Training history plot and evaluation artifacts under `--output_dir`

Example:

```bash
python -m experiments.rotinv.train_rotinv_with_features \
	--data_path /path/to/train_data.npz \
	--output_dir ./outputs/rotinv_train
```

### 2) RotInv evaluation

Script: `experiments/rotinv/evaluate_rotinv_with_features.py`

Purpose:
- Evaluates a trained RotInv model over batched test data.

Main inputs:
- `--data_dir`: directory containing test batches
- `--model_path`: trained model file

Main outputs:
- Precision/recall threshold plot
- Magnitude histogram and summary metrics under `--output_dir`

Example:

```bash
python -m experiments.rotinv.evaluate_rotinv_with_features \
	--data_dir /path/to/test_batches \
	--model_path /path/to/rotinv_model.h5 \
	--output_dir ./outputs/rotinv_eval
```

### 3) DANN training (domain adaptation)

Script: `experiments/domain_adaption/train_dann.py`

Purpose:
- Trains domain-adversarial model using source + target domains.

Main inputs:
- `--source_data`: source-domain training set
- `--target_data`: target-domain training set

Main outputs:
- Best/final DANN models
- Training curves and summary text under `--output_dir`

Example:

```bash
python -m experiments.domain_adaption.train_dann \
	--source_data /path/to/source_train.npz \
	--target_data /path/to/target_train.npz \
	--output_dir ./outputs/dann_train
```

### 4) DANN evaluation

Script: `experiments/domain_adaption/test_dann.py`

Purpose:
- Evaluates trained DANN model on source and target test sets.

Main inputs:
- `--model_path`
- `--source_test`
- `--target_test`

Main outputs:
- Per-domain metrics reports, plots, and summary files under `--output_dir`

Example:

```bash
python -m experiments.domain_adaption.test_dann \
	--model_path /path/to/dann_model.h5 \
	--source_test /path/to/source_test.npz \
	--target_test /path/to/target_test.npz \
	--output_dir ./outputs/dann_eval
```

### 5) Control baseline training

Script: `experiments/domain_adaption/train_control_baseline.py`

Purpose:
- Trains non-adversarial control baseline for direct comparison with DANN.

Main input:
- `--train_data`

Main outputs:
- Control model checkpoints/final model and training summaries under `--output_dir`

Example:

```bash
python -m experiments.domain_adaption.train_control_baseline \
	--train_data /path/to/control_train.npz \
	--output_dir ./outputs/control_train
```

### 6) Control baseline evaluation

Script: `experiments/domain_adaption/test_control_baseline.py`

Purpose:
- Evaluates control baseline on test data and reports ROC/PR + confusion metrics.

Main inputs:
- `--model_path`
- `--test_data`

Main outputs:
- Test metrics report and evaluation plots under `--output_dir`

Example:

```bash
python -m experiments.domain_adaption.test_control_baseline \
	--model_path /path/to/control_model.h5 \
	--test_data /path/to/control_test.npz \
	--output_dir ./outputs/control_eval
```

### 7) DANN vs Control analysis

Script: `experiments/analysis/compare_magnitude_histograms.py`

Purpose:
- Produces magnitude-based comparison plots between DANN and control models.

Main inputs:
- `--dann_model`
- `--control_model`
- `--test_data`

Main outputs:
- Comparison plots and text summaries under `--output_dir`

Example:

```bash
python -m experiments.analysis.compare_magnitude_histograms \
	--dann_model /path/to/dann_model.h5 \
	--control_model /path/to/control_model.h5 \
	--test_data /path/to/compare_test.npz \
	--output_dir ./outputs/model_comparison
```

## Notes

- Use module execution (`python -m ...`) from the repository root.
