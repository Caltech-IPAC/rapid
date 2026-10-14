# DEPRECATED - kept for reference only; not imported by the pipeline and not maintained.
# Origin: rapid/RuBR/model/metadata.py (Ashish Mahabal, commit 90fbc83b, 2026-05-29), moved here
# 2026-10-09.  Metadata helpers for the earlier six-feature RotInv model, used by the scripts in
# legacy/dann/.

import os
import pandas as pd


def get_transient_magnitude(id, jid_folder):
    file_name = os.path.join(jid_folder, "combined_truth_table.csv")
    if not os.path.exists(file_name):
        raise FileNotFoundError(f"File not found: {file_name}")

    df = pd.read_csv(file_name)
    id = str(id)
    if not id.endswith("_ou"):
        id += "_ou"
    row = df[df["id"] == id]
    if row.empty:
        raise ValueError(f"ID {id} not found in {file_name}")
    mag = row.iloc[0]["mag"] + row.iloc[0]["zpt"]
    return mag
