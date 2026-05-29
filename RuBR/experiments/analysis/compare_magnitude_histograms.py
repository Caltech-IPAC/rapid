"""
Compare Magnitude Distributions: DANN vs Control Model

This script creates magnitude histograms comparing detection performance
between DANN and control models on the target domain.
"""

import argparse
import os
import numpy as np
import tensorflow as tf
from matplotlib import pyplot as plt
import scienceplots
from model.data import load_dataset, normalize_arrays
from model.layers import (
    rot90_k1,
    rot90_k2,
    rot90_k3,
    gradient_reversal,
    GradientReversalLayer,
)
from model.metadata import get_transient_magnitude

def load_data(data_path):
    return load_dataset(data_path, mmap=False, allow_npy_dict=False)


def get_predictions(model, X, feats):
    """Get model predictions."""
    # Normalize data
    X_norm, feats_norm = normalize_arrays(X, feats)
    
    # Get predictions
    predictions = model.predict([X_norm, feats_norm], verbose=0)
    
    # Handle different output formats (DANN has 2 outputs)
    if isinstance(predictions, list):
        y_pred_prob = predictions[0].flatten()
    else:
        y_pred_prob = predictions.flatten()
    
    return y_pred_prob


def main():
    parser = argparse.ArgumentParser(
        description="Compare magnitude histograms between DANN and control models"
    )
    
    parser.add_argument(
        "--dann_model",
        type=str,
        default="./outputs/dann_train/best_model.h5",
        help="Path to DANN model (.h5 file)",
    )
    parser.add_argument(
        "--control_model",
        type=str,
        default="./outputs/control_train/best_model.h5",
        help="Path to control model (.h5 file)",
    )
    parser.add_argument(
        "--test_data",
        type=str,
        default="./data/target_test.npz",
        help="Path to target domain test data (.npz file)",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./model_comparison_plots",
        help="Directory to save plots (default: ./model_comparison_plots)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Classification threshold for both models (default: 0.5). Overridden by dann_threshold/control_threshold if specified.",
    )
    parser.add_argument(
        "--dann_threshold",
        type=float,
        default=None,
        help="Classification threshold for DANN model (default: uses --threshold value)",
    )
    parser.add_argument(
        "--control_threshold",
        type=float,
        default=None,
        help="Classification threshold for control model (default: uses --threshold value)",
    )
    parser.add_argument(
        "--gpu",
        type=int,
        default=None,
        help="GPU device ID to use (default: auto-select)",
    )
    
    args = parser.parse_args()
    
    # Set individual thresholds (use --threshold as default if not specified)
    dann_threshold = args.dann_threshold if args.dann_threshold is not None else args.threshold
    control_threshold = args.control_threshold if args.control_threshold is not None else args.threshold
    
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
    print("MODEL COMPARISON - MAGNITUDE HISTOGRAMS")
    print("=" * 80)
    print(f"DANN model: {args.dann_model}")
    print(f"Control model: {args.control_model}")
    print(f"Test data: {args.test_data}")
    print(f"DANN threshold: {dann_threshold}")
    print(f"Control threshold: {control_threshold}")
    print(f"Output directory: {args.output_dir}")
    print("=" * 80)
    
    # Load models
    print("\nLoading DANN model...")
    dann_model = tf.keras.models.load_model(
        args.dann_model,
        custom_objects={
            "GradientReversalLayer": GradientReversalLayer,
            "rot90_k1": rot90_k1,
            "rot90_k2": rot90_k2,
            "rot90_k3": rot90_k3
        }
    )
    print("DANN model loaded successfully!")
    
    print("\nLoading control model...")
    control_model = tf.keras.models.load_model(
        args.control_model,
        custom_objects={
            "rot90_k1": rot90_k1,
            "rot90_k2": rot90_k2,
            "rot90_k3": rot90_k3
        }
    )
    print("Control model loaded successfully!")
    
    # Load test data
    print("\n--- Loading Target Domain Test Data ---")
    X, feats, y, metadata = load_data(args.test_data)
    
    # Remove NaN values
    mask = np.isnan(X).any(axis=(1, 2, 3)) | np.isnan(feats).any(axis=1)
    if mask.any():
        print(f"Removing {mask.sum()} samples with NaN values")
        X = X[~mask]
        feats = feats[~mask]
        y = y[~mask]
        metadata = metadata[~mask]
    
    # Get predictions from both models
    print("\nGenerating predictions from DANN model...")
    dann_pred_prob = get_predictions(dann_model, X, feats)
    
    print("Generating predictions from control model...")
    control_pred_prob = get_predictions(control_model, X, feats)
    
    # Extract magnitude values for ground truth positives (y == 1)
    print("\nExtracting magnitude values for ground truth positives...")
    mag_values = []
    dann_probs = []
    control_probs = []
    filter_values = []
    
    for idx, m in enumerate(metadata):
        # CRITICAL: Only process ground truth positives (y == 1)
        if y[idx] != 1:
            continue
        
        try:
            if isinstance(m, dict) and 'match_id' in m and 'jid_folder' in m:
                mag = get_transient_magnitude(m['match_id'], m['jid_folder'])
                if not np.isnan(mag):
                    mag_values.append(mag)
                    dann_probs.append(dann_pred_prob[idx])
                    control_probs.append(control_pred_prob[idx])
                    # Extract filter information
                    filter_name = m.get('filter', m.get('band', 'unknown'))
                    filter_values.append(filter_name)
        except Exception as e:
            print(f"Error getting magnitude for sample {idx}: {e}")
    
    if len(mag_values) == 0:
        print("No valid magnitude values found. Exiting.")
        return
    
    mag_values = np.array(mag_values)
    dann_probs = np.array(dann_probs)
    control_probs = np.array(control_probs)
    filter_values = np.array(filter_values)
    
    print(f"Total ground truth positives with valid magnitudes: {len(mag_values)}")
    print(f"Unique filters found: {np.unique(filter_values)}")
    
    # Apply thresholds to get true positives for each model
    dann_tp_mask = dann_probs > dann_threshold
    control_tp_mask = control_probs > control_threshold
    
    mag_all_positives = mag_values
    mag_dann_tp = mag_values[dann_tp_mask]
    mag_control_tp = mag_values[control_tp_mask]
    
    print(f"DANN true positives: {len(mag_dann_tp)} ({len(mag_dann_tp)/len(mag_values)*100:.1f}%)")
    print(f"Control true positives: {len(mag_control_tp)} ({len(mag_control_tp)/len(mag_values)*100:.1f}%)")
    
    # Create comparison histogram
    print("\nCreating comparison histogram...")
    
    # Apply scienceplots style
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
    
    fig, ax = plt.subplots(figsize=(3.5, 2.8), dpi=600)
    
    # Colors for the three distributions
    colors = {
        'all': '#000000',      # Black - all ground truth
        'dann': '#0000FF',     # Blue - DANN detections
        'control': '#FF0000',  # Red - Control detections
    }
    
    # Use common bins based on all positives range
    bins = np.linspace(mag_all_positives.min(), mag_all_positives.max(), 30)
    
    # Plot all ground truth positives
    ax.hist(mag_all_positives,
            bins=bins,
            label=f"Ground Truth (N={len(mag_all_positives)})",
            color=colors['all'],
            histtype="step",
            linewidth=1.5,
            linestyle='-',
            zorder=2)
    
    # Plot DANN true positives
    if len(mag_dann_tp) > 0:
        ax.hist(mag_dann_tp,
                bins=bins,
                label=f"Domain Adversarial Training (N={len(mag_dann_tp)})",
                color=colors['dann'],
                histtype="step",
                linewidth=1.5,
                linestyle='-',
                zorder=3)
    
    # Plot control true positives
    if len(mag_control_tp) > 0:
        ax.hist(mag_control_tp,
                bins=bins,
                label=f"No Domain Adaptation (N={len(mag_control_tp)})",
                color=colors['control'],
                histtype="step",
                linewidth=1.5,
                linestyle='-',
                zorder=4)
    
    # Set log scale
    # ax.set_yscale("log")
    
    # Labels
    ax.set_xlabel("Magnitude (mag)", fontsize=10)
    ax.set_ylabel("Count", fontsize=10)
    ax.set_title("Target Domain - Model Comparison", fontsize=10, pad=8)
    
    # Legend
    ax.legend(loc='best', frameon=True, 
              fontsize=8, framealpha=1, 
              edgecolor='black', fancybox=False,
              borderpad=0.5, labelspacing=0.3)
    
    # Grid
    ax.grid(True, alpha=0.2, linestyle='-', linewidth=0.3, 
            color='gray', zorder=0, axis='y')
    ax.set_axisbelow(True)
    
    # Styling
    ax.set_facecolor('white')
    fig.patch.set_facecolor('white')
    
    for spine in ax.spines.values():
        spine.set_edgecolor('black')
        spine.set_linewidth(0.8)
    
    plt.tight_layout(pad=0.3)
    
    # Save
    plot_path = os.path.join(args.output_dir, "model_comparison_magnitude_histogram.png")
    plt.savefig(plot_path, dpi=600, bbox_inches='tight', 
                facecolor='white', edgecolor='none')
    plt.savefig(plot_path.replace('.png', '.pdf'), 
                bbox_inches='tight', facecolor='white', edgecolor='none')
    plt.close()
    
    print(f"\nComparison histogram saved to: {plot_path}")
    print(f"PDF version: {plot_path.replace('.png', '.pdf')}")
    
    # Create cumulative histogram
    print("\nCreating cumulative histogram...")
    
    fig, ax = plt.subplots(figsize=(3.5, 2.8), dpi=600)
    
    # Plot cumulative distributions
    ax.hist(mag_all_positives,
            bins=bins,
            label=f"Ground Truth (N={len(mag_all_positives)})",
            color=colors['all'],
            histtype="step",
            linewidth=1.5,
            linestyle='-',
            cumulative=True,
            zorder=2)
    
    if len(mag_dann_tp) > 0:
        ax.hist(mag_dann_tp,
                bins=bins,
                label=f"Domain Adversarial Training (N={len(mag_dann_tp)})",
                color=colors['dann'],
                histtype="step",
                linewidth=1.5,
                linestyle='-',
                cumulative=True,
                zorder=3)
    
    if len(mag_control_tp) > 0:
        ax.hist(mag_control_tp,
                bins=bins,
                label=f"No Domain Adaptation (N={len(mag_control_tp)})",
                color=colors['control'],
                histtype="step",
                linewidth=1.5,
                linestyle='-',
                cumulative=True,
                zorder=4)
    
    # Labels
    ax.set_xlabel("Magnitude (mag)", fontsize=10)
    ax.set_ylabel("Cumulative Count", fontsize=10)
    ax.set_title("Target Domain - Model Comparison (Cumulative)", fontsize=10, pad=8)
    
    # Legend
    ax.legend(loc='best', frameon=True, 
              fontsize=8, framealpha=1, 
              edgecolor='black', fancybox=False,
              borderpad=0.5, labelspacing=0.3)
    
    # Grid
    ax.grid(True, alpha=0.2, linestyle='-', linewidth=0.3, 
            color='gray', zorder=0, axis='y')
    ax.set_axisbelow(True)
    
    # Styling
    ax.set_facecolor('white')
    fig.patch.set_facecolor('white')
    
    for spine in ax.spines.values():
        spine.set_edgecolor('black')
        spine.set_linewidth(0.8)
    
    plt.tight_layout(pad=0.3)
    
    # Save
    cumulative_plot_path = os.path.join(args.output_dir, "model_comparison_magnitude_histogram_cumulative.png")
    plt.savefig(cumulative_plot_path, dpi=600, bbox_inches='tight', 
                facecolor='white', edgecolor='none')
    plt.savefig(cumulative_plot_path.replace('.png', '.pdf'), 
                bbox_inches='tight', facecolor='white', edgecolor='none')
    plt.close()
    
    print(f"\nCumulative histogram saved to: {cumulative_plot_path}")
    print(f"PDF version: {cumulative_plot_path.replace('.png', '.pdf')}")
    
    # Print summary statistics
    print("\n" + "=" * 80)
    print("SUMMARY STATISTICS")
    print("=" * 80)
    print(f"Ground Truth Positives: {len(mag_all_positives)}")
    print(f"Magnitude range: [{mag_all_positives.min():.2f}, {mag_all_positives.max():.2f}]")
    print()
    print(f"DANN Model:")
    print(f"  True Positives: {len(mag_dann_tp)}")
    print(f"  Detection Rate: {len(mag_dann_tp)/len(mag_all_positives)*100:.1f}%")
    if len(mag_dann_tp) > 0:
        print(f"  Magnitude range: [{mag_dann_tp.min():.2f}, {mag_dann_tp.max():.2f}]")
    print()
    print(f"Control Model:")
    print(f"  True Positives: {len(mag_control_tp)}")
    print(f"  Detection Rate: {len(mag_control_tp)/len(mag_all_positives)*100:.1f}%")
    if len(mag_control_tp) > 0:
        print(f"  Magnitude range: [{mag_control_tp.min():.2f}, {mag_control_tp.max():.2f}]")
    print("=" * 80)
    
    # Save statistics to file
    stats_path = os.path.join(args.output_dir, "comparison_statistics.txt")
    with open(stats_path, 'w') as f:
        f.write("MODEL COMPARISON - MAGNITUDE STATISTICS\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"DANN Model: {args.dann_model}\n")
        f.write(f"Control Model: {args.control_model}\n")
        f.write(f"Test Data: {args.test_data}\n")
        f.write(f"DANN Threshold: {dann_threshold}\n")
        f.write(f"Control Threshold: {control_threshold}\n\n")
        f.write(f"Ground Truth Positives: {len(mag_all_positives)}\n")
        f.write(f"Magnitude range: [{mag_all_positives.min():.2f}, {mag_all_positives.max():.2f}]\n\n")
        f.write(f"DANN Model:\n")
        f.write(f"  True Positives: {len(mag_dann_tp)}\n")
        f.write(f"  Detection Rate: {len(mag_dann_tp)/len(mag_all_positives)*100:.1f}%\n")
        if len(mag_dann_tp) > 0:
            f.write(f"  Magnitude range: [{mag_dann_tp.min():.2f}, {mag_dann_tp.max():.2f}]\n")
        f.write(f"\nControl Model:\n")
        f.write(f"  True Positives: {len(mag_control_tp)}\n")
        f.write(f"  Detection Rate: {len(mag_control_tp)/len(mag_all_positives)*100:.1f}%\n")
        if len(mag_control_tp) > 0:
            f.write(f"  Magnitude range: [{mag_control_tp.min():.2f}, {mag_control_tp.max():.2f}]\n")
    
    print(f"\nStatistics saved to: {stats_path}")
    
    # Create filter-wise histograms
    print("\n" + "=" * 80)
    print("CREATING FILTER-WISE HISTOGRAMS")
    print("=" * 80)
    
    unique_filters = np.unique(filter_values)
    print(f"Filters to process: {unique_filters}")
    
    for filter_name in unique_filters:
        print(f"\nProcessing filter: {filter_name}")
        
        # Get mask for this filter
        filter_mask = filter_values == filter_name
        
        mag_filter = mag_values[filter_mask]
        dann_probs_filter = dann_probs[filter_mask]
        control_probs_filter = control_probs[filter_mask]
        
        if len(mag_filter) == 0:
            print(f"  No samples for filter {filter_name}, skipping.")
            continue
        
        print(f"  Total samples: {len(mag_filter)}")
        
        # Apply thresholds
        dann_tp_mask_filter = dann_probs_filter > dann_threshold
        control_tp_mask_filter = control_probs_filter > control_threshold
        
        mag_dann_tp_filter = mag_filter[dann_tp_mask_filter]
        mag_control_tp_filter = mag_filter[control_tp_mask_filter]
        
        print(f"  DANN detections: {len(mag_dann_tp_filter)} ({len(mag_dann_tp_filter)/len(mag_filter)*100:.1f}%)")
        print(f"  Control detections: {len(mag_control_tp_filter)} ({len(mag_control_tp_filter)/len(mag_filter)*100:.1f}%)")
        
        # Create histogram for this filter
        fig, ax = plt.subplots(figsize=(3.5, 2.8), dpi=600)
        
        # Use common bins based on filter data range
        bins = np.linspace(mag_filter.min(), mag_filter.max(), 30)
        
        # Plot all ground truth positives for this filter
        ax.hist(mag_filter,
                bins=bins,
                label=f"Ground Truth (N={len(mag_filter)})",
                color=colors['all'],
                histtype="step",
                linewidth=1.5,
                linestyle='-',
                zorder=2)
        
        # Plot DANN true positives
        if len(mag_dann_tp_filter) > 0:
            ax.hist(mag_dann_tp_filter,
                    bins=bins,
                    label=f"Domain Adversarial Training (N={len(mag_dann_tp_filter)})",
                    color=colors['dann'],
                    histtype="step",
                    linewidth=1.5,
                    linestyle='-',
                    zorder=3)
        
        # Plot control true positives
        if len(mag_control_tp_filter) > 0:
            ax.hist(mag_control_tp_filter,
                    bins=bins,
                    label=f"No Domain Adaptation (N={len(mag_control_tp_filter)})",
                    color=colors['control'],
                    histtype="step",
                    linewidth=1.5,
                    linestyle='-',
                    zorder=4)
        
        # Labels
        ax.set_xlabel("Magnitude (mag)", fontsize=10)
        ax.set_ylabel("Count", fontsize=10)
        ax.set_title(f"Target Domain - Model Comparison ({filter_name} filter)", fontsize=10, pad=8)
        
        # Legend
        ax.legend(loc='best', frameon=True, 
                  fontsize=8, framealpha=1, 
                  edgecolor='black', fancybox=False,
                  borderpad=0.5, labelspacing=0.3)
        
        # Grid
        ax.grid(True, alpha=0.2, linestyle='-', linewidth=0.3, 
                color='gray', zorder=0, axis='y')
        ax.set_axisbelow(True)
        
        # Styling
        ax.set_facecolor('white')
        fig.patch.set_facecolor('white')
        
        for spine in ax.spines.values():
            spine.set_edgecolor('black')
            spine.set_linewidth(0.8)
        
        plt.tight_layout(pad=0.3)
        
        # Save
        filter_safe = str(filter_name).replace('/', '_').replace(' ', '_')
        plot_path_filter = os.path.join(args.output_dir, f"model_comparison_magnitude_histogram_{filter_safe}.png")
        plt.savefig(plot_path_filter, dpi=600, bbox_inches='tight', 
                    facecolor='white', edgecolor='none')
        plt.savefig(plot_path_filter.replace('.png', '.pdf'), 
                    bbox_inches='tight', facecolor='white', edgecolor='none')
        plt.close()
        
        print(f"  Saved: {plot_path_filter}")
        
        # Create cumulative histogram for this filter
        fig, ax = plt.subplots(figsize=(3.5, 2.8), dpi=600)
        
        # Plot cumulative distributions
        ax.hist(mag_filter,
                bins=bins,
                label=f"Ground Truth (N={len(mag_filter)})",
                color=colors['all'],
                histtype="step",
                linewidth=1.5,
                linestyle='-',
                cumulative=True,
                zorder=2)
        
        if len(mag_dann_tp_filter) > 0:
            ax.hist(mag_dann_tp_filter,
                    bins=bins,
                    label=f"Domain Adversarial Training (N={len(mag_dann_tp_filter)})",
                    color=colors['dann'],
                    histtype="step",
                    linewidth=1.5,
                    linestyle='-',
                    cumulative=True,
                    zorder=3)
        
        if len(mag_control_tp_filter) > 0:
            ax.hist(mag_control_tp_filter,
                    bins=bins,
                    label=f"No Domain Adaptation (N={len(mag_control_tp_filter)})",
                    color=colors['control'],
                    histtype="step",
                    linewidth=1.5,
                    linestyle='-',
                    cumulative=True,
                    zorder=4)
        
        # Labels
        ax.set_xlabel("Magnitude (mag)", fontsize=10)
        ax.set_ylabel("Cumulative Count", fontsize=10)
        ax.set_title(f"Target Domain - Model Comparison ({filter_name} filter, Cumulative)", fontsize=10, pad=8)
        
        # Legend
        ax.legend(loc='best', frameon=True, 
                  fontsize=8, framealpha=1, 
                  edgecolor='black', fancybox=False,
                  borderpad=0.5, labelspacing=0.3)
        
        # Grid
        ax.grid(True, alpha=0.2, linestyle='-', linewidth=0.3, 
                color='gray', zorder=0, axis='y')
        ax.set_axisbelow(True)
        
        # Styling
        ax.set_facecolor('white')
        fig.patch.set_facecolor('white')
        
        for spine in ax.spines.values():
            spine.set_edgecolor('black')
            spine.set_linewidth(0.8)
        
        plt.tight_layout(pad=0.3)
        
        # Save
        plot_path_filter_cumulative = os.path.join(args.output_dir, f"model_comparison_magnitude_histogram_{filter_safe}_cumulative.png")
        plt.savefig(plot_path_filter_cumulative, dpi=600, bbox_inches='tight', 
                    facecolor='white', edgecolor='none')
        plt.savefig(plot_path_filter_cumulative.replace('.png', '.pdf'), 
                    bbox_inches='tight', facecolor='white', edgecolor='none')
        plt.close()
        
        print(f"  Saved cumulative: {plot_path_filter_cumulative}")
    
    # Save filter-wise statistics to file
    filter_stats_path = os.path.join(args.output_dir, "comparison_statistics_by_filter.txt")
    with open(filter_stats_path, 'w') as f:
        f.write("MODEL COMPARISON - MAGNITUDE STATISTICS BY FILTER\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"DANN Model: {args.dann_model}\n")
        f.write(f"Control Model: {args.control_model}\n")
        f.write(f"Test Data: {args.test_data}\n")
        f.write(f"DANN Threshold: {dann_threshold}\n")
        f.write(f"Control Threshold: {control_threshold}\n\n")
        
        for filter_name in unique_filters:
            filter_mask = filter_values == filter_name
            mag_filter = mag_values[filter_mask]
            dann_probs_filter = dann_probs[filter_mask]
            control_probs_filter = control_probs[filter_mask]
            
            if len(mag_filter) == 0:
                continue
            
            dann_tp_mask_filter = dann_probs_filter > dann_threshold
            control_tp_mask_filter = control_probs_filter > control_threshold
            
            mag_dann_tp_filter = mag_filter[dann_tp_mask_filter]
            mag_control_tp_filter = mag_filter[control_tp_mask_filter]
            
            f.write(f"\nFILTER: {filter_name}\n")
            f.write("-" * 80 + "\n")
            f.write(f"Ground Truth Positives: {len(mag_filter)}\n")
            f.write(f"Magnitude range: [{mag_filter.min():.2f}, {mag_filter.max():.2f}]\n\n")
            f.write(f"DANN Model:\n")
            f.write(f"  True Positives: {len(mag_dann_tp_filter)}\n")
            f.write(f"  Detection Rate: {len(mag_dann_tp_filter)/len(mag_filter)*100:.1f}%\n")
            if len(mag_dann_tp_filter) > 0:
                f.write(f"  Magnitude range: [{mag_dann_tp_filter.min():.2f}, {mag_dann_tp_filter.max():.2f}]\n")
            f.write(f"\nControl Model:\n")
            f.write(f"  True Positives: {len(mag_control_tp_filter)}\n")
            f.write(f"  Detection Rate: {len(mag_control_tp_filter)/len(mag_filter)*100:.1f}%\n")
            if len(mag_control_tp_filter) > 0:
                f.write(f"  Magnitude range: [{mag_control_tp_filter.min():.2f}, {mag_control_tp_filter.max():.2f}]\n")
    
    print(f"\nFilter-wise statistics saved to: {filter_stats_path}")
    
    # Create combined multi-panel figure with all filters
    print("\n" + "=" * 80)
    print("CREATING COMBINED MULTI-PANEL FIGURE")
    print("=" * 80)
    
    n_filters = len(unique_filters)
    if n_filters > 0:
        # Determine layout (rows x cols)
        if n_filters == 1:
            n_rows, n_cols = 1, 1
        elif n_filters == 2:
            n_rows, n_cols = 1, 2
        elif n_filters <= 4:
            n_rows, n_cols = 2, 2
        elif n_filters <= 6:
            n_rows, n_cols = 2, 3
        elif n_filters <= 9:
            n_rows, n_cols = 3, 3
        else:
            n_rows = int(np.ceil(n_filters / 3))
            n_cols = 3
        
        fig, axes = plt.subplots(n_rows, n_cols, figsize=(3.5*n_cols, 2.8*n_rows), dpi=600)
        
        # Handle single subplot case
        if n_filters == 1:
            axes = np.array([axes])
        axes = axes.flatten()
        
        for idx, filter_name in enumerate(unique_filters):
            ax = axes[idx]
            
            # Get mask for this filter
            filter_mask = filter_values == filter_name
            
            mag_filter = mag_values[filter_mask]
            dann_probs_filter = dann_probs[filter_mask]
            control_probs_filter = control_probs[filter_mask]
            
            if len(mag_filter) == 0:
                ax.text(0.5, 0.5, f"No data\n({filter_name})", 
                       ha='center', va='center', transform=ax.transAxes)
                ax.set_xticks([])
                ax.set_yticks([])
                continue
            
            # Apply thresholds
            dann_tp_mask_filter = dann_probs_filter > dann_threshold
            control_tp_mask_filter = control_probs_filter > control_threshold
            
            mag_dann_tp_filter = mag_filter[dann_tp_mask_filter]
            mag_control_tp_filter = mag_filter[control_tp_mask_filter]
            
            # Use common bins based on filter data range
            bins = np.linspace(mag_filter.min(), mag_filter.max(), 30)
            
            # Plot all ground truth positives for this filter
            ax.hist(mag_filter,
                    bins=bins,
                    label=f"Ground Truth (N={len(mag_filter)})",
                    color=colors['all'],
                    histtype="step",
                    linewidth=1.5,
                    linestyle='-',
                    zorder=2)
            
            # Plot DANN true positives
            if len(mag_dann_tp_filter) > 0:
                ax.hist(mag_dann_tp_filter,
                        bins=bins,
                        label=f"DANN (N={len(mag_dann_tp_filter)})",
                        color=colors['dann'],
                        histtype="step",
                        linewidth=1.5,
                        linestyle='-',
                        zorder=3)
            
            # Plot control true positives
            if len(mag_control_tp_filter) > 0:
                ax.hist(mag_control_tp_filter,
                        bins=bins,
                        label=f"Control (N={len(mag_control_tp_filter)})",
                        color=colors['control'],
                        histtype="step",
                        linewidth=1.5,
                        linestyle='-',
                        zorder=4)
            
            # Labels
            ax.set_xlabel("Magnitude (mag)", fontsize=10)
            ax.set_ylabel("Count", fontsize=10)
            ax.set_title(f"{filter_name} filter", fontsize=10, pad=8)
            
            # Legend with smaller font for multi-panel
            ax.legend(loc='best', frameon=True, 
                      fontsize=7, framealpha=1, 
                      edgecolor='black', fancybox=False,
                      borderpad=0.4, labelspacing=0.2)
            
            # Grid
            ax.grid(True, alpha=0.2, linestyle='-', linewidth=0.3, 
                    color='gray', zorder=0, axis='y')
            ax.set_axisbelow(True)
            
            # Styling
            ax.set_facecolor('white')
            
            for spine in ax.spines.values():
                spine.set_edgecolor('black')
                spine.set_linewidth(0.8)
        
        # Hide unused subplots
        for idx in range(n_filters, len(axes)):
            axes[idx].axis('off')
        
        fig.patch.set_facecolor('white')
        plt.suptitle("Model Comparison by Filter - Target Domain", 
                     fontsize=12, fontweight='bold', y=0.995)
        plt.tight_layout(pad=0.5, rect=[0, 0, 1, 0.99])
        
        # Save combined figure
        combined_plot_path = os.path.join(args.output_dir, "model_comparison_magnitude_histogram_all_filters.png")
        plt.savefig(combined_plot_path, dpi=600, bbox_inches='tight', 
                    facecolor='white', edgecolor='none')
        plt.savefig(combined_plot_path.replace('.png', '.pdf'), 
                    bbox_inches='tight', facecolor='white', edgecolor='none')
        plt.close()
        
        print(f"\nCombined multi-panel figure saved to: {combined_plot_path}")
        print(f"PDF version: {combined_plot_path.replace('.png', '.pdf')}")
        
        # Create combined multi-panel CUMULATIVE figure
        print("\nCreating combined multi-panel cumulative figure...")
        
        fig, axes = plt.subplots(n_rows, n_cols, figsize=(3.5*n_cols, 2.8*n_rows), dpi=600)
        
        # Handle single subplot case
        if n_filters == 1:
            axes = np.array([axes])
        axes = axes.flatten()
        
        for idx, filter_name in enumerate(unique_filters):
            ax = axes[idx]
            
            # Get mask for this filter
            filter_mask = filter_values == filter_name
            
            mag_filter = mag_values[filter_mask]
            dann_probs_filter = dann_probs[filter_mask]
            control_probs_filter = control_probs[filter_mask]
            
            if len(mag_filter) == 0:
                ax.text(0.5, 0.5, f"No data\n({filter_name})", 
                       ha='center', va='center', transform=ax.transAxes)
                ax.set_xticks([])
                ax.set_yticks([])
                continue
            
            # Apply thresholds
            dann_tp_mask_filter = dann_probs_filter > dann_threshold
            control_tp_mask_filter = control_probs_filter > control_threshold
            
            mag_dann_tp_filter = mag_filter[dann_tp_mask_filter]
            mag_control_tp_filter = mag_filter[control_tp_mask_filter]
            
            # Use common bins based on filter data range
            bins = np.linspace(mag_filter.min(), mag_filter.max(), 30)
            
            # Plot cumulative distributions
            ax.hist(mag_filter,
                    bins=bins,
                    label=f"Ground Truth (N={len(mag_filter)})",
                    color=colors['all'],
                    histtype="step",
                    linewidth=1.5,
                    linestyle='-',
                    cumulative=True,
                    zorder=2)
            
            if len(mag_dann_tp_filter) > 0:
                ax.hist(mag_dann_tp_filter,
                        bins=bins,
                        label=f"DANN (N={len(mag_dann_tp_filter)})",
                        color=colors['dann'],
                        histtype="step",
                        linewidth=1.5,
                        linestyle='-',
                        cumulative=True,
                        zorder=3)
            
            if len(mag_control_tp_filter) > 0:
                ax.hist(mag_control_tp_filter,
                        bins=bins,
                        label=f"Control (N={len(mag_control_tp_filter)})",
                        color=colors['control'],
                        histtype="step",
                        linewidth=1.5,
                        linestyle='-',
                        cumulative=True,
                        zorder=4)
            
            # Labels
            ax.set_xlabel("Magnitude (mag)", fontsize=10)
            ax.set_ylabel("Cumulative Count", fontsize=10)
            ax.set_title(f"{filter_name} filter (Cumulative)", fontsize=10, pad=8)
            
            # Legend with smaller font for multi-panel
            ax.legend(loc='best', frameon=True, 
                      fontsize=7, framealpha=1, 
                      edgecolor='black', fancybox=False,
                      borderpad=0.4, labelspacing=0.2)
            
            # Grid
            ax.grid(True, alpha=0.2, linestyle='-', linewidth=0.3, 
                    color='gray', zorder=0, axis='y')
            ax.set_axisbelow(True)
            
            # Styling
            ax.set_facecolor('white')
            
            for spine in ax.spines.values():
                spine.set_edgecolor('black')
                spine.set_linewidth(0.8)
        
        # Hide unused subplots
        for idx in range(n_filters, len(axes)):
            axes[idx].axis('off')
        
        fig.patch.set_facecolor('white')
        plt.suptitle("Model Comparison by Filter - Target Domain (Cumulative)", 
                     fontsize=12, fontweight='bold', y=0.995)
        plt.tight_layout(pad=0.5, rect=[0, 0, 1, 0.99])
        
        # Save combined cumulative figure
        combined_cumulative_plot_path = os.path.join(args.output_dir, "model_comparison_magnitude_histogram_all_filters_cumulative.png")
        plt.savefig(combined_cumulative_plot_path, dpi=600, bbox_inches='tight', 
                    facecolor='white', edgecolor='none')
        plt.savefig(combined_cumulative_plot_path.replace('.png', '.pdf'), 
                    bbox_inches='tight', facecolor='white', edgecolor='none')
        plt.close()
        
        print(f"\nCombined multi-panel cumulative figure saved to: {combined_cumulative_plot_path}")
        print(f"PDF version: {combined_cumulative_plot_path.replace('.png', '.pdf')}")
    
    print("\nComparison completed successfully!")


if __name__ == "__main__":
    main()
