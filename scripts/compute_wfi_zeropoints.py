"""Roman WFI bandpass moments, flux conversions and AB zero points, per SCA and filter.

Each SCA's effective-area curve comes from the Roman Technical Information
Repository's ``Roman_effarea_v8_SCA##_<date>.ecsv`` tables (one file per SCA,
columns ``Wave`` [um] and one per filter [m^2]); its gain and pixel area come
from the gain and area references CRDS recommends for that detector and date.
The CRDS photom reference's ``photmjsr`` is printed beside the computed K as a
check.

Before computing, the data folder is checked: each table against GitHub's
listing (git blob SHA, and any other table GitHub has for that SCA), and each
recommended CRDS reference for presence and CRDS's size. If anything is
missing or out of date the script says what and asks whether to continue
(``--yes`` continues without asking; with no terminal it stops).

Usage: python compute_wfi_zeropoints.py [--effarea-dir DIR] [--sca SCA01 ...]
       [--date 2026-10-29T00:00:00] [--ipc-factor 1.10] [--cache-dir DIR] [--yes]
"""

import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import sys
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
# GitHub's listing of the effective-area tables, each with its git blob SHA
EFFAREA_LISTING_URL = ("https://api.github.com/repos/RomanSpaceTelescope/roman-technical-information/contents/"
                       "roman_technical_information/data/WideFieldInstrument/Imaging/EffectiveAreas?ref=main")

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


def sca_pattern(sca):
    """Return a regular expression matching ``sca`` as a whole token in a file name.

    Parameters
    ----------
    sca : str
        SCA name, ``SCA01`` to ``SCA18``.

    Returns
    -------
    re.Pattern
        Case-insensitive pattern that matches ``SCA01`` but not ``SCA010``.
    """
    return re.compile(rf"(?<![0-9A-Za-z]){sca}(?![0-9])", re.IGNORECASE)


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
    token = sca_pattern(sca)
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


def fetch_reference(server, name, cache_dir, size=None):
    """Return the local path of a CRDS reference file, downloading it into ``cache_dir`` when needed.

    Parameters
    ----------
    server : str
        CRDS server URL.
    name : str
        Reference file name, e.g. ``roman_wfi_gain_0044.asdf``.
    cache_dir : str
        Directory the file is kept in.
    size : int, optional
        CRDS's size of the file [bytes]; a local copy of another size is
        downloaded again.

    Returns
    -------
    str
        Path of the local copy.
    """
    path = os.path.join(cache_dir, name)
    if not os.path.exists(path) or (size is not None and os.path.getsize(path) != size):
        os.makedirs(cache_dir, exist_ok=True)
        url = server.rstrip("/") + "/unchecked_get/references/roman/" + name
        print(f"Fetching {url}", file=sys.stderr, flush=True)
        urllib.request.urlretrieve(url, path + ".part")
        os.replace(path + ".part", path)
    return path


def best_references(sca, date, context, server):
    """Return the gain, area and photom reference names CRDS recommends for one SCA and date.

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

    Returns
    -------
    dict
        Reference file name per kind: ``gain``, ``area`` and ``photom``.

    Raises
    ------
    RuntimeError
        If CRDS has no reference of a kind for this detector and date.
    """
    detector = "WFI" + sca[3:]
    header = {"ROMAN.META.INSTRUMENT.NAME": "WFI",
              "ROMAN.META.INSTRUMENT.DETECTOR": detector,
              "ROMAN.META.EXPOSURE.START_TIME": date}
    refs = crds_call(server, "get_best_references", [context, header, ["gain", "area", "photom"]])
    for kind, name in refs.items():
        if not isinstance(name, str) or not name.endswith(".asdf"):
            raise RuntimeError(f"CRDS has no {kind} reference for {detector} at {date}: {name}")
    return refs


def git_blob_sha(path):
    """Return a file's git blob SHA-1, the checksum GitHub lists for it.

    Parameters
    ----------
    path : str
        Path of the file.

    Returns
    -------
    str
        Hex SHA-1 of ``b"blob <size>\\0"`` followed by the file's bytes.
    """
    with open(path, "rb") as f:
        data = f.read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def check_effarea(effarea_dir, scas, listing_url=EFFAREA_LISTING_URL):
    """Compare the local effective-area tables with the Roman Technical Information Repository's.

    Parameters
    ----------
    effarea_dir : str
        Directory holding the local ``.ecsv`` tables.
    scas : list of str
        SCAs whose tables are checked.
    listing_url : str, optional
        GitHub contents-API URL of the repository's effective-area folder.

    Returns
    -------
    list of str
        One line per problem: a table missing, differing from GitHub's, absent
        from GitHub, or alongside another table GitHub has for that SCA (a
        newer version); a line saying the check could not run when GitHub
        cannot be reached. Empty when every table is GitHub's current one.
    """
    try:
        req = urllib.request.Request(listing_url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            listing = json.load(resp)
        remote = {x["name"]: x["sha"] for x in listing if x["name"].endswith(".ecsv")}
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [f"effective-area tables not checked: GitHub listing failed ({e})"]

    local_files = sorted(glob.glob(os.path.join(effarea_dir, "*.ecsv")))
    problems = []
    for sca in scas:
        token = sca_pattern(sca)
        names = sorted(n for n in remote if token.search(n))
        local = [f for f in local_files if token.search(os.path.basename(f))]
        if not local:
            problems.append(f"{sca}: no local table; GitHub has {', '.join(names) or 'none'}")
            continue
        if len(local) > 1:
            problems.append(f"{sca}: {len(local)} local tables ({', '.join(map(os.path.basename, local))})")
            continue
        name = os.path.basename(local[0])
        if name not in remote:
            problems.append(f"{sca}: {name} is not on GitHub, which has {', '.join(names) or 'none'}")
        elif git_blob_sha(local[0]) != remote[name]:
            problems.append(f"{sca}: {name} differs from GitHub's copy")
        newer = [n for n in names if n != name]
        if newer:
            problems.append(f"{sca}: GitHub also has {', '.join(newer)}")
    return problems


def check_references(cache_dir, refs, server, all_names=None):
    """Compare the local CRDS references with the ones CRDS recommends.

    CRDS reference names are never reused for new content, so a recommended
    name present at CRDS's size is up to date.

    Parameters
    ----------
    cache_dir : str
        Directory the reference files are kept in.
    refs : dict
        Recommended reference names per kind, per SCA: ``{sca: {kind: name}}``.
    server : str
        CRDS server URL.
    all_names : set of str, optional
        The references recommended for all 18 SCAs; a local file outside it
        is reported unused. Defaults to the names in ``refs``.

    Returns
    -------
    problems : list of str
        One line for the recommended references not yet downloaded, with
        their total size, and one per local copy whose size is not CRDS's.
    unused : list of str
        Local ``roman_wfi_{gain,area,photom}_*.asdf`` files CRDS does not
        recommend for any SCA at this date and context.
    sizes : dict
        CRDS's size [bytes] of each recommended reference.
    """
    names = sorted({n for r in refs.values() for n in r.values()})
    info = crds_call(server, "get_file_info_map", ["roman", names, ["size"]])
    sizes = {n: int(info[n]["size"]) for n in names}

    problems = []
    missing = [n for n in names if not os.path.exists(os.path.join(cache_dir, n))]
    if missing:
        gbytes = sum(sizes[n] for n in missing) / 1e9
        problems.append(f"{len(missing)} recommended CRDS reference{'s are' if len(missing) > 1 else ' is'} "
                        f"not in {cache_dir} ({gbytes:.2f} GB to download): {', '.join(missing)}")
    for n in names:
        path = os.path.join(cache_dir, n)
        if n not in missing and os.path.getsize(path) != sizes[n]:
            problems.append(f"{n}: local size {os.path.getsize(path)} bytes, CRDS's {sizes[n]}; "
                            "it will be downloaded again")

    local = [os.path.basename(f) for kind in ("gain", "area", "photom")
             for f in glob.glob(os.path.join(cache_dir, f"roman_wfi_{kind}_*.asdf"))]
    unused = sorted(set(local) - set(all_names or names))
    return problems, unused, sizes


def ask_to_continue(problems, assume_yes):
    """Report what is out of date in the data folder and ask whether to continue.

    Parameters
    ----------
    problems : list of str
        Lines describing what is missing or out of date.
    assume_yes : bool
        Continue without asking (``--yes``).

    Returns
    -------
    bool
        True to continue. False when the answer is not yes, or when there is
        no terminal to ask and ``assume_yes`` is False.
    """
    print("The data folder is not up to date:", file=sys.stderr)
    for line in problems:
        print(f"  - {line}", file=sys.stderr)
    if assume_yes:
        print("Continuing (--yes).", file=sys.stderr)
        return True
    if not sys.stdin.isatty():
        print("No terminal to ask; rerun with --yes to continue anyway.", file=sys.stderr)
        return False
    print("Continue? [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = input()
    except EOFError:  # input closed: no answer is no
        print(file=sys.stderr)
        return False
    return answer.strip().lower() in ("y", "yes")


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


def sca_calibration(refs, server, cache_dir, ipc_factor, sizes=None):
    """Return one SCA's gain, pixel area and photom ``photmjsr`` values from its CRDS references.

    Parameters
    ----------
    refs : dict
        The SCA's reference names per kind, from :func:`best_references`.
    server : str
        CRDS server URL.
    cache_dir : str
        Directory the reference files are kept in.
    ipc_factor : float
        Divisor applied to the gain reference's median, its IPC correction.
    sizes : dict, optional
        CRDS's size [bytes] per reference name; a local copy of another size
        is downloaded again.

    Returns
    -------
    dict
        ``gain`` [e-/DN], the median over the gain reference's science pixels
        divided by ``ipc_factor``; ``omega_pix`` [sr], the area reference's
        nominal pixel area; ``photmjsr``, per filter, the photom reference's
        [(MJy/sr)/(DN/s)]; and ``refs``, the reference names used.
    """
    sizes = sizes or {}

    def local(kind):
        return fetch_reference(server, refs[kind], cache_dir, sizes.get(refs[kind]))

    af, tree = open_reference(local("gain"))
    with af:
        gain = np.asarray(tree["data"], dtype=float)[REFPIX:-REFPIX, REFPIX:-REFPIX]
        gain = float(np.median(gain[np.isfinite(gain) & (gain > 0)]))

    af, tree = open_reference(local("area"))
    with af:
        omega_pix = float(tree["meta"]["photometry"]["pixelarea_steradians"])

    af, tree = open_reference(local("photom"))
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
    p.add_argument("--yes", action="store_true",
                   help="continue without asking when the data folder is not up to date")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    context = args.context or crds_call(args.crds_server, "get_default_context", ["roman", "operational"])

    # Is the data folder up to date: the effective-area tables against GitHub's,
    # the CRDS references against the ones recommended for these SCAs and date
    all_refs = {sca: best_references(sca, args.date, context, args.crds_server) for sca in SCAS}
    refs = {sca: all_refs[sca] for sca in args.sca}
    all_names = {n for r in all_refs.values() for n in r.values()}
    problems = check_effarea(args.effarea_dir, args.sca)
    ref_problems, unused, sizes = check_references(args.cache_dir, refs, args.crds_server, all_names)
    problems += ref_problems
    if unused:
        print(f"Not used for {context} at {args.date}: {', '.join(unused)}", file=sys.stderr)
    if problems and not ask_to_continue(problems, args.yes):
        sys.exit("Stopped: the data folder is not up to date.")

    # Each SCA needs exactly one table: find them all before downloading anything
    try:
        tables = {sca: find_effarea_file(args.effarea_dir, sca) for sca in args.sca}
    except FileNotFoundError as e:
        sys.exit(f"Stopped: {e}")

    header = (
        f"{'SCA':<5} | {'Filter':<6} | {'lam_0 (um)':>10} | {'lam_piv (um)':>12} | "
        f"{'EW (um)':>8} | {'A_peak (m2)':>11} | {'Gain (e-/DN)':>12} | {'Pix (arcsec)':>12} | "
        f"{'C [Jy/(DN/s)]':>14} | {'K [(MJy/sr)/(DN/s)]':>19} | {'K/photmjsr':>10} | {'ZP_AB (mag)':>11}"
    )
    lines = []
    zps = {f: [] for f in FILTERS}
    for sca in args.sca:
        filepath = tables[sca]
        cal =sca_calibration(refs[sca], args.crds_server, args.cache_dir, args.ipc_factor, sizes)
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
