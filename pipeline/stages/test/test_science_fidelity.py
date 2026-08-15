"""
File:    test_science_fidelity.py

Regression tests for the science-fidelity defects the external implementation
review found: the extracted stage bodies were asserted to be the deleted
monolith's bodies, and in eight places they were not.

**These tests assert against real artefacts, not against restated expectations.**
A test that hardcodes "the flag is called `--scicat`" only proves this file and
`science.py` agree with each other; both were wrong together before. So:

* the SFFT argv test builds the real vector and feeds it to
  `sfft_rapid_rimtimsim`'s **own** `argparse` parser — the parser that rejected
  the first extraction's vocabulary with status 2;
* the statistics key tests read the key names out of
  `fits_data_statistics_with_clipping`'s **own** source, so a rename there
  fails these tests rather than silently reintroducing the `KeyError`;
* the sequence-order test asserts the ordering constraint that carries the
  science (statistics before injection), not a literal stage list;
* the config tests load the **real** release TOML and assert every key the
  stages read is present.

The stage modules import numpy, astropy, scipy, boto3, psycopg2, galsim,
romanisim and photutils at module scope; none are installed on the laptop and
the suite must stay discoverable there. The stub helper is the one
`test_sequences.py` uses, duplicated for the same reason it documents.
"""

import argparse
import ast
import re
import inspect
import os
import importlib.util
import sys
import types
import unittest


def _stub(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _module_or_stub(name):
    """The module under `name` — the stub if one was installed, else the
    real package, imported on demand. Written this way because the helper
    no longer shadows installed packages (W8): indexing sys.modules would
    raise KeyError for a real module nobody has imported yet."""
    if name in sys.modules:
        return sys.modules[name]
    import importlib
    return importlib.import_module(name)

def _install_third_party_stubs() -> None:
    """See `test_sequences.py` for the full rationale."""
    names = [
        "numpy", "numpy.ma",
        "scipy", "scipy.ndimage",
        "astropy", "astropy.io", "astropy.io.fits", "astropy.io.ascii",
        "astropy.table", "astropy.wcs", "astropy.coordinates",
        "astropy.units",
        "boto3", "botocore", "botocore.exceptions",
        "psycopg2", "psycopg2.extensions", "psycopg2.sql",
        "dateutil", "dateutil.tz",
        "galsim", "galsim.wcs", "galsim.roman",
        "romanisim", "romanisim.bandpass", "romanisim.catalog",
        "romanisim.image", "romanisim.psf",
        "photutils", "photutils.background", "photutils.segmentation",
        "injectionLightCurveModels",
    ]
    # Stub only what is genuinely MISSING, judged by importability rather
    # than by `sys.modules` membership (W8, 2026-08-06). In the image every
    # name here except injectionLightCurveModels is real, and shadowing a
    # real package with a bare ModuleType broke collection outright:
    # `from astropy.wcs import WCS` found a stub with no WCS, and a stub at
    # "numpy.ma" beneath the real numpy sent numpy 2.x's lazy __getattr__
    # into unbounded recursion. Off a laptop with none of these installed
    # the old form was fine, which is why it survived to be found here.
    for name in names:
        if name in sys.modules:
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                continue
        except (ImportError, ValueError):
            pass
        _stub(name)

    numpy = _module_or_stub("numpy")
    if not hasattr(numpy, "ma"):
        numpy.ma = _module_or_stub("numpy.ma")
        numpy.array = lambda *a, **k: None
        numpy.nanmedian = lambda *a, **k: 0.0
        numpy.nanmin = lambda *a, **k: 0.0
        numpy.nanmax = lambda *a, **k: 0.0
        numpy.isnan = lambda *a, **k: False

    astropy_io = _module_or_stub("astropy.io")
    if not hasattr(astropy_io, "fits"):
        astropy_io.fits = _module_or_stub("astropy.io.fits")
        astropy_io.ascii = _module_or_stub("astropy.io.ascii")

    astropy_table = _module_or_stub("astropy.table")
    if not hasattr(astropy_table, "QTable"):
        astropy_table.QTable = object
        astropy_table.Table = object
        astropy_table.join = lambda *a, **k: None


_install_third_party_stubs()

REPO_ROOT = os.path.dirname(  # .../rapid
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _load_release_toml() -> dict:
    """The real release-content science configuration."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        import tomli as tomllib
    path = os.path.join(REPO_ROOT, "cdf", "science", "pipeline.toml")
    with open(path, "rb") as handle:
        return tomllib.load(handle)


# ---------------------------------------------------------------------------
# Finding 7 — the SFFT invocation must parse
# ---------------------------------------------------------------------------

def _real_sfft_parser() -> argparse.ArgumentParser:
    """Rebuild SFFT's own parser from its source, without importing it.

    `modules/sfft/sfft_rapid_rimtimsim.py` imports the `sfft` package at module
    scope, which is not importable in the test environment. The parser is built
    inside `if __name__ == '__main__':` from a run of literal
    `parser.add_argument(...)` calls, so those calls are lifted out of the AST
    and replayed against a fresh `ArgumentParser`. What is under test is the
    real vocabulary as the real file declares it — a renamed or removed flag
    there fails this test.
    """
    path = os.path.join(REPO_ROOT, "modules", "sfft", "sfft_rapid_rimtimsim.py")
    with open(path) as handle:
        tree = ast.parse(handle.read())

    parser = argparse.ArgumentParser()
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "parser"):
            continue
        args = [ast.literal_eval(a) for a in node.args]
        kwargs = {}
        for keyword in node.keywords:
            if keyword.arg in ("help",):
                continue
            try:
                kwargs[keyword.arg] = ast.literal_eval(keyword.value)
            except ValueError:
                # `type=float` and friends: resolve the few names in use.
                if isinstance(keyword.value, ast.Name):
                    kwargs[keyword.arg] = {"float": float, "int": int,
                                           "str": str}[keyword.value.id]
                else:
                    raise
        parser.add_argument(*args, **kwargs)
        found += 1

    if found == 0:
        raise AssertionError(
            "no add_argument calls found in sfft_rapid_rimtimsim.py; this "
            "test's AST assumptions no longer hold")
    return parser


class _Facts:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Unit:
    key = "exposure1/sca1"

    def __init__(self, facts):
        self.facts = facts


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Workdir:
    def __init__(self, root="/scratch"):
        self.root = root

    def scratch(self, *parts):
        return os.path.join(self.root, *parts)

    def bundle_path(self, *parts):
        return os.path.join(self.root, "bundle", *parts)

    def tool_capture_path(self, name):
        return os.path.join(self.root, "capture", name)


def _context(products=None, science=None, facts=None, parameters=None):
    """A `StageContext` over stand-in collaborators. No I/O, no tools."""
    from pipeline.stages.context import StageContext

    return StageContext(
        workdir=_Workdir(),
        unit=_Unit(_Facts(**(facts or {}))),
        job_type="science",
        science=science if science is not None else _load_release_toml(),
        parameters=parameters or {"s3/products-bucket": "products",
                                  "s3/inputs-bucket": "inputs",
                                  # The key's leading component, on the
                                  # interim parameter path
                                  # (`StageContext.product_prefix`).
                                  "data/class": "real-pristine"},
        logger=_Logger(),
        products=dict(products or {}),
    )


class SfftArgvParsesTests(unittest.TestCase):
    """Finding 7. The first extraction's argv died in argparse with status 2.

    Monolith authority: `5664024^:pipeline/...SciencePipeline.py:1849-1892`.
    """

    def setUp(self):
        from pipeline.stages import science
        self.science = science
        self.parser = _real_sfft_parser()
        self.products = {
            "science_image_bkg_subbed": "/scratch/sci_bkgsub.fits",
            "gainmatched_reference_image": "/scratch/ref_gainmatched.fits",
            "science_psf_normalized": "/scratch/scipsf_normalized.fits",
            "reference_psf": "/scratch/refpsf.fits",
            "science_uncert_image": "/scratch/sci_unc.fits",
            "gainmatched_reference_uncert_image": "/scratch/ref_unc.fits",
            "science_gainmatch_sexcat": "/scratch/scigainmatch.txt",
            "reference_gainmatch_sexcat": "/scratch/refgainmatch.txt",
        }

    def _argv_tail(self, science_image, crossconv):
        context = _context(products=self.products)
        argv = self.science._sfft_argv(
            context, "/code/modules/sfft/sfft_rapid_rimtimsim.py",
            science_image, crossconv)
        # argv[0] is the interpreter and argv[1] the script; the parser sees
        # everything after them.
        return argv, argv[2:]

    def test_rimtimsim_argv_parses(self):
        """The "r"-prefixed branch: no catalogues, hard bright-source mask."""
        argv, tail = self._argv_tail("/scratch/rimtimsim_image.fits", False)
        args = self.parser.parse_args(tail)

        self.assertEqual(args.scifile, self.products["science_image_bkg_subbed"])
        self.assertEqual(args.reffile,
                         self.products["gainmatched_reference_image"])
        # The monolith's constants, lines 1857-1860.
        self.assertEqual(args.bsmaskvalue, 20000.0)
        self.assertEqual(args.bsmaskradius, 30.0)
        # This branch passes no catalogues (lines 1853-1860).
        self.assertIsNone(args.scicat)
        self.assertIsNone(args.refcat)
        # --scipsf is unconditional (lines 1882-1883).
        self.assertEqual(args.scipsf,
                         self.products["science_psf_normalized"])
        self.assertFalse(args.crossconv)

    def test_openuniverse_argv_parses_and_carries_catalogues(self):
        """The non-"r" branch: gain-match catalogues, gentle mask."""
        argv, tail = self._argv_tail("/scratch/openuniverse_image.fits", False)
        args = self.parser.parse_args(tail)

        # The monolith's constants, lines 1867-1878.
        self.assertEqual(args.scicat,
                         self.products["science_gainmatch_sexcat"])
        self.assertEqual(args.refcat,
                         self.products["reference_gainmatch_sexcat"])
        self.assertEqual(args.bsmaskvalue, 50.0)
        self.assertEqual(args.bsmaskradius, 100.0)

    def test_crossconv_argv_parses(self):
        """`--crossconv` is a store_true and brings three companions."""
        argv, tail = self._argv_tail("/scratch/openuniverse_image.fits", True)
        args = self.parser.parse_args(tail)

        self.assertTrue(args.crossconv)
        self.assertEqual(args.refpsf, self.products["reference_psf"])
        self.assertTrue(args.scisegm.endswith("sfftscisegm.fits"))
        self.assertTrue(args.refsegm.endswith("sfftrefsegm.fits"))

    def test_crossconv_is_never_given_a_value(self):
        """The first extraction emitted `--crossconv_flag False`.

        `--crossconv` is `action='store_true'`; a value after it would be
        consumed as a positional and blow the two-positional limit. This
        asserts the *absence* of the old shape directly.
        """
        _argv, tail = self._argv_tail("/scratch/openuniverse_image.fits", True)
        index = tail.index("--crossconv")
        following = tail[index + 1] if index + 1 < len(tail) else "--end"
        self.assertTrue(following.startswith("--"),
                        f"--crossconv was given the value {following!r}")

    def test_retired_flag_names_are_gone(self):
        """The invented vocabulary must not reappear under any branch."""
        retired = {"--sci_star_list", "--ref_star_list", "--crossconv_flag"}
        for image in ("/scratch/rimtimsim_image.fits",
                      "/scratch/openuniverse_image.fits"):
            for crossconv in (True, False):
                _argv, tail = self._argv_tail(image, crossconv)
                self.assertEqual(retired & set(tail), set(),
                                 f"retired SFFT flag in argv for {image}")

    def test_exactly_two_positionals(self):
        """Six positionals is what the first extraction passed; two is legal."""
        for image in ("/scratch/rimtimsim_image.fits",
                      "/scratch/openuniverse_image.fits"):
            for crossconv in (True, False):
                _argv, tail = self._argv_tail(image, crossconv)
                positionals = [token for i, token in enumerate(tail)
                               if not token.startswith("--")
                               and (i == 0 or not tail[i - 1].startswith("--")
                                    or tail[i - 1] in ("--crossconv",))]
                # Simpler and stricter: the real parser accepts it, and
                # argparse raises SystemExit on a third positional.
                self.parser.parse_args(tail)
                self.assertGreaterEqual(len(positionals), 2)


# ---------------------------------------------------------------------------
# Finding 8 — statistics key names
# ---------------------------------------------------------------------------

def _statistics_keys_from_source() -> set:
    """The key names `fits_data_statistics_with_clipping` actually sets.

    Read from the helper's own source rather than restated here, so this test
    tracks the helper: renaming a key there fails these tests instead of
    letting a stage reintroduce a `KeyError` in production.
    """
    path = os.path.join(REPO_ROOT, "modules", "utils", "rapid_pipeline_subs.py")
    with open(path) as handle:
        tree = ast.parse(handle.read())

    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "fits_data_statistics_with_clipping"):
            keys = set()
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Subscript)
                        and isinstance(sub.targets[0].value, ast.Name)
                        and sub.targets[0].value.id == "stats"):
                    keys.add(ast.literal_eval(sub.targets[0].slice))
            return keys
    raise AssertionError("fits_data_statistics_with_clipping not found")


def _statistics_lookups_in(module_path: str) -> set:
    """Every `stats*[...]` string subscript in one stage module."""
    with open(module_path) as handle:
        tree = ast.parse(handle.read())

    lookups = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id.startswith("stats")):
            try:
                lookups.add(ast.literal_eval(node.slice))
            except ValueError:
                continue
    return lookups


class InHouseCToolInvocationTests(unittest.TestCase):
    """No caller may reach for the retired `/code/c/bin` build tree.

    RAPID's eight in-house C binaries used to be compiled into
    `c/bin` inside the image by `c/builds/build_inside_container.sh`.
    They now ship in the `rapid-cmodules` RPM, which installs them to
    `/opt/rapid/bin` — the prefix the Containerfile's PATH owns, and the
    same way `swarp` and `sextractor` are found. The application image
    deliberately excludes `c/` from its source archive, so a caller using
    the old absolute path resolves to nothing at all.

    `bkgest` was such a caller, and the Q8 probe found it the expensive
    way: every science child died at `subtract_background` with "tool not
    found: '/code/c/bin/bkgest'" — one stage after the swarp fix let the
    pipeline reach it. `cforcepsfaper` had the identical bug waiting on
    the forced-photometry path.

    This walks the source rather than the two known sites, so the ninth
    caller fails here instead of on a ramp.
    """

    #: Everything rapid-cmodules installs (its %files list).
    CMODULES_BINARIES = ("bkgest", "cforcepsfaper", "computeOverlapArea",
                         "generateSmoothLampPattern", "hdrupdate",
                         "imheaders", "makeTestFitsFile", "verifyHduSums")

    def _sources(self):
        for sub in ("pipeline", "modules"):
            for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, sub)):
                if "/test" in root or root.endswith("/test"):
                    continue
                for name in files:
                    if name.endswith(".py"):
                        yield os.path.join(root, name)

    def test_no_caller_uses_the_retired_c_bin_path(self):
        offenders = []
        for path in self._sources():
            with open(path) as handle:
                for number, line in enumerate(handle, 1):
                    stripped = line.lstrip()
                    # Comments are skipped deliberately: the two fixed call
                    # sites explain the retired path by naming it, and a
                    # test that forbids describing the bug would force the
                    # explanation out of the code it belongs beside.
                    if stripped.startswith("#"):
                        continue
                    if "c/bin" in line:
                        offenders.append(
                            f"{os.path.relpath(path, REPO_ROOT)}:{number}")
        self.assertEqual(offenders, [],
                         "these call an in-house C binary through the "
                         "retired /code/c/bin tree; rapid-cmodules installs "
                         "them on PATH, so invoke them by bare name")

    def test_bkgest_is_invoked_by_bare_name(self):
        path = os.path.join(REPO_ROOT, "pipeline", "stages", "science.py")
        with open(path) as handle:
            source = handle.read()
        body = source.split("def subtract_background", 1)[1].split(
            "\ndef ", 1)[0]
        self.assertIn('run_tool(["bkgest"', body)

    def test_bkgest_is_pointed_at_its_installed_message_catalogue(self):
        """`-a` is the DIRECTORY holding bkgest's runtime message catalogue.

        Two wrong values in a row, so this asserts the right one rather than
        the absence of a wrong one. It was first passed
        `SOFTWARE_ROOT() + "/c/include"` — a path in no image and on no
        branch. That was then dropped as "an optional argument omitted by
        not passing it", which read the C correctly and the consequences
        wrongly: `bkgest_init_constants.c` defaults the path to `"."`, and
        `bkgest_log_writer.c` appends `/bkgest_errcodes.h` and fopen()s the
        result to decode its own status codes. The failure is not a missing
        annotation — it sets `I_status`, which becomes a non-zero EXIT. The
        Q9 cycle-3 probe saw bkgest compute a correct background and exit
        255 anyway.

        So the argument is load-bearing and the only acceptable value is the
        directory `rapid-cmodules` installs the catalogue into.
        """
        path = os.path.join(REPO_ROOT, "pipeline", "stages", "science.py")
        with open(path) as handle:
            source = handle.read()
        body = source.split("def subtract_background", 1)[1].split(
            "\ndef ", 1)[0]
        # Comments stripped: this asserts about what the stage RUNS, and the
        # lines explaining the two bad values necessarily name them.
        code = "\n".join(line for line in body.split("\n")
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("c/include", code)
        self.assertIn('"-a"', code)
        self.assertIn("CMODULES_SHARE", code)

    def test_the_catalogue_directory_is_under_the_rpm_prefix(self):
        """`CMODULES_SHARE` must name where the RPM actually installs.

        The constant and `rapid-cmodules.spec`'s `%files` are one fact in
        two repos, and nothing at runtime reconciles them — bkgest does not
        report a missing catalogue as anything but its own exit code. This
        pins the app-repo half; `rpms/smoke-test.sh` pins the package half
        by checking the file is on disk after a real install.

        Read from source rather than imported, like the rest of this class:
        `science.py` pulls in astropy at import, so importing it would make
        an assertion about a string literal require the whole science env,
        and skip everywhere that env is absent.
        """
        path = os.path.join(REPO_ROOT, "pipeline", "stages", "science.py")
        with open(path) as handle:
            source = handle.read()
        self.assertIn('CMODULES_SHARE = "/opt/rapid/share/bkgest"', source)


class StatisticsKeyNameTests(unittest.TestCase):
    """Finding 8. `clippedmed` and five others never existed.

    Monolith authority: `5664024^:...SciencePipeline.py:476,482,448-454`, which
    read `gmed`, `gsigma`, `gdatamin`, `gdatamax`, `satcount`, `nancount`.
    """

    def setUp(self):
        self.available = _statistics_keys_from_source()

    def test_helper_still_returns_the_expected_vocabulary(self):
        """A guard on the guard: the six names the stages depend on exist."""
        for key in ("clippedavg", "clippedstd", "nkept", "noutliers", "gmed",
                    "gsigma", "gdatamin", "gdatamax", "satcount", "nancount"):
            self.assertIn(key, self.available)

    def test_no_stage_looks_up_a_key_the_helper_does_not_set(self):
        for name in ("science.py", "reference_image.py"):
            path = os.path.join(REPO_ROOT, "pipeline", "stages", name)
            with self.subTest(module=name):
                unknown = _statistics_lookups_in(path) - self.available
                self.assertEqual(
                    unknown, set(),
                    f"{name} reads statistics keys that do not exist: "
                    f"{sorted(unknown)}")

    def test_the_retired_names_are_gone(self):
        """Named explicitly, because these six were the production KeyErrors."""
        retired = {"clippedmed", "datascale", "gmin", "gmax", "npixsat",
                   "npixnan"}
        for name in ("science.py", "reference_image.py"):
            path = os.path.join(REPO_ROOT, "pipeline", "stages", name)
            with self.subTest(module=name):
                self.assertEqual(_statistics_lookups_in(path) & retired, set())


# ---------------------------------------------------------------------------
# Findings 19 & 20 — variant-specific inputs, and catalogue identity
# ---------------------------------------------------------------------------

class PsfCatalogFilenameTests(unittest.TestCase):
    """Finding 20. Positive and negative catalogues collided on one filename.

    Monolith authority: `5664024^:...SciencePipeline.py:1551-1562` (ZOGY
    negative), `2286-2288` (SFFT negative), `2751-2753` (naive negative) — each
    deriving the negative names by `.replace(".txt", "_negative.txt")`.
    """

    def setUp(self):
        self.release = _load_release_toml()
        self.psfcat = self.release["psfcat_diffimage"]

    def test_release_content_carries_every_psfcat_filename(self):
        """The W4B migration dropped all nine; every one is read by name."""
        for prefix in ("zogy", "sfft", "naive"):
            for suffix in ("psfcat_filename", "psfcat_finder_filename",
                           "psfcat_residual_filename"):
                key = f"output_{prefix}_{suffix}"
                with self.subTest(key=key):
                    self.assertIn(key, self.psfcat)

    def test_reference_psfcat_filenames_present(self):
        """`generatePhotUtilsReferenceImageCatalog` reads these three."""
        psfcat_refimage = self.release["psfcat_refimage"]
        for key in ("output_psfcat_filename", "output_psfcat_finder_filename",
                    "output_psfcat_residual_filename"):
            with self.subTest(key=key):
                self.assertIn(key, psfcat_refimage)

    def test_positive_and_negative_names_differ_for_every_variant(self):
        """The defect directly: same prefix, same sign-less filename."""
        for prefix in ("zogy", "sfft", "naive"):
            positive = self.psfcat[f"output_{prefix}_psfcat_filename"]
            negative = positive.replace(".txt", "_negative.txt")
            with self.subTest(variant=prefix):
                self.assertNotEqual(positive, negative)
                self.assertTrue(negative.endswith("_negative.txt"))

    def test_stage_derives_negative_names_by_replacement(self):
        """The stage's own derivation, exercised without running photutils."""
        from pipeline.stages import science

        source = inspect.getsource(science.psf_catalog_for_difference_image)
        self.assertIn('replace(".txt", "_negative.txt")', source)
        self.assertIn('replace(".fits", "_negative.fits")', source)
        # And the signature must offer the switch at all.
        signature = inspect.signature(science.psf_catalog_for_difference_image)
        self.assertIn("negative", signature.parameters)


class PsfCatalogSchemaTests(unittest.TestCase):
    """Finding 20, second half. The catalogue schema was reduced.

    Monolith authority: `5664024^:...SciencePipeline.py:1500-1537` — sky
    coordinates added as `ra`/`dec`, a separate finder catalogue written, and
    photometry inner-joined with finder results on `id` before the parquet.
    """

    def setUp(self):
        from pipeline.stages import science
        self.source = inspect.getsource(
            science.psf_catalog_for_difference_image)

    def test_sky_coordinates_are_computed_and_added(self):
        self.assertIn("computeSkyCoordsFromPixelCoords", self.source)
        self.assertIn('name="ra"', self.source)
        self.assertIn('name="dec"', self.source)

    def test_finder_catalogue_is_written(self):
        self.assertIn("finder_results", self.source)
        self.assertIn("output_psfcat_finder_filename", self.source)

    def test_parquet_is_written_from_the_join_not_from_phot(self):
        self.assertIn('keys="id"', self.source)
        self.assertIn('join_type="inner"', self.source)
        self.assertIn("joined.to_pandas().to_parquet", self.source)
        # The defect was `phot.to_pandas().to_parquet(...)`.
        self.assertNotIn("phot.to_pandas().to_parquet", self.source)


class VariantSpecificInputTests(unittest.TestCase):
    """Finding 19. SFFT and naive silently used ZOGY's model.

    Monolith authority: `5664024^:...SciencePipeline.py:1972-1984` (SFFT's own
    uncertainty image), `2171` (SFFT's own difference PSF), `2031-2034` and
    `2084-2087` (SFFT detection image follows crossconv), `2469-2476` (naive
    cov-map masking), `2527-2539` (naive's own uncertainty image), `2651`/`2750`
    (naive fits with the reference PSF).
    """

    def setUp(self):
        from pipeline.stages import science
        self.science = science
        self.sfft_source = inspect.getsource(science.catalog_sfft)
        self.naive_source = inspect.getsource(science.naive_difference)

    def test_sfft_builds_its_own_uncertainty_image(self):
        self.assertIn("compute_diffimage_uncertainty", self.sfft_source)
        self.assertIn("sfftdiffimage_uncert_masked.fits", self.sfft_source)

    def test_sfft_uses_its_own_difference_psf(self):
        self.assertIn('context.product("sfft_diffpsf")', self.sfft_source)
        self.assertNotIn('context.product("zogy_diffpsf")', self.sfft_source)

    def test_sfft_does_not_borrow_zogy_uncertainty_or_scorr(self):
        for borrowed in ('zogy_diffimage_unc_masked', 'zogy_scorrimage_masked'):
            with self.subTest(product=borrowed):
                self.assertNotIn(borrowed, self.sfft_source)

    def test_sfft_detection_image_follows_the_crossconv_flag(self):
        """cconv image when cross-convolving, the SFFT difference when not."""
        self.assertIn("sfft_cconv_diffimage", self.sfft_source)
        self.assertIn("detection_positive", self.sfft_source)
        self.assertIn("detection_negative", self.sfft_source)

    def test_naive_masks_with_the_reference_coverage_map(self):
        self.assertIn("mask_difference_image_with_resampled_reference_cov_map",
                      self.naive_source)
        self.assertIn("resampled_reference_cov_map", self.naive_source)

    def test_naive_builds_its_own_uncertainty_image(self):
        self.assertIn("compute_diffimage_uncertainty", self.naive_source)
        self.assertIn("naive_diffimage_unc_masked", self.naive_source)

    def test_naive_fits_with_the_reference_psf(self):
        self.assertIn('context.product("reference_psf")', self.naive_source)
        self.assertNotIn('zogy_diffpsf', self.naive_source)

    def test_naive_detects_on_itself_not_on_zogy_scorr(self):
        self.assertNotIn("zogy_scorrimage_masked", self.naive_source)

    def test_sextractor_accepts_a_per_variant_weight_image(self):
        signature = inspect.signature(
            self.science.sextractor_on_difference_image)
        self.assertIn("weight_image", signature.parameters)


# ---------------------------------------------------------------------------
# Finding 21 — the average must precede injection
# ---------------------------------------------------------------------------

class InjectionOrderingTests(unittest.TestCase):
    """Finding 21. The clipped average was taken over injected pixels.

    Monolith authority: `5664024^:...SciencePipeline.py:788-798` computes
    `avg_sci_img`; the injection block opens at 806 and rebinds
    `science_image_filename` at 886; the reformat consumes the *pre-injection*
    average at 912.
    """

    def setUp(self):
        from pipeline.stages import sequences
        self.names = [name for name, _fn in sequences.SCIENCE_SEQUENCE]

    def test_statistics_stage_exists(self):
        self.assertIn("science_image_statistics", self.names)

    def test_statistics_runs_before_injection(self):
        self.assertLess(self.names.index("science_image_statistics"),
                        self.names.index("inject_fake_sources"),
                        "the clipped science average must be computed before "
                        "fake sources are injected, or injected pixels enter "
                        "the uncertainty model")

    def test_injection_runs_before_reformat(self):
        """The other half of the ordering: the reformat sees injected data."""
        self.assertLess(self.names.index("inject_fake_sources"),
                        self.names.index("reformat_science_image"))

    def test_reformat_consumes_the_produced_average(self):
        """It must read the product, not recompute over the current image."""
        from pipeline.stages import science

        source = inspect.getsource(science.reformat_science_image)
        self.assertIn('context.product("avg_sci_img")', source)
        self.assertNotIn("fits_data_statistics_with_clipping", source)

    def test_statistics_stage_reads_hdu_1(self):
        """The raw science image carries its data in the first extension."""
        from pipeline.stages import science

        source = inspect.getsource(science.science_image_statistics)
        self.assertIn("3.0, 1,", source)


# ---------------------------------------------------------------------------
# Finding 22 — the inline reference catalogue
# ---------------------------------------------------------------------------

class InlineReferenceCatalogTests(unittest.TestCase):
    """Finding 22. The inline build dropped the PhotUtils catalogue.

    Monolith authority: `5664024^:...SciencePipeline.py:487-535` calls
    `generateSExtractorReferenceImageCatalog` **and**
    `generatePhotUtilsReferenceImageCatalog`; the extraction kept only the
    first.
    """

    def setUp(self):
        from pipeline.stages import science
        self.source = inspect.getsource(science._build_reference_image)

    def test_both_reference_catalogues_are_generated(self):
        self.assertIn("generateSExtractorReferenceImageCatalog", self.source)
        self.assertIn("generatePhotUtilsReferenceImageCatalog", self.source)

    def test_the_psf_catalogue_products_are_recorded(self):
        self.assertIn('context.produce("reference_psfcat"', self.source)
        self.assertIn("reference_psfcat_checksum", self.source)

    def test_saturation_rate_uses_the_science_exposure_time(self):
        """Monolith line 549 divides by `exptime_sciimage`, not by 60.

        The dedicated reference-image pipeline divides by 60.0 (its own line
        428) and that stays; the two monoliths genuinely differ here and each
        is reproduced against its own authority.
        """
        self.assertIn('context.fact("exptime")', self.source)

    def test_dedicated_pipeline_still_divides_by_sixty(self):
        from pipeline.stages import reference_image

        source = inspect.getsource(reference_image.image_statistics)
        self.assertIn("/ 60.0", source)


# ---------------------------------------------------------------------------
# Finding 23 — the science PPID
# ---------------------------------------------------------------------------

class PostProcessPpidTests(unittest.TestCase):
    """Finding 23. The difference image was stamped with the reference PPID.

    Monolith authority: `5664024^:pipeline/...PostProcPipeline.py:158` reads a
    single `ppid` from `[SCI_IMAGE]` (value 15 in the master .ini, line 95) and
    stamps it into both headers at lines 238 and 297.
    """

    def test_science_ppid_is_fifteen(self):
        """The route matrix's own value, matching the master .ini."""
        from submission.routes import JOB_TYPE_SCIENCE, ppid_for

        self.assertEqual(ppid_for(JOB_TYPE_SCIENCE), 15)

    def test_both_stamps_use_the_science_ppid(self):
        from pipeline.stages import post_process

        self.assertEqual(post_process.SCIENCE_PPID, 15)
        for stage in (post_process.stamp_reference_image,
                      post_process.stamp_difference_image):
            with self.subTest(stage=stage.__name__):
                source = inspect.getsource(stage)
                self.assertIn("SCIENCE_PPID", source)
                self.assertNotIn("reference_image_ppid", source)

    def test_ppid_lands_in_the_ppid_keyword_slot(self):
        """Position matters: the value is stamped positionally by index.

        `addKeywordsToFITSHeader` pairs `keywords[i]` with `values[i]`, so a
        correct value in the wrong slot writes it to the wrong card.
        """
        from pipeline.stages import post_process

        for stage in (post_process.stamp_reference_image,
                      post_process.stamp_difference_image):
            source = inspect.getsource(stage)
            tree = ast.parse(source.strip())
            keywords = None
            values_length = None
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "keywords":
                    keywords = ast.literal_eval(node.value)
                if isinstance(node, ast.keyword) and node.arg == "values":
                    values_length = len(node.value.body.elts)
            with self.subTest(stage=stage.__name__):
                self.assertIsNotNone(keywords)
                self.assertIsNotNone(values_length)
                self.assertEqual(len(keywords), values_length,
                                 "keyword and value lists must be parallel")
                self.assertIn("PPID", keywords)


# ---------------------------------------------------------------------------
# Round-3 finding #7 — post-process published nothing, loudly reporting success
# ---------------------------------------------------------------------------

class PostProcessPublicationTests(unittest.TestCase):
    """The upload swallowed failures and nothing was ever published.

    `upload_products` called `util.upload_files_to_s3_bucket` and discarded its
    return. That helper does not raise — it returns a boolean nobody read,
    prints on failure, and `continue`s over a file it cannot find. And because
    nothing here ever called `publish_products` or `context.publish`,
    `context.published_products` stayed `{}`, `build_terminal_record`'s
    `if products:` guard was false, and the record carried no products key at
    all — while the entrypoint still closed the attempt (success, published).
    """

    def test_the_upload_goes_through_publish_products(self):
        from pipeline.stages import post_process

        source = inspect.getsource(post_process.upload_products)
        self.assertIn("publish_products", source)

        # The swallow itself: this helper must no longer be CALLED. Checked
        # against the parsed call names rather than the source text, because
        # the docstring names the old helper to explain what it did wrong —
        # a substring check would read the explanation as the defect.
        tree = ast.parse(source.strip())
        called = {ast.unparse(node.func) for node in ast.walk(tree)
                  if isinstance(node, ast.Call)}
        self.assertNotIn("util.upload_files_to_s3_bucket", called)
        self.assertIn("publish_products", called)

    def test_the_publishable_helper_selects_what_to_upload(self):
        # `context.publishable()` rather than an ad-hoc on-disk filter, so all
        # three upload stages answer "what is a published product" identically.
        from pipeline.stages import post_process

        source = inspect.getsource(post_process.upload_products)
        self.assertIn("context.publishable()", source)

    def test_s3objprf_names_the_prefix_the_bytes_land_under(self):
        """The header pointed at a prefix nothing was ever written to.

        S3OBJPRF was stamped as `f"{ctx.job_type}/{ctx.unit.key}"` — the OLD
        object-key shape — while the upload has been run- and attempt-scoped
        since review finding #18. The header is exactly what someone reads to
        find the object again, so the two must agree.
        """
        from pipeline.stages import post_process

        for stage in (post_process.stamp_reference_image,
                      post_process.stamp_difference_image):
            source = inspect.getsource(stage)
            tree = ast.parse(source.strip())
            keywords = values = None
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "keywords":
                    keywords = ast.literal_eval(node.value)
                if isinstance(node, ast.keyword) and node.arg == "values":
                    values = node.value.body.elts

            with self.subTest(stage=stage.__name__):
                slot = keywords.index("S3OBJPRF")
                expression = ast.unparse(values[slot])
                self.assertEqual("ctx.product_prefix()", expression)
                # The old shape must be GONE from the stamped values, not
                # merely joined by the new one. Checked over the parsed value
                # expressions rather than the source text so a docstring may
                # still name the defect it describes.
                stamped = {ast.unparse(value) for value in values}
                self.assertFalse(
                    [text for text in stamped if "unit.key" in text],
                    "the old job_type/unit key shape is still stamped")

    def test_a_publishing_upload_failure_reaches_the_caller(self):
        """`publish_products` raises; the old helper returned a boolean.

        Asserted against the real helper rather than the source text, because
        what matters is that a failed upload cannot be closed as a publication.
        """
        import os
        import tempfile

        from pipeline.runtime.errors import StorageError
        from pipeline.stages import post_process

        class RefusingS3:
            def put_object(self, **kwargs):
                raise RuntimeError("injected upload failure")

        class Ctx:
            job_type = "post-process"
            s3 = RefusingS3()
            logger = type("L", (), {"info": lambda *a, **k: None})()

            def __init__(self, path):
                self.products = {"difference_image": path}
                self.published_products = {}

            def parameter(self, name):
                return "products-bucket"

            def product_prefix(self):
                return "post-process/run-1/90000/7/attempt-1"

            def publishable(self):
                return [(n, v) for n, v in sorted(self.products.items())
                        if isinstance(v, str) and os.path.isfile(v)]

            def publish(self, *a, **k):
                raise AssertionError("must not publish after a failed upload")

            def record(self, **facts):
                raise AssertionError("must not record after a failed upload")

        with tempfile.NamedTemporaryFile(suffix=".fits") as handle:
            handle.write(b"stamped bytes")
            handle.flush()

            with self.assertRaises(StorageError):
                post_process.upload_products(Ctx(handle.name))


# ---------------------------------------------------------------------------
# Release-content completeness — every key the stages read must exist
# ---------------------------------------------------------------------------

class ReleaseContentCompletenessTests(unittest.TestCase):
    """The class of defect behind findings 8 and 20: keys read but not present.

    The W4B migration from the master `.ini` to `cdf/science/pipeline.toml`
    dropped thirteen keys that stage code still reads by name. Each would have
    been a `KeyError` at run time, in a stage that had already done real work.
    """

    def setUp(self):
        self.release = _load_release_toml()

    def _science_value_keys(self, module_name):
        """Every `(section, key)` a module reads through `science_value`."""
        path = os.path.join(REPO_ROOT, "pipeline", "stages", module_name)
        with open(path) as handle:
            tree = ast.parse(handle.read())

        pairs = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr == "science_value"):
                continue
            if len(node.args) < 2:
                continue
            try:
                pairs.add((ast.literal_eval(node.args[0]),
                           ast.literal_eval(node.args[1])))
            except ValueError:
                continue
        return pairs

    def test_every_science_value_key_exists_in_release_content(self):
        for module in ("science.py", "reference_image.py", "post_process.py"):
            for section, key in self._science_value_keys(module):
                with self.subTest(module=module, section=section, key=key):
                    self.assertIn(section, self.release)
                    self.assertIn(key, self.release[section])

    def test_naive_output_filename_present(self):
        self.assertIn("naive_output_diffimage_file",
                      self.release["naive_diffimage"])

    def _subscripted_keys(self, relative_path, dict_name):
        """Every literal key a module reads as `<dict_name>["..."]`.

        The `science_value` walk above only sees the STAGES. A section dict
        is also handed whole to the science helpers, which then subscript it
        directly — so a key dropped from release content is invisible to that
        walk and surfaces as a `KeyError` in a job instead.
        """
        path = os.path.join(REPO_ROOT, *relative_path.split("/"))
        with open(path) as handle:
            tree = ast.parse(handle.read())

        keys = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            value = node.value
            if not (isinstance(value, ast.Name) and value.id == dict_name):
                continue
            try:
                keys.add(ast.literal_eval(node.slice))
            except ValueError:
                continue
        return keys

    def test_every_awaicgen_key_the_coadd_reads_exists(self):
        """`generateReferenceImage` takes the `[awaicgen]` section whole.

        W9 found two more of the same W4B drop this class was written for:
        `awaicgen_input_images_list_file` and `awaicgen_input_uncert_list_file`
        were still in the master .ini and never carried into release content,
        so `build_reference_image` raised `KeyError` after downloading its PSF
        and its 48 coadd inputs — 36 seconds of real work before the failure.
        """
        for key in self._subscripted_keys(
                "pipeline/referenceImageSubs.py", "awaicgen_dict"):
            with self.subTest(key=key):
                self.assertIn(key, self.release["awaicgen"])

    # -- one call deeper -------------------------------------------------
    #
    # The two tests above walk the STAGE-BODY modules. Both sections are
    # then handed whole to a builder in `modules/utils/rapid_pipeline_subs.py`
    # that subscripts keys of its own, and those were invisible to this
    # class: the W9 ramp found `awaicgen_num_threads` and eleven sextractor
    # keys missing there, each of which would have cost one more live
    # attempt to discover. The command-line builders are where the real key
    # requirement lives, so that is what these walk.

    def _builder_keys(self, function: str, mapping: str) -> set:
        """Keys `function` subscripts out of its `mapping` argument.

        Text-matched rather than AST-walked because these builders spell the
        same key two ways — `d["sextractor_CATALOG_TYPE".lower()]` beside
        `d["awaicgen_num_threads"]` — and the `.lower()` form is a call
        expression that `_subscripted_keys`' `literal_eval` cannot read.
        """
        path = os.path.join(REPO_ROOT, "modules", "utils",
                            "rapid_pipeline_subs.py")
        with open(path) as handle:
            source = handle.read()
        body = source.split(f"def {function}", 1)[1].split("\ndef ", 1)[0]
        keys = set(re.findall(mapping + r'\["([^"]+)"\]', body))
        keys |= set(k.lower() for k in
                    re.findall(mapping + r'\["([^"]+)"\.lower\(\)\]', body))
        self.assertTrue(keys, f"no keys extracted from {function}")
        return keys

    def test_every_awaicgen_key_the_command_builder_reads_exists(self):
        """`build_awaicgen_command_line_args`, one call past the stage body.

        The four geometry keys are excluded because they are NOT release
        content: they are per-field and are filled in by
        `pipeline.mosaic_geometry.resolve_awaicgen_geometry` from the
        `tile_position` fact. `pipeline/test/test_mosaic_geometry.py` proves
        the resolved section is complete; this proves the rest of the section
        is declared.
        """
        computed = {"awaicgen_mosaic_size_x", "awaicgen_mosaic_size_y",
                    "awaicgen_RA_center", "awaicgen_Dec_center"}
        for key in self._builder_keys("build_awaicgen_command_line_args",
                                      "awaicgen_dict"):
            if key in computed:
                continue
            with self.subTest(key=key):
                self.assertIn(key, self.release["awaicgen"])

    def test_every_sextractor_key_the_command_builder_reads_exists(self):
        """`build_sextractor_command_line_args`, for all four sections.

        Eleven keys were missing from every `[sextractor_*]` section — the
        same W4B drop as the awaicgen ones. The first, `sextractor_catalog_type`,
        failed the W9 ramp's first step AFTER a 145-second coadd had
        succeeded; the other ten would have cost ten more steps.

        Seven keys are supplied at runtime by the stage body, not by release
        content — the input and output filenames, which are per-attempt paths
        and so are correctly absent here.
        """
        path = os.path.join(REPO_ROOT, "pipeline", "referenceImageSubs.py")
        with open(path) as handle:
            subs = handle.read()
        runtime = set(k.lower() for k in re.findall(
            r'sextractor_refimage_dict\["([^"]+)"\.lower\(\)\]\s*=', subs))
        self.assertTrue(runtime, "no runtime-supplied sextractor keys found")

        required = self._builder_keys("build_sextractor_command_line_args",
                                      "sextractor_dict")
        for section in ("sextractor_refimage", "sextractor_sciimage",
                        "sextractor_diffimage", "sextractor_gainmatch"):
            for key in required - runtime:
                with self.subTest(section=section, key=key):
                    self.assertIn(key, self.release[section])

    def test_every_swarp_key_the_command_builder_reads_exists(self):
        """`build_swarp_command_line_args`, the third builder of this class.

        Twenty keys were missing from `[swarp]` — the same W4B drop as the
        awaicgen and sextractor ones. `swarp_header_only` is only the
        builder's third read, which is why it stood in front of the other
        nineteen: it failed all 2,158 science attempts of the Q8 smoke run
        at `resample_reference_image` before any of them could show
        themselves.

        Three keys are per-attempt paths supplied by the stage body and so
        are correctly absent from release content. They are spelled out
        rather than derived, because deriving them — regexing every
        `swarp_dict[...] =` in the resample function, as the sextractor
        test derives its seven — would also capture `swarp_subtract_back`,
        `swarp_back_type` and `swarp_back_default`. Those three are
        OVERRIDES, assigned only after the first swarp call has already
        read them: release content must still carry them, and a derived
        exclusion set would quietly license their removal.
        """
        runtime_paths = {"swarp_input_image", "swarp_imageout_name",
                         "swarp_weightout_name"}
        for key in self._builder_keys("build_swarp_command_line_args",
                                      "swarp_dict"):
            if key in runtime_paths:
                continue
            with self.subTest(key=key):
                self.assertIn(key, self.release["swarp"])

    def test_every_command_line_builder_is_covered_by_this_class(self):
        """No builder may exist in the utils module without a completeness test.

        The three tests above each arrived reactively, after the builder in
        question had already failed a live attempt: awaicgen and sextractor
        from the W9 ramp, swarp from the Q8 smoke run. Each time the fix
        walked the builder that had just fired and stopped there, so the
        next uncovered builder was always one live failure away. This
        closes the enumeration instead: a new `build_*_command_line_args`
        fails here until it is given a test and listed below.
        """
        path = os.path.join(REPO_ROOT, "modules", "utils",
                            "rapid_pipeline_subs.py")
        with open(path) as handle:
            source = handle.read()
        builders = set(re.findall(r"def (build_\w+_command_line_args)\(",
                                  source))
        covered = {"build_awaicgen_command_line_args",
                   "build_sextractor_command_line_args",
                   "build_swarp_command_line_args"}
        self.assertEqual(builders, covered,
                         "a command-line builder has no completeness test; "
                         "add one beside the others and list it here")


class ObjectUrisComeFromTheManifest(unittest.TestCase):
    """No stage may derive one object's URI from another's by string surgery.

    `download_inputs` located the reference PSF as
    `reference_image_uri.replace("image.fits", "psf.fits")`, which turns
    `awaicgen_output_mosaic_image.fits` into
    `awaicgen_output_mosaic_psf.fits` — an object the reference-image job
    has never written. Its attempt directory holds the mosaic, the coverage
    map, the uncertainty image, two catalogues and the detector PSF, and no
    `*_mosaic_psf.fits` at all. Every science child therefore died at
    `run_zogy` with `FileNotFoundError`, five stages and one probe cycle
    after the configuration defects had been cleared out of the way.

    The manifest names every object a unit needs, and the dedicated
    reference-image job reads its PSF from `psf_uri` like anything else.
    Deriving a second name for the same thing is what let the two job
    types disagree about which file exists.

    This walks the stage sources for the shape rather than asserting about
    the one site that failed, so the next derived URI fails here instead of
    on a ramp.

    **Three sites match the shape and only one was a defect**, which is
    the distinction this test has to get right or it is noise.
    `resolve_reference_image` derives the coverage map, the uncertainty
    image and the SExtractor catalogue from the same mosaic URI, and all
    three objects DO exist beside the mosaic in the reference job's output
    (verified in the live bucket: `awaicgen_output_mosaic_cov_map.fits`,
    `..._uncert_image.fits`, `..._refimsexcat.txt`). Those derivations are
    a latent coupling — they will break the day the reference job renames
    an output — but they are not what failed, and asserting against them
    here would fail the suite for a defect nobody has. They are listed as
    known and allowed, so a NEW derived URI still fails, and the list
    itself is the record of the coupling.

    The PSF is not on that list because its derived name never named
    anything.
    """

    #: `<something>_uri.replace(` or `context.fact("...").replace(` —
    #: a URI that came from outside this process being rewritten into
    #: another URI.
    DERIVED_URI = re.compile(
        r"(?:\w*_uri|\bfact\([^)]*\))\s*\.replace\s*\(", re.MULTILINE)

    #: Substrings identifying derivations that resolve to objects the
    #: reference-image job really writes, verified against the live bucket
    #: rather than assumed. A fourth derived URI has to be justified the
    #: same way — by listing the object beside the mosaic — before it is
    #: added here.
    KNOWN_RESOLVING = (
        "os.path.basename(reference_uri), awaicgen[key]",
        '"image.fits", "refimsexcat.txt"',
    )

    def _sources(self):
        for sub in ("pipeline", "modules"):
            for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, sub)):
                if "/test" in root or root.endswith("/test"):
                    continue
                for name in files:
                    if name.endswith(".py"):
                        yield os.path.join(root, name)

    def test_no_stage_derives_an_object_uri_from_another(self):
        offenders = []
        for path in self._sources():
            with open(path) as handle:
                lines = handle.readlines()
            for number, line in enumerate(lines, start=1):
                if line.lstrip().startswith("#"):
                    continue
                if not self.DERIVED_URI.search(line):
                    continue
                # The call can wrap, and the argument that identifies it is
                # often on the next line, so the allowlist is checked
                # against the statement rather than the matched line.
                statement = "".join(lines[number - 1:number + 2])
                if any(known in statement for known in self.KNOWN_RESOLVING):
                    continue
                offenders.append(
                    f"{os.path.relpath(path, REPO_ROOT)}:{number}: "
                    f"{line.strip()}")
        self.assertEqual(
            offenders, [],
            "a stage derives one object's URI from another's by string "
            "replacement; the manifest names every object a unit needs, and "
            "a derived name can point at something nothing ever wrote:\n"
            + "\n".join(offenders))


class EveryStageConfigReadIsSatisfied(unittest.TestCase):
    """The whole class of dropped-key defects, not one builder at a time.

    The three builder tests above close the three command-line builders.
    `gain_match` is not one of them: it reads `[gainmatch]` directly in a
    stage body, so `verbose` and `upload_intermediate_products` were
    outside every existing test's reach and cost a fourth submission cycle
    to find (`q9_fix_round.rst`). Widening the enumeration by one test per
    defect is how the previous four were found, each one stage further
    down the same path.

    This asserts the property instead: **every science-configuration key
    the payload reads anywhere is either declared in the release file or
    assigned by the payload before use.** `scripts/audit_science_config_reads.py`
    resolves the section-to-variable binding structurally and propagates
    it to a fixed point, so a key read two calls past the accessor is
    covered without anyone naming that call. Exit status 0 is the gate.

    Run as a subprocess rather than imported because the audit parses the
    payload with `ast` and never imports it -- which is what lets it run
    on a laptop where numpy and friends are absent, the same constraint
    the stub helper at the top of this file exists for.
    """

    def test_no_science_config_key_is_read_without_being_provided(self):
        import subprocess

        script = os.path.join(REPO_ROOT, "scripts",
                              "audit_science_config_reads.py")
        self.assertTrue(os.path.exists(script),
                        f"the config audit is missing at {script}")

        completed = subprocess.run([sys.executable, script],
                                   capture_output=True, text=True)
        self.assertEqual(
            completed.returncode, 0,
            "the payload reads science-configuration keys that no "
            "configuration home provides:\n" + completed.stdout)


# ---------------------------------------------------------------------------
# Legacy-upload excision (catalog co-design Q8/X8)
# ---------------------------------------------------------------------------

class LegacyUploadExcisionTests(unittest.TestCase):
    """The legacy `<date>/jid<jid>/…` upload branches are gone.

    `referenceImageSubs.py` and `differenceImageSubs.py` built a second,
    non-attempt-scoped key shape and uploaded unconditionally under it — the
    one write path in the codebase outside create-once, so a retry could
    overwrite an earlier attempt's bytes (catalog co-design evidence pack
    §1.1, X8). The functions are still called for their science-computation
    side effects; only the upload side effects are excised, since every
    product they compute is published exactly once by the calling stage's
    `upload_products`, through `context.product_prefix()` and
    `publish_products`.

    Asserted against the parsed source, like `PostProcessPublicationTests`
    above: a docstring or comment is allowed to keep naming the old shape to
    explain what was removed, but no `Call` node may construct it.
    """

    LEGACY_KEY_MARKERS = ("jid", "upload_files_to_s3_bucket",
                          "upload_file", "put_object")

    def _module_source(self, relative_path):
        path = os.path.join(REPO_ROOT, *relative_path.split("/"))
        with open(path) as handle:
            return handle.read()

    def _call_names(self, source):
        tree = ast.parse(source)
        return {ast.unparse(node.func) for node in ast.walk(tree)
                if isinstance(node, ast.Call)}

    def test_referenceimagesubs_builds_no_jid_key(self):
        """No `jid`/`job_proc_date` string assembly survives in the file.

        The legacy key was built as
        ``job_proc_date + "/jid" + str(jid) + "/" + ...`` — a plain string
        join, not a call, so this is a substring search over non-comment
        lines rather than an AST call check.
        """
        source = self._module_source("pipeline/referenceImageSubs.py")
        offenders = [
            line for line in source.splitlines()
            if '"/jid"' in line and not line.lstrip().startswith("#")]
        self.assertEqual(
            offenders, [],
            "a jid-keyed string assembly survives the excision:\n"
            + "\n".join(offenders))

    def test_differenceimagesubs_builds_no_jid_key(self):
        source = self._module_source("pipeline/differenceImageSubs.py")
        offenders = [
            line for line in source.splitlines()
            if '"/jid"' in line and not line.lstrip().startswith("#")]
        self.assertEqual(
            offenders, [],
            "a jid-keyed string assembly survives the excision:\n"
            + "\n".join(offenders))

    def test_referenceimagesubs_calls_no_upload(self):
        """None of the three reference-image builders call an uploader.

        Checked against parsed call names, not source text, for the same
        reason `PostProcessPublicationTests` does it that way: the comments
        left behind to explain the excision name the old helpers, and a
        substring check would read the explanation as a live call.
        """
        source = self._module_source("pipeline/referenceImageSubs.py")
        called = self._call_names(source)
        offenders = {name for name in called
                    if any(marker in name for marker in
                           ("upload_file", "upload_files_to_s3_bucket",
                            "put_object"))}
        self.assertEqual(
            offenders, set(),
            f"referenceImageSubs.py still calls an uploader: {offenders}")

    def test_differenceimagesubs_calls_no_upload(self):
        source = self._module_source("pipeline/differenceImageSubs.py")
        called = self._call_names(source)
        offenders = {name for name in called
                    if any(marker in name for marker in
                           ("upload_file", "upload_files_to_s3_bucket",
                            "put_object"))}
        self.assertEqual(
            offenders, set(),
            f"differenceImageSubs.py still calls an uploader: {offenders}")

    def test_the_stage_callers_no_longer_pass_upload_flags(self):
        """The call sites dropped the dead `upload_to_s3_bucket` argument.

        A stray `True`/`upload_key_prefix=` positional at one of the four
        call sites would mean a signature drifted back toward the excised
        parameter rather than the calling stage's own
        `context.product_prefix()` / `publish_products` path.
        """
        for relative_path in ("pipeline/stages/reference_image.py",
                              "pipeline/stages/science.py"):
            source = self._module_source(relative_path)
            with self.subTest(module=relative_path):
                self.assertNotIn("upload_key_prefix", source)
                self.assertNotIn("upload_to_s3_bucket", source)


if __name__ == "__main__":
    unittest.main()
