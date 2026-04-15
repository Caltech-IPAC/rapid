#!/usr/bin/env python3
"""
Read and display sample RAPID alert packets.

Usage:
    python read_sample.py                          # read sample/sample_alerts.avro
    python read_sample.py output/jid1061_alerts.avro  # read any .avro file
    python read_sample.py --stamps                 # also display cutout stamps
"""

import argparse
import io
import math
import os
import sys

import fastavro
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(SCRIPT_DIR, 'sample', 'sample_alerts.avro')


def parse_args():
    p = argparse.ArgumentParser(description='Read and display RAPID alert packets')
    p.add_argument('avro_file', nargs='?', default=DEFAULT_PATH,
                   help='Path to .avro alert file (default: sample/sample_alerts.avro)')
    p.add_argument('--stamps', action='store_true',
                   help='Display cutout stamps using matplotlib')
    p.add_argument('-n', '--max-alerts', type=int, default=None,
                   help='Maximum number of alerts to display')
    return p.parse_args()


def flux_to_mag(flux_njy):
    """Convert nanojansky to AB magnitude."""
    if flux_njy is None or flux_njy <= 0:
        return None
    return -2.5 * math.log10(flux_njy) + 31.4


def show_stamps(alert):
    """Display cutout stamps for one alert."""
    from astropy.io import fits as afits
    import matplotlib.pyplot as plt

    labels = ['Difference', 'Science', 'Template']
    keys = ['cutoutDifference', 'cutoutScience', 'cutoutTemplate']

    stamps = []
    for key in keys:
        raw = alert.get(key)
        if raw is not None:
            hdul = afits.open(io.BytesIO(raw))
            stamps.append(hdul[0].data)
        else:
            stamps.append(None)

    n_valid = sum(s is not None for s in stamps)
    if n_valid == 0:
        print('    (no cutout stamps)')
        return

    fig, axes = plt.subplots(1, 3, figsize=(12, 4))
    for ax, stamp, label in zip(axes, stamps, labels):
        if stamp is not None:
            vmin, vmax = np.nanpercentile(stamp, [5, 95])
            ax.imshow(stamp, origin='lower', cmap='gray', vmin=vmin, vmax=vmax)
            cx, cy = stamp.shape[1] // 2, stamp.shape[0] // 2
            ax.plot(cx, cy, 'c+', ms=8, mew=1)
        else:
            ax.text(0.5, 0.5, 'N/A', ha='center', va='center', transform=ax.transAxes)
        ax.set_title(label)
    src = alert['diaSource']
    fig.suptitle(f'diaSourceId={alert["diaSourceId"]}  band={src["band"]}')
    plt.tight_layout()
    plt.show()


def print_alert(alert, index, show_cutouts=False):
    """Print a summary of one alert."""
    src = alert['diaSource']
    obj = alert.get('diaObject')
    prv = alert.get('prvDiaSources')

    flux = src.get('psfFlux')
    mag = flux_to_mag(flux)
    mag_str = f'{mag:.1f} AB' if mag is not None else 'N/A'
    flux_str = f'{flux:.1f} nJy' if flux is not None else 'N/A'

    print(f'\n--- Alert {index} ---')
    print(f'  diaSourceId:   {alert["diaSourceId"]}')
    print(f'  band:          {src["band"]}')
    print(f'  RA, Dec:       {src["ra"]:.6f}, {src["dec"]:.6f}')
    print(f'  MJD:           {src["midpointMjdTai"]}')
    print(f'  psfFlux:       {flux_str}  ({mag_str})')
    print(f'  SNR:           {src.get("snr", "N/A")}')
    print(f'  visit:         {src["visit"]}  detector: {src["detector"]}')

    # PSF-fit quality
    sharp = src.get('sharpness')
    if sharp is not None:
        print(f'  sharpness:     {sharp:.3f}  '
              f'roundness1: {src.get("roundness1", "N/A")}  '
              f'peak: {src.get("peak", "N/A")}')

    # diaObject
    if obj:
        print(f'  diaObject:     id={obj["diaObjectId"]}  '
              f'nDiaSources={obj["nDiaSources"]}  '
              f'MJD range: {obj.get("firstDiaSourceMjdTai", "?")} -- '
              f'{obj.get("lastDiaSourceMjdTai", "?")}')
    else:
        print(f'  diaObject:     None')

    # prvDiaSources
    if prv:
        bands = set(p['band'] for p in prv)
        print(f'  prvDiaSources: {len(prv)} epochs  bands: {", ".join(sorted(bands))}')
    else:
        print(f'  prvDiaSources: None')

    # Cutouts
    has_diff = alert.get('cutoutDifference') is not None
    has_sci = alert.get('cutoutScience') is not None
    has_tpl = alert.get('cutoutTemplate') is not None
    print(f'  cutouts:       diff={has_diff}  sci={has_sci}  tpl={has_tpl}')

    if show_cutouts:
        show_stamps(alert)


def main():
    args = parse_args()

    if not os.path.exists(args.avro_file):
        print(f'Error: file not found: {args.avro_file}')
        sys.exit(1)

    with open(args.avro_file, 'rb') as f:
        reader = fastavro.reader(f)
        schema = reader.writer_schema
        alerts = list(reader)

    print(f'File: {args.avro_file}')
    schema_name = schema.get('name', 'unknown')
    if schema.get('namespace'):
        schema_name = f'{schema["namespace"]}.{schema_name}'
    print(f'Schema: {schema_name}')
    print(f'Total alerts: {len(alerts)}')

    # Summary statistics
    n_with_obj = sum(1 for a in alerts if a.get('diaObject'))
    n_with_prv = sum(1 for a in alerts if a.get('prvDiaSources'))
    n_with_stamp = sum(1 for a in alerts if a.get('cutoutDifference'))
    bands = set(a['diaSource']['band'] for a in alerts)
    print(f'With diaObject:     {n_with_obj}/{len(alerts)}')
    print(f'With prvDiaSources: {n_with_prv}/{len(alerts)}')
    print(f'With cutouts:       {n_with_stamp}/{len(alerts)}')
    print(f'Bands:              {", ".join(sorted(bands))}')

    limit = args.max_alerts or len(alerts)
    for i, alert in enumerate(alerts[:limit]):
        print_alert(alert, i, show_cutouts=args.stamps)

    if limit < len(alerts):
        print(f'\n... ({len(alerts) - limit} more alerts not shown)')


if __name__ == '__main__':
    main()
