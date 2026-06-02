import numpy as np


def load_dataset(data_path, mmap=False, allow_npy_dict=True):
    print(f"Loading data from {data_path}")
    if allow_npy_dict and data_path.endswith(".npy"):
        data = np.load(data_path, allow_pickle=True).item()
    else:
        mmap_mode = "r" if mmap else None
        data = np.load(data_path, allow_pickle=True, mmap_mode=mmap_mode)

    if isinstance(data, np.lib.npyio.NpzFile):
        X = data["X"]
        feats = data["feats"]
        y = data["y"]
        metadata = data["metadata"]
    else:
        X = data["X"]
        feats = data["feats"]
        y = data["y"]
        metadata = data["metadata"]

    if X.ndim == 4 and X.shape[1] == 3 and X.shape[-1] != 3:
        X = X.transpose(0, 2, 3, 1)

    if getattr(feats, "dtype", None) == object:
        feats = np.asarray([list(f.values()) if hasattr(f, "values") else f for f in feats])

    y = np.asarray(y)

    print(f"Loaded data shapes - X: {X.shape}, feats: {feats.shape}, y: {y.shape}")
    print(f"Class distribution: {np.bincount(y.astype(int))}")
    return X, feats, y, metadata


def normalize_arrays(X, feats):
    X_norm = (X - X.mean(axis=(0, 1, 2), keepdims=True)) / (X.std(axis=(0, 1, 2), keepdims=True) + 1e-6)
    feats_norm = (feats - feats.mean(axis=0)) / (feats.std(axis=0) + 1e-6)
    return X_norm, feats_norm
