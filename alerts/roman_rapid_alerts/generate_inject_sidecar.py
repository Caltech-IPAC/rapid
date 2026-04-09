#!/usr/bin/env python3
"""
Generate inject_sidecar.parquet by cross-matching simple-inject catalogs
against SExtractor detection catalogs.

Each job has ~100 point sources injected into the difference image.
This script finds which injects were detected (within MATCH_RADIUS_PX pixels)
and records the matched diaSourceId, plus the OU2024 truth label at that position
(from truth_labels.parquet).

Usage:
    python generate_inject_sidecar.py
    python generate_inject_sidecar.py --data-dir ./data/20260227 --inject-dir ./provenance/inject_catalogs
"""

import argparse
import glob
import json
import math
import os
import re

import pandas as pd

MATCH_RADIUS_PX = 3.0

# Default paths relative to this script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_DATA_DIR    = os.path.join(SCRIPT_DIR, 'data', '20260227')
DEFAULT_INJECT_DIR  = os.path.join(SCRIPT_DIR, 'provenance', 'inject_catalogs')
DEFAULT_HEADER_DIR  = os.path.join(SCRIPT_DIR, 'provenance', 'headers')
DEFAULT_TRUTH_PATH  = os.path.join(SCRIPT_DIR, 'truth_sidecar', 'truth_labels.parquet')
DEFAULT_OUTPUT_PATH = os.path.join(SCRIPT_DIR, 'truth_sidecar', 'inject_sidecar.parquet')


def parse_sextractor(path):
    """Parse a SExtractor catalog file, returning list of dicts."""
    col_map = {}
    rows = []
    with open(path) as f:
        for line in f:
            line = line.rstrip('\n')
            if line.startswith('#'):
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        col_map[int(parts[1])] = parts[2]
                    except ValueError:
                        pass
            elif line.strip():
                vals = line.split()
                row = {}
                for col_num, name in col_map.items():
                    idx = col_num - 1
                    if idx < len(vals):
                        try:
                            row[name] = float(vals[idx])
                        except ValueError:
                            row[name] = None
                rows.append(row)
    return rows


def parse_inject_catalog(path):
    """Parse whitespace-separated inject catalog (xpix, ypix, flux)."""
    injects = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('xpix'):
                continue
            parts = line.split()
            if len(parts) >= 3:
                injects.append({
                    'xpix': float(parts[0]),
                    'ypix': float(parts[1]),
                    'flux': float(parts[2]),
                })
    return injects


def find_nearest_detection(xpix, ypix, detections):
    """Find nearest SExtractor detection within MATCH_RADIUS_PX."""
    best_dist = MATCH_RADIUS_PX + 1
    best = None
    for det in detections:
        dx = xpix - det.get('XWIN_IMAGE', 0)
        dy = ypix - det.get('YWIN_IMAGE', 0)
        d = math.hypot(dx, dy)
        if d < best_dist:
            best_dist = d
            best = det
    if best is not None and best_dist <= MATCH_RADIUS_PX:
        return best, best_dist
    return None, None


def parse_args():
    p = argparse.ArgumentParser(
        description='Generate inject sidecar by cross-matching inject catalogs '
                    'to SExtractor detections')
    p.add_argument('--data-dir', default=DEFAULT_DATA_DIR,
                   help='Directory containing jid*/ job folders with '
                        'SExtractor catalogs (default: %(default)s)')
    p.add_argument('--inject-dir', default=DEFAULT_INJECT_DIR,
                   help='Directory containing inject catalog files '
                        '(default: %(default)s)')
    p.add_argument('--header-dir', default=DEFAULT_HEADER_DIR,
                   help='Directory containing saved FITS header JSONs '
                        '(default: %(default)s)')
    p.add_argument('--truth-path', default=DEFAULT_TRUTH_PATH,
                   help='Path to truth_labels.parquet '
                        '(default: %(default)s)')
    p.add_argument('--output-path', default=DEFAULT_OUTPUT_PATH,
                   help='Output path for inject_sidecar.parquet '
                        '(default: %(default)s)')
    return p.parse_args()


def main():
    args = parse_args()

    # Load truth labels to look up OU2024 obj_type for matched detections
    truth_df = pd.read_parquet(args.truth_path)
    truth_df = truth_df.drop_duplicates(subset=['diaSourceId'], keep='first')
    truth_lookup = dict(zip(truth_df['diaSourceId'], truth_df['obj_type']))

    # Find all inject catalog files and extract jid from filename
    inject_files = sorted(glob.glob(os.path.join(args.inject_dir, 'jid*_*_inject.txt')))
    print(f'Found {len(inject_files)} inject catalogs')

    all_rows = []

    for inject_path in inject_files:
        fname = os.path.basename(inject_path)
        jid = re.match(r'(jid\d+)_', fname).group(1)

        header_path = os.path.join(args.header_dir, f'{jid}_header.json')
        with open(header_path) as f:
            hdr = json.load(f)
        expid = int(hdr['EXPID'])

        sext_path = os.path.join(args.data_dir, jid, 'diffimage_masked.txt')
        detections = parse_sextractor(sext_path)

        injects = parse_inject_catalog(inject_path)

        n_detected = 0
        for inject_idx, inj in enumerate(injects):
            det, dist = find_nearest_detection(inj['xpix'], inj['ypix'], detections)

            if det is not None:
                number = int(det.get('NUMBER', 0) or 0)
                dia_source_id = expid * 1_000_000 + number
                ou24_type = truth_lookup.get(dia_source_id)
                n_detected += 1
            else:
                dia_source_id = None
                ou24_type = None
                dist = None

            all_rows.append({
                'jid':            jid,
                'inject_idx':     inject_idx,
                'xpix':           inj['xpix'],
                'ypix':           inj['ypix'],
                'flux':           inj['flux'],
                'detected':       det is not None,
                'diaSourceId':    dia_source_id,
                'match_dist_px':  dist,
                'ou24_obj_type':  ou24_type,
            })

        print(f'  {jid}: {len(injects)} injects, {n_detected} detected '
              f'({100*n_detected/len(injects):.0f}%)')

    inject_df = pd.DataFrame(all_rows)
    inject_df['diaSourceId'] = inject_df['diaSourceId'].astype('Int64')

    os.makedirs(os.path.dirname(args.output_path), exist_ok=True)
    inject_df.to_parquet(args.output_path, index=False)
    print(f'\nSaved {len(inject_df)} rows to {args.output_path}')

    # Summary stats
    n_total = len(inject_df)
    n_det = inject_df['detected'].sum()
    n_undet = n_total - n_det
    det_with_ou24 = inject_df[inject_df['detected'] & inject_df['ou24_obj_type'].notna()]
    det_bogus = inject_df[inject_df['detected'] & inject_df['ou24_obj_type'].isna()]

    print(f'\nSummary:')
    print(f'  Total injects:     {n_total}')
    print(f'  Detected:          {n_det} ({100*n_det/n_total:.1f}%)')
    print(f'  Undetected:        {n_undet} ({100*n_undet/n_total:.1f}%)')
    print(f'  Detected + OU24 match:  {len(det_with_ou24)} '
          f'(labeled as real by OU2024 truth)')
    print(f'  Detected + no OU24:     {len(det_bogus)} '
          f'(mislabeled as bogus -- these should be REAL)')

    if len(det_with_ou24) > 0:
        print(f'\n  OU2024 type breakdown for detected injects:')
        print(det_with_ou24['ou24_obj_type'].value_counts().to_string(header=False))


if __name__ == '__main__':
    main()
