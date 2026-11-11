"""SFFT's subtraction pass without its two kernel-phase stacks, the same pixels (memory-diet).

sfft 1.7.3's NumPy subtraction (``ElementalSFFTSubtract_Numpy.ESSN`` with a
solution given) fills ``Kab_Wla`` and ``Kab_Wmb``, ``(2 * KerHW + 1, N0, N1)``
complex128 each: with ``GKerHW = 9`` that is
2 x 19 x 255 MiB on 4089 x 4089, 9,695 MiB of the difference job's 15,005 MiB anon peak
(run 212, job 66156). ``Kab_Wla[a]`` is ``Wl ** a`` and ``Wl`` depends on the
row alone (``Wm``, the column alone), so here each stack is one column (one
row) of those powers, computed by the same expressions, and the library's
own compiled ``Construct_FDIFF`` reads it through a zero-stride view. Every
other step is the library's, in its order, through its compiled kernels;
the conjugate spectra, unused by subtraction, are not made. The solve pass
(``SFFTSolution`` None) is the library's own, untouched.

:func:`install` swaps it in for the subtraction pass only.
"""

import numpy as np
from numpy.lib.stride_tricks import as_strided
from sfft.sfftcore import SFFTSubtract

_ESSN = SFFTSubtract.ElementalSFFTSubtract_Numpy.ESSN


def subtract(PixA_I, PixA_J, SFFTConfig, SFFTSolution, NUM_CPU_THREADS_4SUBTRACT=8):
    """``ESSN(..., SFFTSolution=SFFTSolution, Subtract=True)``'s ``(Solution, PixA_DIFF)``."""
    import pyfftw
    pyfftw.config.NUM_THREADS = NUM_CPU_THREADS_4SUBTRACT
    pyfftw.interfaces.cache.enable()

    SFFTParam_dict, SFFTModule_dict = SFFTConfig
    N0, N1 = SFFTParam_dict['N0'], SFFTParam_dict['N1']
    w0, w1 = SFFTParam_dict['w0'], SFFTParam_dict['w1']
    DK, DB = SFFTParam_dict['DK'], SFFTParam_dict['DB']
    if PixA_I.shape != (N0, N1) or PixA_J.shape != (N0, N1):
        raise Exception('MeLOn ERROR: INCONSISTENT shape of input images I & J, [%d, %d] required!' % (N0, N1))
    L0, L1 = SFFTParam_dict['L0'], SFFTParam_dict['L1']
    Fab, Fij, Fpq = SFFTParam_dict['Fab'], SFFTParam_dict['Fij'], SFFTParam_dict['Fpq']
    SCALE, SCALE_L = SFFTParam_dict['SCALE'], SFFTParam_dict['SCALE_L']
    Fijab = SFFTParam_dict['Fijab']

    REF_pq = np.array([(p, q) for p in range(DB+1) for q in range(DB+1-p)]).astype(np.int32)
    REF_ij = np.array([(i, j) for i in range(DK+1) for j in range(DK+1-i)]).astype(np.int32)
    REF_ab = np.array([(a_pos-w0, b_pos-w1) for a_pos in range(L0) for b_pos in range(L1)]).astype(np.int32)
    SREF_ijab = np.array([(ij, ab) for ij in range(Fij) for ab in range(Fab)]).astype(np.int32)

    if not PixA_I.flags['C_CONTIGUOUS']:
        PixA_I = np.ascontiguousarray(PixA_I, np.float64)
    else:
        PixA_I = PixA_I.astype(np.float64)
    if not PixA_J.flags['C_CONTIGUOUS']:
        PixA_J = np.ascontiguousarray(PixA_J, np.float64)
    else:
        PixA_J = PixA_J.astype(np.float64)

    PixA_X = np.zeros((N0, N1), dtype=np.int32)
    PixA_Y = np.zeros((N0, N1), dtype=np.int32)
    PixA_CX = np.zeros((N0, N1), dtype=np.float64)
    PixA_CY = np.zeros((N0, N1), dtype=np.float64)
    SFFTModule_dict['SpatialCoor'](PixA_X=PixA_X, PixA_Y=PixA_Y, PixA_CX=PixA_CX, PixA_CY=PixA_CY)

    SPixA_Iij = np.zeros((Fij, N0, N1), dtype=np.float64)
    SPixA_Tpq = np.zeros((Fpq, N0, N1), dtype=np.float64)
    SFFTModule_dict['SpatialPoly'](REF_ij=REF_ij, REF_pq=REF_pq, PixA_CX=PixA_CX, PixA_CY=PixA_CY,
                                   PixA_I=PixA_I, SPixA_Iij=SPixA_Iij, SPixA_Tpq=SPixA_Tpq)
    del PixA_CX, PixA_CY

    _SPixA_FFT = np.empty((1 + Fij + Fpq, N0, N1), dtype=np.complex128)
    _SPixA_FFT[0, :, :] = PixA_J.astype(np.complex128)
    _SPixA_FFT[1: Fij+1, :, :] = SPixA_Iij.astype(np.complex128)
    _SPixA_FFT[Fij+1:, :, :] = SPixA_Tpq.astype(np.complex128)
    del PixA_I, PixA_J, SPixA_Iij, SPixA_Tpq
    _SPixA_FFT = SCALE * pyfftw.interfaces.numpy_fft.fft2(_SPixA_FFT)
    PixA_FJ = _SPixA_FFT[0, :, :]
    SPixA_FIij = _SPixA_FFT[1: Fij+1, :, :]
    SPixA_FTpq = _SPixA_FFT[Fij+1:, :, :]

    Solution = SFFTSolution.astype(np.float64)
    a_ijab = Solution[: Fijab]
    b_pq = Solution[Fijab:]

    # Wl's row and Wm's column, by the library's expressions on the same coordinates.
    Wl = np.exp((-2j*np.pi/N0) * PixA_X[:, :1].astype(np.float64))     # (N0, 1)
    Wm = np.exp((-2j*np.pi/N1) * PixA_Y[:1, :].astype(np.float64))     # (1, N1)
    del PixA_X, PixA_Y
    Kab_Wla_column = np.empty((L0, N0, 1), dtype=np.complex128)
    Kab_Wmb_row = np.empty((L1, 1, N1), dtype=np.complex128)
    if w0 == w1:
        wx = w0
        for aob in range(-wx, wx+1):
            Kab_Wla_column[aob + wx] = Wl ** aob
            Kab_Wmb_row[aob + wx] = Wm ** aob
    else:
        for a in range(-w0, w0+1):
            Kab_Wla_column[a + w0] = Wl ** a
        for b in range(-w1, w1+1):
            Kab_Wmb_row[b + w1] = Wm ** b
    s = Kab_Wla_column.strides
    Kab_Wla = as_strided(Kab_Wla_column, shape=(L0, N0, N1), strides=(s[0], s[1], 0))
    s = Kab_Wmb_row.strides
    Kab_Wmb = as_strided(Kab_Wmb_row, shape=(L1, N0, N1), strides=(s[0], 0, s[2]))

    PixA_FDIFF = np.empty((N0, N1), dtype=np.complex128)
    PixA_FDIFF = SFFTModule_dict['Construct_FDIFF'](
        SREF_ijab=SREF_ijab, REF_ab=REF_ab, a_ijab=a_ijab.astype(np.complex128),
        SPixA_FIij=SPixA_FIij, Kab_Wla=Kab_Wla, Kab_Wmb=Kab_Wmb, b_pq=b_pq.astype(np.complex128),
        SPixA_FTpq=SPixA_FTpq, PixA_FJ=PixA_FJ, PixA_FDIFF=PixA_FDIFF)
    del _SPixA_FFT, PixA_FJ, SPixA_FIij, SPixA_FTpq
    PixA_DIFF = np.empty_like(PixA_FDIFF)
    PixA_DIFF[:, :] = SCALE_L * pyfftw.interfaces.numpy_fft.ifft2(PixA_FDIFF)
    PixA_DIFF = PixA_DIFF.real
    return Solution, PixA_DIFF


def _essn(PixA_I, PixA_J, SFFTConfig, SFFTSolution=None, Subtract=False, NUM_CPU_THREADS_4SUBTRACT=8,
          VERBOSE_LEVEL=2):
    if SFFTSolution is None or not Subtract:
        return _ESSN(PixA_I, PixA_J, SFFTConfig, SFFTSolution=SFFTSolution, Subtract=Subtract,
                     NUM_CPU_THREADS_4SUBTRACT=NUM_CPU_THREADS_4SUBTRACT, VERBOSE_LEVEL=VERBOSE_LEVEL)
    return subtract(PixA_I, PixA_J, SFFTConfig, SFFTSolution, NUM_CPU_THREADS_4SUBTRACT)


def install():
    """Route sfft's NumPy subtraction pass through :func:`subtract`; the solve pass stays the library's."""
    SFFTSubtract.ElementalSFFTSubtract_Numpy.ESSN = staticmethod(_essn)
