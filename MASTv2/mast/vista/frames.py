"""Frames: load, orient, convert, flatten and render what the instrument saved.

README
------
Everything the model sees of a scan goes through this module.

* **Loading.** :func:`load_sxm` reads a ``.sxm`` with MAST's reader (``read_sxm`` +
  ``sxm_oriented_frames``) into float32 arrays for every channel and direction, oriented so
  that row 0 is the frame's top (high-v) edge and the backward pass is un-mirrored, in SI
  units, NaN where a row was never acquired. :func:`load_dat` reads a spectrum.
  :func:`scan_from_arrays` wraps arrays captured from a scan in progress.
* **Display coordinates** (``protocol.DISPLAY_CONVENTION``): a frame of ``nx`` x ``ny``
  pixels is shown at scale ``k`` (:func:`display_scale`): integer nearest-neighbour
  upscaling, or block-mean downsampling for frames larger than ``display_max``.
  :func:`display_to_scan_nm` / :func:`scan_nm_to_display` convert exactly, sub-pixel
  included, and are exact inverses of each other.
* **Flattening** (:func:`flatten`): ``none``, ``plane``, ``line`` (plane, then per-row
  median offset), ``poly2`` (second-order surface), ``highpass`` (subtract a median
  background of a given width in nm).
* **Rendering**: percentile contrast, gray (black = low), never-acquired pixels in one flat
  dark blue (:data:`NAN_RGB`), integer nearest-neighbour scaling only, lossless PNG.
  :func:`plan_view` places a whole frame or an exact region of it (widened to whole scan
  pixels) at the largest whole number of image pixels per scan pixel that fits
  ``display_max``; :func:`render_values` turns that view into a PNG.
* **Spectra** render as line plots (:func:`render_spectrum`); axes and units are allowed on
  a plot because a plot is not a camera image.
* **Units** (:func:`display_unit`): one model unit per quantity, everywhere: heights pm,
  currents pA, voltages V, frequencies Hz. Model-facing JSON keys carry it as a suffix
  (:func:`unit_key`: ``height_pm``, ``values_pa``); :func:`decimals_for` sets the rounding
  from a channel's value span (about 1/100 of it, 1..6 decimals).

Nothing here touches the instrument: only the saved files and arrays.
"""
from __future__ import annotations

import io
import math
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .protocol import DISPLAY_MAX_DEFAULT

DIRECTIONS = ("forward", "backward")
FLATTEN_MODES = ("none", "plane", "line", "poly2", "highpass")
HIGHPASS_NM_DEFAULT = 3.0
CLIP_PCT_DEFAULT = 0.5
#: Pixels never acquired (rows of an aborted or running scan): one flat dark blue, so they
#: are never read as "lowest height".
NAN_RGB = (24, 32, 104)
NAN_NAME = "dark blue"
#: A frame whose Z varies by less than this was taken at constant height: Z is flat and the
#: default channel becomes the current (:func:`default_channel`).
CONSTANT_HEIGHT_Z_RANGE_M = 1e-12

#: SI unit by channel name when the file does not say (``:DATA_INFO:`` normally does).
_UNIT_BY_NAME = {"z": "m", "current": "A", "bias": "V", "phase": "deg", "amplitude": "m",
                 "frequency shift": "Hz", "excitation": "V", "x": "m", "y": "m"}


# ── containers ──────────────────────────────────────────────────────────────
@dataclass
class ScanData:
    """One scan frame, oriented (row 0 = the frame's top edge), SI units, NaN = not acquired.

    ``arrays`` maps ``"<channel>/<direction>"`` to a float32 ``(ny, nx)`` array."""

    arrays: dict[str, np.ndarray]
    channels: list[str]
    units: dict[str, str]
    geometry: dict
    nm_per_px: float
    nm_per_px_y: float
    bias_v: float | None = None
    setpoint_a: float | None = None
    setpoint_unit: str = "A"
    scan_dir: str | None = None
    acquired_rows: int = 0
    rec_time: str = ""
    feedback: str | None = None
    source_name: str = ""

    def directions(self, channel: str) -> list[str]:
        return [d for d in DIRECTIONS if f"{channel}/{d}" in self.arrays]


@dataclass
class SpectrumData:
    """One spectrum (.dat): columns in SI units; the first column is the sweep axis."""

    columns: dict[str, np.ndarray]
    units: dict[str, str]
    header: dict[str, str] = field(default_factory=dict)
    experiment: str = ""
    x_nm: float | None = None
    y_nm: float | None = None
    bias_v: float | None = None
    setpoint_a: float | None = None
    setpoint_unit: str = "A"
    rec_time: str = ""
    source_name: str = ""


# ── small parsers ───────────────────────────────────────────────────────────
def _first_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float, np.floating, np.integer)):
        v = float(value)
        return v if math.isfinite(v) else None
    for tok in str(value).replace(";", " ").split():
        try:
            v = float(tok)
        except ValueError:
            continue
        if math.isfinite(v):
            return v
    return None


def _sxm_header_text(path: Path, max_bytes: int = 1_048_576) -> str:
    with open(path, "rb") as fh:
        chunk = fh.read(max_bytes)
    end = chunk.find(b":SCANIT_END:")
    if end < 0:
        end = chunk.find(b"\x1a\x04")
    return chunk[: max(end, 0)].decode("latin-1", errors="replace")


def sxm_channel_units(path: str | Path) -> dict[str, str]:
    """Channel name -> SI unit from the ``:DATA_INFO:`` block (MAST's parser drops it)."""
    text = _sxm_header_text(Path(path))
    units: dict[str, str] = {}
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip().upper() == ":DATA_INFO:")
    except StopIteration:
        return units
    for ln in lines[start + 1:]:
        s = ln.strip()
        if not s or s.startswith(":"):
            break
        parts = [p.strip() for p in ln.split("\t")]
        parts = [p for p in parts if p != ""]
        if len(parts) >= 3 and parts[1] != "Name":
            units[parts[1]] = parts[2]
    return units


def unit_for_channel(name: str, units: dict[str, str] | None = None) -> str:
    if units and name in units and units[name]:
        return str(units[name])
    low = str(name).strip().lower()
    if low in _UNIT_BY_NAME:
        return _UNIT_BY_NAME[low]
    if "freq" in low:
        return "Hz"
    if "current" in low:
        return "A"
    m = re.search(r"\(([^()]+)\)\s*$", str(name))
    return m.group(1).strip() if m else ""


# ── loading ─────────────────────────────────────────────────────────────────
def _acquired_rows(arr: np.ndarray) -> int:
    a = np.asarray(arr)
    if a.ndim != 2 or a.size == 0:
        return 0
    return int(np.isfinite(a).any(axis=1).sum())


def _floats(value: Any) -> list[float]:
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                pass
        return out
    out = []
    for tok in str(value or "").split():
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


def frame_geometry(header: dict) -> dict | None:
    """``{cx_nm, cy_nm, w_nm, h_nm, angle_deg, nx, ny}`` from a parsed .sxm header; None
    when the geometry keys are missing. Same mapping as STM-Bench's
    ``stmbench.human.render.frame_geometry`` (MIT), kept here so MAST needs no STM-Bench."""
    rng = _floats(header.get("scan_range"))
    off = _floats(header.get("scan_offset"))
    px = header.get("scan_pixels")
    px = [int(v) for v in px] if isinstance(px, (list, tuple)) else [int(v) for v in _floats(px)]
    if len(rng) < 1 or len(off) < 2 or len(px) < 2:
        return None
    w_m = rng[0]
    h_m = rng[1] if len(rng) > 1 else rng[0]
    ang = _floats(header.get("scan_angle"))
    return {"cx_nm": off[0] * 1e9, "cy_nm": off[1] * 1e9, "w_nm": w_m * 1e9, "h_nm": h_m * 1e9,
            "angle_deg": ang[0] if ang else 0.0, "nx": px[0], "ny": px[1]}


def load_sxm(path: str | Path) -> ScanData:
    """Every channel and direction of a ``.sxm`` file, oriented, SI units, float32.

    Raises ``FileNotFoundError`` / ``ValueError`` with a message that says what is missing."""
    from mast.io.nanonis_files import read_sxm, sxm_oriented_frames

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"no such .sxm file: {p}")
    scan = read_sxm(str(p))
    header = scan.get("header") or {}
    geom = frame_geometry(header)
    if geom is None:
        raise ValueError(f"{p.name}: header has no scan range / offset / pixels")
    names = list(header.get("channel_names") or [])
    flags = list(header.get("channel_directions") or [])
    try:
        file_units = sxm_channel_units(p)
    except OSError:
        file_units = {}
    arrays: dict[str, np.ndarray] = {}
    channels: list[str] = []
    units: dict[str, str] = {}
    for i, name in enumerate(names):
        fr = sxm_oriented_frames(scan, name)
        fwd, bwd = fr.get("forward"), fr.get("backward")
        flag = flags[i] if i < len(flags) else "both"
        if "bwd" in flag and "fwd" not in flag:
            fwd, bwd = None, fwd          # MAST hands a backward-only channel back as "forward"
        got = False
        for direction, arr in (("forward", fwd), ("backward", bwd)):
            if arr is None:
                continue
            a = np.asarray(arr, dtype=np.float32)
            if a.ndim != 2 or a.shape != (int(geom["ny"]), int(geom["nx"])):
                continue
            arrays[f"{name}/{direction}"] = np.ascontiguousarray(a)
            got = True
        if got:
            channels.append(name)
            units[name] = unit_for_channel(name, file_units)
    if not arrays:
        raise ValueError(f"{p.name}: no channel data could be read")
    first = arrays.get(f"{_find(channels, 'Z') or channels[0]}/forward")
    if first is None:
        first = next(iter(arrays.values()))
    rec = f"{str(header.get('rec_date', '')).strip()} {str(header.get('rec_time', '')).strip()}".strip()
    return ScanData(
        arrays=arrays, channels=channels, units=units, geometry=dict(geom),
        nm_per_px=float(geom["w_nm"]) / int(geom["nx"]), nm_per_px_y=float(geom["h_nm"]) / int(geom["ny"]),
        bias_v=_first_float(header.get("bias")), setpoint_a=_first_float(header.get("z-controller>setpoint")),
        setpoint_unit=_setpoint_unit(header.get("z-controller>setpoint_unit"), header.get("z-controller>setpoint")),
        scan_dir=(str(header.get("scan_dir", "")).strip().lower() or None),
        acquired_rows=_acquired_rows(first), rec_time=rec, feedback=_feedback_state(header),
        source_name=p.name)


def _setpoint_unit(unit_field: Any, value_field: Any) -> str:
    """The feedback setpoint's SI unit: the header's unit field, else a unit written after the
    number (``"2.0E-10 A"``), else amperes (current feedback)."""
    u = str(unit_field or "").strip()
    if u:
        return u
    toks = str(value_field or "").split()
    if len(toks) >= 2 and toks[-1].isalpha():
        return toks[-1]
    return "A"


def _feedback_state(header: dict) -> str | None:
    """``"ON"`` / ``"OFF"`` from ``Z-Controller>Controller status``, else from the ``on``
    column of the ``:Z-CONTROLLER:`` table; ``None`` when the file says neither."""
    status = header.get("z-controller>controller_status")
    if status not in (None, ""):
        return str(status).strip().upper()
    row = str(header.get("z-controller", "")).strip()
    toks = [t.strip() for t in row.split("\t")]
    if len(toks) >= 2 and toks[1] in ("0", "1"):
        return "ON" if toks[1] == "1" else "OFF"
    return None


def scan_from_arrays(arrays: dict[str, np.ndarray], *, geometry: dict, meta: dict | None = None) -> ScanData:
    """Wrap arrays captured from a scan in progress. Keys ``"Z/forward"`` (or ``"Z"`` for the
    forward pass); arrays must already be oriented (row 0 = top edge, backward un-mirrored),
    in SI units, NaN for rows not yet acquired."""
    meta = dict(meta or {})
    out: dict[str, np.ndarray] = {}
    channels: list[str] = []
    shape = None
    for key, arr in (arrays or {}).items():
        name, _, direction = str(key).partition("/")
        direction = direction or "forward"
        if direction not in DIRECTIONS:
            raise ValueError(f"array key {key!r}: direction must be forward or backward")
        a = np.array(arr, dtype=np.float32)
        if a.ndim != 2 or min(a.shape) < 1:
            raise ValueError(f"array {key!r} is not a 2-D frame (shape {a.shape})")
        if shape is None:
            shape = a.shape
        elif a.shape != shape:
            raise ValueError(f"array {key!r} has shape {a.shape}, others {shape}")
        out[f"{name}/{direction}"] = a
        if name not in channels:
            channels.append(name)
    if not out:
        raise ValueError("no arrays given")
    g = dict(geometry or {})
    ny, nx = shape
    missing = [k for k in ("cx_nm", "cy_nm", "w_nm", "h_nm") if g.get(k) is None]
    if missing:
        raise ValueError(f"geometry lacks {missing}")
    g = {"cx_nm": float(g["cx_nm"]), "cy_nm": float(g["cy_nm"]), "w_nm": float(g["w_nm"]),
         "h_nm": float(g["h_nm"]), "angle_deg": float(g.get("angle_deg") or 0.0), "nx": int(nx), "ny": int(ny)}
    given_units = dict(meta.get("units") or {})
    units = {c: unit_for_channel(c, given_units) for c in channels}
    first = out.get(f"{_find(channels, 'Z') or channels[0]}/forward")
    if first is None:
        first = next(iter(out.values()))
    acq = meta.get("acquired_rows")
    return ScanData(
        arrays=out, channels=channels, units=units, geometry=g,
        nm_per_px=g["w_nm"] / nx, nm_per_px_y=g["h_nm"] / ny,
        bias_v=_first_float(meta.get("bias_v")), setpoint_a=_first_float(meta.get("setpoint_a")),
        setpoint_unit=str(meta.get("setpoint_unit") or "A"),
        scan_dir=(str(meta.get("scan_dir") or "").strip().lower() or None),
        acquired_rows=int(acq) if acq is not None else _acquired_rows(first),
        rec_time=str(meta.get("rec_time") or ""), feedback=meta.get("feedback"),
        source_name=str(meta.get("source_name") or "scan in progress"))


def column_unit(name: str) -> str:
    """``"Current (A)"`` -> ``"A"``; ``"Current [bwd] (A)"`` -> ``"A"``; no unit -> ``""``."""
    groups = re.findall(r"\(([^()]*)\)", str(name))
    return groups[-1].strip() if groups else ""


def load_dat(path: str | Path) -> SpectrumData:
    """A spectrum file: columns (SI) and header; the first column is the sweep axis."""
    from mast.io.nanonis_files import read_dat

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"no such .dat file: {p}")
    d = read_dat(str(p))
    cols = {str(k): np.asarray(v, dtype=np.float64) for k, v in (d.get("columns") or {}).items()}
    if not cols:
        raise ValueError(f"{p.name}: no data columns")
    hdr = {str(k): str(v) for k, v in (d.get("header") or {}).items()}
    x_m, y_m = _first_float(hdr.get("X (m)")), _first_float(hdr.get("Y (m)"))
    return SpectrumData(
        columns=cols, units={k: column_unit(k) for k in cols}, header=hdr,
        experiment=hdr.get("Experiment", ""), x_nm=None if x_m is None else x_m * 1e9,
        y_nm=None if y_m is None else y_m * 1e9, bias_v=_first_float(hdr.get("Bias>Bias (V)")),
        setpoint_a=_first_float(hdr.get("Z-Controller>Setpoint")),
        setpoint_unit=_setpoint_unit(hdr.get("Z-Controller>Setpoint unit"), hdr.get("Z-Controller>Setpoint")),
        rec_time=hdr.get("Saved Date") or hdr.get("Date", ""), source_name=p.name)


# ── channels and units ─────────────────────────────────────────────────────
def _norm(name: str) -> str:
    return re.sub(r"[\s_.\-]+", "", str(name).lower())


def _find(channels: list[str], name: str) -> str | None:
    for c in channels:
        if c == name:
            return c
    low = str(name).lower()
    for c in channels:
        if c.lower() == low:
            return c
    n = _norm(name)
    for c in channels:
        if _norm(c) == n:
            return c
    n = _norm(_strip_unit(name))
    for c in channels:
        if _norm(_strip_unit(c)) == n:
            return c
    return None


_ALIASES = {"height": "z", "topography": "z", "topo": "z", "it": "current", "i": "current",
            "tunnelingcurrent": "current", "tunnellingcurrent": "current"}


def resolve_channel(name: str, channels: list[str], units: dict[str, str] | None = None) -> str:
    """The channel ``name`` refers to: exact, case-insensitive, punctuation-insensitive, or an
    alias (``df`` / ``Δf`` / ``frequency shift`` -> the channel in Hz). ``KeyError`` lists the
    channels that exist."""
    found = _find(channels, name)
    if found:
        return found
    n = _norm(name)
    alias = _ALIASES.get(n)
    if alias:
        found = _find(channels, alias)
        if found:
            return found
    if n in ("df", "δf", "deltaf", "frequencyshift", "freqshift"):
        for c in channels:
            if unit_for_channel(c, units) == "Hz" or "freq" in c.lower():
                return c
    raise KeyError(f"no channel {name!r}; this frame has {channels}")


def default_channel(channels: list[str], arrays: dict[str, np.ndarray]) -> str:
    """Z, unless Z varies by less than 1 pm (constant height): then Current, then a
    frequency-shift channel, then the first channel that varies."""
    z = _find(channels, "Z")
    if z is not None:
        arr = arrays.get(f"{z}/forward")
        if arr is None:
            arr = arrays.get(f"{z}/backward")
        if arr is not None and _span(arr) >= CONSTANT_HEIGHT_Z_RANGE_M:
            return z
    cur = _find(channels, "Current")
    if cur is not None and _span(_any_dir(arrays, cur)) > 0:
        return cur
    for c in channels:
        if ("freq" in c.lower() or unit_for_channel(c) == "Hz") and _span(_any_dir(arrays, c)) > 0:
            return c
    for c in channels:
        if c != z and _span(_any_dir(arrays, c)) > 0:
            return c
    return z or channels[0]


def _any_dir(arrays: dict[str, np.ndarray], channel: str) -> np.ndarray | None:
    a = arrays.get(f"{channel}/forward")
    return a if a is not None else arrays.get(f"{channel}/backward")


def _span(arr: np.ndarray | None) -> float:
    if arr is None:
        return 0.0
    a = np.asarray(arr)
    fin = a[np.isfinite(a)]
    return float(fin.max() - fin.min()) if fin.size else 0.0


#: SI unit -> (model unit, factor). Every quantity of a kind is reported in ONE unit,
#: everywhere the model reads numbers: heights pm, currents pA, voltages V, frequencies Hz.
MODEL_UNITS = {"m": ("pm", 1e12), "A": ("pA", 1e12), "V": ("V", 1.0), "Hz": ("Hz", 1.0),
               "N": ("pN", 1e12), "deg": ("deg", 1.0), "s": ("s", 1.0)}


def display_unit(si_unit: str) -> tuple[float, str]:
    """``(factor, unit)``: multiply SI values by ``factor`` to get the model unit
    (:data:`MODEL_UNITS`); an unknown SI unit is reported as it is."""
    got = MODEL_UNITS.get(str(si_unit or "").strip())
    return (got[1], got[0]) if got else (1.0, str(si_unit or ""))


def unit_suffix(unit: str) -> str:
    """Key suffix of a model unit: ``pm`` -> ``pm``, ``pA`` -> ``pa``, ``Hz`` -> ``hz``,
    ``V`` -> ``v``; ``""`` for a unitless quantity."""
    return re.sub(r"[^a-z0-9]", "", str(unit or "").lower())


def unit_key(name: str, unit: str) -> str:
    """``unit_key("height", "pm") == "height_pm"``: model-facing keys carry their unit."""
    suf = unit_suffix(unit)
    return f"{name}_{suf}" if suf else name


def decimals_for(span: float | None) -> int:
    """Decimals that resolve about 1/100 of a value span (in model units), within 1..6:
    a 50 pm height range -> 1, a 4 pA current range -> 2, a 0.4 V sweep -> 3."""
    if span is None or not math.isfinite(span) or span <= 0:
        return 1
    return int(min(6, max(1, 2 - math.floor(math.log10(span)))))


def round_to(v: Any, decimals: int) -> float | None:
    """``v`` rounded to ``decimals`` (``-0.0`` becomes ``0.0``); ``None`` for NaN / missing."""
    if v is None:
        return None
    f = float(v)
    if not math.isfinite(f):
        return None
    return round(f, int(decimals)) + 0.0


def robust_span(*arrays: np.ndarray) -> float | None:
    """99.5th minus 0.5th percentile of the finite values of all ``arrays``."""
    vals = [np.asarray(a, dtype=float).ravel() for a in arrays if a is not None]
    if not vals:
        return None
    v = np.concatenate(vals)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return None
    lo, hi = np.percentile(v, [0.5, 99.5])
    return float(hi - lo)


# ── display coordinates ────────────────────────────────────────────────────
def scale_pair(nx: int, ny: int, display_max: int = DISPLAY_MAX_DEFAULT) -> tuple[int, int]:
    """``(num, den)`` with ``k = num/den``: ``(s, 1)`` for integer upscaling by ``s`` or
    ``(1, f)`` for block-mean downsampling by ``f`` (``protocol.DISPLAY_CONVENTION``)."""
    nx, ny, dm = int(nx), int(ny), int(display_max)
    if nx < 1 or ny < 1 or dm < 1:
        raise ValueError(f"bad frame size {nx}x{ny} or display_max {dm}")
    big = max(nx, ny)
    if big <= dm:
        return max(1, dm // big), 1
    return 1, int(math.ceil(big / dm))


def display_scale(nx: int, ny: int, display_max: int = DISPLAY_MAX_DEFAULT) -> float:
    num, den = scale_pair(nx, ny, display_max)
    return num / den


def scale_text(num: int, den: int) -> str:
    return str(num) if den == 1 else f"1/{den}"


def display_size(nx: int, ny: int, display_max: int = DISPLAY_MAX_DEFAULT) -> tuple[int, int]:
    num, den = scale_pair(nx, ny, display_max)
    if den == 1:
        return int(nx) * num, int(ny) * num
    return int(math.ceil(nx / den)), int(math.ceil(ny / den))


def _geom_and_pair(entry: Any, display_max: int | None) -> tuple[dict, int, int]:
    geom = getattr(entry, "geometry", None)
    if geom is None and isinstance(entry, dict):
        geom = entry
    if not geom or "nx" not in geom:
        raise ValueError("this frame has no scan geometry (spectra and plots have none)")
    nx, ny = int(geom["nx"]), int(geom["ny"])
    if display_max is None:
        k = getattr(entry, "display_scale", None)
        if k is not None and not isinstance(entry, dict):
            k = float(k)
            if k >= 1:
                return geom, int(round(k)), 1
            return geom, 1, int(round(1.0 / k))
        display_max = DISPLAY_MAX_DEFAULT
    num, den = scale_pair(nx, ny, display_max)
    return geom, num, den


def native_to_scan_nm(geom: dict, col: Any, row: Any) -> tuple[Any, Any]:
    """Continuous native coordinates (0 = the left / top EDGE of the frame, pixel ``i``
    covers ``[i, i+1)``) -> scan-frame nm. Accepts scalars or arrays."""
    w, h = float(geom["w_nm"]), float(geom["h_nm"])
    nx, ny = int(geom["nx"]), int(geom["ny"])
    u = -w / 2.0 + np.asarray(col, dtype=float) * (w / nx)
    v = h / 2.0 - np.asarray(row, dtype=float) * (h / ny)
    a = math.radians(float(geom.get("angle_deg") or 0.0))
    c, s = math.cos(a), math.sin(a)
    X = float(geom["cx_nm"]) + c * u - s * v
    Y = float(geom["cy_nm"]) + s * u + c * v
    return _scalarize(X), _scalarize(Y)


def scan_nm_to_native(geom: dict, X: Any, Y: Any) -> tuple[Any, Any]:
    """Inverse of :func:`native_to_scan_nm`."""
    w, h = float(geom["w_nm"]), float(geom["h_nm"])
    nx, ny = int(geom["nx"]), int(geom["ny"])
    a = math.radians(float(geom.get("angle_deg") or 0.0))
    c, s = math.cos(a), math.sin(a)
    dx = np.asarray(X, dtype=float) - float(geom["cx_nm"])
    dy = np.asarray(Y, dtype=float) - float(geom["cy_nm"])
    u = c * dx + s * dy
    v = -s * dx + c * dy
    col = (u + w / 2.0) * (nx / w)
    row = (h / 2.0 - v) * (ny / h)
    return _scalarize(col), _scalarize(row)


def display_to_scan_nm(entry: Any, x: Any, y: Any, display_max: int | None = None) -> tuple[Any, Any]:
    """Display coordinates of a frame (continuous, origin at the top-left corner, ``x``
    right, ``y`` down) -> scan-frame nm. ``entry`` is a :class:`~.archive.FrameEntry` (its
    own display scale is used unless ``display_max`` is given) or a geometry dict.
    ``tests/v2/vista/test_frames.py`` pins it to STM-Bench's pixel-centre formula."""
    geom, num, den = _geom_and_pair(entry, display_max)
    col = np.asarray(x, dtype=float) * den / num
    row = np.asarray(y, dtype=float) * den / num
    return native_to_scan_nm(geom, col, row)


def scan_nm_to_display(entry: Any, X: Any, Y: Any, display_max: int | None = None) -> tuple[Any, Any]:
    """Inverse of :func:`display_to_scan_nm`."""
    geom, num, den = _geom_and_pair(entry, display_max)
    col, row = scan_nm_to_native(geom, X, Y)
    return _scalarize(np.asarray(col) * num / den), _scalarize(np.asarray(row) * num / den)


def _scalarize(a: Any) -> Any:
    arr = np.asarray(a)
    return float(arr) if arr.ndim == 0 else arr


# ── flattening ──────────────────────────────────────────────────────────────
def _fit_surface(z: np.ndarray, order: int) -> np.ndarray:
    ny, nx = z.shape
    ok = np.isfinite(z)
    if ok.sum() < (3 if order == 1 else 6):
        return np.zeros_like(z)
    yy, xx = np.mgrid[0:ny, 0:nx]
    xs = (xx - (nx - 1) / 2.0) / max(nx, 1)
    ys = (yy - (ny - 1) / 2.0) / max(ny, 1)
    cols = [np.ones_like(xs), xs, ys]
    if order >= 2:
        cols += [xs * xs, xs * ys, ys * ys]
    a = np.column_stack([c[ok] for c in cols])
    coef, *_ = np.linalg.lstsq(a, z[ok], rcond=None)
    return sum(cf * c for cf, c in zip(coef, cols, strict=True))


def _row_offsets(z: np.ndarray) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(z, axis=1, keepdims=True)
    return np.where(np.isfinite(med), med, 0.0)


def median_background(z: np.ndarray, width_px: float) -> np.ndarray:
    """A median-filtered background with a window of ``width_px`` pixels (computed on a
    block-averaged copy when the window is wide, then interpolated back)."""
    from scipy.ndimage import map_coordinates, median_filter

    z = np.asarray(z, dtype=float)
    ok = np.isfinite(z)
    fill = float(np.nanmedian(z)) if ok.any() else 0.0
    zf = np.where(ok, z, fill)
    width_px = max(3.0, float(width_px))
    d = max(1, int(math.ceil(width_px / 15.0)))  # a median of <= 15 pixels on a block-averaged copy
    small = block_mean(zf, d) if d > 1 else zf
    size = max(3, int(round(width_px / d)) | 1)
    bg_small = median_filter(small, size=size, mode="nearest")
    if d == 1:
        return bg_small
    ny, nx = z.shape
    rows = (np.arange(ny) + 0.5) / d - 0.5
    cols = (np.arange(nx) + 0.5) / d - 0.5
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    return map_coordinates(bg_small, [rr, cc], order=1, mode="nearest")


def flatten(z: np.ndarray, mode: str = "plane", *, nm_per_px: float | None = None,
            highpass_nm: float = HIGHPASS_NM_DEFAULT) -> np.ndarray:
    """A float64 copy of ``z`` with a background removed; NaN pixels stay NaN.

    ``plane``: least-squares plane; ``line``: plane, then each row's median offset;
    ``poly2``: second-order surface; ``highpass``: plane, then a median background of
    ``highpass_nm`` (needs ``nm_per_px``); ``none``: unchanged."""
    zz = np.array(z, dtype=float)
    if zz.ndim != 2:
        raise ValueError("flatten needs a 2-D array")
    if mode not in FLATTEN_MODES:
        raise ValueError(f"flatten must be one of {FLATTEN_MODES}, not {mode!r}")
    if mode == "none":
        return zz
    nan = ~np.isfinite(zz)
    if mode == "poly2":
        out = zz - _fit_surface(zz, 2)
    else:
        out = zz - _fit_surface(zz, 1)
    if mode == "line":
        out = out - _row_offsets(out)
    elif mode == "highpass":
        if not nm_per_px or nm_per_px <= 0:
            raise ValueError("highpass flattening needs the pixel size")
        out = out - median_background(out, float(highpass_nm) / float(nm_per_px))
    out[nan] = np.nan
    return out


# ── pixels ──────────────────────────────────────────────────────────────────
def block_mean(z: np.ndarray, f: int) -> np.ndarray:
    """NaN-aware mean over ``f`` x ``f`` blocks (edge blocks may be partial)."""
    f = int(f)
    z = np.asarray(z, dtype=float)
    if f <= 1:
        return z.copy()
    ny, nx = z.shape
    py, px = (-ny) % f, (-nx) % f
    zp = np.pad(z, ((0, py), (0, px)), constant_values=np.nan)
    blocks = zp.reshape(zp.shape[0] // f, f, zp.shape[1] // f, f)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(blocks, axis=(1, 3))


def colour_limits(z: np.ndarray, clip_pct: float = CLIP_PCT_DEFAULT) -> tuple[float, float]:
    """``(lo, hi)``: the ``clip_pct`` / ``100 - clip_pct`` percentiles of the finite values."""
    vals = np.asarray(z, dtype=float)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return 0.0, 1.0
    c = float(min(max(clip_pct, 0.0), 49.0))
    lo, hi = (float(v) for v in np.percentile(vals, [c, 100.0 - c]))
    if not hi > lo:
        lo, hi = float(vals.min()), float(vals.max())
    return lo, hi


def grey_levels(z: np.ndarray, lo: float, hi: float) -> tuple[np.ndarray, np.ndarray]:
    """uint8 grey levels (black = ``lo``, white = ``hi``) and the NaN mask. A flat region
    (``hi <= lo``) is mid grey."""
    z = np.asarray(z, dtype=float)
    nan = ~np.isfinite(z)
    if hi > lo:
        norm = (z - lo) / (hi - lo)
    else:
        norm = np.full(z.shape, 0.5)
    with np.errstate(invalid="ignore"):
        g = np.clip(np.where(nan, 0.0, norm), 0.0, 1.0)
    return np.rint(g * 255.0).astype(np.uint8), nan


def compose_rgb(grey: np.ndarray, nan: np.ndarray) -> np.ndarray:
    """``L`` image when nothing is missing, else RGB with missing pixels in :data:`NAN_RGB`."""
    if not nan.any():
        return grey
    rgb = np.repeat(grey[:, :, None], 3, axis=2)
    rgb[nan] = NAN_RGB
    return rgb


def upscale(img: np.ndarray, s: int) -> np.ndarray:
    """Integer nearest-neighbour enlargement (each pixel becomes an ``s`` x ``s`` block)."""
    s = int(s)
    if s <= 1:
        return img
    return np.repeat(np.repeat(img, s, axis=0), s, axis=1)


# ── grid (graph paper) ──────────────────────────────────────────────────────
# VISTA darkens every cell boundary of an ARC frame to 3/4: graph paper to count cells on. An
# STM frame has no cells, so this grid is metric: square cells of 1, 2 or 5 x 10^n nm counted
# from the top-left corner of the frame's full image, finer as a view zooms in. Each line is one
# image pixel wide, on the pixel that holds its exact position (evenly spaced to a pixel at any
# magnification), blended a quarter of the way toward an amber no grey level has (darkening
# would vanish on black and could pass for a trench).
GRID_RGB = (255, 176, 0)
GRID_COLOUR_NAME = "amber"
GRID_MAX_CELLS = 16       # at most this many cells across the longer side of a view ...
GRID_MIN_CELL_PX = 2      # ... and at least this many scan pixels per cell


def nice_step(x: float) -> float:
    """The smallest 1, 2 or 5 x 10^n that is >= ``x`` (``x`` > 0); 12 significant digits."""
    x = float(x)
    if not (x > 0 and math.isfinite(x)):
        raise ValueError("nice_step needs a positive finite number")
    n = math.floor(math.log10(x)) - 1       # one decade low: log10 may be an ulp off
    while True:
        for m in (1, 2, 5):
            v = float(f"{m * 10.0 ** n:.12g}")
            if v >= x * (1 - 1e-9):
                return v
        n += 1


@dataclass(frozen=True)
class Grid:
    """The grid of one view: lines every ``spacing_nm`` from the frame's top-left corner, at
    scan-pixel coordinates ``k * spacing_nm / nm_per_px_x`` (k = 0, 1, ...) along x and
    likewise along y with ``nm_per_px_y``."""

    spacing_nm: float
    nm_per_px_x: float
    nm_per_px_y: float


def view_grid(view: "View", nm_per_px_x: float, nm_per_px_y: float) -> Grid:
    """The grid for a view: the smallest nice spacing with at most :data:`GRID_MAX_CELLS`
    cells across the view's longer side and at least :data:`GRID_MIN_CELL_PX` scan pixels per
    cell."""
    i0, j0, i1, j1 = view.native
    px, py = float(nm_per_px_x), float(nm_per_px_y)
    extent = max((i1 - i0) * px, (j1 - j0) * py)
    return Grid(nice_step(max(extent / GRID_MAX_CELLS, GRID_MIN_CELL_PX * max(px, py))), px, py)


def grid_lines(lo: int, hi: int, step_px: float, up: int, down: int, size: int) -> list[int]:
    """Image pixels of the grid lines of a view spanning scan pixels ``[lo, hi)``: line ``k`` at
    scan-pixel coordinate ``k * step_px`` lies on pixel ``floor((k * step_px - lo) * up / down
    + 1e-9)``; lines at or past ``hi`` and pixels outside ``[0, size)`` are dropped."""
    out: list[int] = []
    k = max(0, math.ceil(lo / step_px - 1e-9))
    while True:
        pos = k * step_px
        if pos >= hi - 1e-9:
            return out
        p = math.floor((pos - lo) * up / down + 1e-9)
        if 0 <= p < size and (not out or p != out[-1]):
            out.append(p)
        k += 1


def draw_grid(img: np.ndarray, view: "View", grid: Grid) -> np.ndarray:
    """``img`` (a rendered view, L or RGB) as RGB with the grid drawn: every pixel on a line
    becomes ``(3 * pixel + GRID_RGB) // 4`` per channel (:func:`grid_lines` places the lines)."""
    i0, j0, i1, j1 = view.native
    rgb = (np.repeat(img[:, :, None], 3, axis=2) if img.ndim == 2 else img).astype(np.int32)
    H, W = rgb.shape[:2]
    xs = grid_lines(i0, i1, grid.spacing_nm / grid.nm_per_px_x, view.up, view.down, W)
    ys = grid_lines(j0, j1, grid.spacing_nm / grid.nm_per_px_y, view.up, view.down, H)
    mask = np.zeros((H, W), dtype=bool)
    mask[:, xs] = True
    mask[ys, :] = True
    rgb[mask] = (3 * rgb[mask] + np.asarray(GRID_RGB, dtype=np.int32)) // 4
    return rgb.astype(np.uint8)


def encode_png(img: np.ndarray) -> bytes:
    from PIL import Image

    a = np.ascontiguousarray(img)
    if a.ndim == 2:
        im = Image.fromarray(a.astype(np.uint8), mode="L")
    else:
        im = Image.fromarray(a.astype(np.uint8), mode="RGB")
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def png_size(png: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        return int(im.size[0]), int(im.size[1])


# ── views (a whole frame or an exact region of it) ─────────────────────────
@dataclass
class View:
    """Where a rendered image sits in its frame. Display px are those of the frame's full
    image at its display scale ``num/den``; native px are the frame's own pixels."""

    region_px: tuple[int, int, int, int]       # x, y, width, height in display px (whole scan px)
    native: tuple[int, int, int, int]          # i0, j0, i1, j1 (half-open)
    image_size: tuple[int, int]                # W, H of the rendered image
    up: int                                    # image px per native px (>= 1) ...
    down: int                                  # ... or native px per image px (block mean)
    num: int
    den: int

    @property
    def magnification(self) -> float:
        """Rendered image px per display px of the full frame image."""
        return (self.up / self.down) * (self.den / self.num)


def region_to_native(region: tuple[int, int, int, int] | None, nx: int, ny: int,
                     num: int, den: int) -> tuple[int, int, int, int]:
    """Native pixel box ``(i0, j0, i1, j1)`` covering a display-px region, widened outward
    to whole native pixels. ``ValueError`` when the region leaves the frame."""
    W = nx * num if den == 1 else int(math.ceil(nx / den))
    H = ny * num if den == 1 else int(math.ceil(ny / den))
    if region is None:
        return 0, 0, nx, ny
    x, y, w, h = (int(v) for v in region)
    if w < 1 or h < 1:
        raise ValueError("region width and height must be at least 1")
    if x < 0 or y < 0 or x + w > W or y + h > H:
        raise ValueError(f"region x={x}, y={y}, width={w}, height={h} leaves the frame's "
                         f"{W}x{H} display image (x + width <= {W}, y + height <= {H})")
    if den == 1:
        i0, j0 = x // num, y // num
        i1, j1 = -(-(x + w) // num), -(-(y + h) // num)
    else:
        i0, j0 = x * den, y * den
        i1, j1 = min(nx, (x + w) * den), min(ny, (y + h) * den)
    return i0, j0, i1, j1


def plan_view(nx: int, ny: int, num: int, den: int, region: tuple[int, int, int, int] | None,
              display_max: int) -> View:
    """How a region (display px, or ``None`` = whole frame) is shown: the largest whole
    number of image pixels per native pixel that fits ``display_max``, or block-mean
    downsampling when the region has more native pixels than that."""
    i0, j0, i1, j1 = region_to_native(region, nx, ny, num, den)
    cw, ch = i1 - i0, j1 - j0
    big = max(cw, ch)
    if big <= display_max:
        up, down = max(1, int(display_max) // big), 1
        size = (cw * up, ch * up)
    else:
        up, down = 1, int(math.ceil(big / display_max))
        size = (int(math.ceil(cw / down)), int(math.ceil(ch / down)))
    if den == 1:
        reg = (i0 * num, j0 * num, cw * num, ch * num)
    else:
        rx, ry = i0 // den, j0 // den
        reg = (rx, ry, int(math.ceil(i1 / den)) - rx, int(math.ceil(j1 / den)) - ry)
    return View(region_px=reg, native=(i0, j0, i1, j1), image_size=size, up=up, down=down, num=num, den=den)


def render_values(z: np.ndarray, view: View, *, clip_pct: float = CLIP_PCT_DEFAULT,
                  limits: tuple[float, float] | None = None,
                  grid: Grid | None = None) -> tuple[bytes, float, float]:
    """PNG of the native crop of ``z`` (already flattened) described by ``view``, with
    ``grid`` drawn when given; returns ``(png, lo, hi)`` with the colour limits used
    (percentiles of the crop, which the grid does not change)."""
    i0, j0, i1, j1 = view.native
    crop = np.asarray(z, dtype=float)[j0:j1, i0:i1]
    lo, hi = limits if limits is not None else colour_limits(crop, clip_pct)
    if view.down > 1:
        crop = block_mean(crop, view.down)
    grey, nan = grey_levels(crop, lo, hi)
    img = upscale(compose_rgb(grey, nan), view.up)
    if grid is not None:
        img = draw_grid(img, view, grid)
    return encode_png(img), float(lo), float(hi)


def view_corners_nm(geom: dict, view: View) -> dict:
    """Scan-frame nm of the four corners of a view's region."""
    i0, j0, i1, j1 = view.native
    out = {}
    for name, (c, r) in {"top_left": (i0, j0), "top_right": (i1, j0),
                         "bottom_left": (i0, j1), "bottom_right": (i1, j1)}.items():
        X, Y = native_to_scan_nm(geom, c, r)
        out[name] = [round(float(X), 4), round(float(Y), 4)]
    return out


# ── labels ──────────────────────────────────────────────────────────────────
def _esc(v: Any) -> str:
    return (str(v).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\n", " "))


def visual_label(kind: str, **attrs: Any) -> str:
    """``<visual kind="current" frame="t0007.s0" .../>``; ``None`` attributes are omitted."""
    parts = [f'kind="{_esc(kind)}"']
    for k, v in attrs.items():
        if v is None:
            continue
        parts.append(f'{k}="{_esc(v)}"')
    return "<visual " + " ".join(parts) + "/>"


def fmt_num(v: float, nd: int = 1) -> str:
    if v is None or not math.isfinite(v):
        return "nan"
    out = f"{v:.{nd}f}"
    return out[1:] if out.startswith("-") and float(out) == 0.0 else out


def range_attr(channel: str, unit: str) -> str:
    """Label attribute for the color scale: ``z_range_pm`` for Z, else ``range_<unit suffix>``."""
    if str(channel).strip().lower() == "z" and unit == "pm":
        return "z_range_pm"
    return unit_key("range", unit) if unit else "range"


# ── spectra ─────────────────────────────────────────────────────────────────
def render_spectrum(columns: dict[str, np.ndarray], units: dict[str, str], *, x_col: str,
                    y_cols: list[str], x_range: tuple[float, float] | None = None,
                    width_px: int = DISPLAY_MAX_DEFAULT, panel_px: int = 300) -> tuple[bytes, dict]:
    """Line plot(s) of ``y_cols`` against ``x_col`` (one panel each, shared x), values in
    model units. ``x_range`` is in the x column's model unit. Returns ``(png, meta)``."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    xs_si = np.asarray(columns[x_col], dtype=float)
    fx, ux = display_unit(units.get(x_col, ""))
    xs = xs_si * fx
    sel = np.ones(xs.shape, dtype=bool)
    if x_range is not None:
        lo, hi = sorted(float(v) for v in x_range)
        sel = in_range(xs, lo, hi)
        if not sel.any():
            raise ValueError(f"x_range {lo}..{hi} {ux} contains no samples "
                             f"(x spans {np.nanmin(xs):.4g}..{np.nanmax(xs):.4g} {ux})")
    width_px = max(600, int(width_px))
    n = len(y_cols)
    height_px = max(360, min(width_px, 90 + panel_px * n))
    fig = Figure(figsize=(width_px / 100.0, height_px / 100.0), dpi=100)
    FigureCanvasAgg(fig)
    axes = fig.subplots(n, 1, sharex=True, squeeze=False)[:, 0]
    meta_cols = []
    for ax, yc in zip(axes, y_cols, strict=True):
        ys_si = np.asarray(columns[yc], dtype=float)
        fy, uy = display_unit(units.get(yc, ""))
        ax.plot(xs[sel], ys_si[sel] * fy, lw=1.6, color="#1f4e9c")
        ax.set_ylabel(f"{_strip_unit(yc)} ({uy})" if uy else yc, fontsize=13)
        ax.grid(True, alpha=0.35)
        ax.tick_params(labelsize=12)
        meta_cols.append({"column": yc, "unit": uy})
    axes[-1].set_xlabel(f"{_strip_unit(x_col)} ({ux})" if ux else x_col, fontsize=13)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    png = buf.getvalue()
    shown = xs[sel][np.isfinite(xs[sel])]          # an x column without a finite value shows nothing
    meta = {"x_col": x_col, "x_unit": ux, "y": meta_cols, "n_points": int(sel.sum()),
            "x_shown": [float(shown.min()), float(shown.max())] if shown.size else [math.nan, math.nan]}
    return png, meta


def in_range(xs: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """``lo <= xs <= hi`` with a relative tolerance of 1e-9 (unit conversion rounding)."""
    eps = 1e-9 * max(1.0, abs(lo), abs(hi))
    return (xs >= lo - eps) & (xs <= hi + eps)


def _strip_unit(name: str) -> str:
    return re.sub(r"\s*\([^()]*\)\s*$", "", str(name)).strip() or str(name)

