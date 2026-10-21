"""scripts/read_packets.py's cutout check: size and the stated edge fill.

The field's real packets hold no stamp that reaches off the chip, so the
edge-fill contract is exercised here, on stamps cut by the packet stage's
own ``extract_stamp`` from a small synthetic chip.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import numpy as np
from astropy.io import fits

from rapidpipe.science.alerts.cutouts import extract_stamp

_SPEC = importlib.util.spec_from_file_location(
    "read_packets", Path(__file__).resolve().parents[2] / "scripts" / "read_packets.py")
read_packets = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(read_packets)

CHIP = np.arange(1.0, 41.0 * 50.0 + 1.0, dtype=np.float32).reshape(41, 50)  # no zero pixel


def test_a_stamp_at_the_chip_corner_passes_and_reports_the_edge():
    stamp = extract_stamp(CHIP, 1.0, 1.0, half_width=4)  # 1-based pixel (1, 1)
    assert read_packets.check_cutout(stamp, 9, 0, 0, CHIP.shape) == (None, True)


def test_an_interior_stamp_passes_without_an_edge():
    stamp = extract_stamp(CHIP, 20.0, 20.0, half_width=4)
    assert read_packets.check_cutout(stamp, 9, 19, 19, CHIP.shape) == (None, False)


def test_off_chip_pixels_that_are_not_the_fill_fail():
    data = np.ones((9, 9), dtype=np.float32)
    buffer = io.BytesIO()
    fits.PrimaryHDU(data=data).writeto(buffer)
    failure, at_edge = read_packets.check_cutout(buffer.getvalue(), 9, 0, 0, CHIP.shape)
    assert at_edge and failure == "56 off-chip pixels not 0.0"


def test_a_stamp_of_the_wrong_size_or_none_fails():
    stamp = extract_stamp(CHIP, 20.0, 20.0, half_width=3)
    assert read_packets.check_cutout(stamp, 9, 19, 19, CHIP.shape)[0] == "shape (7, 7), expected (9, 9)"
    assert read_packets.check_cutout(None, 9, 19, 19, CHIP.shape)[0] == "missing"
