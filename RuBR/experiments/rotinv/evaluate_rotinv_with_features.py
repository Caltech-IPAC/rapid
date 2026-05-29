import os
import argparse
import numpy as np
import tensorflow as tf
import pandas as pd
from sklearn.metrics import classification_report
import matplotlib.pyplot as plt
import gc
from model.layers import rot90_k1, rot90_k2, rot90_k3

# Import load_data from the package train module
from experiments.rotinv.train_rotinv_with_features import load_data

def load_model(path):
    # Add custom Lambda functions to custom_objects for model loading
    model = tf.keras.models.load_model(
        path,
        custom_objects={
            'tf': tf,
            'rot90_k1': rot90_k1,
            'rot90_k2': rot90_k2,
            'rot90_k3': rot90_k3
        }
    )
    return model

def evaluate_model(model, X, feats, y):
    predictions = model.predict([X, feats], verbose=0).flatten()
    y_pred = predictions > 0.5
    assert y_pred.shape == y.shape
    accuracy = np.mean(y_pred == y)
    precision = (
        np.sum((y_pred == 1) & (y == 1)) / np.sum(y_pred == 1)
        if np.sum(y_pred == 1) > 0
        else 0
    )
    recall = (
        np.sum((y_pred == 1) & (y == 1)) / np.sum(y == 1) if np.sum(y == 1) > 0 else 0
    )
    return accuracy, precision, recall, predictions

def main():
    parser = argparse.ArgumentParser(description="Test a rot-inv-feat model on batched data")
    parser.add_argument(
        "--data_dir",
        type=str,
        required=True,
        help="Directory containing the batched data files (.npz)",
    )
    parser.add_argument(
        "--model_path",
        type=str,
        required=True,
        help="Path to the trained model file (.h5)",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./inj_only_outs_sqrt_var",
        help="Directory to save output plots (default: current directory)",
    )
    parser.add_argument(
        "--num_thresholds",
        type=int,
        default=100,
        help="Number of thresholds to test for precision-recall curve (default: 100)",
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    all_predictions = []
    all_y = []
    all_metadata = []
    model = load_model(args.model_path)

    print(f"Using model: {args.model_path}")
    print(f"Data directory: {args.data_dir}")
    print(f"Output directory: {args.output_dir}")
    print("=" * 50)

    for data_path in sorted(os.listdir(args.data_dir)):
        if data_path.endswith(".npz") or data_path.endswith(".npy"):
            print(f"Loading data from {data_path}")
            X, feats, y, metadata = load_data(os.path.join(args.data_dir, data_path))
            # Ensure feats is a 2D float array
            if feats.dtype == object:
                if len(feats) > 0 and isinstance(feats[0], dict):
                    feat_keys = sorted(feats[0].keys())
                    feats = np.array([[f[k] for k in feat_keys] for f in feats], dtype=np.float32)
                else:
                    feats = np.array([list(f.values()) if hasattr(f, "values") else f for f in feats], dtype=np.float32)
            else:
                feats = feats.astype(np.float32)
            X = X.astype(np.float32)
            y = y.astype(np.int32)

            accuracy, precision, recall, predictions = evaluate_model(model, X, feats, y)
            print(f"Model Accuracy: {accuracy:.4f}")
            print(f"Model Precision: {precision:.4f}")
            print(f"Model Recall: {recall:.4f}")
            all_metadata.extend(metadata)
            all_predictions.extend(predictions)
            all_y.extend(y)
            del X, feats
            gc.collect()

    thresholds = np.linspace(0, 1, args.num_thresholds)
    precisions = []
    recalls = []
    y = np.array(all_y)
    predictions = np.array(all_predictions)

    for t in thresholds:
        y_pred = predictions > t
        precision = (
            np.sum((y_pred == 1) & (y == 1)) / np.sum(y_pred == 1)
            if np.sum(y_pred == 1) > 0
            else 0
        )
        recall = (
            np.sum((y_pred == 1) & (y == 1)) / np.sum(y == 1)
            if np.sum(y == 1) > 0
            else 0
        )
        precisions.append(precision)
        recalls.append(recall)

    plt.figure(figsize=(4, 3))
    plt.plot(thresholds, precisions, label="Precision")
    plt.plot(thresholds, recalls, label="Recall")
    precisions = np.array(precisions)
    recalls = np.array(recalls)

    # Annotate precision and recall at precision of 90, 95, 98
    for target_precision in [0.90, 0.95, 0.98]:
        idx = (np.abs(precisions - target_precision)).argmin()
        t = thresholds[idx]
        p = precisions[idx]
        r = recalls[idx]
        plt.scatter([t], [p], color="red")
        plt.scatter([t], [r], color="green")
        plt.annotate(
            f"P={p:.2f}, R={r:.2f}\nT={t:.2f}",
            (t, p),
            textcoords="offset points",
            xytext=(0,10),
            ha='center',
            color="red",
            fontsize=9,
            arrowprops=dict(arrowstyle="->", color="red", lw=1)
        )

    plt.xlabel("Threshold")
    plt.ylabel("Score")
    plt.title("Precision and Recall vs Threshold")
    plt.legend()
    plt.grid(True)
    precision_recall_plot_path = os.path.join(
        args.output_dir, "precision_recall_vs_threshold.png"
    )
    plt.savefig(precision_recall_plot_path)
    print(f"Saved precision-recall plot to: {precision_recall_plot_path}")

    # Find the threshold where precision is closest to 0.9
    target_precision = 0.90
    precisions = np.array(precisions)
    thresholds = np.array(thresholds)
    idx = (np.abs(precisions - target_precision)).argmin()
    best_threshold = thresholds[idx]
    print(
        f"Threshold where precision is closest to {target_precision*100}%: {best_threshold:.3f} (Precision: {precisions[idx]:.3f}, Recall: {recalls[idx]:.3f})"
    )

    # Plot histogram of number of elements with y==1 in different metadata['mag'] bins
    mag_values = np.array([m["mag"] for m in all_metadata])
    y_positive = y == 1
    mag_positive = mag_values[y_positive]
    mag_predict_positive = mag_values[(predictions > best_threshold) & y_positive]
    plt.figure(figsize=(8, 6))
    plt.hist(
        [mag_positive, mag_predict_positive],
        label=["PSF Detections", "Model Detections"],
        color=["black", "blue"],
        histtype="step",
    )
    plt.yscale("log")
    plt.legend()
    plt.xlabel("Magnitude (mag)")
    plt.ylabel("Count")
    plt.title("Magnitude Histogram")
    plt.grid(True)
    histogram_plot_path = os.path.join(args.output_dir, "histogram_y1_mag_bins.png")
    plt.savefig(histogram_plot_path)
    print(f"Saved magnitude histogram to: {histogram_plot_path}")

    filters = np.array([m["filter"] for m in all_metadata])
    tp_filters = filters[y_positive]
    print("Unique filters:", np.unique(filters))
    filter_counts = pd.Series(filters).value_counts()
    print("Filter counts:\n", filter_counts)
    print("True Positive Filter counts:\n", pd.Series(tp_filters).value_counts())
    y_pred_final = predictions > best_threshold
    print("Final Classification Report (threshold={:.3f}):".format(best_threshold))
    print(classification_report(y, y_pred_final, digits=4))

if __name__ == "__main__":
    main()
