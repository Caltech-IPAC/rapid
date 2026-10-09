# Model card

## Train / eval from NPZ

Hyperparameters: `configs/rb_model.yaml`. Entry points:

- Train: `rubrat train supervised --train … --val … --feats-scaler … --output …`
- Eval: `rubrat evaluate-research --checkpoint … --val … --test … --feats-scaler … --survey {hltds,gbtds}`
- Scores only: `rubrat infer --checkpoint … --npz … --feats-scaler … --output …`

Step-by-step commands with path placeholders: [README](../README.md).

## Intended use

RuBR-AT scores Roman RAPID difference-image detections as real or bogus. It is
a research classifier for simulated HLTDS and GBTDS products, not a calibrated
operational alert policy for on-sky Roman data.

## Inputs

The image input is a 64×64×3 arcsinh-transformed stack of science, reference,
and SFFT difference cutouts. The tabular input contains nine ordered features:
transformed SNR, fit statistic, clean-fit indicator, log chi-square, log positional
error, log fitted pixels, two roundness values, and sharpness. Survey and filter
IDs provide contextual embeddings.

The exact operational preprocessing and catalog-column contract is implemented
by `rubrat.rapid.RAPIDRealBogusClassifier`; see the
[RAPID integration guide](rapid-pipeline-integration.md). The canonical filter
IDs are recorded in `configs/filter_registry.yaml` and are the same IDs written
by the dataset builders.

## Architecture

The image encoder evaluates a shared residual CNN under four right-angle
rotations. It first counter-rotates the four feature maps into a common frame
and averages them. This aligned mean is equivariant, not invariant. A second,
distinct average over the four spatial rotations of that mean makes the token
tensor invariant. The 8 by 8 map is flattened and linearly projected to 64
tokens of width 128. The model's sigmoid output is called a Real/Bogus score;
it is not interpreted as a posterior probability unless a validation-fitted
calibrator is applied.

For input image `X`, shared spatial encoder `E`, and right-angle rotation
`R_k`, the aligned average is

```
M(X) = (1/4) sum_k R_{-k} E(R_k X).
```

It obeys `M(R_j X) = R_j M(X)`. The second spatial average is

```
I(X) = (1/4) sum_j R_j M(X),
```

which obeys `I(R_l X) = I(X)`. The no-rotation ablation removes both operations
and evaluates `E(X)` once.

The residual schedule is: 3 by 3 stem convolution with 32 channels,
BatchNorm, GELU; residual blocks (32, stride 1), (64, stride 2), (64, stride
1), (128, stride 2), and (128, stride 2). Each residual block is Conv-BN-GELU-
Conv-BN-add-GELU and uses a 1 by 1 projection when shape or stride changes.
For 64-pixel inputs, the resulting map is 8 by 8 by 128.

The tabular query is `q = LN(W_f f) + e_survey + e_filter`. For head `h` with
`d_h = 32`, `Q_h = q W_Q,h`, `K_h = K W_K,h`, and `V_h = V W_V,h`. Each head
returns `softmax(Q_h K_h^T / sqrt(d_h)) V_h`. The four outputs are concatenated
and passed through the learned output projection used by Keras multi-head
attention. Attention dropout is 0.3. The attended vector is added to a
projected-query skip and the global mean of the image tokens, normalized, and
passed through a 128-256-128 residual feed-forward block. The classification
head is Dense(128, GELU), dropout 0.3, and Dense(1, sigmoid).

The final serialized RuBR-AT artifact contains 879,329 parameters in total, of
which 877,601 are trainable. The remaining 1,728 are BatchNormalization moving
statistics.
`scripts/verify_rotation_invariance.py` measures token-level and score-level
invariance for all four rotations and archives the numerical error.

`configs/rb_model.yaml` is authoritative for dimensions and optimization.
Keras serialization names remain compatible with Unified-RAPID checkpoints.

## Limitations

Performance depends on simulation fidelity, truth matching, subtraction
method, filter, crowding, source magnitude, and the chosen definition of real.
Results must therefore identify the dataset policy, split manifest, threshold,
and magnitude policy. Raw scores should not be interpreted as calibrated
posterior probabilities. The review analysis fits a logistic calibrator on
validation scores only and reports both raw and calibrated reliability on the
untouched test data.
