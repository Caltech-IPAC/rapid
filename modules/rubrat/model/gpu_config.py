"""Pin TensorFlow/CUDA to physical GPU 0 for this project.

Import this module before ``import tensorflow`` anywhere in the process.
"""

from __future__ import annotations

import os

# Physical GPU index on the host. Only GPU 0 is exposed to TensorFlow.
os.environ["CUDA_VISIBLE_DEVICES"] = os.environ.get(
    "RUBRAT_CUDA_DEVICE", os.environ.get("UNIFIED_RAPID_CUDA_DEVICE", "0")
)
