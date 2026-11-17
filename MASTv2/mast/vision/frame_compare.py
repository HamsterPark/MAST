# -*- coding: utf-8 -*-
"""Before/after comparison of one scanned area for a *local* change (pure numpy/scipy, no IO).

Used by ``CompareFramesLocalChange``.  Question answered: after something was done inside a box
(a tip-field "write", a pulse, ...), does the box region differ from the rest of the frame
by more than two unmanipulated frames would?

Pipeline: row-median + plane flatten -> register AFTER onto BEFORE on *low-passed* images ->
Fourier shift, crop to the overlap -> difference -> rms inside vs outside the box.

## Why the registration runs on low-passed images, in a bounded window

A frame of a crystal is dominated by its lattice.  Cross-correlation of two lattice images has
an (almost) equal peak at every lattice translation; a whitened phase correlation
(``skimage.registration.phase_cross_correlation``) makes those peaks even more equal and the
global maximum is then decided by noise.  On lattice images it can
return shifts of tens of pixels where the true drift is only a few pixels.  A *small* frame has
very few non-lattice features, so the aliasing is worst exactly there.

So the images are Gaussian low-passed (``lp_nm``, default 0.4 nm, i.e. about one lattice
period) which removes the lattice, a broad background is subtracted, and the plain
(not whitened) normalised cross-correlation over the overlap is searched only inside ``|shift| <= window_px``.
What remains to register on are the lattice-free features: defects, adsorbates, step edges,
slow height variations.  Sub-pixel refinement is a parabola through the integer peak whose
offset is clamped to +-0.5 px and skipped when the peak sits on the window edge.
If the frames contain nothing but the lattice, the shift is only defined modulo a lattice
vector; that case is detected (``feature_ratio`` tiny) and flagged, not silently resolved.

## What the numbers mean

``rms_in / rms_out`` of the difference is large for a box-shaped change.  A change of the
whole tip state (contrast, resolution) shows up everywhere, so the ratio stays near 1.  That is
the point of the box test.  Still, two unmanipulated frames also differ (drift residue, tip
changes); a candidate must exceed that *null* ratio measured with the same box on two frames
that were not written.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

__all__ = ["flatten", "resample_to", "register_lowpassed", "fourier_shifted",
           "compare_local", "box_mask", "RegResult", "CompareResult",
           "RATIO_CANDIDATE", "NULL_MARGIN", "NCC_WEAK", "FEATURE_RATIO_MIN"]

#: rms_in/rms_out above which the difference is box-shaped enough to be worth a look
RATIO_CANDIDATE = 2.0
#: with a null pair given, the ratio must also exceed NULL_MARGIN * null ratio
NULL_MARGIN = 1.5
#: normalised cross-correlation at the registration peak below which the shift is untrusted
NCC_WEAK = 0.2
#: rms of the band-passed image relative to the flattened one below which only the lattice is there
FEATURE_RATIO_MIN = 0.05


def flatten(img) -> np.ndarray:
    """Row-median subtraction followed by a least-squares plane removal."""
    z = np.asarray(img, dtype=float)
    z = z - np.median(z, axis=1, keepdims=True)
    ny, nx = z.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    A = np.column_stack([np.ones(z.size), xx.ravel() / max(nx, 1), yy.ravel() / max(ny, 1)])
    coef, *_ = np.linalg.lstsq(A, z.ravel(), rcond=None)
    return z - (A @ coef).reshape(ny, nx)


def resample_to(img, shape) -> np.ndarray:
    """Cubic resampling of ``img`` onto ``shape`` (same physical area, different pixel count)."""
    from scipy.ndimage import zoom
    img = np.asarray(img, dtype=float)
    if img.shape == tuple(shape):
        return img
    return zoom(img, (shape[0] / img.shape[0], shape[1] / img.shape[1]), order=3)


def fourier_shifted(img, shift) -> np.ndarray:
    """``img`` translated by ``shift`` (dy, dx) px with Fourier interpolation (wraps around)."""
    from scipy.ndimage import fourier_shift
    return np.real(np.fft.ifftn(fourier_shift(np.fft.fftn(img), tuple(shift))))


def _parabolic(cm: float, c0: float, cp: float) -> float:
    """Offset of the peak from the middle sample (Gaussian peak: parabola through the logs)."""
    if min(cm, c0, cp) > 1e-6:
        cm, c0, cp = np.log(cm), np.log(c0), np.log(cp)
    d = cm - 2.0 * c0 + cp
    return 0.0 if d == 0 else float(np.clip(0.5 * (cm - cp) / d, -0.5, 0.5))


@dataclass
class RegResult:
    shift: np.ndarray                    # (dy, dx) px; apply with fourier_shifted(after, shift)
    ncc: float = 0.0                     # normalised correlation at the peak
    at_edge: bool = False                # peak on the edge of the search window
    feature_ratio: float = 1.0           # rms(band-passed) / rms(flattened), mean of both images
    shift_lowpass: np.ndarray = field(default_factory=lambda: np.zeros(2))   # before the outside-box fine tuning
    refined_px: np.ndarray = field(default_factory=lambda: np.zeros(2))      # the fine-tuning that was applied
    warnings: list = field(default_factory=list)


def register_lowpassed(ref, img, nm_per_px: float, lp_nm: float = 0.4, bg_nm: float = 3.0,
                       window_px: int = 20, refine_passes: int = 2, exclude=None) -> RegResult:
    """Shift moving ``img`` onto ``ref``; see the module docstring for why low-passed + bounded.

    ``refine_passes`` extra passes re-register the already shifted image (the residual is small,
    which removes the edge-induced bias that grows with the shift) and add the residual, which
    stays inside the +-0.5 px clamp.  ``exclude`` (bool array, True = leave out) removes the region
    that is expected to have changed (the box) from the correlation, so the change cannot pull the
    registration towards itself.  The hole is in the same place in both images only once AFTER has
    been moved onto BEFORE, so the first pass needs a margin around the box that covers the drift
    (``exclude_margin_nm`` in :func:`compare_local`); the refinement passes then work with aligned holes.
    """
    res = _register_once(ref, img, nm_per_px, lp_nm, bg_nm, window_px, exclude)
    for _ in range(max(0, int(refine_passes))):
        if res.at_edge:
            break
        r2 = _register_once(ref, fourier_shifted(np.asarray(img, dtype=float), res.shift),
                            nm_per_px, lp_nm, bg_nm, window_px, exclude)
        if np.abs(r2.shift).max() > 1.0:       # a residual that large means the first pass was not a good peak
            break
        res.shift = res.shift + r2.shift
        res.ncc = max(res.ncc, r2.ncc)
    return res


def _register_once(ref, img, nm_per_px, lp_nm, bg_nm, window_px, exclude=None) -> RegResult:
    from scipy.ndimage import gaussian_filter
    ref = np.asarray(ref, dtype=float)
    img = np.asarray(img, dtype=float)
    if ref.shape != img.shape:
        raise ValueError("image shapes differ: %s vs %s" % (ref.shape, img.shape))
    ny, nx = ref.shape
    lp = max(lp_nm / nm_per_px, 0.0)
    bg = min(bg_nm, 0.25 * min(ny, nx) * nm_per_px) / nm_per_px

    def band(z):
        if lp > 0:
            z = gaussian_filter(z, lp)
        return z - gaussian_filter(z, bg)

    a, b = band(ref), band(img)
    fr = [float(band(z).std() / max(z.std(), 1e-30)) for z in (ref, img)]
    res = RegResult(shift=np.zeros(2), feature_ratio=float(np.mean(fr)))
    # Normalised cross-correlation over the overlap at every lag (local means removed), computed with
    # zero-padded FFTs.  Neither a Hann window nor a global-mean/overlap-area normalisation will do:
    # both bias the peak towards zero shift (by ~2 px at 5 px drift on a 128 px frame) because the
    # content that slides out of the overlap is not zero-mean.
    P = (2 * ny, 2 * nx)
    m = np.ones_like(a) if exclude is None else (~np.asarray(exclude, dtype=bool)).astype(float)
    a, b = a * m, b * m
    F = lambda z: np.fft.fft2(z, P)                        # noqa: E731
    X = lambda fa, fb: np.fft.fftshift(np.real(np.fft.ifft2(fa * np.conj(fb))))   # noqa: E731
    fa, fb, fo = F(a), F(b), F(m)
    cy, cx = ny, nx                                        # zero lag after fftshift
    ry = int(max(1, min(window_px, ny // 4)))
    rx = int(max(1, min(window_px, nx // 4)))
    sl = (slice(cy - ry, cy + ry + 1), slice(cx - rx, cx + rx + 1))
    ly = np.arange(-ry, ry + 1)
    lx = np.arange(-rx, rx + 1)
    n = np.maximum(X(fo, fo)[sl], 1.0)
    sab, sa, sb = X(fa, fb)[sl], X(fa, fo)[sl], X(fo, fb)[sl]
    saa, sbb = X(F(a * a), fo)[sl], X(fo, F(b * b))[sl]
    num = sab - sa * sb / n
    den = np.sqrt(np.maximum((saa - sa * sa / n) * (sbb - sb * sb / n), 1e-30))
    win = num / den
    iy, ix = np.unravel_index(int(np.argmax(win)), win.shape)
    res.at_edge = bool(iy in (0, win.shape[0] - 1) or ix in (0, win.shape[1] - 1))
    dy, dx = float(ly[iy]), float(lx[ix])
    if not res.at_edge:
        dy += _parabolic(win[iy - 1, ix], win[iy, ix], win[iy + 1, ix])
        dx += _parabolic(win[iy, ix - 1], win[iy, ix], win[iy, ix + 1])
    res.shift = np.array([dy, dx])
    res.ncc = float(win.max())
    if res.feature_ratio < FEATURE_RATIO_MIN:
        res.warnings.append("lattice_only")
    if res.at_edge:
        res.warnings.append("peak_at_window_edge")
    if res.ncc < NCC_WEAK:
        res.warnings.append("weak_correlation")
    return res


def box_mask(shape, nm_per_px: float, cx_nm: float, cy_nm: float, size_nm: float,
             y0: int = 0, x0: int = 0) -> np.ndarray:
    """Boolean mask of the box; coordinates in nm from the left / top of the (uncropped) BEFORE frame."""
    yy, xx = np.mgrid[y0:y0 + shape[0], x0:x0 + shape[1]]
    h = size_nm / 2.0
    return (np.abs((xx + 0.5) * nm_per_px - cx_nm) <= h) & (np.abs((yy + 0.5) * nm_per_px - cy_nm) <= h)


@dataclass
class CompareResult:
    before: np.ndarray                   # cropped, flattened (not high-passed)
    after: np.ndarray                    # registered, cropped
    diff: np.ndarray                     # high-passed after - before (display / rms basis)
    mask: np.ndarray                     # box mask in the cropped frame
    crop: tuple                          # (y0, y1, x0, x1) in BEFORE pixels
    reg: RegResult
    rms_in: float
    rms_out: float
    ratio: float
    box_px: int


def compare_local(before, after, nm_per_px: float, box_cx_nm: float, box_cy_nm: float, box_size_nm: float,
                  hp_nm: float = 1.0, lp_nm: float = 0.4, window_px: int = 20,
                  exclude_margin_nm: float = 0.3, refine_px: float = 0.7,
                  rms_hp_factor: float = 2.0) -> CompareResult:
    """Flatten, register AFTER onto BEFORE, crop to the overlap, difference, rms in/out of the box.

    ``hp_nm`` (Gaussian high-pass sigma, 0 = off) is applied to both images before differencing and
    is the basis of the rms numbers as well as the display; the low-passed registration never sees it.
    ``rms_hp_factor``: the rms numbers use a high-pass of sigma max(hp_nm, factor * box size) so that the
    change itself does not leak into rms_out; the display difference uses ``hp_nm``.
    ``refine_px`` clamps the final fine tuning of the shift on the difference outside the box (0 = off).
    Raises ``ValueError`` for different shapes or a box with no pixels (or most pixels) in the overlap.
    """
    from scipy.ndimage import gaussian_filter
    b = flatten(before)
    a = flatten(after)
    if b.shape != a.shape:
        raise ValueError("image shapes differ: %s vs %s" % (b.shape, a.shape))
    # the box is where the change is expected: keep it (plus a margin that covers the drift) out of the registration
    excl = box_mask(b.shape, nm_per_px, box_cx_nm, box_cy_nm, box_size_nm + 2 * exclude_margin_nm)
    if excl.all() or excl.mean() > 0.6:
        excl = None
    reg = register_lowpassed(b, a, nm_per_px, lp_nm=lp_nm, window_px=window_px, exclude=excl)
    reg.shift_lowpass = reg.shift.copy()
    if hp_nm > 0:
        s = hp_nm / nm_per_px
        b_hp, a_hp0 = b - gaussian_filter(b, s), a - gaussian_filter(a, s)
    else:
        b_hp, a_hp0 = b, a
    ny, nx = b.shape
    if refine_px > 0:
        # The low-passed estimate is good to a fraction of a pixel; on a lattice a residual of 0.2 px still
        # leaves a lattice-periodic difference everywhere, which would inflate rms_out.  Fine-tune the shift on the
        # difference OUTSIDE the box, clamped to +-refine_px around the lattice-free estimate (far below a
        # lattice period, so this cannot hop to a neighbouring lattice translation).
        from scipy.optimize import minimize
        pad = int(np.ceil(np.abs(reg.shift).max() + refine_px)) + 2
        ring = np.zeros((ny, nx), bool)
        ring[pad:ny - pad, pad:nx - pad] = True
        if excl is not None:
            ring &= ~excl
        if ring.sum() > 64:
            c0 = reg.shift.copy()

            def cost(v):
                v = np.clip(v, -refine_px, refine_px)
                dd = fourier_shifted(a_hp0, c0 + v) - b_hp
                return float(dd[ring].std())

            best = minimize(cost, np.zeros(2), method="Nelder-Mead",
                            options={"xatol": 0.01, "fatol": 1e-9, "initial_simplex": [[0, 0], [0.25, 0], [0, 0.25]]})
            v = np.clip(best.x, -refine_px, refine_px)
            if cost(v) < cost(np.zeros(2)):
                reg.shift = c0 + v
                reg.refined_px = v
    sh = reg.shift
    a_hp = fourier_shifted(a_hp0, sh)
    a_reg = fourier_shifted(a, sh)
    y0, y1 = int(np.ceil(max(sh[0], 0))), ny - int(np.ceil(max(-sh[0], 0)))
    x0, x1 = int(np.ceil(max(sh[1], 0))), nx - int(np.ceil(max(-sh[1], 0)))
    if y1 - y0 < 8 or x1 - x0 < 8:
        raise ValueError("overlap too small after registration")
    d = (a_hp - b_hp)[y0:y1, x0:x1]
    # rms basis: a high-pass at the display scale leaks a localised change into its surroundings (a bump of
    # radius r loses r^2/(r^2+sigma^2) of its weight to a halo of size sigma, outside the box).  Use a
    # broader one, so rms_out is not inflated by the change itself.
    s_rms = max(hp_nm, rms_hp_factor * box_size_nm) / nm_per_px
    ds = fourier_shifted(a, sh) - b
    ds = (ds - gaussian_filter(ds, s_rms))[y0:y1, x0:x1]
    m = box_mask(d.shape, nm_per_px, box_cx_nm, box_cy_nm, box_size_nm, y0, x0)
    n_in = int(m.sum())
    if n_in < 4:
        raise ValueError("the box has no pixels inside the overlap")
    if n_in > 0.5 * m.size:
        raise ValueError("the box covers more than half of the overlap; inside/outside is not meaningful")
    rin, rout = float(ds[m].std()), float(ds[~m].std())
    return CompareResult(before=b[y0:y1, x0:x1], after=a_reg[y0:y1, x0:x1], diff=d, mask=m,
                         crop=(y0, y1, x0, x1), reg=reg, rms_in=rin, rms_out=rout,
                         ratio=rin / rout if rout > 0 else float("inf"), box_px=n_in)
