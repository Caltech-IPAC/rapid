"""CNN spatial encoders for Phase 2 classification models."""

from __future__ import annotations

from typing import Any

import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers

from classification import gpu_config  # noqa: F401


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class ResidualBlock(layers.Layer):
    """Small pre-activation residual block used by the CNN encoder."""

    def __init__(self, filters: int, strides: int = 1, name: str | None = None, **kwargs: Any):
        super().__init__(name=name, **kwargs)
        self.filters = int(filters)
        self.strides = int(strides)
        self.conv1 = layers.Conv2D(self.filters, 3, strides=self.strides, padding="same", use_bias=False)
        self.bn1 = layers.BatchNormalization()
        self.act1 = layers.Activation("gelu")
        self.conv2 = layers.Conv2D(self.filters, 3, padding="same", use_bias=False)
        self.bn2 = layers.BatchNormalization()
        self.skip = None
        self.out_act = layers.Activation("gelu")

    def build(self, input_shape):
        channels = int(input_shape[-1])
        if channels != self.filters or self.strides != 1:
            self.skip = layers.Conv2D(self.filters, 1, strides=self.strides, padding="same", use_bias=False)
        super().build(input_shape)

    def call(self, inputs, training: bool | None = None):
        x = self.conv1(inputs)
        x = self.bn1(x, training=training)
        x = self.act1(x)
        x = self.conv2(x)
        x = self.bn2(x, training=training)
        shortcut = self.skip(inputs) if self.skip is not None else inputs
        return self.out_act(x + shortcut)

    def get_config(self) -> dict[str, Any]:
        config = super().get_config()
        config.update({"filters": self.filters, "strides": self.strides})
        return config


def _build_resnet_spatial_encoder(
    input_shape: tuple[int, int, int],
    *,
    base_filters: int = 32,
) -> keras.Model:
    """Build a shared CNN that maps 64/128 px images to an 8x8x(4*base_filters) map."""
    bf = int(base_filters)
    inputs = keras.Input(shape=input_shape, name="encoder_image")
    x = layers.Conv2D(bf, 3, padding="same", use_bias=False, name="stem_conv")(inputs)
    x = layers.BatchNormalization(name="stem_bn")(x)
    x = layers.Activation("gelu", name="stem_gelu")(x)

    x = ResidualBlock(bf, strides=1, name="res_block_0")(x)
    x = ResidualBlock(2 * bf, strides=2, name="res_block_1")(x)
    x = ResidualBlock(2 * bf, strides=1, name="res_block_2")(x)
    x = ResidualBlock(4 * bf, strides=2, name="res_block_3")(x)
    if int(input_shape[0]) == 128:
        x = ResidualBlock(4 * bf, strides=2, name="res_block_extra_128")(x)
    x = ResidualBlock(4 * bf, strides=2, name="final_residual_block")(x)
    return keras.Model(inputs, x, name="resnet_spatial_encoder")


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class RotInvariantResNetEncoder(layers.Layer):
    """Shared four-rotation CNN wrapper returning 64 spatial tokens.

    The shared CNN is evaluated on k=0..3 rotations. Branch feature maps are
    counter-rotated into the canonical frame and averaged. A second four-way
    spatial symmetrization keeps the exposed token tensor invariant to 90
    degree input rotations while preserving an 8x8 token grid for attention.
    """

    def __init__(
        self,
        input_shape: tuple[int, int, int],
        d_model: int,
        base_filters: int = 32,
        recompute_grad: bool = True,
        name: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(name=name, **kwargs)
        self.input_shape_spec = tuple(int(v) for v in input_shape)
        self.d_model = int(d_model)
        self.base_filters = int(base_filters)
        self.token_channels = 4 * self.base_filters
        self.recompute_grad = bool(recompute_grad)
        self.spatial_encoder = _build_resnet_spatial_encoder(self.input_shape_spec, base_filters=self.base_filters)
        self.cnn_projection = layers.Dense(self.d_model, use_bias=False, name="cnn_projection")

    def _encode_once(self, x, training: bool | None = None):
        return self.spatial_encoder(x, training=training)

    def build(self, input_shape):
        self.spatial_encoder.build((None, *self.input_shape_spec))
        self.cnn_projection.build((None, 64, self.token_channels))
        super().build(input_shape)

    def call(self, inputs, training: bool | None = None):
        def branch(k: int):
            xk = tf.image.rot90(inputs, k=k)
            yk = self._encode_once(xk, training=training)
            return tf.image.rot90(yk, k=(4 - k) % 4)

        maps = [branch(k) for k in range(4)]
        fmap = tf.add_n(maps) / 4.0
        # Convert the equivariant canonical map to a strictly 90-degree
        # invariant token grid. This makes saved encoder behavior deterministic
        # for direct encoder use and for downstream factories.
        fmap = tf.add_n([tf.image.rot90(fmap, k=k) for k in range(4)]) / 4.0
        tokens = tf.reshape(fmap, [tf.shape(fmap)[0], -1, self.token_channels])
        return self.cnn_projection(tokens)

    def get_config(self) -> dict[str, Any]:
        config = super().get_config()
        config.update(
            {
                "input_shape": self.input_shape_spec,
                "d_model": self.d_model,
                "base_filters": self.base_filters,
                "recompute_grad": self.recompute_grad,
            }
        )
        return config


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class ResNetEncoder(layers.Layer):
    """Non-rotation-invariant encoder ablation."""

    def __init__(
        self,
        input_shape: tuple[int, int, int],
        d_model: int,
        base_filters: int = 32,
        name: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(name=name, **kwargs)
        self.input_shape_spec = tuple(int(v) for v in input_shape)
        self.d_model = int(d_model)
        self.base_filters = int(base_filters)
        self.token_channels = 4 * self.base_filters
        self.spatial_encoder = _build_resnet_spatial_encoder(self.input_shape_spec, base_filters=self.base_filters)
        self.cnn_projection = layers.Dense(self.d_model, use_bias=False, name="cnn_projection")

    def call(self, inputs, training: bool | None = None):
        fmap = self.spatial_encoder(inputs, training=training)
        tokens = tf.reshape(fmap, [tf.shape(fmap)[0], -1, self.token_channels])
        return self.cnn_projection(tokens)

    def build(self, input_shape):
        self.spatial_encoder.build((None, *self.input_shape_spec))
        self.cnn_projection.build((None, 64, self.token_channels))
        super().build(input_shape)

    def get_config(self) -> dict[str, Any]:
        config = super().get_config()
        config.update(
            {"input_shape": self.input_shape_spec, "d_model": self.d_model, "base_filters": self.base_filters}
        )
        return config


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class SmallCNNEncoder(layers.Layer):
    """Conventional three-block CNN baseline returning an 8 by 8 token grid."""

    def __init__(self, input_shape: tuple[int, int, int], d_model: int, name: str | None = None, **kwargs: Any):
        super().__init__(name=name, **kwargs)
        self.input_shape_spec = tuple(int(v) for v in input_shape)
        self.d_model = int(d_model)
        self.convs = [layers.Conv2D(filters, 3, padding="same", activation="gelu", name=f"small_conv_{i}")
                      for i, filters in enumerate((16, 32, 64))]
        self.pools = [layers.MaxPool2D(2, name=f"small_pool_{i}") for i in range(3)]
        self.projection = layers.Dense(self.d_model, use_bias=False, name="small_cnn_projection")

    def call(self, inputs, training: bool | None = None):
        x = inputs
        for conv, pool in zip(self.convs, self.pools):
            x = pool(conv(x))
        tokens = tf.reshape(x, [tf.shape(x)[0], -1, 64])
        return self.projection(tokens)

    def get_config(self) -> dict[str, Any]:
        return {**super().get_config(), "input_shape": self.input_shape_spec, "d_model": self.d_model}
