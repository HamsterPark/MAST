# -*- coding: utf-8 -*-
"""Core of ``CompareFramesLocalChange`` on synthetic frames (no instrument, no real data)."""
from __future__ import annotations

import numpy as np
import pytest

from mast.vision.frame_compare import compare_local, register_lowpassed, resample_to

N = 128
NM = 0.05                                   # 6.4 nm frame, lattice period 8 px = 0.4 nm
YY, XX = np.mgrid[0:N, 0:N].astype(float)
BLOBS = [(30, 40, 5, 3.0), (90, 30, 4, -2.5), (70, 100, 6, 3.0), (25, 105, 5, -2.0), (60, 70, 4, 2.0),
         (105, 80, 5, 2.5), (45, 15, 4, -2.0), (100, 115, 4, 2.0)]


def scene(dy=0.0, dx=0.0, lattice=True, blobs=True, bump=None, gain=1.0):
    """Analytic image; content moved by (dy, dx) px, so no wrap-around enters the test."""
    y, x = YY - dy, XX - dx
    z = np.zeros((N, N))
    if lattice:
        k = 2 * np.pi / 8.0
        z += np.cos(k * x) + np.cos(k * (0.5 * x + 0.866 * y)) + np.cos(k * (0.5 * x - 0.866 * y))
    if blobs:
        for by, bx, s, a in BLOBS:
            z += a * np.exp(-((y - by) ** 2 + (x - bx) ** 2) / (2 * s * s))
    if bump:
        by, bx, s, a = bump
        z += a * np.exp(-((y - by) ** 2 + (x - bx) ** 2) / (2 * s * s))
    return gain * z


def test_local_bump_is_found_with_subpixel_shift():
    rng = np.random.default_rng(1)
    truth = (3.4, -2.6)                                  # drift of AFTER relative to BEFORE, px
    bump = (64, 64, 5, 3.0)                              # written change, centre of the frame, 0.25 nm sigma
    before = scene() + rng.normal(0, 0.05, (N, N))
    after = scene(*truth, bump=bump) + rng.normal(0, 0.05, (N, N))
    r = compare_local(before, after, NM, 3.2, 3.2, 1.6)
    # the registration moves AFTER back onto BEFORE, i.e. by minus the drift
    assert abs(r.reg.shift[0] + truth[0]) < 0.3 and abs(r.reg.shift[1] + truth[1]) < 0.3
    assert abs(r.reg.shift_lowpass[0] + truth[0]) < 0.6 and abs(r.reg.shift_lowpass[1] + truth[1]) < 0.6
    assert r.ratio > 3.0
    # null: the same drift without the written change must stay near 1
    null = compare_local(before, scene(*truth) + rng.normal(0, 0.05, (N, N)), NM, 3.2, 3.2, 1.6)
    assert null.ratio < 1.6 and r.ratio > 2 * null.ratio


def test_global_contrast_change_is_not_box_shaped():
    rng = np.random.default_rng(2)
    before = scene() + rng.normal(0, 0.05, (N, N))
    after = scene(1.0, -1.5, gain=1.4) + rng.normal(0, 0.05, (N, N))
    r = compare_local(before, after, NM, 3.2, 3.2, 1.6)
    assert 0.7 < r.ratio < 1.4


def test_lattice_alias_is_not_taken_with_a_shift_beyond_half_a_period():
    """5.3 px > half the 8 px period: the lattice alone cannot tell +5.3 from -2.7 inside a 20 px window."""
    rng = np.random.default_rng(3)
    before = scene() + rng.normal(0, 0.1, (N, N))
    after = scene(0.0, 5.3) + rng.normal(0, 0.1, (N, N))
    reg = register_lowpassed(before, after, NM, window_px=20)
    assert abs(reg.shift[1] + 5.3) < 0.3 and abs(reg.shift[0]) < 0.3
    assert "lattice_only" not in reg.warnings


def test_lattice_only_frames_are_flagged_not_trusted():
    before = scene(blobs=False)
    after = scene(0.0, 5.3, blobs=False)
    reg = register_lowpassed(before, after, NM, window_px=20)
    assert "lattice_only" in reg.warnings                # shift is defined only modulo the lattice
    assert np.abs(reg.shift).max() <= 20.5               # and it never leaves the window


def test_subpixel_refinement_is_clamped():
    rng = np.random.default_rng(4)
    reg = register_lowpassed(scene() + rng.normal(0, 0.1, (N, N)), scene(1.0, 1.0) + rng.normal(0, 0.1, (N, N)), NM)
    frac = reg.shift - np.round(reg.shift)
    assert np.all(np.abs(frac) <= 0.5 + 1e-9)


def test_shape_mismatch_and_bad_box():
    with pytest.raises(ValueError):
        compare_local(scene(), scene()[:100, :100], NM, 3.2, 3.2, 3.0)
    with pytest.raises(ValueError):
        compare_local(scene(), scene(), NM, 3.2, 3.2, 5.0)          # box covers most of the frame
    with pytest.raises(ValueError):
        compare_local(scene(), scene(), NM, 50.0, 50.0, 1.0)        # box outside the frame


def test_resample_to_keeps_content():
    big = scene(lattice=False)
    small = resample_to(big, (64, 64))
    assert small.shape == (64, 64)
    assert abs(resample_to(small, (N, N)) - big).mean() < 0.1
