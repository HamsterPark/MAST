"""Optical-station measurement protocols (docs/v2/design/optical_station_witec.md §6.3, D11).

==================  ========  =====================================================================
skill               safety    what
==================  ========  =====================================================================
SHGExcitationScan   CONFIRM   excitation-wavelength SHG scan: tunable source, spectrometer centre = λ/2
PolarizationMatrix  CONFIRM   par/crs polarisation matrix (manual incident polariser, analyzer in software)
TemperatureSeries   CONFIRM   spectra versus sample temperature (ramp -> settle -> measure)
PowerSeries         CONFIRM   spectra versus excitation power (+ log-log slope, SHG expects ~2)
==================  ========  =====================================================================

What a protocol is
------------------
An ordinary Python skill that drives **devices directly** through the instrument registry
(``Microscope`` / ``Spectrometer`` / ``Laser`` / ``TunableLaser`` / ``PowerMeter`` /
``TemperatureController``) — it does not call the atomic optics skills. The skill itself runs under
``ExecutionContext`` (instrument token, sample gate: tags ``spectrum`` + ``acquisition``); the
hardware calls inside are guarded by the drivers' Layer-0 limits. Out of range is **refused, never
clamped**: the whole plan is checked against the device limits *before* anything is touched.

Everything a person has to do goes through ``mast.operator_hitl.prompts.ask`` (bounded wait,
cancellable). Where the operator can sign a physical fact (which λ/2 plate is in the beam, the
incident polariser angle, the NPI pump current, "laser area safe") the prompt is ``kind="attest"`` and
the answer lands in the optical-state registry. **No code in this module writes an optical-state
slot** — only the operator's answer does (``prompts.answer`` -> ``set_by_operator``).

Safe direction
--------------
Whatever ends a run (done, operator declined / timed out / cancelled, abort, device error), the
``finally`` block does, each step on its own and never raising:

1. every external source in the run: ``emission_off()``;
2. the WITec laser shutter: closed (and the answer checked);
3. when the run did not finish normally: spectrometer ``stop()`` and the microscope's ``stop_all()``
   (sequencers, stage);
4. temperature controller (only when the run drove it): ``stop()`` — or, after a *normal* finish and
   only if ``final_setpoint_c`` was given, the controller is sent there (never after an abort, and never
   to a controller somebody else has stopped);
5. the WITec write access is handed back (design decision D5) unless ``release_write_access=false`` — and
   not while the shutter is not confirmed closed (a later ``StopWITec`` / ``CloseWITecShutter`` needs it).

A WITec shutter that cannot be **confirmed closed** (the close raised, or the driver answers "still
open") is a fault: ``stop_all`` is sent, the spectrum in hand is stored, and the run stops with
``stop_kind = shutter_not_closed`` — no prompt is posted and no new action starts after it (``_Run.check``).
A stop that somebody else asks for while a protocol runs (``StopWITec``, ``CloseWITecShutter``, ``LaserEmissionOff``,
``StopTemperatureRamp``, the withdrawal of ``laser_area_safe``, the E-STOP) acts once; the next step would undo it
(open the shutter again, switch the source back on, send the next setpoint). Every station device therefore counts
such stops (``external_stop_epoch``, ``mast.instruments.external_stop``); the run records the epoch of each device it
uses when it begins and compares them at every ``check()`` — in front of every hardware call, so also before a shutter
is reopened, a source switched on or a setpoint sent. A change ends the run cleanly with ``stop_kind =
stopped_externally`` (the reason the device was given is in the text; a pending prompt is cancelled); afterwards no
shutter is reopened, no source switched on and no setpoint sent, the final one included. A switch-on or a setpoint
that the driver itself cancelled because a stop was asked for after it started (``EmissionPreempted`` /
``TemperaturePreempted``) ends the run the same way. The run's own closing and
cleanup do not count. A temperature controller that reads *stopped* although this run only ever sends setpoints
ends the run with ``stop_kind = temperature_stopped``; that read-back is a supplement, since a stop is not always
readable (a TC100 without a status register).

Laser light needs a valid ``laser_area_safe`` attestation — in **every** protocol, whichever laser it
uses: the real WITec driver refuses each light-emitting operation (shutter open, exposure, Analyzer
series) without it. It is checked before anything is touched (``dry_run`` lists the signing under
``before_start``; with ``ask_attest_laser=false`` a missing signature refuses the run), asked for before
the first step, checked again at the start of every step and right before every exposure / series (a
signature lasts 8 h). When it is missing or has expired the operator is asked to sign again — a source
this run switched on is switched off for the time of the signing — and if nobody signs, the run takes the
safe direction and stops. Switching an external source's emission **on** additionally needs
``allow_emission_control`` in the device config (checked by the driver).

``allow_emission_control=false`` means *software does not add light* (design doc 6.4): while a source
emits, every increase - a channel's amplitude or the power raised from 0 or from any lit level - passes
the full emission gate in the drivers; only dimming, tuning the colour and shutting off do not. So a
plan that must raise something on a source that is emitting (or whose state cannot be read) is **refused
up front, before anything is touched**, not halted halfway through the preparation: SHGExcitationScan
when ``amplitude_pct`` is above the channel's present amplitude, PowerSeries when the series raises the
power from one step to the next (the source emits from the first step on) or when the first power is
above the power the source has now. A source that is still off is not refused: its amplitude / power is
set first and the operator switches it on afterwards. The message names the two ways out (set the
channel / power by hand first - afterwards the protocol only dims - or enable ``allow_emission_control``
after the bench self-test); ``dry_run`` cannot read the source and says so in a note. An amplitude / power
that cannot be read counts as "possibly lower", an emission state that cannot be read as "emitting".

When a person has to reach into the beam path (swapping the λ/2 plate, turning the incident polariser) the
WITec shutter is closed (and must read closed), and the external source is taken dark first: by software when
it can be - whether this run switched it on or it was already on - and read back; when software cannot confirm
it off, the operator is asked to switch it off or block the beam, and the protocol goes on only after that.
The prompt says only what was read back. Afterwards the source comes back by software (``manage_emission``,
``allow_emission_control``, a fresh signature) or, otherwise, the operator is asked to switch it on.

Files
-----
Per step, immediately after the exposure (so an interrupted run keeps everything it measured):

* ``<stem>.npy``  — row 0 = wavelength (nm), rows 1… = frames (same layout as AcquireSpectrum);
* ``<stem>.json`` — sidecar (``schema = mast.spectrum/1`` plus ``protocol``, ``conditions``,
  ``optical_state`` snapshot, ``instruments`` status, ``operator_prompts``…).

``<stem>`` is generated from the conditions, e.g. ``shg_810nm_p0.422mW_a0deg`` — nobody renames files
by hand. Both files go into the active sample's ``raw/other`` through the experiment file store
(staging copies stay under ``artifacts/optics_protocols`` when no sample is active).

The run summary — ``<run_id>_summary.json`` + ``.csv`` — is rewritten atomically after every step in
the staging run directory; at the end (or when the run stops) a PNG overview is drawn and the three
files are archived into the sample too. The summary says which step the run stopped at and why.

Analyzer convention of PolarizationMatrix (derivation and doubts)
-----------------------------------------------------------------
Survey of earlier polarisation-resolved series ("0,0 / 0,135 / 10,5 / 10,140 … 360,135" style sets): the
incident polarisation theta is turned by hand in 10° steps and the analyzer reading ``rot_deg`` alternates
between ``theta/2`` and ``theta/2 + 135``. So in the usual bench convention

* par:  ``rot = theta/2``;   crs:  ``rot = theta/2 + 135``.

The factor 1/2 only makes sense if the analyzer acts like a half-wave plate in front of a fixed
polariser (the effective transmission axis is ``2·rot``); then ``2·(theta/2) = theta`` is parallel and
``2·(theta/2 + 135) = theta + 270 ≡ theta + 90 (mod 180)`` is crossed. Under that reading ``+135`` and
``+45`` are the same physical setting (the reading is periodic in 90°), and ``+135`` is simply what the
lab used. We keep the **historical numbers as defaults** (``analyzer_scale = 0.5``,
``analyzer_offset_deg = 0``, ``crs_offset_deg = 135``) so new data line up with the old sets, and expose
the three constants because they are an inference, not documented hardware behaviour. Open doubts:

* whether ``rot = 0`` really is "parallel to theta = 0" is an alignment fact of the bench, not something
  the software knows (``analyzer_offset_deg`` shifts it);
* the history records the *requested* theta; this module sets the analyzer from the *attested* actual
  angle (they agree within ``angle_tolerance_deg``, otherwise the operator is asked again);
* a reading of exactly 360° is sent as 0° (same physical position) so the wheel's end stop is never
  asked for.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable

import numpy as np

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.instruments import config as inst_config
from mast.instruments.base import (
    InstrumentError,
    InstrumentUnavailable,
    MotionAborted,
    OperationNotSupported,
)
from mast.instruments.laser import EmissionControlDisabled, EmissionPreempted, LaserLimitError
from mast.instruments.microscope import MicroscopeLimitError, WriteAccessDenied
from mast.instruments.power_meter import PowerMeterError
from mast.instruments.registry import get_instrument_registry
from mast.instruments.spectrometer import SpectrometerLimitError
from mast.instruments.temperature import TemperatureLimitError, TemperaturePreempted, TemperatureWriteDisabled
from mast.operator_hitl import optical_state, prompts
from mast.operator_hitl.prompts import OperatorPrompt, PromptOption
from mast.skills.base import BaseSkill
from mast.skills.builtins.optics_common import (
    LiveStream,
    abort_probe,
    atomic_write_bytes,
    dump_json_bytes,
    hlc_now,
    json_safe,
    new_run_id,
    resolve_device,
    scope_ids,
    store_in_experiment,
)

logger = logging.getLogger(__name__)

__all__ = ["SHGExcitationScan", "PolarizationMatrix", "TemperatureSeries", "PowerSeries"]

SKILL_VERSION = "1.0.0"

#: optical-state slot ids the protocols read or ask the operator to sign (identity strings)
SLOT_LASER_SAFE = "laser_area_safe"
SLOT_WAVEPLATE = "excitation_waveplate"
SLOT_INCIDENT = "incident_polarizer_deg"
SLOT_POWER_PCT = "npi_power_pct"

#: hard ceiling on the number of steps in one run (a typo in a step size must not queue 10^6 spectra)
MAX_STEPS = 2000
#: stem length budget: the experiment folder allows 58 characters for a file name incl. extension
STEM_LIMIT = 48
#: per-step result rows echoed in the SkillResult (the summary files always carry all of them)
ROWS_IN_RESULT = 120

_TAGS = ["optics", "optical-station", "protocol", "spectrum", "acquisition", "write"]


# ── refusal / halt ────────────────────────────────────────────────────────────


class ProtocolRefused(Exception):
    """The request is refused *before* any hardware was touched."""


class ProtocolHalt(Exception):
    """A running protocol has to stop cleanly. ``kind`` is an identity string (never translated)."""

    def __init__(self, kind: str, message: str, *, phase: str = "step"):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.phase = phase


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ── parameter parsing ───────────────────────────────────────────────────────────


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _as_number(v: Any) -> float | None:
    """A finite float, or ``None`` for anything else (an unreadable device value)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _get(params: dict, name: str, default: Any = None) -> Any:
    v = params.get(name)
    return default if _blank(v) else v


def _as_float(params: dict, name: str, default: float | None, *, lo: float | None = None,
              hi: float | None = None, lo_open: bool = False) -> float | None:
    v = _get(params, name, default)
    if v is None:
        return None
    if isinstance(v, bool):
        raise ProtocolRefused(tr(f"参数 {name} 必须是数，不是布尔值", f"parameter {name} must be a number, not a boolean"))
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ProtocolRefused(tr(f"参数 {name} = {v!r} 不是数", f"parameter {name} = {v!r} is not a number")) from None
    if not math.isfinite(x):
        raise ProtocolRefused(tr(f"参数 {name} 必须是有限的数", f"parameter {name} must be finite"))
    if lo is not None and (x <= lo if lo_open else x < lo):
        raise ProtocolRefused(tr(f"参数 {name} = {x:g} 低于下限 {lo:g}（拒绝，不截断）",
                                 f"parameter {name} = {x:g} is below the minimum {lo:g} (refused, not clamped)"))
    if hi is not None and x > hi:
        raise ProtocolRefused(tr(f"参数 {name} = {x:g} 高于上限 {hi:g}（拒绝，不截断）",
                                 f"parameter {name} = {x:g} is above the maximum {hi:g} (refused, not clamped)"))
    return x


def _as_int(params: dict, name: str, default: int | None, *, lo: int | None = None,
            hi: int | None = None) -> int | None:
    x = _as_float(params, name, None if default is None else float(default),
                  lo=None if lo is None else float(lo), hi=None if hi is None else float(hi))
    if x is None:
        return None
    if x != int(x):
        raise ProtocolRefused(tr(f"参数 {name} = {x:g} 必须是整数", f"parameter {name} = {x:g} must be a whole number"))
    return int(x)


def _as_bool(params: dict, name: str, default: bool) -> bool:
    v = _get(params, name, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "yes", "1", "on"):
        return True
    if s in ("false", "no", "0", "off"):
        return False
    raise ProtocolRefused(tr(f"参数 {name} = {v!r} 必须是 true/false", f"parameter {name} = {v!r} must be true/false"))


def _as_str(params: dict, name: str, default: str = "") -> str:
    v = _get(params, name, default)
    return str(v).strip()


def _as_choice(params: dict, name: str, default: str, allowed: Iterable[str]) -> str:
    allowed = tuple(allowed)
    s = _as_str(params, name, default).lower()
    if s not in allowed:
        raise ProtocolRefused(tr(f"参数 {name} = {s!r} 不在 {list(allowed)} 之内",
                                 f"parameter {name} = {s!r} is not one of {list(allowed)}"))
    return s


def _number_list(raw: Any, label: str) -> list[float]:
    """``"810, 820 830"`` / ``"[810, 820]"`` / a list -> floats (finite, not boolean, non-empty)."""
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        s = str(raw).strip()
        if s.startswith("["):
            try:
                items = json.loads(s)
            except ValueError:
                raise ProtocolRefused(tr(f"{label} 不是合法的 JSON 数组: {s[:60]!r}",
                                         f"{label} is not a valid JSON array: {s[:60]!r}")) from None
            if not isinstance(items, list):
                raise ProtocolRefused(tr(f"{label} 必须是数组", f"{label} must be an array"))
        else:
            items = [t for t in re.split(r"[,;\s]+", s) if t]
    out: list[float] = []
    for it in items:
        if isinstance(it, bool):
            raise ProtocolRefused(tr(f"{label} 里有布尔值 {it!r}", f"{label} contains the boolean {it!r}"))
        try:
            x = float(it)
        except (TypeError, ValueError):
            raise ProtocolRefused(tr(f"{label} 里的 {it!r} 不是数", f"{label}: {it!r} is not a number")) from None
        if not math.isfinite(x):
            raise ProtocolRefused(tr(f"{label} 里有非有限值 {it!r}", f"{label} contains the non-finite value {it!r}"))
        out.append(x)
    if not out:
        raise ProtocolRefused(tr(f"{label} 是空的", f"{label} is empty"))
    if len(out) > MAX_STEPS:
        raise ProtocolRefused(tr(f"{label} 有 {len(out)} 个点，超过上限 {MAX_STEPS}",
                                 f"{label} has {len(out)} points, more than the limit {MAX_STEPS}"))
    return out


def _frange(start: float, stop: float, step: float, label: str) -> list[float]:
    """Inclusive arithmetic range; direction follows start -> stop, ``step`` is a magnitude."""
    step = abs(step)
    if step <= 0:
        raise ProtocolRefused(tr(f"{label}: 步长必须非零", f"{label}: the step must be non-zero"))
    span = abs(stop - start)
    n = int(math.floor(span / step + 1e-9)) + 1
    if n > MAX_STEPS:
        raise ProtocolRefused(tr(f"{label}: {n} 个点超过上限 {MAX_STEPS}",
                                 f"{label}: {n} points exceed the limit {MAX_STEPS}"))
    sign = 1.0 if stop >= start else -1.0
    return [round(start + sign * step * i, 9) for i in range(n)]


def _series(params: dict, *, list_key: str, start_key: str, stop_key: str, step_key: str,
            label: str, default: tuple[float, float, float] | None = None) -> list[float]:
    """The step list: an explicit list, or start/stop/step (all three), or ``default``."""
    has_list = not _blank(params.get(list_key))
    rng = [params.get(k) for k in (start_key, stop_key, step_key)]
    n_rng = sum(0 if _blank(v) else 1 for v in rng)
    if has_list and n_rng:
        raise ProtocolRefused(tr(f"{list_key} 与 {start_key}/{stop_key}/{step_key} 只能给一种",
                                 f"give either {list_key} or {start_key}/{stop_key}/{step_key}, not both"))
    if has_list:
        return _number_list(params[list_key], list_key)
    if n_rng:
        if n_rng != 3:
            raise ProtocolRefused(tr(f"{start_key}/{stop_key}/{step_key} 要三个一起给",
                                     f"{start_key}/{stop_key}/{step_key} must all be given together"))
        a = _as_float(params, start_key, None)
        b = _as_float(params, stop_key, None)
        s = _as_float(params, step_key, None)
        return _frange(float(a), float(b), float(s), label)
    if default is not None:
        return _frange(*default, label)
    raise ProtocolRefused(tr(f"没有给 {label}：请给 {list_key}，或 {start_key}/{stop_key}/{step_key}",
                             f"no {label} given: pass {list_key}, or {start_key}/{stop_key}/{step_key}"))


# ── names ─────────────────────────────────────────────────────────────────────────


def _sig(x: float | None, n: int = 3) -> str:
    """``x`` with ``n`` significant digits, no exponent, no trailing zeros (``0.4217`` -> ``0.422``)."""
    if x is None or not math.isfinite(float(x)):
        return "NA"
    x = float(x)
    if x == 0:
        return "0"
    digits = max(0, n - int(math.floor(math.log10(abs(x)))) - 1)
    s = f"{x:.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _safe(text: Any, limit: int = 16) -> str:
    return "".join(c for c in str(text or "") if c.isalnum() or c in "-_")[:limit]


def _stem(*parts: str, tag: str = "") -> str:
    """Join the non-empty parts with ``_`` (+ the tag), capped at :data:`STEM_LIMIT` characters."""
    bits = [p for p in parts if p]
    if tag:
        bits.append(tag)
    return "_".join(bits)[:STEM_LIMIT].rstrip("_")


# ── spectrum analysis ────────────────────────────────────────────────────────────


def _running_median(y: np.ndarray, k: int = 5) -> np.ndarray:
    """Edge-padded running median (kills single-pixel cosmic spikes, keeps wide peaks)."""
    if y.size < k:
        return y.copy()
    pad = k // 2
    win = np.lib.stride_tricks.sliding_window_view(np.pad(y, pad, mode="edge"), k)
    return np.median(win, axis=1)


def _trapz(y: np.ndarray, x: np.ndarray) -> float:
    if x.size < 2:
        return 0.0
    return float(np.sum((y[1:] + y[:-1]) * np.diff(x)) / 2.0)


def feature_integral(axis_nm: Any, counts: Any, *, center_nm: float, window_nm: float,
                     search_nm: float, snr_min: float = 5.0) -> dict:
    """Locate the strongest feature within ``search_nm`` of ``center_nm`` and integrate it.

    * peak position: arg-max of a 5-point running median inside the search window (a single hot pixel
      cannot win; a peak sitting on the window edge is reported as not found);
    * background: median of everything farther than one window width from the peak (fallback: the 20th
      percentile of the spectrum when too little is left), noise = MAD of the same pixels;
    * intensity: trapezoid of ``counts - background`` over ``peak ± window/2`` (counts·nm).

    Never raises; ``found`` says whether the feature is above ``snr_min`` noise.
    """
    out: dict[str, Any] = {"found": False, "expected_nm": float(center_nm), "window_nm": float(window_nm),
                           "search_nm": float(search_nm)}
    try:
        y = np.asarray(counts, dtype=np.float64)
        ax = None if axis_nm is None else np.asarray(axis_nm, dtype=np.float64)
        if ax is None or ax.shape != y.shape:
            out["reason"] = "no wavelength axis"
            return out
        ok = np.isfinite(ax) & np.isfinite(y)
        if int(ok.sum()) < 8:
            out["reason"] = "too few finite pixels"
            return out
        ax, y = ax[ok], y[ok]
        order = np.argsort(ax)
        ax, y = ax[order], y[order]
        out["axis_range_nm"] = [float(ax[0]), float(ax[-1])]
        near = np.flatnonzero(np.abs(ax - center_nm) <= search_nm)
        if near.size == 0:
            out["reason"] = "expected position is outside the spectrometer window"
            return out
        sm = _running_median(y, 5)
        k = int(near[int(np.argmax(sm[near]))])
        x_pk = float(ax[k])
        half = max(window_nm, 0.0) / 2.0
        far = np.abs(ax - x_pk) > 2.0 * half
        if int(far.sum()) >= 10:
            bg = float(np.median(y[far]))
            sigma = 1.4826 * float(np.median(np.abs(y[far] - bg)))
        else:
            bg = float(np.percentile(y, 20))
            sigma = 1.4826 * float(np.median(np.abs(y - np.median(y))))
        sigma = max(sigma, math.sqrt(max(bg, 1.0)) * 0.1, 1e-12)
        inwin = np.abs(ax - x_pk) <= half
        height = float(sm[k]) - bg
        out.update(peak_nm=x_pk, peak_offset_nm=x_pk - float(center_nm), background=bg, noise=sigma,
                   snr=height / sigma, peak_counts=float(np.max(y[inwin])) if inwin.any() else float(y[k]),
                   peak_height=height, n_pixels=int(inwin.sum()))
        if int(inwin.sum()) >= 3:
            out["integral"] = _trapz(y[inwin] - bg, ax[inwin])
        else:
            out["integral"] = None
            out["reason"] = "window holds fewer than 3 pixels"
        at_edge = k in (int(near[0]), int(near[-1])) and near.size > 2
        out["at_search_edge"] = bool(at_edge)
        out["found"] = bool(height > 0 and height / sigma >= snr_min and not at_edge
                            and out.get("integral") is not None)
        if not out["found"] and "reason" not in out:
            out["reason"] = ("peak sits on the edge of the search window" if at_edge
                             else "no feature above the noise")
        return out
    except Exception as exc:  # noqa: BLE001 - analysis must never fail a measurement
        out["reason"] = f"{type(exc).__name__}: {exc}"
        return out


def loglog_fit(xs: Iterable[Any], ys: Iterable[Any]) -> dict | None:
    """Least-squares ``log10 y = a + s·log10 x`` over the positive finite pairs (>= 3 needed)."""
    pts = []
    for x, y in zip(xs, ys):
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fx) and math.isfinite(fy) and fx > 0 and fy > 0:
            pts.append((math.log10(fx), math.log10(fy)))
    if len(pts) < 3:
        return None
    lx = np.array([p[0] for p in pts])
    ly = np.array([p[1] for p in pts])
    sxx = float(np.sum((lx - lx.mean()) ** 2))
    if sxx <= 0:
        return None
    slope = float(np.sum((lx - lx.mean()) * (ly - ly.mean())) / sxx)
    icpt = float(ly.mean() - slope * lx.mean())
    resid = ly - (icpt + slope * lx)
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((ly - ly.mean()) ** 2))
    n = len(pts)
    se = math.sqrt(ss_res / (n - 2) / sxx) if n > 2 else None
    return {"slope": slope, "intercept_log10": icpt, "stderr": se, "n": n,
            "r2": (1.0 - ss_res / ss_tot) if ss_tot > 0 else None}


def analyzer_reading(theta_deg: float, mode: str, *, scale: float, zero: float, crs_offset: float) -> float:
    """Analyzer reading for incident polarisation ``theta_deg`` (see the module docstring).

    ``scale·theta + zero`` (+ ``crs_offset`` for crossed). A result outside [0, 360) is wrapped — the
    wheel is physically periodic in 360°, so this is the same position, not a clamp.
    """
    r = scale * float(theta_deg) + zero + (crs_offset if mode == "crs" else 0.0)
    if r >= 360.0 or r < 0.0:
        r = r % 360.0
    return round(r, 9)


def _angle_diff(a: float, b: float) -> float:
    """Signed smallest difference a-b on a 360° circle."""
    return ((float(a) - float(b) + 180.0) % 360.0) - 180.0


def _option_covers(option: dict, nm: float) -> bool | None:
    rng = (option.get("meta") or {}).get("valid_nm")
    if not rng:
        return None
    return float(rng[0]) <= float(nm) <= float(rng[1])


def _csv_cell(v: Any) -> Any:
    if isinstance(v, float):
        return "" if not math.isfinite(v) else repr(v)
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(json_safe(v), ensure_ascii=False)
    return "" if v is None else v


def _stack(frames: list[np.ndarray], axis_nm: np.ndarray | None) -> tuple[np.ndarray, bool]:
    """Row 0 = axis (NaN when unknown), rows 1… = frames, NaN-padded to one width."""
    width = max([len(f) for f in frames] + ([len(axis_nm)] if axis_nm is not None else []))
    ragged = len({len(f) for f in frames}) > 1 or (
        axis_nm is not None and len(axis_nm) != len(frames[0]))
    out = np.full((len(frames) + 1, width), np.nan, dtype=np.float64)
    if axis_nm is not None:
        out[0, :len(axis_nm)] = axis_nm
    for i, f in enumerate(frames, start=1):
        out[i, :len(f)] = np.asarray(f, dtype=np.float64)
    return out, ragged


# ── common parameters ───────────────────────────────────────────────────────────


def _spec(name: str, type_: str, zh: str, en: str, *, unit: str = "", required: bool = False,
          default: Any = None, lo: float | None = None, hi: float | None = None,
          allowed: list | None = None) -> ParameterSpec:
    return ParameterSpec(name=name, type=type_, description=tr(zh, en), unit=unit, required=required,
                         default=default, min_value=lo, max_value=hi, allowed_values=allowed)


def _common_specs() -> list[ParameterSpec]:
    """Parameters every protocol has (acquisition, devices that only get *read*, run control)."""
    return [
        _spec("microscope_id", "str", "显微镜（同时是光谱仪）的设备 id；留空 = 唯一一台（没有显微镜时退到唯一一台光谱仪）",
              "Microscope (also the spectrometer) device id; empty = the only one (falls back to the only spectrometer when there is no microscope)",
              default=""),
        _spec("integration_s", "float", "每帧积分时间（秒），也是写进 sidecar 的条件", "Integration time per frame (s), also recorded as a condition in the sidecar",
              unit="s", default=1.0, lo=0.001, hi=3600.0),
        _spec("accumulations", "int", "累加次数（作为帧数交给光谱仪）", "Number of accumulations (handed to the spectrometer as the frame count)",
              default=1, lo=1, hi=10000),
        _spec("save_raw", "bool", "让设备端同时存它自己的原始文件（含义由驱动决定，如 WITec 项目）",
              "Let the device also save its own raw file (meaning decided by the driver, e.g. the WITec project)", default=True),
        _spec("beam_path", "str", "取谱前把光路设成这个状态；none = 不动", "Beam path to set before acquiring; none = leave it",
              default="raman", allowed=["raman", "confocal", "white_light", "none"]),
        _spec("power_meter_id", "str", "功率计设备 id；留空/none = 不用；auto = 唯一一台。功率计必须真的在光路里（没有常驻取样片时它读到的是别的东西）",
              "Power meter device id; empty/none = not used; auto = the only one. The meter must really be in the beam (without a resident pick-off it reads something else)",
              default=""),
        _spec("power_averages", "int", "每次读功率平均的次数", "Readings averaged per power measurement", default=5, lo=1, hi=1000),
        _spec("temperature_id", "str", "温控设备 id；留空/none = 不记录温度；auto = 唯一一台（只读，除 TemperatureSeries 外不会下设定）",
              "Temperature controller id; empty/none = temperature not recorded; auto = the only one (read only, except for TemperatureSeries)",
              default=""),
        _spec("settle_s", "float", "改变光源/设置之后等多久再取谱（可取消）", "Wait after changing the source/settings before acquiring (cancellable)",
              unit="s", default=0.5, lo=0.0, hi=600.0),
        _spec("tag", "str", "附加在文件名上的短标签（字母数字 - _，最多 16 个字符）", "Short label appended to file names (letters, digits, - _, at most 16 characters)",
              default=""),
        _spec("saturation_counts", "float", "计数 ≥ 此值时标记饱和；0 = 不检查", "Flag saturation when a pixel reaches this many counts; 0 = no check",
              default=0.0, lo=0.0, hi=1e12),
        _spec("prompt_timeout_s", "float", "每个操作员提示的等待上限（秒）；超时 = 干净停下", "Upper bound for each operator prompt (s); a timeout stops the run cleanly",
              unit="s", default=1800.0, lo=0.05, hi=86400.0),
        _spec("ask_write_access", "bool", "WITec 没给写权限时弹窗请操作员点 COM Automation，然后重试（最多两次）；快门没能确认关闭不算缺写权限：那是直接停下，不弹窗",
              "When WITec has not handed over write access, ask the operator to press COM Automation and retry (at most twice); a shutter that "
              "could not be confirmed closed is not a missing grant: that stops the run, no prompt", default=True),
        _spec("release_write_access", "bool", "结束时把 WITec 写权限还回去（设计决策 D5）；连着跑几个协议、不想每次重新点 COM Automation 时设 false",
              "Hand the WITec write access back when the protocol ends (design decision D5); set false when running several protocols in a row so COM Automation need not be pressed again each time",
              default=True),
        _spec("max_total_s", "float", "整次运行的时间上限（含等人）；超过就停在当前点并保存", "Time limit of the whole run (operator waits included); beyond it the run stops and saves",
              unit="s", default=86400.0, lo=10.0, hi=604800.0),
        _spec("dry_run", "bool", "**试跑**：只生成并返回执行计划（步骤、预计时长、哪些步骤要人动手）并检查参数与限值；不碰硬件、不弹窗、不写文件",
              "**Dry run**: only build and return the execution plan (steps, estimated time, which steps need a person) and check parameters and limits; touches no hardware, posts no prompt, writes no file",
              default=False),
    ]


def _source_specs(*, external: bool = True, witec: bool = True) -> list[ParameterSpec]:
    out: list[ParameterSpec] = []
    if external:
        out += [
            _spec("laser_id", "str", "外部光源设备 id；留空/none = 不用；auto = 唯一一台", "External source device id; empty/none = not used; auto = the only one", default=""),
            _spec("manage_emission", "bool", "由协议用软件开/关外部光源出光（需要设备 allow_emission_control；laser_area_safe 签署是所有协议都要的）；false = 操作员手动开，协议只在开始时请他确认。"
                  "设备 allow_emission_control=false 时，光源出光期间软件不加光：光源在出光（或读不到状态）而计划里的通道幅度/功率高于当前值，会被整体拒绝，请开始前先在厂商软件里手动设好（协议只会调暗、调颜色）；光源还关着则不拒",
                  "Let the protocol switch the external source on/off in software (needs allow_emission_control in the device config, besides the laser_area_safe attestation every protocol needs); false = the operator switches it on by hand and is asked to confirm at the start. "
                  "With allow_emission_control=false software does not add light while the source emits: when the source is emitting (or its state cannot be read) and the plan's channel amplitude / power is above the present value, the plan is refused up front - set it by hand in the vendor software before starting (the protocol only dims and tunes the colour); a source that is still off is not refused",
                  default=True),
            _spec("ask_attest_laser", "bool", "laser_area_safe 签署无效/过期时弹窗请操作员签署（开始前、每步、每次曝光前都查）；false = 拒绝开始 / 干净停下", "When the laser_area_safe attestation is missing/expired, ask the operator to sign (checked before the start, at every step and before every exposure); false = refuse to start / stop cleanly",
                  default=True),
            _spec("warmup_s", "float", "软件开出光后等多久再开始（光源预热，可取消）", "Wait after switching the emission on in software (source warm-up, cancellable)",
                  unit="s", default=0.0, lo=0.0, hi=3600.0),
        ]
    if witec:
        out += [
            _spec("witec_laser", "str", "用 WITec 自带激光（名字或波长，如 '532'）：协议选中它，只在每次取谱期间开快门；留空 = 不用", "Use a WITec laser (name or wavelength, e.g. '532'): selected by the protocol, shutter open only during each acquisition; empty = not used",
                  default=""),
            _spec("witec_power_mw", "float", "WITec 激光功率（mW，超出 microscope_limits 即拒绝）；不给 = 不改", "WITec laser power (mW, refused beyond microscope_limits); not given = leave unchanged",
                  unit="mW", lo=0.0, hi=1000.0),
        ]
    return out


def _parse_common(params: dict) -> SimpleNamespace:
    c = SimpleNamespace()
    c.dry_run = _as_bool(params, "dry_run", False)
    c.tag = _safe(_as_str(params, "tag", ""), 16)
    c.integration_s = _as_float(params, "integration_s", 1.0, lo=0.0, hi=3600.0, lo_open=True)
    c.accumulations = _as_int(params, "accumulations", 1, lo=1, hi=10000)
    c.save_raw = _as_bool(params, "save_raw", True)
    c.prompt_timeout_s = _as_float(params, "prompt_timeout_s", 1800.0, lo=0.05, hi=86400.0)
    c.ask_write_access = _as_bool(params, "ask_write_access", True)
    c.release_write_access = _as_bool(params, "release_write_access", True)
    c.max_total_s = _as_float(params, "max_total_s", 86400.0, lo=10.0, hi=604800.0)
    c.saturation_counts = _as_float(params, "saturation_counts", 0.0, lo=0.0, hi=1e12)
    c.settle_s = _as_float(params, "settle_s", 0.5, lo=0.0, hi=600.0)
    c.microscope_id = _as_str(params, "microscope_id", "")
    c.beam_path = _as_choice(params, "beam_path", "raman", ("raman", "confocal", "white_light", "none"))
    c.power_meter_id = _as_str(params, "power_meter_id", "")
    c.power_averages = _as_int(params, "power_averages", 5, lo=1, hi=1000)
    c.temperature_id = _as_str(params, "temperature_id", "")
    return c


def _parse_source(params: dict, c: SimpleNamespace) -> None:
    c.laser_id = _as_str(params, "laser_id", "")
    c.manage_emission = _as_bool(params, "manage_emission", True)
    c.ask_attest_laser = _as_bool(params, "ask_attest_laser", True)
    c.warmup_s = _as_float(params, "warmup_s", 0.0, lo=0.0, hi=3600.0)
    c.witec_laser = _as_str(params, "witec_laser", "")
    c.witec_power_mw = _as_float(params, "witec_power_mw", None, lo=0.0, hi=1000.0)
    if c.witec_power_mw is not None and not c.witec_laser:
        raise ProtocolRefused(tr("witec_power_mw 需要同时给 witec_laser", "witec_power_mw needs witec_laser as well"))
    c.beam_off_during_swap = True


# ── plan ─────────────────────────────────────────────────────────────────────────


class _Step:
    """One planned step: ``conditions`` are what the plan intends, ``manual`` tags what needs a person."""

    def __init__(self, index: int, label: str, conditions: dict, manual: list[str] | None = None,
                 est_s: float = 0.0):
        self.index = index
        self.label = label
        self.conditions = conditions
        self.manual = list(manual or [])
        self.est_s = float(est_s)

    def as_dict(self) -> dict:
        return {"index": self.index, "label": self.label, "conditions": json_safe(self.conditions),
                "manual": list(self.manual), "est_s": round(self.est_s, 1)}


class _Plan:
    def __init__(self, steps: list[_Step], problems: list[str] | None = None, notes: list[str] | None = None):
        self.steps = steps
        self.problems = list(problems or [])
        self.notes = list(notes or [])
        #: prompts that come before the first step (the laser attestation)
        self.pre_manual: list[str] = []

    @property
    def est_machine_s(self) -> float:
        return sum(s.est_s for s in self.steps)

    @property
    def n_manual(self) -> int:
        return len(self.pre_manual) + sum(len(s.manual) for s in self.steps)


# ── devices ───────────────────────────────────────────────────────────────────────


class _Rig:
    """The devices one run uses (built lazily by the registry; nothing is connected here)."""

    def __init__(self) -> None:
        self.reg: Any = None
        self.mic_id: str | None = None
        self.mic: Any = None
        self.spec_id: str = ""
        self.spec: Any = None
        self.laser_id: str | None = None
        self.laser: Any = None
        self.pm_id: str | None = None
        self.pm: Any = None
        self.tc_id: str | None = None
        self.tc: Any = None
        self.info: dict[str, dict] = {}


def _kind_of(reg: Any, dev_id: str) -> str:
    entry = reg.device_config(dev_id)
    if entry is None:
        raise ProtocolRefused(tr(f"没有设备 {dev_id!r}", f"there is no device {dev_id!r}"))
    return inst_config.device_kind(str(entry.get("type")))


def _pick(reg: Any, kind: str, want: str, given: str, label: str) -> str | None:
    """Device id for *kind*. ``want``: ``required`` | ``optional`` | ``none``.

    ``given``: explicit id | ``auto`` (the only enabled device of that kind) | ``none`` | empty
    (required: same as auto; optional: not used).
    """
    if want == "none":
        return None
    g = (given or "").strip()
    if g.lower() == "none" or (not g and want == "optional"):
        return None
    if not g or g.lower() == "auto":
        try:
            return resolve_device(reg, kind=kind, role=None, device_id=None)
        except InstrumentError as exc:
            raise ProtocolRefused(tr(f"{label}: {exc}", f"{label}: {exc}")) from exc
    got = _kind_of(reg, g)
    if got != kind:
        raise ProtocolRefused(tr(f"{label} {g!r} 是 {got}，不是 {kind}", f"{label} {g!r} is a {got}, not a {kind}"))
    return g


def _pick_microscope(reg: Any, given: str) -> tuple[str, str]:
    g = (given or "").strip()
    if g and g.lower() != "auto":
        kind = _kind_of(reg, g)
        if kind not in ("microscope", "spectrometer"):
            raise ProtocolRefused(tr(f"显微镜/光谱仪 {g!r} 是 {kind}", f"microscope/spectrometer {g!r} is a {kind}"))
        return g, kind
    try:
        return resolve_device(reg, kind="microscope", role=None, device_id=None), "microscope"
    except InstrumentUnavailable:
        pass                       # no microscope configured: a plain spectrometer will do
    except InstrumentError as exc:
        raise ProtocolRefused(str(exc)) from exc
    try:
        return resolve_device(reg, kind="spectrometer", role=None, device_id=None), "spectrometer"
    except InstrumentError as exc:
        raise ProtocolRefused(str(exc)) from exc


def _resolve_rig(c: SimpleNamespace, *, laser: str = "none", temperature: str = "optional",
                 require_mic: bool = False) -> _Rig:
    reg = get_instrument_registry()
    rig = _Rig()
    rig.reg = reg
    try:
        rig.mic_id, kind = _pick_microscope(reg, c.microscope_id)
        rig.spec_id = rig.mic_id
        rig.mic = reg.microscope(rig.mic_id) if kind == "microscope" else None
        rig.spec = reg.spectrometer(rig.mic_id)
        if require_mic and rig.mic is None:
            raise ProtocolRefused(tr(f"{rig.mic_id} 只是光谱仪，这个协议要分析器/快门，需要显微镜",
                                     f"{rig.mic_id} is only a spectrometer; this protocol needs a microscope (analyzer/shutter)"))
        rig.laser_id = _pick(reg, "laser", laser, getattr(c, "laser_id", ""), tr("外部光源", "external source"))
        rig.laser = reg.laser(rig.laser_id) if rig.laser_id else None
        rig.pm_id = _pick(reg, "power_meter", "optional", c.power_meter_id, tr("功率计", "power meter"))
        rig.pm = reg.power_meter(rig.pm_id) if rig.pm_id else None
        rig.tc_id = _pick(reg, "temperature", temperature, c.temperature_id, tr("温控", "temperature controller"))
        rig.tc = reg.temperature_controller(rig.tc_id) if rig.tc_id else None
    except ProtocolRefused:
        raise
    except InstrumentError as exc:
        raise ProtocolRefused(str(exc)) from exc
    for role, dev_id in (("microscope", rig.mic_id if rig.mic is not None else None),
                         ("spectrometer", rig.spec_id), ("laser", rig.laser_id),
                         ("power_meter", rig.pm_id), ("temperature", rig.tc_id)):
        if dev_id:
            entry = reg.device_config(dev_id) or {}
            rig.info[role] = {"id": dev_id, "name": entry.get("name", dev_id), "type": entry.get("type")}
    return rig


def _match_laser(lasers: list[dict], ref: Any) -> dict | None:
    """The WITec laser a reference (index / name / wavelength in nm) points at."""
    for las in lasers:
        if (str(ref) == str(las.get("index")) or str(ref) == str(las.get("name"))
                or (las.get("wavelength_nm") is not None
                    and str(ref) == str(int(round(float(las["wavelength_nm"])))))):
            return las
    return None


# ── the running protocol ─────────────────────────────────────────────────────────


def _is_witec(exc: BaseException, name: str) -> bool:
    """Is *exc* the WITec driver's exception class *name*? Looked up lazily, by type: a rig without the WITec driver
    has nothing of that type to raise, and the exception classes cannot be told apart by their bases (``ShutterNotClosed``
    is a ``WriteAccessDenied``, ``StopInProgress`` a ``MotionAborted``)."""
    try:
        from mast.instruments.witec import microscope as wm
    except Exception:  # noqa: BLE001
        return False
    cls = getattr(wm, name, None)
    return isinstance(cls, type) and isinstance(exc, cls)


def _stopped_elsewhere(what: str, exc: BaseException) -> ProtocolHalt:
    """The driver cancelled or refused the action because a stop was asked for - after it began, or while the stop is
    under way: somebody else's stop, so the same clean stop as one the epochs show."""
    return ProtocolHalt("stopped_externally", tr(
        f"{what}: 被别处要求的停止打断，没有执行（{exc}）—— 干净停下；之后不再开快门、不再开光源、不再下温度设定",
        f"{what}: pre-empted by a stop asked for elsewhere ({exc}) — stopping cleanly; "
        "from here on no shutter is reopened, no source switched on and no setpoint sent"))


def _classify(exc: BaseException, what: str) -> ProtocolHalt:
    """Map a device exception to a clean halt (kind = identity string)."""
    if isinstance(exc, ProtocolHalt):
        return exc
    if _is_witec(exc, "StopInProgress"):       # a MotionAborted: told apart from an abort BEFORE that branch
        return _stopped_elsewhere(what, exc)
    if _is_witec(exc, "ShutterNotClosed"):     # a WriteAccessDenied, but the light may be on: what the shutter fault ends with
        return ProtocolHalt("shutter_not_closed", tr(f"{what}: WITec 快门没能确认关闭 —— {exc}",
                                                     f"{what}: the WITec shutter could not be confirmed closed — {exc}"))
    if isinstance(exc, MotionAborted):
        return ProtocolHalt("aborted", f"{what}: {exc}")
    if isinstance(exc, (MicroscopeLimitError, LaserLimitError, TemperatureLimitError,
                        SpectrometerLimitError, PowerMeterError)):
        return ProtocolHalt("limit", tr(f"{what}: 越限被拒 —— {exc}", f"{what}: refused by a limit — {exc}"))
    if isinstance(exc, EmissionControlDisabled):
        return ProtocolHalt("emission_refused", tr(f"{what}: 被设备配置拒绝 —— {exc}",
                                                   f"{what}: refused by the device config — {exc}"))
    if isinstance(exc, TemperatureWriteDisabled):
        return ProtocolHalt("device_disabled", tr(f"{what}: 被设备配置拒绝 —— {exc}",
                                                  f"{what}: refused by the device config — {exc}"))
    if isinstance(exc, OperationNotSupported):
        return ProtocolHalt("not_supported", tr(f"{what}: 驱动不支持 —— {exc}", f"{what}: not supported by the driver — {exc}"))
    if isinstance(exc, (EmissionPreempted, TemperaturePreempted)):
        # the driver cancelled the switch-on / the setpoint because a stop was asked for after it started (and put
        # back what it had switched on)
        return _stopped_elsewhere(what, exc)
    if isinstance(exc, InstrumentError):
        return ProtocolHalt("device_error", f"{what}: {exc}")
    return ProtocolHalt("error", f"{what}: {type(exc).__name__}: {exc}")


#: halt kinds that are a deliberate, understood clean stop (everything else is a failure)
_CLEAN_STOPS = frozenset({
    "operator_declined", "operator_timeout", "operator_cancelled", "laser_not_attested",
    "waveplate_not_covering", "center_not_settable", "write_access", "polarizer_mismatch",
    "temperature_timeout", "temperature_stopped", "stopped_externally", "time_budget", "emission_refused",
    "device_disabled", "scope_changed",
})


def _declined(ans: Any) -> bool:
    """An answered attest that says no: a 'no' choice, or a false value (what a UI sends for 'do not sign')."""
    if str(getattr(ans, "choice", "") or "").lower() in ("no", "cancel", "false"):
        return True
    v = getattr(ans, "value", None)
    return v is False or (v is not None and str(v).strip().lower() in ("false", "no", "0"))


def _slot_equals(store: Any, slot_id: str, value: float) -> bool:
    sv = store.get(slot_id)
    return sv is not None and isinstance(sv.value, (int, float)) and abs(float(sv.value) - float(value)) <= 1e-9


def _state_changed(before: dict, after: dict) -> bool:
    def key(snap: dict) -> dict:
        return {k: (v.get("value"), v.get("set_at")) for k, v in (snap or {}).items() if isinstance(v, dict)}
    return key(before) != key(after)


class _Swap:
    """What :meth:`_Run.begin_swap` did to make the beam path safe for a person's hands.

    The swap prompt quotes it — only what was read back or confirmed, never what was merely attempted — and
    :meth:`_Run.end_swap` undoes it afterwards. ``source`` is one of ``none`` (no external source in this run),
    ``unmanaged`` (the light is the operator's own: the protocol cannot touch it), ``not_emitting`` (read back
    dark already), ``off_by_software`` (switched off, read back off) or ``off_by_hand`` (software could not take
    it dark, so the operator switched it off or blocked the beam and confirmed)."""

    def __init__(self) -> None:
        self.laser_id: str | None = None
        #: "" without a microscope; "closed" = the (selected) shutter, confirmed closed; "all" = every laser's shutter
        #: (``close_all_shutters``), ``shutters`` being the driver's report
        self.shutter = ""
        self.shutters: dict = {}
        self.source = "none"
        self.read_back = ""          # what the source reported when software could not confirm it off

    def describe(self) -> tuple[str, str]:
        """(zh, en) sentence for the prompt, from the facts only."""
        zh: list[str] = []
        en: list[str] = []
        if self.shutter == "closed":
            zh.append("WITec 快门已关（读回确认）。")
            en.append("The WITec shutter is closed (confirmed by read-back).")
        elif self.shutter == "all":
            rep = self.shutters
            closed = [str(x) for x in list(rep.get("closed") or []) + list(rep.get("already_closed") or [])]
            zh_list = "、".join(closed) or "驱动没有逐台列出"
            en_list = ", ".join(closed) or "the driver gave no list"
            unchecked = str(rep.get("skipped") or "")
            if rep.get("scope") == "selected_laser_only":
                # close_every_laser is off in the device config: the others were not looked at, and the sentence must not say they were
                zh.append(f"只关了选中那台激光的快门（读回确认：{zh_list}）；其他激光的快门没有去看（设备配置里 close_every_laser 是关的）"
                          "——伸手进光路之前，请在 WITec Control 里把它们关上，或物理挡光。")
                en.append(f"Only the selected laser's shutter is closed (confirmed by read-back: {en_list}); the other lasers' shutters were "
                          "not looked at (close_every_laser is off in the device config) — before you reach in, close them in WITec Control "
                          "or block those beams.")
            elif unchecked or rep.get("shutter_confirmed") is False:
                # begin_swap stops the run on such a report; kept truthful for any other caller
                zh.append(f"不是每台激光的快门都查过：{unchecked or '驱动没有全部确认'}（读回确认已关：{zh_list}）。")
                en.append(f"Not every laser's shutter was checked: {unchecked or 'the driver did not confirm all of them'} "
                          f"(read back closed: {en_list}).")
            else:
                zh.append(f"WITec 每台激光的快门都已关（读回确认：{zh_list}）。")
                en.append(f"Every WITec laser shutter is closed (confirmed by read-back: {en_list}).")
            nosw = [str(x) for x in rep.get("no_software_shutter") or []]
            if nosw:
                zh.append(f"没有软件快门的激光（用它的话请物理挡光）：{'、'.join(nosw)}。")
                en.append(f"Lasers with no software shutter (block the beam physically if one of them is in use): {', '.join(nosw)}.")
        sid = self.laser_id
        if self.source == "off_by_software":
            zh.append(f"光源 {sid} 已由软件关出光（读回：已关）。")
            en.append(f"The source {sid} has been switched off by software (read back: off).")
        elif self.source == "off_by_hand":
            zh.append(f"软件没能确认光源 {sid} 已关（读回：{self.read_back}）；你已确认手动关了它或挡住了光路。")
            en.append(f"Software could not confirm the source {sid} off (read back: {self.read_back}); you confirmed that you "
                      "switched it off or blocked the beam.")
        elif self.source == "not_emitting":
            zh.append(f"光源 {sid} 读回没有在出光。")
            en.append(f"The source {sid} reads not emitting.")
        elif self.source == "unmanaged":
            zh.append("这个协议控制不了你的光源：伸手进光路之前请先关掉光源或挡住光路。")
            en.append("This protocol does not control your light source: switch it off or block the beam before you reach into the beam path.")
        return "".join(zh), " ".join(en)


class _Run:
    """One execution of a protocol: prompts, devices, files, summary and the safe-direction finish."""

    def __init__(self, proto: "_Protocol", context: Any, params: dict, cfg: SimpleNamespace,
                 rig: _Rig, plan: _Plan):
        self.proto = proto
        self.name = proto.NAME
        self.short = proto.SHORT
        self.ctx = context
        self.params = params
        self.cfg = cfg
        self.rig = rig
        self.plan = plan
        self.probe = abort_probe(context)
        self.run_id = new_run_id(self.short)
        self.started_utc = _utc()
        self.finished_utc = ""
        self.t0 = time.monotonic()
        #: the experiment/sample this run belongs to; if it changes mid-run nothing more is filed under the new one
        self.scope0 = scope_ids()
        self.dir: Path | None = None
        self.live: LiveStream | None = None
        self.rows: list[dict] = []
        self.step_status: dict[int, str] = {}
        self.cur: _Step | None = None
        self.prompts_log: list[dict] = []
        self.warnings: list[str] = []
        self.stems: set[str] = set()
        self.data_paths: list[str] = []
        self.all_stored = True
        self.status = "running"
        self.stop_kind = ""
        self.stop_reason = ""
        self.stop_phase = ""
        self.stopped_at: int | None = None
        self.phase = "prepare"
        self.cleanup_report: dict = {}
        self.analysis: dict = {}
        self.final_paths: list[str] = []
        self.final_stored: dict = {}
        self.summary_error = ""
        self.emission_by_software = False
        self.source_mode = ""
        #: set (to the reason) once a WITec shutter close could not be confirmed: from then on nothing but the
        #: halt happens — no prompt, no new action (see :meth:`close_shutter`)
        self.shutter_fault = ""
        #: the temperature controller has been sent a setpoint by this run (so a "stopped" state is somebody else's)
        self.tc_commanded = False
        #: the devices this run uses and their ``external_stop_epoch`` when it began (see :meth:`watch_stops`)
        self.stop_watch: dict[str, tuple[Any, int]] = {}
        #: the text, once a device has counted a stop asked for from outside the run: sticky (see :meth:`external_stop`)
        self.stopped_externally = ""
        self.center_now: float | None = None
        self.center_driver_ok = True
        self.center_log: list[dict] = []
        self.commands_temperature = False
        self.last_axis: np.ndarray | None = None
        self.last_axis_ver: Any = None
        self.n_prompts = 0
        self.next_index = 1
        self._warn_counts: dict[str, int] = {}
        #: protocol state shared between steps
        self.feature_nm: float | None = None
        self.last_incident_requested: float | None = None
        self.last_incident_actual: float | None = None
        self.power_mode = ""
        self.meter_wavelength: float | None = None

    # -- state ------------------------------------------------------------------

    @property
    def cur_index(self) -> int | None:
        return self.cur.index if self.cur is not None else None

    def elapsed_s(self) -> float:
        return time.monotonic() - self.t0

    def aborted(self) -> bool:
        return bool(self.probe()) if self.probe is not None else False

    def over_budget(self) -> bool:
        return self.elapsed_s() > self.cfg.max_total_s

    def _abort_flag(self) -> bool:
        # also what ends a wait for the operator: a stop asked for from outside cancels the prompt
        return self.aborted() or self.over_budget() or bool(self.external_stop())

    def _abort_reason(self) -> str:
        fn = getattr(self.ctx, "abort_reason", None)
        try:
            return str(fn()) if callable(fn) else ""
        except Exception:  # noqa: BLE001
            return ""

    def raise_if_shutter_fault(self) -> None:
        if self.shutter_fault:
            raise ProtocolHalt("shutter_not_closed", self.shutter_fault, phase=self.phase)

    @staticmethod
    def _epoch_of(dev: Any) -> int:
        try:
            return int(getattr(dev, "external_stop_epoch", 0) or 0)
        except Exception:  # noqa: BLE001 - unreadable counts as 0
            return 0

    def watch_stops(self) -> None:
        """Record the ``external_stop_epoch`` of every device this run uses (microscope, external source, temperature
        controller; unreadable = 0). A stop that somebody else asks for later changes one of them; this run's own
        closing and cleanup never do (they do not call ``note_external_stop``). Not ``Laser.off_epoch`` /
        ``TemperatureController.stop_epoch``: those count EVERY off / stop, the run's own included, and exist to
        cancel a switch-on or a setpoint that is queued (it then raises ``EmissionPreempted`` /
        ``TemperaturePreempted``, which :func:`_classify` turns into the same clean stop)."""
        rig = self.rig
        for label, dev in (("microscope", rig.mic), ("light source", rig.laser), ("temperature controller", rig.tc)):
            if dev is not None:
                self.stop_watch[label] = (dev, self._epoch_of(dev))

    def external_stop(self) -> str:
        """The text (``""`` = none) saying which device counted a stop from outside the run since it began. Sticky:
        once seen it stays, whatever the counters do afterwards."""
        if self.stopped_externally:
            return self.stopped_externally
        for label, (dev, epoch0) in self.stop_watch.items():
            now = self._epoch_of(dev)
            if now != epoch0:
                reason = str(getattr(dev, "external_stop_reason", "") or "")
                ident = str(getattr(dev, "device_id", "") or label)
                self.stopped_externally = tr(
                    f"收到外部停止请求：{label} {ident}（第 {now - epoch0} 次，原因：{reason or '没写'}）—— 干净停下；之后不再开快门、不再开光源、不再下温度设定（终点温度也不发）",
                    f"a stop was asked for from outside this run: {label} {ident} (#{now - epoch0}, reason: {reason or 'none given'}) — stopping cleanly; "
                    "the shutter, the source and the temperature controller are not touched again (no final setpoint either)")
                self.warn(self.stopped_externally)
                return self.stopped_externally
        return ""

    def raise_if_stopped_externally(self) -> None:
        why = self.external_stop()
        if why:
            raise ProtocolHalt("stopped_externally", why, phase=self.phase)

    def halt_for(self, exc: BaseException, what: str) -> ProtocolHalt:
        """:func:`_classify`, plus what only the run knows about a stop - the one place every device exception goes
        through. A device call that is cut or refused because the station is being stopped (a ``MotionAborted``, the
        WITec's ``StopInProgress``, a pre-empted switch-on or setpoint) or that fails right after someone stopped it (the
        WITec acquisition that finds its sequencer idle) is

        * ``aborted`` when an abort was asked for (the operator, the system, the E-STOP's latch): an abort outranks, as
          in :meth:`check`;
        * ``stopped_externally`` when a stop from outside was counted since the run began - the stop skills count BEFORE
          they stop, so by the time the device raises the counter has moved - with who asked in the text and the
          device's own words kept;
        * what :func:`_classify` says otherwise: a ``StopInProgress`` or a pre-empted action is ``stopped_externally``
          by its type (``registry.stop_all`` does not count), a plain ``MotionAborted`` is ``aborted`` and any other
          device error stays one.

        Refusals (limits, config, not supported) and unexpected errors are never relabelled."""
        halt = _classify(exc, what)
        if isinstance(exc, ProtocolHalt) or halt.kind not in ("aborted", "device_error", "stopped_externally"):
            return halt
        words = f"{what}: {exc}"
        if halt.kind != "device_error" and self.aborted():
            return ProtocolHalt("aborted", words, phase=halt.phase)
        if not self.aborted():
            why = self.external_stop()
            if why:
                return ProtocolHalt("stopped_externally", f"{why} [{words}]", phase=halt.phase)
        return halt

    def check(self) -> None:
        self.raise_if_shutter_fault()           # before anything else: with a shutter that may be open nothing goes on
        if self.aborted():
            why = self._abort_reason()
            raise ProtocolHalt("aborted", tr("操作员/系统中止" + (f"（{why}）" if why else ""),
                                             "aborted by the operator/system" + (f" ({why})" if why else "")),
                               phase=self.phase)
        self.raise_if_stopped_externally()      # in front of every hardware call: nothing is reopened, resent or switched on
        if self.over_budget():
            raise ProtocolHalt("time_budget", tr(f"超过整次运行的时间上限 {self.cfg.max_total_s:.0f} s",
                                                 f"the run exceeded its time budget of {self.cfg.max_total_s:.0f} s"),
                               phase=self.phase)
        if scope_ids() != self.scope0:
            raise ProtocolHalt("scope_changed", tr(
                "运行期间当前实验/样品被切换了 —— 停下，免得后面的谱记到别的样品名下", 
                "the active experiment/sample was switched during the run — stopping so that no further spectrum is filed under another sample"),
                phase=self.phase)

    def sleep(self, seconds: float, chunk: float = 0.05) -> None:
        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            self.check()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(chunk, left))

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)
            logger.warning("%s %s: %s", self.name, self.run_id, text)

    def warn_limited(self, key: str, text: str, limit: int = 10) -> None:
        """A recurring kind of warning is listed ``limit`` times, then summarised once."""
        n = self._warn_counts.get(key, 0) + 1
        self._warn_counts[key] = n
        if n <= limit:
            self.warn(text)
        elif n == limit + 1:
            self.warn(tr(f"（'{key}' 类警告超过 {limit} 条，后面的不再逐条列出）",
                         f"(more than {limit} '{key}' warnings; the rest are not listed one by one)"))

    # -- hardware calls ----------------------------------------------------------

    def hw(self, what: str, fn: Callable, *args: Any, passthrough: tuple = (), check: bool = True,
           **kwargs: Any) -> Any:
        """Call a device method; turn its exceptions into clean halts.

        ``WriteAccessDenied`` (the operator has not pressed COM Automation) is answered with a prompt
        and a bounded number of retries - except the WITec driver's ``ShutterNotClosed``, which is a
        ``WriteAccessDenied`` too but means a shutter that could not be confirmed closed: the light may be on, so
        ``stop_all``, the fault is recorded and the run halts with ``shutter_not_closed``, nobody is asked to press
        anything. ``passthrough`` exception types are re-raised untouched.
        ``check=False`` skips the abort test in front of the call: used for reads that finish a step whose
        exposure is already in hand — an abort must not throw a measured spectrum away.
        """
        attempts = 0
        while True:
            if check:
                self.check()
            try:
                return fn(*args, **kwargs)
            except passthrough:
                raise
            except ProtocolHalt:
                raise
            except WriteAccessDenied as exc:
                if _is_witec(exc, "ShutterNotClosed"):
                    self._shutter_fault(what, f"{type(exc).__name__}: {exc}")
                    self.raise_if_shutter_fault()
                attempts += 1
                if not self.cfg.ask_write_access or attempts > 2:
                    raise ProtocolHalt("write_access", tr(f"{what}: WITec 没有交出写权限 —— {exc}",
                                                          f"{what}: WITec has not handed over write access — {exc}")) from exc
                self._ask_write_access(what, exc)
            except Exception as exc:  # noqa: BLE001
                raise self.halt_for(exc, what) from exc

    def _ask_write_access(self, what: str, exc: BaseException) -> None:
        p = OperatorPrompt(
            kind="confirm", severity="action",
            title=tr("请在 WITec Control 里点「COM Automation」", "Please press \"COM Automation\" in WITec Control"),
            message=tr(f"协议要做「{what}」，但 WITec 还没有把写权限交给 MAST。请在 WITec Control 里点 COM Automation"
                       "（弹出的窗口会锁住 GUI，直到点 Revoke），然后在这里确认。",
                       f"The protocol needs to \"{what}\" but WITec has not handed write access to MAST. Press COM Automation in "
                       "WITec Control (the window that pops up locks the GUI until you press Revoke), then confirm here."),
            context={"reason": "write_access", "what": what})
        ans = self.ask(p)
        if not ans.ok:
            raise ProtocolHalt("operator_declined", tr("操作员没有交出 WITec 写权限", "the operator did not hand over WITec write access"))

    # -- operator prompts ----------------------------------------------------------

    def ask(self, prompt: OperatorPrompt, *, timeout_s: float | None = None) -> "prompts.OperatorAnswer":
        """Post *prompt*, wait (bounded, cancellable). Returns an answered :class:`OperatorAnswer`;
        timeout / cancel / abort become a clean halt."""
        prompt.skill = prompt.skill or self.name
        prompt.context = {**(prompt.context or {}), "protocol": self.name, "run_id": self.run_id,
                          "step": self.cur_index}
        limit = self.cfg.prompt_timeout_s if timeout_s is None else float(timeout_s)
        limit = max(0.05, min(limit, self.cfg.max_total_s - self.elapsed_s()))
        prompt.timeout_s = limit
        self.check()
        self.n_prompts += 1
        try:
            ans = prompts.ask(prompt, check_abort=self._abort_flag, poll_s=0.25, timeout_s=limit)
        except RuntimeError as exc:
            self.check()                    # abort / budget -> the proper halt
            raise _classify(exc, tr("等待操作员", "waiting for the operator")) from exc
        self._log_prompt(prompt, ans)
        if ans.status == "timeout":
            raise ProtocolHalt("operator_timeout", tr(f"操作员 {limit:.0f} s 内没有回答：{prompt.title}",
                                                      f"no operator answer within {limit:.0f} s: {prompt.title}"))
        if ans.status != "answered":
            raise ProtocolHalt("operator_cancelled", tr(f"提示被取消/驳回（{ans.status}）：{prompt.title}",
                                                        f"the prompt was cancelled/dismissed ({ans.status}): {prompt.title}"))
        return ans

    def _log_prompt(self, prompt: OperatorPrompt, ans: "prompts.OperatorAnswer") -> None:
        self.prompts_log.append({
            "step": self.cur_index, "prompt_id": getattr(ans, "prompt_id", "") or prompt.id,
            "kind": prompt.kind, "title": prompt.title, "status": ans.status, "choice": ans.choice,
            "value": json_safe(ans.value), "answered_by": ans.answered_by, "source": ans.source,
            "answered_at": ans.answered_at, "attest_slots": list(prompt.attest_slots),
        })

    # -- laser safety ------------------------------------------------------------------

    def await_state(self, predicate: Callable[[], bool], timeout_s: float = 2.0, step_s: float = 0.02) -> bool:
        """An operator's answer reaches us in two steps — the prompt backend wakes the waiting ``ask`` first,
        the registry write (``prompts.answer`` -> ``set_by_operator``, possibly persisted) follows a moment
        later. Reading the registry in between would see the *old* value, so after an answered attest give the
        second step a bounded moment instead of judging on stale state."""
        end = time.monotonic() + timeout_s
        while True:
            try:
                if predicate():
                    return True
            except Exception:  # noqa: BLE001
                pass
            if time.monotonic() >= end:
                return False
            time.sleep(step_s)

    def _emission_state(self) -> bool | None:
        try:
            return self.rig.laser.emission_state()
        except Exception:  # noqa: BLE001
            return None

    def tc_state(self) -> str | None:
        """The temperature controller's reported run state (``running`` / ``stopped`` / ``holding`` / ``unknown``)."""
        try:
            st = self.rig.tc.status().get("state")
        except Exception:  # noqa: BLE001
            return None
        return None if st is None else str(st)

    def tc_stopped_by_someone(self) -> bool:
        """This run has sent the controller a setpoint (so it was running) and it now reads *stopped*; this run
        itself only stops it in :meth:`cleanup`, so somebody else did (``StopTemperatureRamp``, the panel)."""
        return self.tc_commanded and self.tc_state() == "stopped"

    def ensure_attested(self, *, beam_on: bool = False) -> None:
        """A valid ``laser_area_safe`` attestation before any laser light is used, asking for one when allowed.

        The real WITec driver refuses every light-emitting operation (shutter open, exposure, Analyzer
        series) without it, and one signature lasts 8 h — so this runs before the first step, at the start
        of every step and right before every exposure / series. ``beam_on``: a source this run switched on
        is emitting right now; it is switched off for the time the operator needs to sign again and comes
        back once the signature is in."""
        store = optical_state.get_store()
        if store.is_valid(SLOT_LASER_SAFE, expect=True):
            return
        expired = store.get(SLOT_LASER_SAFE) is not None
        if beam_on and self.rig.laser is not None and not self._source_off_confirmed():
            raise ProtocolHalt("emission_refused", tr(
                f"签署已过期，但没能确认光源 {self.rig.laser_id} 已关出光（读回不是「已关」）—— 光可能还开着，不请人签署，停下",
                f"the signature has expired, but the source {self.rig.laser_id} could not be confirmed off (the read-back is not 'off') — "
                "the beam may still be on, so the operator is not asked to sign; stopping"))
        if not self.cfg.ask_attest_laser:
            raise ProtocolHalt("laser_not_attested", tr(
                f"laser_area_safe 签署{'已过期' if expired else '无效'}，且 ask_attest_laser=false —— 不使用激光",
                f"the laser_area_safe attestation {'has expired' if expired else 'is missing'} and ask_attest_laser=false — "
                "no laser light is used"))
        p = OperatorPrompt(
            kind="attest", severity="safety", attest_slots=[SLOT_LASER_SAFE],
            title=tr("激光区安全确认（签署）", "Laser area safety (attestation)"),
            message=tr(f"协议即将使用激光（开快门 / 开出光 / 采谱），而 laser_area_safe 签署{'已过期' if expired else '还没有'}。"
                       "请确认：已戴护目镜、光路已封闭、光路上没有人、挡光件和厂商软件状态正确，然后签署（有效 8 小时）。",
                       f"The protocol is about to use laser light (shutter / emission / acquisition) and the laser_area_safe attestation "
                       f"{'has expired' if expired else 'is missing'}. Confirm: goggles on, beam path enclosed, nobody in the beam, "
                       "blockers and vendor software in the right state, then sign (valid for 8 hours)."),
            context={"reason": "laser_attest", "slot": SLOT_LASER_SAFE, "attest_value": True, "expired": expired})
        ans = self.ask(p)
        declined = _declined(ans)
        if not declined:
            self.await_state(lambda: store.is_valid(SLOT_LASER_SAFE, expect=True))
        if not store.is_valid(SLOT_LASER_SAFE, expect=True):
            raise ProtocolHalt("operator_declined" if declined else "laser_not_attested", tr(
                "操作员没有签署激光区安全 —— 不使用激光", "the operator did not attest laser-area safety — no laser light is used"))
        if beam_on and self.rig.laser is not None:        # signed again: the beam we switched off for it comes back
            self.hw("emission on", self.rig.laser.emission_on)
            if self.cfg.warmup_s:
                self.sleep(self.cfg.warmup_s)

    def bring_up_source(self) -> None:
        """Get the external source emitting, through both gates (or ask a person to)."""
        laser = self.rig.laser
        if laser is None:
            return
        state = self._emission_state()
        if state is True:
            if self.emission_by_software:                 # this run switched it back on after a swap
                self.source_mode = "software"
                return
            self.source_mode = "already_on"
            self.warn(tr("光源已经在出光（不是本协议打开的）", "the source was already emitting (not switched on by this protocol)"))
            return
        supports = "emission" in getattr(laser, "SUPPORTS", ())
        if self.cfg.manage_emission and supports:
            if not getattr(laser, "allow_emission_control", False):
                raise ProtocolHalt("emission_refused", tr(
                    f"{self.rig.laser_id}: 设备配置里 allow_emission_control=false，软件不能开出光；"
                    "请手动开光源并用 manage_emission=false，或在自检通过后打开该配置",
                    f"{self.rig.laser_id}: allow_emission_control is false in the device config, so software cannot switch the "
                    "emission on; switch the source on by hand and pass manage_emission=false, or enable it after the bench self-test"),
                    phase="prepare")
            self.ensure_attested()
            self.hw("emission on", laser.emission_on)
            self.emission_by_software = True
            self.source_mode = "software"
            if self.cfg.warmup_s:
                self.sleep(self.cfg.warmup_s)
            return
        hint_zh, hint_en = self.proto._raise_hint(self)        # allow_emission_control=false: software does not add light
        p = OperatorPrompt(
            kind="confirm", severity="safety",
            title=tr("请手动打开光源并确认", "Switch the source on by hand and confirm"),
            message=tr(f"光源 {self.rig.laser_id} 不由软件开关出光。请按厂商软件/钥匙开关把它打开到测量需要的状态，确认后协议才继续。{hint_zh}",
                       f"The source {self.rig.laser_id} is not switched in software. Switch it on in the vendor software / with the key to the state the "
                       f"measurement needs, then confirm to continue.{' ' + hint_en if hint_en else ''}"),
            context={"reason": "manual_emission"})
        ans = self.ask(p)
        if not ans.ok:
            raise ProtocolHalt("operator_declined", tr("操作员没有确认光源已打开", "the operator did not confirm that the source is on"))
        if self._emission_state() is False:
            raise ProtocolHalt("emission_refused", tr("确认之后光源仍报告没有出光", "the source still reports no emission after the confirmation"))
        self.source_mode = "manual"

    # -- the WITec shutter -----------------------------------------------------------------

    def close_shutter(self, what: str, *, quiet: bool = False, raise_now: bool = True) -> None:
        """Close the WITec shutter and check what the driver answers.

        A shutter that is not *confirmed* closed — the call raised, or the driver answers "still open" — is a
        fault the protocol cannot work around: ``stop_all`` is sent, :attr:`shutter_fault` is set, and from then
        on no prompt is posted and no new action starts (:meth:`check`). ``quiet`` is the path right after an
        exposure: no operator prompt for write access (the shutter is what is at stake) and no abort test.
        ``raise_now=False`` lets the caller finish what it holds (the spectrum just measured) before the halt."""
        mic = self.rig.mic
        if mic is None:
            return
        cause = ""
        now_open: Any = None
        try:
            now_open = mic.set_shutter(False) if quiet else self.hw(what, mic.set_shutter, False)
        except ProtocolHalt:
            raise
        except Exception as exc:  # noqa: BLE001
            cause = f"{type(exc).__name__}: {exc}"
        if not cause and now_open is False:
            return
        self._shutter_fault(what, cause or tr("驱动回答快门仍是开着的", "the driver answers that the shutter is still open"))
        if raise_now:
            self.raise_if_shutter_fault()

    def _shutter_fault(self, what: str, cause: str) -> None:
        mic = self.rig.mic
        try:
            mic.stop_all()                         # never raises; needs no write access
            stopped = tr("已发 stop_all", "stop_all was sent")
        except Exception:  # noqa: BLE001
            stopped = tr("stop_all 也没发出去", "stop_all could not be sent either")
        try:
            if mic.set_shutter(False) is False:
                again = tr("stop_all 之后再关一次：读回已关", "closed again after stop_all: read back closed")
            else:
                again = tr("stop_all 之后再关一次：读回仍是开", "closed again after stop_all: still reads open")
        except Exception as exc:  # noqa: BLE001
            again = tr(f"stop_all 之后再关一次也失败：{exc}", f"closing again after stop_all failed too: {exc}")
        self.shutter_fault = tr(
            f"WITec 快门没能确认关闭（{what}：{cause}）—— {stopped}；{again}。协议停下，不再弹任何提示，已采的谱保留；请到 WITec 电脑上确认快门已关",
            f"the WITec shutter could not be confirmed closed ({what}: {cause}) — {stopped}; {again}. The protocol stops, posts no further "
            "prompt and keeps the spectra measured so far; check on the WITec PC that the shutter is closed")
        self.warn(self.shutter_fault)

    # -- hands in the beam path (swapping an optic, turning the incident polariser) ------------------

    def _source_off_confirmed(self, settle_s: float = 2.0) -> bool:
        """Take the external source dark (the safe direction: always allowed, never raises) and wait — a moment,
        bounded — for the read-back to say *off*. Only an explicit "off" counts: still on, or unreadable, is not
        confirmed."""
        laser = self.rig.laser
        if laser is None:
            return True
        try:
            state = laser.emission_off()
        except Exception:  # noqa: BLE001
            state = None
        if state is False:
            return True
        return self.await_state(lambda: laser.emission_state() is False, timeout_s=settle_s)

    def _close_every_shutter(self) -> dict | None:
        """A person is about to reach into the beam path: close EVERY shutter the driver can reach
        (``close_all_shutters`` - the WITec has one per laser), not only the selected laser's. A driver without it
        closes the one shutter it has and returns ``None``. Not confirmed closed -> ``stop_all`` and the run stops;
        no prompt for write access here, with hands about to go in there is only one safe answer. A report that says
        ``shutter_confirmed=False``, or that left a laser out (``skipped``: a sequencer is running, or its state could
        not be read), is not a success because nothing was raised: it counts as not confirmed."""
        mic = self.rig.mic
        closer = getattr(mic, "close_all_shutters", None)
        if not callable(closer):
            self.close_shutter("close the WITec shutter before the swap")
            return None
        what = "close every WITec shutter before the swap"
        self.check()
        try:
            report = closer()
        except ProtocolHalt:
            raise
        except OperationNotSupported as exc:                 # a laser with no software shutter: block the beam physically
            raise _classify(exc, what) from exc
        except Exception as exc:  # noqa: BLE001 - ShutterNotClosed included: the light may still be on
            self._shutter_fault(what, f"{type(exc).__name__}: {exc}")
            self.raise_if_shutter_fault()
            raise
        out = dict(report) if isinstance(report, dict) else {}
        if out.get("shutter_confirmed") is False or out.get("skipped"):
            why = str(out.get("skipped") or "") or "shutter_confirmed=false"
            self._shutter_fault(what, tr(f"驱动没能确认每台激光的快门都已关：{why}",
                                         f"the driver could not confirm that every laser's shutter is closed: {why}"))
            self.raise_if_shutter_fault()
        return out

    def begin_swap(self) -> _Swap:
        """Make the beam path safe before a person reaches into it.

        Every WITec shutter is closed (and must read closed, else the run stops). The external source is taken dark
        by software when it can be — whether this run switched it on or it was already on — and read back; when
        software cannot confirm it off, the operator is asked to switch it off or block the beam first, and the
        protocol goes on only after that. The returned :class:`_Swap` says what is true, for the prompt."""
        sw = _Swap()
        mic, laser = self.rig.mic, self.rig.laser
        if mic is not None:
            report = self._close_every_shutter()
            sw.shutter, sw.shutters = ("all", report) if report is not None else ("closed", {})
        if laser is None:
            if not getattr(self.cfg, "witec_laser", ""):
                sw.source = "unmanaged"
            return sw
        sw.laser_id = self.rig.laser_id
        if self._emission_state() is False:
            sw.source = "not_emitting"
            return sw
        if self._source_off_confirmed():
            sw.source = "off_by_software"
            return sw
        sw.read_back = tr("仍在出光", "still emitting") if self._emission_state() is True else tr("读不到", "unreadable")
        self._ask_source_dark(sw)
        sw.source = "off_by_hand"
        return sw

    def end_swap(self, sw: _Swap) -> None:
        """Bring the source back the way it was before :meth:`begin_swap`: by software when the device config and
        ``manage_emission`` allow it (signature checked again first), otherwise the operator is asked to."""
        if sw.source not in ("off_by_software", "off_by_hand"):
            return
        laser = self.rig.laser
        software = (sw.source == "off_by_software" and self.cfg.manage_emission
                    and "emission" in getattr(laser, "SUPPORTS", ()) and getattr(laser, "allow_emission_control", False))
        if not software:
            self._ask_source_back_on()
            return
        self.ensure_attested()
        self.hw("emission on", laser.emission_on)
        self.emission_by_software = True
        if self.cfg.warmup_s:
            self.sleep(self.cfg.warmup_s)

    def _ask_source_dark(self, sw: _Swap) -> None:
        sid = self.rig.laser_id
        p = OperatorPrompt(
            kind="confirm", severity="safety",
            title=tr("请先关掉光源或挡住光路", "Switch the source off or block the beam first"),
            message=tr(f"软件没能确认光源 {sid} 已关出光（读回：{sw.read_back}）。请在厂商软件里关掉它（或用挡光件挡住光路）；"
                       "确认之后，协议才会请你伸手进光路。",
                       f"Software could not confirm that the source {sid} is off (read back: {sw.read_back}). Switch it off in the vendor "
                       "software (or block the beam with a beam block); the protocol asks you to reach into the beam path only after you confirm."),
            context={"reason": "source_off_for_swap", "laser_id": sid, "read_back": sw.read_back})
        ans = self.ask(p)
        if not ans.ok:
            raise ProtocolHalt("operator_declined", tr("操作员没有确认光源已关或光路已挡", "the operator did not confirm that the source is off or the beam blocked"))

    def _ask_source_back_on(self) -> None:
        sid = self.rig.laser_id
        p = OperatorPrompt(
            kind="confirm", severity="safety",
            title=tr("换好了：请把光源重新打开并确认", "Swap done: switch the source back on and confirm"),
            message=tr(f"光学件已换好。请重新打开光源 {sid}（或拿掉挡光件），确认后协议才继续。",
                       f"The swap is done. Switch the source {sid} back on (or take the beam block away), then confirm to continue."),
            context={"reason": "source_back_on", "laser_id": sid})
        ans = self.ask(p)
        if not ans.ok:
            raise ProtocolHalt("operator_declined", tr("操作员没有确认光源已重新打开", "the operator did not confirm that the source is back on"))
        if self._emission_state() is False:
            raise ProtocolHalt("emission_refused", tr("确认之后光源仍报告没有出光", "the source still reports no emission after the confirmation"))

    # -- spectrometer centre ------------------------------------------------------------

    def set_center(self, nm: float) -> dict:
        """Put the spectrometer centre at *nm* (within ``center_tolerance_nm`` of the present one it is
        left alone). Drivers that cannot (OperationNotSupported) fall back per ``center_fallback``."""
        cfg = self.cfg
        tol = float(getattr(cfg, "center_tolerance_nm", 0.0) or 0.0)
        if self.center_now is not None and abs(nm - self.center_now) <= tol:
            return {"center_nm": self.center_now, "requested_nm": nm, "method": "kept"}
        spec = self.rig.spec
        if self.center_driver_ok:
            try:
                self.hw("set the spectrometer centre", spec.set_center_wavelength, nm, passthrough=(OperationNotSupported,))
                self.center_now = float(nm)
                rec = {"center_nm": float(nm), "requested_nm": nm, "method": "driver"}
                self.center_log.append(rec)
                return rec
            except OperationNotSupported:
                self.center_driver_ok = False
                self.warn(tr("驱动不能设谱仪中心，按 center_fallback 处理", "the driver cannot set the spectrometer centre; using center_fallback"))
        fb = getattr(cfg, "center_fallback", "prompt")
        if fb == "fail":
            raise ProtocolHalt("center_not_settable", tr("驱动不能设谱仪中心，且 center_fallback=fail",
                                                         "the driver cannot set the spectrometer centre and center_fallback=fail"))
        if fb == "state":
            name = getattr(cfg, "center_state", "")
            mic = self.rig.mic
            if mic is None or not name:
                raise ProtocolHalt("center_not_settable", tr("center_fallback=state 需要显微镜和 center_state", "center_fallback=state needs a microscope and center_state"))
            if (self.center_log or [{}])[-1].get("method") != "state":
                self.hw("load the WITec preset", mic.load_state, name)
            try:
                reported = self.rig.spec.status().get("center_wavelength_nm")
            except Exception:  # noqa: BLE001
                reported = None
            self.center_now = None if reported is None else float(reported)
            rec = {"center_nm": self.center_now, "requested_nm": nm, "method": "state", "state": name}
            self.center_log.append(rec)
            return rec
        lim = spec.limits
        p = OperatorPrompt(
            kind="number", severity="action", unit="nm", number_min=float(lim.min_center_nm), number_max=float(lim.max_center_nm),
            title=tr(f"请把谱仪中心设到 {nm:.1f} nm", f"Set the spectrometer centre to {nm:.1f} nm"),
            message=tr(f"驱动不能经 COM 设谱仪中心。请在 WITec Control 里把谱仪中心波长设到 {nm:.1f} nm，然后填写你实际设的值（nm）。",
                       f"The driver cannot set the spectrometer centre over COM. Set the centre wavelength to {nm:.1f} nm in WITec Control, then enter the value you actually set (nm)."),
            context={"reason": "center", "requested_nm": nm})
        ans = self.ask(p)
        reported = float(ans.value)
        self.center_now = reported
        rec = {"center_nm": reported, "requested_nm": nm, "method": "operator", "answered_by": ans.answered_by}
        self.center_log.append(rec)
        return rec

    # -- power ---------------------------------------------------------------------------

    def read_power_mw(self, wavelength_nm: float | None, *, check: bool = True) -> float | None:
        """Mean power in mW from the power meter (None when there is none)."""
        pm = self.rig.pm
        if pm is None:
            return None
        w = self.hw("read the power meter", pm.read_power_w, wavelength_nm=wavelength_nm,
                    averages=self.cfg.power_averages, check=check)
        return float(w) * 1e3

    def ask_power_mw(self, label: str) -> float | None:
        """Ask the operator for a power reading (mW)."""
        p = OperatorPrompt(
            kind="number", severity="action", unit="mW", number_min=0.0,
            title=tr(f"请填写功率读数（{label}）", f"Enter the power reading ({label})"),
            message=tr("这台机器没有常驻功率计。请读出样品处/光路里的功率，填写数值（mW）。",
                       "There is no resident power meter. Read the power at the sample / in the beam and enter it (mW)."),
            context={"reason": "power_reading", "label": label})
        ans = self.ask(p)
        return float(ans.value)

    # -- files -------------------------------------------------------------------------

    def begin(self) -> None:
        from mast._runtime_paths import project_root

        self.watch_stops()                       # from here on a stop asked for from outside ends the run
        self.dir = Path(project_root()) / "artifacts" / "optics_protocols" / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            self.live = LiveStream(
                f"local/spectra/{self.rig.spec_id}", "spectrum",
                title=tr(f"光谱 · {self.name}", f"Spectrum · {self.name}"), source=self.name,
                x_label="Wavelength", x_unit="nm", y_label="Counts", y_unit="counts",
                meta={"device_id": self.rig.spec_id, "protocol": self.name})
        except Exception as exc:  # noqa: BLE001
            logger.debug("live stream unavailable: %s", exc)
            self.live = None
        self.write_summary()

    def unique_stem(self, stem: str) -> str:
        base, n = stem, 1
        while stem in self.stems:
            n += 1
            suffix = f"_n{n}"
            stem = base[:STEM_LIMIT - len(suffix)] + suffix
        self.stems.add(stem)
        return stem

    def device_status(self) -> dict:
        out: dict[str, Any] = {}

        def grab(role: str, fn: Callable) -> None:
            try:
                out[role] = {"id": self.rig.info.get(role, {}).get("id"), "status": json_safe(fn())}
            except Exception as exc:  # noqa: BLE001
                out[role] = {"id": self.rig.info.get(role, {}).get("id"), "error": f"{type(exc).__name__}: {exc}"}

        if self.rig.mic is not None:
            grab("microscope", self.rig.mic.microscope_status)
        grab("spectrometer", self.rig.spec.status)
        if self.rig.laser is not None:
            grab("laser", self.rig.laser.status)
        if self.rig.pm is not None:
            grab("power_meter", self.rig.pm.status)
        if self.rig.tc is not None:
            grab("temperature", self.rig.tc.status)
        return out

    def base_conditions(self) -> dict:
        """The conditions every sidecar carries; protocol steps overwrite the ones they set."""
        cond: dict[str, Any] = {
            "excitation_nm": None, "power_mw": None, "analyzer_deg": None, "temperature_c": None,
            "spectrometer_center_nm": None, "integration_s": self.cfg.integration_s,
            "accumulations": self.cfg.accumulations,
        }
        if self.rig.mic is not None:
            try:
                cond["analyzer_deg"] = float(self.rig.mic.get_analyzer_deg())
            except Exception:  # noqa: BLE001
                pass
        if self.rig.tc is not None:
            try:
                cond["temperature_c"] = self.rig.tc.read_c()
            except Exception:  # noqa: BLE001
                pass
        try:
            cen = self.rig.spec.status().get("center_wavelength_nm")
            cond["spectrometer_center_nm"] = None if cen is None else float(cen)
        except Exception:  # noqa: BLE001
            pass
        if cond["spectrometer_center_nm"] is None:
            cond["spectrometer_center_nm"] = self.center_now
        return cond

    # -- exposure and storage ------------------------------------------------------------

    def archive(self, paths: list[Path]) -> dict:
        """File *paths* into the run's sample — never into another one. When the active experiment/sample
        changed since the start the files stay in the staging directory (nothing is lost, nothing is
        mis-attributed) and the run stops at its next check."""
        if scope_ids() != self.scope0:
            self.warn(tr("当前实验/样品已切换：这些文件留在暂存目录，没有归档进新样品",
                         "the active experiment/sample changed: these files stay in the staging directory and were not filed under the new sample"))
            return {"stored": False, "reason": "scope changed", "paths": [str(p) for p in paths]}
        return store_in_experiment(paths, skill=self.name)

    def _shutter_window(self):
        import contextlib

        run = self

        @contextlib.contextmanager
        def window():
            mic = run.rig.mic
            if mic is None or not run.cfg.witec_laser:
                yield
                return
            try:
                run.hw("open the WITec shutter", mic.set_shutter, True)
                yield
            finally:
                # whatever happened inside — a failed open included — the shutter goes shut and the answer is
                # checked. Not confirmed closed: stop_all, the fault is recorded and the run halts as soon as the
                # spectrum in hand is stored (``raise_now=False``: a measured spectrum is never thrown away)
                run.close_shutter("close the WITec shutter after the exposure", quiet=True, raise_now=False)

        return window()

    def expose(self, step: _Step, *, device_name: str, meta: dict | None = None) -> SimpleNamespace:
        """One exposure through the spectrometer. Keeps the optical-state snapshot taken just before."""
        cfg, spec = self.cfg, self.rig.spec
        store = optical_state.get_store()
        self.ensure_attested(beam_on=self.emission_by_software)       # the last gate before the shutter / exposure
        before = store.snapshot()
        simple = {k: v for k, v in (meta or {}).items() if isinstance(v, (str, int, float, bool))}
        t0 = time.monotonic()
        started, hlc0 = _utc(), hlc_now()
        with self._shutter_window():
            res = self.hw("acquire a spectrum", spec.acquire,
                          exposure_ms=cfg.integration_s * 1000.0, frames=cfg.accumulations,
                          save=cfg.save_raw, base_name=device_name, tag=self.short,
                          meta={"mast_run_id": self.run_id, "protocol": self.name, "step": step.index, **simple},
                          timeout_s=None, check_abort=self.probe)
        elapsed = time.monotonic() - t0
        cut = self.external_stop() or None     # flagged, not thrown away: the spectrum is in hand, but a stop mid-exposure may have cut it
        if not getattr(res, "frames", None):
            raise ProtocolHalt("no_data", tr("采集结束但没有任何一帧到达", "the acquisition finished but no frame arrived"))
        stack, ragged = _stack(res.frames, res.axis_nm)
        mean = np.nanmean(stack[1:], axis=0)
        after = store.snapshot()
        sat = None
        if cfg.saturation_counts:
            sat = bool(np.nanmax(stack[1:]) >= cfg.saturation_counts)
        return SimpleNamespace(res=res, stack=stack, ragged=ragged, mean=mean, axis=stack[0] if np.isfinite(stack[0]).any() else None,
                               optical_before=before, optical_after=after, external_stop=cut,
                               optical_changed=_state_changed(before, after), started=started, hlc_start=hlc0,
                               hlc_end=hlc_now(), elapsed_s=elapsed, saturated=sat,
                               max_counts=float(np.nanmax(stack[1:])), peak_all=int(np.nanargmax(mean)))

    def store_step(self, step: _Step, expo: SimpleNamespace, *, stem: str, conditions: dict, analysis: dict,
                   extra: dict | None = None) -> dict:
        """Write ``<stem>.npy`` + ``<stem>.json`` for this step and archive them. Never loses the data:
        when archiving fails the staging copies stay where they are and are reported."""
        import hashlib

        assert self.dir is not None
        stem = self.unique_stem(stem)
        buf = io.BytesIO()
        np.save(buf, expo.stack, allow_pickle=False)
        payload = buf.getvalue()
        sha = hashlib.sha256(payload).hexdigest()
        npy_path, json_path = self.dir / f"{stem}.npy", self.dir / f"{stem}.json"
        res = expo.res
        counts = {"min": float(np.nanmin(expo.mean)), "max": float(np.nanmax(expo.mean)),
                  "mean": float(np.nanmean(expo.mean))}
        peak: dict[str, Any] = {"pixel": expo.peak_all, "counts": float(expo.mean[expo.peak_all])}
        if expo.axis is not None and expo.peak_all < len(expo.axis) and math.isfinite(float(expo.axis[expo.peak_all])):
            peak["nm"] = float(expo.axis[expo.peak_all])
        sidecar = {
            "schema": "mast.spectrum/1",
            "layout": "npy rows: [0] = wavelength_nm (NaN when unknown), [1..] = frames (counts)",
            "skill": self.name, "run_id": self.run_id,
            "protocol": {"name": self.name, "version": SKILL_VERSION, "run_id": self.run_id, "step": step.index,
                         "n_steps": len(self.plan.steps), "label": step.label, "stem": stem},
            "conditions": json_safe(conditions),
            "optical_state": expo.optical_before,
            "optical_state_changed_during_exposure": expo.optical_changed,
            **({"optical_state_after": expo.optical_after} if expo.optical_changed else {}),
            "operator_prompts": [p for p in self.prompts_log if p.get("step") == step.index],
            "instruments": self.device_status(),
            "time": {"started_utc": getattr(res, "started_utc", "") or expo.started,
                     "finished_utc": getattr(res, "finished_utc", "") or _utc(),
                     "hlc_start": expo.hlc_start, "hlc_end": expo.hlc_end, "elapsed_s": round(expo.elapsed_s, 3)},
            "device": dict(self.rig.info.get("spectrometer") or {}),
            "settings": {"requested": {"exposure_ms": self.cfg.integration_s * 1000.0, "frames": self.cfg.accumulations},
                         "laser_line_nm": getattr(self.rig.spec, "laser_line_nm", None)},
            "scope": {**scope_ids(), "action_id": None, "step_id": f"{self.run_id}:{step.index}"},
            "files": {"array": {"name": npy_path.name, "sha256": sha, "size": len(payload),
                                "shape": list(expo.stack.shape), "dtype": "float64", "ragged_frames": expo.ragged},
                      "device_files": json_safe(getattr(res, "files", []))},
            "result": {"frames": len(res.frames), "peak": peak, "counts": counts, "saturated": expo.saturated,
                       "analysis": json_safe(analysis)},
            "acquisition": json_safe(getattr(res, "meta", {})),
            "extra": json_safe(extra or {}),
        }
        if getattr(expo, "external_stop", None):
            sidecar["external_stop_during_exposure"] = expo.external_stop
        try:
            atomic_write_bytes(npy_path, payload)
            atomic_write_bytes(json_path, dump_json_bytes(sidecar))
        except Exception as exc:  # noqa: BLE001
            raise ProtocolHalt("error", tr(f"写光谱文件失败: {exc}", f"could not write the spectrum files: {exc}")) from exc
        stored = self.archive([npy_path, json_path])
        self.data_paths.extend(stored["paths"])
        self.all_stored = self.all_stored and bool(stored.get("stored"))
        self._publish_live(expo, step)
        return {"stem": stem, "paths": stored["paths"], "stored": bool(stored.get("stored")), "sha256": sha,
                "peak": peak, "counts": counts}

    def _publish_live(self, expo: SimpleNamespace, step: _Step) -> None:
        live = self.live
        if live is None or not live.active:
            return
        try:
            ver = self.last_axis_ver
            axis = expo.axis
            if axis is not None and len(axis) == len(expo.mean):
                if self.last_axis is None or not np.array_equal(self.last_axis, axis):
                    ver = live.set_axis(axis, unit="nm")
                    self.last_axis, self.last_axis_ver = np.array(axis), ver
            else:
                ver = None
            live.publish({"y": np.asarray(expo.mean, dtype=np.float64), "axis_ver": ver,
                          "meta": {"run_id": self.run_id, "protocol": self.name, "step": step.index, "final": True}})
        except Exception as exc:  # noqa: BLE001 - live plotting is never a reason to fail a step
            logger.debug("live publish failed: %s", exc)

    # -- steps and the summary ------------------------------------------------------------

    def begin_step(self, step: _Step) -> None:
        self.cur = step
        self.step_status[step.index] = "running"

    def complete_step(self, step: _Step, row: dict) -> None:
        self.rows.append(row)
        self.step_status[step.index] = "done"
        self.cur = None
        self.write_summary()

    def summary_doc(self, *, final: bool = False) -> dict:
        steps = [{"index": s.index, "label": s.label, "status": self.step_status.get(s.index, "pending")}
                 for s in self.plan.steps]
        return {
            "schema": "mast.optics_protocol/1", "protocol": self.name, "version": SKILL_VERSION,
            "run_id": self.run_id, "status": self.status,
            "stop": ({"kind": self.stop_kind, "reason": self.stop_reason, "at_step": self.stopped_at,
                      "phase": self.stop_phase} if self.stop_kind else None),
            "current_step": self.cur_index, "steps_done": len(self.rows), "steps_planned": len(self.plan.steps),
            "started_utc": self.started_utc, "updated_utc": _utc(),
            "finished_utc": self.finished_utc if final else None, "elapsed_s": round(self.elapsed_s(), 1),
            "params": json_safe(self.params), "devices": self.rig.info,
            "plan": {"estimated_machine_s": round(self.plan.est_machine_s, 1), "manual_steps": self.plan.n_manual,
                     "notes": self.plan.notes},
            "steps": steps, "rows": json_safe(self.rows), "analysis": json_safe(self.analysis),
            "operator_prompts": self.prompts_log, "warnings": self.warnings, "cleanup": json_safe(self.cleanup_report),
            "scope": scope_ids(), "centre_log": json_safe(self.center_log),
            "optical_state": optical_state.get_store().snapshot(),
        }

    def write_summary(self, *, final: bool = False) -> None:
        """Rewrite ``<run_id>_summary.json`` / ``.csv`` atomically. A failure here is logged, never fatal."""
        if self.dir is None:
            return
        try:
            atomic_write_bytes(self.dir / f"{self.run_id}_summary.json", dump_json_bytes(self.summary_doc(final=final)))
            if self.rows:
                cols = list(self.proto.CSV_COLUMNS)
                for r in self.rows:
                    for k in r:
                        if k not in cols:
                            cols.append(k)
                out = io.StringIO()
                w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
                w.writeheader()
                for r in self.rows:
                    w.writerow({k: _csv_cell(r.get(k)) for k in cols})
                atomic_write_bytes(self.dir / f"{self.run_id}_summary.csv", out.getvalue().encode("utf-8"))
            self.summary_error = ""
        except Exception as exc:  # noqa: BLE001
            self.summary_error = f"{type(exc).__name__}: {exc}"
            logger.warning("%s: summary write failed: %s", self.run_id, self.summary_error)

    # -- ending -------------------------------------------------------------------------------

    def record_halt(self, h: ProtocolHalt) -> None:
        """Write down where and why the run stopped (``stopped_at`` = the first step that did not finish)."""
        if self.shutter_fault and h.kind != "shutter_not_closed":
            # a shutter that may be open outranks whatever else ended the run; the other reason is kept in the text
            h = ProtocolHalt("shutter_not_closed", f"{self.shutter_fault} [{h.kind}: {h.message}]", phase=h.phase)
        self.stop_kind = h.kind
        self.stop_reason = h.message
        self.stop_phase = "step" if self.phase == "steps" else "prepare"
        if h.kind == "aborted":
            self.status = "aborted"
        elif h.kind in _CLEAN_STOPS:
            self.status = "stopped"
        else:
            self.status = "failed"
        if self.phase == "steps":
            idx = self.cur.index if self.cur is not None else self.next_index
            self.stopped_at = idx if idx <= len(self.plan.steps) else None       # None: after the last step
            if self.cur is not None and self.step_status.get(self.cur.index) == "running":
                self.step_status[self.cur.index] = "failed" if self.status == "failed" else "stopped"

    def cleanup(self) -> dict:
        """The safe direction. Each action on its own; none of them raises."""
        rep: dict[str, Any] = {}
        ok_end = self.status == "completed"
        rig = self.rig
        if rig.laser is not None:
            try:
                state = rig.laser.emission_off()
                rep["emission"] = {"id": rig.laser_id, "state_after": state}
                if state is True:
                    self.warn(tr(f"{rig.laser_id}: emission_off 之后读回仍在出光！请现场确认并手动关闭",
                                 f"{rig.laser_id}: the source still reports emission after emission_off! Check on site and switch it off by hand"))
            except Exception as exc:  # noqa: BLE001
                rep["emission"] = {"id": rig.laser_id, "error": f"{type(exc).__name__}: {exc}"}
        shutter_unconfirmed = False
        if rig.mic is not None:
            try:
                if rig.mic.set_shutter(False) is not False:
                    raise InstrumentError("the shutter still reads open")
                rep["shutter"] = "closed"
            except Exception as exc:  # noqa: BLE001
                shutter_unconfirmed = True
                rep["shutter"] = f"close failed: {type(exc).__name__}: {exc}"
                self.warn(tr(f"WITec 快门没能确认关闭: {exc}", f"the WITec shutter could not be confirmed closed: {exc}"))
        if not ok_end:
            try:
                rig.spec.stop()
                rep["spectrometer"] = "stop sent"
            except Exception as exc:  # noqa: BLE001
                rep["spectrometer"] = f"stop failed: {exc}"
            if rig.mic is not None:
                rig.mic.stop_all()                  # sequencers and stage; never raises, needs no write access
                rep["microscope"] = "stop_all sent"
        if rig.tc is not None and self.commands_temperature:
            final = getattr(self.cfg, "final_setpoint_c", None)
            if ok_end and final is not None and not self.aborted() and self.external_stop():
                # a stop was asked for from outside (StopTemperatureRamp, the E-STOP …) since the run began
                rig.tc.stop()
                rep["temperature"] = {"action": "stop", "note": "a stop was asked for from outside the run; no final setpoint sent"}
                self.warn(tr(f"收到外部停止请求：不再发终点温度 {final} °C", f"a stop was asked for from outside the run: the final setpoint {final} °C is not sent"))
            elif ok_end and final is not None and not self.aborted() and self.tc_stopped_by_someone():
                # somebody pressed StopTemperatureRamp since the last point: a new setpoint would start the stage again
                rig.tc.stop()
                rep["temperature"] = {"action": "stop", "note": "the controller was stopped by somebody else; no final setpoint sent"}
                self.warn(tr(f"温控已被别人停了：不再发终点温度 {final} °C", f"the controller was stopped by somebody else: the final setpoint {final} °C is not sent"))
            elif ok_end and final is not None and not self.aborted():
                try:
                    rig.tc.set_setpoint(final, ramp_c_per_min=getattr(self.cfg, "ramp_c_per_min", None))
                    rep["temperature"] = {"action": "final_setpoint", "setpoint_c": final}
                except Exception as exc:  # noqa: BLE001
                    rig.tc.stop()
                    rep["temperature"] = {"action": "stop", "note": f"final setpoint refused: {exc}"}
                    self.warn(tr(f"终点温度 {final} °C 没能设上（{exc}），已改为停温控", f"the final setpoint {final} °C could not be set ({exc}); the controller was stopped instead"))
            else:
                rig.tc.stop()
                rep["temperature"] = {"action": "stop"}
        if rig.mic is not None and getattr(self.cfg, "release_write_access", True):
            if shutter_unconfirmed:
                # D5 hands the grant back when the job ends — but not while the shutter may still be open:
                # a later StopWITec / CloseWITecShutter needs it to close that shutter
                rep["write_access"] = "kept: the shutter is not confirmed closed"
            else:
                # last: D5 — the operator's COM Automation grant goes back when the job ends
                try:
                    rig.mic.release_write_access()
                    rep["write_access"] = "released"
                except Exception as exc:  # noqa: BLE001
                    rep["write_access"] = f"release failed: {type(exc).__name__}: {exc}"
        self.cleanup_report = rep
        return rep

    def finish(self) -> None:
        """Final analysis, summary files (+ PNG) and their archiving. Never raises."""
        self.finished_utc = _utc()
        if self.dir is None:
            return
        try:
            self.analysis = self.proto.finish_analysis(self) or {}
        except Exception as exc:  # noqa: BLE001
            self.analysis = {"error": f"{type(exc).__name__}: {exc}"}
        png = None
        if self.rows:
            try:
                png = self.proto.plot(self, self.dir / f"{self.run_id}_summary.png")
            except Exception as exc:  # noqa: BLE001
                self.warn(tr(f"汇总图没画出来: {exc}", f"the summary plot could not be drawn: {exc}"))
        self.write_summary(final=True)
        files = [self.dir / f"{self.run_id}_summary.json"]
        if (self.dir / f"{self.run_id}_summary.csv").is_file():
            files.append(self.dir / f"{self.run_id}_summary.csv")
        if png is not None and Path(png).is_file():
            files.append(Path(png))
        files = [f for f in files if f.is_file()]
        try:
            self.final_stored = self.archive(files)
            self.final_paths = list(self.final_stored.get("paths", []))
        except Exception as exc:  # noqa: BLE001
            self.final_stored = {"stored": False, "reason": f"{type(exc).__name__}: {exc}"}
            self.final_paths = [str(f) for f in files]
        self.all_stored = self.all_stored and bool(self.final_stored.get("stored"))
        try:
            if self.dir.is_dir() and not any(self.dir.iterdir()):
                self.dir.rmdir()
        except OSError:
            pass


# ── protocol base ───────────────────────────────────────────────────────────────


def _spread(values: Iterable[Any]) -> float | None:
    """Coefficient of variation (std/mean) of the finite values, None when undefined."""
    v = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    if len(v) < 2:
        return None
    m = float(np.mean(v))
    return float(np.std(v) / abs(m)) if m else None


def _agg(violations: dict[str, list[float]], fmt: Callable[[str, list[float]], str]) -> list[str]:
    return [fmt(k, v) for k, v in violations.items() if v]


class _Protocol(BaseSkill):
    """Template shared by the four protocols: parse -> resolve devices -> plan -> check -> run.

    Subclasses implement ``metadata`` and the hooks below. The class is abstract (no ``metadata``), so
    the registry never lists it.
    """

    NAME = ""
    SHORT = ""
    CSV_COLUMNS: tuple[str, ...] = ()

    # -- hooks ------------------------------------------------------------------------
    def _parse(self, params: dict) -> SimpleNamespace:  # pragma: no cover - abstract
        raise NotImplementedError

    def _rig_needs(self, cfg: SimpleNamespace) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError

    def _plan(self, cfg: SimpleNamespace, rig: _Rig) -> _Plan:  # pragma: no cover - abstract
        raise NotImplementedError

    def _prepare(self, run: _Run) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _step(self, run: _Run, step: _Step) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError

    def finish_analysis(self, run: _Run) -> dict:
        return {}

    def plot(self, run: _Run, path: Path) -> Path | None:
        return None

    def _raise_hint(self, run: _Run) -> tuple[str, str]:
        """(zh, en) sentence added to the "switch the source on by hand" prompt when the protocol sets a channel /
        the power itself and ``allow_emission_control=false`` means software does not add light; empty otherwise."""
        return "", ""

    # -- shared static checks (no I/O unless ``read_hw``) -----------------------------------
    def _preflight(self, cfg: SimpleNamespace, rig: _Rig, plan: _Plan, *, read_hw: bool) -> list[str]:
        problems: list[str] = []
        try:
            rig.spec.check_request(cfg.integration_s * 1000.0, cfg.accumulations)
        except Exception as exc:  # noqa: BLE001
            problems.append(tr(f"光谱仪限值: {exc}", f"spectrometer limit: {exc}"))
        if len(plan.steps) > MAX_STEPS:
            problems.append(tr(f"{len(plan.steps)} 步超过上限 {MAX_STEPS}", f"{len(plan.steps)} steps exceed the limit {MAX_STEPS}"))
        if plan.est_machine_s > cfg.max_total_s:
            problems.append(tr(f"预计机器时间 {plan.est_machine_s:.0f} s 已超过 max_total_s={cfg.max_total_s:.0f} s",
                               f"the estimated machine time {plan.est_machine_s:.0f} s already exceeds max_total_s={cfg.max_total_s:.0f} s"))
        if getattr(cfg, "witec_laser", ""):
            problems += self._check_witec_laser(cfg, rig, read_hw=read_hw)
        if rig.laser is not None:
            problems += self._check_emission(cfg, rig)
        problems += self._check_attestation(cfg, plan)
        return problems

    def _check_witec_laser(self, cfg: SimpleNamespace, rig: _Rig, *, read_hw: bool) -> list[str]:
        mic = rig.mic
        if mic is None:
            return [tr("witec_laser 需要显微镜（WITec），这里只有光谱仪", "witec_laser needs a microscope (WITec); only a spectrometer is configured")]
        caps = mic.microscope_limits.max_laser_power_mw
        ref = cfg.witec_laser
        power = cfg.witec_power_mw
        if read_hw:
            try:
                las = _match_laser(mic.list_lasers(), ref)
            except Exception as exc:  # noqa: BLE001
                return [tr(f"读不到 WITec 激光列表: {exc}", f"cannot read the WITec laser list: {exc}")]
            if las is None:
                return [tr(f"WITec 没有激光 {ref!r}", f"WITec has no laser {ref!r}")]
            keys = [str(las.get("name"))]
            if las.get("wavelength_nm") is not None:
                keys.insert(0, str(int(round(float(las["wavelength_nm"])))))
            controllable = bool(las.get("power_controllable"))
        else:
            keys = [str(ref)]
            try:
                keys.insert(0, str(int(round(float(ref)))))
            except ValueError:
                pass
            controllable = True
        if power is None:
            return []
        if not controllable:
            return [tr(f"WITec 激光 {ref!r} 没有软件功率控制，不能给 witec_power_mw", f"WITec laser {ref!r} has no software power control; witec_power_mw cannot be used")]
        cap = next((float(caps[k]) for k in keys if k in caps), None)
        if cap is None:
            return [tr(f"microscope_limits 里没有给 {ref!r} 配 max_laser_power_mw —— 拒绝", f"no max_laser_power_mw configured for {ref!r} in microscope_limits — refused")]
        if not 0.0 <= power <= cap:
            return [tr(f"WITec 激光功率 {power:g} mW 超出 [0, {cap:g}] mW", f"WITec laser power {power:g} mW is outside [0, {cap:g}] mW")]
        return []

    def _check_emission(self, cfg: SimpleNamespace, rig: _Rig) -> list[str]:
        """The device-config gate for switching an external source on in software (the attestation is
        checked for every protocol in :meth:`_check_attestation`)."""
        laser = rig.laser
        out: list[str] = []
        software = cfg.manage_emission and "emission" in getattr(laser, "SUPPORTS", ())
        try:
            emitting = laser.emission_state() if not cfg.dry_run else None
        except Exception:  # noqa: BLE001
            emitting = None
        if software and emitting is not True and not getattr(laser, "allow_emission_control", False):
            out.append(tr(f"{rig.laser_id}: allow_emission_control=false，软件不能开出光 —— 手动开光源并用 manage_emission=false，或在自检后打开该配置",
                          f"{rig.laser_id}: allow_emission_control is false, software cannot switch the emission on — switch it on by hand with manage_emission=false, or enable it after the bench self-test"))
        return out

    @staticmethod
    def _source_may_be_live(laser: Any) -> bool:
        """The source is emitting now, or that cannot be read. The drivers gate a raise only while the source emits
        (and treat an unreadable emission state as emitting - fail closed); the plan does the same: a source that
        is off gets its amplitude / power set first and the operator switches it on afterwards."""
        try:
            return laser.emission_state() is not False
        except Exception:  # noqa: BLE001
            return True

    @staticmethod
    def _check_attestation(cfg: SimpleNamespace, plan: _Plan) -> list[str]:
        """``laser_area_safe`` must be valid before any light is used (the real WITec driver demands it for the
        shutter, the exposure and the Analyzer series). Not valid: the operator will be asked to sign (listed
        under ``before_start``), or — with ``ask_attest_laser=false`` — the run is refused."""
        store = optical_state.get_store()
        try:
            store.slot(SLOT_LASER_SAFE)
        except optical_state.OpticalStateError as exc:
            return [str(exc)]
        if store.is_valid(SLOT_LASER_SAFE, expect=True):
            return []
        if cfg.ask_attest_laser:
            plan.pre_manual.append("laser_attest")
            plan.notes.append(tr("laser_area_safe 没有有效签署：开始前会弹窗请操作员签署（真机 WITec 驱动在开快门/采谱前也要它；长扫描中途过期会再问）",
                                 "no valid laser_area_safe attestation: the operator will be asked to sign before the start (the real WITec driver needs it for the shutter and the exposures too; a long run asks again when it expires)"))
            return []
        return [tr("laser_area_safe 没有有效签署，且 ask_attest_laser=false —— 拒绝（快门/出光/采谱都需要它）",
                   "there is no valid laser_area_safe attestation and ask_attest_laser=false — refused (the shutter, the emission and the exposures all need it)")]

    # -- execute ------------------------------------------------------------------------------
    def execute(self, context, params: dict) -> SkillResult:
        params = dict(params or {})
        try:
            cfg = self._parse(params)
            rig = _resolve_rig(cfg, **self._rig_needs(cfg))
            plan = self._plan(cfg, rig)
            plan.problems += self._preflight(cfg, rig, plan, read_hw=not cfg.dry_run)
        except ProtocolRefused as exc:
            return self._refusal(str(exc))
        except Exception as exc:  # noqa: BLE001 - planning is pure: a bug here must not escape as a raw exception
            logger.exception("%s: planning failed", self.NAME)
            return self._refusal(tr(f"规划阶段出错（没有碰硬件）: {type(exc).__name__}: {exc}",
                                    f"planning failed (no hardware was touched): {type(exc).__name__}: {exc}"))
        if cfg.dry_run:
            return self._dry_run(cfg, rig, plan)
        if plan.problems:
            return self._refusal("; ".join(plan.problems), problems=plan.problems)
        probe = abort_probe(context)
        if probe is not None and probe():
            return self._refusal(tr("操作员已中止 —— 协议没有开始", "aborted by the operator — the protocol was not started"))
        return self._run(_Run(self, context, params, cfg, rig, plan))

    def _refusal(self, message: str, *, problems: list[str] | None = None) -> SkillResult:
        return SkillResult(
            skill_name=self.NAME, success=False, error=message,
            data={"refused": True, "touched_hardware": False, "problems": problems or [message]},
            summary=tr(f"{self.NAME} 被拒绝（没有碰任何硬件）: {message}", f"{self.NAME} refused (no hardware was touched): {message}"))

    def _dry_run(self, cfg: SimpleNamespace, rig: _Rig, plan: _Plan) -> SkillResult:
        steps = [s.as_dict() for s in plan.steps]
        limit = 400
        data = {
            "dry_run": True, "protocol": self.NAME, "touched_hardware": False, "valid": not plan.problems,
            "problems": plan.problems, "notes": plan.notes, "n_steps": len(steps),
            "estimated_machine_s": round(plan.est_machine_s, 1),
            "manual_prompts_expected": plan.n_manual, "before_start": list(plan.pre_manual),
            "steps": steps[:limit], "steps_truncated": len(steps) > limit, "devices": rig.info,
        }
        mins = plan.est_machine_s / 60.0
        if plan.problems:
            return SkillResult(skill_name=self.NAME, success=False, data=data, error="; ".join(plan.problems),
                               summary=tr(f"[试跑] {self.NAME}: 计划有 {len(plan.problems)} 个问题 —— {plan.problems[0]}",
                                          f"[DRY RUN] {self.NAME}: the plan has {len(plan.problems)} problem(s) — {plan.problems[0]}"))
        return SkillResult(
            skill_name=self.NAME, success=True, data=data,
            summary=tr(f"[试跑] {self.NAME}: {len(steps)} 步，机器时间约 {mins:.1f} min，预计 {plan.n_manual} 次人工操作 —— 没有碰硬件",
                       f"[DRY RUN] {self.NAME}: {len(steps)} step(s), about {mins:.1f} min of machine time, {plan.n_manual} manual action(s) expected — no hardware touched"))

    def _run(self, run: _Run) -> SkillResult:
        try:
            run.begin()
            run.ensure_attested()                    # sign first, then start: nothing is configured before it
            self._prepare(run)
            run.phase = "steps"
            for step in run.plan.steps:
                run.next_index = step.index
                run.check()
                run.begin_step(step)
                try:
                    run.ensure_attested(beam_on=run.emission_by_software)     # a signature lasts 8 h: ask again or stop
                    row = self._step(run, step)
                except ProtocolHalt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise run.halt_for(exc, step.label) from exc
                run.complete_step(step, row)
                run.next_index = step.index + 1
                run.raise_if_shutter_fault()      # the step's spectrum is stored; a shutter that may be open ends the run here
                run.raise_if_stopped_externally()       # …and so does a stop somebody asked for meanwhile
            run.status = "completed"
        except ProtocolHalt as halt:
            run.record_halt(halt)
        except Exception as exc:  # noqa: BLE001 - never leave without the safe-direction finish
            logger.exception("%s %s: unexpected error", self.NAME, run.run_id)
            run.record_halt(run.halt_for(exc, self.NAME))
        finally:
            if run.status == "running":      # BaseException (KeyboardInterrupt, SystemExit) on its way out
                run.record_halt(ProtocolHalt("aborted", tr("被中断", "interrupted")))
            try:
                run.cleanup()
            except Exception:  # noqa: BLE001
                logger.exception("%s %s: cleanup failed", self.NAME, run.run_id)
            try:
                run.finish()
            except Exception:  # noqa: BLE001
                logger.exception("%s %s: finish failed", self.NAME, run.run_id)
        return self._result(run)

    def _result(self, run: _Run) -> SkillResult:
        ok = run.status == "completed"
        paths = list(run.data_paths)
        data: dict[str, Any] = {
            "protocol": self.NAME, "run_id": run.run_id, "status": run.status,
            "steps_planned": len(run.plan.steps), "steps_done": len(run.rows),
            "stopped_at_step": run.stopped_at, "stop_kind": run.stop_kind or None,
            "stop_reason": run.stop_reason or None, "stop_phase": run.stop_phase or None,
            "stored_in_experiment": bool(run.all_stored), "paths": paths + list(run.final_paths),
            "spectrum_files": paths, "summary_files": list(run.final_paths),
            "summary_path": next((p for p in run.final_paths if p.endswith("_summary.json")), None),
            "summary_csv_path": next((p for p in run.final_paths if p.endswith("_summary.csv")), None),
            "plot_path": next((p for p in run.final_paths if p.endswith("_summary.png")), None),
            "rows": json_safe(run.rows[:ROWS_IN_RESULT]), "rows_truncated": len(run.rows) > ROWS_IN_RESULT,
            "analysis": json_safe(run.analysis), "cleanup": json_safe(run.cleanup_report),
            "operator_prompts": len(run.prompts_log), "warnings": list(run.warnings),
            "elapsed_s": round(run.elapsed_s(), 1), "devices": run.rig.info,
        }
        if run.summary_error:
            data["summary_error"] = run.summary_error
        n, total = len(run.rows), len(run.plan.steps)
        if ok:
            where = Path(run.final_paths[0]).name if run.final_paths else ""
            summary = tr(f"{self.NAME}: 完成 {n}/{total} 步，用时 {run.elapsed_s() / 60:.1f} min → {where}",
                         f"{self.NAME}: completed {n}/{total} steps in {run.elapsed_s() / 60:.1f} min → {where}")
            error = ""
        else:
            where = (tr(f"停在第 {run.stopped_at} 步", f"stopped at step {run.stopped_at}") if run.stopped_at
                     else tr("停在最后一步之后", "stopped after the last step") if run.stop_phase == "step"
                     else tr("停在准备阶段", "stopped during preparation"))
            summary = tr(f"{self.NAME}: {where}（{run.stop_kind}），已完成 {n}/{total} 步，已采数据全部保留；安全收尾已执行",
                         f"{self.NAME}: {where} ({run.stop_kind}); {n}/{total} steps done, all measured data kept; safe-direction finish performed")
            error = f"[{run.stop_kind}] {run.stop_reason}"
        return SkillResult(skill_name=self.NAME, success=ok, data=data, error=error, summary=summary)


# ═══════════════════════════════════════════════════════════════════════════════════
# 1. SHGExcitationScan
# ═══════════════════════════════════════════════════════════════════════════════════


class SHGExcitationScan(_Protocol):
    """Excitation-wavelength SHG scan — the lab's most expensive manual routine, automated.

    For each excitation wavelength λ of the list, in order:

    1. **λ/2 check** — ``store.waveplate_covers(waveplate_slot, λ)``. When the plate in the beam does not
       cover λ (or none is registered) an ``attest`` prompt asks the operator to swap and sign the plate
       (options come from the slot definition); the check is repeated and, if it still fails, the run
       stops cleanly. A software-controlled source is switched **off** while the plate is swapped.
    2. source wavelength (and amplitude, once) on ``channel`` of the tunable source;
    3. spectrometer centre = λ/2 + ``center_offset_nm`` (``set_center_wavelength``). A driver that cannot
       (``OperationNotSupported``, e.g. WITec without COM grating access) is handled by
       ``center_fallback``: ``prompt`` (ask the operator to set it in WITec Control and report it),
       ``state`` (load the WITec preset ``center_state``) or ``fail``;
    4. spectrum;
    5. power — power meter, or (``power_prompt``) the operator's reading, or none;
    6. SHG intensity = background-subtracted integral around the peak found near λ/2, normalised
       ``I/P²`` (default), ``I/P`` or not at all.

    The summary gives λ, P, I and I/P² (and I/P) per step, with the position of the peak against λ/2.
    """

    NAME = "SHGExcitationScan"
    SHORT = "shg"
    CSV_COLUMNS = ("step", "time_utc", "excitation_nm", "expected_shg_nm", "center_nm", "power_mw",
                   "power_drift_pct", "amplitude_pct", "analyzer_deg", "temperature_c", "intensity",
                   "i_over_p", "i_over_p2", "i_norm", "peak_nm", "peak_offset_nm", "peak_counts", "snr",
                   "found", "saturated", "waveplate", "stem", "file")

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=self.NAME, version=SKILL_VERSION, category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr(
                "激发波长 SHG 扫描（光学站协议）：对波长表里的每个 λ 依次——检查激发光路 λ/2 片是否覆盖该波长（不覆盖/未登记就弹窗请操作员换片并签署；"
                "换片前先关 WITec 快门、把外部光源关出光——软件能关就关并读回确认，关不掉就请操作员手动关或挡光，换完再恢复，提示只写读回的事实）、设外部可调谐光源的波长（幅度开始时设一次）、把谱仪中心设到 λ/2 + center_offset_nm"
                "（驱动设不了就按 center_fallback：弹窗请人在 WITec 里设 / 载入预设 / 失败）、取谱、量功率（功率计，或请操作员输入）、"
                "算 SHG 积分强度并按 P²（或 P、或不）归一化。每步立刻存 .npy + .json（文件名含波长/功率/分析器角，sidecar 含全部条件、"
                "光学状态快照、设备状态与本步的操作员签署），并更新汇总 JSON+CSV（结束时 PNG）。"
                "用光前要有 laser_area_safe 有效签署（开始前、每步、每次曝光前都查；无效且 ask_attest_laser=true 时弹窗请操作员签，签不上就安全收尾停下；真机 WITec 驱动开快门/采谱也要它），软件开外部光源出光另需设备配置 allow_emission_control；"
                "allow_emission_control=false 时，光源出光期间软件不加光：光源在出光（或读不到状态）而幅度高于通道当前值的计划整体拒绝（出路：先在厂商软件里手动设好，或自检后打开该配置）。"
                "任何停止（操作员拒绝/超时/中止/出错）都走安全收尾（外部光源关出光、WITec 快门关），已采数据全部保留，汇总写明停在哪一步、为什么。"
                "别人在运行中按了停止（StopWITec / CloseWITecShutter / LaserEmissionOff / StopTemperatureRamp、撤销签署、急停）：下一步就干净停下（stop_kind=stopped_externally），不再重开快门/光源、不再下温度设定。"
                "越限一律拒绝、不钳位。dry_run=true 只返回执行计划，不碰硬件、不弹窗。",
                "Excitation-wavelength SHG scan (optical-station protocol): for each λ in the list — check that the excitation-path λ/2 plate covers it "
                "(if not, or none is registered, ask the operator to swap and sign the plate; before the swap the WITec shutter is closed and the external source is taken dark - by software "
                "when it can, read back, otherwise the operator is asked to switch it off or block the beam - and afterwards restored; the prompt says only what was read back), "
                "set the wavelength of the external tunable source (amplitude once at the start), put the spectrometer centre at λ/2 + center_offset_nm "
                "(if the driver cannot, follow center_fallback: ask a person to set it in WITec / load a preset / fail), acquire, measure the power "
                "(power meter, or the operator's reading), compute the SHG integral and normalise by P² (or P, or not at all). Each step is saved at once as .npy + .json "
                "(file name carries wavelength/power/analyzer angle; the sidecar carries all conditions, the optical-state snapshot, device status and this step's operator "
                "attestations) and the summary JSON+CSV is updated (PNG at the end). Laser light needs a valid laser_area_safe attestation (checked before the start, at every step and "
                "before every exposure; when missing/expired and ask_attest_laser=true the operator is asked to sign, and if nobody signs the run stops safely); switching the emission on in software "
                "additionally needs allow_emission_control in the device config; with allow_emission_control=false software does not add light while the source emits, so a plan whose amplitude_pct is above "
                "the channel's present amplitude is refused up front when the source is emitting or unreadable (ways out: set it by hand in the vendor software first, or enable allow_emission_control after the bench self-test). Any stop (operator declined/timed out, abort, error) takes the "
                "safe direction (external source emission off, WITec shutter closed); all measured data is kept and the summary says at which step and why. "
                "A stop somebody else asks for during the run (StopWITec / CloseWITecShutter / LaserEmissionOff / StopTemperatureRamp, withdrawing the attestation, the E-STOP) ends it cleanly at the next check (stop_kind=stopped_externally); the shutter, the source and the setpoint are not touched again. "
                "Out of range is refused, never clamped. dry_run=true only returns the plan; no hardware, no prompt."),
            parameters=[
                _spec("laser_id", "str", "可调谐外部光源设备 id；留空/auto = 唯一一台激光", "Tunable external source device id; empty/auto = the only laser", default=""),
                _spec("wavelengths_nm", "str", "激发波长表 nm：'810,820,830' 或 JSON 数组；与 start_nm/stop_nm/step_nm 二选一",
                      "Excitation wavelengths in nm: '810,820,830' or a JSON array; use either this or start_nm/stop_nm/step_nm", default=""),
                _spec("start_nm", "float", "起始波长 nm（与 stop_nm、step_nm 一起给）", "Start wavelength in nm (together with stop_nm and step_nm)", unit="nm", lo=100.0, hi=5000.0),
                _spec("stop_nm", "float", "终止波长 nm（含）", "Stop wavelength in nm (inclusive)", unit="nm", lo=100.0, hi=5000.0),
                _spec("step_nm", "float", "步长 nm（取绝对值，方向由 start→stop 决定）", "Step in nm (magnitude; the direction follows start→stop)", unit="nm", lo=0.01, hi=2000.0),
                _spec("channel", "int", "光源的波长通道号（SELECT 为 0..7）", "Wavelength channel of the source (0..7 for SELECT)", default=0, lo=0, hi=63),
                _spec("amplitude_pct", "float", "通道幅度 %（超过设备 max_amplitude_pct 即拒绝）；必填——没有安全的默认值",
                      "Channel amplitude in % (refused above the device's max_amplitude_pct); required — there is no safe default", unit="%", required=True, lo=0.0, hi=100.0),
                _spec("isolate_channel", "bool", "开始时把其他通道幅度关到 0，免得多个波长同时照样品", "Set the other channels' amplitude to 0 at the start so only one wavelength reaches the sample", default=True),
                _spec("center_offset_nm", "float", "谱仪中心 = λ/2 + 该偏移", "Spectrometer centre = λ/2 + this offset", unit="nm", default=0.0, lo=-200.0, hi=200.0),
                _spec("center_tolerance_nm", "float", "与当前中心相差不超过它就不再重设（常见做法：λ 步长 2 nm、中心每 5 nm 才动一次）",
                      "Do not re-set the centre when it is within this of the present one (a common habit: 2 nm wavelength steps, centre moved every 5 nm)", unit="nm", default=0.0, lo=0.0, hi=100.0),
                _spec("center_fallback", "str", "驱动不能设谱仪中心时：prompt = 弹窗请人在 WITec 里设并填实际值；state = 载入预设 center_state；fail = 停",
                      "When the driver cannot set the centre: prompt = ask a person to set it in WITec and enter the value; state = load the preset center_state; fail = stop",
                      default="prompt", allowed=["prompt", "state", "fail"]),
                _spec("center_state", "str", "center_fallback=state 时载入的 WITec 预设名", "WITec preset to load when center_fallback=state", default=""),
                _spec("normalize", "str", "强度归一化：P2 = I/P²（默认），P = I/P，none = 不归一化", "Intensity normalisation: P2 = I/P² (default), P = I/P, none = raw", default="P2", allowed=["P2", "P", "none"]),
                _spec("waveplate_slot", "str", "登记激发光路 λ/2 片的光学状态槽位；none = 不检查", "Optical-state slot that registers the excitation λ/2 plate; none = no check", default=SLOT_WAVEPLATE),
                _spec("analyzer_deg", "float", "给了就先把 Analyzer 设到这个角度（显微镜）；不给 = 不动", "When given, set the Analyzer to this angle first (microscope); not given = leave it", unit="deg", lo=0.0, hi=360.0),
                _spec("power_prompt", "bool", "没有功率计时每步弹窗请操作员输入功率读数（mW）；false = 记 null", "Without a power meter ask the operator for a power reading (mW) at each step; false = record null", default=False),
                _spec("integration_window_nm", "float", "SHG 峰积分窗全宽 nm", "Full width of the SHG integration window in nm", unit="nm", default=8.0, lo=0.5, hi=200.0),
                _spec("search_nm", "float", "在 λ/2 ± 该值内找峰", "Search the peak within λ/2 ± this", unit="nm", default=10.0, lo=1.0, hi=200.0),
                *_source_specs(external=True, witec=False)[1:],     # manage_emission / ask_attest_laser / warmup_s
                *_common_specs(),
            ],
            estimated_duration_s=1800.0, composition_level=3, tags=[*_TAGS, "shg"],
        )

    # -- parse / devices / plan -----------------------------------------------------------------

    def _parse(self, params: dict) -> SimpleNamespace:
        c = _parse_common(params)
        _parse_source(params, c)
        c.witec_laser, c.witec_power_mw = "", None        # SHG light comes from the external source
        c.channel = _as_int(params, "channel", 0, lo=0, hi=63)
        c.amplitude_pct = _as_float(params, "amplitude_pct", None, lo=0.0, hi=100.0)
        if c.amplitude_pct is None:
            raise ProtocolRefused(tr("amplitude_pct 必填（没有安全的默认值）", "amplitude_pct is required (there is no safe default)"))
        c.wavelengths = _series(params, list_key="wavelengths_nm", start_key="start_nm", stop_key="stop_nm",
                                step_key="step_nm", label=tr("激发波长", "excitation wavelengths"))
        c.isolate_channel = _as_bool(params, "isolate_channel", True)
        c.center_offset_nm = _as_float(params, "center_offset_nm", 0.0, lo=-200.0, hi=200.0)
        c.center_tolerance_nm = _as_float(params, "center_tolerance_nm", 0.0, lo=0.0, hi=100.0)
        c.center_fallback = _as_choice(params, "center_fallback", "prompt", ("prompt", "state", "fail"))
        c.center_state = _as_str(params, "center_state", "")
        if c.center_fallback == "state" and not c.center_state:
            raise ProtocolRefused(tr("center_fallback=state 需要 center_state", "center_fallback=state needs center_state"))
        c.normalize = _as_choice(params, "normalize", "P2", ("p2", "p", "none"))
        slot = _as_str(params, "waveplate_slot", SLOT_WAVEPLATE)
        c.waveplate_slot = "none" if slot.lower() == "none" else slot
        c.analyzer_deg = _as_float(params, "analyzer_deg", None, lo=0.0, hi=360.0)
        c.power_prompt = _as_bool(params, "power_prompt", False)
        c.integration_window_nm = _as_float(params, "integration_window_nm", 8.0, lo=0.5, hi=200.0)
        c.search_nm = _as_float(params, "search_nm", 10.0, lo=1.0, hi=200.0)
        return c

    def _rig_needs(self, cfg: SimpleNamespace) -> dict:
        return {"laser": "required", "temperature": "optional", "require_mic": False}

    def _preflight(self, cfg: SimpleNamespace, rig: _Rig, plan: _Plan, *, read_hw: bool) -> list[str]:
        out = super()._preflight(cfg, rig, plan, read_hw=read_hw)
        if read_hw:                                   # reads of the source (channel table, emission state); nothing is written
            try:
                chans = rig.laser.channels()
            except Exception as exc:  # noqa: BLE001
                out.append(tr(f"读不到光源的通道表: {exc}", f"cannot read the source's channel table: {exc}"))
            else:
                n = len(chans)
                if not 0 <= cfg.channel < n:
                    out.append(tr(f"通道 {cfg.channel} 不在 [0, {n - 1}] 内", f"channel {cfg.channel} is outside [0, {n - 1}]"))
                else:
                    out += self._check_amplitude_raise(cfg, rig, chans)
        elif self._raise_is_gated(cfg, rig):          # a dry run reads nothing: say what to look out for
            plan.notes.append(tr(
                f"试跑读不到光源：设备 allow_emission_control=false 时，光源出光期间软件不加光 —— 若开始时它已经在出光，通道 {cfg.channel} 的幅度必须已经 ≥ {cfg.amplitude_pct:g}%"
                f"（请先在厂商软件里手动设好；协议只会把它调暗到 {cfg.amplitude_pct:g}%、调颜色），或在自检通过后打开 allow_emission_control；光源还关着的话，协议先把幅度设好，再请你点亮",
                f"a dry run cannot read the source: with allow_emission_control=false software does not add light while the source is emitting — if it is already "
                f"emitting when you start, channel {cfg.channel} must already be at {cfg.amplitude_pct:g}% or more (set it by hand in the vendor software first; the "
                f"protocol only dims it to {cfg.amplitude_pct:g}% and tunes the colour), or enable allow_emission_control after the bench self-test; a source that is "
                "still off gets its amplitude set first and you are asked to switch it on afterwards"))
        return out

    @staticmethod
    def _raise_is_gated(cfg: SimpleNamespace, rig: _Rig) -> bool:
        """``_prepare`` sets the channel to ``amplitude_pct`` and the device config does not let software add light
        (docs 6.4: ``allow_emission_control: false`` means MAST only dims, tunes the colour and shuts off)."""
        return not getattr(rig.laser, "allow_emission_control", False) and cfg.amplitude_pct > 0

    def _check_amplitude_raise(self, cfg: SimpleNamespace, rig: _Rig, chans: list[dict]) -> list[str]:
        """With ``allow_emission_control=false`` the plan may not raise the channel of a source that is emitting.

        While the source emits, every increase - from 0, or on a channel that is already lit - passes the full
        emission gate in the drivers, so ``_prepare`` setting ``amplitude_pct`` above the present amplitude would be
        refused by the driver, halfway through the preparation. It is refused here instead, before anything is
        touched. Fine: the same amplitude or a lower one (the protocol dims), a requested amplitude of 0, and a
        source that is off (the raise adds no light; the operator switches the source on at the confirm that
        follows). An amplitude that cannot be read counts as "possibly lower", an emission state that cannot be
        read as "emitting" (fail closed), as in the drivers."""
        if not self._raise_is_gated(cfg, rig):
            return []
        row = next((c for c in chans if _as_number(c.get("channel")) == cfg.channel), None)
        cur = _as_number((row or {}).get("amplitude_pct"))
        if cur is not None and cfg.amplitude_pct <= cur + 1e-9 + 1e-6 * abs(cur):
            return []
        if not self._source_may_be_live(rig.laser):
            return []
        shown_zh, shown_en = (f"{cur:g}%", f"{cur:g}%") if cur is not None else ("一个读不到的幅度", "an amplitude that cannot be read")
        return [tr(
            f"{rig.laser_id}: 设备配置里 allow_emission_control=false，而光源正在出光（或读不到出光状态），所以软件不加光；计划却要把通道 {cfg.channel} 从 {shown_zh} 抬到 "
            f"{cfg.amplitude_pct:g}%，驱动会拒绝。有两条路：① 开始前在厂商软件里手动把通道 {cfg.channel} 设到至少 {cfg.amplitude_pct:g}%"
            f"（之后协议只会把它调暗到 {cfg.amplitude_pct:g}%、调颜色）；② 台上自检通过后，在设备配置里打开 allow_emission_control",
            f"{rig.laser_id}: allow_emission_control is false and the source is emitting (or its emission state cannot be read), so software adds no light — yet the plan "
            f"raises channel {cfg.channel} from {shown_en} to {cfg.amplitude_pct:g}%, which the driver would refuse. Two ways out: (1) before starting, set channel "
            f"{cfg.channel} to at least {cfg.amplitude_pct:g}% by hand in the vendor software (afterwards the protocol only dims it to {cfg.amplitude_pct:g}% and tunes the "
            "colour); (2) enable allow_emission_control in the device config after the bench self-test")]

    def _raise_hint(self, run: _Run) -> tuple[str, str]:
        cfg = run.cfg
        if not self._raise_is_gated(cfg, run.rig):
            return "", ""
        return (f"设备配置里 allow_emission_control=false：光源一出光，软件就不再加光。协议已在光源还关着时把通道 {cfg.channel} 设成 {cfg.amplitude_pct:g}%；"
                f"打开光源后若厂商软件里显示低于 {cfg.amplitude_pct:g}%，请手动设上去（之后协议只会把它调暗、调颜色）。",
                f"The device config has allow_emission_control=false: once the source emits, software adds no light. The protocol set channel {cfg.channel} to "
                f"{cfg.amplitude_pct:g}% while the source was still off; if the vendor software shows less than {cfg.amplitude_pct:g}% after you switch the source on, "
                "set it by hand (afterwards the protocol only dims it and tunes the colour).")

    def _plan(self, cfg: SimpleNamespace, rig: _Rig) -> _Plan:
        problems: list[str] = []
        notes: list[str] = []
        laser, spec = rig.laser, rig.spec
        if "wavelength" not in getattr(laser, "SUPPORTS", ()):
            problems.append(tr(f"{rig.laser_id} 不是可调谐光源（没有 wavelength 能力）", f"{rig.laser_id} is not a tunable source (no wavelength capability)"))
        store = optical_state.get_store()
        plate_opts: list[dict] | None = None
        cur_opt: str | None = None
        if cfg.waveplate_slot != "none":
            try:
                slot = store.slot(cfg.waveplate_slot)
                plate_opts = [dict(o) for o in slot.options]
                sv = store.get(cfg.waveplate_slot)
                cur_opt = None if sv is None else str(sv.value)
            except optical_state.OpticalStateError as exc:
                problems.append(str(exc))
        if not getattr(laser, "write_enabled", True):
            problems.append(tr(f"{rig.laser_id}: write_enabled=false，不能设波长/幅度（实机自检后才在配置里打开）",
                               f"{rig.laser_id}: write_enabled is false, wavelength/amplitude cannot be set (enable it after the bench self-test)"))
        lim = laser.limits
        slim = spec.limits
        out_lam: list[float] = []
        out_center: list[float] = []
        out_pm: list[float] = []
        no_plate: list[float] = []
        if lim.min_nm is None or lim.max_nm is None:
            problems.append(tr(f"{rig.laser_id}: 没有配置波长范围 —— 拒绝", f"{rig.laser_id}: no wavelength range configured — refused"))
        if lim.max_amplitude_pct is None:
            problems.append(tr(f"{rig.laser_id}: 没有配置 max_amplitude_pct —— 拒绝", f"{rig.laser_id}: no max_amplitude_pct configured — refused"))
        elif not 0.0 <= cfg.amplitude_pct <= lim.max_amplitude_pct:
            problems.append(tr(f"幅度 {cfg.amplitude_pct:g}% 超出 [0, {lim.max_amplitude_pct:g}]%", f"amplitude {cfg.amplitude_pct:g}% is outside [0, {lim.max_amplitude_pct:g}]%"))
        if rig.mic is not None and cfg.analyzer_deg is not None:
            lo, hi = rig.mic.microscope_limits.analyzer_deg
            if not lo <= cfg.analyzer_deg <= hi:
                problems.append(tr(f"Analyzer 角 {cfg.analyzer_deg:g}° 超出 [{lo:g}, {hi:g}]°", f"analyzer angle {cfg.analyzer_deg:g}° is outside [{lo:g}, {hi:g}]°"))
        if cfg.analyzer_deg is not None and rig.mic is None:
            problems.append(tr("analyzer_deg 需要显微镜", "analyzer_deg needs a microscope"))
        steps: list[_Step] = []
        prev_center: float | None = None
        n_center_moves = 0
        for i, lam in enumerate(cfg.wavelengths, start=1):
            manual: list[str] = []
            if lim.min_nm is not None and lim.max_nm is not None and not lim.min_nm <= lam <= lim.max_nm:
                out_lam.append(lam)
            center = lam / 2.0 + cfg.center_offset_nm
            if not slim.min_center_nm <= center <= slim.max_center_nm:
                out_center.append(lam)
            if rig.pm is not None and not rig.pm.MIN_NM <= lam <= rig.pm.MAX_NM:
                out_pm.append(lam)
            if plate_opts is not None:
                cur = next((o for o in plate_opts if str(o.get("id")) == cur_opt), None) if cur_opt else None
                if cur is None or _option_covers(cur, lam) is not True:
                    cand = [o for o in plate_opts if _option_covers(o, lam)]
                    if cand:
                        manual.append("waveplate")
                        cur_opt = str(cand[0]["id"])
                    else:
                        no_plate.append(lam)
            est = cfg.settle_s + cfg.integration_s * cfg.accumulations + 1.0 + (0.5 if rig.pm is not None else 0.0)
            if prev_center is None or abs(center - prev_center) > cfg.center_tolerance_nm:
                est += 3.0
                prev_center = center
                n_center_moves += 1
            if cfg.power_prompt and rig.pm is None:
                manual.append("power_reading")
            steps.append(_Step(i, f"λ={_sig(lam, 6)} nm", {
                "excitation_nm": lam, "spectrometer_center_nm": center, "amplitude_pct": cfg.amplitude_pct,
                "channel": cfg.channel, "analyzer_deg": cfg.analyzer_deg}, manual, est))
        if out_lam:
            problems.append(tr(f"{len(out_lam)} 个波长超出光源范围 [{lim.min_nm}, {lim.max_nm}] nm（如 {out_lam[0]:g}）",
                               f"{len(out_lam)} wavelength(s) outside the source range [{lim.min_nm}, {lim.max_nm}] nm (e.g. {out_lam[0]:g})"))
        if out_center:
            problems.append(tr(f"{len(out_center)} 个 λ/2+偏移 超出谱仪中心范围 [{slim.min_center_nm:g}, {slim.max_center_nm:g}] nm（如 λ={out_center[0]:g}）",
                               f"{len(out_center)} value(s) of λ/2+offset are outside the spectrometer centre range [{slim.min_center_nm:g}, {slim.max_center_nm:g}] nm (e.g. λ={out_center[0]:g})"))
        if out_pm:
            problems.append(tr(f"{len(out_pm)} 个波长超出功率计校正范围 [{rig.pm.MIN_NM:g}, {rig.pm.MAX_NM:g}] nm", f"{len(out_pm)} wavelength(s) outside the power-meter correction range [{rig.pm.MIN_NM:g}, {rig.pm.MAX_NM:g}] nm"))
        if no_plate:
            problems.append(tr(
                f"槽位 {cfg.waveplate_slot} 里没有任何一片覆盖 {len(no_plate)} 个波长（如 {no_plate[0]:g} nm）—— 请在 optical_station.json 里补片，或用 waveplate_slot=none 跳过检查",
                f"no plate in slot {cfg.waveplate_slot} covers {len(no_plate)} wavelength(s) (e.g. {no_plate[0]:g} nm) — add a plate in optical_station.json or pass waveplate_slot=none to skip the check"))
        n_swaps = sum(1 for s in steps if "waveplate" in s.manual)
        if n_swaps:
            notes.append(tr(f"按现在登记的状态，预计要换/确认 λ/2 片 {n_swaps} 次", f"with the state registered now, the λ/2 plate has to be swapped/confirmed {n_swaps} time(s)"))
        if cfg.center_fallback == "prompt":
            notes.append(tr(f"若驱动不能经 COM 设谱仪中心，最多会为 {n_center_moves} 次中心变化各弹一次窗（center_fallback=prompt）",
                            f"if the driver cannot set the spectrometer centre over COM, up to {n_center_moves} centre prompt(s) will be posted (center_fallback=prompt)"))
        if rig.pm is None and not cfg.power_prompt:
            notes.append(tr("没有功率计：功率记为 null，P 归一化不可用", "no power meter: power is recorded as null and the P-normalisation is unavailable"))
        return _Plan(steps, problems, notes)

    # -- run ----------------------------------------------------------------------------------------------

    def _prepare(self, run: _Run) -> None:
        cfg, rig = run.cfg, run.rig
        mic, laser = rig.mic, rig.laser
        if mic is not None:
            run.close_shutter("close the WITec shutter")
            if cfg.beam_path != "none":
                run.hw("set the beam path", mic.set_beam_path, cfg.beam_path)
            if cfg.analyzer_deg is not None:
                run.hw("set the analyzer", mic.set_analyzer_deg, cfg.analyzer_deg)
        if cfg.isolate_channel:
            for ch in run.hw("read the source channels", laser.channels):
                idx = int(ch.get("channel", -1))
                if idx != cfg.channel and float(ch.get("amplitude_pct") or 0.0) > 0.0:
                    run.hw(f"zero channel {idx}", laser.set_amplitude_pct, 0.0, channel=idx)
        first = cfg.wavelengths[0]
        self._ensure_waveplate(run, first)           # before any light is on
        run.hw("set the source wavelength", laser.set_wavelength_nm, first, channel=cfg.channel)
        run.hw("set the source amplitude", laser.set_amplitude_pct, cfg.amplitude_pct, channel=cfg.channel)
        run.bring_up_source()

    def _ensure_waveplate(self, run: _Run, lam: float) -> None:
        slot_id = run.cfg.waveplate_slot
        if slot_id == "none":
            return
        store = optical_state.get_store()
        if store.waveplate_covers(slot_id, lam) is True:
            return
        slot = store.slot(slot_id)
        cur = store.get(slot_id)
        covering = [str(o["id"]) for o in slot.options if _option_covers(dict(o), lam)]
        # the prompt shows the slot's own labels (an asker's labels are dropped), so a mark on the option itself would
        # never reach the operator: which plates cover this wavelength goes into the message
        options = [PromptOption(str(o["id"]), str(o.get("label") or o["id"])) for o in slot.options]
        names = {str(o["id"]): f"{o.get('label') or o['id']} ({o['id']})" for o in slot.options}
        cur_txt = names.get(str(cur.value), str(cur.value)) if cur else None
        cover_zh = (f"覆盖 {lam:g} nm 的片：{'、'.join(names[i] for i in covering)}。" if covering
                    else f"槽位里没有登记任何覆盖 {lam:g} nm 的片。")
        cover_en = (f"Plates that cover {lam:g} nm: {', '.join(names[i] for i in covering)}." if covering
                    else f"No plate registered in the slot covers {lam:g} nm.")
        sw = run.begin_swap()                    # shutter shut, source dark (by software, else the operator is asked)
        state_zh, state_en = sw.describe()
        p = OperatorPrompt(
            kind="attest", severity="action", attest_slots=[slot_id], options=options,
            title=tr(f"请换激发光路 λ/2 片，使其覆盖 {lam:g} nm", f"Swap the excitation λ/2 plate so it covers {lam:g} nm"),
            message=tr(f"激发波长 {lam:g} nm，当前登记的片是 {cur_txt or '（未登记）'}，它不覆盖这个波长。{cover_zh}"
                       f"{state_zh}请换上合适的片，然后在下面签署你装上的是哪一片。",
                       f"Excitation wavelength {lam:g} nm; the plate registered now is {cur_txt or '(none registered)'} and does not cover it. {cover_en} "
                       f"{state_en} Swap in a suitable plate, then sign which plate you installed."),
            context={"reason": "waveplate", "excitation_nm": lam, "current": cur.value if cur else None, "covering": covering})
        run.ask(p)
        run.await_state(lambda: store.waveplate_covers(slot_id, lam) is True)
        if store.waveplate_covers(slot_id, lam) is not True:
            now = store.get(slot_id)
            raise ProtocolHalt("waveplate_not_covering", tr(
                f"签署的片（{now.value if now else '无'}）仍不覆盖 {lam:g} nm —— 停下",
                f"the plate signed ({now.value if now else 'none'}) still does not cover {lam:g} nm — stopping"))
        run.end_swap(sw)

    def _step(self, run: _Run, step: _Step) -> dict:
        cfg, rig = run.cfg, run.rig
        lam = float(step.conditions["excitation_nm"])
        self._ensure_waveplate(run, lam)
        run.hw("set the source wavelength", rig.laser.set_wavelength_nm, lam, channel=cfg.channel)
        run.sleep(cfg.settle_s)
        cen = run.set_center(lam / 2.0 + cfg.center_offset_nm)
        p_before = run.read_power_mw(lam)
        power_source = "meter" if rig.pm is not None else None
        if p_before is None and cfg.power_prompt:
            p_before, power_source = run.ask_power_mw(f"{lam:g} nm"), "operator"
        expo = run.expose(step, device_name=f"{run.run_id}_s{step.index:03d}",
                          meta={"excitation_nm": lam, "protocol_step": step.index})
        p_after = run.read_power_mw(lam, check=False) if rig.pm is not None else None
        reads = [p for p in (p_before, p_after) if p is not None]
        power = (sum(reads) / len(reads)) if reads else None
        drift = (abs(p_after - p_before) / power * 100.0) if (p_before is not None and p_after is not None and power) else None
        analysis = feature_integral(expo.axis, expo.mean, center_nm=lam / 2.0, window_nm=cfg.integration_window_nm,
                                    search_nm=cfg.search_nm)
        integral = analysis.get("integral")
        i_p = (integral / power) if (integral is not None and power) else None
        i_p2 = (integral / power ** 2) if (integral is not None and power) else None
        i_norm = {"p2": i_p2, "p": i_p, "none": integral}[cfg.normalize]
        cond = run.base_conditions()
        sv = optical_state.get_store().get(cfg.waveplate_slot) if cfg.waveplate_slot != "none" else None
        cond.update(
            excitation_nm=lam, expected_shg_nm=lam / 2.0, power_mw=power, power_before_mw=p_before,
            power_after_mw=p_after, power_source=power_source, power_drift_pct=drift,
            amplitude_pct=cfg.amplitude_pct, channel=cfg.channel, source_mode=run.source_mode,
            center_request_nm=lam / 2.0 + cfg.center_offset_nm, center_method=cen.get("method"),
            waveplate=None if sv is None else sv.value, normalize=cfg.normalize)
        if cen.get("center_nm") is not None:
            cond["spectrometer_center_nm"] = cen["center_nm"]
        ana = cond["analyzer_deg"] if cond["analyzer_deg"] is not None else cfg.analyzer_deg
        stem = _stem(self.SHORT, f"{_sig(lam, 6)}nm", f"p{_sig(power, 3)}mW" if power is not None else "",
                     f"a{_sig(ana, 4)}deg" if ana is not None else "", tag=cfg.tag)
        stored = run.store_step(step, expo, stem=stem, conditions=cond, analysis={
            **analysis, "intensity": integral, "i_over_p": i_p, "i_over_p2": i_p2, "i_norm": i_norm})
        if not analysis.get("found"):
            run.warn_limited("no_peak", tr(f"第 {step.index} 步 (λ={lam:g} nm): 在 {lam / 2:.1f} nm 附近没有找到 SHG 峰（{analysis.get('reason', '')}）",
                                           f"step {step.index} (λ={lam:g} nm): no SHG peak found near {lam / 2:.1f} nm ({analysis.get('reason', '')})"))
        if power is None and cfg.normalize != "none":
            run.warn_limited("no_power", tr(f"第 {step.index} 步: 没有功率，{cfg.normalize} 归一化不可用", f"step {step.index}: no power, the {cfg.normalize} normalisation is unavailable"))
        return {
            "step": step.index, "time_utc": expo.started, "excitation_nm": lam, "expected_shg_nm": lam / 2.0,
            "center_nm": cond["spectrometer_center_nm"], "power_mw": power, "power_drift_pct": drift,
            "amplitude_pct": cfg.amplitude_pct, "analyzer_deg": ana, "temperature_c": cond["temperature_c"],
            "intensity": integral, "i_over_p": i_p, "i_over_p2": i_p2, "i_norm": i_norm,
            "peak_nm": analysis.get("peak_nm"), "peak_offset_nm": analysis.get("peak_offset_nm"),
            "peak_counts": analysis.get("peak_counts"), "snr": analysis.get("snr"), "found": analysis.get("found"),
            "saturated": expo.saturated, "waveplate": cond["waveplate"], "stem": stored["stem"], "file": stored["paths"][0],
        }

    def finish_analysis(self, run: _Run) -> dict:
        rows = run.rows
        offs = [abs(r["peak_offset_nm"]) for r in rows if r.get("found") and r.get("peak_offset_nm") is not None]
        return {
            "normalize": run.cfg.normalize, "n_steps": len(rows),
            "n_peak_found": sum(1 for r in rows if r.get("found")),
            "n_with_power": sum(1 for r in rows if r.get("power_mw") is not None),
            "spread_raw": _spread(r.get("intensity") for r in rows),
            "spread_i_over_p": _spread(r.get("i_over_p") for r in rows),
            "spread_i_over_p2": _spread(r.get("i_over_p2") for r in rows),
            "peak_offset_abs_max_nm": max(offs) if offs else None,
            "spread_note": "std/mean over the steps; a flat I/P² means the profile followed P²",
        }

    def plot(self, run: _Run, path: Path) -> Path | None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        rows = run.rows
        x = [r["excitation_nm"] for r in rows]

        def col(key: str) -> list[float]:
            return [float("nan") if r.get(key) is None else float(r[key]) for r in rows]

        fig, axes = plt.subplots(3, 1, sharex=True, figsize=(7.5, 8.0))
        axes[0].plot(x, col("power_mw"), "o-", ms=3, lw=0.8)
        axes[0].set_ylabel("P (mW)")
        axes[1].plot(x, col("intensity"), "o-", ms=3, lw=0.8)
        axes[1].set_ylabel("I (counts·nm)")
        axes[2].plot(x, col("i_over_p2"), "o-", ms=3, lw=0.8, label="I/P²")
        axes[2].set_ylabel("I/P²")
        axes[2].set_xlabel("Excitation wavelength (nm)")
        for ax in axes:
            ax.grid(alpha=0.3)
        fig.suptitle(f"SHG excitation scan · {run.run_id} · {run.status}")
        fig.tight_layout()
        fig.savefig(path, dpi=140)
        plt.close(fig)
        return path


# ── shared bits of the generic (single-feature) protocols ─────────────────────────────


def _center_specs() -> list[ParameterSpec]:
    return [
        _spec("center_nm", "float", "取谱前把谱仪中心设到这里（一次）；不给 = 不动", "Spectrometer centre set once before the measurement; not given = leave it",
              unit="nm", lo=100.0, hi=3000.0),
        _spec("center_fallback", "str", "驱动不能设谱仪中心时：prompt = 弹窗请人在 WITec 里设并填实际值；state = 载入预设 center_state；fail = 停",
              "When the driver cannot set the centre: prompt = ask a person to set it in WITec and enter the value; state = load the preset center_state; fail = stop",
              default="prompt", allowed=["prompt", "state", "fail"]),
        _spec("center_state", "str", "center_fallback=state 时载入的 WITec 预设名", "WITec preset to load when center_fallback=state", default=""),
    ]


def _feature_specs() -> list[ParameterSpec]:
    return [
        _spec("feature_nm", "float", "要积分的谱峰位置 nm；不给 = 取第一条谱最强的峰并在整次运行里固定（低信噪比的第一条谱可能选错，此时请显式给出）",
              "Position in nm of the feature to integrate; not given = the strongest peak of the first spectrum, then fixed for the run (a noisy first spectrum can pick wrongly — give it explicitly then)",
              unit="nm", lo=100.0, hi=3000.0),
        _spec("feature_window_nm", "float", "积分窗全宽 nm（找峰范围为 ±2×该值）", "Full width of the integration window in nm (the peak is searched within ±2× this)",
              unit="nm", default=10.0, lo=0.5, hi=500.0),
    ]


def _parse_center(params: dict, c: SimpleNamespace) -> None:
    c.center_nm = _as_float(params, "center_nm", None, lo=100.0, hi=3000.0)
    c.center_fallback = _as_choice(params, "center_fallback", "prompt", ("prompt", "state", "fail"))
    c.center_state = _as_str(params, "center_state", "")
    c.center_tolerance_nm = 0.0
    if c.center_fallback == "state" and not c.center_state:
        raise ProtocolRefused(tr("center_fallback=state 需要 center_state", "center_fallback=state needs center_state"))


def _parse_feature(params: dict, c: SimpleNamespace) -> None:
    c.feature_nm = _as_float(params, "feature_nm", None, lo=100.0, hi=3000.0)
    c.feature_window_nm = _as_float(params, "feature_window_nm", 10.0, lo=0.5, hi=500.0)


def _feature_center(run: _Run, expo: SimpleNamespace) -> float | None:
    """The feature position: given, or the strongest peak of the first spectrum (fixed afterwards)."""
    if run.cfg.feature_nm is not None:
        return float(run.cfg.feature_nm)
    if run.feature_nm is None and expo.axis is not None:
        y = np.nan_to_num(np.asarray(expo.mean, dtype=np.float64))
        k = int(np.argmax(_running_median(y, 5)))
        if k < len(expo.axis) and math.isfinite(float(expo.axis[k])):
            run.feature_nm = float(expo.axis[k])
    return run.feature_nm


def _feature_analysis(run: _Run, expo: SimpleNamespace) -> dict:
    center = _feature_center(run, expo)
    if center is None:
        return {"found": False, "reason": "no feature position (no wavelength axis)"}
    w = run.cfg.feature_window_nm
    out = feature_integral(expo.axis, expo.mean, center_nm=center, window_nm=w, search_nm=2.0 * w)
    out["tracked_nm"] = center
    return out


def _prepare_light(run: _Run, *, analyzer_deg: float | None = None) -> None:
    """Common start: shutter closed, beam path, analyzer, WITec laser, centre — NOT the external source."""
    cfg, rig = run.cfg, run.rig
    mic = rig.mic
    if mic is not None:
        run.close_shutter("close the WITec shutter")
        if cfg.beam_path != "none":
            run.hw("set the beam path", mic.set_beam_path, cfg.beam_path)
        if analyzer_deg is not None:
            run.hw("set the analyzer", mic.set_analyzer_deg, analyzer_deg)
        if getattr(cfg, "witec_laser", ""):
            run.hw("select the WITec laser", mic.select_laser, cfg.witec_laser)
            # WITec's shutter belongs to the SELECTED laser: the one just selected may have been left open by a
            # person, and the close above was for the previous one. Shut it before its power is touched.
            run.close_shutter("close the shutter of the WITec laser just selected")
            if getattr(cfg, "witec_power_mw", None) is not None:
                run.hw("set the WITec laser power", mic.set_laser_power_mw, cfg.witec_power_mw)
    if getattr(cfg, "center_nm", None) is not None:
        run.set_center(float(cfg.center_nm))


def _source_exclusive(c: SimpleNamespace) -> None:
    if c.witec_laser and c.laser_id and c.laser_id.lower() not in ("none",):
        raise ProtocolRefused(tr("witec_laser 和 laser_id 只能给一种光源", "give either witec_laser or laser_id, not both"))


def _temperature_label(cond: dict) -> str:
    t = cond.get("temperature_c")
    return "" if t is None else f"T{_sig(t, 4)}C"


# ═══════════════════════════════════════════════════════════════════════════════════
# 2. PolarizationMatrix
# ═══════════════════════════════════════════════════════════════════════════════════


class PolarizationMatrix(_Protocol):
    """par/crs polarisation matrix: manual incident polarisation x analyzer in software.

    For every incident angle theta the operator turns the incident polariser by hand and **signs the
    angle actually set** (``attest`` prompt on slot ``incident_polarizer_deg``; ``angle_entry=confirm``
    makes it a one-click "I set the requested angle"). Then, per mode, the analyzer is set by software
    and a spectrum is taken. ``both`` takes par then crs at the same theta (one prompt per theta), the
    order the lab used.

    Analyzer reading = ``analyzer_scale·theta + analyzer_offset_deg`` (+ ``crs_offset_deg`` for crs); the
    defaults reproduce the lab's history (par ``theta/2``, crs ``theta/2 + 135``) — derivation and the
    doubts about it are in the module docstring.
    """

    NAME = "PolarizationMatrix"
    SHORT = "polmat"
    CSV_COLUMNS = ("step", "time_utc", "mode", "incident_deg_requested", "incident_deg_actual", "analyzer_deg",
                   "intensity", "peak_nm", "peak_counts", "snr", "found", "saturated", "power_mw",
                   "temperature_c", "stem", "file")

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=self.NAME, version=SKILL_VERSION, category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr(
                "par/crs 偏振矩阵（光学站协议）：入射偏振没有软件轴，所以每个入射角弹一次 number 型 attest 提示，请操作员把入射偏振转到 θ 并签署实际角度"
                "（槽位 incident_polarizer_deg；angle_entry=confirm 时改成一键确认；转之前先关 WITec 快门、把外部光源关出光——软件能关就关并读回确认，关不掉就请操作员手动关或挡光——转好后再恢复）；分析器由软件设（par: θ/2，crs: θ/2+135——实验室历史做法，"
                "可用 analyzer_scale/analyzer_offset_deg/crs_offset_deg 改）。mode=both 在同一个 θ 下先 par 后 crs（只弹一次）。"
                "光源三选一：WITec 自带激光（witec_laser，只在取谱期间开快门）、外部光源（laser_id，软件开出光另需 allow_emission_control）、"
                "或都不给（光由操作员手动开好）。用光前要有 laser_area_safe 有效签署（开始前、每步、每次曝光前都查，无效时弹窗请签，签不上就安全收尾停下）。每步立刻存 .npy + .json（文件名含模式/入射角/分析器角，sidecar 含全部条件与光学状态快照）并更新汇总；"
                "任何停止都走安全收尾，已采数据保留。" "别人在运行中按了停止（StopWITec / CloseWITecShutter / LaserEmissionOff / StopTemperatureRamp、撤销签署、急停）：下一步就干净停下（stop_kind=stopped_externally），不再重开快门/光源、不再下温度设定。"
                "dry_run=true 只返回计划（含要弹几次窗），不碰硬件。越限拒绝、不钳位。",
                "par/crs polarisation matrix (optical-station protocol): the incident polarisation has no software axis, so for every incident angle a number-type attest prompt asks the "
                "operator to turn the polariser to θ and sign the angle actually set (slot incident_polarizer_deg; angle_entry=confirm makes it a one-click confirmation; before the turn the WITec shutter is closed and the external source is taken dark - by software when it can, read back, otherwise the operator is asked - and afterwards restored); the "
                "analyzer is set by software (par: θ/2, crs: θ/2+135 — the lab's history, adjustable via analyzer_scale/analyzer_offset_deg/crs_offset_deg). mode=both takes par then crs "
                "at the same θ (one prompt). Light: a WITec laser (witec_laser, shutter open only during acquisitions), an external source (laser_id; switching it on in software also needs "
                "allow_emission_control), or neither (the operator has switched the light on). Laser light needs a valid laser_area_safe attestation (checked before the start, at every step and before every exposure; "
                "the operator is asked to sign when it is missing/expired, and if nobody signs the run stops safely). Each step is saved at once as .npy + .json (file name carries mode/incident angle/analyzer angle; the sidecar carries all "
                "conditions and the optical-state snapshot) and the summary is updated; any stop takes the safe direction and keeps the data. "
                "A stop somebody else asks for during the run (StopWITec / CloseWITecShutter / LaserEmissionOff / StopTemperatureRamp, withdrawing the attestation, the E-STOP) ends it cleanly at the next check (stop_kind=stopped_externally); the shutter, the source and the setpoint are not touched again. dry_run=true only returns the plan (incl. how many prompts to expect) and touches no hardware. Out of range is refused, never clamped."),
            parameters=[
                _spec("incident_angles_deg", "str", "入射偏振角表 °：'0,10,20' 或 JSON 数组；与 start_deg/stop_deg/step_deg 二选一；都不给 = 0…360 每 10°",
                      "Incident polarisation angles in °: '0,10,20' or a JSON array; use either this or start_deg/stop_deg/step_deg; neither = 0…360 every 10°", default=""),
                _spec("start_deg", "float", "起始入射角 °", "Start incident angle (°)", unit="deg", lo=0.0, hi=360.0),
                _spec("stop_deg", "float", "终止入射角 °（含）", "Stop incident angle (°, inclusive)", unit="deg", lo=0.0, hi=360.0),
                _spec("step_deg", "float", "入射角步长 °", "Incident angle step (°)", unit="deg", lo=0.1, hi=360.0),
                _spec("mode", "str", "par = 分析器平行入射偏振，crs = 垂直，both = 每个角先 par 后 crs", "par = analyzer parallel to the incident polarisation, crs = crossed, both = par then crs at each angle",
                      default="both", allowed=["par", "crs", "both"]),
                _spec("analyzer_scale", "float", "分析器读数 = scale·θ + offset（历史做法 0.5：分析器读数是有效偏振角的一半）", "Analyzer reading = scale·θ + offset (history: 0.5 — the reading is half the effective polarisation angle)", default=0.5, lo=0.001, hi=100.0),
                _spec("analyzer_offset_deg", "float", "两种模式共用的零点偏移 °（台子对零的校准量）", "Zero offset in ° shared by both modes (the bench's alignment of zero)", unit="deg", default=0.0, lo=-360.0, hi=360.0),
                _spec("crs_offset_deg", "float", "crs 比 par 多出的读数 °（历史 135，在读数周期 90° 下等价于 45）", "Extra reading of crs over par in ° (history 135, equivalent to 45 for a reading period of 90°)", unit="deg", default=135.0, lo=-360.0, hi=360.0),
                _spec("angle_entry", "str", "number = 操作员填实际角度；confirm = 一键确认'已转到要求的角度'（仍是操作员签署）", "number = the operator types the actual angle; confirm = one-click 'set to the requested angle' (still the operator's attestation)",
                      default="number", allowed=["number", "confirm"]),
                _spec("angle_tolerance_deg", "float", "签署角与要求角允许的差；超过就再问（最多三次）后停", "Allowed difference between the signed and the requested angle; beyond it the operator is asked again (three times at most), then the run stops",
                      unit="deg", default=2.0, lo=0.0, hi=45.0),
                *_center_specs(),
                *_feature_specs(),
                *_source_specs(external=True, witec=True),
                *_common_specs(),
            ],
            estimated_duration_s=2400.0, composition_level=3, tags=[*_TAGS, "polarization"],
        )

    def _parse(self, params: dict) -> SimpleNamespace:
        c = _parse_common(params)
        _parse_source(params, c)
        _source_exclusive(c)
        _parse_center(params, c)
        _parse_feature(params, c)
        c.angles = _series(params, list_key="incident_angles_deg", start_key="start_deg", stop_key="stop_deg",
                           step_key="step_deg", label=tr("入射角", "incident angles"), default=(0.0, 360.0, 10.0))
        bad = [a for a in c.angles if not 0.0 <= a <= 360.0]
        if bad:
            raise ProtocolRefused(tr(f"入射角必须在 [0, 360]° 内（如 {bad[0]:g}）", f"incident angles must lie in [0, 360]° (e.g. {bad[0]:g})"))
        c.mode = _as_choice(params, "mode", "both", ("par", "crs", "both"))
        c.analyzer_scale = _as_float(params, "analyzer_scale", 0.5, lo=0.001, hi=100.0)
        c.analyzer_offset_deg = _as_float(params, "analyzer_offset_deg", 0.0, lo=-360.0, hi=360.0)
        c.crs_offset_deg = _as_float(params, "crs_offset_deg", 135.0, lo=-360.0, hi=360.0)
        c.angle_entry = _as_choice(params, "angle_entry", "number", ("number", "confirm"))
        c.angle_tolerance_deg = _as_float(params, "angle_tolerance_deg", 2.0, lo=0.0, hi=45.0)
        return c

    def _rig_needs(self, cfg: SimpleNamespace) -> dict:
        return {"laser": "optional", "temperature": "optional", "require_mic": True}

    def _modes(self, cfg: SimpleNamespace) -> list[str]:
        return ["par", "crs"] if cfg.mode == "both" else [cfg.mode]

    def _reading(self, cfg: SimpleNamespace, theta: float, mode: str) -> float:
        return analyzer_reading(theta, mode, scale=cfg.analyzer_scale, zero=cfg.analyzer_offset_deg,
                                crs_offset=cfg.crs_offset_deg)

    def _plan(self, cfg: SimpleNamespace, rig: _Rig) -> _Plan:
        problems: list[str] = []
        notes: list[str] = []
        store = optical_state.get_store()
        try:
            store.slot(SLOT_INCIDENT)
        except optical_state.OpticalStateError as exc:
            problems.append(str(exc))
        lo, hi = rig.mic.microscope_limits.analyzer_deg
        slim = rig.spec.limits
        if cfg.center_nm is not None and not slim.min_center_nm <= cfg.center_nm <= slim.max_center_nm:
            problems.append(tr(f"谱仪中心 {cfg.center_nm:g} nm 超出 [{slim.min_center_nm:g}, {slim.max_center_nm:g}] nm",
                               f"spectrometer centre {cfg.center_nm:g} nm is outside [{slim.min_center_nm:g}, {slim.max_center_nm:g}] nm"))
        steps: list[_Step] = []
        bad: list[tuple[float, str, float]] = []
        i = 0
        for theta in cfg.angles:
            for k, mode in enumerate(self._modes(cfg)):
                i += 1
                reading = self._reading(cfg, theta, mode)
                if not lo <= reading <= hi:
                    bad.append((theta, mode, reading))
                est = cfg.settle_s + cfg.integration_s * cfg.accumulations + 1.5 + (0.5 if rig.pm is not None else 0.0)
                steps.append(_Step(i, f"θ={_sig(theta, 5)}° {mode}", {
                    "incident_deg": theta, "mode": mode, "analyzer_deg": reading},
                    ["incident_polarizer"] if k == 0 else [], est))
        if bad:
            t, m, r = bad[0]
            problems.append(tr(f"{len(bad)} 个分析器读数超出显微镜范围 [{lo:g}, {hi:g}]°（如 θ={t:g}° {m} → {r:g}°）",
                               f"{len(bad)} analyzer reading(s) outside the microscope range [{lo:g}, {hi:g}]° (e.g. θ={t:g}° {m} → {r:g}°)"))
        notes.append(tr(f"预计弹 {len(cfg.angles)} 次入射偏振签署提示（每个 θ 一次）", f"expect {len(cfg.angles)} incident-polariser attest prompts (one per θ)"))
        notes.append(tr("分析器读数按 analyzer_scale/offset/crs_offset 由 θ 推出（默认复现实验室历史：par=θ/2，crs=θ/2+135）",
                        "analyzer readings follow from θ via analyzer_scale/offset/crs_offset (defaults reproduce the lab's history: par=θ/2, crs=θ/2+135)"))
        if not cfg.witec_laser and not cfg.laser_id:
            notes.append(tr("没有指定光源：假定操作员已经把光开好", "no source given: the operator is assumed to have switched the light on"))
        if cfg.laser_id and cfg.laser_id.lower() != "none":
            notes.append(tr("每次转入射偏振之前：WITec 快门先关、外部光源先关出光（软件能关就关并读回确认，关不掉就请你手动关或挡光），转好后再恢复"
                            "（软件允许就自动开，否则请你开）；手动模式下每个 θ 会多弹一次「请重新打开光源」",
                            "before every incident-polariser turn the WITec shutter is closed and the external source is taken dark (by software when it can, read back; otherwise "
                            "you are asked to switch it off or block the beam), and it is restored afterwards (by software when allowed, otherwise you are asked); in manual mode "
                            "every θ costs one more \"switch the source back on\" prompt"))
        return _Plan(steps, problems, notes)

    def _prepare(self, run: _Run) -> None:
        _prepare_light(run)
        run.bring_up_source()

    def _ask_incident(self, run: _Run, theta: float) -> float:
        cfg = run.cfg
        store = optical_state.get_store()
        prev: float | None = None
        sw = run.begin_swap()                    # a person turns the polariser by hand: shutter shut, source dark first
        state_zh, state_en = sw.describe()
        for attempt in range(1, 4):
            note = ""
            if prev is not None:
                note = tr(f"（上一次你签署的是 {prev:g}°，与要求的 {theta:g}° 相差超过 {cfg.angle_tolerance_deg:g}°，请重新转到并签署。）",
                          f" (You signed {prev:g}° last time, more than {cfg.angle_tolerance_deg:g}° from the requested {theta:g}°; please set it again and sign.)")
            common = dict(kind="attest", severity="action", attest_slots=[SLOT_INCIDENT],
                          title=tr(f"请把入射偏振转到 {theta:g}°", f"Turn the incident polarisation to {theta:g}°"),
                          context={"reason": "incident_polarizer", "requested_deg": theta, "attempt": attempt})
            if cfg.angle_entry == "number":
                p = OperatorPrompt(unit="deg", number_min=0.0, number_max=360.0,
                                   message=tr(f"{state_zh}请把激发光路上的起偏器/λ/2 转到 {theta:g}°，然后填写你实际设的角度（°）。" + note,
                                              f"{state_en} Turn the polariser/λ/2 in the excitation path to {theta:g}°, then enter the angle you actually set (°)." + note),
                                   **common)
            else:
                p = OperatorPrompt(options=[PromptOption(_sig(theta, 6), tr(f"已转到 {theta:g}°", f"Set to {theta:g}°"))],
                                   message=tr(f"{state_zh}请把激发光路上的起偏器/λ/2 转到 {theta:g}°，转好后点确认。" + note,
                                              f"{state_en} Turn the polariser/λ/2 in the excitation path to {theta:g}°, then confirm." + note),
                                   **common)
            ans = run.ask(p)
            try:
                actual = float(ans.value)
            except (TypeError, ValueError):
                raise ProtocolHalt("error", tr(f"签署的值 {ans.value!r} 不是角度", f"the signed value {ans.value!r} is not an angle")) from None
            run.await_state(lambda a=actual: _slot_equals(store, SLOT_INCIDENT, a))
            sv = store.get(SLOT_INCIDENT)
            if sv is None or not isinstance(sv.value, (int, float)) or abs(float(sv.value) - actual) > 1e-9:
                raise ProtocolHalt("error", tr("签署没有进入光学状态登记（槽位值与回答不一致）—— 不带着不可追溯的角度往下做",
                                               "the attestation did not reach the optical-state registry (slot value differs from the answer) — not continuing with an untraceable angle"))
            if abs(_angle_diff(actual, theta)) <= cfg.angle_tolerance_deg:
                run.end_swap(sw)                 # the hands are out: the source comes back the way it was
                return actual
            prev = actual
        raise ProtocolHalt("polarizer_mismatch", tr(
            f"入射偏振签署的角度三次都偏离要求的 {theta:g}° 超过 {cfg.angle_tolerance_deg:g}° —— 停下",
            f"the signed incident angle was off the requested {theta:g}° by more than {cfg.angle_tolerance_deg:g}° three times — stopping"))

    def _step(self, run: _Run, step: _Step) -> dict:
        cfg, rig = run.cfg, run.rig
        theta = float(step.conditions["incident_deg"])
        mode = str(step.conditions["mode"])
        if run.last_incident_requested != theta:
            run.last_incident_actual = self._ask_incident(run, theta)
            run.last_incident_requested = theta
        actual = float(run.last_incident_actual)
        reading = self._reading(cfg, actual, mode)
        run.hw("set the analyzer", rig.mic.set_analyzer_deg, reading)
        run.sleep(cfg.settle_s)
        p_before = run.read_power_mw(None)
        expo = run.expose(step, device_name=f"{run.run_id}_s{step.index:03d}",
                          meta={"mode": mode, "incident_deg": actual, "analyzer_deg": reading})
        p_after = run.read_power_mw(None, check=False) if rig.pm is not None else None
        reads = [p for p in (p_before, p_after) if p is not None]
        power = (sum(reads) / len(reads)) if reads else None
        analysis = _feature_analysis(run, expo)
        cond = run.base_conditions()
        cond.update(mode=mode, incident_polarizer_deg=actual, incident_polarizer_requested_deg=theta,
                    analyzer_deg=reading, analyzer_scale=cfg.analyzer_scale, analyzer_offset_deg=cfg.analyzer_offset_deg,
                    crs_offset_deg=cfg.crs_offset_deg, power_mw=power, source_mode=run.source_mode,
                    witec_laser=cfg.witec_laser or None)
        if cfg.witec_laser:
            cond["excitation_nm"] = self._witec_nm(run)
        elif rig.laser is not None:
            cond["excitation_nm"] = self._laser_nm(rig.laser)
        stem = _stem(self.SHORT, mode, f"i{_sig(actual, 4)}deg", f"a{_sig(reading, 5)}deg", tag=cfg.tag)
        stored = run.store_step(step, expo, stem=stem, conditions=cond, analysis=analysis)
        if not analysis.get("found"):
            run.warn_limited("no_feature", tr(f"第 {step.index} 步: 没有找到积分用的谱峰（{analysis.get('reason', '')}）",
                                              f"step {step.index}: the feature to integrate was not found ({analysis.get('reason', '')})"))
        return {
            "step": step.index, "time_utc": expo.started, "mode": mode, "incident_deg_requested": theta,
            "incident_deg_actual": actual, "analyzer_deg": reading, "intensity": analysis.get("integral"),
            "peak_nm": analysis.get("peak_nm"), "peak_counts": analysis.get("peak_counts"), "snr": analysis.get("snr"),
            "found": analysis.get("found"), "saturated": expo.saturated, "power_mw": power,
            "temperature_c": cond["temperature_c"], "stem": stored["stem"], "file": stored["paths"][0],
        }

    @staticmethod
    def _witec_nm(run: _Run) -> float | None:
        try:
            las = _match_laser(run.rig.mic.list_lasers(), run.cfg.witec_laser)
            return None if las is None or las.get("wavelength_nm") is None else float(las["wavelength_nm"])
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _laser_nm(laser: Any) -> float | None:
        try:
            if hasattr(laser, "get_wavelength_nm"):
                return laser.get_wavelength_nm()
            return getattr(laser, "wavelength_nm", None)
        except Exception:  # noqa: BLE001
            return None

    def finish_analysis(self, run: _Run) -> dict:
        out: dict[str, Any] = {"modes": {}}
        for mode in ("par", "crs"):
            pts = [(r["incident_deg_actual"], r["intensity"]) for r in run.rows
                   if r.get("mode") == mode and r.get("intensity") is not None]
            if not pts:
                continue
            ys = [p[1] for p in pts]
            hi, lo = max(ys), min(ys)
            out["modes"][mode] = {"n": len(pts), "max": hi, "min": lo,
                                  "angle_at_max_deg": pts[ys.index(hi)][0],
                                  "visibility": (hi - lo) / (hi + lo) if (hi + lo) else None}
        out["analyzer_convention"] = {"scale": run.cfg.analyzer_scale, "offset_deg": run.cfg.analyzer_offset_deg,
                                      "crs_offset_deg": run.cfg.crs_offset_deg}
        return out

    def plot(self, run: _Run, path: Path) -> Path | None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=(10.0, 4.6))
        ax1 = fig.add_subplot(1, 2, 1)
        ax2 = fig.add_subplot(1, 2, 2, projection="polar")
        for mode, style in (("par", "o-"), ("crs", "s--")):
            pts = sorted((r["incident_deg_actual"], r["intensity"]) for r in run.rows
                         if r.get("mode") == mode and r.get("intensity") is not None)
            if not pts:
                continue
            th = np.array([p[0] for p in pts])
            y = np.array([p[1] for p in pts])
            ax1.plot(th, y, style, ms=3, lw=0.8, label=mode)
            ax2.plot(np.deg2rad(th), y, style, ms=3, lw=0.8, label=mode)
        ax1.set_xlabel("Incident polarisation (deg)")
        ax1.set_ylabel("Intensity (counts·nm)")
        ax1.grid(alpha=0.3)
        ax1.legend()
        fig.suptitle(f"Polarisation matrix · {run.run_id} · {run.status}")
        fig.tight_layout()
        fig.savefig(path, dpi=140)
        plt.close(fig)
        return path


# ═══════════════════════════════════════════════════════════════════════════════════
# 3. TemperatureSeries
# ═══════════════════════════════════════════════════════════════════════════════════


class TemperatureSeries(_Protocol):
    """Spectra versus temperature: set -> wait until stable (bounded, cancellable) -> [autofocus] ->
    measure -> save. The temperature goes into the sidecar, never into the file name's "memo".

    *Stable* means the controller's PV stayed within ``tolerance_c`` of the target for ``hold_s``
    continuously; ``timeout_s`` bounds each point (a point that does not settle stops the run cleanly
    — the next points would be measured at the wrong temperature). ``measure`` is ``single`` (one
    spectrum) or ``analyzer_series`` (the WITec sequencer ``run_analyzer_series``; the data stay in the
    WITec project, the sidecar records the series and whatever arrays the driver returns).
    """

    NAME = "TemperatureSeries"
    SHORT = "tser"
    CSV_COLUMNS = ("step", "time_utc", "temperature_set_c", "temperature_c", "temperature_pv_end_c", "settle_s",
                   "measure", "analyzer_deg", "intensity", "peak_nm", "peak_counts", "snr", "found", "saturated",
                   "power_mw", "autofocus_z_um", "stem", "file")

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=self.NAME, version=SKILL_VERSION, category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr(
                "变温系列（光学站协议）：对温度表里的每个 T——设定温控（可限升降温速率 ramp_c_per_min）→ 等稳定（PV 在 tolerance_c 内连续保持 hold_s；"
                "每点最多等 timeout_s，可取消，超时则干净停下）→ 可选 autofocus（热漂移会跑焦）→ 测量（single = 一条谱；analyzer_series = 调用 WITec 的 Analyzer 系列）"
                "→ 立刻落盘（温度写进 sidecar 而不是靠手敲名字）并更新汇总。光源三选一：WITec 自带激光、外部光源、或操作员手动开好。用光前要有 laser_area_safe 有效签署（开始前、每步、每次曝光前都查，无效时弹窗请签，签不上就安全收尾停下）。"
                "结束或中止都走安全收尾：外部光源关出光、WITec 快门关、温控 stop（正常结束且给了 final_setpoint_c 时才回到该温度；中止时、或温控已被别人停了时，绝不再发新设定——后一种情况协议直接停下）。"
                "别人在运行中按了停止（StopWITec / CloseWITecShutter / LaserEmissionOff / StopTemperatureRamp、撤销签署、急停）：下一步就干净停下（stop_kind=stopped_externally），不再重开快门/光源、不再下温度设定。"
                "越限（温度范围、升降温速率、write_enabled=false）一律拒绝。dry_run=true 只返回计划与预计时长，不碰硬件。",
                "Temperature series (optical-station protocol): for each T in the list — set the controller (optionally limiting the ramp with ramp_c_per_min) → wait until stable "
                "(PV within tolerance_c continuously for hold_s; at most timeout_s per point, cancellable, a timeout stops the run cleanly) → optional autofocus (thermal drift "
                "moves the focus) → measure (single = one spectrum; analyzer_series = run the WITec Analyzer series) → save at once (the temperature goes into the sidecar, not into a typed "
                "name) and update the summary. Light: a WITec laser, an external source, or switched on by the operator; laser light needs a valid laser_area_safe attestation (checked before the start, at every step and before every exposure; the operator is asked to sign when it is missing/expired, and if nobody signs the run stops safely). Any end or abort takes the safe direction: external source emission "
                "off, WITec shutter closed, temperature controller stopped (it is sent to final_setpoint_c only after a normal finish and when given; never a new setpoint after an abort, or to a controller somebody else stopped - in that case the run stops). "
                "A stop somebody else asks for during the run (StopWITec / CloseWITecShutter / LaserEmissionOff / StopTemperatureRamp, withdrawing the attestation, the E-STOP) ends it cleanly at the next check (stop_kind=stopped_externally); the shutter, the source and the setpoint are not touched again. "
                "Out of range (temperature range, ramp rate, write_enabled=false) is refused. dry_run=true only returns the plan and estimated time; no hardware."),
            parameters=[
                _spec("temperature_id", "str", "温控设备 id；留空/auto = 唯一一台", "Temperature controller id; empty/auto = the only one", default=""),
                _spec("temperatures_c", "str", "温度表 °C：'25,50,75' 或 JSON 数组；与 start_c/stop_c/step_c 二选一", "Temperatures in °C: '25,50,75' or a JSON array; use either this or start_c/stop_c/step_c", default=""),
                _spec("start_c", "float", "起始温度 °C", "Start temperature (°C)", unit="°C", lo=-273.15, hi=3000.0),
                _spec("stop_c", "float", "终止温度 °C（含）", "Stop temperature (°C, inclusive)", unit="°C", lo=-273.15, hi=3000.0),
                _spec("step_c", "float", "温度步长 °C", "Temperature step (°C)", unit="°C", lo=0.01, hi=3000.0),
                _spec("ramp_c_per_min", "float", "升降温速率 °C/min（超过设备 max_ramp_c_per_min 即拒绝）；不给 = 设备默认", "Ramp rate in °C/min (refused above the device's max_ramp_c_per_min); not given = the device default",
                      unit="°C/min", lo=0.001, hi=1000.0),
                _spec("tolerance_c", "float", "视为到位的温差 °C", "Temperature band counted as 'at target' (°C)", unit="°C", default=0.5, lo=0.01, hi=50.0),
                _spec("hold_s", "float", "PV 需连续落在带内多久才算稳定", "How long the PV must stay inside the band to count as stable", unit="s", default=30.0, lo=0.0, hi=36000.0),
                _spec("timeout_s", "float", "每个温度点最多等多久（含升降温），超时就干净停下", "Longest wait per temperature point (ramp included); on timeout the run stops cleanly", unit="s", default=1800.0, lo=0.05, hi=86400.0),
                _spec("poll_s", "float", "读温度的间隔", "Temperature polling interval", unit="s", default=2.0, lo=0.01, hi=60.0),
                _spec("measure", "str", "每个温度点做什么：single = 一条谱；analyzer_series = WITec Analyzer 系列", "What to do at each point: single = one spectrum; analyzer_series = a WITec Analyzer series",
                      default="single", allowed=["single", "analyzer_series"]),
                _spec("autofocus", "bool", "测量前自动对焦（WITec；热漂移会跑焦）", "Autofocus before measuring (WITec; thermal drift moves the focus)", default=False),
                _spec("analyzer_deg", "float", "single 模式：先把 Analyzer 设到这个角度；不给 = 不动", "single mode: set the Analyzer to this angle first; not given = leave it", unit="deg", lo=0.0, hi=360.0),
                _spec("series_start_deg", "float", "analyzer_series：起始角", "analyzer_series: start angle", unit="deg", default=0.0, lo=0.0, hi=360.0),
                _spec("series_stop_deg", "float", "analyzer_series：终止角", "analyzer_series: stop angle", unit="deg", default=360.0, lo=0.0, hi=360.0),
                _spec("series_points", "int", "analyzer_series：点数", "analyzer_series: number of points", default=73, lo=2, hi=1441),
                _spec("final_setpoint_c", "float", "正常结束后把温控送到这里（中止时不会）；不给 = 停温控", "After a normal finish send the controller here (never after an abort); not given = stop the controller",
                      unit="°C", lo=-273.15, hi=3000.0),
                *_center_specs(),
                *_feature_specs(),
                *[s for s in _source_specs(external=True, witec=True)],
                *[s for s in _common_specs() if s.name != "temperature_id"],
            ],
            estimated_duration_s=3600.0, composition_level=3, tags=[*_TAGS, "temperature"],
        )

    def _parse(self, params: dict) -> SimpleNamespace:
        c = _parse_common(params)
        _parse_source(params, c)
        _source_exclusive(c)
        _parse_center(params, c)
        _parse_feature(params, c)
        c.temps = _series(params, list_key="temperatures_c", start_key="start_c", stop_key="stop_c", step_key="step_c",
                          label=tr("温度", "temperatures"))
        bad = [t for t in c.temps if not -273.15 <= t <= 3000.0]
        if bad:
            raise ProtocolRefused(tr(f"温度 {bad[0]:g} °C 不是物理合理的值", f"temperature {bad[0]:g} °C is not a physical value"))
        c.ramp_c_per_min = _as_float(params, "ramp_c_per_min", None, lo=0.0, hi=1000.0, lo_open=True)
        c.tolerance_c = _as_float(params, "tolerance_c", 0.5, lo=0.01, hi=50.0)
        c.hold_s = _as_float(params, "hold_s", 30.0, lo=0.0, hi=36000.0)
        c.timeout_s = _as_float(params, "timeout_s", 1800.0, lo=0.05, hi=86400.0)
        c.poll_s = _as_float(params, "poll_s", 2.0, lo=0.01, hi=60.0)
        c.measure = _as_choice(params, "measure", "single", ("single", "analyzer_series"))
        c.autofocus = _as_bool(params, "autofocus", False)
        c.analyzer_deg = _as_float(params, "analyzer_deg", None, lo=0.0, hi=360.0)
        c.series_start_deg = _as_float(params, "series_start_deg", 0.0, lo=0.0, hi=360.0)
        c.series_stop_deg = _as_float(params, "series_stop_deg", 360.0, lo=0.0, hi=360.0)
        c.series_points = _as_int(params, "series_points", 73, lo=2, hi=1441)
        c.final_setpoint_c = _as_float(params, "final_setpoint_c", None, lo=-273.15, hi=3000.0)
        return c

    def _rig_needs(self, cfg: SimpleNamespace) -> dict:
        need_mic = bool(cfg.measure == "analyzer_series" or cfg.autofocus or cfg.witec_laser
                        or cfg.analyzer_deg is not None)
        return {"laser": "optional", "temperature": "required", "require_mic": need_mic}

    def _plan(self, cfg: SimpleNamespace, rig: _Rig) -> _Plan:
        problems: list[str] = []
        notes: list[str] = []
        tc = rig.tc
        lim = tc.limits
        if not getattr(tc, "write_enabled", False):
            problems.append(tr(f"{rig.tc_id}: write_enabled=false，不能下温度设定（实机自检后才在配置里打开）",
                               f"{rig.tc_id}: write_enabled is false, setpoints cannot be written (enable it after the bench self-test)"))
        if lim.min_c is None or lim.max_c is None:
            problems.append(tr(f"{rig.tc_id}: 没有配置温度范围 —— 拒绝", f"{rig.tc_id}: no temperature range configured — refused"))
        else:
            out = [t for t in cfg.temps if not lim.min_c <= t <= lim.max_c]
            if out:
                problems.append(tr(f"{len(out)} 个温度超出 [{lim.min_c:g}, {lim.max_c:g}] °C（如 {out[0]:g}）", f"{len(out)} temperature(s) outside [{lim.min_c:g}, {lim.max_c:g}] °C (e.g. {out[0]:g})"))
            if cfg.final_setpoint_c is not None and not lim.min_c <= cfg.final_setpoint_c <= lim.max_c:
                problems.append(tr(f"final_setpoint_c={cfg.final_setpoint_c:g} 超出 [{lim.min_c:g}, {lim.max_c:g}] °C", f"final_setpoint_c={cfg.final_setpoint_c:g} is outside [{lim.min_c:g}, {lim.max_c:g}] °C"))
        if cfg.ramp_c_per_min is not None and lim.max_ramp_c_per_min is not None and cfg.ramp_c_per_min > lim.max_ramp_c_per_min:
            problems.append(tr(f"ramp_c_per_min={cfg.ramp_c_per_min:g} 超过设备上限 {lim.max_ramp_c_per_min:g} °C/min", f"ramp_c_per_min={cfg.ramp_c_per_min:g} exceeds the device limit {lim.max_ramp_c_per_min:g} °C/min"))
        slim = rig.spec.limits
        if cfg.center_nm is not None and not slim.min_center_nm <= cfg.center_nm <= slim.max_center_nm:
            problems.append(tr(f"谱仪中心 {cfg.center_nm:g} nm 超出 [{slim.min_center_nm:g}, {slim.max_center_nm:g}] nm", f"spectrometer centre {cfg.center_nm:g} nm is outside [{slim.min_center_nm:g}, {slim.max_center_nm:g}] nm"))
        if rig.mic is not None:
            ml = rig.mic.microscope_limits
            lo, hi = ml.analyzer_deg
            angles = [cfg.analyzer_deg] if cfg.analyzer_deg is not None else []
            if cfg.measure == "analyzer_series":
                angles += [cfg.series_start_deg, cfg.series_stop_deg]
                if cfg.series_points > ml.max_series_points:
                    problems.append(tr(f"series_points={cfg.series_points} 超过显微镜上限 {ml.max_series_points}", f"series_points={cfg.series_points} exceeds the microscope limit {ml.max_series_points}"))
                if cfg.integration_s > ml.max_integration_s:
                    problems.append(tr(f"integration_s={cfg.integration_s:g} 超过显微镜上限 {ml.max_integration_s:g} s", f"integration_s={cfg.integration_s:g} exceeds the microscope limit {ml.max_integration_s:g} s"))
            for a in angles:
                if not lo <= a <= hi:
                    problems.append(tr(f"Analyzer 角 {a:g}° 超出 [{lo:g}, {hi:g}]°", f"analyzer angle {a:g}° is outside [{lo:g}, {hi:g}]°"))
        if cfg.measure == "single":
            measure_s = cfg.settle_s + cfg.integration_s * cfg.accumulations + 1.0
        else:
            measure_s = cfg.series_points * (cfg.integration_s * cfg.accumulations + 0.5) + 2.0
        steps: list[_Step] = []
        prev: float | None = None
        for i, t in enumerate(cfg.temps, start=1):
            ramp_s = 0.0
            if prev is not None and cfg.ramp_c_per_min:
                ramp_s = abs(t - prev) / cfg.ramp_c_per_min * 60.0
            est = ramp_s + cfg.hold_s + measure_s + (10.0 if cfg.autofocus else 0.0)
            steps.append(_Step(i, f"T={_sig(t, 5)} °C", {"temperature_set_c": t, "measure": cfg.measure,
                                                           "analyzer_deg": cfg.analyzer_deg}, [], est))
            prev = t
        notes.append(tr("预计时长不含第一个点之前从当前温度升降温的时间（要读温控才知道）", "the estimate leaves out the ramp from the present temperature to the first point (needs a controller read)"))
        return _Plan(steps, problems, notes)

    def _prepare(self, run: _Run) -> None:
        cfg = run.cfg
        run.commands_temperature = True
        _prepare_light(run, analyzer_deg=cfg.analyzer_deg if cfg.measure == "single" else None)
        run.bring_up_source()

    def _wait_stable(self, run: _Run, target: float) -> dict:
        cfg, tc = run.cfg, run.rig.tc
        t_start = time.monotonic()
        t_in: float | None = None
        last: float | None = None
        reads = 0
        while True:
            run.check()
            st = run.hw("read the temperature controller", tc.status)       # PV and run state in one read
            if run.tc_commanded and str(st.get("state") or "") == "stopped":
                raise self._stopped_by_someone()
            pv = _as_number(st.get("pv_c"))
            reads += 1
            last = pv
            now = time.monotonic()
            if pv is not None and abs(pv - target) <= cfg.tolerance_c:
                if t_in is None:
                    t_in = now
                if now - t_in >= cfg.hold_s:
                    return {"pv_c": pv, "settle_s": now - t_start, "reads": reads}
            else:
                t_in = None
            if now - t_start > cfg.timeout_s:
                raise ProtocolHalt("temperature_timeout", tr(
                    f"{cfg.timeout_s:.0f} s 内温度没有稳定在 {target:g} ± {cfg.tolerance_c:g} °C（最后读数 {last}）",
                    f"the temperature did not settle at {target:g} ± {cfg.tolerance_c:g} °C within {cfg.timeout_s:.0f} s (last reading {last})"))
            run.sleep(cfg.poll_s)

    @staticmethod
    def _stopped_by_someone() -> ProtocolHalt:
        return ProtocolHalt("temperature_stopped", tr(
            "温控器读回「已停止」，而本协议没有停它（有人按了 StopTemperatureRamp 或面板上的停止）—— 停下，之后不再发任何温度设定",
            "the temperature controller reads 'stopped' and this run did not stop it (somebody pressed StopTemperatureRamp or the panel's stop) — "
            "stopping; no further setpoint is sent"))

    def _step(self, run: _Run, step: _Step) -> dict:
        cfg, rig = run.cfg, run.rig
        t_set = float(step.conditions["temperature_set_c"])
        run.commands_temperature = True
        if run.tc_stopped_by_someone():                 # a stopped controller is somebody's decision: never overrule it
            raise self._stopped_by_someone()
        run.hw("set the temperature", rig.tc.set_setpoint, t_set, ramp_c_per_min=cfg.ramp_c_per_min)
        run.tc_commanded = True
        settled = self._wait_stable(run, t_set)
        af: dict | None = None
        if cfg.autofocus:
            af = run.hw("autofocus", rig.mic.autofocus, check_abort=run.probe)
        run.sleep(cfg.settle_s)
        p_before = run.read_power_mw(None)
        expo: SimpleNamespace | None = None
        if cfg.measure == "single":
            expo = run.expose(step, device_name=f"{run.run_id}_s{step.index:03d}",
                              meta={"temperature_set_c": t_set, "protocol_step": step.index})
            analysis = _feature_analysis(run, expo)
            started = expo.started
        else:
            analysis, started = {"found": None, "reason": "analyzer series (data in the WITec project)"}, _utc()
        p_after = run.read_power_mw(None, check=False) if rig.pm is not None else None
        reads = [p for p in (p_before, p_after) if p is not None]
        power = (sum(reads) / len(reads)) if reads else None
        cond = run.base_conditions()
        pv_end = cond["temperature_c"]
        cond.update(temperature_set_c=t_set, temperature_settled_c=settled["pv_c"], settle_s=settled["settle_s"],
                    ramp_c_per_min=cfg.ramp_c_per_min, tolerance_c=cfg.tolerance_c, hold_s=cfg.hold_s,
                    power_mw=power, measure=cfg.measure, autofocus=af, source_mode=run.source_mode,
                    witec_laser=cfg.witec_laser or None)
        cond["temperature_c"] = settled["pv_c"]            # the temperature at which the measurement started
        cond["temperature_pv_end_c"] = pv_end
        if cfg.witec_laser:
            cond["excitation_nm"] = PolarizationMatrix._witec_nm(run)
        elif rig.laser is not None:
            cond["excitation_nm"] = PolarizationMatrix._laser_nm(rig.laser)
        if cfg.measure == "single":
            # the temperature lives in the sidecar and the summary, not in the name (typing it into
            # names by hand is error-prone); the step number keeps the names unique
            stem = _stem(self.SHORT, f"s{step.index:03d}", "single", tag=cfg.tag)
            stored = run.store_step(step, expo, stem=stem, conditions=cond, analysis=analysis)
            intensity = analysis.get("integral")
            if not analysis.get("found"):
                run.warn_limited("no_feature", tr(f"第 {step.index} 步: 没有找到积分用的谱峰（{analysis.get('reason', '')}）",
                                                  f"step {step.index}: the feature to integrate was not found ({analysis.get('reason', '')})"))
            ana, saturated = cond["analyzer_deg"], expo.saturated
        else:
            ser = self._series(run, step, cond)
            stored, intensity, ana, saturated = ser, None, None, None
            started, pv_end = ser["started"], ser["pv_end"]
            analysis = {"found": None, "series": ser["series"]}
        return {
            "step": step.index, "time_utc": started, "temperature_set_c": t_set, "temperature_c": settled["pv_c"],
            "temperature_pv_end_c": pv_end, "settle_s": settled["settle_s"], "measure": cfg.measure,
            "analyzer_deg": ana, "intensity": intensity, "peak_nm": analysis.get("peak_nm"),
            "peak_counts": analysis.get("peak_counts"), "snr": analysis.get("snr"), "found": analysis.get("found"),
            "saturated": saturated, "power_mw": power,
            "autofocus_z_um": None if not af else af.get("z_um"), "stem": stored["stem"], "file": stored["paths"][0] if stored.get("paths") else None,
        }

    def _series(self, run: _Run, step: _Step, cond: dict) -> dict:
        """An Analyzer series at this temperature (WITec sequencer). Data stay in the WITec project."""
        cfg, rig = run.cfg, run.rig
        store = optical_state.get_store()
        run.ensure_attested(beam_on=run.emission_by_software)         # the last gate before the shutter / series
        before = store.snapshot()
        stem = run.unique_stem(_stem(self.SHORT, f"s{step.index:03d}", "anaser", tag=cfg.tag))
        started, t0 = _utc(), time.monotonic()
        with run._shutter_window():
            res = run.hw("run the Analyzer series", rig.mic.run_analyzer_series, start_deg=cfg.series_start_deg,
                         stop_deg=cfg.series_stop_deg, points=cfg.series_points, integration_s=cfg.integration_s,
                         accumulations=cfg.accumulations, name=f"{run.run_id}_{stem}", check_abort=run.probe)
        elapsed = time.monotonic() - t0
        res = dict(res or {})
        try:
            pv_end = rig.tc.read_c()                          # where the stage is when the series is over
        except Exception:  # noqa: BLE001
            pv_end = None
        cond = {**cond, "temperature_pv_end_c": pv_end}
        spectra, axis = res.pop("spectra", None), res.pop("axis_nm", None)
        paths: list[Path] = []
        sha = None
        layout = "no array: the series data are in the WITec project (see extra.series)"
        if spectra is not None and axis is not None:
            arr = np.vstack([np.asarray(axis, dtype=np.float64)[None, :], np.asarray(spectra, dtype=np.float64)])
            buf = io.BytesIO()
            np.save(buf, arr, allow_pickle=False)
            import hashlib

            sha = hashlib.sha256(buf.getvalue()).hexdigest()
            atomic_write_bytes(run.dir / f"{stem}.npy", buf.getvalue())
            paths.append(run.dir / f"{stem}.npy")
            layout = "npy rows: [0] = wavelength_nm, [1..] = spectra in the order of extra.series.angles_deg"
        sidecar = {
            "schema": "mast.spectrum/1", "layout": layout, "skill": self.NAME, "run_id": run.run_id,
            "protocol": {"name": self.NAME, "version": SKILL_VERSION, "run_id": run.run_id, "step": step.index,
                         "n_steps": len(run.plan.steps), "label": step.label, "stem": stem},
            "conditions": json_safe({**cond, "series_start_deg": cfg.series_start_deg, "series_stop_deg": cfg.series_stop_deg,
                                     "series_points": cfg.series_points}),
            "optical_state": before, "optical_state_after": store.snapshot(),
            "operator_prompts": [p for p in run.prompts_log if p.get("step") == step.index],
            "instruments": run.device_status(),
            "time": {"started_utc": started, "finished_utc": _utc(), "elapsed_s": round(elapsed, 3)},
            "scope": {**scope_ids(), "action_id": None, "step_id": f"{run.run_id}:{step.index}"},
            "files": {"array": None if sha is None else {"name": f"{stem}.npy", "sha256": sha}},
            "extra": {"series": json_safe(res)},
        }
        atomic_write_bytes(run.dir / f"{stem}.json", dump_json_bytes(sidecar))
        paths.append(run.dir / f"{stem}.json")
        stored = run.archive(paths)
        run.data_paths.extend(stored["paths"])
        run.all_stored = run.all_stored and bool(stored.get("stored"))
        return {"stem": stem, "paths": stored["paths"], "stored": bool(stored.get("stored")), "started": started,
                "series": json_safe(res), "pv_end": pv_end}

    def finish_analysis(self, run: _Run) -> dict:
        pts = [(r["temperature_c"], r["intensity"]) for r in run.rows if r.get("intensity") is not None and r.get("temperature_c") is not None]
        return {"measure": run.cfg.measure, "n_points": len(run.rows), "n_with_intensity": len(pts),
                "settle_s_max": max((r["settle_s"] for r in run.rows if r.get("settle_s") is not None), default=None)}

    def plot(self, run: _Run, path: Path) -> Path | None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        rows = [r for r in run.rows if r.get("temperature_c") is not None]
        fig, axes = plt.subplots(2, 1, sharex=True, figsize=(7.2, 6.0))
        t = [r["temperature_c"] for r in rows]
        axes[0].plot(t, [np.nan if r.get("intensity") is None else r["intensity"] for r in rows], "o-", ms=3, lw=0.8)
        axes[0].set_ylabel("Intensity (counts·nm)")
        axes[1].plot(t, [np.nan if r.get("peak_nm") is None else r["peak_nm"] for r in rows], "o-", ms=3, lw=0.8)
        axes[1].set_ylabel("Peak position (nm)")
        axes[1].set_xlabel("Temperature (°C)")
        for ax in axes:
            ax.grid(alpha=0.3)
        fig.suptitle(f"Temperature series · {run.run_id} · {run.status}")
        fig.tight_layout()
        fig.savefig(path, dpi=140)
        plt.close(fig)
        return path


# ═══════════════════════════════════════════════════════════════════════════════════
# 4. PowerSeries
# ═══════════════════════════════════════════════════════════════════════════════════


class PowerSeries(_Protocol):
    """Spectra versus excitation power, with the log-log slope (SHG expects ~2, linear Raman ~1).

    Three ways the power gets set, decided by the source:

    * ``witec_laser`` (532/633 nm): ``Microscope.set_laser_power_mw`` (mW, capped by ``microscope_limits``);
    * an external source whose driver supports ``power`` (``Laser.SUPPORTS``): ``Laser.set_power`` (in
      the device's ``power_unit``, capped by ``limits.max_power``);
    * an external source without software power (NPI Rainbow: no documented command, and the vendor
      commands are never sent): an ``attest`` prompt per step asks the operator to set the value in the
      vendor software and to sign it into slot ``npi_power_pct`` (%).

    A power meter (``power_meter_id``) measures P at every step; the slope is fitted against the
    measured P, else against the set / signed value.
    """

    NAME = "PowerSeries"
    SHORT = "pser"
    CSV_COLUMNS = ("step", "time_utc", "power_set", "power_unit", "power_mw", "power_drift_pct", "intensity",
                   "peak_nm", "peak_counts", "snr", "found", "saturated", "temperature_c", "stem", "file")

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=self.NAME, version=SKILL_VERSION, category=SkillCategory.WRITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr(
                "功率系列（光学站协议）：对功率表里的每个值——设功率（WITec 532/633 激光：set_laser_power_mw，mW；外部光源有软件功率控制：set_power；"
                "NPI 这类没有的：每步弹 attest 提示，请操作员在厂商软件里设到 X% 并签署到 npi_power_pct 槽位）→ 量功率（功率计）→ 取谱 → 对指定谱峰积分 → 落盘并更新汇总。"
                "结束时给出功率与强度的对数斜率（SHG 预期约 2，线性拉曼约 1）及拟合误差。WITec 激光只在取谱期间开快门；软件开外部光源出光另需 allow_emission_control。用光前要有 laser_area_safe 有效签署（开始前、每步、每次曝光前都查，无效时弹窗请签，签不上就安全收尾停下）。"
                "任何停止都走安全收尾，已采数据保留。" "别人在运行中按了停止（StopWITec / CloseWITecShutter / LaserEmissionOff / StopTemperatureRamp、撤销签署、急停）：下一步就干净停下（stop_kind=stopped_externally），不再重开快门/光源、不再下温度设定。"
                "越限（WITec 功率上限、光源 max_power、槽位范围）拒绝、不钳位。dry_run=true 只返回计划，不碰硬件、不弹窗。",
                "Power series (optical-station protocol): for each value in the power list — set the power (WITec 532/633 laser: set_laser_power_mw, mW; external source with software "
                "power control: set_power; sources like NPI without it: an attest prompt per step asks the operator to set X% in the vendor software and sign it into the npi_power_pct slot) → "
                "measure the power (power meter) → acquire → integrate the chosen peak → save and update the summary. At the end it reports the log-log slope of intensity against power (SHG expects about 2, "
                "linear Raman about 1) with its fit error. The WITec laser's shutter is open only during acquisitions; switching an external source on in software also needs allow_emission_control. Laser light needs a valid laser_area_safe attestation (checked before the start, at every step and before every exposure; the operator is asked to sign when it is missing/expired, and if nobody signs the run stops safely). Any "
                "stop takes the safe direction and keeps the data. "
                "A stop somebody else asks for during the run (StopWITec / CloseWITecShutter / LaserEmissionOff / StopTemperatureRamp, withdrawing the attestation, the E-STOP) ends it cleanly at the next check (stop_kind=stopped_externally); the shutter, the source and the setpoint are not touched again. Out of range (WITec power cap, source max_power, slot range) is refused, never clamped. dry_run=true only returns the plan; no hardware, no prompt."),
            parameters=[
                _spec("powers", "str", "功率表：'0.5,1,2,4' 或 JSON 数组；与 start_power/stop_power/step_power 二选一。单位随光源：WITec = mW；外部软件控制 = 设备 power_unit；手动 = %",
                      "Power list: '0.5,1,2,4' or a JSON array; use either this or start_power/stop_power/step_power. Unit follows the source: WITec = mW; external software control = the device's power_unit; manual = %", default=""),
                _spec("start_power", "float", "起始功率（单位见 powers）", "Start power (unit as for powers)", lo=0.0, hi=1e6),
                _spec("stop_power", "float", "终止功率（含）", "Stop power (inclusive)", lo=0.0, hi=1e6),
                _spec("step_power", "float", "功率步长", "Power step", lo=1e-6, hi=1e6),
                _spec("power_slot", "str", "手动功率模式下登记功率 % 的光学状态槽位", "Optical-state slot that registers the power % in manual power mode", default=SLOT_POWER_PCT),
                _spec("meter_wavelength_nm", "float", "功率计校正波长 nm；不给 = 取 WITec 激光波长 / 光源当前波长", "Power-meter correction wavelength in nm; not given = the WITec laser's wavelength / the source's current wavelength",
                      unit="nm", lo=100.0, hi=5000.0),
                _spec("analyzer_deg", "float", "先把 Analyzer 设到这个角度；不给 = 不动", "Set the Analyzer to this angle first; not given = leave it", unit="deg", lo=0.0, hi=360.0),
                *_center_specs(),
                *_feature_specs(),
                *[s for s in _source_specs(external=True, witec=True) if s.name != "witec_power_mw"],
                *_common_specs(),
            ],
            estimated_duration_s=1200.0, composition_level=3, tags=[*_TAGS, "power"],
        )

    def _parse(self, params: dict) -> SimpleNamespace:
        c = _parse_common(params)
        _parse_source(params, c)
        c.witec_power_mw = None                      # the series itself sets the power at every step
        _parse_center(params, c)
        _parse_feature(params, c)
        c.powers = _series(params, list_key="powers", start_key="start_power", stop_key="stop_power",
                           step_key="step_power", label=tr("功率", "powers"))
        if any(p < 0 for p in c.powers):
            raise ProtocolRefused(tr("功率不能为负", "powers must not be negative"))
        has_laser = bool(c.laser_id) and c.laser_id.lower() != "none"
        if bool(c.witec_laser) == has_laser:
            raise ProtocolRefused(tr("请给且只给一种光源：witec_laser 或 laser_id", "give exactly one source: witec_laser or laser_id"))
        c.power_slot = _as_str(params, "power_slot", SLOT_POWER_PCT)
        c.meter_wavelength_nm = _as_float(params, "meter_wavelength_nm", None, lo=100.0, hi=5000.0)
        c.analyzer_deg = _as_float(params, "analyzer_deg", None, lo=0.0, hi=360.0)
        return c

    def _rig_needs(self, cfg: SimpleNamespace) -> dict:
        return {"laser": "none" if cfg.witec_laser else "required", "temperature": "optional",
                "require_mic": bool(cfg.witec_laser or cfg.analyzer_deg is not None)}

    @staticmethod
    def _mode(cfg: SimpleNamespace, rig: _Rig) -> str:
        if cfg.witec_laser:
            return "witec"
        return "software" if "power" in getattr(rig.laser, "SUPPORTS", ()) else "manual"

    def _preflight(self, cfg: SimpleNamespace, rig: _Rig, plan: _Plan, *, read_hw: bool) -> list[str]:
        out = super()._preflight(cfg, rig, plan, read_hw=read_hw)
        if rig.laser is not None and self._mode(cfg, rig) == "software":
            out += self._check_power_raises(cfg, rig, plan, read_hw=read_hw)
        return out

    def _check_power_raises(self, cfg: SimpleNamespace, rig: _Rig, plan: _Plan, *, read_hw: bool) -> list[str]:
        """With ``allow_emission_control=false`` the series may not raise the power of a source that emits.

        While the source emits, every increase - from 0 or from any lit level - passes the full emission gate in the
        drivers (docs 6.4: MAST then only dims, tunes the colour and shuts off), so ``set_power`` of a value above
        the one before is refused by the driver. Two places in the series: (a) a step that follows a lower one - the
        source emits from the first step on, so this is decided from the list alone and a dry run knows it too;
        (b) the first power against the power the source has now - only matters when the source is emitting (or that
        cannot be read) and needs a read of the source, so a dry run only gets a note; a source that is off gets its
        first power set and the operator switches it on afterwards. Fine: lowering the power, to 0 as well, and
        repeating it. A power that cannot be read counts as "possibly lower", an emission state that cannot be read
        as "emitting" (fail closed), as in the drivers."""
        laser = rig.laser
        if getattr(laser, "allow_emission_control", False):
            return []
        unit = laser.limits.power_unit
        out: list[str] = []

        def above(b: float, a: float) -> bool:
            return b > a + 1e-9 + 1e-6 * abs(a)

        raises = [(k, a, b) for k, (a, b) in enumerate(zip(cfg.powers, cfg.powers[1:]), start=2) if above(b, a)]
        if raises:
            k, a, b = raises[0]
            out.append(tr(
                f"{rig.laser_id}: 设备配置里 allow_emission_control=false，软件不加光；而功率表有 {len(raises)} 处是往上抬的（如第 {k} 步：{a:g} → {b:g} {unit}），"
                "驱动会在运行到那一步时拒绝。有两条路：① 把功率表改成从高到低排（协议只调暗；起始功率开始前在厂商软件里手动设好）；"
                "② 台上自检通过后，在设备配置里打开 allow_emission_control",
                f"{rig.laser_id}: allow_emission_control is false, so software does not add light, and the series raises the power at {len(raises)} place(s) "
                f"(e.g. step {k}: {a:g} -> {b:g} {unit}) — the driver would refuse when the run reaches that step. Two ways out: (1) order the series from high "
                "to low, so the protocol only dims (set the starting power by hand in the vendor software before the run); "
                "(2) enable allow_emission_control in the device config after the bench self-test"))
        first = cfg.powers[0]
        if first > 0:
            if not read_hw:
                plan.notes.append(tr(
                    f"试跑读不到光源：设备 allow_emission_control=false 时，光源出光期间软件不加光 —— 若开始时 {rig.laser_id} 已经在出光，它的功率必须已经 ≥ {first:g} {unit}"
                    f"（请先在厂商软件里手动设好；协议只会把它调暗），或在自检通过后打开 allow_emission_control；光源还关着的话，协议先把功率设好，再请你点亮",
                    f"a dry run cannot read the source: with allow_emission_control=false software does not add light while the source is emitting — if {rig.laser_id} is "
                    f"already emitting when you start, its power must already be {first:g} {unit} or more (set it by hand in the vendor software first; the protocol only "
                    "dims it), or enable allow_emission_control after the bench self-test; a source that is still off gets its power set first and you are asked to "
                    "switch it on afterwards"))
            else:
                try:
                    cur = _as_number(laser.get_power())
                except Exception:  # noqa: BLE001
                    cur = None
                if (cur is None or above(first, cur)) and self._source_may_be_live(laser):
                    shown_zh, shown_en = (f"{cur:g} {unit}", f"{cur:g} {unit}") if cur is not None else ("一个读不到的功率", "a power that cannot be read")
                    out.append(tr(
                        f"{rig.laser_id}: 设备配置里 allow_emission_control=false，而光源正在出光（或读不到出光状态），所以软件不加光；功率表却要把功率从 {shown_zh} 抬到 "
                        f"{first:g} {unit}，驱动会拒绝。有两条路：① 开始前在厂商软件里手动把功率设到至少 {first:g} {unit}（之后协议只会把它调暗）；"
                        "② 台上自检通过后，在设备配置里打开 allow_emission_control",
                        f"{rig.laser_id}: allow_emission_control is false and the source is emitting (or its emission state cannot be read), so software adds no light — yet the "
                        f"series starts by raising the power from {shown_en} to {first:g} {unit}, which the driver would refuse. Two ways out: (1) before starting, set the power "
                        f"to at least {first:g} {unit} by hand in the vendor software (afterwards the protocol only dims it); (2) enable allow_emission_control in the device "
                        "config after the bench self-test"))
        return out

    def _raise_hint(self, run: _Run) -> tuple[str, str]:
        cfg, rig = run.cfg, run.rig
        if getattr(run, "power_mode", "") != "software" or getattr(rig.laser, "allow_emission_control", False) or not cfg.powers[0] > 0:
            return "", ""
        unit = rig.laser.limits.power_unit
        return (f"设备配置里 allow_emission_control=false：光源一出光，软件就不再加光。协议已在光源还关着时把功率设成 {cfg.powers[0]:g} {unit}；"
                f"打开光源后若厂商软件里显示低于 {cfg.powers[0]:g} {unit}，请手动设上去（之后协议只会把它调暗）。",
                f"The device config has allow_emission_control=false: once the source emits, software adds no light. The protocol set the power to "
                f"{cfg.powers[0]:g} {unit} while the source was still off; if the vendor software shows less than {cfg.powers[0]:g} {unit} after you switch the source "
                "on, set it by hand (afterwards the protocol only dims it).")

    def _check_witec_laser(self, cfg: SimpleNamespace, rig: _Rig, *, read_hw: bool) -> list[str]:
        # every power of the series must respect the cap (the base class only knows witec_power_mw)
        out = super()._check_witec_laser(cfg, rig, read_hw=read_hw)
        if out:
            return out
        mic = rig.mic
        caps = mic.microscope_limits.max_laser_power_mw
        keys = [str(cfg.witec_laser)]
        las = None
        if read_hw:
            try:
                las = _match_laser(mic.list_lasers(), cfg.witec_laser)
            except Exception:  # noqa: BLE001
                las = None
        if las is not None:
            keys = [str(las.get("name"))]
            if las.get("wavelength_nm") is not None:
                keys.insert(0, str(int(round(float(las["wavelength_nm"])))))
            if not las.get("power_controllable"):
                return [tr(f"WITec 激光 {cfg.witec_laser!r} 没有软件功率控制", f"WITec laser {cfg.witec_laser!r} has no software power control")]
        else:
            try:
                keys.insert(0, str(int(round(float(cfg.witec_laser)))))
            except ValueError:
                pass
        cap = next((float(caps[k]) for k in keys if k in caps), None)
        if cap is None:
            return [tr(f"microscope_limits 里没有给 {cfg.witec_laser!r} 配 max_laser_power_mw —— 拒绝", f"no max_laser_power_mw configured for {cfg.witec_laser!r} in microscope_limits — refused")]
        over = [p for p in cfg.powers if not 0.0 <= p <= cap]
        if over:
            return [tr(f"{len(over)} 个功率超出 WITec 激光上限 [0, {cap:g}] mW（如 {over[0]:g}）", f"{len(over)} power(s) outside the WITec laser cap [0, {cap:g}] mW (e.g. {over[0]:g})")]
        return []

    def _plan(self, cfg: SimpleNamespace, rig: _Rig) -> _Plan:
        problems: list[str] = []
        notes: list[str] = []
        mode = self._mode(cfg, rig)
        unit = "mW"
        manual_tag: list[str] = []
        if mode == "software":
            laser, lim = rig.laser, rig.laser.limits
            unit = lim.power_unit
            if not getattr(laser, "write_enabled", False):
                problems.append(tr(f"{rig.laser_id}: write_enabled=false，不能设功率", f"{rig.laser_id}: write_enabled is false, the power cannot be set"))
            if lim.max_power is None:
                problems.append(tr(f"{rig.laser_id}: 没有配置 max_power —— 拒绝", f"{rig.laser_id}: no max_power configured — refused"))
            else:
                over = [p for p in cfg.powers if not 0.0 <= p <= lim.max_power]
                if over:
                    problems.append(tr(f"{len(over)} 个功率超出 [0, {lim.max_power:g}] {unit}（如 {over[0]:g}）", f"{len(over)} power(s) outside [0, {lim.max_power:g}] {unit} (e.g. {over[0]:g})"))
        elif mode == "manual":
            unit = "%"
            manual_tag = ["npi_power"]
            store = optical_state.get_store()
            try:
                slot = store.slot(cfg.power_slot)
            except optical_state.OpticalStateError as exc:
                problems.append(str(exc))
            else:
                if slot.kind != "number":
                    problems.append(tr(f"槽位 {slot.id} 不是数值槽位", f"slot {slot.id} is not a number slot"))
                else:
                    over = [p for p in cfg.powers if (slot.min is not None and p < slot.min) or (slot.max is not None and p > slot.max)]
                    if over:
                        problems.append(tr(f"{len(over)} 个功率超出槽位 {slot.id} 的范围 [{slot.min}, {slot.max}]（如 {over[0]:g}）", f"{len(over)} power(s) outside the range [{slot.min}, {slot.max}] of slot {slot.id} (e.g. {over[0]:g})"))
            notes.append(tr(f"该光源没有软件功率控制：每步弹一次签署提示（共 {len(cfg.powers)} 次）", f"this source has no software power control: one attest prompt per step ({len(cfg.powers)} in all)"))
        if rig.mic is not None and cfg.analyzer_deg is not None:
            lo, hi = rig.mic.microscope_limits.analyzer_deg
            if not lo <= cfg.analyzer_deg <= hi:
                problems.append(tr(f"Analyzer 角 {cfg.analyzer_deg:g}° 超出 [{lo:g}, {hi:g}]°", f"analyzer angle {cfg.analyzer_deg:g}° is outside [{lo:g}, {hi:g}]°"))
        slim = rig.spec.limits
        if cfg.center_nm is not None and not slim.min_center_nm <= cfg.center_nm <= slim.max_center_nm:
            problems.append(tr(f"谱仪中心 {cfg.center_nm:g} nm 超出 [{slim.min_center_nm:g}, {slim.max_center_nm:g}] nm", f"spectrometer centre {cfg.center_nm:g} nm is outside [{slim.min_center_nm:g}, {slim.max_center_nm:g}] nm"))
        if rig.pm is None:
            notes.append(tr("没有功率计：斜率对设定值（或签署值）拟合，不是实测功率", "no power meter: the slope is fitted against the set (or signed) value, not a measured power"))
        est = cfg.settle_s + cfg.integration_s * cfg.accumulations + 1.0 + (1.0 if rig.pm is not None else 0.0)
        steps = [_Step(i, f"P={_sig(p, 4)} {unit}", {"power_set": p, "power_unit": unit, "power_mode": mode,
                                                      "analyzer_deg": cfg.analyzer_deg}, list(manual_tag), est)
                 for i, p in enumerate(cfg.powers, start=1)]
        return _Plan(steps, problems, notes)

    def _prepare(self, run: _Run) -> None:
        cfg, rig = run.cfg, run.rig
        run.power_mode = self._mode(cfg, rig)
        _prepare_light(run, analyzer_deg=cfg.analyzer_deg)
        first = cfg.powers[0]
        wl = cfg.meter_wavelength_nm
        if run.power_mode == "witec":
            run.hw("set the WITec laser power", rig.mic.set_laser_power_mw, first)
            if wl is None:
                wl = PolarizationMatrix._witec_nm(run)
        elif run.power_mode == "software":
            run.hw("set the source power", rig.laser.set_power, first)
        else:
            self._ask_power(run, first)
        if wl is None and rig.laser is not None:
            wl = PolarizationMatrix._laser_nm(rig.laser)
        run.meter_wavelength = wl
        if rig.pm is not None and wl is not None:
            run.hw("set the power-meter wavelength", rig.pm.set_wavelength_nm, wl)
        if rig.laser is not None:
            run.bring_up_source()

    def _ask_power(self, run: _Run, value: float) -> float:
        cfg = run.cfg
        store = optical_state.get_store()
        slot = store.slot(cfg.power_slot)
        p = OperatorPrompt(
            kind="attest", severity="action", attest_slots=[cfg.power_slot], unit=slot.unit or "%",
            number_min=slot.min, number_max=slot.max,
            title=tr(f"请把功率设到 {value:g}{slot.unit or '%'}", f"Set the power to {value:g}{slot.unit or '%'}"),
            message=tr(f"{run.rig.laser_id} 没有软件功率控制。请在厂商软件里把它设到 {value:g}{slot.unit or '%'}，然后填写你实际设的值。",
                       f"{run.rig.laser_id} has no software power control. Set it to {value:g}{slot.unit or '%'} in the vendor software, then enter the value you actually set."),
            context={"reason": "power", "requested": value, "slot": cfg.power_slot})
        ans = run.ask(p)
        actual = float(ans.value)
        run.await_state(lambda: _slot_equals(store, cfg.power_slot, actual))
        sv = store.get(cfg.power_slot)
        if sv is None or abs(float(sv.value) - actual) > 1e-9:
            raise ProtocolHalt("error", tr("功率签署没有进入光学状态登记", "the power attestation did not reach the optical-state registry"))
        return actual

    def _step(self, run: _Run, step: _Step) -> dict:
        cfg, rig = run.cfg, run.rig
        value = float(step.conditions["power_set"])
        unit = str(step.conditions["power_unit"])
        attested: float | None = None
        readback: float | None = None
        if run.power_mode == "witec":
            run.hw("set the WITec laser power", rig.mic.set_laser_power_mw, value)
            readback = rig.mic.get_laser_power_mw()
        elif run.power_mode == "software":
            run.hw("set the source power", rig.laser.set_power, value)
            readback = rig.laser.get_power()
        elif step.index > 1:                        # the first value was already asked for in _prepare
            attested = self._ask_power(run, value)
        else:
            attested = float(optical_state.get_store().get(cfg.power_slot).value)
        run.sleep(cfg.settle_s)
        p_before = run.read_power_mw(run.meter_wavelength)
        expo = run.expose(step, device_name=f"{run.run_id}_s{step.index:03d}",
                          meta={"power_set": value, "power_unit": unit, "protocol_step": step.index})
        p_after = run.read_power_mw(run.meter_wavelength, check=False) if rig.pm is not None else None
        reads = [p for p in (p_before, p_after) if p is not None]
        power = (sum(reads) / len(reads)) if reads else None
        drift = (abs(p_after - p_before) / power * 100.0) if (p_before is not None and p_after is not None and power) else None
        analysis = _feature_analysis(run, expo)
        cond = run.base_conditions()
        cond.update(power_set=value, power_unit=unit, power_mode=run.power_mode, power_attested=attested,
                    power_readback=readback, power_mw=power, power_before_mw=p_before, power_after_mw=p_after,
                    power_drift_pct=drift, power_source="meter" if rig.pm is not None else None,
                    source_mode=run.source_mode, witec_laser=cfg.witec_laser or None)
        if cfg.witec_laser:
            cond["excitation_nm"] = PolarizationMatrix._witec_nm(run)
            if power is None and readback is not None:
                cond["power_mw"], cond["power_source"] = readback, "witec_readback"
        elif rig.laser is not None:
            cond["excitation_nm"] = PolarizationMatrix._laser_nm(rig.laser)
        ptxt = f"p{_sig(attested if attested is not None else value, 4)}{'pct' if unit == '%' else unit}"
        stem = _stem(self.SHORT, f"{_sig(cond['excitation_nm'], 4)}nm" if cond.get("excitation_nm") else "", ptxt, tag=cfg.tag)
        stored = run.store_step(step, expo, stem=stem, conditions=cond, analysis=analysis)
        if not analysis.get("found"):
            run.warn_limited("no_feature", tr(f"第 {step.index} 步: 没有找到积分用的谱峰（{analysis.get('reason', '')}）",
                                              f"step {step.index}: the feature to integrate was not found ({analysis.get('reason', '')})"))
        return {
            "step": step.index, "time_utc": expo.started, "power_set": attested if attested is not None else value,
            "power_unit": unit, "power_mw": cond["power_mw"], "power_drift_pct": drift,
            "intensity": analysis.get("integral"), "peak_nm": analysis.get("peak_nm"),
            "peak_counts": analysis.get("peak_counts"), "snr": analysis.get("snr"), "found": analysis.get("found"),
            "saturated": expo.saturated, "temperature_c": cond["temperature_c"], "stem": stored["stem"],
            "file": stored["paths"][0],
        }

    def finish_analysis(self, run: _Run) -> dict:
        rows = [r for r in run.rows if r.get("intensity") is not None]
        n_meter = sum(1 for r in rows if r.get("power_mw") is not None)
        use_meter = n_meter >= 3
        xs = [r["power_mw"] if use_meter else r.get("power_set") for r in rows]
        fit = loglog_fit(xs, [r["intensity"] for r in rows])
        return {"x_source": "measured_power_mw" if use_meter else "set_or_signed_power", "fit": fit,
                "n_points": len(rows),
                "hint": "log-log slope: SHG ~ 2, linear Raman ~ 1" + ("" if fit else " (needs at least 3 positive points)")}

    def plot(self, run: _Run, path: Path) -> Path | None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        rows = [r for r in run.rows if r.get("intensity") is not None]
        use_meter = sum(1 for r in rows if r.get("power_mw") is not None) >= 3
        xs = np.array([float(r["power_mw"] if use_meter else r.get("power_set")) for r in rows])
        ys = np.array([float(r["intensity"]) for r in rows])
        fig, ax = plt.subplots(figsize=(6.2, 5.2))
        good = (xs > 0) & (ys > 0)
        ax.loglog(xs[good], ys[good], "o", ms=4)
        fit = (run.analysis or {}).get("fit")
        if fit and good.sum() >= 2:
            gx = np.array([xs[good].min(), xs[good].max()])
            ax.loglog(gx, 10 ** fit["intercept_log10"] * gx ** fit["slope"], "-", lw=1,
                      label=f"slope {fit['slope']:.2f}" + (f" ± {fit['stderr']:.2f}" if fit.get("stderr") is not None else ""))
            ax.legend()
        ax.set_xlabel("Power (mW)" if use_meter else "Power (set value)")
        ax.set_ylabel("Intensity (counts·nm)")
        ax.grid(alpha=0.3, which="both")
        fig.suptitle(f"Power series · {run.run_id} · {run.status}")
        fig.tight_layout()
        fig.savefig(path, dpi=140)
        plt.close(fig)
        return path
