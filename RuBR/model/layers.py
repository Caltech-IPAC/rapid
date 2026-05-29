import tensorflow as tf
from tensorflow.keras import layers, Model


def rot90_k1(x):
    return tf.image.rot90(x, k=1)


def rot90_k2(x):
    return tf.image.rot90(x, k=2)


def rot90_k3(x):
    return tf.image.rot90(x, k=3)


@tf.custom_gradient
def gradient_reversal(x, lambda_):
    def grad(dy):
        return -lambda_ * dy, None

    return x, grad


class GradientReversalLayer(layers.Layer):
    def __init__(self, lambda_=1.0, **kwargs):
        super().__init__(**kwargs)
        self.lambda_ = lambda_

    def call(self, x):
        return gradient_reversal(x, self.lambda_)

    def get_config(self):
        config = super().get_config()
        config.update({"lambda_": self.lambda_})
        return config


def image_encoder(
    inputs,
    img_shape,
    mode="mean",
    conv_kernel_initializer=None,
    dense_kernel_initializer=None,
):
    x1 = inputs
    x2 = layers.Lambda(rot90_k1, output_shape=img_shape)(inputs)
    x3 = layers.Lambda(rot90_k2, output_shape=img_shape)(inputs)
    x4 = layers.Lambda(rot90_k3, output_shape=img_shape)(inputs)

    enc_input = layers.Input(shape=img_shape)

    conv_kwargs = {"kernel_size": 3, "padding": "same"}
    if conv_kernel_initializer is not None:
        conv_kwargs["kernel_initializer"] = conv_kernel_initializer

    x = layers.Conv2D(32, **conv_kwargs)(enc_input)
    x = layers.BatchNormalization()(x)
    x = layers.ReLU()(x)
    x = layers.MaxPooling2D(pool_size=2, strides=2)(x)

    x = layers.Conv2D(64, **conv_kwargs)(x)
    x = layers.BatchNormalization()(x)
    x = layers.ReLU()(x)
    x = layers.MaxPooling2D(pool_size=2, strides=2)(x)

    x = layers.Conv2D(128, **conv_kwargs)(x)
    x = layers.BatchNormalization()(x)
    x = layers.ReLU()(x)
    x = layers.MaxPooling2D(pool_size=2, strides=2)(x)
    x = layers.Flatten()(x)

    dense_units = x.shape[-1] // 2
    dense_kwargs = {"activation": "relu"}
    if dense_kernel_initializer is not None:
        dense_kwargs["kernel_initializer"] = dense_kernel_initializer
    x = layers.Dense(dense_units, **dense_kwargs)(x)

    encoder = Model(enc_input, x, name="shared_encoder")

    e1 = encoder(x1)
    e2 = encoder(x2)
    e3 = encoder(x3)
    e4 = encoder(x4)

    if mode == "concat":
        return layers.Concatenate(axis=1)([e1, e2, e3, e4])
    return layers.Average()([e1, e2, e3, e4])
