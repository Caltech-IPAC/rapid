"""
Control Training Script for Transient Detection

This script trains a standard CNN model (without domain adversarial training)
on the source domain (e.g., Open Universe) to serve as a baseline for comparison
with domain adversarial approaches.

This provides a control to measure the improvement gained from domain adaptation.
"""

import gc
import argparse
import os
import tensorflow as tf
from tensorflow.keras import layers, Model
import numpy as np
from sklearn.metrics import confusion_matrix, classification_report
from matplotlib import pyplot as plt
from sklearn.model_selection import train_test_split
from model.data import load_dataset
from model.layers import image_encoder
from model.callbacks import F1EarlyStopping

# Set random seeds for reproducibility
np.random.seed(42)
tf.random.set_seed(42)

def load_data(data_path):
    """
    Load the dataset from the specified path.

    Args:
        data_path (str): Path to the dataset file (.npz file).

    Returns:
        tuple: X, feats, y, metadata as numpy arrays.
    """
    return load_dataset(data_path, mmap=False, allow_npy_dict=False)


def create_standard_model(img_shape, num_features):
    """
    Create a standard CNN model for transient detection (no domain adaptation).
    
    Args:
        img_shape (tuple): Shape of input images (H, W, C)
        num_features (int): Number of tabular features
    
    Returns:
        tf.keras.Model: Standard classification model
    """
    # Inputs
    img_input = layers.Input(shape=img_shape, name="image_input")
    feat_input = layers.Input(shape=(num_features,), name="feature_input")
    
    # Feature extraction
    img_features = image_encoder(img_input, img_shape, mode="mean")
    feat_features = layers.Dense(32, activation="relu", name="feat_encoder")(feat_input)
    
    # Combine features
    combined_features = layers.Concatenate(name="combined_features")([img_features, feat_features])
    
    # Classification head
    x = layers.Dense(128, activation="relu", name="fc1")(combined_features)
    x = layers.Dropout(0.3)(x)
    x = layers.Dense(64, activation="relu", name="fc2")(x)
    output = layers.Dense(1, activation="sigmoid", name="output")(x)
    
    # Create model
    model = Model(
        inputs=[img_input, feat_input],
        outputs=output,
        name="StandardClassifier"
    )
    
    # Compile
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=0.001),
        loss="binary_crossentropy",
        metrics=["accuracy", "precision", "recall"]
    )
    
    return model


def plot_training_history(history, output_dir):
    """Plot and save training history."""
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    # Accuracy
    axes[0, 0].plot(history.history["accuracy"], label="Train")
    axes[0, 0].plot(history.history["val_accuracy"], label="Val")
    axes[0, 0].set_title("Accuracy")
    axes[0, 0].set_xlabel("Epoch")
    axes[0, 0].set_ylabel("Accuracy")
    axes[0, 0].legend()
    axes[0, 0].grid(True)
    
    # Loss
    axes[0, 1].plot(history.history["loss"], label="Train")
    axes[0, 1].plot(history.history["val_loss"], label="Val")
    axes[0, 1].set_title("Loss")
    axes[0, 1].set_xlabel("Epoch")
    axes[0, 1].set_ylabel("Loss")
    axes[0, 1].legend()
    axes[0, 1].grid(True)
    
    # Precision
    axes[1, 0].plot(history.history["precision"], label="Train")
    axes[1, 0].plot(history.history["val_precision"], label="Val")
    axes[1, 0].set_title("Precision")
    axes[1, 0].set_xlabel("Epoch")
    axes[1, 0].set_ylabel("Precision")
    axes[1, 0].legend()
    axes[1, 0].grid(True)
    
    # Recall
    axes[1, 1].plot(history.history["recall"], label="Train")
    axes[1, 1].plot(history.history["val_recall"], label="Val")
    axes[1, 1].set_title("Recall")
    axes[1, 1].set_xlabel("Epoch")
    axes[1, 1].set_ylabel("Recall")
    axes[1, 1].legend()
    axes[1, 1].grid(True)
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "training_history.png"), dpi=300, bbox_inches="tight")
    plt.close()


def main():
    parser = argparse.ArgumentParser(
        description="Train standard model (control) for transient detection"
    )

    # Data arguments
    parser.add_argument(
        "--train_data",
        type=str,
        required=True,
        help="Path to training data (.npz file)",
    )
    parser.add_argument(
        "--max_samples",
        type=int,
        default=None,
        help="Maximum number of training samples (default: use all)",
    )

    # Training arguments
    parser.add_argument(
        "--epochs", 
        type=int, 
        default=60, 
        help="Number of training epochs (default: 60)"
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=256,
        help="Batch size for training (default: 512)",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=0.005,
        help="Learning rate for Adam optimizer (default: 0.001)",
    )

    # Data split arguments
    parser.add_argument(
        "--val_size",
        type=float,
        default=0.15,
        help="Validation set size fraction (default: 0.15)",
    )
    parser.add_argument(
        "--random_state",
        type=int,
        default=42,
        help="Random state for reproducible splits (default: 42)",
    )

    # Model arguments
    parser.add_argument(
        "--patience", 
        type=int, 
        default=10, 
        help="Early stopping patience (default: 10)"
    )
    parser.add_argument(
        "--class_weight_pos",
        type=float,
        default=2.0,
        help="Class weight for positive class (default: 2.0)",
    )

    # Output arguments
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./control_output_snr",
        help="Directory to save model and results (default: ./control_output)",
    )

    # GPU arguments
    parser.add_argument(
        "--gpu",
        type=int,
        default=None,
        help="GPU device ID to use (default: auto-select)",
    )

    args = parser.parse_args()

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)

    # Set GPU device if specified
    if args.gpu is not None:
        physical_devices = tf.config.experimental.list_physical_devices("GPU")
        if physical_devices:
            tf.config.experimental.set_visible_devices(
                physical_devices[args.gpu], "GPU"
            )
            tf.config.experimental.set_memory_growth(physical_devices[args.gpu], True)

    print("=" * 80)
    print("STANDARD MODEL TRAINING (CONTROL)")
    print("=" * 80)
    print(f"Training data: {args.train_data}")
    print(f"Output directory: {args.output_dir}")
    print(f"Epochs: {args.epochs}")
    print(f"Batch size: {args.batch_size}")
    print(f"Learning rate: {args.learning_rate}")
    print("=" * 80)

    # Load training data
    print("\n--- Loading Training Data ---")
    X, feats, y, metadata = load_data(args.train_data)
    
    # Limit samples if specified
    if args.max_samples is not None and len(X) > args.max_samples:
        print(f"Limiting to {args.max_samples} samples")
        indices = np.random.choice(len(X), args.max_samples, replace=False)
        X = X[indices]
        feats = feats[indices]
        y = y[indices]
        metadata = metadata[indices]
    
    # Remove NaN values
    mask = np.isnan(X).any(axis=(1, 2, 3)) | np.isnan(feats).any(axis=1)
    if mask.any():
        print(f"Removing {mask.sum()} samples with NaN values")
        X = X[~mask]
        feats = feats[~mask]
        y = y[~mask]
        metadata = metadata[~mask]
    
    # Normalize data
    print("\nNormalizing data...")
    X = (X - X.mean(axis=(0, 1, 2), keepdims=True)) / (X.std(axis=(0, 1, 2), keepdims=True) + 1e-6)
    feats = (feats - feats.mean(axis=0)) / (feats.std(axis=0) + 1e-6)
    
    print(f"\nProcessed data shapes:")
    print(f"X: {X.shape}, feats: {feats.shape}, y: {y.shape}")
    print(f"Class distribution: {np.bincount(y.astype(int))}")
    
    # Split into train and validation
    print(f"\nSplitting data (val_size={args.val_size})...")
    X_train, X_val, feats_train, feats_val, y_train, y_val = train_test_split(
        X, feats, y,
        test_size=args.val_size,
        stratify=y,
        random_state=args.random_state,
        shuffle=True
    )
    
    print(f"Train set: {len(X_train)} samples")
    print(f"Validation set: {len(X_val)} samples")
    print(f"Train class distribution: {np.bincount(y_train.astype(int))}")
    print(f"Val class distribution: {np.bincount(y_val.astype(int))}")
    
    # Clean up memory
    del X, feats, y
    gc.collect()
    
    # Create model
    img_shape = X_train[0].shape
    num_features = feats_train.shape[1]
    
    print(f"\nCreating standard model...")
    print(f"Image shape: {img_shape}")
    print(f"Number of features: {num_features}")
    
    model = create_standard_model(img_shape, num_features)
    model.summary()
    
    # Setup callbacks
    early_stopping = F1EarlyStopping(
        precision_key="val_precision",
        recall_key="val_recall",
        patience=args.patience,
        restore_best_weights=True,
    )
    
    callbacks = [
        early_stopping,
        tf.keras.callbacks.ModelCheckpoint(
            os.path.join(args.output_dir, "best_model.h5"),
            save_best_only=True,
            monitor="val_recall",
            mode="max",
            verbose=1
        ),
        tf.keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss", factor=0.5, patience=5, min_lr=1e-6, verbose=1
        ),
    ]
    
    # Sample weights (handle class imbalance)
    sample_weights = np.ones(len(y_train))
    sample_weights[y_train == 1] = args.class_weight_pos
    
    print(f"\nClass weight for positive samples: {args.class_weight_pos}")
    
    # Train model
    print(f"\nStarting training...")
    history = model.fit(
        [X_train, feats_train],
        y_train,
        epochs=args.epochs,
        batch_size=args.batch_size,
        validation_data=([X_val, feats_val], y_val),
        callbacks=callbacks,
        sample_weight=sample_weights,
        verbose=1
    )
    
    # Save final model
    final_model_path = os.path.join(args.output_dir, "final_model.h5")
    model.save(final_model_path)
    print(f"\nFinal model saved to: {final_model_path}")
    
    # Plot training history
    plot_training_history(history, args.output_dir)
    
    # Evaluate on validation set
    print(f"\nEvaluating model on validation set...")
    results = model.evaluate([X_val, feats_val], y_val, verbose=0)
    
    print(f"\nValidation Results:")
    for name, value in zip(model.metrics_names, results):
        print(f"{name}: {value:.4f}")
    
    # Make predictions
    y_pred_prob = model.predict([X_val, feats_val], verbose=0).flatten()
    y_pred = (y_pred_prob > 0.5).astype(int)
    
    # Classification report
    print(f"\nClassification Report:")
    print(classification_report(y_val, y_pred))
    
    # Confusion matrix
    cm = confusion_matrix(y_val, y_pred)
    print(f"\nConfusion Matrix:")
    print(cm)
    
    # Calculate F1 score
    tn, fp, fn, tp = cm.ravel()
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    print(f"\nF1 Score: {f1:.4f}")
    
    # Save results
    results_path = os.path.join(args.output_dir, "training_results.txt")
    with open(results_path, "w") as f:
        f.write("CONTROL MODEL TRAINING RESULTS\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"Training data: {args.train_data}\n\n")
        f.write("Validation Results:\n")
        for name, value in zip(model.metrics_names, results):
            f.write(f"{name}: {value:.4f}\n")
        f.write(f"\nF1 Score: {f1:.4f}\n")
        f.write(f"\nClassification Report:\n")
        f.write(classification_report(y_val, y_pred))
        f.write(f"\nConfusion Matrix:\n{cm}\n")
    
    print(f"\nResults saved to: {results_path}")
    print(f"\nTraining completed successfully!")
    print(f"All outputs saved to: {args.output_dir}")


if __name__ == "__main__":
    main()
