# -*- coding: utf-8 -*-
"""Drift-corrected frame averaging: registration core (``mast.vision.frame_average``)."""
from __future__ import annotations

import numpy as np
import pytest

from mast.vision.frame_average import (
    average_registered, common_crop, flatten, register_shift, valid_mask)

N = 128
YY, XX = np.mgrid[0:N, 0:N].astype(float)
PERIOD = 8.0


def lattice(dy=0.0, dx=0.0, bump=True):
    """Hexagon-ish periodic image sampled at (y+dy, x+dx); ``bump`` adds ONE non-periodic feature."""
    y, x = YY + dy, XX + dx
    k = 2 * np.pi / PERIOD
    z = (np.cos(k * x) + np.cos(k * (0.5 * x + 0.866 * y)) + np.cos(k * (0.5 * x - 0.866 * y)))
    if bump:
        z = z + 6.0 * np.exp(-((y - 60) ** 2 + (x - 70) ** 2) / (2 * 6.0 ** 2))
    return z


def noisy(z, sigma, seed):
    return z + np.random.default_rng(seed).normal(0, sigma, z.shape)


@pytest.mark.parametrize("dy,dx", [(0.0, 0.0), (1.3, -2.6), (-3.4, 2.1), (3.7, 3.7)])
def test_known_subpixel_shift_recovered_with_noise(dy, dx):
    ref, img = noisy(lattice(), 0.5, 1), noisy(lattice(dy, dx), 0.5, 2)
    sh = register_shift(ref, img, rmax_px=20)
    assert abs(sh[0] - dy) < 0.2 and abs(sh[1] - dx) < 0.2, (sh, dy, dx)


def test_search_window_beats_lattice_alias():
    """With rmax larger than the period the aliases at +-8 px are inside the window; the
    true (smallest-drift) peak must still win -- and a window smaller than the true shift
    must NOT return an alias far away (it is bounded by rmax)."""
    sh = register_shift(noisy(lattice(), 0.8, 3), noisy(lattice(3.3, -2.2), 0.8, 4), rmax_px=20)
    assert abs(sh[0] - 3.3) < 0.2 and abs(sh[1] + 2.2) < 0.2
    far = register_shift(lattice(bump=False), lattice(12.0, 0.0, bump=False), rmax_px=5)
    assert np.all(np.abs(far) <= 5.0 + 0.6), far


def test_whitened_phase_correlation_would_alias_but_ours_does_not():
    """Documents the reason for the design: whitening makes lattice translations equal."""
    ref, img = noisy(lattice(bump=False), 0.5, 5), noisy(lattice(2.0, 1.0, bump=False), 0.5, 6)
    F = np.fft.fft2(ref) * np.conj(np.fft.fft2(img))
    c = np.real(np.fft.ifft2(F / (np.abs(F) + 1e-12)))
    # lattice translation by one period is (nearly) as strong as the true peak in the whitened map
    idx_true = (int(round(-2.0)) % N, int(round(-1.0)) % N)
    idx_alias = ((int(round(-2.0)) + 8) % N, (int(round(-1.0)) + 8) % N)
    assert c[idx_alias] > 0.5 * c[idx_true]          # ambiguous without a window
    sh = register_shift(ref, img, rmax_px=20)
    assert abs(sh[0] - 2.0) < 0.2 and abs(sh[1] - 1.0) < 0.2


def test_valid_mask_masks_only_the_wrapped_edge():
    m = valid_mask((10, 12), (2.3, 0.0))
    assert m[:3].sum() == 0 and m[3:].all()
    m = valid_mask((10, 12), (-1.2, 0.0))
    assert m[-2:].sum() == 0 and m[:-2].all()
    m = valid_mask((10, 12), (0.0, 1.5))
    assert m[:, :2].sum() == 0 and m[:, 2:].all()
    m = valid_mask((10, 12), (0.0, -0.4))
    assert m[:, -1].sum() == 0 and m[:, :-1].all()
    assert valid_mask((10, 12), (0.0, 0.0)).all()


def test_common_crop_covers_all_shifts():
    assert common_crop((100, 100), [[0, 0], [2.3, -1.2], [-1.1, 4.0]]) == (3, 98, 4, 98)


def test_average_has_no_wrapped_content_and_improves_noise():
    shifts = [(0.0, 0.0), (1.4, -2.2), (-2.6, 1.9), (3.1, 3.3), (-1.7, -3.0), (0.6, 2.5)]
    imgs = [noisy(lattice(dy, dx), 1.5, 10 + i) for i, (dy, dx) in enumerate(shifts)]
    res = average_registered(imgs, rmax_px=15)
    assert res.n_images == 6
    for got, (dy, dx) in zip(res.shifts, shifts):
        assert abs(got[0] - dy) < 0.25 and abs(got[1] - dx) < 0.25, (got, dy, dx)
    y0, y1, x0, x1 = res.crop
    truth = flatten(lattice())[y0:y1, x0:x1]
    single_err = np.std(res.reference - truth)
    avg_err = np.std(res.average - truth)
    assert np.isfinite(res.average).all()
    assert avg_err < 0.6 * single_err                 # ~1/sqrt(6) in the ideal case
    # a wrapped edge (not masked) would put the bump's far side into the crop: error stays small everywhere
    assert np.abs(res.average - truth).max() < 4 * avg_err + 1.0


def test_mismatched_sizes_and_empty_rejected():
    with pytest.raises(ValueError, match="sizes differ"):
        average_registered([np.zeros((64, 64)), np.zeros((64, 60))])
    with pytest.raises(ValueError):
        average_registered([])
    with pytest.raises(ValueError):
        register_shift(np.zeros((8, 8)), np.zeros((8, 9)))
