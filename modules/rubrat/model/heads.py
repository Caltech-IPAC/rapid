"""Output heads for Phase 2 classification models."""

from __future__ import annotations

from tensorflow.keras import layers

from classification import gpu_config  # noqa: F401


def _mlp_head(x, units: int, dropout: float, name: str):
    x = layers.Dense(units, activation="gelu", name=f"{name}_dense")(x)
    if dropout > 0:
        x = layers.Dropout(dropout, name=f"{name}_dropout")(x)
    return x


def build_rb_head(x, hidden_dim: int = 128, dropout: float = 0.1):
    x = _mlp_head(x, hidden_dim, dropout, "head_rb")
    return layers.Dense(1, activation="sigmoid", name="rb")(x)


def build_rr_head(x, num_classes: int = 3, hidden_dim: int = 128, dropout: float = 0.1):
    x = _mlp_head(x, hidden_dim, dropout, "head_rr")
    return layers.Dense(num_classes, activation="softmax", name="rr")(x)


def build_sn_head(x, num_classes: int = 5, hidden_dim: int = 128, dropout: float = 0.1):
    x = _mlp_head(x, hidden_dim, dropout, "head_sn")
    return layers.Dense(num_classes, activation="softmax", name="sn")(x)


def build_var_head(x, num_classes: int = 2, hidden_dim: int = 128, dropout: float = 0.1):
    x = _mlp_head(x, hidden_dim, dropout, "head_var")
    return layers.Dense(num_classes, activation="softmax", name="var")(x)


def build_gbtds3_head(x, num_classes: int = 3, hidden_dim: int = 128, dropout: float = 0.1):
    x = _mlp_head(x, hidden_dim, dropout, "head_gbtds3")
    return layers.Dense(num_classes, activation="softmax", name="gbtds3")(x)
