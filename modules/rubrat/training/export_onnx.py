#!/usr/bin/env python
"""Export a trained RuBR-AT checkpoint to ONNX, the model the pipeline runs.

ONNX is the model contract (Ben, 2026-11-07): the pipeline scores with
``onnxruntime`` and carries no TensorFlow or Keras, so this export runs in
the training environment (TensorFlow, Keras and ``tf2onnx``), as the last
act of a training run (``train_rb.py``) or by hand on a checkpoint. A
delivered model is a prefix holding three files under ``SHA256SUMS``: the
ONNX file (``best.onnx``), the feature scaler (``canonical_9.json``) and the
threshold (``threshold.json``); after writing the ONNX file this rewrites
``SHA256SUMS`` in its directory over those of the three that are present.

The custom layers are registered by importing ``model_factory``. Keras's
exact GELU lowers to ``Erfc``, which ``tf2onnx`` lacks; it is written as
``1 - Erf(x)``. Opset 17. The ONNX inputs keep the Keras input names
(``images``, ``tabular``, ``survey_id``, ``filter_id``); the output is ``rb``.
The 2026-10-06 checkpoint's export scored 512 random inputs within 5.7e-7 of
TensorFlow, every label equal at its threshold.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

#: The delivery's files beside the ONNX file, in ``SHA256SUMS`` when present.
DELIVERY_FILES = ("canonical_9.json", "threshold.json")


def export(checkpoint: Path, output: Path) -> Path:
    """Write ``checkpoint`` as ONNX to ``output``; then ``SHA256SUMS`` beside it."""
    import numpy as np
    import tensorflow as tf
    import tf2onnx
    from tensorflow import keras

    from modules.rubrat.model import model_factory  # noqa: F401  registers the custom layers

    model = keras.models.load_model(checkpoint, compile=False)
    spec = [tf.TensorSpec((None,) + tuple(t.shape[1:]), t.dtype, name=t.name) for t in model.inputs]

    def erfc(ctx, node, name, args):
        # Erfc(x) = 1 - Erf(x); ONNX has Erf (opset 9+), no Erfc.
        one = ctx.make_const(name + "_one", np.array(1.0, dtype=np.float32))
        erf = ctx.make_node("Erf", [node.input[0]], name=name + "_erf")
        out = node.output[0]
        ctx.remove_node(node.name)
        ctx.make_node("Sub", [one.output[0], erf.output[0]], outputs=[out], name=name + "_sub")

    output.parent.mkdir(parents=True, exist_ok=True)
    tf2onnx.convert.from_keras(model, input_signature=spec, opset=17, output_path=str(output),
                               custom_op_handlers={"Erfc": (erfc, [])})
    write_sums(output)
    return output


def write_sums(onnx_file: Path) -> Path:
    """``SHA256SUMS`` beside ``onnx_file`` over it and the delivery files present there."""
    directory = onnx_file.parent
    lines = [f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n"
             for name in (onnx_file.name, *DELIVERY_FILES) if (directory / name).is_file()]
    path = directory / "SHA256SUMS"
    path.write_text("".join(lines))
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--checkpoint", type=Path, required=True, help="The .keras checkpoint.")
    parser.add_argument("--output", type=Path, required=True, help="The .onnx file to write.")
    args = parser.parse_args()
    export(args.checkpoint, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
