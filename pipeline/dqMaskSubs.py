"""
Masking of Roman WFI L2 pixels by their data-quality flags.

The L2 DQ array marks pixels the ramp fit could not, or should not, be trusted on.  Nothing
downstream of ingest reads the DQ array, so the flagged pixels are set to NaN in the science
image (and in its uncertainty planes) at ingest, where every later step sees them as missing.

The default mask is

    DO_NOT_USE | SATURATED | DEAD | HOT | NONLINEAR | NO_LIN_CORR | UNRELIABLE_FLAT

JUMP_DET is deliberately left out.  On guided commissioning data it flags 0.2-0.3% of pixels,
concentrated on stars and recurring at the same pixel frame to frame, and the L2 ramp fit has
already fitted around the jumps; masking it would punch NaN holes in stars.  GW_AFFECTED_DATA
is left out as well: on its own it marks the full-height guide-window column stripe, which is
statistically indistinguishable from clean pixels, while the guide-window box itself also
carries DO_NOT_USE and is masked through that bit.
"""

import numpy as np


default_dq_mask_bits = 0x2110C03


def find_dq_masked_pixels(dq,mask_bits=default_dq_mask_bits):

    '''
    Return a boolean array, True where any of mask_bits is set in the DQ array.
    '''

    if not 0 <= mask_bits < 2**32:
        raise ValueError(f"mask_bits = {mask_bits} is not a 32-bit unsigned value")

    dq = np.asarray(dq)

    if not np.issubdtype(dq.dtype,np.integer):
        raise TypeError(f"DQ array has non-integer dtype {dq.dtype}")

    return (dq.astype(np.uint32,copy=False) & np.uint32(mask_bits)) != 0


def apply_dq_mask(data,masked):

    '''
    Set the masked pixels of a floating-point array to NaN, in place, and return how many
    there were.
    '''

    if not np.issubdtype(data.dtype,np.floating):
        raise TypeError(f"Cannot write NaN into an array of dtype {data.dtype}")

    if data.shape != masked.shape:
        raise ValueError(f"Array shape {data.shape} does not match mask shape {masked.shape}")

    data[masked] = np.nan

    return int(np.count_nonzero(masked))
