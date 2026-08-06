"""Canonical generator for the Roman sky tessellation at NSIDE=512.

TWO COPIES, ONE SOURCE. This file lives in rapid_systems
(``tools/tessellation/roman_tessellation.py``) and is duplicated
byte-for-byte into the pipeline repo at
``database/modules/utils/roman_tessellation.py``. rapid_systems is where
it is EDITED; the pipeline copy exists because the pipeline needs the
closed form at runtime and the two repos deploy independently — a
cross-repo import would couple their release cycles for one
self-contained module whose only dependency is numpy.

The duplication is enforced, not trusted: the pipeline repo's
``database/modules/utils/test/test_roman_tessellation.py`` compares the
two files byte-for-byte whenever both are present, and says so plainly
when only one is. Edit here, copy there.

This module is the *authority* for the tessellation: it produces all
6,291,458 rows deterministically from closed-form rules, with the 2024
hand-applied repairs encoded programmatically rather than carried as
opaque bytes. Its output is identified by content digest
(`digest.py`), and the versioned PostgreSQL installation is loaded from
it (`cloudformation/db-migrations/015-tessellation.sql` + `load.sh`).

Provenance and the derivation
-----------------------------
The operative artifact until 2026-08-06 was
`roman_tessellation_nside512.db` (SQLite, sha256 `fc554b06…`), itself
converted by Thoth in August 2024 from `romantessellation_nside512.fits`
— see `database/schema/roman_tessellation_nside512.txt` in the pipeline
repo for that conversion story, the decmin/decmax column swap, and the
two pole-tile patches.

The generation rule was recovered by inspection and proved by exhaustive
row-identical comparison against that carried copy (`certify.py
--compare-sqlite`): **all 6,291,458 rows match bit-for-bit, on every
column, with no tolerance**.

The scheme is HEALPix ring *latitudes* with doubled RA resolution — it
is NOT HEALPix proper, and the row count reflects that:

    rings      = 4*NSIDE - 1                    = 2047
    dec bins   = rings + 2 (two pole caps)      = 2049
    rows       = 24*NSIDE**2 + 2                = 6,291,458

HEALPix would give `12*NSIDE**2`; this tessellation uses `8*i` RA bins
in polar-cap ring `i` where HEALPix uses `4*i`, hence twice the tiles.

Float32 is load-bearing, not incidental
---------------------------------------
Every stage of the original FITS pipeline was float32, and the stored
SQLite doubles are the *shortest decimal representation* of those
float32 values, re-parsed as doubles. Reproducing the artifact bit-for-
bit therefore requires reproducing that rounding exactly, and requires
knowing which quantity was rounded at which step:

  * `cdec`      — float32 of the exact ring-centre declination.
  * `decmin`/`decmax` — float32 of the exact ring-*boundary* declination
    at half-integer ring index. NOT the midpoint of the already-rounded
    neighbouring centres: those two differ by one ULP on 1,595 of the
    2,047 rings, and only the boundary form reproduces the artifact.
  * `cra`/`ramin`/`ramax` — computed in float32 *arithmetic* as
    `360*k/n` and `180*(2k±1)/n`. Computing `cra ± 180/n` in double and
    rounding once does not reproduce it.
  * the two pole-tile `cdec` values — plain doubles. They were typed by
    hand as SQL literals in 2024 (`update skytiles set … cdec =
    89.9771575 …`), so no float32 rounding was ever applied to them.

`s32()` below is that "float32 then shortest-decimal" projection.

The south-pole degenerate box — already repaired upstream
--------------------------------------------------------
The 2024 conversion note records the south-pole tile arriving with
`ramin = ramax = 0.0`, a degenerate bounding box invisible to every RA
overlap predicate, and `roman_tessellation_db.py` carries a special case
that appends rtid 6291458 by hand for exactly that reason.

**That defect is not present in the carried copy.** Both `skytiles` and
`vskytiles` hold `ramin = 0.0, ramax = 360.0` there — the repair was
applied at some point after the note was written and was never recorded.
This generator emits the same correct full-RA encoding, so the canonical
output is row-identical to the carried copy on every column of all
6,291,458 rows, with NO differences at all.

The consequence for the pipeline is that the access module's south-pole
special case is dead code against this artifact, and the versioned
PostgreSQL install inherits a correct box with no fix-up needed. The
`fix_south_pole=False` switch below reproduces the *documented* degenerate
encoding, so certification can demonstrate which of the two the carried
copy actually holds rather than asserting it from the note.
"""

import math

import numpy as np

NSIDE = 512
NRINGS = 4 * NSIDE - 1              # 2047 HEALPix ring latitudes
NDECBINS = NRINGS + 2               # 2049 declination bins (rings + 2 pole caps)
NROWS = 24 * NSIDE * NSIDE + 2      # 6,291,458 sky tiles

F32 = np.float32

# Column order matches the carried SQLite `skytiles` table exactly, so a
# row tuple from `gen_rows()` compares directly against a row read from it.
COLUMNS = ("cra", "cdec", "ramin", "ramax", "decmax", "decmin", "rtid", "dbid")


def ring_z(x):
    """HEALPix z (= sin dec) at ring index `x`, counted from the north pole.

    Integer `x` gives a ring centre; half-integer gives the boundary
    between rings `x-0.5` and `x+0.5`. Valid over 0 <= x <= 4*NSIDE.
    """
    if x < NSIDE:
        return 1.0 - x * x / (3.0 * NSIDE * NSIDE)
    if x <= 3 * NSIDE:
        return 4.0 / 3.0 - 2.0 * x / (3.0 * NSIDE)
    j = 4 * NSIDE - x
    return -(1.0 - j * j / (3.0 * NSIDE * NSIDE))


def dec_at(x):
    """Declination in degrees at ring index `x` (see `ring_z`)."""
    return math.degrees(math.asin(ring_z(x)))


def s32(v):
    """Project a double through float32 and its shortest decimal form.

    This is the FITS-float32 -> Thoth text -> SQLite double path that the
    2024 conversion went through; reproducing it is what makes the output
    bit-identical to the carried copy rather than merely close.
    """
    return float(str(F32(v)))


def nrabins(i):
    """Number of RA bins in ring `i` (1..NRINGS), counted from the north."""
    if i < NSIDE:
        return 8 * i
    if i <= 3 * NSIDE:
        return 8 * NSIDE
    return 8 * (4 * NSIDE - i)


# Cap boundary and pole-tile centre, both closed-form. The 2024 note
# records `decmin=89.954315, cdec=89.9771575` as hand-applied repairs;
# they are reproduced here from the rule instead of being transcribed.
CAP_BOUNDARY = s32(dec_at(0.5))             # 89.954315
POLE_CDEC = (90.0 + CAP_BOUNDARY) / 2.0     # 89.9771575 (a plain double)


def gen_rows(fix_south_pole=True):
    """Yield every tile as a `COLUMNS`-ordered tuple, rtid ascending.

    `fix_south_pole=False` reproduces the carried copy's degenerate
    `ramin = ramax = 0.0` south-pole box; the default emits the corrected
    full-RA box. See the module docstring.
    """
    yield (0.0, POLE_CDEC, 0.0, 360.0, 90.0, CAP_BOUNDARY, 1, NDECBINS)

    rtid = 2
    for i in range(1, NRINGS + 1):
        n = nrabins(i)
        n32 = F32(n)
        cdec = s32(dec_at(i))
        decmax = s32(dec_at(i - 0.5))
        decmin = s32(dec_at(i + 0.5))
        dbid = NDECBINS - i                 # dbid runs south -> north
        for k in range(n):
            # float32 arithmetic throughout — see the module docstring.
            cra = float(str(F32(360.0) * F32(k) / n32))
            ramin = float(str(F32(180.0) * F32(2 * k - 1) / n32))
            ramax = float(str(F32(180.0) * F32(2 * k + 1) / n32))
            yield (cra, cdec, ramin, ramax, decmax, decmin, rtid, dbid)
            rtid += 1

    south_ramax = 360.0 if fix_south_pole else 0.0
    yield (0.0, -POLE_CDEC, 0.0, south_ramax, -CAP_BOUNDARY, -90.0, NROWS, 1)


def gen_decbins():
    """Yield `(dbid, cdec, nrabins)` for all 2049 declination bins."""
    yield (1, -POLE_CDEC, 1)
    for dbid in range(2, NDECBINS):
        i = NDECBINS - dbid
        yield (dbid, s32(dec_at(i)), nrabins(i))
    yield (NDECBINS, POLE_CDEC, 1)


# --- the closed-form inverse: rtid(ra, dec) with no I/O ---------------------
#
# Certification (`certify.py --regularity`) proves this equivalent to a
# lookup against the generated table over all 6,291,458 tiles, which is
# what lets the pipeline hot path drop the per-source query entirely.

# Ring boundaries, descending: _BOUND[i-1] is ring i's decmax, _BOUND[i]
# its decmin. Built once at import (2,048 entries, ~16 KB).
_BOUND = [s32(dec_at(i - 0.5)) for i in range(1, NRINGS + 2)]

# rtid of each ring's k=0 tile, precomputed (2,047 entries).
_OFFSET = [0] * (NRINGS + 1)
_acc = 2
for _i in range(1, NRINGS + 1):
    _OFFSET[_i] = _acc
    _acc += nrabins(_i)
del _acc, _i


def ring_of(dec):
    """Ring index (1..NRINGS) containing `dec`, or 0 / NRINGS+1 for the poles."""
    if dec >= _BOUND[0]:
        return 0
    if dec < _BOUND[NRINGS]:
        return NRINGS + 1
    lo, hi = 1, NRINGS
    while lo < hi:
        mid = (lo + hi) // 2
        if dec >= _BOUND[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def rtid_of(ra, dec):
    """Sky-tile id containing `(ra, dec)`. Closed form, no database access.

    Matches the stored half-open predicate `decmin <= dec < decmax` and
    `ramin <= ra < ramax`, including the first RA bin's wrap (its stored
    `ramin` is negative, so it straddles RA=0).
    """
    i = ring_of(dec)
    if i == 0:
        return 1
    if i > NRINGS:
        return NROWS
    n = nrabins(i)
    k = int(math.floor((ra % 360.0) * n / 360.0 + 0.5)) % n
    return _OFFSET[i] + k


def rtid_of_arrays(ra, dec):
    """Vectorized `rtid_of` over numpy arrays — the pipeline hot path.

    Thousands of sources per catalog resolve in one pass with no I/O and
    no per-source Python call.
    """
    ra = np.asarray(ra, dtype=np.float64)
    dec = np.asarray(dec, dtype=np.float64)

    bound = np.asarray(_BOUND, dtype=np.float64)
    # _BOUND descends, so search the reversed (ascending) array.
    # ring i satisfies _BOUND[i] <= dec < _BOUND[i-1].
    idx = np.searchsorted(bound[::-1], dec, side="right")
    i = (NRINGS + 1) - idx

    n = np.where(
        i < NSIDE, 8 * i,
        np.where(i <= 3 * NSIDE, 8 * NSIDE, 8 * (4 * NSIDE - i)),
    )
    n = np.maximum(n, 1)                      # poles: avoid a divide by zero
    k = np.mod(np.floor(np.mod(ra, 360.0) * n / 360.0 + 0.5).astype(np.int64), n)

    offset = np.asarray(_OFFSET, dtype=np.int64)
    safe_i = np.clip(i, 1, NRINGS)
    out = offset[safe_i] + k
    out = np.where(i < 1, 1, out)
    out = np.where(i > NRINGS, NROWS, out)
    return out.astype(np.int64)


def center_of(rtid):
    """`(cra, cdec)` of `rtid` — closed form, no database access."""
    if rtid == 1:
        return (0.0, POLE_CDEC)
    if rtid == NROWS:
        return (0.0, -POLE_CDEC)
    i, k = _ring_and_bin(rtid)
    n32 = F32(nrabins(i))
    return (float(str(F32(360.0) * F32(k) / n32)), s32(dec_at(i)))


def corners_of(rtid, widened=False):
    """`(ramin, ramax, decmin, decmax)` of `rtid` — closed form, no I/O.

    Returns the CANONICAL bounds: the same values the ``skytiles`` table
    and the builder's rows carry.

    A note on what the SQLite class returned, because it is not the same
    thing and the difference is real if small. That class read corners
    from the R-tree ``vskytiles``, with a comment claiming the R-tree had
    "more precision". It does not — it has *different* values. SQLite's
    R-tree stores float32 and rounds every box OUTWARD by design, so no
    contained point can be missed: ``min`` bounds round down, ``max``
    bounds round up. Measured across the carried copy, ``vskytiles``
    differs from ``skytiles`` by up to **2 float32 ULPs**, always
    outward.

    That widening is a property of SQLite's index, not of the
    tessellation, and it is not reproducible from the generation rule —
    it depends on the double the R-tree was handed at insert time. So it
    is not reproduced here. The canonical bounds are the tessellation's;
    the R-tree's were a slightly-too-large box that happened to be what
    one caller read.

    ``widened=True`` returns the float32 values widened to doubles
    (44.6173477172852 rather than 44.617348) — the same numbers, printed
    in full. This is NOT the R-tree's outward rounding; it is only the
    difference between a shortest-decimal rendering and a full one, and
    exists for callers comparing against a full-precision dump.
    """
    if rtid == 1:
        return (0.0, 360.0, CAP_BOUNDARY, 90.0)
    if rtid == NROWS:
        return (0.0, 360.0, -90.0, -CAP_BOUNDARY)
    i, k = _ring_and_bin(rtid)
    n32 = F32(nrabins(i))
    ramin32 = F32(180.0) * F32(2 * k - 1) / n32
    ramax32 = F32(180.0) * F32(2 * k + 1) / n32
    decmin32 = F32(dec_at(i + 0.5))
    decmax32 = F32(dec_at(i - 0.5))
    if widened:
        return (float(ramin32), float(ramax32),
                float(decmin32), float(decmax32))
    return (float(str(ramin32)), float(str(ramax32)),
            float(str(decmin32)), float(str(decmax32)))


def neighbors_of(rtid):
    """rtids of every tile sharing an edge or corner with `rtid`.

    Closed form, no I/O. Same contract as the SQLite class's
    `get_all_neighboring_rtids`: left/right within the ring, then every
    tile in the rings above and below whose RA span overlaps this one.
    A pole tile's neighbours are the whole adjacent ring.
    """
    if rtid == 1:
        return list(range(_OFFSET[1], _OFFSET[1] + nrabins(1)))
    if rtid == NROWS:
        last = NRINGS
        return list(range(_OFFSET[last], _OFFSET[last] + nrabins(last)))

    i, k = _ring_and_bin(rtid)
    n = nrabins(i)
    out = [_OFFSET[i] + (k - 1) % n, _OFFSET[i] + (k + 1) % n]

    lo, hi = (2 * k - 1) / (2.0 * n), (2 * k + 1) / (2.0 * n)   # RA span, turns
    for j in (i - 1, i + 1):
        if j < 1:
            out.append(1)
            continue
        if j > NRINGS:
            out.append(NROWS)
            continue
        out.extend(_overlapping_bins(j, lo, hi))
    return out


def _overlapping_bins(j, lo, hi):
    """rtids in ring `j` whose RA span touches the turn-fraction span [lo, hi].

    Bin `b` of ring `m` spans turn fractions [(2b-1)/2m, (2b+1)/2m]. Tiles
    that merely share an edge count as neighbours (corner adjacency), so
    the comparison is inclusive at both ends. Floating-point slack of half
    a bin-width is folded in so an exactly-shared edge is never missed.
    """
    m = nrabins(j)
    eps = 0.25 / m
    first = int(math.ceil(lo * m - 0.5 - eps))
    last = int(math.floor(hi * m + 0.5 + eps))
    return [_OFFSET[j] + (b % m) for b in range(first, last + 1)]


def _ring_and_bin(rtid):
    """Invert rtid -> (ring index, RA bin) for a non-pole tile."""
    if rtid < 2 or rtid >= NROWS:
        raise ValueError("not an interior rtid: %r" % (rtid,))
    lo, hi = 1, NRINGS
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _OFFSET[mid] <= rtid:
            lo = mid
        else:
            hi = mid - 1
    return lo, rtid - _OFFSET[lo]
