"""modules/sfft/sfft_lowmem.py: SFFT's subtraction pass gives the library's pixels, bit for bit."""

import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("sfft")
pytest.importorskip("numba")
pytest.importorskip("pyfftw")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "modules" / "sfft"))
import sfft_lowmem  # noqa: E402
from sfft.sfftcore.SFFTConfigure import SingleSFFTConfigure  # noqa: E402


@pytest.mark.parametrize("shape", [(64, 64), (72, 56)])
def test_subtraction_pass_equals_the_librarys_bit_for_bit(shape):
    rng = np.random.default_rng(7)
    config = SingleSFFTConfigure.SSC(NX=shape[0], NY=shape[1], KerHW=4, KerPolyOrder=0, BGPolyOrder=0,
                                     ConstPhotRatio=True, BACKEND_4SUBTRACT="Numpy",
                                     NUM_CPU_THREADS_4SUBTRACT=1, NUMBA_CACHE=False, VERBOSE_LEVEL=0)
    image_i, image_j = rng.normal(size=shape), rng.normal(size=shape)
    solution = rng.normal(size=config[0]["NEQ"])
    expected = sfft_lowmem._ESSN(image_i, image_j, config, SFFTSolution=solution, Subtract=True,
                                 NUM_CPU_THREADS_4SUBTRACT=1, VERBOSE_LEVEL=0)[1]
    got = sfft_lowmem.subtract(image_i, image_j, config, solution, NUM_CPU_THREADS_4SUBTRACT=1)[1]
    assert got.tobytes() == expected.tobytes()
