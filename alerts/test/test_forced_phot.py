"""Tests for alerts.forced_phot.

Geometry first: the containment predicate that every later section
builds on, exercised on a real SCA footprint (rid 1799519, socsim
2026-08-07) and on synthetic footprints at the RA wrap and the pole, plus
the two radius constants against synthetic rolled footprints.
"""

import math

import numpy as np
import pytest

from alerts.forced_phot import (CONE_RADIUS_DEG, SCA_HALF_DIAGONAL_DEG,
                                SCI_BASENAME, Measurement, Position,
                                PrevImage, contains_positions,
                                diff_psf_basename, find_prev_images,
                                q3c_search, sci_psf_basename)

# l2filemeta corners of a real chip (ra1,dec1 .. ra4,dec4, perimeter order)
# and its center (== CRVAL). Sides ~0.125 deg, rolled ~30 deg.
REAL_CORNERS = (266.69423625996524, -28.95060965014533,
                266.5716784556369, -28.886592489916563,
                266.64246844025536, -28.780530765996883,
                266.7656594139374, -28.844732312424743)
REAL_RA0, REAL_DEC0 = 266.66827099595, -28.865888886712


def square(ra_c, dec_c, half_deg):
    """Axis-aligned square footprint in perimeter order, RA wrapped."""
    return ((ra_c - half_deg) % 360, dec_c - half_deg,
            (ra_c + half_deg) % 360, dec_c - half_deg,
            (ra_c + half_deg) % 360, dec_c + half_deg,
            (ra_c - half_deg) % 360, dec_c + half_deg)


def unit(ra, dec):
    ra, dec = np.radians(ra), np.radians(dec)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], -1)


def separation_deg(ra1, dec1, ra2, dec2):
    return np.degrees(np.arccos(np.clip(np.sum(unit(ra1, dec1) * unit(ra2, dec2), axis=-1), -1, 1)))


# ---------------------------------------------------------------------------
# contains_positions on a real footprint
# ---------------------------------------------------------------------------

def test_real_chip_contains_its_center_and_rejects_far_points():
    inside = contains_positions([REAL_CORNERS],
                                [REAL_RA0, REAL_RA0 + 0.1, REAL_RA0, 266.72, 266.60],
                                [REAL_DEC0, REAL_DEC0, REAL_DEC0 + 0.1, -28.90, -28.95])
    assert inside.tolist() == [[True, False, False, True, False]]


def test_real_chip_corners_are_in_perimeter_order():
    # the predicate assumes adjacent corners share an edge; the registration
    # script guarantees it and this pins the convention on real data
    ra, dec = REAL_CORNERS[0::2], REAL_CORNERS[1::2]
    sides = [separation_deg(ra[i], dec[i], ra[(i + 1) % 4], dec[(i + 1) % 4]) for i in range(4)]
    diagonals = [separation_deg(ra[0], dec[0], ra[2], dec[2]), separation_deg(ra[1], dec[1], ra[3], dec[3])]
    assert max(sides) < min(diagonals)
    assert all(0.12 < s < 0.13 for s in sides)


def test_reversed_perimeter_order_gives_same_answer():
    reversed_corners = REAL_CORNERS[6:8] + REAL_CORNERS[4:6] + REAL_CORNERS[2:4] + REAL_CORNERS[0:2]
    assert contains_positions([reversed_corners], [REAL_RA0], [REAL_DEC0]).tolist() == [[True]]
    assert contains_positions([reversed_corners], [REAL_RA0 + 0.1], [REAL_DEC0]).tolist() == [[False]]


def test_antipode_of_interior_point_is_rejected():
    # same-side test alone admits the antipode; the hemisphere test must not
    assert not contains_positions([REAL_CORNERS], [(REAL_RA0 + 180) % 360], [-REAL_DEC0])[0, 0]


# ---------------------------------------------------------------------------
# RA wrap and poles
# ---------------------------------------------------------------------------

def test_footprint_straddling_ra_zero():
    wrap = square(0.0, 10.0, 0.0625)
    inside = contains_positions([wrap], [359.99, 0.01, 0.1, 359.9], [10.0, 10.0, 10.0, 10.0])
    assert inside.tolist() == [[True, True, False, False]]


def test_polar_cap_footprint():
    cap = (0.0, 89.9, 90.0, 89.9, 180.0, 89.9, 270.0, 89.9)
    inside = contains_positions([cap], [0.0, 45.0, 45.0, 0.0], [90.0, 89.95, 89.8, -90.0])
    assert inside.tolist() == [[True, True, False, False]]


# ---------------------------------------------------------------------------
# shapes and errors
# ---------------------------------------------------------------------------

def test_matrix_shape_and_vectorization():
    corners = [REAL_CORNERS, square(0.0, 10.0, 0.0625)]
    ra = [REAL_RA0, 0.0, 1.0, 359.99]
    dec = [REAL_DEC0, 10.0, 1.0, 10.0]
    matrix = contains_positions(corners, ra, dec)
    assert matrix.shape == (2, 4)
    assert matrix.dtype == bool
    # every entry equals the one-image, one-position evaluation
    for i, c in enumerate(corners):
        for j in range(4):
            assert matrix[i, j] == contains_positions([c], [ra[j]], [dec[j]])[0, 0]


def test_empty_inputs():
    assert contains_positions(np.zeros((0, 8)), [1.0], [1.0]).shape == (0, 1)
    assert contains_positions([REAL_CORNERS], [], []).shape == (1, 0)


def test_bad_corner_shape_raises():
    with pytest.raises(ValueError):
        contains_positions([REAL_CORNERS[:6]], [0.0], [0.0])


# ---------------------------------------------------------------------------
# the radius constants, against synthetic rolled footprints
# ---------------------------------------------------------------------------

def rolled_copies(corners, ra0, dec0, n, max_offset_deg, rng):
    """`n` copies of a footprint, each shifted and rolled at random in the
    tangent plane at (ra0, dec0). Returns (corners (n, 8), centers (n, 2))."""
    c = unit(ra0, dec0)
    east = np.cross([0.0, 0.0, 1.0], c); east /= np.linalg.norm(east); north = np.cross(c, east)
    v = unit(corners[0::2], corners[1::2])
    xy = np.stack([(v @ east) / (v @ c), (v @ north) / (v @ c)], axis=1)      # gnomonic, radians
    out = np.empty((n, 8)); centers = np.empty((n, 2))
    for i in range(n):
        roll = rng.uniform(0, 2 * np.pi); r = np.radians(rng.uniform(0, max_offset_deg)); a = rng.uniform(0, 2 * np.pi)
        rot = np.array([[np.cos(roll), -np.sin(roll)], [np.sin(roll), np.cos(roll)]])
        pts = xy @ rot.T + [r * np.cos(a), r * np.sin(a)]
        vec = c + pts[:, [0]] * east + pts[:, [1]] * north
        vec /= np.linalg.norm(vec, axis=1, keepdims=True)
        out[i, 0::2] = np.degrees(np.arctan2(vec[:, 1], vec[:, 0])) % 360
        out[i, 1::2] = np.degrees(np.arcsin(vec[:, 2]))
        cen = vec.mean(axis=0)
        centers[i] = np.degrees(np.arctan2(cen[1], cen[0])) % 360, np.degrees(np.arcsin(cen[2] / np.linalg.norm(cen)))
    return out, centers


def test_half_diagonal_bounds_point_containment():
    # any footprint containing a point has its center within one
    # half-diagonal of it: the point-search cone radius
    rng = np.random.default_rng(1)
    corners, centers = rolled_copies(REAL_CORNERS, REAL_RA0, REAL_DEC0, 3000, 0.15, rng)
    inside = contains_positions(corners, [REAL_RA0], [REAL_DEC0])[:, 0]
    assert inside.sum() > 500
    dist = separation_deg(centers[:, 0], centers[:, 1], REAL_RA0, REAL_DEC0)
    assert dist[inside].max() < SCA_HALF_DIAGONAL_DEG
    # and the bound is tight: something inside sits beyond 0.9 of it
    assert dist[inside].max() > 0.9 * SCA_HALF_DIAGONAL_DEG


def test_cone_radius_catches_every_footprint_holding_a_chip_position():
    # positions spread over the alerting chip; any footprint holding one of
    # them must have its center inside CONE_RADIUS_DEG of the chip center
    rng = np.random.default_rng(2)
    corners, centers = rolled_copies(REAL_CORNERS, REAL_RA0, REAL_DEC0, 3000, 0.30, rng)
    # positions: the chip's own corners pulled 1% inward plus its center
    ra = np.array(REAL_CORNERS[0::2]); dec = np.array(REAL_CORNERS[1::2])
    pos_ra = np.append(REAL_RA0 + 0.99 * (ra - REAL_RA0), REAL_RA0)
    pos_dec = np.append(REAL_DEC0 + 0.99 * (dec - REAL_DEC0), REAL_DEC0)
    holds_any = contains_positions(corners, pos_ra, pos_dec).any(axis=1)
    dist = separation_deg(centers[:, 0], centers[:, 1], REAL_RA0, REAL_DEC0)
    assert holds_any.sum() > 500
    assert dist[holds_any].max() < CONE_RADIUS_DEG
    assert dist[holds_any].max() > 2 * SCA_HALF_DIAGONAL_DEG * 0.9   # near the tight bound
    assert CONE_RADIUS_DEG > 2 * SCA_HALF_DIAGONAL_DEG


# ---------------------------------------------------------------------------
# search: q3c_search builds the query, find_prev_images assigns positions
# ---------------------------------------------------------------------------

JOB_DIR = "s3://bucket/20271001/jid42"


def db_row(pid, rid, corners, band="W146", mjdobs=61680.5, fid=8, expid=100, sca=7):
    row = {"pid": pid, "rid": rid, "expid": expid, "sca": sca, "fid": fid, "band": band,
           "mjdobs": mjdobs, "diff_filename": f"{JOB_DIR}/sfftdiffimage_masked.fits",
           "l2_filename": f"s3://l2/r{rid}.fits.gz"}
    for name, value in zip(("ra1", "dec1", "ra2", "dec2", "ra3", "dec3", "ra4", "dec4"), corners):
        row[name] = value
    return row


class RecordingQuery:
    """Stand-in for a provider's _query: records the call, returns canned rows."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))
        return self.rows


def test_q3c_search_query_shape_and_row_mapping():
    q = RecordingQuery([db_row(1, 10, REAL_CORNERS)])
    images = q3c_search(q, REAL_RA0, REAL_DEC0, fid=1, ppid=15, mjd_lo=61670.0, mjd_hi=61685.0)
    sql, params = q.calls[0]
    assert "q3c_radial_query(d.ra0, d.dec0, %s, %s, %s)" in sql
    assert "JOIN l2files l ON l.rid = d.rid" in sql and "JOIN filters f" in sql
    assert "d.status > 0" in sql and "d.vbest > 0" in sql
    assert "d.fid = %s" in sql and "d.ppid = %s" in sql
    assert "l.mjdobs >= %s" in sql and "l.mjdobs < %s" in sql
    assert params == (REAL_RA0, REAL_DEC0, CONE_RADIUS_DEG, 1, 15, 61670.0, 61685.0)
    (img,) = images
    assert isinstance(img, PrevImage)
    assert (img.pid, img.rid, img.band, img.sca, img.expid) == (1, 10, "W146", 7, 100)
    assert img.corners == REAL_CORNERS and len(img.corners) == 8
    assert img.diff_filename == f"{JOB_DIR}/sfftdiffimage_masked.fits"
    assert img.sci_filename == f"{JOB_DIR}/{SCI_BASENAME}"
    assert img.diff_psf == f"{JOB_DIR}/sfftdiffpsf.fits"
    # as seen in real job dir 20260722/jid129264 (socsim, W146, SCA 7)
    assert img.sci_psf == f"{JOB_DIR}/WFI_SCA07_F146_PSF_DET_DIST_normalized.fits"
    assert img.l2_filename == "s3://l2/r10.fits.gz"
    assert img.position_index.size == 0


def test_q3c_search_optional_filters_are_omitted_when_none():
    q = RecordingQuery([])
    assert q3c_search(q, REAL_RA0, REAL_DEC0) == []
    sql, params = q.calls[0]
    assert "d.fid = %s" not in sql and "d.ppid = %s" not in sql and "mjdobs >=" not in sql
    assert params == (REAL_RA0, REAL_DEC0, CONE_RADIUS_DEG)


def test_q3c_search_diff_basename_override_switches_image_and_psf_together():
    q = RecordingQuery([db_row(1, 10, REAL_CORNERS)])
    (img,) = q3c_search(q, REAL_RA0, REAL_DEC0, diff_basename="zogy_diffimage_masked.fits")
    assert img.diff_filename == f"{JOB_DIR}/zogy_diffimage_masked.fits"
    assert img.diff_psf == f"{JOB_DIR}/diffpsf.fits"


def test_psf_basenames_follow_the_job_directory_convention():
    # rimtimsim jid143919: Z087 on SCA 2 -> WFI_SCA02_F087_PSF_DET_DIST_normalized.fits
    assert sci_psf_basename("Z087", 2) == "WFI_SCA02_F087_PSF_DET_DIST_normalized.fits"
    assert sci_psf_basename("F184", 18) == "WFI_SCA18_F184_PSF_DET_DIST_normalized.fits"
    assert diff_psf_basename("sfftdiffimage_dconv_masked.fits") == "sfftdiffpsf_dconv.fits"
    # an unknown filter or flavor must fail before anything is staged
    with pytest.raises(ValueError):
        sci_psf_basename("F146", 1)          # Roman token given where the RAPID name is expected
    with pytest.raises(ValueError):
        diff_psf_basename("naive_diffimage_masked.fits")


def shifted(corners, dra, ddec):
    return tuple(v + (dra if i % 2 == 0 else ddec) for i, v in enumerate(corners))


def test_find_prev_images_assigns_positions_and_drops_empty_images():
    # three cone candidates: the chip itself, a copy shifted by half a chip
    # (holds only the positions on one side), and a neighbouring SCA that
    # holds none of them
    same = db_row(1, 10, REAL_CORNERS, mjdobs=61680.1)
    half = db_row(2, 11, shifted(REAL_CORNERS, 0.07, 0.0), mjdobs=61680.2)
    neighbour = db_row(3, 12, shifted(REAL_CORNERS, 0.16, 0.0), mjdobs=61680.3)
    q = RecordingQuery([same, half, neighbour])
    positions = [Position(101, REAL_RA0, REAL_DEC0),            # center: in same + half
                 Position(102, REAL_RA0 - 0.05, REAL_DEC0),     # west side: in same only
                 Position(103, REAL_RA0 + 0.05, REAL_DEC0),     # east side: in same + half
                 Position(104, REAL_RA0 + 0.5, REAL_DEC0)]      # nowhere
    kept = find_prev_images(q, positions, REAL_RA0, REAL_DEC0, fid=1)
    assert [img.pid for img in kept] == [1, 2]
    assert kept[0].position_index.tolist() == [0, 1, 2]
    assert kept[1].position_index.tolist() == [0, 2]
    # ids survive: the driver measures positions[idx] on each image
    assert [positions[i].pos_id for i in kept[1].position_index] == [101, 103]


def test_find_prev_images_with_no_positions_or_no_candidates():
    q = RecordingQuery([db_row(1, 10, REAL_CORNERS)])
    assert find_prev_images(q, [], REAL_RA0, REAL_DEC0) == []
    assert find_prev_images(RecordingQuery([]), [Position(1, REAL_RA0, REAL_DEC0)], REAL_RA0, REAL_DEC0) == []


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

def test_records_are_plain_and_keyed():
    p = Position(pos_id=7, ra=1.0, dec=2.0)
    with pytest.raises(Exception):
        p.ra = 3.0                       # frozen: the key/position pair never mutates
    img = PrevImage(rid=1, pid=2, diff_filename="d.fits", sci_filename="s.fits", diff_psf="dp.fits",
                    sci_psf="sp.fits", l2_filename="l2.fits.gz", mjdobs=60000.0, fid=8, band="W146",
                    expid=3, sca=4, corners=REAL_CORNERS)
    assert img.position_index.size == 0
    m = Measurement(pos_id=7, rid=1, x=10.0, y=20.0, flux=math.nan, fluxerr=math.nan, flags=1)
    assert m.flags == 1 and math.isnan(m.flux)
