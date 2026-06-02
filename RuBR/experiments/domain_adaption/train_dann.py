"""
Domain Adversarial Training for Transient Detection

This script implements Domain Adversarial Neural Networks (DANN) to learn
domain-invariant features for transient detection across different data domains.

Architecture:
- Feature Extractor: Shared encoder for both domains
- Label Classifier: Predicts transient/non-transient
- Domain Classifier: Predicts source/target domain (with gradient reversal)
"""

import gc
import argparse
import os
import tensorflow as tf
import numpy as np
from sklearn.metrics import confusion_matrix, classification_report
from matplotlib import pyplot as plt
from sklearn.model_selection import train_test_split
from model.data import load_dataset
from model.callbacks import F1EarlyStopping

# Import model components from flat package module
from model.dann_model import (
    rot90_k1,
    rot90_k2,
    rot90_k3,
    GradientReversalLayer,
    create_dann_model
)

# Set random seeds for reproducibility
np.random.seed(42)
tf.random.set_seed(42)

only_tp = True

def load_data(data_path):
    return load_dataset(data_path, mmap=True, allow_npy_dict=False)


class BatchMetricsLogger(tf.keras.callbacks.Callback):
    """Logs metrics at each batch for iteration-wise plotting."""
    
    def __init__(self):
        super(BatchMetricsLogger, self).__init__()
        self.batch_metrics = {
            'iteration': [],
            'label_output_loss': [],
            'domain_output_loss': [],
            'loss': []
        }
        self.current_iteration = 0
    
    def on_train_batch_end(self, batch, logs=None):
        self.current_iteration += 1
        self.batch_metrics['iteration'].append(self.current_iteration)
        self.batch_metrics['label_output_loss'].append(logs.get('label_output_loss', 0))
        self.batch_metrics['domain_output_loss'].append(logs.get('domain_output_loss', 0))
        self.batch_metrics['loss'].append(logs.get('loss', 0))


def plot_training_history(history, batch_metrics, output_dir):
    """Plot and save training history (epoch-wise and iteration-wise)."""
    
    # Create figure with more subplots to include iteration-wise plots
    fig = plt.figure(figsize=(20, 14))
    gs = fig.add_gridspec(3, 3, hspace=0.3, wspace=0.3)
    
    # Row 1: Label classifier metrics (epoch-wise)
    ax1 = fig.add_subplot(gs[0, 0])
    ax1.plot(history.history["label_output_accuracy"], label="Train")
    ax1.plot(history.history["val_label_output_accuracy"], label="Val")
    ax1.set_title("Label Accuracy (Epoch-wise)")
    ax1.set_xlabel("Epoch")
    ax1.set_ylabel("Accuracy")
    ax1.legend()
    ax1.grid(True)
    
    ax2 = fig.add_subplot(gs[0, 1])
    ax2.plot(history.history["label_output_loss"], label="Train")
    ax2.plot(history.history["val_label_output_loss"], label="Val")
    ax2.set_title("Label Loss (Epoch-wise)")
    ax2.set_xlabel("Epoch")
    ax2.set_ylabel("Loss")
    ax2.legend()
    ax2.grid(True)
    
    ax3 = fig.add_subplot(gs[0, 2])
    ax3.plot(history.history["label_output_precision"], label="Train Prec")
    ax3.plot(history.history["val_label_output_precision"], label="Val Prec")
    ax3.plot(history.history["label_output_recall"], label="Train Rec")
    ax3.plot(history.history["val_label_output_recall"], label="Val Rec")
    ax3.set_title("Label Precision/Recall (Epoch-wise)")
    ax3.set_xlabel("Epoch")
    ax3.set_ylabel("Score")
    ax3.legend()
    ax3.grid(True)
    
    # Row 2: Domain classifier metrics (epoch-wise)
    ax4 = fig.add_subplot(gs[1, 0])
    ax4.plot(history.history["domain_output_accuracy"], label="Train")
    ax4.plot(history.history["val_domain_output_accuracy"], label="Val")
    ax4.set_title("Domain Accuracy (Epoch-wise)")
    ax4.set_xlabel("Epoch")
    ax4.set_ylabel("Accuracy")
    ax4.legend()
    ax4.grid(True)
    
    ax5 = fig.add_subplot(gs[1, 1])
    ax5.plot(history.history["domain_output_loss"], label="Train")
    ax5.plot(history.history["val_domain_output_loss"], label="Val")
    ax5.set_title("Domain Loss (Epoch-wise)")
    ax5.set_xlabel("Epoch")
    ax5.set_ylabel("Loss")
    ax5.legend()
    ax5.grid(True)
    
    ax6 = fig.add_subplot(gs[1, 2])
    ax6.plot(history.history["loss"], label="Train Total")
    ax6.plot(history.history["val_loss"], label="Val Total")
    ax6.set_title("Total Loss (Epoch-wise)")
    ax6.set_xlabel("Epoch")
    ax6.set_ylabel("Loss")
    ax6.legend()
    ax6.grid(True)
    
    # Row 3: Iteration-wise metrics (from batch logger)
    if batch_metrics is not None and len(batch_metrics['iteration']) > 0:
        ax7 = fig.add_subplot(gs[2, 0])
        ax7.plot(batch_metrics['iteration'], batch_metrics['label_output_loss'], 
                linewidth=0.5, alpha=0.7)
        ax7.set_title("Label Loss (Iteration-wise)")
        ax7.set_xlabel("Iteration")
        ax7.set_ylabel("Loss")
        ax7.grid(True, alpha=0.3)
        
        ax8 = fig.add_subplot(gs[2, 1])
        ax8.plot(batch_metrics['iteration'], batch_metrics['domain_output_loss'], 
                linewidth=0.5, alpha=0.7)
        ax8.set_title("Domain Loss (Iteration-wise)")
        ax8.set_xlabel("Iteration")
        ax8.set_ylabel("Loss")
        ax8.grid(True, alpha=0.3)
        
        ax9 = fig.add_subplot(gs[2, 2])
        ax9.plot(batch_metrics['iteration'], batch_metrics['loss'], 
                linewidth=0.5, alpha=0.7)
        ax9.set_title("Total Loss (Iteration-wise)")
        ax9.set_xlabel("Iteration")
        ax9.set_ylabel("Loss")
        ax9.grid(True, alpha=0.3)
    
    plt.savefig(os.path.join(output_dir, "training_history.png"), dpi=300, bbox_inches="tight")
    plt.close()
    
    # Save batch metrics to file for later analysis
    if batch_metrics is not None and len(batch_metrics['iteration']) > 0:
        batch_metrics_path = os.path.join(output_dir, "batch_metrics.npz")
        np.savez(batch_metrics_path, **batch_metrics)
        print(f"Batch metrics saved to: {batch_metrics_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Train Domain Adversarial model for transient detection"
    )

    # Data arguments
    parser.add_argument(
        "--source_data",
        type=str,
        required=True,
        help="Path to source domain training data (.npz file)",
    )
    parser.add_argument(
        "--target_data",
        type=str,
        required=True,
        help="Path to target domain data (.npz file)",
    )
    parser.add_argument(
        "--max_samples",
        type=int,
        default=100_000,
        help="Maximum number of samples per domain (default: use all)",
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
        default=0.0025,
        help="Learning rate for Adam optimizer (default: 0.0025)",
    )
    parser.add_argument(
        "--lambda_domain",
        type=float,
        default=1.0,
        help="Weight for gradient reversal layer (default: 1.0)",
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
        help="Class weight for positive class in label classification (default: 8.0)",
    )

    # Output arguments
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./dann_output_tp_only",
        help="Directory to save model and results (default: ./dann_output_full)",
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
    print("DOMAIN ADVERSARIAL NEURAL NETWORK TRAINING")
    print("=" * 80)
    print(f"Source domain data: {args.source_data}")
    print(f"Target domain data: {args.target_data}")
    print(f"Output directory: {args.output_dir}")
    print(f"Epochs: {args.epochs}")
    print(f"Batch size: {args.batch_size}")
    print(f"Learning rate: {args.learning_rate}")
    print(f"Lambda (domain): {args.lambda_domain}")
    print("=" * 80)

    # Load source domain data
    print("\n--- Loading Source Domain Data ---")
    X_src, feats_src, y_src, metadata_src = load_data(args.source_data)
    
    # Load target domain data
    print("\n--- Loading Target Domain Data ---")
    X_tgt, feats_tgt, y_tgt, metadata_tgt = load_data(args.target_data)
    
    # Limit samples if specified
    if args.max_samples is not None:
        if len(X_src) > args.max_samples:
            print(f"Limiting source domain to {args.max_samples} samples")
            indices = np.random.choice(len(X_src), args.max_samples, replace=False)
            X_src = X_src[indices]
            feats_src = feats_src[indices]
            y_src = y_src[indices]
            metadata_src = metadata_src[indices]
        
        if len(X_tgt) > args.max_samples:
            print(f"Limiting target domain to {args.max_samples} samples")
            indices = np.random.choice(len(X_tgt), args.max_samples, replace=False)
            X_tgt = X_tgt[indices]
            feats_tgt = feats_tgt[indices]
            y_tgt = y_tgt[indices]
            metadata_tgt = metadata_tgt[indices]
    
    # Remove NaN values
    mask_src = np.isnan(X_src).any(axis=(1, 2, 3)) | np.isnan(feats_src).any(axis=1)
    if mask_src.any():
        print(f"Removing {mask_src.sum()} source samples with NaN values")
        X_src = X_src[~mask_src]
        feats_src = feats_src[~mask_src]
        y_src = y_src[~mask_src]
        metadata_src = metadata_src[~mask_src]
    
    mask_tgt = np.isnan(X_tgt).any(axis=(1, 2, 3)) | np.isnan(feats_tgt).any(axis=1)
    if mask_tgt.any():
        print(f"Removing {mask_tgt.sum()} target samples with NaN values")
        X_tgt = X_tgt[~mask_tgt]
        feats_tgt = feats_tgt[~mask_tgt]
        y_tgt = y_tgt[~mask_tgt]
        metadata_tgt = metadata_tgt[~mask_tgt]
    
    # Normalize data
    print("\nNormalizing data...")
    X_src = (X_src - X_src.mean(axis=(0, 1, 2), keepdims=True)) / (X_src.std(axis=(0, 1, 2), keepdims=True) + 1e-6)
    X_tgt = (X_tgt - X_tgt.mean(axis=(0, 1, 2), keepdims=True)) / (X_tgt.std(axis=(0, 1, 2), keepdims=True) + 1e-6)
    
    feats_src = (feats_src - feats_src.mean(axis=0)) / (feats_src.std(axis=0) + 1e-6)
    feats_tgt = (feats_tgt - feats_tgt.mean(axis=0)) / (feats_tgt.std(axis=0) + 1e-6)
    
    # Create domain labels (0 = source, 1 = target)
    domain_src = np.zeros(len(X_src))
    domain_tgt = np.ones(len(X_tgt))
    
    # Combine data from both domains
    X_combined = np.concatenate([X_src, X_tgt], axis=0)
    feats_combined = np.concatenate([feats_src, feats_tgt], axis=0)
    y_combined = np.concatenate([y_src, y_tgt], axis=0)
    domain_combined = np.concatenate([domain_src, domain_tgt], axis=0)
    metadata_combined = np.concatenate([metadata_src, metadata_tgt], axis=0)
    
    print(f"\nCombined data shapes:")
    print(f"X: {X_combined.shape}, feats: {feats_combined.shape}")
    print(f"Labels: {y_combined.shape}, Domains: {domain_combined.shape}")
    print(f"Source samples: {len(X_src)}, Target samples: {len(X_tgt)}")
    print(f"Label distribution: {np.bincount(y_combined.astype(int))}")
    print(f"Domain distribution: {np.bincount(domain_combined.astype(int))}")
    
    # Split into train and validation
    print(f"\nSplitting data (val_size={args.val_size})...")
    (
        X_train, X_val,
        feats_train, feats_val,
        y_train, y_val,
        domain_train, domain_val,
        metadata_train, metadata_val
    ) = train_test_split(
        X_combined, feats_combined, y_combined, domain_combined, metadata_combined,
        test_size=args.val_size,
        stratify=domain_combined,  # Stratify by domain to keep balance
        random_state=args.random_state,
        shuffle=True
    )
    
    print(f"Train set: {len(X_train)} samples")
    print(f"Validation set: {len(X_val)} samples")
    
    # Clean up memory
    del X_src, X_tgt, feats_src, feats_tgt, y_src, y_tgt
    del X_combined, feats_combined, y_combined, domain_combined
    gc.collect()
    
    # Create model
    img_shape = X_train[0].shape
    num_features = feats_train.shape[1]
    
    print(f"\nCreating DANN model...")
    print(f"Image shape: {img_shape}")
    print(f"Number of features: {num_features}")
    print(f"Lambda (gradient reversal): {args.lambda_domain}")
    
    model = create_dann_model(img_shape, num_features, lambda_domain=args.lambda_domain)
    model.summary()
    
    # Setup callbacks
    batch_logger = BatchMetricsLogger()
    early_stopping = F1EarlyStopping(
        precision_key="val_label_output_precision",
        recall_key="val_label_output_recall",
        patience=args.patience,
        restore_best_weights=True,
    )
    
    callbacks = [
        batch_logger,
        early_stopping,
        tf.keras.callbacks.ModelCheckpoint(
            os.path.join(args.output_dir, "best_model.h5"),
            save_best_only=True,
            monitor="val_label_output_recall",
            mode="max",
            verbose=1
        ),
        tf.keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss", factor=0.5, patience=5, min_lr=1e-6, verbose=1
        ),
    ]
    
    # Calculate class weights for label classification (handle class imbalance)
    # In unsupervised DANN: only train label classifier on SOURCE domain
    # Train domain classifier on BOTH domains
    
    # Identify source vs target samples in training set
    is_source_train = (domain_train == 0)
    is_target_train = (domain_train == 1)
    
    n_source = is_source_train.sum()
    n_target = is_target_train.sum()
    n_neg_src = np.sum((y_train == 0) & is_source_train)
    n_pos_src = np.sum((y_train == 1) & is_source_train)
    
    print(f"\nTraining set composition:")
    print(f"  Source samples: {n_source} (Negative={n_neg_src}, Positive={n_pos_src})")
    print(f"  Target samples: {n_target} (labels NOT used for training)")
    print(f"Class weight for positive samples: {args.class_weight_pos}")
    
    # For multi-output models, sample_weight needs to be a list/tuple of arrays
    # One array per output, matching the order of outputs in the model
    
    # Label classifier: Only train on SOURCE domain (set target weights to 0)
    label_sample_weights = np.zeros(len(y_train))  # Initialize all to 0
    label_sample_weights[is_source_train] = 1.0  # Enable source samples
    label_sample_weights[(y_train == 1) & is_source_train] = args.class_weight_pos  # Weight positive class
    
    # Domain classifier: Train on BOTH domains equally
    domain_sample_weights = np.ones(len(y_train)) if not only_tp else (y_train == 1)
    
    # Train model
    print(f"\nStarting training...")
    history = model.fit(
        [X_train, feats_train],
        [y_train, domain_train],  # Use list instead of dict
        epochs=args.epochs,
        batch_size=args.batch_size,
        validation_data=(
            [X_val, feats_val],
            [y_val, domain_val]  # Use list instead of dict
        ),
        callbacks=callbacks,
        sample_weight=[label_sample_weights, domain_sample_weights],  # List of arrays
        verbose=1
    )
    
    # Save final model
    final_model_path = os.path.join(args.output_dir, "final_model.h5")
    model.save(final_model_path)
    print(f"\nFinal model saved to: {final_model_path}")
    
    # Plot training history (epoch-wise and iteration-wise)
    plot_training_history(history, batch_logger.batch_metrics, args.output_dir)
    
    # Evaluate on validation set
    print(f"\nEvaluating model on validation set...")
    results = model.evaluate(
        [X_val, feats_val],
        [y_val, domain_val],  # Use list instead of dict
        verbose=0
    )
    
    print(f"\nValidation Results:")
    for name, value in zip(model.metrics_names, results):
        print(f"{name}: {value:.4f}")
    
    # Make predictions
    y_pred_label, y_pred_domain = model.predict([X_val, feats_val], verbose=0)
    y_pred_label_binary = (y_pred_label > 0.5).astype(int).flatten()
    
    # Classification report for labels
    print(f"\nLabel Classification Report:")
    print(classification_report(y_val, y_pred_label_binary))
    
    # Confusion matrix
    cm = confusion_matrix(y_val, y_pred_label_binary)
    print(f"\nLabel Confusion Matrix:")
    print(cm)
    
    # Save results
    results_path = os.path.join(args.output_dir, "training_results.txt")
    with open(results_path, "w") as f:
        f.write("DOMAIN ADVERSARIAL TRAINING RESULTS\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"Source domain: {args.source_data}\n")
        f.write(f"Target domain: {args.target_data}\n")
        f.write(f"Lambda (gradient reversal): {args.lambda_domain}\n\n")
        f.write("Validation Results:\n")
        for name, value in zip(model.metrics_names, results):
            f.write(f"{name}: {value:.4f}\n")
        f.write(f"\nLabel Classification Report:\n")
        f.write(classification_report(y_val, y_pred_label_binary))
        f.write(f"\nLabel Confusion Matrix:\n{cm}\n")
    
    print(f"\nResults saved to: {results_path}")
    print(f"\nTraining completed successfully!")
    print(f"All outputs saved to: {args.output_dir}")


if __name__ == "__main__":
    main()
