"""Loss helpers for CNN classification training."""

from __future__ import annotations

import tensorflow as tf
from tensorflow import keras

from classification import gpu_config  # noqa: F401


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class FocalBinaryCrossentropy(keras.losses.Loss):
    """Binary focal cross-entropy for the RB head."""

    def __init__(self, gamma: float = 2.0, alpha: float | None = None, name: str = "focal_binary_crossentropy"):
        super().__init__(name=name)
        self.gamma = float(gamma)
        self.alpha = None if alpha is None else float(alpha)

    def call(self, y_true, y_pred):
        y_true_f = tf.cast(tf.reshape(y_true, tf.shape(y_pred)), tf.float32)
        y_pred_f = tf.clip_by_value(tf.cast(y_pred, tf.float32), keras.backend.epsilon(), 1.0 - keras.backend.epsilon())
        ce = -(y_true_f * tf.math.log(y_pred_f) + (1.0 - y_true_f) * tf.math.log(1.0 - y_pred_f))
        p_t = y_true_f * y_pred_f + (1.0 - y_true_f) * (1.0 - y_pred_f)
        mod = tf.pow(1.0 - p_t, self.gamma)
        if self.alpha is not None:
            alpha_t = y_true_f * self.alpha + (1.0 - y_true_f) * (1.0 - self.alpha)
            mod = mod * alpha_t
        return tf.reduce_mean(mod * ce)

    def get_config(self):
        config = super().get_config()
        config.update({"gamma": self.gamma, "alpha": self.alpha})
        return config


def focal_bce(gamma: float = 2.0, alpha: float | None = None):
    return FocalBinaryCrossentropy(gamma=gamma, alpha=alpha)


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class WeightedSparseCategoricalCrossentropy(keras.losses.Loss):
    """Sparse categorical CE with per-class weights."""

    def __init__(
        self, class_weights: dict[int, float] | list[float], name: str = "weighted_sparse_categorical_crossentropy"
    ):
        super().__init__(name=name)
        if isinstance(class_weights, dict):
            max_key = max(int(k) for k in class_weights)
            self.class_weights = [float(class_weights.get(i, 1.0)) for i in range(max_key + 1)]
        else:
            self.class_weights = [float(v) for v in class_weights]
        self._weights = tf.constant(self.class_weights, dtype=tf.float32)
        self._ce = keras.losses.SparseCategoricalCrossentropy(reduction="none")

    def call(self, y_true, y_pred):
        y = tf.cast(y_true, tf.int32)
        sample_weights = tf.gather(self._weights, y)
        return tf.reduce_mean(self._ce(y, y_pred) * sample_weights)

    def get_config(self):
        config = super().get_config()
        config.update({"class_weights": self.class_weights})
        return config


def weighted_sparse_categorical_crossentropy(class_weights: dict[int, float] | list[float]):
    return WeightedSparseCategoricalCrossentropy(class_weights)


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class SparseLabelSmoothingCrossentropy(keras.losses.Loss):
    """Sparse categorical cross-entropy with configurable label smoothing."""

    def __init__(self, num_classes: int, label_smoothing: float = 0.0, name: str = "sparse_label_smoothing_ce"):
        super().__init__(name=name)
        self.num_classes = int(num_classes)
        self.label_smoothing = float(label_smoothing)
        if self.num_classes < 2:
            raise ValueError("num_classes must be at least two")
        if not 0.0 <= self.label_smoothing < 1.0:
            raise ValueError("label_smoothing must be in [0, 1)")

    def call(self, y_true, y_pred):
        labels = tf.one_hot(tf.cast(y_true, tf.int32), depth=self.num_classes, dtype=tf.float32)
        return keras.losses.categorical_crossentropy(labels, y_pred, label_smoothing=self.label_smoothing)

    def get_config(self):
        return {
            **super().get_config(),
            "num_classes": self.num_classes,
            "label_smoothing": self.label_smoothing,
        }


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class MaskedSparseCategoricalCrossentropy(keras.losses.Loss):
    """Sparse categorical CE that ignores rows with y == ignore_label."""

    def __init__(self, ignore_label: int = -1, name: str = "masked_sparse_categorical_crossentropy"):
        super().__init__(name=name)
        self.ignore_label = int(ignore_label)
        self._ce = keras.losses.SparseCategoricalCrossentropy(reduction="none")

    def call(self, y_true, y_pred):
        y = tf.cast(y_true, tf.int32)
        mask = tf.not_equal(y, self.ignore_label)
        safe_y = tf.where(mask, y, tf.zeros_like(y))
        vals = self._ce(safe_y, y_pred)
        vals = tf.boolean_mask(vals, mask)
        return tf.cond(tf.size(vals) > 0, lambda: tf.reduce_mean(vals), lambda: tf.constant(0.0, dtype=tf.float32))

    def get_config(self):
        config = super().get_config()
        config.update({"ignore_label": self.ignore_label})
        return config


def masked_sparse_categorical_crossentropy(ignore_label: int = -1):
    return MaskedSparseCategoricalCrossentropy(ignore_label=ignore_label)
