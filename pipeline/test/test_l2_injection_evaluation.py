"""The vectorised light-curve evaluation equals the per-source loop it replaced.

`modules.fake_src.rapid_l2_injections._evaluate_catalogs_at_mjd` used to
evaluate one source at a time through `SinusoidalLightCurve` and
`GaussianLightCurve`. Dev f0630bae vectorised it per catalogue by INLINING
the two light-curve formulae, which duplicated the photometric model in
`modules/fake_src/injectionLightCurveModels.py` and made the Gaussian
model's `time_bounds` branch unreachable from the injector. The port keeps
the speed-up and calls the model functions with arrays instead, so the
model module stays the single source of truth. These tests pin that the
vectorised result is exactly what the per-source loop gives through the
same functions, and pin the edge cases the loop handled implicitly.

Stub tier. `rapid_l2_injections` imports romanisim, roman_datamodels, crds,
asdf and romancal at module import; those are stubbed here ONLY when they
are not installed, so the test runs on a laptop without shadowing a real
installation (the W8 rule in pipeline/stages/test/test_context.py).
"""

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest

import numpy as np


def _ensure(name, **attrs):
    """Insert a stub module for `name` only if it cannot be imported.

    A plain import attempt rather than `importlib.util.find_spec`: another
    test in the same session may already have stubbed a parent module with
    no `__spec__`, and `find_spec` raises on those instead of answering.
    """
    module = sys.modules.get(name)
    if module is None:
        try:
            module = importlib.import_module(name)
        except Exception:  # not installed, or a stubbed parent with no __path__
            module = types.ModuleType(name)
            sys.modules[name] = module
            parent, _, child = name.rpartition(".")
            if parent and parent in sys.modules:
                setattr(sys.modules[parent], child, module)
    for key, value in attrs.items():
        if not hasattr(module, key):
            setattr(module, key, value)
    return module


def _load_injections():
    for name in ("asdf", "crds", "roman_datamodels", "roman_datamodels.datamodels",
                 "romancal", "romancal.assign_wcs", "romanisim", "romanisim.models",
                 "romanisim.models.parameters", "romanisim.psf", "romanisim.wcs",
                 "romanisim.image"):
        _ensure(name)
    _ensure("romancal.assign_wcs", AssignWcsStep=object)
    _ensure("romanisim.image", inject_sources_into_l2=lambda *a, **k: None)
    _ensure("astropy.table", Table=object)
    return importlib.import_module("modules.fake_src.rapid_l2_injections")


class _LinearWCS:
    """A deterministic stand-in for romanisim.wcs.GWCS.toImage."""

    def toImage(self, ra, dec, units="deg"):
        x = (np.asarray(ra, dtype=np.float64) - 268.0) * 40000.0 - 500.0
        y = (np.asarray(dec, dtype=np.float64) + 28.6) * 40000.0 - 500.0
        return x, y


def _catalogue(n, seed=1):
    rng = np.random.default_rng(seed)
    sources = {}
    for i in range(n):
        kind = "sinusoidal" if i % 3 else "gaussian"
        if kind == "sinusoidal":
            params = {"magnitude": rng.uniform(20, 26), "amplitude": rng.uniform(0.01, 2),
                      "period": 10 ** rng.uniform(-2.7, 3), "phase": rng.uniform(0, 1)}
        else:
            params = {"magnitude": rng.uniform(23, 27), "peak_amplitude": rng.uniform(0.1, 5),
                      "peak_time": rng.uniform(61678, 61687), "sigma": 10 ** rng.uniform(-2.7, 2)}
        sources[str(i)] = {"ra": rng.uniform(268.0, 268.1), "dec": rng.uniform(-28.6, -28.5),
                           "type": kind, "parameters": params}
    return sources


def _per_source_reference(models, sources, mjd, wcs, image_size):
    """The loop the vectorised code replaced, through the same model functions."""
    ny, nx = image_size
    ra = np.array([s["ra"] for s in sources.values()], dtype=np.float64)
    dec = np.array([s["dec"] for s in sources.values()], dtype=np.float64)
    x, y = wcs.toImage(ra, dec)
    keep = (x >= -50.0) & (x < nx + 50.0) & (y >= -50.0) & (y < ny + 50.0)
    out = []
    for s, r, d, xi, yi, k in zip(sources.values(), ra, dec, x, y, keep):
        if not k:
            continue
        p = s["parameters"]
        if s["type"] == "sinusoidal":
            mag = models.SinusoidalLightCurve(mjd, p["magnitude"], p["amplitude"], p["period"], p["phase"])
            flux = 10 ** (-0.4 * mag)
        else:
            static = 10 ** (-0.4 * p["magnitude"])
            peak = static * (10 ** (0.4 * p["peak_amplitude"]) - 1.0)
            flux = models.GaussianLightCurve(mjd, p["peak_time"], peak, p["sigma"], static)
        out.append((r, d, flux, xi, yi))
    cols = list(zip(*out)) if out else [[]] * 5
    return tuple(np.array(c, dtype=np.float64) for c in cols)


class VectorisedEvaluationMatchesTheLoop(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.inj = _load_injections()
        cls.models = importlib.import_module("modules.fake_src.injectionLightCurveModels")
        cls.tmp = tempfile.mkdtemp(prefix="l2inj_")
        cls.mjd = 61682.5
        cls.size = (4088, 4088)
        cls.wcs = _LinearWCS()

    def _write(self, name, sources):
        cat = os.path.join(self.tmp, f"{name}.json")
        with open(cat, "w") as fh:
            json.dump(sources, fh)
        lst = os.path.join(self.tmp, f"{name}.txt")
        with open(lst, "w") as fh:
            fh.write(cat + "\n")
        return lst

    def test_identical_to_the_per_source_loop(self):
        sources = _catalogue(400)
        lst = self._write("cat", sources)
        got = self.inj._evaluate_catalogs_at_mjd(lst, self.mjd, self.size, self.wcs, "F146")
        want = _per_source_reference(self.models, sources, self.mjd, self.wcs, self.size)
        self.assertEqual(len(got), 5)
        self.assertGreater(len(got[0]), 0, "the fixture must keep some sources on-image")
        self.assertLess(len(got[0]), len(sources), "the fixture must also drop some")
        for name, g, w in zip(("ra", "dec", "flux", "x", "y"), got, want):
            np.testing.assert_array_equal(g, w, err_msg=f"{name} differs from the per-source loop")

    def test_uses_the_model_module_not_an_inlined_copy(self):
        import inspect
        src = inspect.getsource(self.inj._evaluate_catalogs_at_mjd)
        self.assertIn("SinusoidalLightCurve(", src)
        self.assertIn("GaussianLightCurve(", src)
        self.assertNotIn("np.sin(2 * np.pi", src,
                         "the sinusoid must come from injectionLightCurveModels, not a copy")

    def test_empty_catalogue_returns_five_empty_float_arrays(self):
        lst = self._write("empty", {})
        got = self.inj._evaluate_catalogs_at_mjd(lst, self.mjd, self.size, self.wcs, "F146")
        self.assertEqual([len(a) for a in got], [0, 0, 0, 0, 0])
        for a in got:
            self.assertEqual(a.dtype, np.float64)

    def test_unknown_type_raises_before_any_evaluation(self):
        sources = _catalogue(30)
        sources["0"]["type"] = "sawtooth"
        lst = self._write("bad", sources)
        with self.assertRaises(ValueError) as ctx:
            self.inj._evaluate_catalogs_at_mjd(lst, self.mjd, self.size, self.wcs, "F146")
        self.assertIn("sawtooth", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
