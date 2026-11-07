"""What ``observe`` shows on an optical station: the newest spectrum.

On the STM, ``observe`` returns the newest scan image from the VISTA archive. An optical
station has no scans; what it makes is **spectra**, and ``AcquireSpectrum`` already writes
each one as a pair of files in the experiment folder (``skills/builtins/optics_spectro.py``):

``spec_<stamp>_<device>[_<tag>].npy``
    float64, shape ``(1 + frames, pixels)``; row 0 is the wavelength axis in nm (NaN
    when the spectrometer does not know it), each further row is one frame in counts;
``spec_<stamp>_<device>[_<tag>].json``
    the sidecar (schema ``mast.spectrum/1``): device, settings before the exposure,
    exposure and frames requested, the laser line, the peak and counts of the mean
    spectrum, the acquisition record, the scope.

This module finds the newest such pair, reads the numbers a model needs and renders one
PNG. It reads files and nothing else - it never talks to a device, so ``observe`` stays
cheap and cannot disturb a measurement.

The text summary is the part the model reads; the image is for looking. The numbers:

* ``peaks`` - the most prominent peaks of the mean spectrum (position in nm and, when
  the laser line is known, Raman shift in cm-1, height, prominence);
* ``max_counts`` of the mean spectrum, and ``frame_max_counts`` / ``saturated`` over
  every frame: a detector that clipped is the commonest way for a spectrum to look fine
  and be wrong. The threshold is :data:`DEFAULT_SATURATION_COUNTS` (60000, below the
  65535 ceiling of a 16-bit CCD), changeable with ``MAST_SPECTRUM_SATURATION_COUNTS``
  or by the sidecar's own ``saturation_counts``;
* exposure, number of frames and the excitation wavelength, from the sidecar.

Rendering uses ``Figure`` with ``FigureCanvasAgg`` and never ``pyplot``: no global
backend is switched, no window can open and it is safe on any thread (the same choice as
``gallery/render.py``).
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"key": None, "summary": None, "png": None}

SATURATION_ENV = "MAST_SPECTRUM_SATURATION_COUNTS"
DEFAULT_SATURATION_COUNTS = 60000.0
MAX_PEAKS = 5
#: files of this shape are spectra; anything else in the folder is somebody else's
SPECTRUM_GLOB = "spec_*.npy"
SIDECAR_SCHEMA = "mast.spectrum/1"
#: how many staging days to look back through when no experiment folder is in play
_STAGING_DAYS = 3


@dataclass(frozen=True)
class SpectrumFile:
    """One spectrum on disk."""

    path: Path
    mtime: float
    size: int

    @property
    def sidecar_path(self) -> Path:
        return self.path.with_suffix(".json")

    @property
    def key(self) -> tuple[str, float, int]:
        return (str(self.path), self.mtime, self.size)


def saturation_threshold(sidecar: dict | None = None) -> float:
    """Counts at or above which a pixel counts as saturated (sidecar > environment > 60000)."""
    for raw in ((sidecar or {}).get("saturation_counts"), os.environ.get(SATURATION_ENV)):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0:
            return v
    return DEFAULT_SATURATION_COUNTS


# ── finding spectra ───────────────────────────────────────────────────────────────

def spectrum_dirs() -> list[Path]:
    """Where spectra live: the active sample's ``raw/other`` and the staging folders
    (``<root>/artifacts/spectra/<date>``, where a spectrum stays when no sample is
    selected). Existing folders only; nothing is created."""
    dirs: list[Path] = []
    try:
        from mast.core import experiment_paths as ep
        from mast.skills.builtins.optics_common import resolve_scope_dirs

        scope = resolve_scope_dirs(create=False)
        if scope is not None:
            exp_dir, sdir = scope
            raw = ep.raw_dir(ep.sample_dir(exp_dir, sdir), ".npy")
            dirs.append(Path(raw))
    except Exception as exc:  # noqa: BLE001 - no scope is a normal state
        logger.debug("optical observe: no experiment folder (%s)", exc)
    try:
        from mast._runtime_paths import project_root

        base = project_root() / "artifacts" / "spectra"
        if base.is_dir():
            days = sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)[:_STAGING_DAYS]
            dirs.extend(days)
    except Exception as exc:  # noqa: BLE001
        logger.debug("optical observe: staging folders unreadable (%s)", exc)
    seen: set[str] = set()
    out = []
    for d in dirs:
        if d.is_dir() and str(d) not in seen:
            seen.add(str(d))
            out.append(d)
    return out


def list_spectra(limit: int = 5, *, roots: list[Path] | None = None) -> list[SpectrumFile]:
    """The newest ``limit`` spectra, newest first."""
    found: list[SpectrumFile] = []
    for d in (roots if roots is not None else spectrum_dirs()):
        try:
            for p in d.glob(SPECTRUM_GLOB):
                try:
                    st = p.stat()
                except OSError:
                    continue
                found.append(SpectrumFile(p, st.st_mtime, st.st_size))
        except OSError:
            continue
    found.sort(key=lambda s: (s.mtime, s.path.name), reverse=True)
    return found[:max(0, int(limit))]


def newest_spectrum(*, roots: list[Path] | None = None) -> SpectrumFile | None:
    got = list_spectra(1, roots=roots)
    return got[0] if got else None


def listing_keys(*, roots: list[Path] | None = None) -> set[tuple[str, float, int]]:
    """Identity of every spectrum on disk now (to tell, after an act, which are new)."""
    return {s.key for s in list_spectra(10_000, roots=roots)}


def new_since(before: set[tuple[str, float, int]], *, roots: list[Path] | None = None,
              limit: int = 5) -> list[SpectrumFile]:
    """Spectra that were not in ``before``, newest first. A file is new when its path,
    modification time or size differs (a rewritten file counts)."""
    return [s for s in list_spectra(10_000, roots=roots) if s.key not in before][:max(0, int(limit))]


# ── reading one ───────────────────────────────────────────────────────────────────

def _read_sidecar(sf: SpectrumFile) -> dict:
    try:
        raw = json.loads(sf.sidecar_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def load(sf: SpectrumFile):
    """``(axis_nm | None, frames (n, pixels), sidecar)``. Raises ``ValueError`` for a file
    that is not a spectrum array."""
    import numpy as np

    arr = np.load(sf.path, allow_pickle=False)
    if arr.ndim != 2 or arr.shape[0] < 2 or arr.shape[1] < 2:
        raise ValueError(f"{sf.path.name}: not a spectrum array (shape {tuple(arr.shape)})")
    axis = np.asarray(arr[0], dtype=float)
    frames = np.asarray(arr[1:], dtype=float)
    return (axis if np.isfinite(axis).any() else None), frames, _read_sidecar(sf)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _dig(d: dict, *path: str) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def find_peaks_in(y, axis, *, laser_nm: float | None, limit: int = MAX_PEAKS) -> list[dict]:
    """Most prominent peaks of ``y``, strongest first: ``{pixel, nm?, raman_shift_cm1?,
    counts, prominence}``."""
    import numpy as np
    from scipy.signal import find_peaks

    y = np.asarray(y, dtype=float)
    ok = np.isfinite(y)
    if ok.sum() < 5:
        return []
    if not ok.all():                       # fill gaps so the filter sees a continuous trace
        y = np.interp(np.arange(y.size), np.flatnonzero(ok), y[ok])
    smooth = np.convolve(y, np.ones(3) / 3.0, mode="same")
    span = float(np.max(smooth) - np.min(smooth))
    if span <= 0:
        return []
    noise = 1.4826 * float(np.median(np.abs(np.diff(y)))) / math.sqrt(2.0)
    prom = max(0.05 * span, 5.0 * noise)
    idx, props = find_peaks(smooth, prominence=prom, distance=max(3, y.size // 200))
    order = np.argsort(props["prominences"])[::-1][:max(0, int(limit))]
    out = []
    for k in order:
        i = int(idx[k])
        row: dict[str, Any] = {"pixel": i, "counts": round(float(y[i]), 2),
                               "prominence": round(float(props["prominences"][k]), 2)}
        if axis is not None and i < len(axis) and math.isfinite(float(axis[i])):
            nm = float(axis[i])
            row["nm"] = round(nm, 3)
            if laser_nm and nm > 0:
                row["raman_shift_cm1"] = round(1e7 / laser_nm - 1e7 / nm, 1)
        out.append(row)
    return out


def summarize(sf: SpectrumFile, *, saturation_counts: float | None = None,
              max_peaks: int = MAX_PEAKS, loaded=None) -> dict:
    """The numbers about one spectrum (see the module docstring). ``loaded`` is the result
    of :func:`load` when the caller already has it."""
    import numpy as np

    axis, frames, side = loaded if loaded is not None else load(sf)
    mean = np.nanmean(frames, axis=0)
    thr = saturation_counts if saturation_counts is not None else saturation_threshold(side)
    laser = (_num(_dig(side, "settings", "laser_line_nm"))
             or _num(_dig(side, "extra", "laser_nm")) or _num(_dig(side, "extra", "excitation_nm")))
    exposure = (_num(_dig(side, "settings", "requested", "exposure_ms"))
                or _num(_dig(side, "settings", "before", "exposure_ms")))
    finite = frames[np.isfinite(frames)]
    frame_max = float(finite.max()) if finite.size else None
    imax = int(np.nanargmax(mean)) if np.isfinite(mean).any() else None
    out: dict[str, Any] = {
        "kind": "spectrum", "file": sf.path.name, "path": str(sf.path),
        "age_s": round(max(0.0, time.time() - sf.mtime), 1),
        "frames": int(frames.shape[0]), "pixels": int(frames.shape[1]),
    }
    dev = _dig(side, "device", "id")
    if dev:
        out["device_id"] = dev
    if axis is not None:
        a = axis[np.isfinite(axis)]
        out["axis_nm"] = [round(float(a.min()), 3), round(float(a.max()), 3)]
    if exposure is not None:
        out["exposure_ms"] = exposure
    if laser is not None:
        out["laser_nm"] = laser
    if imax is not None:
        out["max_counts"] = round(float(mean[imax]), 2)
        out["max_at"] = ({"nm": round(float(axis[imax]), 3), "pixel": imax}
                         if axis is not None and math.isfinite(float(axis[imax])) else {"pixel": imax})
        out["baseline_counts"] = round(float(np.nanmedian(mean)), 2)
    if frame_max is not None:
        out["frame_max_counts"] = round(frame_max, 2)
        out["saturation_threshold"] = thr
        out["saturated"] = bool(frame_max >= thr)
        if out["saturated"]:
            out["saturated_pixels"] = int((frames >= thr).sum())
    out["peaks"] = find_peaks_in(mean, axis, laser_nm=laser, limit=max_peaks)
    optical_state = side.get("optical_state")
    if isinstance(optical_state, dict) and optical_state:
        out["optical_state_recorded"] = True
    if not side:
        out["sidecar"] = "missing or unreadable"
    return out


def render_png(sf: SpectrumFile, summary: dict | None = None, *, loaded=None) -> bytes:
    """One PNG of the mean spectrum: axes, peaks marked, the saturation level when the
    trace comes anywhere near it. ASCII text only (the default plot font has no CJK)."""
    import numpy as np
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    axis, frames, side = loaded if loaded is not None else load(sf)
    summary = summary or summarize(sf, loaded=(axis, frames, side))
    mean = np.nanmean(frames, axis=0)
    x = axis if axis is not None else np.arange(mean.size, dtype=float)
    fig = Figure(figsize=(8.0, 3.4), dpi=110)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.plot(x, mean, lw=0.9, color="#1f4e79")
    pk = summary.get("peaks") or []
    if pk:
        px = [x[p["pixel"]] for p in pk if p["pixel"] < len(x)]
        py = [p["counts"] for p in pk if p["pixel"] < len(x)]
        ax.plot(px, py, "v", ms=5, color="#c0392b")
    thr = summary.get("saturation_threshold")
    top = float(np.nanmax(mean)) if np.isfinite(mean).any() else 0.0
    if thr and (top >= 0.5 * thr or summary.get("saturated")):
        ax.axhline(thr, ls="--", lw=0.8, color="#c0392b")
    ax.set_xlabel("Wavelength (nm)" if axis is not None else "Pixel")
    ax.set_ylabel("Counts")
    bits = [sf.path.stem]
    if summary.get("exposure_ms") is not None:
        bits.append(f"{summary['exposure_ms']:g} ms x {summary.get('frames', '?')}")
    if summary.get("laser_nm") is not None:
        bits.append(f"laser {summary['laser_nm']:g} nm")
    if summary.get("saturated"):
        bits.append("SATURATED")
    ax.set_title("   ".join(bits), fontsize=8)
    ax.grid(alpha=0.25, lw=0.5)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


def label_for(sf: SpectrumFile) -> str:
    """The text placed before the image in an observation (VISTA's ``<visual .../>`` form)."""
    return f'<visual kind="spectrum" file="{sf.path.name}"/>'


def latest_view(*, roots: list[Path] | None = None, with_image: bool = True
                ) -> tuple[dict | None, bytes | None]:
    """``(summary, png)`` of the newest spectrum, or ``(None, None)`` when there is none.
    A file that cannot be read gives ``({"kind": "spectrum", "file": ..., "error": ...}, None)``
    - an unreadable newest spectrum is not the same as no spectrum."""
    sf = newest_spectrum(roots=roots)
    if sf is None:
        return None, None
    # An observation is asked for again and again while nothing new has been measured:
    # rendering the same spectrum each time would cost ~0.2 s for nothing. One entry,
    # keyed by (path, mtime, size, saturation threshold, image wanted).
    ckey = (sf.key, saturation_threshold(), bool(with_image))
    with _cache_lock:
        hit = _cache.get("key") == ckey
        cached = (dict(_cache["summary"]), _cache["png"]) if hit else None
    if cached is not None:
        cached[0]["age_s"] = round(max(0.0, time.time() - sf.mtime), 1)
        return cached
    try:
        loaded = load(sf)
        summary = summarize(sf, loaded=loaded)
        png = render_png(sf, summary, loaded=loaded) if with_image else None
        with _cache_lock:
            _cache.update(key=ckey, summary=dict(summary), png=png)
        return summary, png
    except Exception as exc:  # noqa: BLE001
        logger.warning("optical observe: cannot read %s (%s)", sf.path, exc)
        return {"kind": "spectrum", "file": sf.path.name, "path": str(sf.path),
                "error": f"{type(exc).__name__}: {exc}"[:300]}, None


__all__ = [
    "DEFAULT_SATURATION_COUNTS", "MAX_PEAKS", "SATURATION_ENV", "SpectrumFile",
    "find_peaks_in", "label_for", "latest_view", "list_spectra", "listing_keys", "load",
    "new_since", "newest_spectrum", "render_png", "saturation_threshold", "spectrum_dirs",
    "summarize",
]
