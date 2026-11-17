# -*- coding: utf-8 -*-
"""Drift-corrected averaging of repeated frames of the same area (pure numpy/scipy, no IO).

Used by ``AverageFramesDriftCorrected``.  Pipeline: row-median + plane flatten ->
registration of every image to the first one -> Fourier shift -> average of the
overlap, cropped to the rectangle every image covers.

## Why a *windowed, plain, range-limited* cross-correlation

On a periodic lattice the cross-correlation of two frames has an (almost) equal peak at
every lattice translation.  A whitened phase correlation
(``skimage.registration.phase_cross_correlation``) flattens the amplitude spectrum, so
those peaks are *even more* equal and the global maximum is decided by noise, so the
returned shift can be far larger than the true drift between the frames.  Averaging with such shifts smears the lattice or, worse,
produces a clean-looking lattice that is a different registration than the real one.

So: (1) Hann-window both images (no wrap-around edge artefact), (2) plain -- not
whitened -- cross-correlation, (3) search the maximum ONLY inside ``|shift| <= rmax_px``
(drift between consecutive frames of one area is small; a lattice-translation alias
is outside that window), (4) parabolic sub-pixel refinement of that peak.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

__all__ = ["flatten", "register_shift", "fourier_shifted", "valid_mask",
           "common_crop", "average_registered", "AverageResult"]


def flatten(img: np.ndarray) -> np.ndarray:
    """Row-median subtraction followed by a least-squares plane fit and removal."""
    z = np.asarray(img, dtype=float)
    z = z - np.median(z, axis=1, keepdims=True)
    ny, nx = z.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    A = np.column_stack([np.ones(z.size), xx.ravel() / max(nx, 1), yy.ravel() / max(ny, 1)])
    coef, *_ = np.linalg.lstsq(A, z.ravel(), rcond=None)
    return z - (A @ coef).reshape(ny, nx)


def _parabolic(cm: float, c0: float, cp: float) -> float:
    d = cm - 2.0 * c0 + cp
    return 0.0 if d == 0 else float(0.5 * (cm - cp) / d)


def register_shift(ref: np.ndarray, img: np.ndarray, rmax_px: int = 20) -> np.ndarray:
    """Shift ``(dy, dx)`` in px that moves ``img`` onto ``ref`` (apply with a Fourier shift).

    Plain Hann-windowed cross-correlation, peak searched only within
    ``|shift| <= rmax_px`` (see the module docstring), parabolic sub-pixel refinement.
    """
    ref = np.asarray(ref, dtype=float)
    img = np.asarray(img, dtype=float)
    if ref.shape != img.shape:
        raise ValueError("image shapes differ: %s vs %s" % (ref.shape, img.shape))
    ny, nx = ref.shape
    w = np.outer(np.hanning(ny), np.hanning(nx))
    F = np.fft.fft2((ref - ref.mean()) * w) * np.conj(np.fft.fft2((img - img.mean()) * w))
    c = np.fft.fftshift(np.real(np.fft.ifft2(F)))
    cy, cx = ny // 2, nx // 2
    ry = int(max(0, min(rmax_px, cy - 2, ny - cy - 3)))
    rx = int(max(0, min(rmax_px, cx - 2, nx - cx - 3)))
    win = c[cy - ry:cy + ry + 1, cx - rx:cx + rx + 1]
    iy, ix = np.unravel_index(int(np.argmax(win)), win.shape)
    iy += cy - ry
    ix += cx - rx
    dy = iy + _parabolic(c[iy - 1, ix], c[iy, ix], c[iy + 1, ix]) - cy
    dx = ix + _parabolic(c[iy, ix - 1], c[iy, ix], c[iy, ix + 1]) - cx
    return np.array([dy, dx], dtype=float)


def fourier_shifted(img: np.ndarray, shift: np.ndarray) -> np.ndarray:
    """``img`` translated by ``shift`` (dy, dx) with Fourier interpolation (wraps around)."""
    from scipy.ndimage import fourier_shift
    return np.real(np.fft.ifftn(fourier_shift(np.fft.fftn(img), tuple(shift))))


def valid_mask(shape, shift) -> np.ndarray:
    """1 where a Fourier-shifted image is real data, 0 on the edge that wrapped around.

    Only the side the content moved *away from* is invalid: shift > 0 -> leading rows /
    columns, shift < 0 -> trailing ones.
    """
    ny, nx = shape
    m = np.ones((ny, nx))
    iy, ix = int(np.ceil(abs(shift[0]))), int(np.ceil(abs(shift[1])))
    if iy:
        if shift[0] > 0:
            m[:iy, :] = 0
        else:
            m[ny - iy:, :] = 0
    if ix:
        if shift[1] > 0:
            m[:, :ix] = 0
        else:
            m[:, nx - ix:] = 0
    return m


def common_crop(shape, shifts) -> tuple[int, int, int, int]:
    """``(y0, y1, x0, x1)``: the rectangle valid in every shifted image."""
    ny, nx = shape
    sy = [s[0] for s in shifts]
    sx = [s[1] for s in shifts]
    y0 = int(np.ceil(max([v for v in sy if v > 0], default=0)))
    y1 = ny - int(np.ceil(max([-v for v in sy if v < 0], default=0)))
    x0 = int(np.ceil(max([v for v in sx if v > 0], default=0)))
    x1 = nx - int(np.ceil(max([-v for v in sx if v < 0], default=0)))
    return y0, y1, x0, x1


@dataclass
class AverageResult:
    average: np.ndarray                 # cropped to the common rectangle
    reference: np.ndarray               # first (flattened) image, same crop
    shifts: list = field(default_factory=list)   # [[dy, dx], ...] in px, one per image
    crop: tuple = (0, 0, 0, 0)
    n_images: int = 0


def average_registered(images, rmax_px: int = 20, flatten_first: bool = True) -> AverageResult:
    """Flatten, register to ``images[0]``, Fourier-shift, average the overlap, crop.

    Raises ``ValueError`` for an empty list or images of different sizes (an average over
    frames of different pixel grids would silently be an average over different areas).
    """
    imgs = [np.asarray(i, dtype=float) for i in images]
    if not imgs:
        raise ValueError("no images")
    shape = imgs[0].shape
    if any(i.shape != shape for i in imgs):
        raise ValueError("image sizes differ: %s" % sorted({i.shape for i in imgs}))
    if flatten_first:
        imgs = [flatten(i) for i in imgs]
    ref = imgs[0]
    acc = np.zeros(shape)
    cnt = np.zeros(shape)
    shifts = []
    for z in imgs:
        sh = register_shift(ref, z, rmax_px)
        shifts.append([round(float(sh[0]), 3), round(float(sh[1]), 3)])
        m = valid_mask(shape, sh)
        acc += fourier_shifted(z, sh) * m
        cnt += m
    avg = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)
    y0, y1, x0, x1 = common_crop(shape, shifts)
    return AverageResult(average=avg[y0:y1, x0:x1], reference=ref[y0:y1, x0:x1],
                         shifts=shifts, crop=(y0, y1, x0, x1), n_images=len(imgs))
