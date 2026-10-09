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
