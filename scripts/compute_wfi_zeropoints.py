"""Roman WFI bandpass moments, flux conversions and AB zero points, per SCA and filter.

Each SCA's effective-area curve comes from the Roman Technical Information
Repository's ``Roman_effarea_v8_SCA##_<date>.ecsv`` tables (one file per SCA,
columns ``Wave`` [um] and one per filter [m^2]); its gain and pixel area come
from the gain and area references CRDS recommends for that detector and date.
The CRDS photom reference's ``photmjsr`` is printed beside the computed K as a
check.

Usage: python compute_wfi_zeropoints.py [--effarea-dir DIR] [--sca SCA01 ...]
       [--date 2026-10-29T00:00:00] [--ipc-factor 1.10] [--cache-dir DIR]
"""

import argparse
import datetime
import glob
import json
import os
import re
import urllib.request
import warnings

import asdf
import astropy.units as u
import numpy as np
from astropy import constants as const
from astropy.io import ascii
from astropy.io.ascii import InconsistentTableError

# --- Configuration ---
# One folder holds both the Roman_effarea_v8_SCA##_*.ecsv tables and the
# downloaded CRDS gain, area and photom references
DATA_DIR = os.path.expanduser("~/Folks/rapid/Roman_Technical_Information_Repository")
EFFAREA_DIR = DATA_DIR
CACHE_DIR = DATA_DIR
CRDS_SERVER = "https://roman-crds.stsci.edu"

# The gain reference holds the photon-transfer gain, which IPC biases high.
# CRDS photom (context roman_0078) implies gain = median(gain reference) / 1.100
# on WFI01 and WFI10, the same in every filter; this script divides by it too.
IPC_FACTOR = 1.10

REFPIX = 4  # Reference-pixel border of the 4096 x 4096 gain array, excluded from its median

# Photometric constants
H_PLANCK = const.h.to_value(u.J * u.s)  # 6.62607015e-34 J*s
JY_TO_SI = 1e-26                        # 1 Jy = 1e-26 W / m^2 / Hz
AB_OFFSET = 8.90                        # -2.5 log10(3631 Jy)

# Sanity bounds on unitless columns: wavelengths in um, areas in m^2 (the
# telescope's 2.4 m aperture collects at most pi * 1.2^2 = 4.52 m^2)
WAVE_RANGE_UM = (0.1, 10.0)
MAX_AREA_M2 = 4.6

FILTERS = ["F062", "F087", "F106", "F129", "F146", "F158", "F184", "F213"]
SCAS = [f"SCA{i:02d}" for i in range(1, 19)]


def find_effarea_file(effarea_dir, sca):
    """Return the one effective-area file in ``effarea_dir`` whose name carries ``sca``.

    Parameters
    ----------
    effarea_dir : str
        Directory holding the ``.ecsv`` effective-area tables.
    sca : str
        SCA name, ``SCA01`` to ``SCA18``.

    Returns
    -------
    str
        Path of the matching file.

    Raises
    ------
    FileNotFoundError
        If no file, or more than one, names this SCA.
    """
    token = re.compile(rf"(?<![0-9A-Za-z]){sca}(?![0-9])", re.IGNORECASE)
    files = sorted(glob.glob(os.path.join(effarea_dir, "*.ecsv")))
    matches = [f for f in files if token.search(os.path.basename(f))]
    if len(matches) != 1:
        raise FileNotFoundError(f"expected one .ecsv file naming {sca} in {effarea_dir}, found {matches}")
    return matches[0]


def load_effective_area(filepath, filt):
    """Read one filter's effective-area curve from a per-SCA ``.ecsv`` table, cleaned and sorted.

    Columns with units are converted to um and m^2; unitless columns are taken
    as um and m^2 (the tables' header says so) and checked against plausible
    ranges, so a table in other units fails rather than giving wrong zero points.

    Parameters
    ----------
    filepath : str
        Path of the per-SCA table (columns ``Wave`` and one per filter).
    filt : str
        Filter column name, e.g. ``F158``.

    Returns
    -------
    wave_um : numpy.ndarray
        Wavelengths [um], ascending, finite.
    a_eff_m2 : numpy.ndarray
        Effective area [m^2] at ``wave_um``, finite and non-negative.

    Raises
    ------
    ValueError
        If the wavelength or filter column is missing or ambiguous, the values
        are out of range for um or m^2, or fewer than two usable points remain.
    """
    try:
        tbl = ascii.read(filepath, format="ecsv")
    except InconsistentTableError:
        # The Roman tables' free-text header line ("Effective area for Roman
        # (m^2) - ...") is not YAML, so astropy refuses them as ECSV; their
        # body is plain CSV.
        tbl = ascii.read(filepath, format="csv", comment=r"\s*#")

    wave_cols = [c for c in tbl.colnames if c.lower().startswith("wave")]
    if len(wave_cols) != 1:
        raise ValueError(f"{filepath}: expected one wavelength column, found {wave_cols}")
    if filt not in tbl.colnames:
        raise ValueError(f"{filepath}: no {filt} column (columns: {tbl.colnames})")

    wave_col, area_col = tbl[wave_cols[0]], tbl[filt]
    wave = wave_col.quantity.to_value(u.um) if wave_col.unit is not None else np.array(wave_col, dtype=float)
    area = area_col.quantity.to_value(u.m**2) if area_col.unit is not None else np.array(area_col, dtype=float)

    valid = np.isfinite(wave) & np.isfinite(area) & (area >= 0)
    wave, area = wave[valid], area[valid]
    idx = np.argsort(wave)
    wave, area = wave[idx], area[idx]

    if wave.size < 2 or not np.any(area > 0):
        raise ValueError(f"{filepath}: {filt} has no usable effective-area points")
    if wave[0] < WAVE_RANGE_UM[0] or wave[-1] > WAVE_RANGE_UM[1]:
        raise ValueError(f"{filepath}: wavelengths {wave[0]:g} to {wave[-1]:g} are not in um")
    if area.max() > MAX_AREA_M2:
        raise ValueError(f"{filepath}: {filt} peak area {area.max():g} is not in m^2")
    return wave, area


def crds_call(server, method, params):
    """Call one CRDS JSON-RPC method and return its result.

    Parameters
    ----------
    server : str
        CRDS server URL.
    method : str
        JSON-RPC method name, e.g. ``get_best_references``.
    params : list
        The method's positional parameters.

    Returns
    -------
    object
        The call's ``result``.

    Raises
    ------
    RuntimeError
        If the server returns an error.
    """
    body = json.dumps({"jsonrpc": "1.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(server.rstrip("/") + "/json/", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        reply = json.load(resp)
    if reply.get("error"):
        raise RuntimeError(f"CRDS {method}: {reply['error']}")
    return reply["result"]


def fetch_reference(server, name, cache_dir):
    """Return the local path of a CRDS reference file, downloading it once into ``cache_dir``.

    Parameters
    ----------
    server : str
        CRDS server URL.
    name : str
        Reference file name, e.g. ``roman_wfi_gain_0044.asdf``.
    cache_dir : str
        Directory the file is kept in.

    Returns
    -------
    str
        Path of the local copy.
    """
    path = os.path.join(cache_dir, name)
    if not os.path.exists(path):
        os.makedirs(cache_dir, exist_ok=True)
        url = server.rstrip("/") + "/unchecked_get/references/roman/" + name
        print(f"Fetching {url}", flush=True)
        urllib.request.urlretrieve(url, path + ".part")
        os.replace(path + ".part", path)
    return path


def open_reference(path):
    """Open a Roman ASDF reference and return its ``roman`` tree, quieting version warnings.

    Parameters
    ----------
    path : str
        Path of the ``.asdf`` reference.

    Returns
    -------
    tuple
        ``(asdf.AsdfFile, dict)``: the open file (close it when done) and its ``roman`` node.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", asdf.exceptions.AsdfWarning)
        af = asdf.open(path, lazy_load=True)
    return af, af["roman"]


def sca_calibration(sca, date, context, server, cache_dir, ipc_factor):
    """Return one SCA's gain, pixel area and photom ``photmjsr`` values from its CRDS references.

    Parameters
    ----------
    sca : str
        SCA name, ``SCA01`` to ``SCA18`` (CRDS detector ``WFI01`` to ``WFI18``).
    date : str
        Exposure start time the references are selected for, ``YYYY-MM-DDTHH:MM:SS``.
    context : str
        CRDS context, e.g. ``roman_0078.pmap``.
    server : str
        CRDS server URL.
    cache_dir : str
        Directory the reference files are kept in.
    ipc_factor : float
        Divisor applied to the gain reference's median, its IPC correction.

    Returns
    -------
    dict
        ``gain`` [e-/DN], the median over the gain reference's science pixels
        divided by ``ipc_factor``; ``omega_pix`` [sr], the area reference's
        nominal pixel area; ``photmjsr``, per filter, the photom reference's
        [(MJy/sr)/(DN/s)]; and ``refs``, the reference names used.
    """
    detector = "WFI" + sca[3:]
    header = {"ROMAN.META.INSTRUMENT.NAME": "WFI",
              "ROMAN.META.INSTRUMENT.DETECTOR": detector,
              "ROMAN.META.EXPOSURE.START_TIME": date}
    refs = crds_call(server, "get_best_references", [context, header, ["gain", "area", "photom"]])
    for kind, name in refs.items():
        if not isinstance(name, str) or not name.endswith(".asdf"):
            raise RuntimeError(f"CRDS has no {kind} reference for {detector} at {date}: {name}")

    af, tree = open_reference(fetch_reference(server, refs["gain"], cache_dir))
    with af:
        gain = np.asarray(tree["data"], dtype=float)[REFPIX:-REFPIX, REFPIX:-REFPIX]
        gain = float(np.median(gain[np.isfinite(gain) & (gain > 0)]))

    af, tree = open_reference(fetch_reference(server, refs["area"], cache_dir))
    with af:
        omega_pix = float(tree["meta"]["photometry"]["pixelarea_steradians"])

    af, tree = open_reference(fetch_reference(server, refs["photom"], cache_dir))
    with af:
        photmjsr = {f: tree["phot_table"][f]["photmjsr"] for f in FILTERS if f in tree["phot_table"]}

    return {"gain": gain / ipc_factor, "omega_pix": omega_pix,
            "photmjsr": {f: float(v) for f, v in photmjsr.items() if v is not None}, "refs": refs}


def compute_filter_metrics(wave_um, a_eff_m2, gain, omega_pix):
    """Integrate an effective-area curve into bandpass moments, flux conversions and the AB zero point.

    Parameters
    ----------
    wave_um : numpy.ndarray
        Wavelengths [um], ascending.
    a_eff_m2 : numpy.ndarray
        Effective area [m^2] (electrons per photon included) at ``wave_um``.
    gain : float
        Gain [e-/DN].
    omega_pix : float
        Pixel solid angle [sr].

    Returns
    -------
    dict
        ``lambda_0`` mean wavelength [um], ``lambda_piv`` pivot wavelength
        [um], ``ew`` equivalent width [um], ``a_peak`` peak area [m^2], ``C``
        [Jy/(DN/s)], ``K`` [(MJy/sr)/(DN/s)] and ``ZP_AB``, the AB magnitude
        of 1 DN/s.
    """
    # Peak effective area (m^2)
    a_peak = np.max(a_eff_m2)

    # Equivalent Width (um): integral(A_eff d_lambda) / A_peak
    int_a = np.trapezoid(a_eff_m2, wave_um)
    ew = int_a / a_peak

    # Mean wavelength (um): integral(A_eff * lambda d_lambda) / integral(A_eff d_lambda)
    int_a_lam = np.trapezoid(a_eff_m2 * wave_um, wave_um)
    lambda_0 = int_a_lam / int_a

    # Pivot wavelength (um): sqrt( integral(A_eff * lambda d_lambda) / integral(A_eff / lambda d_lambda) )
    int_a_over_lam = np.trapezoid(a_eff_m2 / wave_um, wave_um)
    lambda_piv = np.sqrt(int_a_lam / int_a_over_lam)

    # Count rate (DN/s) for a 1 Jy source:
    # Note: d_lambda / lambda is dimensionless, so wave_um units cancel directly
    r_1jy = (JY_TO_SI / (H_PLANCK * gain)) * int_a_over_lam

    # C: Jy / (DN/s)
    c_val = 1.0 / r_1jy

    # K (photmjsr): (MJy/sr) / (DN/s)
    k_val = c_val / (1e6 * omega_pix)

    # AB Zero Point: mag corresponding to 1 DN/s
    zp_ab = -2.5 * np.log10(c_val) + AB_OFFSET

    return {
        "lambda_0": lambda_0,
        "lambda_piv": lambda_piv,
        "ew": ew,
        "a_peak": a_peak,
        "C": c_val,
        "K": k_val,
        "ZP_AB": zp_ab,
    }


def parse_args(argv=None):
    """Parse the command line.

    Parameters
    ----------
    argv : list of str, optional
        Arguments; ``sys.argv[1:]`` when None.

    Returns
    -------
    argparse.Namespace
        The parsed options.
    """
    # Wide enough that the default data folder prints on one line
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=lambda prog: argparse.HelpFormatter(prog, width=120))
    p.add_argument("--effarea-dir", default=EFFAREA_DIR, help="directory of Roman_effarea_v8_SCA##_*.ecsv (default: %(default)s)")
    p.add_argument("--sca", nargs="+", default=SCAS, choices=SCAS, metavar="SCA",
                   help="SCAs to compute, SCA01 to SCA18 (default: all)")
    p.add_argument("--date", default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                   help="exposure start time the CRDS references are selected for (default: now, UTC)")
    p.add_argument("--context", help="CRDS context, e.g. roman_0078.pmap (default: the server's operational one)")
    p.add_argument("--crds-server", default=CRDS_SERVER, help="CRDS server URL (default: %(default)s)")
    p.add_argument("--cache-dir", default=CACHE_DIR, help="where CRDS references are downloaded once (default: %(default)s)")
    p.add_argument("--ipc-factor", type=float, default=IPC_FACTOR,
                   help="divisor applied to the gain reference's median (default: %(default)s)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    context = args.context or crds_call(args.crds_server, "get_default_context", ["roman", "operational"])

    header = (
        f"{'SCA':<5} | {'Filter':<6} | {'lam_0 (um)':>10} | {'lam_piv (um)':>12} | "
        f"{'EW (um)':>8} | {'A_peak (m2)':>11} | {'Gain (e-/DN)':>12} | {'Pix (arcsec)':>12} | "
        f"{'C [Jy/(DN/s)]':>14} | {'K [(MJy/sr)/(DN/s)]':>19} | {'K/photmjsr':>10} | {'ZP_AB (mag)':>11}"
    )
    lines = []
    zps = {f: [] for f in FILTERS}
    for sca in args.sca:
        filepath = find_effarea_file(args.effarea_dir, sca)
        cal = sca_calibration(sca, args.date, context, args.crds_server, args.cache_dir, args.ipc_factor)
        pix_arcsec = np.sqrt(cal["omega_pix"]) * u.rad.to(u.arcsec)
        lines.append(f"# {sca}: {os.path.basename(filepath)}, "
                     + ", ".join(f"{k} {v}" for k, v in sorted(cal["refs"].items())))
        for filt in FILTERS:
            wave, area = load_effective_area(filepath, filt)
            m = compute_filter_metrics(wave, area, cal["gain"], cal["omega_pix"])
            photmjsr = cal["photmjsr"].get(filt)
            check = f"{m['K'] / photmjsr:>10.4f}" if photmjsr else f"{'n/a':>10}"
            zps[filt].append(m["ZP_AB"])
            lines.append(
                f"{sca:<5} | {filt:<6} | {m['lambda_0']:>10.4f} | {m['lambda_piv']:>12.4f} | "
                f"{m['ew']:>8.4f} | {m['a_peak']:>11.3f} | {cal['gain']:>12.4f} | {pix_arcsec:>12.5f} | "
                f"{m['C']:>14.4e} | {m['K']:>19.4f} | {check} | {m['ZP_AB']:>11.3f}"
            )

    print(f"\nCRDS context {context}, references for {args.date}; "
          f"gain = median(gain reference) / {args.ipc_factor:g}\n")
    print(header)
    print("-" * len(header))
    print("\n".join(lines))

    if len(args.sca) > 1:
        print(f"\nZP_AB over {len(args.sca)} SCAs:")
        print(f"{'Filter':<6} | {'median':>7} | {'min':>7} | {'max':>7}")
        for filt in FILTERS:
            z = np.array(zps[filt])
            print(f"{filt:<6} | {np.median(z):>7.3f} | {z.min():>7.3f} | {z.max():>7.3f}")


if __name__ == "__main__":
    main()
