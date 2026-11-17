# -*- coding: utf-8 -*-
"""Gap modulation at mains harmonics, from a tunnelling-current oscilloscope trace.

Pure numpy (no hardware).  Why it works in the log domain: ``I ~ exp(-2 kappa z)``, so a gap
modulation ``dz`` is a *linear* ln|I| modulation ``A = 2 kappa dz`` whatever the setpoint.  Use
``|I|`` -- at negative sample bias the current is negative and ``y[y > 0]`` silently drops everything.

Why the "live current" reads high: the Z loop runs in log mode, so the *geometric* mean of |I| sits
at the setpoint while the *arithmetic* mean (what a single reading averages to) is inflated by
``I0(A)`` for a sinusoidal ln-modulation of amplitude A (modified Bessel function).

This is a different job from :mod:`mast.monitoring.features` (PSD band powers / ``line_ratio`` of
the amps, for tip-health trending): that gives *how much power sits in 45-65 Hz*; this gives
*picometres of gap modulation at each harmonic* and the fine frequency.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

#: Verdict thresholds on the dominant harmonic, pm (empirical rules of thumb for atomic resolution).
OK_PM = 10.0
FIX_PM = 35.0
#: |f_fit - n*f_mains| below this (Hz) counts as mains-locked (128 ms trace -> ~0.4 Hz scatter).
LOCK_TOL_HZ = 1.0
FINE_SPAN_HZ = 15.0
FINE_STEP_HZ = 0.05


def _design(t: np.ndarray, freqs) -> np.ndarray:
    cols = []
    for f in freqs:
        w = 2.0 * math.pi * f * t
        cols += [np.sin(w), np.cos(w)]
    cols.append(np.ones_like(t))
    return np.column_stack(cols)


def ln_abs_current(samples_a, floor_pa: float = 0.5) -> tuple[np.ndarray, float]:
    """(ln|I| with |I| in pA clipped at floor, fraction of samples at/below the floor)."""
    a = np.abs(np.asarray(samples_a, float)) * 1e12
    a = a[np.isfinite(a)]
    if a.size == 0:
        return a, 1.0
    below = float(np.mean(a <= floor_pa))
    return np.log(np.clip(a, floor_pa, None)), below


def harmonic_amplitudes(lnI: np.ndarray, dt: float, freqs) -> dict:
    """Joint least-squares ln-amplitude A_f = hypot(a, b) for every f (ln|I| = sum a sin + b cos + c)."""
    t = np.arange(lnI.size) * dt
    coef, *_ = np.linalg.lstsq(_design(t, freqs), lnI, rcond=None)
    return {float(f): float(math.hypot(coef[2 * i], coef[2 * i + 1])) for i, f in enumerate(freqs)}


def fine_frequency(lnI: np.ndarray, dt: float, f_guess: float, span: float = FINE_SPAN_HZ,
                   step: float = FINE_STEP_HZ) -> float:
    """Frequency in f_guess +- span minimising the single-sinusoid fit residual."""
    t = np.arange(lnI.size) * dt
    best_f, best_r = f_guess, math.inf
    for f in np.arange(max(f_guess - span, step), f_guess + span + step / 2, step):
        A = _design(t, [f])
        coef, *_ = np.linalg.lstsq(A, lnI, rcond=None)
        r = float(np.sum((lnI - A @ coef) ** 2))
        if r < best_r:
            best_f, best_r = float(f), r
    return best_f


def pm_from_ln(amp_ln: float, two_kappa_nm: float) -> float:
    """Gap modulation (pm) for a ln-amplitude: A / (2 kappa) with 2 kappa in 1/nm."""
    return amp_ln / two_kappa_nm * 1000.0


def harmonic_list(f_mains: float, fs: float, max_harmonic: int = 0) -> list:
    """n*f_mains for n = 1..max_harmonic, always below 0.9*Nyquist (max_harmonic<=0 -> as many as fit)."""
    top = int(0.9 * fs / 2.0 // f_mains)
    n = top if max_harmonic <= 0 else min(max_harmonic, top)
    return [f_mains * k for k in range(1, n + 1)]


def predicted_arith_geo(amp_ln: float) -> float:
    """Arithmetic/geometric mean of exp(A sin) over a period = I0(A)."""
    return float(np.i0(amp_ln))


def verdict_for(dominant_pm: float) -> str:
    if dominant_pm <= OK_PM:
        return "ok"
    if dominant_pm <= FIX_PM:
        return "marginal"
    return "fix_source"


@dataclass
class MainsGapResult:
    ok: bool
    error: str = ""
    n_traces_used: int = 0
    n_traces_rejected: int = 0
    fs_hz: float = 0.0
    duration_s: float = 0.0
    harmonics_hz: list = field(default_factory=list)
    per_harmonic_pm: dict = field(default_factory=dict)      # "300" -> median pm
    dominant_hz: float = 0.0
    dominant_pm: float = 0.0
    dominant_amp_ln: float = 0.0
    fine_freq_hz: float = 0.0
    fine_freq_spread_hz: float = 0.0
    offset_from_harmonic_hz: float = 0.0
    mains_locked: bool = False
    mean_abs_pa_arith: float = 0.0
    mean_abs_pa_geo: float = 0.0
    arith_over_geo: float = 0.0
    predicted_i0: float = 0.0
    verdict: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def analyze_traces(traces, dt: float, f_mains: float = 50.0, max_harmonic: int = 0,
                   two_kappa_nm: float = 19.0, floor_pa: float = 0.5,
                   max_below_floor: float = 0.5) -> MainsGapResult:
    """``traces``: list of current arrays in A (sign irrelevant); ``dt`` seconds per sample."""
    if not dt or dt <= 0 or not math.isfinite(dt):
        return MainsGapResult(False, error="invalid dt")
    if f_mains <= 0 or two_kappa_nm <= 0:
        return MainsGapResult(False, error="f_mains and two_kappa_nm must be > 0")
    fs = 1.0 / dt
    freqs = harmonic_list(f_mains, fs, max_harmonic)
    if not freqs:
        return MainsGapResult(False, fs_hz=fs, error="sampling rate too low for the mains frequency")
    lns, rejected = [], 0
    arith, geo = [], []
    for y in traces:
        ln, below = ln_abs_current(y, floor_pa)
        if ln.size < 16 or below > max_below_floor:
            rejected += 1
            continue
        lns.append(ln)
        arith.append(float(np.mean(np.exp(ln))))
        geo.append(float(np.exp(np.mean(ln))))
    if not lns:
        return MainsGapResult(False, n_traces_rejected=rejected, fs_hz=fs,
                              error="no usable samples (empty, or all traces mostly below the %.2g pA floor)"
                              % floor_pa)
    n = min(x.size for x in lns)
    lns = [x[:n] for x in lns]
    amps = np.array([[harmonic_amplitudes(x, dt, freqs)[f] for f in freqs] for x in lns])
    med_ln = np.median(amps, axis=0)
    pm = [pm_from_ln(a, two_kappa_nm) for a in med_ln]
    k = int(np.argmax(med_ln))
    f_dom = freqs[k]
    fits = [fine_frequency(x, dt, f_dom) for x in lns]
    f_fit = float(np.median(fits))
    off = f_fit - f_dom
    a_med, g_med = float(np.median(arith)), float(np.median(geo))
    return MainsGapResult(
        ok=True, n_traces_used=len(lns), n_traces_rejected=rejected, fs_hz=fs, duration_s=n * dt,
        harmonics_hz=freqs,
        per_harmonic_pm={("%g" % f): round(p, 3) for f, p in zip(freqs, pm)},
        dominant_hz=f_dom, dominant_pm=pm[k], dominant_amp_ln=float(med_ln[k]),
        fine_freq_hz=f_fit, fine_freq_spread_hz=float(np.std(fits)), offset_from_harmonic_hz=off,
        mains_locked=abs(off) <= LOCK_TOL_HZ,
        mean_abs_pa_arith=a_med, mean_abs_pa_geo=g_med, arith_over_geo=a_med / g_med,
        predicted_i0=predicted_arith_geo(float(med_ln[k])), verdict=verdict_for(pm[k]),
    )
