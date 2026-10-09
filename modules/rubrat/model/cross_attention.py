"""Tabular-conditioned cross-attention fusion layers."""

from __future__ import annotations

from typing import Any

import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers

from classification import gpu_config  # noqa: F401


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class TabularQueryBuilder(layers.Layer):
    """Build one tabular query token with survey/filter conditioning."""

    def __init__(
        self,
        d_model: int,
        num_surveys: int = 2,
        num_filters: int = 16,
        use_context_embeddings: bool = True,
        name: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(name=name, **kwargs)
        self.d_model = int(d_model)
        self.num_surveys = int(num_surveys)
        self.num_filters = int(num_filters)
        self.use_context_embeddings = bool(use_context_embeddings)
        self.tabular_dense = layers.Dense(self.d_model, name="tabular_projection")
        self.norm = layers.LayerNormalization(name="tabular_norm")
        self.survey_embedding = (
            layers.Embedding(self.num_surveys, self.d_model, name="survey_embedding")
            if self.use_context_embeddings
            else None
        )
        self.filter_embedding = (
            layers.Embedding(self.num_filters, self.d_model, name="filter_embedding")
            if self.use_context_embeddings
            else None
        )

    def call(self, inputs, training: bool | None = None):
        tabular, survey_id, filter_id = inputs
        query_vec = self.tabular_dense(tabular)
        query_vec = self.norm(query_vec)
        if self.use_context_embeddings:
            survey_vec = self.survey_embedding(tf.cast(survey_id, tf.int32))
            filter_vec = self.filter_embedding(tf.cast(filter_id, tf.int32))
            query_vec = query_vec + survey_vec + filter_vec
        else:
            # Keep scalar inputs connected without allowing them to carry signal.
            zero = tf.cast(tf.reduce_sum(survey_id) + tf.reduce_sum(filter_id), query_vec.dtype) * 0.0
            query_vec = query_vec + zero
        query_tok = tf.expand_dims(query_vec, axis=1)
        return query_vec, query_tok

    def build(self, input_shape):
        tabular_shape = input_shape[0]
        self.tabular_dense.build(tabular_shape)
        self.norm.build((tabular_shape[0], self.d_model))
        if self.use_context_embeddings:
            self.survey_embedding.build(input_shape[1])
            self.filter_embedding.build(input_shape[2])
        super().build(input_shape)

    def get_config(self) -> dict[str, Any]:
        config = super().get_config()
        config.update(
            {
                "d_model": self.d_model,
                "num_surveys": self.num_surveys,
                "num_filters": self.num_filters,
                "use_context_embeddings": self.use_context_embeddings,
            }
        )
        return config


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class CrossAttentionFusion(layers.Layer):
    """Fuse a tabular query token with CNN spatial key/value tokens."""

    def __init__(
        self,
        d_model: int,
        n_heads: int,
        ff_dim: int,
        dropout: float = 0.1,
        name: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(name=name, **kwargs)
        self.d_model = int(d_model)
        self.n_heads = int(n_heads)
        self.ff_dim = int(ff_dim)
        self.dropout = float(dropout)
        self.attn = layers.MultiHeadAttention(
            num_heads=self.n_heads,
            key_dim=max(1, self.d_model // self.n_heads),
            dropout=self.dropout,
            name="cross_mha",
        )
        self.query_skip = layers.Dense(self.d_model, name="query_skip")
        self.image_pool = layers.GlobalAveragePooling1D(name="image_gap_skip")
        self.norm1 = layers.LayerNormalization(name="fusion_norm")
        self.ffn_dense = layers.Dense(self.ff_dim, activation="gelu", name="fusion_ffn_dense")
        self.ffn_dropout = layers.Dropout(self.dropout, name="fusion_ffn_dropout")
        self.ffn_out = layers.Dense(self.d_model, name="fusion_ffn_out")
        self.norm2 = layers.LayerNormalization(name="fusion_ffn_norm")

    def call(self, inputs, training: bool | None = None, return_attention_scores: bool = False):
        query_vec, query_tok, cnn_kv = inputs
        attended, scores = self.attn(
            query=query_tok,
            value=cnn_kv,
            key=cnn_kv,
            training=training,
            return_attention_scores=True,
        )
        attended = tf.squeeze(attended, axis=1)
        z = attended + self.query_skip(query_vec) + self.image_pool(cnn_kv)
        z = self.norm1(z)
        ffn = self.ffn_dense(z)
        ffn = self.ffn_dropout(ffn, training=training)
        ffn = self.ffn_out(ffn)
        z = self.norm2(z + ffn)
        if return_attention_scores:
            return z, scores
        return z

    def build(self, input_shape):
        query_vec_shape, query_tok_shape, cnn_kv_shape = input_shape
        self.attn.build(query_tok_shape, cnn_kv_shape, cnn_kv_shape)
        self.query_skip.build(query_vec_shape)
        self.image_pool.build(cnn_kv_shape)
        self.norm1.build((query_vec_shape[0], self.d_model))
        self.ffn_dense.build((query_vec_shape[0], self.d_model))
        self.ffn_dropout.build((query_vec_shape[0], self.ff_dim))
        self.ffn_out.build((query_vec_shape[0], self.ff_dim))
        self.norm2.build((query_vec_shape[0], self.d_model))
        super().build(input_shape)

    def get_config(self) -> dict[str, Any]:
        config = super().get_config()
        config.update(
            {
                "d_model": self.d_model,
                "n_heads": self.n_heads,
                "ff_dim": self.ff_dim,
                "dropout": self.dropout,
            }
        )
        return config


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class GAPLateConcatFusion(layers.Layer):
    """Ablation fusion: image GAP + late tabular concatenation, no attention."""

    def __init__(
        self,
        d_model: int,
        ff_dim: int,
        dropout: float = 0.1,
        name: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(name=name, **kwargs)
        self.d_model = int(d_model)
        self.ff_dim = int(ff_dim)
        self.dropout = float(dropout)
        self.image_pool = layers.GlobalAveragePooling1D(name="gap_late_image_pool")
        self.concat_projection = layers.Dense(self.d_model, activation="gelu", name="gap_late_projection")
        self.dropout_layer = layers.Dropout(self.dropout, name="gap_late_dropout")
        self.ffn = layers.Dense(self.ff_dim, activation="gelu", name="gap_late_ffn")
        self.out = layers.Dense(self.d_model, name="gap_late_out")
        self.norm = layers.LayerNormalization(name="gap_late_norm")

    def call(self, inputs, training: bool | None = None):
        query_vec, cnn_kv = inputs
        z = tf.concat([query_vec, self.image_pool(cnn_kv)], axis=-1)
        z = self.concat_projection(z)
        z = self.dropout_layer(z, training=training)
        ffn = self.ffn(z)
        return self.norm(z + self.out(ffn))

    def build(self, input_shape):
        query_shape, image_shape = input_shape
        pooled_shape = (query_shape[0], image_shape[-1])
        concat_shape = (query_shape[0], int(query_shape[-1]) + int(image_shape[-1]))
        self.image_pool.build(image_shape)
        self.concat_projection.build(concat_shape)
        self.dropout_layer.build((query_shape[0], self.d_model))
        self.ffn.build((query_shape[0], self.d_model))
        self.out.build((query_shape[0], self.ff_dim))
        self.norm.build((query_shape[0], self.d_model))
        del pooled_shape
        super().build(input_shape)

    def get_config(self) -> dict[str, Any]:
        config = super().get_config()
        config.update({"d_model": self.d_model, "ff_dim": self.ff_dim, "dropout": self.dropout})
        return config


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class TabularOnlyFusion(layers.Layer):
    """Ablation fusion that carries only tabular signal."""

    def __init__(
        self,
        d_model: int,
        ff_dim: int,
        dropout: float = 0.1,
        name: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(name=name, **kwargs)
        self.d_model = int(d_model)
        self.ff_dim = int(ff_dim)
        self.dropout = float(dropout)
        self.projection = layers.Dense(self.d_model, activation="gelu", name="tabular_only_projection")
        self.dropout_layer = layers.Dropout(self.dropout, name="tabular_only_dropout")
        self.ffn = layers.Dense(self.ff_dim, activation="gelu", name="tabular_only_ffn")
        self.out = layers.Dense(self.d_model, name="tabular_only_out")
        self.norm = layers.LayerNormalization(name="tabular_only_norm")

    def call(self, inputs, training: bool | None = None):
        query_vec, cnn_kv = inputs
        zero_image = tf.reduce_sum(cnn_kv, axis=[1, 2], keepdims=False) * 0.0
        z = self.projection(query_vec) + tf.expand_dims(zero_image, axis=-1)[:, : self.d_model] * 0.0
        z = self.dropout_layer(z, training=training)
        ffn = self.ffn(z)
        return self.norm(z + self.out(ffn))

    def build(self, input_shape):
        query_shape, _image_shape = input_shape
        self.projection.build(query_shape)
        self.dropout_layer.build((query_shape[0], self.d_model))
        self.ffn.build((query_shape[0], self.d_model))
        self.out.build((query_shape[0], self.ff_dim))
        self.norm.build((query_shape[0], self.d_model))
        super().build(input_shape)

    def get_config(self) -> dict[str, Any]:
        config = super().get_config()
        config.update({"d_model": self.d_model, "ff_dim": self.ff_dim, "dropout": self.dropout})
        return config


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class ImageOnlyFusion(layers.Layer):
    """Image-token baseline with no tabular or context signal."""

    def __init__(self, d_model: int, ff_dim: int, dropout: float = 0.1, name: str | None = None, **kwargs: Any):
        super().__init__(name=name, **kwargs)
        self.d_model = int(d_model)
        self.ff_dim = int(ff_dim)
        self.dropout = float(dropout)
        self.pool = layers.GlobalAveragePooling1D(name="image_only_pool")
        self.projection = layers.Dense(self.d_model, activation="gelu", name="image_only_projection")
        self.dropout_layer = layers.Dropout(self.dropout, name="image_only_dropout")
        self.ffn = layers.Dense(self.ff_dim, activation="gelu", name="image_only_ffn")
        self.out = layers.Dense(self.d_model, name="image_only_out")
        self.norm = layers.LayerNormalization(name="image_only_norm")

    def call(self, inputs, training: bool | None = None):
        query_vec, cnn_kv = inputs
        z = self.projection(self.pool(cnn_kv))
        z = z + tf.reduce_sum(query_vec, axis=-1, keepdims=True) * 0.0
        z = self.dropout_layer(z, training=training)
        return self.norm(z + self.out(self.ffn(z)))

    def get_config(self) -> dict[str, Any]:
        return {**super().get_config(), "d_model": self.d_model, "ff_dim": self.ff_dim, "dropout": self.dropout}


@keras.utils.register_keras_serializable(package="UnifiedRAPID")
class GatedFusion(layers.Layer):
    """Parameter-comparable sigmoid-gated image/tabular fusion baseline."""

    def __init__(self, d_model: int, ff_dim: int, dropout: float = 0.1, name: str | None = None, **kwargs: Any):
        super().__init__(name=name, **kwargs)
        self.d_model = int(d_model)
        self.ff_dim = int(ff_dim)
        self.dropout = float(dropout)
        self.pool = layers.GlobalAveragePooling1D(name="gated_image_pool")
        self.image_projection = layers.Dense(self.d_model, name="gated_image_projection")
        self.tabular_projection = layers.Dense(self.d_model, name="gated_tabular_projection")
        self.gate = layers.Dense(self.d_model, activation="sigmoid", name="gated_gate")
        self.dropout_layer = layers.Dropout(self.dropout, name="gated_dropout")
        self.ffn = layers.Dense(self.ff_dim, activation="gelu", name="gated_ffn")
        self.out = layers.Dense(self.d_model, name="gated_out")
        self.norm = layers.LayerNormalization(name="gated_norm")

    def call(self, inputs, training: bool | None = None):
        query_vec, cnn_kv = inputs
        image_vec = self.image_projection(self.pool(cnn_kv))
        tabular_vec = self.tabular_projection(query_vec)
        gate = self.gate(tf.concat([image_vec, tabular_vec], axis=-1))
        z = gate * image_vec + (1.0 - gate) * tabular_vec
        z = self.dropout_layer(z, training=training)
        return self.norm(z + self.out(self.ffn(z)))

    def get_config(self) -> dict[str, Any]:
        return {**super().get_config(), "d_model": self.d_model, "ff_dim": self.ff_dim, "dropout": self.dropout}
