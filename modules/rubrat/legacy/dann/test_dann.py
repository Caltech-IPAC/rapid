"""
Test Domain Adversarial model on both source and target domains.

This script evaluates a trained DANN model on test data from both domains
and provides comprehensive metrics.
"""

import argparse
import os
import numpy as np
import pandas as pd
import tensorflow as tf
from sklearn.metrics import (
    confusion_matrix,
    classification_report,
    roc_auc_score,
    precision_recall_curve,
    roc_curve,
    auc
)
from matplotlib import pyplot as plt
import scienceplots
from model.data import load_dataset, normalize_arrays
from model.metadata import get_transient_magnitude

# Import model components from flat package module
from model.dann_model import (
    rot90_k1,
    rot90_k2,
    rot90_k3,
    GradientReversalLayer
)
def load_data(data_path):
    return load_dataset(data_path, mmap=False, allow_npy_dict=False)

def evaluate_domain(model, X, feats, y, domain_name, output_dir, metadata=None, threshold=0.5):
    """
    Evaluate model on a specific domain.
    
    Args:
        model: Trained DANN model
        X: Image data
        feats: Tabular features
        y: Labels
        domain_name: Name of the domain (for logging)
        output_dir: Directory to save results
        metadata: Optional metadata containing magnitude information
        threshold: Classification threshold for histogram (default: 0.5)
    
    Returns:
        dict: Dictionary of evaluation metrics
    """
    print(f"\n{'='*60}")
    print(f"Evaluating on {domain_name} Domain")
    print(f"{'='*60}")
    
    # Remove NaN values
    mask = np.isnan(X).any(axis=(1, 2, 3)) | np.isnan(feats).any(axis=1)
    if mask.any():
        print(f"Removing {mask.sum()} samples with NaN values")
        X = X[~mask]
        feats = feats[~mask]
        y = y[~mask]
        if metadata is not None:
            metadata = metadata[~mask]
    
    # Normalize data (using same approach as training)
    X, feats = normalize_arrays(X, feats)
    
    # Get predictions (only label output matters for testing)
    predictions = model.predict([X, feats], verbose=0)
    
    # Handle different output formats
    if isinstance(predictions, list):
        y_pred_prob = predictions[0]  # Label predictions
        y_pred_domain = predictions[1]  # Domain predictions
    else:
        y_pred_prob = predictions
        y_pred_domain = None
    
    y_pred_prob = y_pred_prob.flatten()
    y_pred = (y_pred_prob > 0.5).astype(int)
    
    # Calculate metrics
    accuracy = (y_pred == y).mean()
    
    # Confusion matrix
    cm = confusion_matrix(y, y_pred)
    tn, fp, fn, tp = cm.ravel()
    
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    
    # ROC AUC
    try:
        roc_auc = roc_auc_score(y, y_pred_prob)
        fpr, tpr, _ = roc_curve(y, y_pred_prob)
    except:
        roc_auc = 0.0
        fpr, tpr = None, None
    
    # Precision-Recall AUC
    try:
        precision_curve, recall_curve, _ = precision_recall_curve(y, y_pred_prob)
        pr_auc = auc(recall_curve, precision_curve)
    except:
        pr_auc = 0.0
        precision_curve, recall_curve = None, None

    # Find optimal F1 threshold
    print(f"\nFinding optimal F1 threshold...")
    thresholds_f1 = np.linspace(0, 1, 1000)
    f1_scores = []
    precision_scores = []
    
    for thresh in thresholds_f1:
        y_pred_thresh = (y_pred_prob > thresh).astype(int)
        cm_thresh = confusion_matrix(y, y_pred_thresh, labels=[0, 1])
        
        if cm_thresh.size == 4:
            tn_t, fp_t, fn_t, tp_t = cm_thresh.ravel()
        elif cm_thresh.size == 1:
            if y_pred_thresh.sum() == 0:
                tn_t, fp_t, fn_t, tp_t = cm_thresh[0, 0], 0, y.sum(), 0
            else:
                tn_t, fp_t, fn_t, tp_t = 0, (y == 0).sum(), 0, y.sum()
        else:
            tn_t, fp_t, fn_t, tp_t = 0, 0, 0, 0
        
        prec_t = tp_t / (tp_t + fp_t) if (tp_t + fp_t) > 0 else 0
        rec_t = tp_t / (tp_t + fn_t) if (tp_t + fn_t) > 0 else 0
        f1_t = 2 * prec_t * rec_t / (prec_t + rec_t) if (prec_t + rec_t) > 0 else 0
        f1_scores.append(f1_t)
        precision_scores.append(prec_t)
    
    f1_scores = np.array(f1_scores)
    precision_scores = np.array(precision_scores)
    
    # Find optimal F1 threshold
    best_f1_idx = np.argmax(f1_scores)
    best_f1_threshold = thresholds_f1[best_f1_idx]
    best_f1_score = f1_scores[best_f1_idx]
    
    # Calculate metrics at optimal F1 threshold
    y_pred_best_f1 = (y_pred_prob > best_f1_threshold).astype(int)
    cm_best_f1 = confusion_matrix(y, y_pred_best_f1)
    tn_best, fp_best, fn_best, tp_best = cm_best_f1.ravel()
    
    accuracy_best_f1 = (y_pred_best_f1 == y).mean()
    precision_best_f1 = tp_best / (tp_best + fp_best) if (tp_best + fp_best) > 0 else 0
    recall_best_f1 = tp_best / (tp_best + fn_best) if (tp_best + fn_best) > 0 else 0
    
    # Find threshold where precision is closest to 60%
    print(f"\nFinding threshold for 60% precision...")
    target_precision = 0.60
    prec_60_idx = np.argmin(np.abs(precision_scores - target_precision))
    prec_60_threshold = thresholds_f1[prec_60_idx]
    
    # Calculate metrics at 60% precision threshold
    y_pred_prec_60 = (y_pred_prob > prec_60_threshold).astype(int)
    cm_prec_60 = confusion_matrix(y, y_pred_prec_60)
    tn_p60, fp_p60, fn_p60, tp_p60 = cm_prec_60.ravel()
    
    accuracy_prec_60 = (y_pred_prec_60 == y).mean()
    precision_prec_60 = tp_p60 / (tp_p60 + fp_p60) if (tp_p60 + fp_p60) > 0 else 0
    recall_prec_60 = tp_p60 / (tp_p60 + fn_p60) if (tp_p60 + fn_p60) > 0 else 0
    f1_prec_60 = 2 * precision_prec_60 * recall_prec_60 / (precision_prec_60 + recall_prec_60) if (precision_prec_60 + recall_prec_60) > 0 else 0
    
    # Print results at default threshold (0.5)
    print(f"\nMetrics for {domain_name} (at threshold 0.5):")
    print(f"Accuracy: {accuracy:.4f}")
    print(f"Precision: {precision:.4f}")
    print(f"Recall: {recall:.4f}")
    print(f"F1 Score: {f1:.4f}")
    print(f"ROC AUC: {roc_auc:.4f}")
    print(f"PR AUC: {pr_auc:.4f}")
    
    print(f"\nConfusion Matrix (at threshold 0.5):")
    print(cm)
    
    # Print results at optimal F1 threshold
    print(f"\n{'='*60}")
    print(f"Metrics at Optimal F1 Threshold ({best_f1_threshold:.4f}):")
    print(f"{'='*60}")
    print(f"F1 Score: {best_f1_score:.4f}")
    print(f"Accuracy: {accuracy_best_f1:.4f}")
    print(f"Precision: {precision_best_f1:.4f}")
    print(f"Recall: {recall_best_f1:.4f}")
    print(f"\nConfusion Matrix (at threshold {best_f1_threshold:.4f}):")
    print(cm_best_f1)
    
    # Print results at 60% precision threshold
    print(f"\n{'='*60}")
    print(f"Metrics at 60% Precision Threshold ({prec_60_threshold:.4f}):")
    print(f"{'='*60}")
    print(f"Precision: {precision_prec_60:.4f} (target: 0.60)")
    print(f"Recall: {recall_prec_60:.4f}")
    print(f"F1 Score: {f1_prec_60:.4f}")
    print(f"Accuracy: {accuracy_prec_60:.4f}")
    print(f"\nConfusion Matrix (at threshold {prec_60_threshold:.4f}):")
    print(cm_prec_60)
    
    print(f"\nClassification Report (at threshold 0.5):")
    print(classification_report(y, y_pred))
    
    # Plot ROC and Precision-Recall curves
    if (fpr is not None and tpr is not None) or (precision_curve is not None and recall_curve is not None):
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        
        # ROC Curve
        if fpr is not None and tpr is not None:
            axes[0].plot(fpr, tpr, linewidth=2, label=f'ROC (AUC = {roc_auc:.3f})')
            axes[0].plot([0, 1], [0, 1], 'k--', linewidth=1, label='Random')
            axes[0].set_xlabel('False Positive Rate', fontsize=12)
            axes[0].set_ylabel('True Positive Rate', fontsize=12)
            axes[0].set_title(f'ROC Curve - {domain_name} Domain', fontsize=13)
            axes[0].legend(fontsize=10)
            axes[0].grid(True, alpha=0.3)
        
        # Precision-Recall Curve
        if precision_curve is not None and recall_curve is not None:
            axes[1].plot(recall_curve, precision_curve, linewidth=2, label=f'PR (AUC = {pr_auc:.3f})')
            axes[1].axhline(y=y.mean(), color='k', linestyle='--', linewidth=1, label=f'Baseline ({y.mean():.3f})')
            axes[1].set_xlabel('Recall', fontsize=12)
            axes[1].set_ylabel('Precision', fontsize=12)
            axes[1].set_title(f'Precision-Recall Curve - {domain_name} Domain', fontsize=13)
            axes[1].legend(fontsize=10)
            axes[1].grid(True, alpha=0.3)
        
        plt.tight_layout()
        curves_path = os.path.join(output_dir, f"{domain_name}_curves.png")
        plt.savefig(curves_path, dpi=300, bbox_inches="tight")
        plt.close()
        print(f"ROC and PR curves saved to: {curves_path}")
    
    # Plot Precision vs Threshold and Recall vs Threshold
    print(f"\nGenerating Precision and Recall vs Threshold plots...")
    thresholds = np.linspace(0, 1, 100)
    precisions = []
    recalls = []
    
    for thresh in thresholds:
        y_pred_thresh = (y_pred_prob > thresh).astype(int)
        cm_thresh = confusion_matrix(y, y_pred_thresh, labels=[0, 1])
        
        if cm_thresh.size == 4:  # Full confusion matrix
            tn_t, fp_t, fn_t, tp_t = cm_thresh.ravel()
        elif cm_thresh.size == 1:  # Edge case: only one class predicted
            if y_pred_thresh.sum() == 0:  # All predicted as 0
                tn_t, fp_t, fn_t, tp_t = cm_thresh[0, 0], 0, y.sum(), 0
            else:  # All predicted as 1
                tn_t, fp_t, fn_t, tp_t = 0, (y == 0).sum(), 0, y.sum()
        else:
            # Handle other edge cases
            tn_t, fp_t, fn_t, tp_t = 0, 0, 0, 0
        
        prec_t = tp_t / (tp_t + fp_t) if (tp_t + fp_t) > 0 else 0
        rec_t = tp_t / (tp_t + fn_t) if (tp_t + fn_t) > 0 else 0
        
        precisions.append(prec_t)
        recalls.append(rec_t)
    
    precisions = np.array(precisions)
    recalls = np.array(recalls)
    
    # Create plot
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    
    # Precision vs Threshold
    axes[0].plot(thresholds, precisions, linewidth=2, color='blue', label='Precision')
    axes[0].axvline(x=0.5, color='red', linestyle='--', linewidth=1, label='Default Threshold (0.5)')
    axes[0].set_xlabel('Threshold', fontsize=12)
    axes[0].set_ylabel('Precision', fontsize=12)
    axes[0].set_title(f'Precision vs Threshold - {domain_name} Domain', fontsize=13)
    axes[0].legend(fontsize=10)
    axes[0].grid(True, alpha=0.3)
    axes[0].set_xlim([0, 1])
    axes[0].set_ylim([0, 1.05])
    
    # Recall vs Threshold
    axes[1].plot(thresholds, recalls, linewidth=2, color='green', label='Recall')
    axes[1].axvline(x=0.5, color='red', linestyle='--', linewidth=1, label='Default Threshold (0.5)')
    axes[1].set_xlabel('Threshold', fontsize=12)
    axes[1].set_ylabel('Recall', fontsize=12)
    axes[1].set_title(f'Recall vs Threshold - {domain_name} Domain', fontsize=13)
    axes[1].legend(fontsize=10)
    axes[1].grid(True, alpha=0.3)
    axes[1].set_xlim([0, 1])
    axes[1].set_ylim([0, 1.05])
    
    plt.tight_layout()
    threshold_curves_path = os.path.join(output_dir, f"{domain_name}_precision_recall_vs_threshold.png")
    plt.savefig(threshold_curves_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"Precision and Recall vs Threshold plots saved to: {threshold_curves_path}")
    
    # Save results to file
    results_path = os.path.join(output_dir, f"{domain_name}_test_results.txt")
    with open(results_path, "w") as f:
        f.write(f"Test Results for {domain_name} Domain\n")
        f.write("=" * 60 + "\n\n")
        f.write("Metrics at Default Threshold (0.5):\n")
        f.write("-" * 60 + "\n")
        f.write(f"Accuracy: {accuracy:.4f}\n")
        f.write(f"Precision: {precision:.4f}\n")
        f.write(f"Recall: {recall:.4f}\n")
        f.write(f"F1 Score: {f1:.4f}\n")
        f.write(f"ROC AUC: {roc_auc:.4f}\n")
        f.write(f"PR AUC: {pr_auc:.4f}\n\n")
        f.write(f"Confusion Matrix:\n{cm}\n\n")
        f.write(f"Classification Report:\n")
        f.write(classification_report(y, y_pred))
        f.write("\n" + "=" * 60 + "\n\n")
        f.write(f"Metrics at Optimal F1 Threshold ({best_f1_threshold:.4f}):\n")
        f.write("-" * 60 + "\n")
        f.write(f"F1 Score: {best_f1_score:.4f}\n")
        f.write(f"Accuracy: {accuracy_best_f1:.4f}\n")
        f.write(f"Precision: {precision_best_f1:.4f}\n")
        f.write(f"Recall: {recall_best_f1:.4f}\n\n")
        f.write(f"Confusion Matrix:\n{cm_best_f1}\n\n")
        f.write(f"Classification Report:\n")
        f.write(classification_report(y, y_pred_best_f1))
        f.write("\n" + "=" * 60 + "\n\n")
        f.write(f"Metrics at 60% Precision Threshold ({prec_60_threshold:.4f}):\n")
        f.write("-" * 60 + "\n")
        f.write(f"Precision: {precision_prec_60:.4f} (target: 0.60)\n")
        f.write(f"Recall: {recall_prec_60:.4f}\n")
        f.write(f"F1 Score: {f1_prec_60:.4f}\n")
        f.write(f"Accuracy: {accuracy_prec_60:.4f}\n\n")
        f.write(f"Confusion Matrix:\n{cm_prec_60}\n\n")
        f.write(f"Classification Report:\n")
        f.write(classification_report(y, y_pred_prec_60))
    
    print(f"\nResults saved to: {results_path}")
    
    # Save misclassification examples
    false_positives = (y == 0) & (y_pred == 1)
    false_negatives = (y == 1) & (y_pred == 0)
    
    n_fp = false_positives.sum()
    n_fn = false_negatives.sum()
    
    print(f"\nMisclassifications:")
    print(f"  False Positives: {n_fp}")
    print(f"  False Negatives: {n_fn}")
    
    # Save examples of misclassifications (up to 10 of each type)
    n_examples = 10
    
    if n_fp > 0:
        fp_indices = np.where(false_positives)[0]
        n_show_fp = min(n_examples, len(fp_indices))
        selected_fp = np.random.choice(fp_indices, n_show_fp, replace=False)
        
        # Create figure for false positives - show 3 channels separately
        fig, axes = plt.subplots(n_show_fp, 3, figsize=(12, 3 * n_show_fp))
        fig.suptitle(f'{domain_name} Domain - False Positives (Predicted Real, Actually Bogus)', fontsize=14)
        
        if n_show_fp == 1:
            axes = axes.reshape(1, -1)
        
        channel_names = ['Science', 'Reference', 'Difference']
        
        for idx, sample_idx in enumerate(selected_fp):
            img = X[sample_idx]
            
            # Plot each channel separately
            for ch in range(3):
                axes[idx, ch].imshow(img[:, :, ch], cmap='gray', interpolation='nearest')
                if idx == 0:
                    axes[idx, ch].set_title(f'{channel_names[ch]}', fontsize=11)
                axes[idx, ch].axis('off')
            
            # Add prediction info on the left
            axes[idx, 0].text(-0.15, 0.5, f'Pred: {y_pred_prob[sample_idx]:.3f}\nTrue: {y[sample_idx]}', 
                             transform=axes[idx, 0].transAxes, fontsize=10, 
                             verticalalignment='center', horizontalalignment='right')
        
        plt.tight_layout()
        fp_path = os.path.join(output_dir, f"{domain_name}_false_positives.png")
        plt.savefig(fp_path, dpi=200, bbox_inches="tight")
        plt.close()
        print(f"False positive examples saved to: {fp_path}")
    
    if n_fn > 0:
        fn_indices = np.where(false_negatives)[0]
        n_show_fn = min(n_examples, len(fn_indices))
        selected_fn = np.random.choice(fn_indices, n_show_fn, replace=False)
        
        # Create figure for false negatives - show 3 channels separately
        fig, axes = plt.subplots(n_show_fn, 3, figsize=(12, 3 * n_show_fn))
        fig.suptitle(f'{domain_name} Domain - False Negatives (Predicted Bogus, Actually Real)', fontsize=14)
        
        if n_show_fn == 1:
            axes = axes.reshape(1, -1)
        
        channel_names = ['Science', 'Reference', 'Difference']
        
        for idx, sample_idx in enumerate(selected_fn):
            img = X[sample_idx]
            
            # Plot each channel separately
            for ch in range(3):
                axes[idx, ch].imshow(img[:, :, ch], cmap='gray', interpolation='nearest')
                if idx == 0:
                    axes[idx, ch].set_title(f'{channel_names[ch]}', fontsize=11)
                axes[idx, ch].axis('off')
            
            # Add prediction info on the left
            axes[idx, 0].text(-0.15, 0.5, f'Pred: {y_pred_prob[sample_idx]:.3f}\nTrue: {y[sample_idx]}', 
                             transform=axes[idx, 0].transAxes, fontsize=10, 
                             verticalalignment='center', horizontalalignment='right')
        
        plt.tight_layout()
        fn_path = os.path.join(output_dir, f"{domain_name}_false_negatives.png")
        plt.savefig(fn_path, dpi=200, bbox_inches="tight")
        plt.close()
        print(f"False negative examples saved to: {fn_path}")
        
            
    # Create magnitude histogram if metadata is available (only for Target domain)
    if domain_name == "Target":
        try:
            # Apply scienceplots style for publication
            plt.style.use(['science', 'ieee', 'no-latex'])
            plt.rcParams.update({
                'font.size': 10,
                'font.family': 'serif',
                'axes.labelsize': 11,
                'axes.titlesize': 11,
                'xtick.labelsize': 9,
                'ytick.labelsize': 9,
                'legend.fontsize': 9,
                'lines.linewidth': 1.5,
                'grid.linewidth': 0.5,
                'axes.linewidth': 0.8,
            })
            
            print(f"\nUsing threshold {threshold:.2f} for magnitude histogram")
            
            # Extract magnitude values using get_transient_magnitude function
            # ONLY call the function for samples where y == 1 (ground truth positives)
            mag_values = []
            pred_probs = []  # Store corresponding prediction probabilities
            
            for idx, m in enumerate(metadata):
                # CRITICAL: Skip if not a ground truth positive (y != 1)
                # get_transient_magnitude will fail for false positives
                if y[idx] != 1:
                    continue
                
                try:
                    if isinstance(m, dict) and 'match_id' in m and 'jid_folder' in m:
                        mag = get_transient_magnitude(m['match_id'], m['jid_folder'])
                        # Only add if magnitude is valid (not NaN)
                        if not np.isnan(mag):
                            mag_values.append(mag)
                            pred_probs.append(y_pred_prob[idx])
                        else:
                            print(f"Skipping sample {idx}: magnitude is NaN")
                            print(f"ID: {m['match_id']}, Folder: {m['jid_folder']}")
                    else:
                        print(f"Skipping sample {idx}: missing match_id or jid_folder in metadata")
                except Exception as e:
                    print(f"Error getting magnitude for sample {idx}: {e}")
            
            if len(mag_values) == 0:
                print("No valid magnitude values found. Skipping histogram.")
                return {
                    "accuracy": accuracy,
                    "precision": precision,
                    "recall": recall,
                    "f1": f1,
                    "roc_auc": roc_auc,
                    "pr_auc": pr_auc,
                    "confusion_matrix": cm,
                    "best_f1_threshold": best_f1_threshold,
                    "best_f1_score": best_f1_score,
                    "best_f1_accuracy": accuracy_best_f1,
                    "best_f1_precision": precision_best_f1,
                    "best_f1_recall": recall_best_f1,
                    "best_f1_confusion_matrix": cm_best_f1,
                    "prec_60_threshold": prec_60_threshold,
                    "prec_60_precision": precision_prec_60,
                    "prec_60_recall": recall_prec_60,
                    "prec_60_f1": f1_prec_60,
                    "prec_60_accuracy": accuracy_prec_60,
                    "prec_60_confusion_matrix": cm_prec_60
                }
            
            mag_values = np.array(mag_values)
            pred_probs = np.array(pred_probs)
            
            # All samples here are ground truth positives (y==1)
            # Separate by model prediction: true positives vs false negatives
            true_positive_mask = pred_probs > threshold
            
            mag_all_positives = mag_values  # All ground truth positives
            mag_true_positives = mag_values[true_positive_mask]  # Correctly detected
            
            # Create single plot with both histograms overlaid
            fig, ax = plt.subplots(figsize=(3.5, 2.8), dpi=600)
            
            # Use grayscale-friendly colors with different line styles
            colors = {
                'all': '#000000',  # Black
                'tp': 'blue',   # Dark gray
            }
            
            # Use common bins based on all positives range
            bins = np.linspace(mag_all_positives.min(), mag_all_positives.max(), 30)
            
            # Plot all ground truth positives
            ax.hist(mag_all_positives,
                    bins=bins,
                    label="Ground Truth Positives",
                    color=colors['all'],
                    histtype="step",
                    linewidth=1.5,
                    linestyle='-',
                    zorder=2)
            
            # Plot true positives if available
            if len(mag_true_positives) > 0:
                ax.hist(mag_true_positives,
                        bins=bins,
                        label=f"True Positives",
                        color=colors['tp'],
                        histtype="step",
                        linewidth=1.5,
                        linestyle='-',
                        zorder=3)
            
            # Set log scale
            ax.set_yscale("log")
            
            # Labels with proper units
            ax.set_xlabel("Magnitude (mag)", fontsize=10)
            ax.set_ylabel("Count", fontsize=10)
            
            # Concise title
            ax.set_title(f"{domain_name} Domain - Magnitude Distribution", fontsize=10, pad=8)
            
            # Legend with academic styling
            ax.legend(loc='best', frameon=True, 
                        fontsize=8, framealpha=1, 
                        edgecolor='black', fancybox=False,
                        borderpad=0.5, labelspacing=0.3)
            
            # Minimal grid on y-axis only (common for log-scale histograms)
            ax.grid(True, alpha=0.2, linestyle='-', linewidth=0.3, 
                    color='gray', zorder=0, axis='y')
            ax.set_axisbelow(True)
            
            # Clean white background
            ax.set_facecolor('white')
            fig.patch.set_facecolor('white')
            
            # Standard spine styling
            for spine in ax.spines.values():
                spine.set_edgecolor('black')
                spine.set_linewidth(0.8)
            
            # Tight layout
            plt.tight_layout(pad=0.3)
            
            # Save with high DPI for publication
            histogram_plot_path = os.path.join(output_dir, f"{domain_name}_magnitude_histogram.png")
            
            # Save both PNG and PDF
            plt.savefig(histogram_plot_path, dpi=600, bbox_inches='tight', 
                        facecolor='white', edgecolor='none')
            plt.savefig(histogram_plot_path.replace('.png', '.pdf'), 
                        bbox_inches='tight', facecolor='white', edgecolor='none')
            
            plt.close()
            
            print(f"Magnitude histogram saved to: {histogram_plot_path}")
            print(f"Magnitude histogram PDF saved to: {histogram_plot_path.replace('.png', '.pdf')}")
            
            # Print summary statistics
            print(f"\nMagnitude Summary Statistics:")
            print(f"All Ground Truth Positives: {len(mag_all_positives)}")
            if len(mag_true_positives) > 0:
                print(f"True Positives: {len(mag_true_positives)}")
                print(f"Detection rate: {len(mag_true_positives)/len(mag_all_positives)*100:.1f}%")
                print(f"True positives magnitude range: [{mag_true_positives.min():.2f}, {mag_true_positives.max():.2f}]")
            print(f"All positives magnitude range: [{mag_all_positives.min():.2f}, {mag_all_positives.max():.2f}]")
            
            # Create separate plot for false negatives
            false_negative_mask = pred_probs <= threshold
            mag_false_negatives = mag_values[false_negative_mask]
            
            if len(mag_false_negatives) > 0:
                print(f"\nCreating False Negatives magnitude histogram...")
                print(f"False Negatives: {len(mag_false_negatives)}")
                
                fig_fn, ax_fn = plt.subplots(figsize=(3.5, 2.8), dpi=600)
                
                # Use red for false negatives to indicate missed detections
                bins_fn = np.linspace(mag_false_negatives.min(), mag_false_negatives.max(), 30)
                
                ax_fn.hist(mag_false_negatives,
                          bins=bins_fn,
                          label=f"False Negatives (N={len(mag_false_negatives)})",
                          color='red',
                          histtype="step",
                          linewidth=1.5,
                          linestyle='-',
                          zorder=2)
                
                # Set log scale
                ax_fn.set_yscale("log")
                
                # Labels
                ax_fn.set_xlabel("Magnitude (mag)", fontsize=10)
                ax_fn.set_ylabel("Count", fontsize=10)
                ax_fn.set_title(f"{domain_name} Domain - False Negatives Magnitude Distribution", fontsize=10, pad=8)
                
                # Legend
                ax_fn.legend(loc='best', frameon=True, 
                            fontsize=8, framealpha=1, 
                            edgecolor='black', fancybox=False,
                            borderpad=0.5, labelspacing=0.3)
                
                # Grid
                ax_fn.grid(True, alpha=0.2, linestyle='-', linewidth=0.3, 
                          color='gray', zorder=0, axis='y')
                ax_fn.set_axisbelow(True)
                
                # Styling
                ax_fn.set_facecolor('white')
                fig_fn.patch.set_facecolor('white')
                
                for spine in ax_fn.spines.values():
                    spine.set_edgecolor('black')
                    spine.set_linewidth(0.8)
                
                plt.tight_layout(pad=0.3)
                
                # Save
                fn_histogram_path = os.path.join(output_dir, f"{domain_name}_false_negatives_magnitude_histogram.png")
                plt.savefig(fn_histogram_path, dpi=600, bbox_inches='tight', 
                           facecolor='white', edgecolor='none')
                plt.savefig(fn_histogram_path.replace('.png', '.pdf'), 
                           bbox_inches='tight', facecolor='white', edgecolor='none')
                plt.close()
                
                print(f"False negatives magnitude histogram saved to: {fn_histogram_path}")
                print(f"False negatives magnitude range: [{mag_false_negatives.min():.2f}, {mag_false_negatives.max():.2f}]")
            
        except Exception as e:
            print(f"Could not create magnitude histogram: {e}")
    
    # Return metrics (include default, optimal F1, and 60% precision metrics)
    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "roc_auc": roc_auc,
        "pr_auc": pr_auc,
        "confusion_matrix": cm,
        "best_f1_threshold": best_f1_threshold,
        "best_f1_score": best_f1_score,
        "best_f1_accuracy": accuracy_best_f1,
        "best_f1_precision": precision_best_f1,
        "best_f1_recall": recall_best_f1,
        "best_f1_confusion_matrix": cm_best_f1,
        "prec_60_threshold": prec_60_threshold,
        "prec_60_precision": precision_prec_60,
        "prec_60_recall": recall_prec_60,
        "prec_60_f1": f1_prec_60,
        "prec_60_accuracy": accuracy_prec_60,
        "prec_60_confusion_matrix": cm_prec_60
    }


def plot_comparison(source_metrics, target_metrics, output_dir):
    """Plot comparison between source and target domain performance."""
    metrics = ["accuracy", "precision", "recall", "f1", "roc_auc", "pr_auc"]
    source_values = [source_metrics[m] for m in metrics]
    target_values = [target_metrics[m] for m in metrics]
    
    x = np.arange(len(metrics))
    width = 0.35
    
    fig, ax = plt.subplots(figsize=(12, 6))
    bars1 = ax.bar(x - width/2, source_values, width, label="Source Domain", alpha=0.8)
    bars2 = ax.bar(x + width/2, target_values, width, label="Target Domain", alpha=0.8)
    
    ax.set_xlabel("Metrics")
    ax.set_ylabel("Score")
    ax.set_title("Model Performance: Source vs Target Domain")
    ax.set_xticks(x)
    ax.set_xticklabels([m.replace("_", " ").title() for m in metrics])
    ax.legend()
    ax.grid(True, alpha=0.3)
    
    # Add value labels on bars
    for bars in [bars1, bars2]:
        for bar in bars:
            height = bar.get_height()
            ax.annotate(f'{height:.3f}',
                       xy=(bar.get_x() + bar.get_width() / 2, height),
                       xytext=(0, 3),
                       textcoords="offset points",
                       ha='center', va='bottom',
                       fontsize=8)
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "domain_comparison.png"), dpi=300, bbox_inches="tight")
    plt.close()
    
    print(f"\nComparison plot saved to: {os.path.join(output_dir, 'domain_comparison.png')}")


def main():
    parser = argparse.ArgumentParser(
        description="Test Domain Adversarial model"
    )
    
    parser.add_argument(
        "--model_path",
        type=str,
        default="./outputs/dann_train/best_model.h5",
        # required=True,
        help="Path to trained model (.h5 file)",
    )
    parser.add_argument(
        "--source_test",
        type=str,
        default="./data/source_test.npz",
        # required=True,
        help="Path to source domain test data (.npz file)",
    )
    parser.add_argument(
        "--target_test",
        type=str,
        default="./data/target_test.npz",
        # required=True,
        help="Path to target domain test data (.npz file)",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./dann_test_results_tp_only",
        help="Directory to save test results (default: ./dann_test_results)",
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
    print("DOMAIN ADVERSARIAL MODEL TESTING")
    print("=" * 80)
    print(f"Model: {args.model_path}")
    print(f"Source test data: {args.source_test}")
    print(f"Target test data: {args.target_test}")
    print(f"Output directory: {args.output_dir}")
    print("=" * 80)
    
    # Load model with custom objects
    print("\nLoading model...")
    model = tf.keras.models.load_model(
        args.model_path,
        custom_objects={
            "GradientReversalLayer": GradientReversalLayer,
            "rot90_k1": rot90_k1,
            "rot90_k2": rot90_k2,
            "rot90_k3": rot90_k3
        }
    )
    print("Model loaded successfully!")
    
    # Load test data
    print("\n--- Loading Source Domain Test Data ---")
    X_src, feats_src, y_src, metadata_src = load_data(args.source_test)
    
    print("\n--- Loading Target Domain Test Data ---")
    X_tgt, feats_tgt, y_tgt, metadata_tgt = load_data(args.target_test)
    
    # Evaluate on source domain
    source_metrics = evaluate_domain(
        model, X_src, feats_src, y_src, 
        "Source", args.output_dir, metadata_src
    )
    
    # Evaluate on target domain
    target_metrics = evaluate_domain(
        model, X_tgt, feats_tgt, y_tgt,
        "Target", args.output_dir, metadata_tgt
    )
    
    # Plot comparison
    plot_comparison(source_metrics, target_metrics, args.output_dir)
    
    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print("\nDefault Threshold (0.5):")
    print(f"  Source Domain F1: {source_metrics['f1']:.4f}")
    print(f"  Target Domain F1: {target_metrics['f1']:.4f}")
    print(f"  F1 Difference: {abs(source_metrics['f1'] - target_metrics['f1']):.4f}")
    print(f"  Source Domain ROC AUC: {source_metrics['roc_auc']:.4f}")
    print(f"  Target Domain ROC AUC: {target_metrics['roc_auc']:.4f}")
    print(f"  ROC AUC Difference: {abs(source_metrics['roc_auc'] - target_metrics['roc_auc']):.4f}")
    
    print("\nOptimal F1 Threshold:")
    print(f"  Source: threshold={source_metrics['best_f1_threshold']:.4f}, F1={source_metrics['best_f1_score']:.4f}")
    print(f"  Target: threshold={target_metrics['best_f1_threshold']:.4f}, F1={target_metrics['best_f1_score']:.4f}")
    print(f"  Best F1 Difference: {abs(source_metrics['best_f1_score'] - target_metrics['best_f1_score']):.4f}")
    
    print("\n60% Precision Threshold:")
    print(f"  Source: threshold={source_metrics['prec_60_threshold']:.4f}, Precision={source_metrics['prec_60_precision']:.4f}, Recall={source_metrics['prec_60_recall']:.4f}, F1={source_metrics['prec_60_f1']:.4f}")
    print(f"  Target: threshold={target_metrics['prec_60_threshold']:.4f}, Precision={target_metrics['prec_60_precision']:.4f}, Recall={target_metrics['prec_60_recall']:.4f}, F1={target_metrics['prec_60_f1']:.4f}")
    print("=" * 80)
    
    # Save summary
    summary_path = os.path.join(args.output_dir, "summary.txt")
    with open(summary_path, "w") as f:
        f.write("DOMAIN ADVERSARIAL MODEL - TEST SUMMARY\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"Model: {args.model_path}\n\n")
        
        f.write("=" * 80 + "\n")
        f.write("METRICS AT DEFAULT THRESHOLD (0.5)\n")
        f.write("=" * 80 + "\n\n")
        f.write("Source Domain Performance:\n")
        for key in ["accuracy", "precision", "recall", "f1", "roc_auc", "pr_auc"]:
            f.write(f"  {key}: {source_metrics[key]:.4f}\n")
        f.write("\nTarget Domain Performance:\n")
        for key in ["accuracy", "precision", "recall", "f1", "roc_auc", "pr_auc"]:
            f.write(f"  {key}: {target_metrics[key]:.4f}\n")
        f.write("\nDomain Differences:\n")
        f.write(f"  F1 Difference: {abs(source_metrics['f1'] - target_metrics['f1']):.4f}\n")
        f.write(f"  ROC AUC Difference: {abs(source_metrics['roc_auc'] - target_metrics['roc_auc']):.4f}\n")
        
        f.write("\n" + "=" * 80 + "\n")
        f.write("METRICS AT OPTIMAL F1 THRESHOLD\n")
        f.write("=" * 80 + "\n\n")
        f.write("Source Domain (Optimal F1):\n")
        f.write(f"  Threshold: {source_metrics['best_f1_threshold']:.4f}\n")
        f.write(f"  F1 Score: {source_metrics['best_f1_score']:.4f}\n")
        f.write(f"  Accuracy: {source_metrics['best_f1_accuracy']:.4f}\n")
        f.write(f"  Precision: {source_metrics['best_f1_precision']:.4f}\n")
        f.write(f"  Recall: {source_metrics['best_f1_recall']:.4f}\n")
        
        f.write("\nTarget Domain (Optimal F1):\n")
        f.write(f"  Threshold: {target_metrics['best_f1_threshold']:.4f}\n")
        f.write(f"  F1 Score: {target_metrics['best_f1_score']:.4f}\n")
        f.write(f"  Accuracy: {target_metrics['best_f1_accuracy']:.4f}\n")
        f.write(f"  Precision: {target_metrics['best_f1_precision']:.4f}\n")
        f.write(f"  Recall: {target_metrics['best_f1_recall']:.4f}\n")
        
        f.write("\nDomain Differences (Optimal F1):\n")
        f.write(f"  Best F1 Difference: {abs(source_metrics['best_f1_score'] - target_metrics['best_f1_score']):.4f}\n")
        f.write(f"  Threshold Difference: {abs(source_metrics['best_f1_threshold'] - target_metrics['best_f1_threshold']):.4f}\n")
        
        f.write("\n" + "=" * 80 + "\n")
        f.write("METRICS AT 60% PRECISION THRESHOLD\n")
        f.write("=" * 80 + "\n\n")
        f.write("Source Domain (60% Precision):\n")
        f.write(f"  Threshold: {source_metrics['prec_60_threshold']:.4f}\n")
        f.write(f"  Precision: {source_metrics['prec_60_precision']:.4f}\n")
        f.write(f"  Recall: {source_metrics['prec_60_recall']:.4f}\n")
        f.write(f"  F1 Score: {source_metrics['prec_60_f1']:.4f}\n")
        f.write(f"  Accuracy: {source_metrics['prec_60_accuracy']:.4f}\n")
        
        f.write("\nTarget Domain (60% Precision):\n")
        f.write(f"  Threshold: {target_metrics['prec_60_threshold']:.4f}\n")
        f.write(f"  Precision: {target_metrics['prec_60_precision']:.4f}\n")
        f.write(f"  Recall: {target_metrics['prec_60_recall']:.4f}\n")
        f.write(f"  F1 Score: {target_metrics['prec_60_f1']:.4f}\n")
        f.write(f"  Accuracy: {target_metrics['prec_60_accuracy']:.4f}\n")
    
    print(f"\nSummary saved to: {summary_path}")
    print(f"\nTesting completed successfully!")


if __name__ == "__main__":
    main()
