"""Keras model factories for Phase 2 CNN classifiers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tensorflow as tf
import yaml
from tensorflow import keras
from tensorflow.keras import layers

from classification import gpu_config  # noqa: F401
from classification.cnn_encoder import ResNetEncoder, RotInvariantResNetEncoder, SmallCNNEncoder
from classification.cross_attention import (
    CrossAttentionFusion,
    GAPLateConcatFusion,
    GatedFusion,
    ImageOnlyFusion,
    TabularOnlyFusion,
    TabularQueryBuilder,
)
from classification.data_utils import EXPECTED_FEATURE_NAMES
from classification.heads import build_rb_head


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class PerStampImageStandardization(layers.Layer):
    """Per-cutout, per-channel standardization: z-score each stamp independently."""

    def __init__(self, epsilon: float = 1e-6, **kwargs: Any):
        super().__init__(**kwargs)
        self.epsilon = float(epsilon)

    def call(self, inputs, training=None):
        mean = tf.reduce_mean(inputs, axis=[1, 2], keepdims=True)
        var = tf.reduce_mean(tf.square(inputs - mean), axis=[1, 2], keepdims=True)
        std = tf.sqrt(var + tf.cast(self.epsilon, inputs.dtype))
        return (inputs - mean) / std

    def get_config(self) -> dict[str, Any]:
        return {**super().get_config(), "epsilon": self.epsilon}


DEFAULT_FILTERS = {
    "R062": 0,
    "Z087": 1,
    "Y106": 2,
    "J129": 3,
    "H158": 4,
    "F184": 5,
    "K213": 6,
    "F213": 6,
    "F146": 7,
}


def load_config(config: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(config, dict):
        return dict(config)
    with Path(config).open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    return dict(loaded)


def _cfg(config: str | Path | dict[str, Any]) -> dict[str, Any]:
    c = load_config(config)
    c.setdefault("num_features", len(EXPECTED_FEATURE_NAMES))
    c.setdefault("num_surveys", 2)
    c.setdefault("num_filters", max(DEFAULT_FILTERS.values()) + 1)
    c.setdefault("dropout", 0.1)
    c.setdefault("head_hidden_dim", c.get("d_model", 128))
    c.setdefault("rotation_invariant", True)
    c.setdefault("encoder_mode", "resnet")
    c.setdefault("fusion_mode", "cross_attention")
    c.setdefault("use_survey_filter_token", True)
    c.setdefault("image_augmentation", "none")
    c.setdefault("image_normalization", "none")
    c.setdefault("image_standardize_epsilon", 1e-6)
    return c


def _image_branch(images: keras.KerasTensor, config: dict[str, Any]) -> keras.KerasTensor:
    x = images
    aug = str(config.get("image_augmentation", "none"))
    if aug == "random_flip":
        x = keras.layers.RandomFlip(mode="horizontal_and_vertical", name="image_random_flip")(x)
    elif aug == "gbtds_light":
        x = keras.layers.RandomFlip(mode="horizontal_and_vertical", name="image_random_flip")(x)
        x = keras.layers.RandomTranslation(
            height_factor=(-0.015, 0.015),
            width_factor=(-0.015, 0.015),
            fill_mode="constant",
            fill_value=0.0,
            name="image_random_translation",
        )(x)
        x = keras.layers.GaussianNoise(stddev=0.01, name="image_gaussian_noise")(x)
    elif aug != "none":
        raise ValueError(f"Unsupported image_augmentation {aug!r}")
    norm = str(config.get("image_normalization", "none"))
    if norm == "per_stamp_standardize":
        x = PerStampImageStandardization(
            epsilon=float(config.get("image_standardize_epsilon", 1e-6)),
            name="image_standardization",
        )(x)
    elif norm != "none":
        raise ValueError(f"Unsupported image_normalization {norm!r}")
    return x


def _trunk(config: dict[str, Any]):
    image_size = int(config["image_size"])
    d_model = int(config["d_model"])
    images = keras.Input(shape=(image_size, image_size, 3), name="images")
    tabular = keras.Input(shape=(int(config["num_features"]),), name="tabular")
    survey_id = keras.Input(shape=(), dtype="int32", name="survey_id")
    filter_id = keras.Input(shape=(), dtype="int32", name="filter_id")

    encoder_mode = str(config.get("encoder_mode", "resnet"))
    if encoder_mode == "small_cnn":
        encoder_cls = SmallCNNEncoder
    elif encoder_mode == "resnet":
        encoder_cls = RotInvariantResNetEncoder if config.get("rotation_invariant", True) else ResNetEncoder
    else:
        raise ValueError(f"Unsupported encoder_mode {encoder_mode!r}")
    encoder_kwargs: dict[str, Any] = {"d_model": d_model}
    if "base_filters" in config and encoder_cls is not SmallCNNEncoder:
        encoder_kwargs["base_filters"] = int(config["base_filters"])
    if encoder_cls is RotInvariantResNetEncoder and "recompute_grad" in config:
        encoder_kwargs["recompute_grad"] = bool(config["recompute_grad"])
    encoder_name = "small_cnn_encoder" if encoder_cls is SmallCNNEncoder else "rotinv_encoder" if encoder_cls is RotInvariantResNetEncoder else "resnet_encoder"
    cnn_tokens = encoder_cls((image_size, image_size, 3), name=encoder_name, **encoder_kwargs)(
        _image_branch(images, config)
    )
    query_vec, query_tok = TabularQueryBuilder(
        d_model=d_model,
        num_surveys=int(config["num_surveys"]),
        num_filters=int(config["num_filters"]),
        use_context_embeddings=bool(config.get("use_survey_filter_token", True)),
        name="tabular_query_builder",
    )([tabular, survey_id, filter_id])
    fusion_mode = str(config.get("fusion_mode", "cross_attention"))
    if fusion_mode == "cross_attention":
        fused = CrossAttentionFusion(
            d_model=d_model,
            n_heads=int(config["n_heads"]),
            ff_dim=int(config["ff_dim"]),
            dropout=float(config.get("dropout", 0.1)),
            name="cross_attention_fusion",
        )([query_vec, query_tok, cnn_tokens])
    elif fusion_mode == "gap_late_concat":
        fused = GAPLateConcatFusion(
            d_model=d_model,
            ff_dim=int(config["ff_dim"]),
            dropout=float(config.get("dropout", 0.1)),
            name="gap_late_concat_fusion",
        )([query_vec, cnn_tokens])
    elif fusion_mode == "tabular_only":
        fused = TabularOnlyFusion(
            d_model=d_model,
            ff_dim=int(config["ff_dim"]),
            dropout=float(config.get("dropout", 0.1)),
            name="tabular_only_fusion",
        )([query_vec, cnn_tokens])
    elif fusion_mode == "image_only":
        fused = ImageOnlyFusion(
            d_model=d_model,
            ff_dim=int(config["ff_dim"]),
            dropout=float(config.get("dropout", 0.1)),
            name="image_only_fusion",
        )([query_vec, cnn_tokens])
    elif fusion_mode == "gated":
        fused = GatedFusion(
            d_model=d_model,
            ff_dim=int(config["ff_dim"]),
            dropout=float(config.get("dropout", 0.1)),
            name="gated_fusion",
        )([query_vec, cnn_tokens])
    else:
        raise ValueError(f"Unsupported fusion_mode {fusion_mode!r}")
    return [images, tabular, survey_id, filter_id], fused


def build_rb_model(config: str | Path | dict[str, Any]) -> keras.Model:
    c = _cfg(config)
    inputs, fused = _trunk(c)
    out = build_rb_head(fused, hidden_dim=int(c["head_hidden_dim"]), dropout=float(c["dropout"]))
    return keras.Model(inputs=inputs, outputs={"rb": out}, name="rb_model")
