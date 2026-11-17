# -*- coding: utf-8 -*-
"""Synthetic-trace tests for mast.monitoring.mains_gap."""
from __future__ import annotations

import math

import numpy as np
import pytest

from mast.monitoring import mains_gap as mg

DT = 5e-4          # 2 kHz
N = 256
TWO_KAPPA = 19.0


def synth(pm, f=300.0, i0_pa=50.0, sign=1.0, seed=0, noise=0.02, n=N, dt=DT):
    rng = np.random.default_rng(seed)
    t = np.arange(n) * dt
    amp = pm / 1000.0 * TWO_KAPPA
    ln = math.log(i0_pa) + amp * np.sin(2 * math.pi * f * t + rng.uniform(0, 6.28)) + rng.normal(0, noise, n)
    return sign * np.exp(ln) * 1e-12


def traces(pm, **kw):
    return [synth(pm, seed=s, **kw) for s in range(6)]


@pytest.mark.parametrize("pm", [80.0, 10.0])
def test_recovers_known_modulation(pm):
    r = mg.analyze_traces(traces(pm), DT)
    assert r.ok and r.dominant_hz == 300.0
    assert r.dominant_pm == pytest.approx(pm, rel=0.05)
    assert r.per_harmonic_pm["300"] == pytest.approx(r.dominant_pm, rel=1e-3)


def test_fine_frequency_and_lock():
    r = mg.analyze_traces(traces(80.0, f=300.1), DT)
    assert abs(r.fine_freq_hz - 300.1) < 0.5
    assert r.mains_locked
    r2 = mg.analyze_traces(traces(80.0, f=306.0), DT)
    assert r2.dominant_hz == 300.0 and not r2.mains_locked
    assert abs(r2.fine_freq_hz - 306.0) < 0.5


def test_negative_current_same_as_positive():
    a = mg.analyze_traces(traces(80.0), DT)
    b = mg.analyze_traces(traces(80.0, sign=-1.0), DT)
    assert b.ok and b.dominant_pm == pytest.approx(a.dominant_pm)


def test_60hz_mains():
    r = mg.analyze_traces(traces(60.0, f=300.0), DT, f_mains=60.0)
    assert r.dominant_hz == 300.0 and r.dominant_pm == pytest.approx(60.0, rel=0.05)


def test_harmonic_list_limits():
    assert max(mg.harmonic_list(50.0, 2000.0)) < 1000.0
    assert mg.harmonic_list(50.0, 2000.0, 3) == [50.0, 100.0, 150.0]
    assert mg.harmonic_list(50.0, 100.0) == []


def test_arith_geo_matches_i0():
    r = mg.analyze_traces(traces(80.0, noise=0.0), DT)   # A = 1.52
    assert r.arith_over_geo == pytest.approx(r.predicted_i0, rel=0.08)
    assert r.arith_over_geo == pytest.approx(float(np.i0(1.52)), rel=0.08)
    assert r.mean_abs_pa_geo == pytest.approx(50.0, rel=0.03)


@pytest.mark.parametrize("pm,v", [(5.0, "ok"), (10.0, "ok"), (10.1, "marginal"), (35.0, "marginal"),
                                  (35.1, "fix_source"), (190.0, "fix_source")])
def test_verdict_boundaries(pm, v):
    assert mg.verdict_for(pm) == v


def test_error_paths():
    assert not mg.analyze_traces([], DT).ok
    assert not mg.analyze_traces([[]], DT).ok
    below = [np.full(N, 0.1e-12) for _ in range(3)]       # all below the 0.5 pA floor
    r = mg.analyze_traces(below, DT)
    assert not r.ok and "floor" in r.error
    assert not mg.analyze_traces(traces(10.0), 0.0).ok
    # a bad trace among good ones is rejected, not fatal
    r = mg.analyze_traces(traces(40.0) + [below[0]], DT)
    assert r.ok and r.n_traces_rejected == 1 and r.n_traces_used == 6
