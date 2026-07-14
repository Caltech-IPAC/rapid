"""Minimal forward TPV WCS evaluation (pixel -> sky) for tests.

astropy.wcs is unavailable in the pipeline container (binary mismatch with
numpy 2), and python-fitsio exposes no WCS transforms at all, so the tests
carry their own evaluator. Forward TPV is simple enough to inline: linear
CD projection to intermediate world coordinates, the PV distortion
polynomials, then gnomonic (TAN) deprojection.

This evaluator was validated against the real pipeline products: evaluated
on an 18x18 grid across a chip it agreed with the science image's
independent SIP solution to < 1e-4 mas, and it reproduces the database's
sources.ra/dec from (xfit+1, yfit+1) to 0.0 mas (see test_live_db.py).

Accepts any header-like object with .get() (dict, fitsio FITSHDR,
astropy Header).
"""

import numpy as np


def tpv_terms(x, y, r):
    """The TPV polynomial term basis, in standard PV index order 0..23.

    For PV1_* the arguments are (x, y, r); for PV2_* the convention swaps
    the axes, so pass (y, x, r).
    """
    return [np.ones_like(x), x, y, r,                          # 0-3
            x**2, x*y, y**2,                                   # 4-6
            x**3, x**2*y, x*y**2, y**3, r**3,                  # 7-11
            x**4, x**3*y, x**2*y**2, x*y**3, y**4,             # 12-16
            x**5, x**4*y, x**3*y**2, x**2*y**3, x*y**4, y**5,  # 17-22
            r**5]                                              # 23


def tpv_pixel_to_sky(header, px, py):
    """Evaluate a TPV (or plain TAN) WCS at 1-based pixel (px, py).

    Returns (ra, dec) in degrees. Missing PV cards default to the
    identity transform (PV1_1 = PV2_1 = 1, all others 0), so a header
    with no PV cards at all is evaluated as plain TAN.
    """
    px, py = np.asarray(px, dtype=float), np.asarray(py, dtype=float)
    u, v = px - header.get("CRPIX1"), py - header.get("CRPIX2")
    x = header.get("CD1_1") * u + header.get("CD1_2", 0.0) * v
    y = header.get("CD2_1", 0.0) * u + header.get("CD2_2") * v
    r = np.sqrt(x**2 + y**2)
    t1, t2 = tpv_terms(x, y, r), tpv_terms(y, x, r)
    pv1 = [header.get(f"PV1_{i}", 1.0 if i == 1 else 0.0)
           for i in range(len(t1))]
    pv2 = [header.get(f"PV2_{i}", 1.0 if i == 1 else 0.0)
           for i in range(len(t2))]
    xi = np.deg2rad(sum(c * t for c, t in zip(pv1, t1)))
    eta = np.deg2rad(sum(c * t for c, t in zip(pv2, t2)))

    # gnomonic (TAN) deprojection of the tangent-plane coords around CRVAL
    ra0 = np.deg2rad(header.get("CRVAL1"))
    dec0 = np.deg2rad(header.get("CRVAL2"))
    rho = np.hypot(xi, eta)
    c = np.arctan(rho)
    safe_rho = np.where(rho == 0, 1.0, rho)
    dec = np.arcsin(np.cos(c) * np.sin(dec0)
                    + eta * np.sin(c) * np.cos(dec0) / safe_rho)
    ra = ra0 + np.arctan2(xi * np.sin(c),
                          rho * np.cos(dec0) * np.cos(c)
                          - eta * np.sin(dec0) * np.sin(c))
    return float(np.rad2deg(ra)), float(np.rad2deg(dec))


def separation_mas(ra1, dec1, ra2, dec2):
    """Small-angle separation between two positions, in milliarcseconds."""
    dra = (ra1 - ra2) * np.cos(np.deg2rad(dec1))
    return float(np.hypot(dra, dec1 - dec2) * 3.6e6)
