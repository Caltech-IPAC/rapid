"""Tests for alerts.forced_phot.

Geometry first: the containment predicate that every later section
builds on, exercised on a real SCA footprint (rid 1799519, socsim
2026-08-07) and on synthetic footprints at the RA wrap and the pole, plus
the two radius constants against synthetic rolled footprints.
"""

import math
import os

import numpy as np
import pytest

import fitsio

from conftest import NOISE_SIGMA, sky_positions  # noqa: F401 (synthetic_chip is a fixture)
from wcs_eval import separation_mas, tpv_pixel_to_sky

from alerts.forced_phot import (CONE_RADIUS_DEG, FLAG_NONFINITE,
                                SCA_HALF_DIAGONAL_DEG, SCI_BASENAME,
                                STAMP_MARGIN_PX, ChipImage, Measurement,
                                Position, PrevImage, assemble_history,
                                contains_positions, diff_psf_basename,
                                error_array, find_prev_images,
                                forced_photometry, load_psf, open_image,
                                project_positions, psfphot, q3c_search,
                                read_positions_csv, run_prev_images,
                                sci_psf_basename, write_measurements_csv,
                                FORCED_TABLE_DTYPE, forced_measurement_id,
                                read_table, write_table)

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
# photometry: open_image and the stopgap error_array
# ---------------------------------------------------------------------------

def test_open_image_reads_pixels_header_and_wcs(job_dir, tpv_header, chip_image):
    img = open_image(str(job_dir / SCI_BASENAME))
    assert isinstance(img, ChipImage)
    assert img.shape == chip_image.shape
    assert np.allclose(img.data, chip_image + 100_000.0)
    assert img.header["CTYPE1"] == "RA---TPV"
    assert img.error is None and not img.mask.any()
    # the WCS is the header's, distortion included: astropy agrees with the
    # suite's own TPV evaluator (the PV1_0/PV2_0 terms shift CRPIX off CRVAL
    # by a few mas, so CRVAL itself is not the reference)
    for px, py in ((150.5, 150.5), (1.0, 1.0), (301.0, 1.0), (301.0, 301.0), (1.0, 301.0), (77.3, 212.9)):
        ra, dec = img.wcs.all_pix2world([[px, py]], 1)[0]
        ra_ref, dec_ref = tpv_pixel_to_sky(tpv_header, px, py)
        assert separation_mas(ra, dec, ra_ref, dec_ref) < 1.0


def test_open_image_with_uncertainty_file(job_dir, tpv_header, chip_image):
    unc = np.full(chip_image.shape, 2.5, dtype=np.float32)
    unc[10, 10] = 0.0                          # non-positive error -> unusable pixel
    unc[20, 20] = np.nan
    fitsio.write(str(job_dir / "unc.fits"), unc, header=dict(tpv_header), clobber=True)
    img = open_image(str(job_dir / SCI_BASENAME), uncert_path=str(job_dir / "unc.fits"))
    assert img.error is not None and img.error[0, 0] == 2.5
    assert img.mask[10, 10] and img.mask[20, 20] and img.mask.sum() == 2
    fitsio.write(str(job_dir / "bad.fits"), unc[:100, :100], header=dict(tpv_header), clobber=True)
    with pytest.raises(ValueError):
        open_image(str(job_dir / SCI_BASENAME), uncert_path=str(job_dir / "bad.fits"))


def test_open_image_masks_nonfinite_pixels(job_dir, tpv_header, chip_image):
    data = chip_image.copy(); data[5, 5] = np.nan; data[6, 6] = np.inf
    fitsio.write(str(job_dir / "holes.fits"), data, header=dict(tpv_header), clobber=True)
    img = open_image(str(job_dir / "holes.fits"))
    assert img.mask[5, 5] and img.mask[6, 6] and img.mask.sum() == 2


def noisy_field(sigma=3.0, size=512, seed=0):
    rng = np.random.default_rng(seed)
    rows, cols = np.mgrid[0:size, 0:size]
    background = 100.0 + 0.05 * rows                      # a gradient, as in the bulge
    return background + rng.normal(0.0, sigma, (size, size)), background


def test_error_array_diff_recovers_the_noise_level():
    data, _ = noisy_field()
    err = error_array(data, "diff")
    assert err.dtype == np.float32 and err.shape == data.shape
    assert np.isfinite(err).all()
    assert abs(np.median(err) - 3.0) / 3.0 < 0.1


def test_error_array_science_adds_poisson_term_under_sources():
    data, background = noisy_field()
    data[200:210, 200:210] += 5000.0                       # a bright, flat source
    rms_only = error_array(data, "science")                # gain None: warns, no Poisson term
    with_gain = error_array(data, "science", gain=2.0)
    assert abs(np.median(rms_only[100:150, 100:150]) - 3.0) < 0.5
    # away from sources the Poisson term only adds the (clipped) noise
    # itself: a small, positive shift of the typical error
    empty_shift = np.median(with_gain[100:150, 100:150]) - np.median(rms_only[100:150, 100:150])
    assert 0.0 <= empty_shift < 0.5
    # sqrt(rms^2 + 5000/2) ~ 50.1 under the source
    assert abs(np.median(with_gain[202:208, 202:208]) - math.sqrt(9.0 + 2500.0)) < 2.0


def test_error_array_masks_and_rejects_bad_product():
    data, _ = noisy_field()
    data[50, 50] = np.nan
    mask = np.zeros(data.shape, bool); mask[60, 60] = True
    err = error_array(data, "diff", mask=mask)
    assert np.isnan(err[50, 50]) and np.isnan(err[60, 60])
    assert np.isfinite(err[70, 70])
    with pytest.raises(ValueError):
        error_array(data, "ref")


# ---------------------------------------------------------------------------
# photometry: projection, PSF, fitting, and the parent on a synthetic chip
# ---------------------------------------------------------------------------

# synthetic_chip, sky_positions and NOISE_SIGMA come from conftest (shared
# with the provider tests)

def test_project_positions_round_trips_and_applies_margin(synthetic_chip):
    image_path, _, _ = synthetic_chip
    img = open_image(image_path)
    # margin 12 on a 301-px axis: usable range is [13, 289]; probe one
    # pixel inside and outside it (the iterative inverse is good to ~1e-4
    # px, so exactly-on-the-line cases are not meaningful)
    pixels = [(60.0, 80.0), (150.5, 150.5), (12.0, 150.0), (150.0, 290.0), (14.0, 14.0), (288.0, 288.0)]
    positions = sky_positions(img.wcs, pixels)
    x, y, on = project_positions(img.wcs, img.shape, positions)
    assert np.allclose(x, [p[0] for p in pixels], atol=1e-3)
    assert np.allclose(y, [p[1] for p in pixels], atol=1e-3)
    assert on.tolist() == [True, True, False, False, True, True]
    assert STAMP_MARGIN_PX == 12
    x, y, on = project_positions(img.wcs, img.shape, [])
    assert x.size == 0 and on.size == 0


def test_load_psf_normalizes_and_fixes_position(synthetic_chip):
    _, psf_path, _ = synthetic_chip
    psf = load_psf(psf_path)
    assert psf.x_0.fixed and psf.y_0.fixed and not psf.flux.fixed
    assert abs(float(psf.data.sum()) - 1.0) < 1e-6


def test_psfphot_recovers_fluxes_and_flags_masked_position(synthetic_chip):
    image_path, psf_path, sources = synthetic_chip
    img = open_image(image_path); psf = load_psf(psf_path)
    error = np.full(img.shape, NOISE_SIGMA, dtype=np.float32)
    x = np.array([s[0] for s in sources] + [200.0]); y = np.array([s[1] for s in sources] + [200.0])
    flux, fluxerr, flags = psfphot(img.data, error, img.mask, psf, x, y)
    truth = np.array([s[2] for s in sources])
    assert np.allclose(flux[:4], truth, rtol=0.03)
    assert (flags[:4] == 0).all() and np.all(fluxerr[:4] > 0)
    # the position in the NaN hole: NaN flux, flagged with our bit as well as photutils'
    assert math.isnan(flux[4]) and flags[4] & FLAG_NONFINITE and flags[4] != FLAG_NONFINITE
    assert psfphot(img.data, error, img.mask, psf, [], [])[0].size == 0


def test_forced_photometry_parent_end_to_end(synthetic_chip):
    image_path, psf_path, sources = synthetic_chip
    img = open_image(image_path)
    pixels = [(s[0], s[1]) for s in sources] + [(200.0, 200.0), (3.0, 3.0)]   # + hole + off-chip
    positions = sky_positions(img.wcs, pixels, first_id=501)
    rows = forced_photometry(image_path, psf_path, positions, rid=77, product="diff")
    # off-chip position 506 has no row; the hole (505) has a flagged row
    assert [r.pos_id for r in rows] == [501, 502, 503, 504, 505]
    assert all(r.rid == 77 for r in rows)
    for r, (x, y, f) in zip(rows[:4], sources):
        assert abs(r.x - x) < 1e-3 and abs(r.y - y) < 1e-3
        assert abs(r.flux - f) / f < 0.03 and r.flags == 0
        # error came from error_array on noise sigma 1: a few counts for a 7x7 PSF fit
        assert 0.5 < r.fluxerr < 10.0
    assert math.isnan(rows[4].flux) and rows[4].flags & FLAG_NONFINITE
    # nothing on the chip -> no rows, no PSF needed
    far = [Position(9, REAL_RA0, REAL_DEC0)]
    assert forced_photometry(image_path, "/nonexistent/psf.fits", far, rid=77, product="diff") == []


# ---------------------------------------------------------------------------
# driver: run_prev_images, assemble_history, CSV helpers
# ---------------------------------------------------------------------------

class CopyingStage:
    """Stage stub: 'url' is a local file; copy it to a staging dir under its
    basename (as AlertDataProvider._stage does) and record every call."""

    def __init__(self, staging_dir):
        self.dir = staging_dir; self.calls = []

    def __call__(self, url):
        import shutil
        self.calls.append(url)
        if not os.path.exists(url):
            raise FileNotFoundError(url)
        local = os.path.join(self.dir, os.path.basename(url))
        shutil.copy(url, local)
        return local


def prev_image(rid, mjdobs, image_path, psf_path, corners=REAL_CORNERS, position_index=(), sci_psf=None):
    return PrevImage(rid=rid, pid=rid * 10, diff_filename=image_path, sci_filename=image_path,
                     diff_psf=psf_path, sci_psf=sci_psf or psf_path, l2_filename="l2.fits.gz", mjdobs=mjdobs,
                     fid=8, band="W146", expid=1, sca=7, corners=corners,
                     position_index=np.array(position_index, dtype=int))


def test_run_prev_images_measures_products_stamps_rows_and_cleans_up(synthetic_chip, tmp_path):
    image_path, psf_path, sources = synthetic_chip
    img = open_image(image_path)
    positions = sky_positions(img.wcs, [(s[0], s[1]) for s in sources], first_id=1)
    staging = tmp_path / "staging"; staging.mkdir(); stage = CopyingStage(str(staging))
    # as in production, the science PSF has its own basename
    import shutil
    sci_psf = str(tmp_path / "WFI_SCA07_F146_PSF_DET_DIST_normalized.fits"); shutil.copy(psf_path, sci_psf)
    # epoch 2 holds only the first two positions (a partial overlap)
    images = [prev_image(11, 61680.1, image_path, psf_path, position_index=[0, 1, 2, 3], sci_psf=sci_psf),
              prev_image(12, 61680.2, image_path, psf_path, position_index=[0, 1], sci_psf=sci_psf)]
    out = list(run_prev_images(images, positions, stage))
    assert [(i.rid, p, len(rows)) for i, p, rows in out] == \
        [(11, "diff", 4), (11, "science", 4), (12, "diff", 2), (12, "science", 2)]
    for image, product, rows in out:
        for r in rows:
            assert r.product == product and r.mjdobs == image.mjdobs and r.rid == image.rid
            assert r.flags == 0 and abs(r.flux - sources[r.pos_id - 1][2]) / sources[r.pos_id - 1][2] < 0.03
    # every staged copy was deleted except the cached science PSF
    assert sorted(os.listdir(staging)) == [os.path.basename(sci_psf)]
    # the science PSF was staged once for the run; the diff PSF once per epoch
    assert stage.calls.count(sci_psf) == 1 and stage.calls.count(psf_path) == 2
    # the science-image fits are noisier than the diff ones only via the
    # Poisson term; with no GAIN in the header the two products agree
    diff_rows = {r.pos_id: r for r in out[0][2]}; sci_rows = {r.pos_id: r for r in out[1][2]}
    assert all(diff_rows[k].flux == sci_rows[k].flux for k in diff_rows)


def test_run_prev_images_keeps_a_cached_psf_that_shares_a_staged_basename(synthetic_chip, tmp_path):
    # both PSFs staged under the same basename (the stager keys by basename):
    # deleting the diff PSF after epoch 1 must not remove the cached science
    # PSF, and epoch 2's science fit must still succeed
    image_path, psf_path, sources = synthetic_chip
    img = open_image(image_path)
    positions = sky_positions(img.wcs, [(sources[0][0], sources[0][1])])
    staging = tmp_path / "staging"; staging.mkdir(); stage = CopyingStage(str(staging))
    images = [prev_image(11, 61680.1, image_path, psf_path, position_index=[0]),
              prev_image(12, 61680.2, image_path, psf_path, position_index=[0])]
    out = list(run_prev_images(images, positions, stage, strict=True))
    assert [(i.rid, p) for i, p, _ in out] == [(11, "diff"), (11, "science"), (12, "diff"), (12, "science")]
    assert all(rows[0].flags == 0 for _, _, rows in out)


def test_run_prev_images_skips_images_without_positions_and_selects_products(synthetic_chip, tmp_path):
    image_path, psf_path, sources = synthetic_chip
    img = open_image(image_path)
    positions = sky_positions(img.wcs, [(s[0], s[1]) for s in sources])
    staging = tmp_path / "staging"; staging.mkdir(); stage = CopyingStage(str(staging))
    images = [prev_image(11, 61680.1, image_path, psf_path, position_index=[]),
              prev_image(12, 61680.2, image_path, psf_path, position_index=[3])]
    out = list(run_prev_images(images, positions, stage, products=("diff",)))
    assert [(i.rid, p, [r.pos_id for r in rows]) for i, p, rows in out] == [(12, "diff", [4])]


def test_run_prev_images_failure_is_skipped_or_raised(synthetic_chip, tmp_path, caplog):
    image_path, psf_path, sources = synthetic_chip
    img = open_image(image_path)
    positions = sky_positions(img.wcs, [(sources[0][0], sources[0][1])])
    staging = tmp_path / "staging"; staging.mkdir(); stage = CopyingStage(str(staging))
    images = [prev_image(11, 61680.1, "/nonexistent/diff.fits", psf_path, position_index=[0]),
              prev_image(12, 61680.2, image_path, psf_path, position_index=[0])]
    with caplog.at_level("WARNING"):
        out = list(run_prev_images(images, positions, stage, products=("diff",)))
    assert [i.rid for i, _, _ in out] == [12]
    assert "rid=11" in caplog.text and "1 image-product measurements failed" in caplog.text
    with pytest.raises(FileNotFoundError):
        list(run_prev_images(images, positions, stage, products=("diff",), strict=True))


def test_run_prev_images_uses_uncertainty_hook(synthetic_chip, tmp_path, tpv_header, chip_image):
    image_path, psf_path, sources = synthetic_chip
    img = open_image(image_path)
    positions = sky_positions(img.wcs, [(sources[0][0], sources[0][1])])
    unc_path = str(tmp_path / "unc.fits")
    fitsio.write(unc_path, np.full(img.shape, 4.0, dtype=np.float32), header=dict(tpv_header), clobber=True)
    stage = CopyingStage(str(tmp_path / "s")); os.mkdir(tmp_path / "s")
    images = [prev_image(11, 61680.1, image_path, psf_path, position_index=[0])]
    hook = lambda image, product: unc_path if product == "diff" else None
    with_unc = list(run_prev_images(images, positions, stage, products=("diff",), uncert_url=hook))
    without = list(run_prev_images(images, positions, stage, products=("diff",)))
    assert unc_path in stage.calls
    # a 4x larger per-pixel error than the estimate (~1) -> ~4x larger flux error
    ratio = with_unc[0][2][0].fluxerr / without[0][2][0].fluxerr
    assert 3.0 < ratio < 5.0


def test_assemble_history_groups_and_orders():
    rows = [Measurement(1, 12, 0, 0, 1.0, 0.1, product="science", mjdobs=2.0),
            Measurement(2, 11, 0, 0, 5.0, 0.1, product="diff", mjdobs=1.0),
            Measurement(1, 11, 0, 0, 2.0, 0.1, product="diff", mjdobs=1.0),
            Measurement(1, 12, 0, 0, 3.0, 0.1, product="diff", mjdobs=2.0),
            Measurement(1, 11, 0, 0, 4.0, 0.1, product="science", mjdobs=1.0)]
    history = assemble_history(rows)
    assert sorted(history) == [1, 2]
    assert [(r.mjdobs, r.product) for r in history[1]] == \
        [(1.0, "diff"), (1.0, "science"), (2.0, "diff"), (2.0, "science")]
    assert history[2][0].flux == 5.0
    assert assemble_history([]) == {}


def test_csv_round_trip(tmp_path):
    pos_csv = tmp_path / "pos.csv"
    pos_csv.write_text("pos_id,ra,dec\n7,266.6,-28.9\n8,266.7,-28.8\n")
    positions = read_positions_csv(str(pos_csv))
    assert positions == [Position(7, 266.6, -28.9), Position(8, 266.7, -28.8)]
    rows = [Measurement(7, 11, 10.5, 20.5, 123.0, 4.5, flags=0, product="diff", mjdobs=61680.1),
            Measurement(7, 11, 10.5, 20.5, math.nan, math.nan, flags=FLAG_NONFINITE, product="science", mjdobs=61680.1)]
    out_csv = tmp_path / "rows.csv"
    write_measurements_csv(str(out_csv), rows)
    lines = out_csv.read_text().splitlines()
    assert lines[0] == "pos_id,rid,product,mjdobs,x,y,flux,fluxerr,flags"
    assert lines[1] == "7,11,diff,61680.1,10.5,20.5,123.0,4.5,0"
    assert lines[2].startswith("7,11,science,61680.1,10.5,20.5,nan,nan,")


# ---------------------------------------------------------------------------
# per-chip table: id and parquet round trip
# ---------------------------------------------------------------------------

def test_forced_measurement_id_is_deterministic_and_position_sensitive():
    a = forced_measurement_id(11, 777, 266.668271, -28.865889)
    assert a == forced_measurement_id(11, 777, 266.668271, -28.865889)
    assert 0 < a < 2**63
    assert a != forced_measurement_id(12, 777, 266.668271, -28.865889)   # another epoch
    assert a != forced_measurement_id(11, 778, 266.668271, -28.865889)   # another object
    # a re-run at a moved position (~0.7 mas) is a new measurement
    assert a != forced_measurement_id(11, 777, 266.668271 + 2e-7, -28.865889)
    # ...but jitter below the 1e-7 deg (0.36 mas) rounding grain is not
    assert a == forced_measurement_id(11, 777, 266.668271 + 1e-9, -28.865889)


def test_table_parquet_round_trip(tmp_path):
    table = np.zeros(3, dtype=FORCED_TABLE_DTYPE)
    table["forced_id"] = [5, 6, 7]; table["aid"] = [777, 777, 778]; table["rid"] = [11, 12, 11]
    table["band"] = ["W146", "W146", "Z087"]; table["mjdobs"] = [60500.5, 60499.0, 60500.5]
    table["psf_flux"] = [1.5, np.nan, -2.0]; table["science_flux"] = [100.0, 101.0, np.nan]
    table["flags"] = [0, 1 << 16, 0]; table["x"] = [10.5, 11.5, 12.5]
    path = tmp_path / "sub" / "dir" / "forced_phot_pid99.parquet"     # parents created
    write_table(table, str(path))
    back = read_table(str(path))
    assert back.dtype == FORCED_TABLE_DTYPE and len(back) == 3
    for name in FORCED_TABLE_DTYPE.names:
        if table.dtype[name].kind == "f":
            assert np.array_equal(back[name], table[name], equal_nan=True)
        else:
            assert np.array_equal(back[name], table[name])
    assert read_table(str(tmp_path / "missing.parquet")) is None


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
