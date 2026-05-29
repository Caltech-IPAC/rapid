"""
Test Control Model on Test Data

This script evaluates a trained standard (non-domain-adversarial) model 
on test data to establish a baseline performance for comparison.
"""

import argparse
import os
import tensorflow as tf
import numpy as np
from sklearn.metrics import (
    confusion_matrix,
    classification_report,
    roc_auc_score,
    precision_recall_curve,
    auc,
    roc_curve
)
from matplotlib import pyplot as plt
from model.data import load_dataset
from model.layers import rot90_k1, rot90_k2, rot90_k3


def load_data(data_path):
    """
    Load the dataset from the specified path.

    Args:
        data_path (str): Path to the dataset file (.npz file).

    Returns:
        tuple: X, feats, y, metadata as numpy arrays.
    """
    return load_dataset(data_path, mmap=False, allow_npy_dict=False)


def evaluate_model(model, X, feats, y, dataset_name, output_dir):
    """
    Evaluate model on test data.
    
    Args:
        model: Trained model
        X: Image data
        feats: Tabular features
        y: Labels
        dataset_name: Name of the dataset (for logging)
        output_dir: Directory to save results
    
    Returns:
        dict: Dictionary of evaluation metrics
    """
    print(f"\n{'='*60}")
    print(f"Evaluating on {dataset_name}")
    print(f"{'='*60}")
    
    # Remove NaN values
    mask = np.isnan(X).any(axis=(1, 2, 3)) | np.isnan(feats).any(axis=1)
    if mask.any():
        print(f"Removing {mask.sum()} samples with NaN values")
        X = X[~mask]
        feats = feats[~mask]
        y = y[~mask]
    
    # Normalize data (using same approach as training)
    X = (X - X.mean(axis=(0, 1, 2), keepdims=True)) / (X.std(axis=(0, 1, 2), keepdims=True) + 1e-6)
    feats = (feats - feats.mean(axis=0)) / (feats.std(axis=0) + 1e-6)
    
    # Get predictions
    y_pred_prob = model.predict([X, feats], verbose=0).flatten()
    y_pred = (y_pred_prob > 0.5).astype(int)
    
    # Calculate metrics
    accuracy = (y_pred == y).mean()
    
    # Confusion matrix
    cm = confusion_matrix(y, y_pred)
    tn, fp, fn, tp = cm.ravel()
    
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    
    # Specificity (True Negative Rate)
    specificity = tn / (tn + fp) if (tn + fp) > 0 else 0
    
    # ROC AUC
    try:
        roc_auc = roc_auc_score(y, y_pred_prob)
        fpr, tpr, thresholds = roc_curve(y, y_pred_prob)
    except:
        roc_auc = 0.0
        fpr, tpr, thresholds = None, None, None
    
    # Precision-Recall AUC
    try:
        precision_curve, recall_curve, _ = precision_recall_curve(y, y_pred_prob)
        pr_auc = auc(recall_curve, precision_curve)
    except:
        pr_auc = 0.0
        precision_curve, recall_curve = None, None
    
    # Print results
    print(f"\nMetrics for {dataset_name}:")
    print(f"Accuracy: {accuracy:.4f}")
    print(f"Precision: {precision:.4f}")
    print(f"Recall (Sensitivity): {recall:.4f}")
    print(f"Specificity: {specificity:.4f}")
    print(f"F1 Score: {f1:.4f}")
    print(f"ROC AUC: {roc_auc:.4f}")
    print(f"PR AUC: {pr_auc:.4f}")
    
    print(f"\nConfusion Matrix:")
    print(f"              Predicted")
    print(f"              Neg    Pos")
    print(f"Actual Neg   {tn:5d}  {fp:5d}")
    print(f"       Pos   {fn:5d}  {tp:5d}")
    
    print(f"\nClassification Report:")
    print(classification_report(y, y_pred))
    
    # Plot ROC curve
    if fpr is not None and tpr is not None:
        plt.figure(figsize=(10, 5))
        
        # ROC Curve
        plt.subplot(1, 2, 1)
        plt.plot(fpr, tpr, label=f'ROC (AUC = {roc_auc:.3f})', linewidth=2)
        plt.plot([0, 1], [0, 1], 'k--', label='Random', linewidth=1)
        plt.xlabel('False Positive Rate')
        plt.ylabel('True Positive Rate')
        plt.title(f'ROC Curve - {dataset_name}')
        plt.legend()
        plt.grid(True, alpha=0.3)
        
        # Precision-Recall Curve
        plt.subplot(1, 2, 2)
        if precision_curve is not None and recall_curve is not None:
            plt.plot(recall_curve, precision_curve, label=f'PR (AUC = {pr_auc:.3f})', linewidth=2)
            plt.xlabel('Recall')
            plt.ylabel('Precision')
            plt.title(f'Precision-Recall Curve - {dataset_name}')
            plt.legend()
            plt.grid(True, alpha=0.3)
        
        plt.tight_layout()
        plt.savefig(
            os.path.join(output_dir, f"{dataset_name.replace(' ', '_')}_curves.png"), 
            dpi=300, bbox_inches="tight"
        )
        plt.close()
    
    # Save results to file
    results_path = os.path.join(output_dir, f"{dataset_name.replace(' ', '_')}_test_results.txt")
    with open(results_path, "w") as f:
        f.write(f"Test Results for {dataset_name}\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Sample size: {len(y)}\n")
        f.write(f"Class distribution: {np.bincount(y.astype(int))}\n\n")
        f.write(f"Accuracy: {accuracy:.4f}\n")
        f.write(f"Precision: {precision:.4f}\n")
        f.write(f"Recall (Sensitivity): {recall:.4f}\n")
        f.write(f"Specificity: {specificity:.4f}\n")
        f.write(f"F1 Score: {f1:.4f}\n")
        f.write(f"ROC AUC: {roc_auc:.4f}\n")
        f.write(f"PR AUC: {pr_auc:.4f}\n\n")
        f.write(f"Confusion Matrix:\n")
        f.write(f"              Predicted\n")
        f.write(f"              Neg    Pos\n")
        f.write(f"Actual Neg   {tn:5d}  {fp:5d}\n")
        f.write(f"       Pos   {fn:5d}  {tp:5d}\n\n")
        f.write(f"Classification Report:\n")
        f.write(classification_report(y, y_pred))
    
    print(f"\nResults saved to: {results_path}")
    
    # Return metrics
    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "specificity": specificity,
        "f1": f1,
        "roc_auc": roc_auc,
        "pr_auc": pr_auc,
        "confusion_matrix": cm,
        "n_samples": len(y),
        "n_positive": int(y.sum()),
        "n_negative": int((1 - y).sum())
    }


def main():
    parser = argparse.ArgumentParser(
        description="Test control model on test data"
    )
    
    parser.add_argument(
        "--model_path",
        type=str,
        required=True,
        help="Path to trained model (.h5 file)",
    )
    parser.add_argument(
        "--test_data",
        type=str,
        default="./data/target_test.npz",
        help="Path to test data (.npz file)",
    )
    parser.add_argument(
        "--dataset_name",
        type=str,
        default="Test Data",
        help="Name of the test dataset (for labeling, default: 'Test Data')",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./control_test_results_snr",
        help="Directory to save test results (default: ./control_test_results)",
    )
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
    print("CONTROL MODEL TESTING")
    print("=" * 80)
    print(f"Model: {args.model_path}")
    print(f"Test data: {args.test_data}")
    print(f"Dataset name: {args.dataset_name}")
    print(f"Output directory: {args.output_dir}")
    print("=" * 80)
    
    # Load model
    print("\nLoading model...")
    model = tf.keras.models.load_model(
        args.model_path,
        custom_objects={
            "rot90_k1": rot90_k1,
            "rot90_k2": rot90_k2,
            "rot90_k3": rot90_k3
        }
    )
    print("Model loaded successfully!")
    model.summary()
    
    # Load test data
    print(f"\n--- Loading Test Data ({args.dataset_name}) ---")
    X, feats, y, metadata = load_data(args.test_data)
    
    # Evaluate model
    metrics = evaluate_model(
        model, X, feats, y, 
        args.dataset_name, args.output_dir
    )
    
    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Dataset: {args.dataset_name}")
    print(f"Samples: {metrics['n_samples']} (Positive: {metrics['n_positive']}, Negative: {metrics['n_negative']})")
    print(f"Accuracy: {metrics['accuracy']:.4f}")
    print(f"Precision: {metrics['precision']:.4f}")
    print(f"Recall: {metrics['recall']:.4f}")
    print(f"Specificity: {metrics['specificity']:.4f}")
    print(f"F1 Score: {metrics['f1']:.4f}")
    print(f"ROC AUC: {metrics['roc_auc']:.4f}")
    print(f"PR AUC: {metrics['pr_auc']:.4f}")
    print("=" * 80)
    
    # Save summary
    summary_path = os.path.join(args.output_dir, "summary.txt")
    with open(summary_path, "w") as f:
        f.write("CONTROL MODEL - TEST SUMMARY\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"Model: {args.model_path}\n")
        f.write(f"Test data: {args.test_data}\n\n")
        f.write(f"Dataset: {args.dataset_name}\n")
        f.write(f"Samples: {metrics['n_samples']} ")
        f.write(f"(Positive: {metrics['n_positive']}, Negative: {metrics['n_negative']})\n\n")
        f.write("Performance Metrics:\n")
        f.write(f"  Accuracy: {metrics['accuracy']:.4f}\n")
        f.write(f"  Precision: {metrics['precision']:.4f}\n")
        f.write(f"  Recall: {metrics['recall']:.4f}\n")
        f.write(f"  Specificity: {metrics['specificity']:.4f}\n")
        f.write(f"  F1 Score: {metrics['f1']:.4f}\n")
        f.write(f"  ROC AUC: {metrics['roc_auc']:.4f}\n")
        f.write(f"  PR AUC: {metrics['pr_auc']:.4f}\n")
    
    print(f"\nSummary saved to: {summary_path}")
    print(f"\nTesting completed successfully!")


if __name__ == "__main__":
    main()
