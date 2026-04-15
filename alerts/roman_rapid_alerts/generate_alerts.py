#!/usr/bin/env python3
"""
Generate RAPID alert packets using the v01.00 schema (rapid.v01_00).

For each job in the data directory, reads:
  - diffimage_masked.txt          SExtractor catalog
  - diffimage_masked_psfcat.parquet  PSF-fit catalog (sharpness, roundness, etc.)
  - diffimage_masked.fits         ZOGY difference image (cutoutDifference)
  - bkg_subbed_science_image.fits Science image (cutoutScience)
  - awaicgen_output_mosaic_image_resampled_gainmatched.fits  Reference (cutoutTemplate)

Also reads Open Universe truth catalog for the truth sidecar (separate file).

Writes one .avro file per job to the output directory.
Writes truth_sidecar/truth_labels.parquet with ground-truth labels.

Usage:
    python generate_alerts.py
    python generate_alerts.py --data-dir /path/to/data/20260227 --output-dir ./output
"""

import argparse
import io
import os
import json
import math
import fastavro
import fastavro.schema
from astropy.io import fits
from astropy_healpix import HEALPix
from astropy.coordinates import SkyCoord, ICRS
import astropy.units as u
import healpy
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

# ---------------------------------------------------------------------------
# Default paths (relative to this script)
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_SCHEMA_DIR  = os.path.join(SCRIPT_DIR, 'schema', '01', '00')
DEFAULT_DATA_DIR    = os.path.join(SCRIPT_DIR, 'data', '20260227')
DEFAULT_OUTPUT_DIR  = os.path.join(SCRIPT_DIR, 'output')
DEFAULT_SIDECAR_DIR = os.path.join(SCRIPT_DIR, 'truth_sidecar')
DEFAULT_LC_TILE_DIR = os.path.join(SCRIPT_DIR, 'data', 'lc_tiles')
DEFAULT_PROVENANCE_DIR = os.path.join(SCRIPT_DIR, 'provenance')

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STAMP_SIZE      = 64            # cutout half-width: final stamp is 2*STAMP_SIZE+1 pixels
LC_MATCH_ARCSEC = 1.0           # lc catalog cross-match radius
LC_S3_BASE      = 's3://rapid-product-files/lightcurve_hats_catalog/dataset'
MATCH_RADIUS_PX = 3.0           # truth cross-match radius in pixels

# Schema files in dependency order
SCHEMA_FILES = [
    'rapid.v01_00.diaSource.avsc',
    'rapid.v01_00.diaForcedSource.avsc',
    'rapid.v01_00.diaObject.avsc',
    'rapid.v01_00.ssSource.avsc',
    'rapid.v01_00.mpc_orbits.avsc',
    'rapid.v01_00.alert.avsc',
]

ROMAN_FILTERS = ['F062', 'F087', 'F106', 'F129', 'F146', 'F158', 'F184', 'F213']

# fid -> filter string (confirmed from FITS headers, 20260227 run)
FID_TO_FILTER = {
    1: 'F184', 2: 'F158', 3: 'F129', 4: 'F213', 5: 'F062', 6: 'F106', 7: 'F087',
}
# Header filter name -> new schema band name
FILTER_TO_BAND = {
    'F184': 'F184', 'H158': 'F158', 'J129': 'F129', 'K213': 'F213',
    'R062': 'F062', 'Y106': 'F106', 'Z087': 'F087',
}

# Per-filter zero-points (constant per filter in 20260227 run)
# ZPTMAG: from OpenUniverse headers, for DN (includes EXPTIME + collecting area)
FILTER_ZPTMAG = {
    'F184': 18.824126, 'F158': 17.638109, 'F129': 17.638109,
    'F213': 18.824126, 'F062': 16.954336, 'F106': 17.638109,
    'F087': 16.455405,
}

# BANDZPT: bandpass-dependent zero-point (filter transmission sensitivity)
FILTER_BANDZPT = {
    'F184': 14.62199399091576,
    'F158': 15.074164829833911,
    'F129': 15.03962528980125,
    'F106': 15.023547191066587,
    'F087': 14.964334468671934,
    'F062': 15.296832274841094,
    'F213': 14.579315583138646,
}

# Exposure times per filter (seconds)
FILTER_EXPTIME = {
    'F184': 901.175, 'F158': 302.275, 'F129': 302.275,
    'F213': 901.175, 'F062': 161.0, 'F106': 302.275,
    'F087': 101.7,
}

# Effective zero-point for DN/s -> AB mag:
#   ZP_eff = BANDZPT + ZPTMAG - 2.5 * log10(EXPTIME)
# Images are in DN/s, so: mag_AB = -2.5 * log10(f_DN/s) + ZP_eff
FILTER_ZP_EFF = {
    band: FILTER_BANDZPT[band] + FILTER_ZPTMAG[band] - 2.5 * math.log10(FILTER_EXPTIME[band])
    for band in FILTER_BANDZPT
}

# ---------------------------------------------------------------------------
# Module-level path variables (set by main via CLI args)
# ---------------------------------------------------------------------------
SCHEMA_DIR = DEFAULT_SCHEMA_DIR
DATA_DIR = DEFAULT_DATA_DIR
OUTPUT_DIR = DEFAULT_OUTPUT_DIR
SIDECAR_DIR = DEFAULT_SIDECAR_DIR
LC_TILE_DIR = DEFAULT_LC_TILE_DIR
PROVENANCE_DIR = DEFAULT_PROVENANCE_DIR

# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

def load_schema():
    paths = [os.path.join(SCHEMA_DIR, f) for f in SCHEMA_FILES]
    return fastavro.schema.load_schema_ordered(paths)

# ---------------------------------------------------------------------------
# FITS header parsing
# ---------------------------------------------------------------------------

def parse_fits_header(path):
    """Read FITS header -- supports both full FITS and partial header files."""
    if path.endswith('.fits') and os.path.getsize(path) > 36000:
        try:
            with fits.open(path) as hdul:
                h = hdul[0].header
                return {k: str(h[k]).strip() for k in h.keys() if k}
        except Exception:
            pass
    with open(path, 'rb') as f:
        data = f.read()
    hdr = {}
    for i in range(0, len(data), 80):
        card = data[i:i+80].decode('ascii', errors='replace')
        key = card[:8].strip()
        if key == 'END':
            break
        if '=' in card:
            raw = card[10:80].split('/')[0].strip().strip("'").strip()
            hdr[key] = raw
    return hdr


def hdr_float(hdr, key, default=None):
    try:
        return float(hdr[key])
    except (KeyError, ValueError):
        return default


def hdr_int(hdr, key, default=None):
    try:
        return int(float(hdr[key]))
    except (KeyError, ValueError):
        return default

# ---------------------------------------------------------------------------
# SExtractor catalog parsing
# ---------------------------------------------------------------------------

def parse_sextractor(path):
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

# ---------------------------------------------------------------------------
# PSF-fit parquet loading and cross-match to SExtractor
# ---------------------------------------------------------------------------

def load_psf_catalog(job_dir):
    """Load PSF-fit parquet and return (DataFrame, cKDTree on pixel coords)."""
    path = os.path.join(job_dir, 'diffimage_masked_psfcat.parquet')
    if not os.path.exists(path):
        return None, None
    df = pd.read_parquet(path)
    if len(df) == 0:
        return None, None
    xy = np.column_stack([df['x_fit'].values, df['y_fit'].values])
    tree = cKDTree(xy)
    return df, tree


def match_psf(xpos, ypos, psf_df, psf_tree, radius=5.0):
    """Return nearest PSF-fit row within radius pixels, or None."""
    if psf_df is None or xpos is None or ypos is None:
        return None
    dist, idx = psf_tree.query([xpos, ypos], distance_upper_bound=radius)
    if idx >= len(psf_df):
        return None
    return psf_df.iloc[idx]

# ---------------------------------------------------------------------------
# Open Universe truth catalog
# ---------------------------------------------------------------------------

def parse_truth(path):
    if path is None or not os.path.exists(path):
        return []
    sources = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) < 9:
                continue
            sources.append({
                'object_id': int(parts[0]),
                'ra':        float(parts[1]),
                'dec':       float(parts[2]),
                'x':         float(parts[3]),
                'y':         float(parts[4]),
                'realized_flux': float(parts[5]),
                'flux':      float(parts[6]),
                'mag':       float(parts[7]),
                'obj_type':  parts[8],
            })
    return sources


def match_truth(xpos, ypos, truth_sources):
    best_dist, best = MATCH_RADIUS_PX + 1, None
    for src in truth_sources:
        d = math.hypot(xpos - src['x'], ypos - src['y'])
        if d < best_dist:
            best_dist, best = d, src
    return best

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mag_to_flux_njy(mag):
    if mag is None or math.isnan(mag) or mag > 90.0:
        return None
    return 3.631e12 * 10.0 ** (-mag / 2.5)


def safe_float(val, limit=90.0, scale=None):
    if val is None:
        return None
    try:
        v = float(val)
        if not math.isfinite(v) or v >= limit:
            return None
        return v * scale if scale is not None else v
    except (TypeError, ValueError):
        return None

# ---------------------------------------------------------------------------
# FITS cutout extraction (raw bytes for new schema)
# ---------------------------------------------------------------------------

def load_image(path):
    if path is None or not os.path.exists(path):
        return None
    try:
        with fits.open(path, memmap=True) as hdul:
            return hdul[0].data
    except Exception:
        return None


def extract_stamp(data, xpos, ypos, size=STAMP_SIZE):
    """Extract cutout and return raw FITS bytes."""
    if data is None or xpos is None or ypos is None:
        return None
    col = int(round(xpos)) - 1
    row = int(round(ypos)) - 1
    nrows, ncols = data.shape
    r0, r1 = row - size, row + size + 1
    c0, c1 = col - size, col + size + 1
    if r0 < 0 or r1 > nrows or c0 < 0 or c1 > ncols:
        return None
    stamp = data[r0:r1, c0:c1].astype(np.float32)
    buf = io.BytesIO()
    hdu = fits.PrimaryHDU(stamp)
    hdu.writeto(buf)
    return buf.getvalue()

# ---------------------------------------------------------------------------
# LC catalog tile loading and cross-match
# ---------------------------------------------------------------------------
_lc_tile_cache = {}


def _lc_tile_path(norder, npix):
    os.makedirs(LC_TILE_DIR, exist_ok=True)
    return os.path.join(LC_TILE_DIR, f'{norder}_{npix}.parquet')


def _download_lc_tile(norder, npix):
    path = _lc_tile_path(norder, npix)
    if os.path.exists(path):
        return path
    dir_val = (npix // 10000) * 10000
    s3_key = f'{LC_S3_BASE}/Norder={norder}/Dir={dir_val}/Npix={npix}.parquet'
    import subprocess
    result = subprocess.run(
        ['aws', 's3', 'cp', s3_key, path, '--no-sign-request', '--quiet'],
        capture_output=True)
    if result.returncode != 0:
        print(f'  WARN: could not download lc tile Norder={norder} Npix={npix}')
        return None
    return path


def load_lc_tile(ra_deg, dec_deg):
    coord = SkyCoord(ra=ra_deg*u.deg, dec=dec_deg*u.deg, frame='icrs')
    for norder in [4, 5, 6]:
        hp = HEALPix(nside=2**norder, order='nested', frame=ICRS())
        npix = int(hp.skycoord_to_healpix(coord))
        if (norder, npix) in _lc_tile_cache:
            return _lc_tile_cache[(norder, npix)]
        path = _download_lc_tile(norder, npix)
        if path and os.path.exists(path):
            df = pd.read_parquet(path)
            if len(df) == 0:
                continue
            ra_r  = np.deg2rad(df['meanra'].values)
            dec_r = np.deg2rad(df['meandec'].values)
            xyz = np.column_stack([
                np.cos(dec_r) * np.cos(ra_r),
                np.cos(dec_r) * np.sin(ra_r),
                np.sin(dec_r),
            ])
            tree = cKDTree(xyz)
            _lc_tile_cache[(norder, npix)] = (df, tree)
            return df, tree
    return None, None


def match_lc(ra_deg, dec_deg, lc_df, lc_tree):
    if lc_df is None or ra_deg is None or dec_deg is None:
        return None
    ra_r  = math.radians(ra_deg)
    dec_r = math.radians(dec_deg)
    xyz   = [math.cos(dec_r)*math.cos(ra_r),
             math.cos(dec_r)*math.sin(ra_r),
             math.sin(dec_r)]
    thresh = 2.0 * math.sin(math.radians(LC_MATCH_ARCSEC / 3600.0) / 2.0)
    dist, idx = lc_tree.query(xyz, distance_upper_bound=thresh)
    if idx >= len(lc_df):
        return None
    return lc_df.iloc[idx]

# ---------------------------------------------------------------------------
# diaObject builder (new schema: per-filter stats)
# ---------------------------------------------------------------------------

def build_dia_object(lc_row, current_mjd):
    """Build diaObject with per-filter flux statistics from lc data."""
    nd = lc_row['nested_lc_data']
    mjds = [float(m) for m in nd['mjdobs']]

    obj = {
        'diaObjectId':          int(lc_row['aid']),
        'ra':                   float(lc_row['meanra']),
        'dec':                  float(lc_row['meandec']),
        'raErr':                None,
        'decErr':               None,
        'nDiaSources':          int(lc_row['nsources']),
        'firstDiaSourceMjdTai': float(min(mjds)) if mjds else None,
        'lastDiaSourceMjdTai':  float(max(mjds)) if mjds else None,
        'validityStartMjdTai':  float(current_mjd),
    }

    # Compute per-filter flux statistics from light curve data
    filter_fluxes = {}
    for i in range(len(nd['fid'])):
        fid = int(nd['fid'][i])
        band = FID_TO_FILTER.get(fid)
        if band is None:
            continue
        flux = float(nd['fluxfit'][i])
        zp_eff = FILTER_ZP_EFF.get(band)
        if zp_eff is not None and flux > 0:
            mag = zp_eff - 2.5 * math.log10(flux)
            flux_njy = mag_to_flux_njy(mag)
            if flux_njy is not None:
                filter_fluxes.setdefault(band, []).append(flux_njy)

    for filt in ROMAN_FILTERS:
        fluxes = filter_fluxes.get(filt, [])
        if fluxes:
            arr = np.array(fluxes)
            obj[f'{filt}PsfFluxMean']  = float(np.mean(arr))
            obj[f'{filt}PsfFluxSigma'] = float(np.std(arr)) if len(arr) > 1 else None
            obj[f'{filt}PsfFluxNdata'] = len(arr)
            obj[f'{filt}PsfFluxMin']   = float(np.min(arr))
            obj[f'{filt}PsfFluxMax']   = float(np.max(arr))
        else:
            obj[f'{filt}PsfFluxMean']  = None
            obj[f'{filt}PsfFluxSigma'] = None
            obj[f'{filt}PsfFluxNdata'] = None
            obj[f'{filt}PsfFluxMin']   = None
            obj[f'{filt}PsfFluxMax']   = None

    return obj

# ---------------------------------------------------------------------------
# prvDiaSource builder (new schema: full diaSource records)
# ---------------------------------------------------------------------------

def build_prv_dia_sources(lc_row, current_expid, current_mjd):
    """Build list of full diaSource dicts from lc data, excluding current_expid
    and any detections at or after current_mjd (only genuinely prior observations)."""
    nd  = lc_row['nested_lc_data']
    ra  = float(lc_row['meanra'])
    dec = float(lc_row['meandec'])
    prv = []

    for i in range(len(nd['expid'])):
        expid = int(nd['expid'][i])
        if expid == current_expid:
            continue
        if float(nd['mjdobs'][i]) >= current_mjd:
            continue

        fid     = int(nd['fid'][i])
        band    = FID_TO_FILTER.get(fid, 'F184')
        flux_dn = float(nd['fluxfit'][i])
        fluxerr_dn = float(nd['fluxerr'][i])
        zp_eff = FILTER_ZP_EFF.get(band)

        psf_flux = None
        psf_flux_err = None
        if zp_eff is not None and flux_dn > 0:
            mag = zp_eff - 2.5 * math.log10(flux_dn)
            psf_flux = mag_to_flux_njy(mag)
            if fluxerr_dn > 0 and psf_flux is not None:
                magerr = 2.5 / math.log(10) * (fluxerr_dn / flux_dn)
                psf_flux_err = psf_flux * magerr * math.log(10) / 2.5

        snr = (flux_dn / fluxerr_dn) if fluxerr_dn > 0 else None
        sca = int(nd['sca'][i])

        prv.append({
            'diaSourceId':    int(nd['sid'][i]),
            'visit':          expid,
            'detector':       sca,
            'diaObjectId':    int(lc_row['aid']),
            'ssObjectId':     None,
            'parentDiaSourceId': None,
            'midpointMjdTai': float(nd['mjdobs'][i]),
            'ra':             ra,
            'dec':            dec,
            'raErr':          None,
            'decErr':         None,
            'x':              0.0,
            'y':              0.0,
            'xErr':           None,
            'yErr':           None,
            'band':           band,
            'psfFlux':        float(psf_flux) if psf_flux else None,
            'psfFluxErr':     float(psf_flux_err) if psf_flux_err else None,
            'snr':            float(snr) if snr else None,
            'extendedness':   None,
            'reliability':    None,
            'flags':          0,
            'apFlux': None, 'apFluxErr': None,
            'trailFlux': None, 'trailFluxErr': None,
            'trailLength': None, 'trailAngle': None,
            'scienceFlux': None, 'scienceFluxErr': None,
            'templateFlux': None, 'templateFluxErr': None,
            'dipoleMeanFlux': None, 'dipoleFluxErr': None,
            'dipoleLength': None, 'dipoleAngle': None,
            'ixx': None, 'iyy': None, 'ixy': None,
            'ixxErr': None, 'iyyErr': None, 'ixyErr': None,
            'pixelFlags_saturated': None, 'pixelFlags_bad': None,
            'pixelFlags_edge': None, 'pixelFlags_cr': None,
            'timeProcessedMjdTai': None, 'timeWithdrawnMjdTai': None,
            'sca':          sca,
            'field':        0,
            'hp6':          healpy.ang2pix(64, ra, dec, nest=True, lonlat=True),
            'hp9':          healpy.ang2pix(512, ra, dec, nest=True, lonlat=True),
            'pid':          0,
            'expid':        expid,
            'isdiffpos':    bool(nd['isdiffpos'][i]),
            'qfit': None, 'cfit': None, 'redchi': None, 'npixfit': None,
            'sharpness': None, 'roundness1': None, 'roundness2': None,
            'peak': None,
        })

    return prv if prv else None

# ---------------------------------------------------------------------------
# Alert builder
# ---------------------------------------------------------------------------

def build_alert(row, hdr, psf_row=None, sci_data=None, ref_data=None,
                diff_data=None, truth_match=None, lc_df=None, lc_tree=None):
    """Build one alert dict from a SExtractor row + header + PSF-fit row."""
    number   = int(row.get('NUMBER', 0) or 0)
    expid    = hdr_int(hdr,   'EXPID',   0)
    sca_num  = hdr_int(hdr,   'SCA_NUM', 0)
    field_id = hdr_int(hdr,   'FIELD',   0)
    mjd      = hdr_float(hdr, 'MJD-OBS', 0.0)
    filt_hdr = hdr.get('FILTER', 'F184').strip()
    band     = FILTER_TO_BAND.get(filt_hdr, filt_hdr)

    dia_source_id = expid * 1_000_000 + number

    ra    = row.get('ALPHAWIN_J2000')
    dec   = row.get('DELTAWIN_J2000')
    xpos  = row.get('XWIN_IMAGE')
    ypos  = row.get('YWIN_IMAGE')
    flags = int(row.get('FLAGS', 0) or 0)

    ra_err  = safe_float(row.get('ERRAWIN_WORLD'))
    dec_err = safe_float(row.get('ERRBWIN_WORLD'))

    # Photometry: SExtractor MAG_SE + ZP_eff -> AB -> nJy
    zp_eff = FILTER_ZP_EFF.get(band)
    mag_se_best = safe_float(row.get('MAG_BEST'))
    mag_best = (mag_se_best + zp_eff
                if mag_se_best is not None and zp_eff is not None else None)
    magerr_best = safe_float(row.get('MAGERR_BEST'))
    psf_flux    = mag_to_flux_njy(mag_best)
    psf_flux_err = None
    if psf_flux is not None and magerr_best is not None:
        psf_flux_err = psf_flux * magerr_best * math.log(10) / 2.5

    # Aperture photometry
    mag_se_auto = safe_float(row.get('MAG_AUTO'))
    mag_auto = (mag_se_auto + zp_eff
                if mag_se_auto is not None and zp_eff is not None else None)
    ap_flux = mag_to_flux_njy(mag_auto)

    flux_best    = row.get('FLUX_BEST')
    fluxerr_best = row.get('FLUXERR_BEST')
    snr = (flux_best / fluxerr_best
           if flux_best and fluxerr_best and fluxerr_best != 0.0
           else None)

    # HEALPix indices
    ra_f  = float(ra) if ra is not None else 0.0
    dec_f = float(dec) if dec is not None else 0.0
    hp6 = healpy.ang2pix(64, ra_f, dec_f, nest=True, lonlat=True)
    hp9 = healpy.ang2pix(512, ra_f, dec_f, nest=True, lonlat=True)

    # PSF-fit quality metrics from parquet
    sharpness  = float(psf_row['sharpness'])  if psf_row is not None and pd.notna(psf_row.get('sharpness'))  else None
    roundness1 = float(psf_row['roundness1']) if psf_row is not None and pd.notna(psf_row.get('roundness1')) else None
    roundness2 = float(psf_row['roundness2']) if psf_row is not None and pd.notna(psf_row.get('roundness2')) else None
    peak       = float(psf_row['peak'])       if psf_row is not None and pd.notna(psf_row.get('peak'))       else None
    qfit       = float(psf_row['qfit'])       if psf_row is not None and pd.notna(psf_row.get('qfit'))       else None
    cfit       = float(psf_row['cfit'])       if psf_row is not None and pd.notna(psf_row.get('cfit'))       else None
    redchi     = float(psf_row['reduced_chi2']) if psf_row is not None and pd.notna(psf_row.get('reduced_chi2')) else None
    npixfit    = int(psf_row['npixfit'])       if psf_row is not None and pd.notna(psf_row.get('npixfit'))    else None

    # LC catalog cross-match -> diaObject + prvDiaSources
    lc_match = match_lc(ra_f, dec_f, lc_df, lc_tree)
    dia_object = build_dia_object(lc_match, mjd) if lc_match is not None else None
    prv_dia_sources = build_prv_dia_sources(lc_match, expid, mjd) if lc_match is not None else None
    resolved_object_id = int(lc_match['aid']) if lc_match is not None else None

    dia_source = {
        'diaSourceId':    dia_source_id,
        'visit':          expid,
        'detector':       sca_num,
        'diaObjectId':    resolved_object_id,
        'ssObjectId':     None,
        'parentDiaSourceId': None,
        'midpointMjdTai': mjd,
        'ra':             ra_f,
        'dec':            dec_f,
        'raErr':          ra_err,
        'decErr':         dec_err,
        'x':              float(xpos) if xpos is not None else 0.0,
        'y':              float(ypos) if ypos is not None else 0.0,
        'xErr':           None,
        'yErr':           None,
        'band':           band,
        'psfFlux':        float(psf_flux) if psf_flux is not None else None,
        'psfFluxErr':     float(psf_flux_err) if psf_flux_err is not None else None,
        'snr':            float(snr) if snr is not None else None,
        'extendedness':   safe_float(row.get('CLASS_STAR')),
        'reliability':    None,
        'flags':          flags,
        'apFlux':         float(ap_flux) if ap_flux is not None else None,
        'apFluxErr':      None,
        'trailFlux': None, 'trailFluxErr': None,
        'trailLength': None, 'trailAngle': None,
        'scienceFlux': None, 'scienceFluxErr': None,
        'templateFlux': None, 'templateFluxErr': None,
        'dipoleMeanFlux': None, 'dipoleFluxErr': None,
        'dipoleLength': None, 'dipoleAngle': None,
        'ixx':    safe_float(row.get('X2_IMAGE'), scale=0.0121),
        'iyy':    safe_float(row.get('Y2_IMAGE'), scale=0.0121),
        'ixy':    safe_float(row.get('XY_IMAGE'), scale=0.0121),
        'ixxErr': safe_float(row.get('ERRX2_IMAGE'), scale=0.0121),
        'iyyErr': safe_float(row.get('ERRY2_IMAGE'), scale=0.0121),
        'ixyErr': safe_float(row.get('ERRXY_IMAGE'), scale=0.0121),
        'pixelFlags_saturated': None,
        'pixelFlags_bad':       bool(flags & 0x01) if flags else None,
        'pixelFlags_edge':      bool(flags & 0x04) if flags else None,
        'pixelFlags_cr':        None,
        'timeProcessedMjdTai':  None,
        'timeWithdrawnMjdTai':  None,
        'sca':        sca_num,
        'field':      field_id,
        'hp6':        hp6,
        'hp9':        hp9,
        'pid':        0,
        'expid':      expid,
        'isdiffpos':  True,
        'qfit':       qfit,
        'cfit':       cfit,
        'redchi':     redchi,
        'npixfit':    npixfit,
        'sharpness':  sharpness,
        'roundness1': roundness1,
        'roundness2': roundness2,
        'peak':       peak,
    }

    return {
        'diaSourceId':        dia_source_id,
        'observation_reason': None,
        'target_name':        None,
        'diaSource':          dia_source,
        'prvDiaSources':      prv_dia_sources,
        'prvDiaForcedSources': None,
        'diaObject':          dia_object,
        'ssSource':           None,
        'mpc_orbits':         None,
        'cutoutDifference':   extract_stamp(diff_data, xpos, ypos),
        'cutoutScience':      extract_stamp(sci_data,  xpos, ypos),
        'cutoutTemplate':     extract_stamp(ref_data,  xpos, ypos),
    }

# ---------------------------------------------------------------------------
# Per-job processing
# ---------------------------------------------------------------------------

def process_job(jid, schema, include_truth=False):
    job_dir  = os.path.join(DATA_DIR, jid)

    # Try: (1) saved JSON header in provenance, (2) full FITS, (3) partial header in /tmp
    json_hdr = os.path.join(PROVENANCE_DIR, 'headers', f'{jid}_header.json')
    fits_path = os.path.join(job_dir, 'diffimage_masked.fits')
    hdr_path_tmp = os.path.join('/tmp', f'hdr_{jid}.fits')

    if os.path.exists(json_hdr):
        with open(json_hdr) as f:
            hdr = {k: str(v) for k, v in json.load(f).items()}
    elif os.path.exists(fits_path):
        hdr = parse_fits_header(fits_path)
    elif os.path.exists(hdr_path_tmp):
        hdr = parse_fits_header(hdr_path_tmp)
    else:
        print(f'  SKIP {jid}: no FITS or header file found')
        return [], []

    sex_path = os.path.join(job_dir, 'diffimage_masked.txt')
    if not os.path.exists(sex_path):
        print(f'  SKIP {jid}: no SExtractor catalog')
        return [], []
    rows = parse_sextractor(sex_path)

    # Load PSF-fit catalog
    psf_df, psf_tree = load_psf_catalog(job_dir)
    print(f'  {jid}: PSF catalog {"loaded" if psf_df is not None else "unavailable"}'
          + (f' ({len(psf_df)} sources)' if psf_df is not None else ''))

    # Truth catalog
    if include_truth:
        truth_files = [f for f in os.listdir(job_dir) if f.startswith('Roman_TDS_index_')]
        truth_sources = parse_truth(os.path.join(job_dir, truth_files[0])) if truth_files else []
        n_transients = sum(1 for s in truth_sources if s['obj_type'] == 'transient')
        print(f'  {jid}: truth catalog {len(truth_sources)} sources ({n_transients} transients)'
              if truth_sources else f'  {jid}: no truth catalog')
    else:
        truth_sources = []

    # FITS images for cutouts
    sci_data  = load_image(os.path.join(job_dir, 'bkg_subbed_science_image.fits'))
    ref_data  = load_image(os.path.join(job_dir, 'awaicgen_output_mosaic_image_resampled_gainmatched.fits'))
    diff_data = load_image(os.path.join(job_dir, 'diffimage_masked.fits'))
    n_images = sum(x is not None for x in [sci_data, ref_data, diff_data])
    print(f'  {jid}: loaded {n_images}/3 FITS images for stamps')

    # LC tile
    crval1 = hdr_float(hdr, 'CRVAL1')
    crval2 = hdr_float(hdr, 'CRVAL2')
    lc_df, lc_tree = load_lc_tile(crval1, crval2)
    print(f'  {jid}: lc tile {"loaded" if lc_df is not None else "unavailable"}'
          + (f' ({len(lc_df):,} objects)' if lc_df is not None else ''))

    alerts = []
    truth_rows = []
    skipped = 0

    for row in rows:
        try:
            xpos = row.get('XWIN_IMAGE')
            ypos = row.get('YWIN_IMAGE')

            psf_match = match_psf(xpos, ypos, psf_df, psf_tree)
            truth_match = match_truth(xpos or 0.0, ypos or 0.0, truth_sources) if truth_sources else None

            alert = build_alert(row, hdr, psf_row=psf_match,
                                sci_data=sci_data, ref_data=ref_data,
                                diff_data=diff_data, truth_match=truth_match,
                                lc_df=lc_df, lc_tree=lc_tree)
            alerts.append(alert)

            if include_truth:
                expid = hdr_int(hdr, 'EXPID', 0)
                number = int(row.get('NUMBER', 0) or 0)
                dia_source_id = expid * 1_000_000 + number
                truth_rows.append({
                    'diaSourceId':    dia_source_id,
                    'obj_type':       truth_match['obj_type'] if truth_match else None,
                    'truth_ra':       truth_match['ra'] if truth_match else None,
                    'truth_dec':      truth_match['dec'] if truth_match else None,
                    'truth_mag':      truth_match['mag'] if truth_match else None,
                    'truth_object_id': truth_match['object_id'] if truth_match else None,
                })

        except Exception as e:
            skipped += 1
            if skipped <= 3:
                print(f'  WARN row {row.get("NUMBER")}: {e}')

    # Write alerts
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f'{jid}_alerts.avro')
    with open(out_path, 'wb') as f:
        fastavro.writer(f, schema, alerts)

    n_lc_match = sum(1 for a in alerts if a['diaObject'] is not None)
    n_prv      = sum(len(a['prvDiaSources']) for a in alerts if a['prvDiaSources'])
    n_truth    = sum(1 for t in truth_rows if t['obj_type'] is not None)
    truth_str  = f'{n_truth} truth-matched, ' if include_truth else ''
    print(f'  {jid}: {len(alerts)} alerts, {truth_str}'
          f'{n_lc_match} lc matches (avg {n_prv//max(n_lc_match,1)} prvDiaSources)'
          + (f', {skipped} skipped' if skipped else '')
          + f'  ->  {os.path.basename(out_path)}')

    return alerts, truth_rows

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description='Generate RAPID alert packets (rapid.v01_00 schema)')
    p.add_argument('--data-dir', default=DEFAULT_DATA_DIR,
                   help='Directory containing jid*/ job folders '
                        '(default: %(default)s)')
    p.add_argument('--schema-dir', default=DEFAULT_SCHEMA_DIR,
                   help='Directory containing .avsc schema files '
                        '(default: %(default)s)')
    p.add_argument('--output-dir', default=DEFAULT_OUTPUT_DIR,
                   help='Output directory for .avro alert files '
                        '(default: %(default)s)')
    p.add_argument('--sidecar-dir', default=DEFAULT_SIDECAR_DIR,
                   help='Output directory for truth sidecar '
                        '(default: %(default)s)')
    p.add_argument('--lc-tile-dir', default=DEFAULT_LC_TILE_DIR,
                   help='Cache directory for light curve HEALPix tiles '
                        '(default: %(default)s)')
    p.add_argument('--provenance-dir', default=DEFAULT_PROVENANCE_DIR,
                   help='Directory with saved FITS headers (JSON), truth and '
                        'inject catalogs (default: %(default)s)')
    p.add_argument('--jobs', nargs='*', default=None,
                   help='Process only these job IDs (default: all jid* dirs)')
    p.add_argument('--include-truth', action='store_true',
                   help='Enable truth catalog matching and sidecar generation '
                        '(requires Roman_TDS_index_*.txt in job dirs)')
    return p.parse_args()


if __name__ == '__main__':
    args = parse_args()

    # Set module-level path variables from CLI args
    SCHEMA_DIR = args.schema_dir
    DATA_DIR = args.data_dir
    OUTPUT_DIR = args.output_dir
    SIDECAR_DIR = args.sidecar_dir
    LC_TILE_DIR = args.lc_tile_dir
    PROVENANCE_DIR = args.provenance_dir

    print('Loading schema...')
    schema = load_schema()

    if args.jobs:
        jobs = args.jobs
    else:
        jobs = sorted(d for d in os.listdir(DATA_DIR) if d.startswith('jid'))
    print(f'Found {len(jobs)} jobs: {", ".join(jobs)}\n')

    all_truth_rows = []
    total = 0
    for jid in jobs:
        alerts, truth_rows = process_job(jid, schema, include_truth=args.include_truth)
        total += len(alerts)
        all_truth_rows.extend(truth_rows)

    # Write truth sidecar
    if not args.include_truth:
        print(f'\nTruth sidecar: skipped (use --include-truth to enable)')
    elif all_truth_rows:
        os.makedirs(SIDECAR_DIR, exist_ok=True)
        truth_df = pd.DataFrame(all_truth_rows)
        sidecar_path = os.path.join(SIDECAR_DIR, 'truth_labels.parquet')
        truth_df.to_parquet(sidecar_path, index=False)
        n_matched = truth_df['obj_type'].notna().sum()
        print(f'\nTruth sidecar: {len(truth_df)} rows ({n_matched} matched) -> {sidecar_path}')
    else:
        print('\nWARN: no truth rows generated')
    print(f'Total: {total} alerts written to {OUTPUT_DIR}')
