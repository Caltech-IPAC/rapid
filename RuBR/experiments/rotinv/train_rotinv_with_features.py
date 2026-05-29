import gc
import argparse
import os
import random
import tensorflow as tf
from tensorflow.keras import layers, Model
import numpy as np
from sklearn.metrics import confusion_matrix, classification_report
from matplotlib import pyplot as plt
from sklearn.model_selection import train_test_split
from model.data import load_dataset
from model.layers import image_encoder
from model.callbacks import F1EarlyStopping

means = {
    "F184": [0.57817131, 0.58065492, 0.02057936],
    "H158": [1.16567825, 1.16309479, 0.03536996],
    "J129": [0.92620432, 0.92055472, 0.01911548],
    "K213": [1.04127802, 1.04668246, 0.05275641],
    "R062": [0.81320585, 0.81542744, 0.01229751],
    "Y106": [0.70102083, 0.7024932, 0.01716464],
    "Z087": [1.13010884, 1.12491324, 0.01309704],
}

vars = {
    "F184": [24.01642823, 28.37142752, 2.33357588],
    "H158": [141.10233678, 166.69334902, 13.48953537],
    "J129": [103.2038142, 122.14426572, 12.47955061],
    "K213": [47.1821188, 55.16905395, 4.47816794],
    "R062": [178.64370608, 212.49874184, 16.16102734],
    "Y106": [74.89405617, 88.88895939, 7.68320053],
    "Z087": [378.46329339, 451.93130827, 41.04080681],
}

def set_seed(seed):
    os.environ["PYTHONHASHSEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)
    tf.random.set_seed(seed)

def load_data(data_path):
    """
    Load the dataset from the specified path.

    Args:
        data_path (str): Path to the dataset file (.npz).

    Returns:
        tuple: X, feats, y, metadata as numpy arrays.
    """
    X, feats, y, metadata = load_dataset(data_path, mmap=False, allow_npy_dict=True)

    # Vectorized normalization by filter
    filters = np.array([m['filter'] for m in metadata])
    unique_filters = np.unique(filters)
    print(unique_filters)
    for f in unique_filters:
        idx = filters == f
        X[idx] = (X[idx] - means[str(f)]) / np.sqrt(vars[str(f)])

    return X, feats, y, metadata

def prepare_training_data(tp_train, fp_train):
    X_tp, feats_tp, y_tp, metadata_tp = load_data(tp_train)
    X_fp, feats_fp, y_fp, metadata_fp = load_data(fp_train)

    # Combine the data
    X = np.concatenate((X_tp, X_fp), axis=0)
    feats = np.concatenate((feats_tp, feats_fp), axis=0)
    y = np.concatenate((y_tp, y_fp), axis=0)
    metadata = np.concatenate((metadata_tp, metadata_fp), axis=0)
        
    indices = np.random.permutation(len(y))
        
    X = X[indices]
    feats = feats[indices]
    y = y[indices]
    metadata = metadata[indices]

    return X, feats, y, metadata

def prepare_testing_data(tp_test, fp_test):
    X_tp, feats_tp, y_tp, metadata_tp = load_data(tp_test)
    X_fp, feats_fp, y_fp, metadata_fp = load_data(fp_test)

    # Combine the data
    X = np.concatenate((X_tp, X_fp), axis=0)
    feats = np.concatenate((feats_tp, feats_fp), axis=0)
    y = np.concatenate((y_tp, y_fp), axis=0)
    metadata = np.concatenate((metadata_tp, metadata_fp), axis=0)

    return X, feats, y, metadata


def create_hybrid_model(img_shape, num_features):
    """
    Create a hybrid model that combines CNN for images and dense layers for features.

    Args:
        img_shape (tuple): Shape of input images (H, W, C)
        num_features (int): Number of tabular features

    Returns:
        tf.keras.Model: Compiled hybrid model
    """
    # Image input branch
    img_input = layers.Input(shape=img_shape)

    x = image_encoder(
        img_input,
        img_shape,
        mode="mean",
        conv_kernel_initializer="he_normal",
        dense_kernel_initializer="he_normal",
    )

    # Tabular features branch
    feat_input = layers.Input(shape=(num_features,))
    y = layers.Dense(32, activation="relu", kernel_initializer="he_normal")(feat_input)

    # Combine both branches
    combined = layers.Concatenate()([x, y])

    # Output layer
    output = layers.Dense(1, activation="sigmoid", kernel_initializer="he_normal")(combined)

    # Create and compile model
    model = Model(inputs=[img_input, feat_input], outputs=output)
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=0.001),
        loss="binary_crossentropy",
        metrics=["accuracy", "precision", "recall"],
    )

    return model

def plot_training_history(history, output_dir):
    """Plot and save training history."""
    plt.figure(figsize=(15, 5))

    # Plot training & validation accuracy
    plt.subplot(1, 3, 1)
    plt.plot(history.history["accuracy"], label="Train")
    plt.plot(history.history["val_accuracy"], label="Validation")
    plt.title("Model Accuracy")
    plt.ylabel("Accuracy")
    plt.xlabel("Epoch")
    plt.legend()
    plt.grid(True)

    # Plot training & validation loss
    plt.subplot(1, 3, 2)
    plt.plot(history.history["loss"], label="Train")
    plt.plot(history.history["val_loss"], label="Validation")
    plt.title("Model Loss")
    plt.ylabel("Loss")
    plt.xlabel("Epoch")
    plt.legend()
    plt.grid(True)

    # Plot precision and recall
    plt.subplot(1, 3, 3)
    plt.plot(history.history["precision"], label="Train Precision")
    plt.plot(history.history["val_precision"], label="Val Precision")
    plt.plot(history.history["recall"], label="Train Recall")
    plt.plot(history.history["val_recall"], label="Val Recall")
    plt.title("Precision and Recall")
    plt.ylabel("Score")
    plt.xlabel("Epoch")
    plt.legend()
    plt.grid(True)

    plt.tight_layout()
    plt.savefig(
        os.path.join(output_dir, "training_history.png"), dpi=300, bbox_inches="tight"
    )
    plt.show()


def evaluate_model(model, X_test, feats_test, y_test, output_dir):
    """Evaluate the trained model and save results."""
    # Evaluate on test set
    loss, accuracy, precision, recall = model.evaluate(
        [X_test, feats_test], y_test, verbose=0
    )

    print(f"\nTest Results:")
    print(f"Test Loss: {loss:.4f}")
    print(f"Test Accuracy: {accuracy:.4f}")
    print(f"Test Precision: {precision:.4f}")
    print(f"Test Recall: {recall:.4f}")

    # Make predictions
    y_pred_prob = model.predict([X_test, feats_test], verbose=0)
    y_pred = (y_pred_prob > 0.5).astype(int)

    # Calculate confusion matrix and classification report
    cm = confusion_matrix(y_test, y_pred)
    print(f"\nConfusion Matrix:")
    print(cm)
    print(f"\nClassification Report:")
    print(classification_report(y_test, y_pred))

    # Save results to file
    results_path = os.path.join(output_dir, "evaluation_results.txt")
    with open(results_path, "w") as f:
        f.write(f"Test Results:\n")
        f.write(f"Test Loss: {loss:.4f}\n")
        f.write(f"Test Accuracy: {accuracy:.4f}\n")
        f.write(f"Test Precision: {precision:.4f}\n")
        f.write(f"Test Recall: {recall:.4f}\n\n")
        f.write(f"Confusion Matrix:\n{cm}\n\n")
        f.write(f"Classification Report:\n{classification_report(y_test, y_pred)}\n")

    print(f"Results saved to: {results_path}")

    return y_pred_prob, y_pred


def main(model=None):
    parser = argparse.ArgumentParser(
        description="Train hybrid CNN model for transient detection"
    )

    # Data arguments
    parser.add_argument(
        "--data_path",
        type=str,
        required=True,
        help="Path to the training data (.npz file)",
    )
    parser.add_argument(
        "--max_samples",
        type=int,
        default=300000,
        help="Maximum number of samples to use (default: 300000)",
    )

    # Training arguments
    parser.add_argument(
        "--epochs", type=int, default=60, help="Number of training epochs (default: 30)"
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=512,
        help="Batch size for training (default: 512)",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=0.001,
        help="Learning rate for Adam optimizer (default: 0.001)",
    )

    # Data split arguments
    parser.add_argument(
        "--test_size",
        type=float,
        default=0.15,
        help="Test set size fraction (default: 0.15)",
    )
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
        "--patience", type=int, default=10, help="Early stopping patience (default: 10)"
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
        default="./training_output_sqrt_var",
        help="Directory to save model and results (default: ./training_output)",
    )
    parser.add_argument(
        "--model_name",
        type=str,
        default="hybrid_model_rot_inv_final_new_data",
        help="Base name for saved models (default: hybrid_model)",
    )

    # GPU arguments
    parser.add_argument(
        "--gpu",
        type=int,
        default=None,
        help="GPU device ID to use (default: auto-select)",
    )

    args = parser.parse_args()

    set_seed(args.random_state)

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

    print("=" * 60)
    print("HYBRID CNN TRAINING")
    print("=" * 60)
    print(f"Data path: {args.data_path}")
    print(f"Max samples: {args.max_samples}")
    print(f"Output directory: {args.output_dir}")
    print(f"Epochs: {args.epochs}")
    print(f"Batch size: {args.batch_size}")
    print(f"Learning rate: {args.learning_rate}")
    print("=" * 60)

    # Load data
    X, feats, y, metadata = load_data(args.data_path)

    # Limit samples if specified
    if len(X) > args.max_samples:
        print(f"Limiting to {args.max_samples} samples")
        X = X[: args.max_samples]
        feats = feats[: args.max_samples]
        y = y[: args.max_samples]
        metadata = metadata[: args.max_samples]

    # Convert features to proper format
    try:
        if feats.dtype == object and len(feats) > 0 and isinstance(feats[0], dict):
            feat_keys = sorted(feats[0].keys())
            feats_np = np.array([[f[k] for k in feat_keys] for f in feats])
        else:
            feats_np = np.array([list(f.values()) for f in feats])
    except:
        feats_np = feats

    X = X.astype("float32")
    feats_np = feats_np.astype("float32")
    y = y.astype("int32")

    # Remove samples with NaN values
    print(feats_np.shape)
    mask = np.isnan(X).any(axis=(1, 2, 3)) | np.isnan(feats_np).any(axis=1)
    if mask.any():
        print(f"Removing {mask.sum()} samples with NaN values")
        X = X[~mask]
        feats_np = feats_np[~mask]
        y = y[~mask]
        metadata = metadata[~mask]

    print(f"Final data shapes - X: {X.shape}, feats: {feats_np.shape}, y: {y.shape}")
    print(f"Final class distribution: {np.bincount(y)}")

    # Split data into train/val/test
    print(f"\nSplitting data...")
    print(f"Test size: {args.test_size}, Validation size: {args.val_size}")

    # First split: separate test set
    if args.test_size > 0:
        (
            X_train_val,
            X_test,
            feats_train_val,
            feats_test,
            y_train_val,
            y_test,
            metadata_train_val,
            metadata_test,
        ) = train_test_split(
            X,
            feats_np,
            y,
            metadata,
            test_size=args.test_size,
            stratify=y,
            random_state=args.random_state,
            shuffle=True,
        )
    else:
        X_train_val = X
        feats_train_val = feats_np
        y_train_val = y
        metadata_train_val = metadata
        X_test = np.empty((0,) + X.shape[1:])
        feats_test = np.empty((0, feats_np.shape[1]))
        y_test = np.empty((0,))
        metadata_test = []

    # Second split: separate train and validation
    (
        X_train,
        X_val,
        feats_train,
        feats_val,
        y_train,
        y_val,
        metadata_train,
        metadata_val,
    ) = train_test_split(
        X_train_val,
        feats_train_val,
        y_train_val,
        metadata_train_val,
        test_size=args.val_size,
        stratify=y_train_val,
        random_state=args.random_state,
        shuffle=True,
    )

    print(f"Train set: {len(X_train)} samples")
    print(f"Validation set: {len(X_val)} samples")
    print(f"Test set: {len(X_test)} samples")

    # Clean up memory
    del (
        X,
        feats,
        feats_np,
        y,
        metadata,
        X_train_val,
        feats_train_val,
        y_train_val,
        metadata_train_val,
    )
    gc.collect()

    # Create model
    img_shape = X_train[0].shape
    num_features = feats_train.shape[1]

    print(f"\nCreating model...")
    print(f"Image shape: {img_shape}")
    print(f"Number of features: {num_features}")

    model = create_hybrid_model(img_shape, num_features)
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
            os.path.join(args.output_dir, f"{args.model_name}_best_precision.h5"),
            save_best_only=True,
            monitor="val_precision",
            mode="max",
            verbose=1,
        ),
        tf.keras.callbacks.ModelCheckpoint(
            os.path.join(args.output_dir, f"{args.model_name}_best_recall.h5"),
            save_best_only=True,
            monitor="val_recall",
            mode="max",
            verbose=1,
        ),
        tf.keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss", factor=0.5, patience=5, min_lr=1e-6, verbose=1
        ),
    ]

    # Calculate class weights
    # class_weights = compute_class_weight(
    #     "balanced", classes=np.unique(y_train), y=y_train
    # )
    class_weight_dict = {0: 1.0, 1: args.class_weight_pos}

    print(f"\nClass weights: {class_weight_dict}")

    # Train model
    print(f"\nStarting training...")
    history = model.fit(
        [X_train, feats_train],
        y_train,
        epochs=args.epochs,
        batch_size=args.batch_size,
        validation_data=([X_val, feats_val], y_val),
        callbacks=callbacks,
        class_weight=class_weight_dict,
        verbose=1,
    )

    # Save final model
    final_model_path = os.path.join(args.output_dir, f"{args.model_name}_final.h5")
    model.save(final_model_path)
    print(f"\nFinal model saved to: {final_model_path}")

    # Plot training history
    plot_training_history(history, args.output_dir)

    # Evaluate model
    print(f"\nEvaluating model on test set...")
    y_pred_prob, y_pred = evaluate_model(
        model, X_test, feats_test, y_test, args.output_dir
    )

    # Save test set predictions and metadata
    test_results_path = os.path.join(args.output_dir, "test_predictions.npz")
    np.savez(
        test_results_path,
        X=X_test,
        feats=feats_test,
        y_true=y_test,
        y_pred_prob=y_pred_prob,
        y_pred=y_pred,
        metadata=metadata_test,
    )
    print(f"Test predictions saved to: {test_results_path}")

    print(f"\nTraining completed successfully!")
    print(f"All outputs saved to: {args.output_dir}")


if __name__ == "__main__":
    main()
