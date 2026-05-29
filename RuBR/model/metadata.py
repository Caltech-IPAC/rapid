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
