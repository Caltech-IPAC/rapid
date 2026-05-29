"""
Domain Adversarial Neural Network (DANN) Model Architecture

This module defines the DANN architecture for transient detection with domain adaptation.
"""

import tensorflow as tf
from tensorflow.keras import layers, Model
from model.layers import (
    rot90_k1,
    rot90_k2,
    rot90_k3,
    gradient_reversal,
    GradientReversalLayer,
    image_encoder,
)


def create_dann_model(img_shape, num_features, lambda_domain=1.0):
    """
    Create a Domain Adversarial Neural Network model.
    
    Args:
        img_shape (tuple): Shape of input images (H, W, C)
        num_features (int): Number of tabular features
        lambda_domain (float): Weight for gradient reversal layer
    
    Returns:
        tf.keras.Model: DANN model with multiple outputs
    """
    # Inputs
    img_input = layers.Input(shape=img_shape, name="image_input")
    feat_input = layers.Input(shape=(num_features,), name="feature_input")
    
    # Feature extractor (shared)
    img_features = image_encoder(img_input, img_shape, mode="mean")
    feat_features = layers.Dense(32, activation="relu", name="feat_encoder")(feat_input)
    
    # Combine features
    combined_features = layers.Concatenate(name="combined_features")([img_features, feat_features])
    
    # Label classifier (for transient detection)
    label_classifier = layers.Dense(128, activation="relu", name="label_fc1")(combined_features)
    label_classifier = layers.Dropout(0.3)(label_classifier)
    label_classifier = layers.Dense(64, activation="relu", name="label_fc2")(label_classifier)
    label_output = layers.Dense(1, activation="sigmoid", name="label_output")(label_classifier)
    
    # Domain classifier (for domain adaptation)
    # Apply gradient reversal layer
    domain_features = GradientReversalLayer(lambda_=lambda_domain, name="gradient_reversal")(combined_features)
    domain_classifier = layers.Dense(128, activation="relu", name="domain_fc1")(domain_features)
    domain_classifier = layers.Dropout(0.3)(domain_classifier)
    domain_classifier = layers.Dense(64, activation="relu", name="domain_fc2")(domain_classifier)
    domain_output = layers.Dense(1, activation="sigmoid", name="domain_output")(domain_classifier)
    
    # Create model with multiple outputs
    model = Model(
        inputs=[img_input, feat_input],
        outputs=[label_output, domain_output],
        name="DANN"
    )
    
    # Compile with multiple losses
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=0.001),
        loss={
            "label_output": "binary_crossentropy",
            "domain_output": "binary_crossentropy"
        },
        loss_weights={
            "label_output": 1.0,
            "domain_output": 1.0
        },
        metrics={
            "label_output": ["accuracy", "precision", "recall"],
            "domain_output": ["accuracy"]
        }
    )
    
    return model
