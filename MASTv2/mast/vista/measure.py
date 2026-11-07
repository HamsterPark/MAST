"""Measurement tools: ``line_profile``, ``fft_peaks``, ``detect_blobs``, ``diff``, ``stats``.

README
------
Read-only tools over the workspace's :class:`~.archive.Archive`. They **measure, never
interpret**: no tool says "double tip", "atomic resolution" or "the atom moved"; they
return numbers with units and scan-frame positions, and the model judges.

* ``line_profile`` — values along a segment (display px or scan nm end points), averaged
  across a width, plus step candidates from a piecewise-linear change-point fit; one plot.
* ``fft_peaks`` — Hann-windowed, zero-padded 2-D spectrum; strongest local maxima outside
  the DC lobe with spatial frequency (cycles/nm), period, scan-frame angle and strength in
  robust-noise units over the background at the same |k|, and how close each lies to a pixel
  axis's Nyquist frequency (a possible alias); one numbered spectrum image.
* ``detect_blobs`` — local background removed (median of ``background_nm``), smoothed
  residual thresholded at 6 robust sigmas (the residual's lower tail, capped at 8 pixel-to-pixel
  noises where a floor or deep gaps fill that tail), maxima merged unless a saddle separates them,
  each fitted with an elliptical Gaussian; centroid (display px and scan nm), height,
  FWHM, elongation, axis angle; one image with numbered marks (Set-of-Mark).
* ``diff`` — two frames on a common grid (resampled onto the overlap in scan nm when the
  geometry differs), sub-pixel normalized cross-correlation registration with a
  peak / second-peak confidence and the second peak's shift, aligned difference (b minus
  shifted a) as a derived map with values, and the changed regions.
* ``stats`` — moments, percentiles, roughness, forward/backward agreement, line noise,
  rows acquired.

Every derived image is archived with :meth:`Archive.add_derived` (frame kind ``m``) so the
model can inspect it again; marks and numbers exist only on those derived images. The
array-level functions (:func:`find_blobs`, :func:`spectrum_peaks`, :func:`register`,
:func:`change_points`) are pure and unit-tested on synthetic data.
"""
from __future__ import annotations

import functools
import io
import math
import warnings
from statistics import NormalDist
from typing import Any, Callable

import numpy as np

from . import frames as F
from .archive import Archive, FrameEntry
from .protocol import DISPLAY_MAX_DEFAULT, ImageBlock, ToolDef, ToolReply, json_reply
from .viewtools import (FRAME_ID_PATTERN, ArgError, bool_arg, channel_direction_args, check_keys,
                        flatten_args, frame_arg, never_raises, num_arg, region_arg, region_schema, str_arg)

FWHM_PER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))
MARK_RGB = (255, 214, 0)
MAX_PROFILE_SAMPLES = 400
REFINE_PX = 256          # side of the central window the sub-pixel registration is refined on
REFINE_STEP_PX = 1       # the refinement may move the shift at most this far from the parabolic start
#: At most this many blobs per reply (200 blobs make a 45 KB reply), which keeps a reply within
#: a few tens of KB.
MAX_BLOBS = 50
AXIS_MIN_ELONGATION = 1.1   # below this a blob's axis angle is noise and is reported as null
MAX_REGIONS = 30
NEAR_NYQUIST_FRACTION = 0.8  # an FFT peak this close to a pixel axis's Nyquist frequency may be an alias


# ── shared helpers ──────────────────────────────────────────────────────────
def _mad_sigma(v: np.ndarray) -> float:
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return 0.0
    return float(1.4826 * np.median(np.abs(v - np.median(v))))


#: Width of the 5th..30th percentile span of a normal distribution, in sigmas.
_LOWER_SPAN = NormalDist().inv_cdf(0.30) - NormalDist().inv_cdf(0.05)


def _lower_sigma(v: np.ndarray) -> float:
    """Noise sigma from the lower part of a distribution: (30th - 5th percentile) / 1.1205.
    Features that only add to the values (bright blobs; dark ones after the sign flip) do not reach
    that part while they cover less than about 70 % of the pixels, whereas the median absolute
    deviation measures them once they cover a sizeable fraction, as a dense lattice does."""
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return 0.0
    p5, p30 = np.percentile(v, [5.0, 30.0])
    return float((p30 - p5) / _LOWER_SPAN)


#: The residual's noise cannot plausibly exceed this many times its pixel-to-pixel noise
#: (:func:`_hf_sigma`). White noise gives 1; line offsets, a feedback that low-passes the fast axis
#: and correlated (1/f-like) noise give 1 to about 4. A frame whose lower tail is structure, a flat
#: floor or the deep gaps between touching features (the Mimea simulator's close-packed balls on
#: their floor: 189), gives far more, and its lower-tail "noise" would hide every feature.
#: What one frame cannot tell apart: noise smooth along both axes with no pixel-to-pixel part at all
#: (a smoothed image; raw rows are a line time apart, so their electronic noise is independent) looks
#: like topography here, and its bumps are then reported as blobs.
HF_NOISE_CAP = 8.0


def _hf_sigma(v: np.ndarray) -> float:
    """Pixel-to-pixel noise sigma from second differences, 1.4826 * MAD / sqrt(6) along each axis
    (white noise of sigma s gives s; a smooth surface's curvature adds little), the larger of the two:
    rows are acquired one line time apart, so the slow axis still sees the noise when the feedback
    low-passes the fast one. 0 when too few finite differences exist (then nothing is capped)."""
    v = np.asarray(v, dtype=float)
    out = 0.0
    for axis in (0, 1):
        if v.shape[axis] < 3:
            continue
        d = np.diff(v, n=2, axis=axis)
        d = d[np.isfinite(d)]
        if d.size >= 16:
            out = max(out, float(1.4826 * np.median(np.abs(d - np.median(d))) / math.sqrt(6.0)))
    return out


def _pixel_nm(e: FrameEntry) -> tuple[float, float]:
    dx = float(e.nm_per_px)
    dy = float(e.extra.get("nm_per_px_y") or dx)
    return dx, dy


def _flat_frame(archive: Archive, e: FrameEntry, ch: str, direction: str, mode: str, hp: float) -> np.ndarray:
    dx, dy = _pixel_nm(e)
    return F.flatten(archive.values(e, ch, direction), mode, nm_per_px=math.sqrt(dx * dy), highpass_nm=hp)


def _image_entry(archive: Archive, args: dict, key: str = "frame") -> FrameEntry:
    e = frame_arg(archive, args.get(key), key, need_values=True)
    if not e.is_image:
        raise ArgError(f"{key}: {e.fid} is a {e.kind_word}; this tool needs a scan frame")
    return e


def _native_box(e: FrameEntry, region: tuple | None) -> tuple[int, int, int, int]:
    num, den = e.scale_pair
    return F.region_to_native(region, e.nx, e.ny, num, den)


def _native_to_display(e: FrameEntry, col: float, row: float) -> tuple[float, float]:
    num, den = e.scale_pair
    return col * num / den, row * num / den


@functools.lru_cache(maxsize=8)
def _font(size: int):
    """DejaVu Sans (matplotlib's copy) for the mark numbers; Pillow's default font when no TrueType
    font loads (a frozen build without matplotlib's font files, or without FreeType)."""
    from PIL import ImageFont

    try:
        from matplotlib import font_manager

        return ImageFont.truetype(font_manager.findfont("DejaVu Sans", fallback_to_default=True), size)
    except Exception:  # noqa: BLE001 — any working font will do
        try:
            return ImageFont.load_default(size=size)
        except Exception:  # noqa: BLE001 — no FreeType (or Pillow < 10.1): the bitmap font
            return ImageFont.load_default()


def _png_to_rgb(png: bytes) -> np.ndarray:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        return np.asarray(im.convert("RGB")).copy()


def _draw_marks(rgb: np.ndarray, marks: list[tuple[float, float, float, str]], *,
                colour: tuple[int, int, int] = MARK_RGB, ring: bool = True) -> bytes:
    """Numbered rings at image coordinates ``(x, y, radius, text)``; returns PNG bytes."""
    from PIL import Image, ImageDraw

    im = Image.fromarray(np.ascontiguousarray(rgb, dtype=np.uint8), mode="RGB")
    draw = ImageDraw.Draw(im)
    W, H = im.size
    size = int(min(22, max(13, round(0.022 * max(W, H)))))
    font = _font(size)
    for x, y, r, text in marks:
        if ring:
            draw.ellipse([x - r, y - r, x + r, y + r], outline=colour, width=2)
        tx = min(max(0, x + 0.72 * r + 1), W - size * 0.62 * len(text) - 2)
        ty = min(max(0, y - 0.72 * r - size - 1), H - size - 2)
        draw.text((tx, ty), text, fill=colour, font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def _register_derived(archive: Archive, png: bytes, *, tool: str, sources: list[str], description: str,
                      label_attrs: dict, arrays: dict | None = None, geometry: dict | None = None,
                      units: dict | None = None) -> tuple[FrameEntry, ImageBlock]:
    meta: dict[str, Any] = {"tool": tool, "sources": sources, "description": description,
                            "label_attrs": label_attrs}
    if arrays:
        meta.update({"geometry": geometry, "units": units})
    e = archive.add_derived(archive.current_turn, png, meta=meta, arrays=arrays)
    return e, archive.observation(e.fid)[0]


def _render_rgb(z: np.ndarray, e: FrameEntry, box: tuple[int, int, int, int], display_max: int,
                clip_pct: float = F.CLIP_PCT_DEFAULT) -> tuple[np.ndarray, F.View, float, float]:
    """The flattened frame (or its region) as an RGB array at the inspect magnification."""
    num, den = e.scale_pair
    i0, j0, i1, j1 = box
    region = None
    if (i0, j0, i1, j1) != (0, 0, e.nx, e.ny):
        region = (int(i0 * num // den), int(j0 * num // den),
                  max(1, int(math.ceil((i1 - i0) * num / den))), max(1, int(math.ceil((j1 - j0) * num / den))))
        W, H = e.display_size
        region = (region[0], region[1], min(region[2], W - region[0]), min(region[3], H - region[1]))
    view = F.plan_view(e.nx, e.ny, num, den, region, display_max)
    png, lo, hi = F.render_values(z, view, clip_pct=clip_pct)
    return _png_to_rgb(png), view, lo, hi


def _image_xy(view: F.View, col: float, row: float) -> tuple[float, float]:
    """Native continuous coordinates -> pixel coordinates in a rendered view image."""
    i0, j0, _, _ = view.native
    return (col - i0) * view.up / view.down, (row - j0) * view.up / view.down


def _plot_png(width_px: int, height_px: int, draw: Callable) -> bytes:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(width_px / 100.0, height_px / 100.0), dpi=100)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    draw(ax)
    ax.grid(True, alpha=0.35)
    ax.tick_params(labelsize=12)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    return buf.getvalue()


def _point_schema() -> dict:
    return {"type": "object", "additionalProperties": False,
            "description": ("A point: either {x, y} in display pixels of the frame's full image, or "
                            "{x_nm, y_nm} in scan-frame nm."),
            "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                           "x_nm": {"type": "number"}, "y_nm": {"type": "number"}}}


def _frame_schema(desc: str = "Archived scan frame id, e.g. 't0007.s0'.") -> dict:
    return {"type": "string", "pattern": FRAME_ID_PATTERN, "description": desc}


_CHANNEL_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 64,
                   "description": "Channel, e.g. 'Z' or 'Current'. Default: the frame's default channel."}
_DIRECTION_SCHEMA = {"type": "string", "enum": list(F.DIRECTIONS), "description": "Scan pass. Default forward."}
_FLATTEN_SCHEMA = {"type": "string", "enum": list(F.FLATTEN_MODES),
                   "description": "Background removed over the whole frame first (default plane)."}
_HIGHPASS_SCHEMA = {"type": "number", "exclusiveMinimum": 0, "maximum": 10000,
                    "description": "Background width in nm for flatten='highpass'. Default 3."}


# ── change points (line_profile) ────────────────────────────────────────────
def _linfit_sse(n, sx, sy, sxx, sxy, syy):
    with np.errstate(divide="ignore", invalid="ignore"):
        vx = sxx - sx * sx / n
        vxy = sxy - sx * sy / n
        vy = syy - sy * sy / n
        sse = np.where(vx > 1e-12 * np.maximum(1.0, sxx), vy - vxy * vxy / np.where(vx > 0, vx, 1.0), vy)
    return np.maximum(sse, 0.0)


def _linfit(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    if x.size < 2 or np.ptp(x) == 0:
        return 0.0, float(np.mean(y)) if y.size else 0.0
    a, b = np.polyfit(x, y, 1)
    return float(a), float(b)


def change_points(x: np.ndarray, y: np.ndarray, *, max_splits: int = 8, min_seg: int = 3,
                  min_height_sigma: float = 4.0, merge: float | None = None) -> tuple[list[dict], float, list[dict]]:
    """Steps in a profile by binary segmentation into straight segments.

    A segment is split where two straight lines fit much better than one (gain above a
    BIC-like penalty). Splits closer together than ``merge`` (x units; default 6 % of the range,
    at least 4 samples) are one transition, e.g. the two ends of a tip-broadened step; its
    height is the right segment's line minus the left segment's line at the transition's
    middle, and it is reported when that exceeds ``min_height_sigma`` x the per-sample noise
    (robust, from first differences). A bump narrower than ``merge`` therefore cancels out.
    Returns ``(steps, sigma, segments)``: ``steps`` = ``[{index, x, height}]`` with ``height``
    = right minus left; ``segments`` = the fitted straight pieces ``[{start, end, slope, mean, n}]``."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    xs, ys = x[ok], y[ok]
    n = xs.size
    if n < 2 * min_seg + 1:
        return [], 0.0, []
    d = np.diff(ys)
    sigma = _mad_sigma(d) / math.sqrt(2.0)
    if sigma <= 0:
        sigma = max(1e-12, float(np.std(d)) / math.sqrt(2.0)) if d.size else 1e-12
    pref = [np.concatenate([[0.0], np.cumsum(v)]) for v in (np.ones(n), xs, ys, xs * xs, xs * ys, ys * ys)]

    def sums(a, b):
        return [p[b] - p[a] for p in pref]

    penalty = max(25.0, 6.0 * math.log(n)) * sigma * sigma
    segments = [(0, n)]
    splits: list[int] = []
    for _ in range(max_splits):
        best = None
        for (s, e) in segments:
            if e - s < 2 * min_seg + 1:
                continue
            whole = _linfit_sse(*sums(s, e))
            t = np.arange(s + min_seg, e - min_seg + 1)
            left = _linfit_sse(*[p[t] - p[s] for p in pref])
            right = _linfit_sse(*[p[e] - p[t] for p in pref])
            gain = whole - (left + right)
            k = int(np.argmax(gain))
            if best is None or gain[k] > best[0]:
                best = (float(gain[k]), int(t[k]), (s, e))
        if best is None or best[0] < penalty:
            break
        _, t, (s, e) = best
        segments.remove((s, e))
        segments += [(s, t), (t, e)]
        splits.append(t)
    segs_out = []
    for (s, e) in sorted(segments):
        sl, il = _linfit(xs[s:e], ys[s:e])
        segs_out.append({"start": float(xs[s]), "end": float(xs[e - 1]), "slope": sl,
                         "mean": float(np.mean(ys[s:e])), "n": int(e - s)})
    if merge is None:
        spacing = float(np.median(np.diff(xs))) if n > 1 else 0.0
        merge = max(4.0 * spacing, 0.06 * float(xs[-1] - xs[0]))
    seg_by_start = {a: b for (a, b) in segments}
    seg_by_end = {b: a for (a, b) in segments}
    groups: list[list[int]] = []
    for t in sorted(splits):
        xb = 0.5 * (xs[t - 1] + xs[t])
        if groups and xb - 0.5 * (xs[groups[-1][-1] - 1] + xs[groups[-1][-1]]) <= merge:
            groups[-1].append(t)
        else:
            groups.append([t])
    steps = []
    for grp in groups:
        t0, t1 = grp[0], grp[-1]
        s0, e1 = seg_by_end[t0], seg_by_start[t1]
        sl, il = _linfit(xs[s0:t0], ys[s0:t0])
        sr, ir = _linfit(xs[t1:e1], ys[t1:e1])
        xm = 0.25 * (xs[t0 - 1] + xs[t0] + xs[t1 - 1] + xs[t1])
        h = (sr * xm + ir) - (sl * xm + il)
        if abs(h) >= min_height_sigma * sigma:
            steps.append({"index": int(np.nonzero(ok)[0][t0]), "x": float(xm), "height": float(h),
                          "width": float(0.5 * (xs[t1 - 1] + xs[t1]) - 0.5 * (xs[t0 - 1] + xs[t0]))})
    return steps, float(sigma), segs_out


# ── line_profile ────────────────────────────────────────────────────────────
def _point_to_native(e: FrameEntry, p: Any, where: str) -> tuple[float, float]:
    if not isinstance(p, dict):
        raise ArgError(f"{where} must be an object {{x, y}} (display px) or {{x_nm, y_nm}} (scan nm)")
    check_keys(p, {"x", "y", "x_nm", "y_nm"}, set(), where)
    has_px = p.get("x") is not None or p.get("y") is not None          # null = not given
    has_nm = p.get("x_nm") is not None or p.get("y_nm") is not None
    if has_px == has_nm:
        raise ArgError(f"{where}: give either x and y (display px) or x_nm and y_nm (scan nm)")
    if has_px:
        x = num_arg(p, "x", where)
        y = num_arg(p, "y", where)
        if x is None or y is None:
            raise ArgError(f"{where}: give both x and y")
        num, den = e.scale_pair
        col, row = float(x) * den / num, float(y) * den / num
    else:
        X = num_arg(p, "x_nm", where)
        Y = num_arg(p, "y_nm", where)
        if X is None or Y is None:
            raise ArgError(f"{where}: give both x_nm and y_nm")
        col, row = F.scan_nm_to_native(e.geometry, float(X), float(Y))
    tol = 1e-6
    if not (-tol <= col <= e.nx + tol and -tol <= row <= e.ny + tol):
        W, H = e.display_size
        num, den = e.scale_pair
        # the frame covers nx * k display pixels; a downsampled image is wider (ceil) by up to one pixel
        x_max, y_max = (f"{math.floor(n * num / den * 1000) / 1000:g}" for n in (e.nx, e.ny))
        raise ArgError(f"{where} lies outside {e.fid} (display image {W}x{H}, i.e. 0 <= x <= {x_max}, "
                       f"0 <= y <= {y_max})")
    return float(min(max(col, 0.0), e.nx)), float(min(max(row, 0.0), e.ny))


def _point_report(e: FrameEntry, col: float, row: float) -> dict:
    x, y = _native_to_display(e, col, row)
    X, Y = F.native_to_scan_nm(e.geometry, col, row)
    return {"x_px": round(x, 3), "y_px": round(y, 3), "x_nm": round(float(X), 4), "y_nm": round(float(Y), 4)}


def _scan_angle_deg(e: FrameEntry, dcol: float, drow: float) -> float:
    """Direction (deg, scan frame, from +x toward +y) of a native displacement."""
    dx, dy = _pixel_nm(e)
    a = math.degrees(math.atan2(-drow * dy, dcol * dx)) + float(e.geometry.get("angle_deg") or 0.0)
    return (a + 180.0) % 360.0 - 180.0


def make_line_profile_tool(display_max: int = DISPLAY_MAX_DEFAULT) -> ToolDef:
    schema = {"type": "object", "additionalProperties": False, "required": ["frame", "p1", "p2"],
              "properties": {
                  "frame": _frame_schema(), "p1": _point_schema(), "p2": _point_schema(),
                  "channel": _CHANNEL_SCHEMA, "direction": _DIRECTION_SCHEMA,
                  "width_px": {"type": "number", "minimum": 1, "maximum": int(display_max),
                               "description": "Band width in display pixels averaged across the line. Default 1."},
                  "flatten": _FLATTEN_SCHEMA, "highpass_nm": _HIGHPASS_SCHEMA}}
    desc = (
        "Measure values along a straight line in one archived scan frame without changing the instrument. "
        "p1 and p2 are either {x, y} in display pixels of that frame's full image or {x_nm, y_nm} in "
        "scan-frame nm. The flattened frame (default plane) is interpolated bilinearly at up to 400 evenly "
        "spaced samples, at most one scan pixel apart on a line shorter than 400 scan pixels (spacing_nm "
        "gives the spacing), and averaged across width_px. The reply gives distance_nm from p1 and the values "
        "(values_pm for Z, values_pa for current) per sample; the line's length and direction in the scan "
        "frame; the straight segments of a piecewise-linear fit (from_nm, to_nm, slope, mean); and step "
        "candidates where the segments on either side of a short transition differ by at least 4 noise "
        "sigmas: position_nm along the line, the scan-frame x_nm and y_nm, the height (right side minus left "
        "side, e.g. height_pm) and transition_nm. A bump wider than the transition window appears as a rise "
        "and a fall, and a rough or zigzag step edge that the line crosses more than once appears as several "
        "steps; the height between two terraces is better taken as the difference of the means of the segments "
        "on either side. One plot of the profile is returned and archived as a derived frame (plot.frame). It "
        "measures; it does not decide what a step or a feature is. Keys carry their unit: _nm, _deg, _px, and for channel values _pm (Z), _pa (current), _hz (frequency shift), _v (voltage)."
    )
    return ToolDef(name="line_profile", title="Line profile", description=desc, input_schema=schema,
                   read_only=True, idempotent=False)


@never_raises("line_profile")
def line_profile(archive: Archive, args: dict) -> ToolReply:
    check_keys(args, {"frame", "p1", "p2", "channel", "direction", "width_px", "flatten", "highpass_nm"},
               {"frame", "p1", "p2"}, "arguments")
    e = _image_entry(archive, args)
    ch, direction = channel_direction_args(archive, e, args, "arguments")
    mode, hp = flatten_args(args, "arguments")
    width_px = float(num_arg(args, "width_px", "arguments", default=1, lo=1, hi=archive.display_max))
    c1, r1 = _point_to_native(e, args["p1"], "p1")
    c2, r2 = _point_to_native(e, args["p2"], "p2")
    L = math.hypot(c2 - c1, r2 - r1)
    if L < 1.0:
        raise ArgError("p1 and p2 are less than one scan pixel apart")
    dx, dy = _pixel_nm(e)
    length_nm = math.hypot((c2 - c1) * dx, (r2 - r1) * dy)
    z = _flat_frame(archive, e, ch, direction, mode, hp)
    n = int(min(MAX_PROFILE_SAMPLES, max(2, math.ceil(L) + 1)))
    t = np.linspace(0.0, 1.0, n)
    cs = c1 + t * (c2 - c1) - 0.5
    rs = r1 + t * (r2 - r1) - 0.5
    num, den = e.scale_pair
    wn = width_px * den / num
    n_lines = max(1, int(round(wn)))
    offs = (np.arange(n_lines) - (n_lines - 1) / 2.0) * (wn / n_lines)
    nc, nr = -(r2 - r1) / L, (c2 - c1) / L
    from scipy.ndimage import map_coordinates

    samples = np.stack([map_coordinates(z, [rs + o * nr, cs + o * nc], order=1, mode="nearest")
                        for o in offs])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        vals_si = np.nanmean(samples, axis=0)
    fmt = archive.fmt(e, ch)
    unit = fmt.unit
    vals = vals_si * fmt.factor
    dist = t * length_nm
    spacing = length_nm / (n - 1)
    steps, sigma, segments = change_points(dist, vals,
                                           merge=float(np.clip(0.06 * length_nm, 4 * spacing, max(2.0, 4 * spacing))))
    step_out = []
    for i, s in enumerate(steps, 1):
        f = s["x"] / length_nm
        X, Y = F.native_to_scan_nm(e.geometry, c1 + f * (c2 - c1), r1 + f * (r2 - r1))
        step_out.append({"id": i, "position_nm": round(s["x"], 3), fmt.key("height"): F.round_to(s["height"], fmt.decimals),
                         "transition_nm": round(s["width"], 3), "x_nm": round(float(X), 4), "y_nm": round(float(Y), 4)})

    def draw(ax):
        ax.plot(dist, vals, lw=1.6, color="#1f4e9c")
        for s in step_out:
            ax.axvline(s["position_nm"], color="#d62728", ls="--", lw=1.2)
            ax.annotate(str(s["id"]), (s["position_nm"], 1.0), xycoords=("data", "axes fraction"),
                        xytext=(3, -14), textcoords="offset points", color="#d62728", fontsize=13)
        ax.set_xlabel("distance from p1 (nm)", fontsize=13)
        ax.set_ylabel(f"{ch} ({unit})" if unit else ch, fontsize=13)

    png = _plot_png(max(600, archive.display_max), 400, draw)
    attrs = {"channel": ch, "direction": direction, "flatten": mode, "plot": "line profile",
             "x": "distance from p1 [nm]", "y": f"{ch} [{unit}]"}
    de, block = _register_derived(archive, png, tool="line_profile", sources=[e.fid],
                                  description=f"profile of {ch} {direction} from p1 to p2", label_attrs=attrs)
    body: dict[str, Any] = {
        "instrument_unchanged": True, "frame": e.fid, "channel": ch, "direction": direction, "flatten": mode}
    if mode == "highpass":
        body["highpass_nm"] = hp
    body.update({
        "p1": _point_report(e, c1, r1), "p2": _point_report(e, c2, r2),
        "length_nm": round(length_nm, 4), "direction_deg": round(_scan_angle_deg(e, c2 - c1, r2 - r1), 3),
        "width_px": width_px, "width_nm": round(wn * math.sqrt(dx * dy), 4), "n_samples": n,
        "spacing_nm": round(length_nm / (n - 1), 5), "decimals": fmt.decimals,
        "distance_nm": [round(float(v), 4) for v in dist],
        fmt.key("values"): fmt.values(vals_si),
        "steps": step_out,
        "segments": [{"from_nm": round(g["start"], 3), "to_nm": round(g["end"], 3),
                      fmt.key("slope") + "_per_nm": float(f"{g['slope']:.4g}") + 0.0,
                      fmt.key("mean"): F.round_to(g["mean"], fmt.decimals)}
                     for g in segments],
        fmt.key("noise_per_sample"): F.round_to(sigma, fmt.decimals), "step_min_jump_sigma": 4,
        "plot": {"frame": de.fid},
    })
    return json_reply(body, images=[block], meta={"tool": "line_profile", "derived": [de.fid]})


# ── fft_peaks ───────────────────────────────────────────────────────────────
def _longest_finite_rows(z: np.ndarray) -> np.ndarray:
    ok = np.isfinite(z).any(axis=1)
    best, cur, start, best_start = 0, 0, 0, 0
    for i, v in enumerate(ok):
        if v:
            if cur == 0:
                start = i
            cur += 1
            if cur > best:
                best, best_start = cur, start
        else:
            cur = 0
    return z[best_start:best_start + best]


def spectrum_peaks(z: np.ndarray, dx_nm: float, dy_nm: float, *, n_peaks: int = 6,
                   min_strength: float = 3.0, angle_deg: float = 0.0) -> dict:
    """Peaks of the 2-D power spectrum of ``z`` (oriented array, row 0 = top).

    Returns ``{"peaks": [...], "logmag", "fx", "fy", "k_min", "k_nyq", "shape"}``. Peak
    frequencies are cycles/nm (``k = 1/period``) in the frame's own axes (``ku`` right,
    ``kv`` up) and, rotated by ``angle_deg``, in the scan frame (``kx``, ``ky``)."""
    from scipy.fft import fft2, fftfreq, fftshift, next_fast_len
    from scipy.ndimage import maximum_filter

    z = _longest_finite_rows(np.asarray(z, dtype=float))
    if z.shape[0] < 8 or z.shape[1] < 8:
        raise ValueError("the region needs at least 8 x 8 acquired pixels for a spectrum")
    fin = np.isfinite(z)
    z = np.where(fin, z, np.nanmean(z))
    ny, nx = z.shape
    z = z - F._fit_surface(z, 1)
    win = np.outer(np.hanning(ny), np.hanning(nx))
    zw = (z - z.mean()) * win
    pad = 2 if max(ny, nx) <= 1024 else 1                 # zero-pad x2 (finer peak sampling) when affordable
    Py, Px = next_fast_len(pad * ny), next_fast_len(pad * nx)
    spec = fftshift(np.abs(fft2(zw, s=(Py, Px))))
    logm = np.log10(spec + 1e-30 + spec.max() * 1e-12)
    fx = fftshift(fftfreq(Px, d=dx_nm))
    fy = fftshift(fftfreq(Py, d=dy_nm))
    KU, KYa = np.meshgrid(fx, fy)          # KYa: frequency along the row index (downward)
    KV = -KYa
    kr = np.hypot(KU, KV)
    Lx, Ly = nx * dx_nm, ny * dy_nm
    k_min = 2.0 / min(Lx, Ly)
    dc_lobe = (KU * Lx / 2.0) ** 2 + (KV * Ly / 2.0) ** 2 < 1.0     # Hann main lobe: 2 bins per axis
    k_nyq = 0.5 / max(dx_nm, dy_nm)
    dk = 2.0 * max(1.0 / (Px * dx_nm), 1.0 / (Py * dy_nm))
    bins = np.floor(kr / dk).astype(int)
    order = np.argsort(bins, axis=None, kind="stable")
    flat_bins = bins.ravel()[order]
    flat_vals = logm.ravel()[order]
    edges = np.searchsorted(flat_bins, np.arange(flat_bins.max() + 2))
    nb = flat_bins.max() + 1
    loc = np.zeros(nb)
    scl = np.zeros(nb)
    for b in range(nb):
        seg = flat_vals[edges[b]:edges[b + 1]]
        if seg.size:
            # one-sided statistics: peaks only ever add power, and at small |k| they can fill a
            # large part of their own annulus, which drags a median / MAD up with them
            p5, p30 = np.percentile(seg, [5.0, 30.0])
            loc[b], scl[b] = p30, (p30 - p5) / 1.121
    floor = max(1e-6, float(np.median(scl[scl > 0])) * 0.25) if np.any(scl > 0) else 1e-6
    strength = (logm - loc[bins]) / np.maximum(scl[bins], floor)
    half = (KV > 0) | ((KV == 0) & (KU > 0))
    lm = (logm == maximum_filter(logm, size=5, mode="nearest")) & ~dc_lobe & half & (kr <= k_nyq * 1.0001)
    cand = np.argwhere(lm)
    cand = sorted(cand.tolist(), key=lambda rc: -strength[rc[0], rc[1]])
    peaks = []
    a = math.radians(angle_deg)
    for r, c in cand:
        s = float(strength[r, c])
        if s < min_strength or len(peaks) >= n_peaks:
            break
        dr = dc = 0.0
        if 0 < r < Py - 1:
            l0, l1, l2 = logm[r - 1, c], logm[r, c], logm[r + 1, c]
            den = l0 - 2 * l1 + l2
            dr = float(np.clip(0.5 * (l0 - l2) / den, -0.5, 0.5)) if den < 0 else 0.0
        if 0 < c < Px - 1:
            l0, l1, l2 = logm[r, c - 1], logm[r, c], logm[r, c + 1]
            den = l0 - 2 * l1 + l2
            dc = float(np.clip(0.5 * (l0 - l2) / den, -0.5, 0.5)) if den < 0 else 0.0
        ku = float(np.interp(c + dc, np.arange(Px), fx))
        kv = -float(np.interp(r + dr, np.arange(Py), fy))
        k = math.hypot(ku, kv)
        kx = math.cos(a) * ku - math.sin(a) * kv
        ky = math.sin(a) * ku + math.cos(a) * kv
        ang = math.degrees(math.atan2(ky, kx)) % 180.0
        peaks.append({"k_per_nm": k, "period_nm": 1.0 / k if k > 0 else float("inf"), "angle_deg": ang,
                      "kx_per_nm": kx, "ky_per_nm": ky, "ku": ku, "kv": kv, "strength": s,
                      # sampling acts along the pixel axes: a period finer than 2 pixels folds back to
                      # just below that axis's Nyquist frequency
                      "nyquist_fraction": max(abs(ku) * 2.0 * dx_nm, abs(kv) * 2.0 * dy_nm)})
    return {"peaks": peaks, "logmag": logm, "fx": fx, "fy": fy, "k_min": k_min, "k_nyq": k_nyq,
            "dc_lobe_per_nm": [2.0 / Lx, 2.0 / Ly], "shape": (ny, nx), "padded": (Py, Px), "pad": pad}


def _spectrum_image(res: dict, display_max: int) -> tuple[np.ndarray, float, int]:
    """Square log-magnitude image, +kv up, center = k 0, covering [-K, K]^2. Returns
    ``(rgb, K, size)`` with the image size before marks."""
    from scipy.ndimage import map_coordinates

    peaks = res["peaks"]
    kmax = max([p["k_per_nm"] for p in peaks], default=0.0)
    K = res["k_nyq"] if kmax == 0 else min(res["k_nyq"], max(1.35 * kmax, 3.0 * res["k_min"]))
    fx, fy = res["fx"], res["fy"]
    dfx, dfy = fx[1] - fx[0], fy[1] - fy[0]
    G = int(np.clip(2.0 * K / min(dfx, dfy), 64, display_max))
    g = (np.arange(G) + 0.5) / G * 2.0 * K - K
    ku_grid, kv_grid = np.meshgrid(g, g[::-1])
    cols = (ku_grid - fx[0]) / dfx
    rows = (-kv_grid - fy[0]) / dfy
    img = map_coordinates(res["logmag"], [rows, cols], order=1, mode="nearest")
    lo, hi = (float(v) for v in np.percentile(img, [1.0, 99.9]))
    grey, nan = F.grey_levels(img, lo, hi)
    up = max(1, display_max // G)
    rgb = F.upscale(np.repeat(grey[:, :, None], 3, axis=2), up)
    return rgb, K, G * up


def make_fft_peaks_tool(display_max: int = DISPLAY_MAX_DEFAULT) -> ToolDef:
    schema = {"type": "object", "additionalProperties": False, "required": ["frame"],
              "properties": {
                  "frame": _frame_schema(), "region": region_schema(int(display_max)),
                  "channel": _CHANNEL_SCHEMA, "direction": _DIRECTION_SCHEMA,
                  "n_peaks": {"type": "integer", "minimum": 1, "maximum": 20,
                              "description": "Maximum number of peaks listed. Default 6."},
                  "flatten": _FLATTEN_SCHEMA, "highpass_nm": _HIGHPASS_SCHEMA}}
    desc = (
        "Compute the 2-D Fourier spectrum of one archived scan frame, or of a region of it (display pixels "
        "of the frame's full image), without changing the instrument, and list its strongest peaks. The "
        "region is flattened (default plane over the whole frame, then a plane over the region), "
        "Hann-windowed and zero-padded; the central DC lobe (2/field along each axis) is excluded. Each peak "
        "gives the spatial frequency k = 1/period in cycles per nm (k_per_nm, no 2 pi), the real-space "
        "period_nm, the direction of k in the scan frame (angle_deg, from +x toward +y, 0..180; stripes or "
        "atom rows with this period run perpendicular to it), its components and a strength: log-magnitude "
        "above the spectrum's 30th percentile at the same |k|, in robust units of its lower spread; and "
        "nyquist_fraction, its larger component along the frame's own pixel axes as a fraction of that axis's "
        "Nyquist frequency (0.5 / pixel size), with near_nyquist true from 0.8: such a peak may be an alias of "
        "a period finer than two pixels, and a rescan with smaller pixels tells the two apart. Only one "
        "peak of each +k/-k pair is listed; peaks weaker than 3 are not. One image of the log-magnitude "
        "spectrum with the listed peaks numbered is returned (center = k 0, right = +x and up = +y of the "
        "frame's own axes) and archived as a derived frame (image.frame). The spectrum is measured; whether "
        "a peak is a lattice, a reconstruction or noise is your judgement."
    )
    return ToolDef(name="fft_peaks", title="FFT peaks", description=desc, input_schema=schema,
                   read_only=True, idempotent=False)


@never_raises("fft_peaks")
def fft_peaks(archive: Archive, args: dict) -> ToolReply:
    check_keys(args, {"frame", "region", "channel", "direction", "n_peaks", "flatten", "highpass_nm"},
               {"frame"}, "arguments")
    e = _image_entry(archive, args)
    ch, direction = channel_direction_args(archive, e, args, "arguments")
    mode, hp = flatten_args(args, "arguments")
    n_peaks = int(num_arg(args, "n_peaks", "arguments", default=6, lo=1, hi=20, integer=True))
    region = region_arg(e, args, "arguments")
    box = _native_box(e, region)
    i0, j0, i1, j1 = box
    z = _flat_frame(archive, e, ch, direction, mode, hp)[j0:j1, i0:i1]
    dx, dy = _pixel_nm(e)
    ang = float(e.geometry.get("angle_deg") or 0.0)
    res = spectrum_peaks(z, dx, dy, n_peaks=n_peaks, angle_deg=ang)
    rgb, K, size = _spectrum_image(res, archive.display_max)
    marks = []
    for i, p in enumerate(res["peaks"], 1):
        x = (p["ku"] + K) / (2 * K) * size
        y = (K - p["kv"]) / (2 * K) * size
        if 0 <= x <= size and 0 <= y <= size:
            marks.append((x, y, max(7.0, size * 0.018), str(i)))
    png = _draw_marks(rgb, marks)
    attrs = {"channel": ch, "direction": direction, "region": None if region is None else ",".join(map(str, region)),
             "spectrum": "log magnitude", "k_half_width_per_nm": f"{K:.4g}",
             "axes": "center k=0, right +x, up +y (frame axes)", "marks": f"{len(marks)} numbered peaks"}
    de, block = _register_derived(archive, png, tool="fft_peaks", sources=[e.fid],
                                  description=f"log-magnitude spectrum of {ch} {direction}", label_attrs=attrs)
    ny, nx = res["shape"]
    peaks_out = []
    for i, p in enumerate(res["peaks"], 1):
        peaks_out.append({"id": i, "k_per_nm": round(p["k_per_nm"], 5), "period_nm": round(p["period_nm"], 4),
                          "angle_deg": round(p["angle_deg"], 2), "kx_per_nm": round(p["kx_per_nm"], 5),
                          "ky_per_nm": round(p["ky_per_nm"], 5), "strength": round(p["strength"], 2),
                          "nyquist_fraction": round(p["nyquist_fraction"], 3),
                          "near_nyquist": bool(p["nyquist_fraction"] >= NEAR_NYQUIST_FRACTION)})
    body = {
        "instrument_unchanged": True, "frame": e.fid, "channel": ch,
        "direction": direction, "flatten": mode, "region_px": None if region is None else list(region),
        "native_px": list(box), "analysed_px": [nx, ny],
        "field_nm": [round(nx * dx, 4), round(ny * dy, 4)], "window": "hann", "zero_padding": int(res["pad"]),
        "k_resolution_per_nm": round(1.0 / min(nx * dx, ny * dy), 5),
        "dc_lobe_excluded_per_nm": [round(v, 5) for v in res["dc_lobe_per_nm"]],
        "k_nyquist_per_nm": round(res["k_nyq"], 5), "near_nyquist_from": NEAR_NYQUIST_FRACTION,
        "peaks": peaks_out,
        "image": {"frame": de.fid, "size_px": [size, size], "k_half_width_per_nm": round(K, 5),
                  "per_nm_per_image_px": round(2 * K / size, 6)},
    }
    if not peaks_out:
        body["note"] = "no local maximum reached strength 3"
    return json_reply(body, images=[block], meta={"tool": "fft_peaks", "derived": [de.fid]})


# ── detect_blobs ────────────────────────────────────────────────────────────
def _gauss_model(p: np.ndarray, xx: np.ndarray, yy: np.ndarray) -> np.ndarray:
    A, x0, y0, sa, sb, th, c = p
    ct, st = math.cos(th), math.sin(th)
    u = (xx - x0) * ct + (yy - y0) * st
    v = -(xx - x0) * st + (yy - y0) * ct
    return A * np.exp(-0.5 * ((u / sa) ** 2 + (v / sb) ** 2)) + c


#: Second moment, in units of sigma^2, of a 2-D Gaussian weighted by its excess over half its peak
#: (the weights :func:`_moment_start` uses): sum(u^2 w) / sum(w) with w = g - 1/2 where g > 1/2.
_HALF_MAX_MOMENT = (2.0 * (0.5 - 0.5 * math.log(2.0)) - 0.5 * math.log(2.0) ** 2) / (2.0 * (0.5 - 0.5 * math.log(2.0)))


def _moment_start(xs: np.ndarray, ys: np.ndarray, vs: np.ndarray, sigma0: float, lo: float,
                  hi: float) -> tuple[float, float, float]:
    """Start ``(sa, sb, theta)`` of the Gaussian fit from the intensity moments of the pixels above
    half the peak (``vs`` is normalised to a peak of 1). A circular start leaves the model independent
    of theta, so the first step in theta follows rounding noise and can run to a far orientation;
    starting from the moments' ellipse keeps theta's gradient defined. Falls back to a circle of
    ``sigma0`` when fewer than three pixels lie above half the peak."""
    w = vs - 0.5
    keep = w > 0
    if keep.sum() < 3:
        return sigma0, sigma0, 0.0
    w, x, y = w[keep], xs[keep], ys[keep]
    x0, y0 = float((w * x).sum() / w.sum()), float((w * y).sum() / w.sum())
    cxx = float((w * (x - x0) ** 2).sum() / w.sum()) + 1.0 / 12
    cyy = float((w * (y - y0) ** 2).sum() / w.sum()) + 1.0 / 12
    cxy = float((w * (x - x0) * (y - y0)).sum() / w.sum())
    tr, det = cxx + cyy, cxx * cyy - cxy * cxy
    disc = math.sqrt(max(0.0, tr * tr / 4 - det))
    l1, l2 = tr / 2 + disc, max(1e-6, tr / 2 - disc)
    sa = float(np.clip(math.sqrt(l1 / _HALF_MAX_MOMENT), lo, hi))
    sb = float(np.clip(math.sqrt(l2 / _HALF_MAX_MOMENT), lo, hi))
    return sa, sb, 0.5 * math.atan2(2 * cxy, cxx - cyy)


def _fit_blob(r: np.ndarray, use: np.ndarray, row: int, col: int, sigma0: float, R: int) -> dict | None:
    """Elliptical Gaussian + constant fitted to the residual around ``(row, col)``; ``None``
    when the fit fails or wanders off (the caller then uses moments and says so). The fit starts
    from the intensity moments' ellipse; theta is unbounded (an ellipse repeats every pi, so a bound
    only gives the fit a wall to stop at) and is returned folded into [-pi/2, pi/2)."""
    from scipy.optimize import least_squares

    ny, nx = r.shape
    ya, yb = max(0, row - R), min(ny, row + R + 1)
    xa, xb = max(0, col - R), min(nx, col + R + 1)
    sub = r[ya:yb, xa:xb]
    m = use[ya:yb, xa:xb] & np.isfinite(sub)
    if m.sum() < 9:
        return None
    yy, xx = np.mgrid[ya:yb, xa:xb]
    border = np.concatenate([sub[0, :], sub[-1, :], sub[:, 0], sub[:, -1]])
    c0 = float(np.nanmedian(border)) if np.isfinite(border).any() else 0.0
    A0 = float(r[row, col] - c0)
    if not A0 > 0:
        return None
    xs, ys, vs = xx[m].astype(float), yy[m].astype(float), (sub[m] - c0) / A0
    sa0, sb0, th0 = _moment_start(xs, ys, vs, sigma0, 0.35, 1.9 * R)
    p0 = np.array([1.0, col, row, sa0, sb0, th0, 0.0])
    lo = np.array([0.0, col - R, row - R, 0.3, 0.3, -np.inf, -2.0])
    hi = np.array([4.0, col + R, row + R, 2.0 * R, 2.0 * R, np.inf, 2.0])
    try:
        sol = least_squares(lambda p: _gauss_model(p, xs, ys) - vs, p0, bounds=(lo, hi), max_nfev=400)
    except Exception:  # noqa: BLE001 — a failed fit falls back to moments (reported)
        return None
    if not sol.success:
        return None
    A, x0, y0, sa, sb, th, c = sol.x
    if math.hypot(x0 - col, y0 - row) > max(2.0, 1.5 * sigma0 + 1.0):
        return None
    if sa >= 1.95 * R or sb >= 1.95 * R or A <= 0:
        return None
    th = (float(th) + math.pi / 2) % math.pi - math.pi / 2
    return {"A": float(A * A0), "x0": float(x0), "y0": float(y0), "sa": float(sa), "sb": float(sb),
            "theta": th, "c": float(c * A0 + c0), "fit": "gaussian"}


def _moments_blob(r: np.ndarray, comp: np.ndarray, row: int, col: int) -> dict:
    ys, xs = np.nonzero(comp)
    w = r[ys, xs] - 0.5 * r[row, col]
    w = np.clip(np.where(np.isfinite(w), w, 0.0), 0.0, None)
    if w.sum() <= 0:
        w = np.ones_like(w)
    x0, y0 = float((w * xs).sum() / w.sum()), float((w * ys).sum() / w.sum())
    cxx = float((w * (xs - x0) ** 2).sum() / w.sum()) + 1.0 / 12
    cyy = float((w * (ys - y0) ** 2).sum() / w.sum()) + 1.0 / 12
    cxy = float((w * (xs - x0) * (ys - y0)).sum() / w.sum())
    tr, det = cxx + cyy, cxx * cyy - cxy * cxy
    disc = math.sqrt(max(0.0, tr * tr / 4 - det))
    l1, l2 = tr / 2 + disc, max(1e-6, tr / 2 - disc)
    th = 0.5 * math.atan2(2 * cxy, cxx - cyy)
    return {"A": float(r[row, col]), "x0": x0, "y0": y0, "sa": math.sqrt(l1), "sb": math.sqrt(l2),
            "theta": th, "c": 0.0, "fit": "moments"}


def find_blobs(z: np.ndarray, *, dx_nm: float, dy_nm: float, polarity: str = "bright",
               background_nm: float = 3.0, min_height: float | None = None, min_sep_nm: float | None = None,
               max_n: int = 50, box: tuple[int, int, int, int] | None = None,
               threshold_sigma: float = 6.0) -> dict:
    """Compact features in a flattened oriented array ``z`` (SI units).

    Returns ``{"blobs": [...], "noise", "threshold", "n_candidates", "truncated", ...}``;
    ``noise_method`` says whether ``noise`` is the residual's lower-tail sigma or that capped at
    ``HF_NOISE_CAP`` pixel-to-pixel noises (``noise_high_frequency``); each blob has native centroid
    ``col, row`` (continuous, 0 = left/top edge), ``height``
    (SI, magnitude above (bright) or below (dark) the local background), ``sa, sb`` (Gaussian
    sigmas, px), ``theta`` (rad, image axes, y down) and the fit kind."""
    from scipy.ndimage import gaussian_filter, label, maximum_filter

    sign = -1.0 if polarity == "dark" else 1.0
    zz = sign * np.asarray(z, dtype=float)
    ny, nx = zz.shape
    finite = np.isfinite(zz)
    if finite.sum() < 64:
        raise ValueError("fewer than 64 acquired pixels")
    px = math.sqrt(dx_nm * dy_nm)
    fill = float(np.nanmedian(zz))
    zf = np.where(finite, zz, fill)
    bg = F.median_background(zf, background_nm / px)
    r = zf - bg
    r_nan = np.where(finite, r, np.nan)
    noise_lower = _lower_sigma(r_nan)
    # The lower tail is noise only where the features stand on something flat. When it is a floor or the
    # gaps between touching features, it can be a hundred times the pixel noise; the noise is then capped
    # at HF_NOISE_CAP pixel noises (smoothing only lowers noise, so the smoothed noise is capped there too).
    noise_hf = _hf_sigma(r_nan)
    capped = noise_hf > 0 and noise_lower > HF_NOISE_CAP * noise_hf
    noise = HF_NOISE_CAP * noise_hf if capped else noise_lower
    sm = float(np.clip(0.15 / px, 0.7, 2.0))
    rs = gaussian_filter(np.where(finite, r, 0.0), sm)
    weight = gaussian_filter(finite.astype(float), sm)
    rs = np.where(weight > 0.5, rs / np.maximum(weight, 1e-9), np.nan)
    noise_s = _lower_sigma(rs)
    if noise_s <= 0:
        noise_s = max(noise, 1e-30)
    if capped:
        noise_s = min(noise_s, noise)
    thr = threshold_sigma * noise_s
    sep_nm = 0.3 if min_sep_nm is None else float(min_sep_nm)
    rsep = max(1, int(round(sep_nm / px)))
    rs_f = np.where(np.isfinite(rs), rs, -np.inf)
    peaks = (rs_f == maximum_filter(rs_f, size=2 * rsep + 1, mode="nearest")) & (rs_f > thr)
    inside = np.zeros_like(peaks)
    i0, j0, i1, j1 = box if box is not None else (0, 0, nx, ny)
    inside[j0:j1, i0:i1] = True
    peaks &= inside
    cand = sorted((tuple(rc) for rc in np.argwhere(peaks)), key=lambda rc: -rs_f[rc])
    kept: list[tuple[int, int]] = []
    merge_r = max(3.0, 2.5 / px)                 # nm-scale: maxima within 2.5 nm may belong to one blob
    for (rr, cc) in cand:
        merged = False
        for (kr_, kc) in kept:
            d = math.hypot(rr - kr_, cc - kc)
            if d > merge_r:
                continue
            m = int(math.ceil(d)) + 1
            ts = np.linspace(0, 1, m)
            line = rs_f[np.rint(rr + ts * (kr_ - rr)).astype(int), np.rint(cc + ts * (kc - cc)).astype(int)]
            if line.min() >= 0.8 * rs_f[rr, cc]:
                merged = True
                break
        if not merged:
            kept.append((rr, cc))
    # Voronoi ownership so neighbouring blobs do not pull each other's fits
    owner = np.full(zz.shape, -1, dtype=int)
    if kept:
        from scipy.spatial import cKDTree

        tree = cKDTree(np.array(kept, dtype=float))
        yy, xx = np.mgrid[0:ny, 0:nx]
        _, idx = tree.query(np.column_stack([yy.ravel(), xx.ravel()]))
        owner = idx.reshape(ny, nx)
    blobs = []
    W = 45
    for i, (rr, cc) in enumerate(kept[: max_n * 4]):
        ya, yb, xa, xb = max(0, rr - W), min(ny, rr + W + 1), max(0, cc - W), min(nx, cc + W + 1)
        lab, _ = label(rs_f[ya:yb, xa:xb] > 0.5 * rs_f[rr, cc])
        comp = np.zeros(zz.shape, dtype=bool)
        comp[ya:yb, xa:xb] = lab == lab[rr - ya, cc - xa]
        area = float(comp.sum())
        sigma0 = float(np.clip(math.sqrt(area / (2.0 * math.pi * math.log(2.0))), 0.7, 20.0))
        R = int(np.clip(math.ceil(3.0 * sigma0) + 2, 4, 40))
        use = (owner == i) & finite
        fit = _fit_blob(r_nan, use, rr, cc, sigma0, R)
        if fit is None:
            comp_owned = comp & (owner == i)
            fit = _moments_blob(np.where(finite, r, np.nan), comp_owned if comp_owned.any() else comp, rr, cc)
        fwhm_minor = FWHM_PER_SIGMA * min(fit["sa"], fit["sb"])
        if fwhm_minor < 1.0:
            continue                                  # a single-pixel spike, not a feature
        if fit["A"] < 3.0 * noise:
            continue
        if min_height is not None and fit["A"] < min_height:
            continue
        fit.update({"col": fit["x0"] + 0.5, "row": fit["y0"] + 0.5, "height": fit["A"] * 1.0,
                    "peak_sigma": float(rs_f[rr, cc] / noise_s)})
        blobs.append(fit)
    blobs.sort(key=lambda b: -b["height"])
    truncated = len(blobs) > max_n or len(kept) > max_n * 4
    blobs = blobs[:max_n]
    blobs.sort(key=lambda b: (round(b["row"] / max(1.0, 2.0 * sm)), b["col"]))
    return {"blobs": blobs, "noise": noise, "noise_smoothed": noise_s, "threshold": thr,
            "noise_lower_tail": noise_lower, "noise_high_frequency": noise_hf,
            "noise_method": "high_frequency_cap" if capped else "lower_tail",
            "smoothing_px": sm, "n_candidates": len(kept), "truncated": truncated, "sign": sign}


def _blob_geometry(e: FrameEntry, b: dict) -> dict:
    """FWHM along the frame's x and y (nm), elongation and major-axis angle (scan frame)."""
    dx, dy = _pixel_nm(e)
    sa, sb, th = b["sa"], b["sb"], b["theta"]
    sxx = sa * sa * math.cos(th) ** 2 + sb * sb * math.sin(th) ** 2
    syy = sa * sa * math.sin(th) ** 2 + sb * sb * math.cos(th) ** 2
    major_th = th if sa >= sb else th + math.pi / 2
    ang = (-math.degrees(major_th) + float(e.geometry.get("angle_deg") or 0.0)) % 180.0
    return {"fwhm_x_nm": FWHM_PER_SIGMA * math.sqrt(sxx) * dx, "fwhm_y_nm": FWHM_PER_SIGMA * math.sqrt(syy) * dy,
            "fwhm_major_nm": FWHM_PER_SIGMA * max(sa, sb) * math.sqrt(dx * dy),
            "fwhm_minor_nm": FWHM_PER_SIGMA * min(sa, sb) * math.sqrt(dx * dy),
            "elongation": max(sa, sb) / max(min(sa, sb), 1e-9), "axis_angle_deg": ang}


def make_detect_blobs_tool(display_max: int = DISPLAY_MAX_DEFAULT) -> ToolDef:
    schema = {"type": "object", "additionalProperties": False, "required": ["frame"],
              "properties": {
                  "frame": _frame_schema(), "region": region_schema(int(display_max)),
                  "channel": _CHANNEL_SCHEMA, "direction": _DIRECTION_SCHEMA,
                  "polarity": {"type": "string", "enum": ["bright", "dark"],
                               "description": "Look for protrusions (bright, default) or depressions (dark)."},
                  "min_height": {"type": "number", "minimum": 0,
                                 "description": "Only report blobs at least this high above (dark: this deep "
                                                "below) their local background, in the channel's unit (pm for "
                                                "Z, pA for current)."},
                  "min_sep_nm": {"type": "number", "exclusiveMinimum": 0, "maximum": 100,
                                 "description": "Maxima closer than this are one candidate. Default 0.3 nm."},
                  "background_nm": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000,
                                    "description": "Width of the median background removed first. Default 3 nm; "
                                                   "use a larger value for larger features."},
                  "max_n": {"type": "integer", "minimum": 1, "maximum": MAX_BLOBS,
                            "description": "At most this many blobs (up to 50), the highest. Default 50."}}}
    desc = (
        "Find compact bright (or dark) features in one archived scan frame, or in a region of it (display "
        "pixels of the frame's full image), without changing the instrument. The frame is plane-flattened, a "
        "median background of background_nm is subtracted, the residual is lightly smoothed, and local maxima "
        "above 6 noise sigmas are kept (the noise is measured on the lower part of the residual, which features "
        "do not reach, so a dense lattice does not raise it; where that lower part is a flat floor or deep gaps "
        "between touching features, the noise is capped at 8 times the pixel-to-pixel noise from second "
        "differences, and detection.noise_method says which applied); maxima not separated by a saddle are merged; each "
        "feature is fitted with an elliptical Gaussian. Per blob: id, centroid in display pixels of the full frame "
        "(x_px, y_px) and in scan-frame nm (x_nm, y_nm), height relative to the local background (height_pm "
        "for Z, height_pa for current; negative for dark blobs), FWHM along the frame's x and y and along its "
        "major/minor axes (nm), elongation (major/minor), the major axis angle (axis_angle_deg, scan frame, "
        "from +x toward +y; null when the elongation is below 1.1, where noise decides the axis) and whether "
        "the centroid comes from the Gaussian fit or from intensity moments "
        "(fit). Blobs are numbered in reading order (top to bottom, left to right). One image of the frame "
        "or region with numbered rings is returned and archived as a derived frame (image.frame); the marks "
        "exist only on that image. It counts and locates; it does not identify species or decide what is an "
        "atom. Keys carry their unit: _nm, _deg, _px, and for channel values _pm (Z), _pa (current), _hz (frequency shift), _v (voltage)."
    )
    return ToolDef(name="detect_blobs", title="Detect blobs", description=desc, input_schema=schema,
                   read_only=True, idempotent=False)


@never_raises("detect_blobs")
def detect_blobs(archive: Archive, args: dict) -> ToolReply:
    check_keys(args, {"frame", "region", "channel", "direction", "polarity", "min_height", "min_sep_nm",
                      "background_nm", "max_n"}, {"frame"}, "arguments")
    e = _image_entry(archive, args)
    ch, direction = channel_direction_args(archive, e, args, "arguments")
    polarity = str_arg(args, "polarity", "arguments", default="bright", choices=("bright", "dark"))
    fmt = archive.fmt(e, ch)
    factor = fmt.factor
    mh = num_arg(args, "min_height", "arguments", lo=0)
    sep = num_arg(args, "min_sep_nm", "arguments", lo=0, hi=100, lo_exclusive=True)
    bgnm = float(num_arg(args, "background_nm", "arguments", default=3.0, lo=0, hi=1000, lo_exclusive=True))
    max_n = int(num_arg(args, "max_n", "arguments", default=50, lo=1, hi=MAX_BLOBS, integer=True))
    region = region_arg(e, args, "arguments")
    box = _native_box(e, region)
    dx, dy = _pixel_nm(e)
    if bgnm < 3.0 * math.sqrt(dx * dy):
        raise ArgError(f"background_nm must be at least 3 scan pixels ({3 * math.sqrt(dx * dy):.3g} nm here)")
    z = _flat_frame(archive, e, ch, direction, "plane", F.HIGHPASS_NM_DEFAULT)
    res = find_blobs(z, dx_nm=dx, dy_nm=dy, polarity=polarity, background_nm=bgnm,
                     min_height=None if mh is None else float(mh) / factor, min_sep_nm=sep, max_n=max_n, box=box)
    rgb, view, _, _ = _render_rgb(z, e, box, archive.display_max)
    blobs_out, marks = [], []
    for i, b in enumerate(res["blobs"], 1):
        g = _blob_geometry(e, b)
        xd, yd = _native_to_display(e, b["col"], b["row"])
        X, Y = F.native_to_scan_nm(e.geometry, b["col"], b["row"])
        blobs_out.append({
            "id": i, "x_px": round(xd, 2), "y_px": round(yd, 2), "x_nm": round(float(X), 4),
            "y_nm": round(float(Y), 4), fmt.key("height"): fmt.value(res["sign"] * b["height"]),
            "fwhm_x_nm": round(g["fwhm_x_nm"], 3), "fwhm_y_nm": round(g["fwhm_y_nm"], 3),
            "fwhm_major_nm": round(g["fwhm_major_nm"], 3), "fwhm_minor_nm": round(g["fwhm_minor_nm"], 3),
            "elongation": round(g["elongation"], 2),
            "axis_angle_deg": (round(g["axis_angle_deg"], 1) if round(g["elongation"], 2) >= AXIS_MIN_ELONGATION
                               else None),
            "fit": b["fit"]})
        ix, iy = _image_xy(view, b["col"], b["row"])
        rad = max(9.0, 0.65 * FWHM_PER_SIGMA * max(b["sa"], b["sb"]) * view.up / view.down + 6.0)
        marks.append((ix, iy, rad, str(i)))
    png = _draw_marks(rgb, marks)
    x, y, w, h = view.region_px
    attrs = {"channel": ch, "direction": direction, "flatten": "plane", "region": f"{x},{y},{w},{h}",
             "magnification": f"{view.magnification:.4g}", "marks": f"{len(marks)} numbered rings"}
    de, block = _register_derived(archive, png, tool="detect_blobs", sources=[e.fid],
                                  description=f"{polarity} blobs in {ch} {direction}", label_attrs=attrs)
    body = {
        "instrument_unchanged": True, "frame": e.fid, "channel": ch,
        "direction": direction, "polarity": polarity,
        "region_px": [x, y, w, h], "n_blobs": len(blobs_out), "truncated_to_max_n": bool(res["truncated"]),
        "detection": {"background_nm": bgnm, "smoothing_px": round(res["smoothing_px"], 2),
                      fmt.key("noise"): fmt.value(res["noise"]), "noise_method": res["noise_method"],
                      fmt.key("noise_lower_tail"): fmt.value(res["noise_lower_tail"]),
                      fmt.key("noise_high_frequency"): fmt.value(res["noise_high_frequency"]),
                      fmt.key("threshold_smoothed"): fmt.value(res["threshold"]),
                      fmt.key("min_height"): mh, "min_sep_nm": 0.3 if sep is None else sep},
        "blobs": blobs_out,
        "image": {"frame": de.fid, "size_px": list(view.image_size),
                  "image_px_per_display_px": round(view.magnification, 4)},
    }
    return json_reply(body, images=[block], meta={"tool": "detect_blobs", "derived": [de.fid]})


# ── diff ────────────────────────────────────────────────────────────────────
def _ncc_surface(a: np.ndarray, ma: np.ndarray, b: np.ndarray, mb: np.ndarray, lim: int,
                 min_overlap: float) -> np.ndarray:
    """Normalised cross-correlation ``NCC[dy+lim, dx+lim]`` of ``b(p)`` with ``a(p - d)`` over
    the valid overlap, for integer shifts ``|d| <= lim`` (zero padded, no wrap-around)."""
    from scipy.signal import fftconvolve

    ny, nx = a.shape
    af, bf = a[::-1, ::-1], ma[::-1, ::-1]
    C = fftconvolve(b, af, mode="full")
    EA = fftconvolve(mb, (a * a)[::-1, ::-1], mode="full")
    EB = fftconvolve(b * b, bf, mode="full")
    NO = fftconvolve(mb, bf, mode="full")
    cy, cx = ny - 1, nx - 1
    sl = (slice(cy - lim, cy + lim + 1), slice(cx - lim, cx + lim + 1))
    den = np.sqrt(np.clip(EA[sl], 0, None) * np.clip(EB[sl], 0, None))
    ncc = np.where(den > 0, C[sl] / np.where(den > 0, den, 1.0), 0.0)
    ncc[NO[sl] < min_overlap] = -1.0
    return ncc


def _shift_image(a: np.ndarray, dy: float, dx: float, order: int = 1) -> np.ndarray:
    """``out(p) = a(p - d)``; NaN where the source is outside or NaN."""
    from scipy.ndimage import shift

    fin = np.isfinite(a)
    filled = np.where(fin, a, 0.0)
    out = shift(filled, (dy, dx), order=order, mode="constant", cval=0.0)
    m = shift(fin.astype(float), (dy, dx), order=1, mode="constant", cval=0.0)
    return np.where(m > 0.999, out, np.nan)


def register(a: np.ndarray, b: np.ndarray, *, max_shift_frac: float = 0.4) -> dict:
    """Shift ``d = (dx, dy)`` (px, x right, y down) with ``b(p) ~ a(p - d)``: the content moved by
    ``+d`` from ``a`` to ``b``. Sub-pixel by a parabolic estimate refined on a cubic-shifted
    NCC. ``confidence`` = main NCC peak / second-highest local maximum; ``second_dx, second_dy`` is
    the whole-pixel shift of that alternative alignment, in the same convention as ``d``."""
    from scipy.optimize import minimize

    ma = np.isfinite(a).astype(float)
    mb = np.isfinite(b).astype(float)
    a0 = np.where(ma > 0, a - np.nanmean(a), 0.0)
    b0 = np.where(mb > 0, b - np.nanmean(b), 0.0)
    ny, nx = a.shape
    lim = max(1, int(max_shift_frac * min(ny, nx)))
    ncc = _ncc_surface(a0, ma, b0, mb, lim, 0.3 * min(ma.sum(), mb.sum()))
    k = np.unravel_index(int(np.argmax(ncc)), ncc.shape)
    peak = float(ncc[k])

    def parab(axis: int) -> float:
        idx = list(k)
        if not 0 < k[axis] < ncc.shape[axis] - 1:
            return 0.0
        idx[axis] = k[axis] - 1
        m1 = float(ncc[tuple(idx)])
        idx[axis] = k[axis] + 1
        p1 = float(ncc[tuple(idx)])
        curv = m1 - 2 * peak + p1
        return float(np.clip(0.5 * (m1 - p1) / curv, -0.5, 0.5)) if curv < 0 else 0.0

    dy0, dx0 = k[0] - lim + parab(0), k[1] - lim + parab(1)
    # the best alternative alignment: the highest correlation farther from the peak than twice
    # the image's own correlation length (a ridge or a lattice gives a high one: ambiguous)
    ell = _corr_length(a0, ma, min(lim, 32))
    excl = float(np.clip(2.0 * ell, 2.0, max(2.0, lim / 2.0)))
    yy, xx = np.mgrid[0:ncc.shape[0], 0:ncc.shape[1]]
    far = np.hypot(yy - k[0], xx - k[1]) > excl
    alt = np.where(far & (ncc > -1.0), ncc, -np.inf)
    second, second_d = float("nan"), None
    if np.isfinite(alt).any():
        k2 = np.unravel_index(int(np.argmax(alt)), alt.shape)
        second, second_d = float(alt[k2]), (int(k2[1] - lim), int(k2[0] - lim))

    from scipy.ndimage import binary_erosion, shift as nd_shift, spline_filter

    # refine on a central window of at most REFINE_PX^2 (the shift is global; cost stays bounded)
    wy, wx = min(ny, REFINE_PX), min(nx, REFINE_PX)
    y0w, x0w = (ny - wy) // 2, (nx - wx) // 2
    win = (slice(y0w, y0w + wy), slice(x0w, x0w + wx))
    coeffs = spline_filter(a0[win], order=3)
    # One overlap for the whole refinement: the pixels of b where the shifted a is valid for every shift
    # within REFINE_STEP_PX of the start (the farthest the refinement may move), i.e. the start's
    # overlap eroded by that many pixels. An overlap recomputed for each shift gains or loses a whole
    # row or column whenever a shift component crosses an integer; the objective then jumps there
    # and Nelder-Mead, with weak correlation, stops on the jump (the shift snapped to an integer).
    valid0 = nd_shift(ma[win], (dy0, dx0), order=1, mode="constant", cval=0.0) > 0.999
    side = 2 * REFINE_STEP_PX + 1
    ok = binary_erosion(valid0, structure=np.ones((side, side), dtype=bool), border_value=0) & (mb[win] > 0)
    v = b0[win][ok]
    v = v - v.mean() if v.size else v
    vv = float((v * v).sum())

    def neg_ncc(d):
        sa = nd_shift(coeffs, (d[0], d[1]), order=3, mode="constant", cval=0.0, prefilter=False)[ok]
        u = sa - sa.mean()
        den = math.sqrt(float((u * u).sum()) * vv)
        return -float((u * v).sum() / den) if den > 0 else 1.0

    dy, dx = dy0, dx0
    refined = False
    if int(ok.sum()) >= 16:
        try:
            f0 = neg_ncc(np.array([dy0, dx0]))
            sol = minimize(neg_ncc, np.array([dy0, dx0]), method="Nelder-Mead",
                           options={"xatol": 2e-3, "fatol": 1e-9, "maxiter": 120,
                                    "initial_simplex": np.array([[dy0, dx0], [dy0 + 0.3, dx0], [dy0, dx0 + 0.3]])})
            ry, rx = float(sol.x[0]), float(sol.x[1])
            if (float(sol.fun) <= f0 and abs(ry - dy0) <= REFINE_STEP_PX
                    and abs(rx - dx0) <= REFINE_STEP_PX):
                dy, dx, refined = ry, rx, True
        except Exception:  # noqa: BLE001 — keep the parabolic estimate
            pass
    conf = peak / second if second and second > 0 else float("inf")
    at_edge = abs(k[0] - lim) >= lim or abs(k[1] - lim) >= lim
    return {"dx": float(dx), "dy": float(dy), "ncc_peak": peak, "second_peak": second,
            "second_dx": None if second_d is None else second_d[0],
            "second_dy": None if second_d is None else second_d[1],
            "confidence": conf, "limit_px": lim, "at_search_edge": bool(at_edge), "refined": refined,
            "exclusion_px": excl}


def _corr_length(a0: np.ndarray, ma: np.ndarray, lim: int) -> float:
    """Radius (px) at which the image's normalized autocorrelation first falls below 0.5."""
    acf = _ncc_surface(a0, ma, a0, ma, max(1, lim), 0.3 * ma.sum())
    yy, xx = np.mgrid[-lim:lim + 1, -lim:lim + 1]
    rr = np.rint(np.hypot(yy, xx)).astype(int)
    for r in range(1, lim + 1):
        ring = acf[rr == r]
        if ring.size and float(ring.mean()) < 0.5:
            return float(r)
    return float(lim)


def _same_geometry(ga: dict, gb: dict) -> bool:
    for k in ("cx_nm", "cy_nm", "w_nm", "h_nm", "angle_deg"):
        if abs(float(ga.get(k) or 0) - float(gb.get(k) or 0)) > 1e-9 * max(1.0, abs(float(ga.get(k) or 0))):
            return False
    return int(ga["nx"]) == int(gb["nx"]) and int(ga["ny"]) == int(gb["ny"])


def _clip_polygon(subject: list[tuple[float, float]], box: tuple[float, float, float, float]) -> list:
    """Sutherland-Hodgman clip of a polygon by an axis-aligned box (u0, v0, u1, v1)."""
    u0, v0, u1, v1 = box

    def clip(poly, inside, cross):
        out = []
        for i, cur in enumerate(poly):
            prev = poly[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
        return out

    def cx(val, axis):
        def f(p, q):
            t = (val - p[axis]) / (q[axis] - p[axis])
            return (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))
        return f

    poly = subject
    for inside, cross in ((lambda p: p[0] >= u0, cx(u0, 0)), (lambda p: p[0] <= u1, cx(u1, 0)),
                          (lambda p: p[1] >= v0, cx(v0, 1)), (lambda p: p[1] <= v1, cx(v1, 1))):
        if not poly:
            break
        poly = clip(poly, inside, cross)
    return poly


def _uv_of(g: dict, X: Any, Y: Any) -> tuple[Any, Any]:
    a = math.radians(float(g.get("angle_deg") or 0.0))
    c, s = math.cos(a), math.sin(a)
    dX, dY = np.asarray(X, float) - g["cx_nm"], np.asarray(Y, float) - g["cy_nm"]
    return c * dX + s * dY, -s * dX + c * dY


def _common_grid(ea: FrameEntry, eb: FrameEntry, za: np.ndarray, zb: np.ndarray,
                 box_a: tuple[int, int, int, int]) -> tuple[np.ndarray, np.ndarray, dict, float]:
    """Resample both frames onto a grid in frame a's axes covering their overlap (within
    ``box_a``), spacing = the coarser pixel size. Returns ``(A, B, grid_geometry, spacing)``."""
    from scipy.ndimage import map_coordinates

    ga, gb = ea.geometry, eb.geometry
    s = max(*_pixel_nm(ea), *_pixel_nm(eb))
    i0, j0, i1, j1 = box_a
    ua0 = -ga["w_nm"] / 2 + i0 * ga["w_nm"] / ga["nx"]
    ua1 = -ga["w_nm"] / 2 + i1 * ga["w_nm"] / ga["nx"]
    va1 = ga["h_nm"] / 2 - j0 * ga["h_nm"] / ga["ny"]
    va0 = ga["h_nm"] / 2 - j1 * ga["h_nm"] / ga["ny"]
    corners = [F.native_to_scan_nm(gb, c, r) for c, r in ((0, 0), (gb["nx"], 0), (gb["nx"], gb["ny"]), (0, gb["ny"]))]
    quad = [tuple(float(t) for t in _uv_of(ga, X, Y)) for X, Y in corners]
    poly = _clip_polygon(quad, (ua0, va0, ua1, va1))
    if len(poly) < 3:
        raise ArgError(f"{ea.fid} and {eb.fid} do not overlap" + (" inside the region" if box_a != (0, 0, ea.nx, ea.ny) else ""))
    us = [p[0] for p in poly]
    vs = [p[1] for p in poly]
    gw, gh = int(math.floor((max(us) - min(us)) / s)), int(math.floor((max(vs) - min(vs)) / s))
    if gw < 16 or gh < 16:
        raise ArgError(f"the overlap of {ea.fid} and {eb.fid} is only {gw}x{gh} pixels of {s:.3g} nm")
    u = min(us) + (np.arange(gw) + 0.5) * s
    v = max(vs) - (np.arange(gh) + 0.5) * s
    UU, VV = np.meshgrid(u, v)
    a_ang = math.radians(float(ga.get("angle_deg") or 0.0))
    X = ga["cx_nm"] + math.cos(a_ang) * UU - math.sin(a_ang) * VV
    Y = ga["cy_nm"] + math.sin(a_ang) * UU + math.cos(a_ang) * VV

    def sample(e: FrameEntry, z: np.ndarray) -> np.ndarray:
        col, row = F.scan_nm_to_native(e.geometry, X, Y)
        col, row = np.asarray(col) - 0.5, np.asarray(row) - 0.5
        out = map_coordinates(np.where(np.isfinite(z), z, 0.0), [row, col], order=1, mode="constant", cval=0.0)
        m = map_coordinates(np.isfinite(z).astype(float), [row, col], order=1, mode="constant", cval=0.0)
        inside = (col >= -0.5) & (col <= e.nx - 0.5) & (row >= -0.5) & (row <= e.ny - 0.5)
        return np.where((m > 0.999) & inside, out, np.nan)

    uc = min(us) + gw * s / 2
    vc = max(vs) - gh * s / 2
    grid = {"cx_nm": float(ga["cx_nm"] + math.cos(a_ang) * uc - math.sin(a_ang) * vc),
            "cy_nm": float(ga["cy_nm"] + math.sin(a_ang) * uc + math.cos(a_ang) * vc),
            "w_nm": gw * s, "h_nm": gh * s, "angle_deg": float(ga.get("angle_deg") or 0.0), "nx": gw, "ny": gh}
    return sample(ea, za), sample(eb, zb), grid, s


def _lowpass(z: np.ndarray, s_nm: float) -> np.ndarray:
    """Light NaN-aware smoothing (0.5 nm, 0.7..3 px) so registration follows features, not pixel noise."""
    from scipy.ndimage import gaussian_filter

    fin = np.isfinite(z)
    sig = float(np.clip(0.5 / s_nm, 0.7, 3.0))
    num = gaussian_filter(np.where(fin, z, 0.0), sig)
    den = gaussian_filter(fin.astype(float), sig)
    return np.where(fin & (den > 0.5), num / np.maximum(den, 1e-9), np.nan)


def _bandpass(z: np.ndarray, s_nm: float) -> np.ndarray:
    from scipy.ndimage import gaussian_filter

    fin = np.isfinite(z)
    zf = np.where(fin, z, np.nanmean(z))
    hp = zf - gaussian_filter(zf, max(8.0, 2.0 / s_nm))
    lp = gaussian_filter(hp, float(np.clip(0.5 / s_nm, 0.7, 3.0)))
    return np.where(fin, lp, np.nan)


def _diverging_rgb(d: np.ndarray, L: float) -> np.ndarray:
    t = np.clip(np.where(np.isfinite(d), d / L, 0.0), -1.0, 1.0)
    rgb = np.empty(d.shape + (3,), dtype=float)
    pos, neg = np.clip(t, 0, 1), np.clip(-t, 0, 1)
    rgb[..., 0] = 255 * (1 - neg * 0.85)
    rgb[..., 1] = 255 * (1 - 0.85 * (pos + neg))
    rgb[..., 2] = 255 * (1 - pos * 0.85)
    out = np.rint(rgb).astype(np.uint8)
    out[~np.isfinite(d)] = (64, 64, 64)
    return out


def make_diff_tool(display_max: int = DISPLAY_MAX_DEFAULT) -> ToolDef:
    schema = {"type": "object", "additionalProperties": False, "required": ["frame_a", "frame_b"],
              "properties": {
                  "frame_a": _frame_schema("The earlier (reference) scan frame id."),
                  "frame_b": _frame_schema("The later scan frame id."),
                  "region": region_schema(int(display_max)),
                  "channel": _CHANNEL_SCHEMA, "direction": _DIRECTION_SCHEMA,
                  "flatten": {"type": "string", "enum": list(F.FLATTEN_MODES),
                              "description": "Background removed from both frames before registering and "
                                             "subtracting. Default highpass (median background of highpass_nm), "
                                             "so steps and the bow of piezo creep do not dominate the "
                                             "correlation; use plane to compare large features."},
                  "highpass_nm": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000,
                                  "description": "Background width in nm for flatten='highpass'. Default 3."},
                  "align": {"type": "boolean", "description": "Register b to a before subtracting. Default true."}}}
    desc = (
        "Compare two archived scan frames without changing the instrument. Both are flattened the same way "
        "(default highpass: a median background of highpass_nm = 3 nm is removed, so steps and the bow of "
        "piezo creep do not dominate; plane keeps large features); frames "
        "with different geometry are resampled onto their overlap in scan-frame nm (at the coarser pixel "
        "size), and region (display pixels of frame_a) restricts the comparison. With align=true (default) "
        "the content of frame_b is registered to frame_a by sub-pixel normalized cross-correlation: "
        "registration.shift_nm is the displacement of the image content from frame_a to frame_b in "
        "scan-frame nm (shift_px in grid pixels), with the correlation peak and confidence = main peak / the "
        "best correlation farther away than twice the image's correlation length (close to 1 means "
        "ambiguous, e.g. a bare periodic lattice or parallel stripes; peak_at_search_limit means the shift "
        "was not found); second_peak_shift_nm (second_peak_shift_px, whole grid pixels) is where that best "
        "alternative lies, a displacement like shift_nm: on a repeating pattern the two differ by one "
        "repeat. The aligned difference (b minus shifted a, median offset and plane removed) is "
        "returned as a derived image (red = higher in b, blue = lower in b, white = unchanged, gray = outside "
        "the overlap) and archived with its values (difference.frame), so read_values and inspect work on "
        "it. Changed regions above 5 robust noise sigmas are numbered with centroid (x_nm, y_nm), sign, "
        "area_nm2 and peak change (peak_change_pm for Z). It measures change; it does not say why something "
        "changed. Keys carry their unit: _nm, _deg, _px, and for channel values _pm (Z), _pa (current), _hz (frequency shift), _v (voltage)."
    )
    return ToolDef(name="diff", title="Difference of two frames", description=desc, input_schema=schema,
                   read_only=True, idempotent=False)


@never_raises("diff")
def diff(archive: Archive, args: dict) -> ToolReply:
    from scipy.ndimage import label as nd_label

    check_keys(args, {"frame_a", "frame_b", "region", "channel", "direction", "align", "flatten", "highpass_nm"},
               {"frame_a", "frame_b"}, "arguments")
    ea = _image_entry(archive, args, "frame_a")
    eb = _image_entry(archive, args, "frame_b")
    if ea.kind == "m" or eb.kind == "m":
        raise ArgError("diff compares scan frames (kinds s or p)")
    ch, direction = channel_direction_args(archive, ea, args, "arguments")
    try:
        ch_b = F.resolve_channel(ch, list(eb.channels), eb.units)
    except KeyError as exc:
        raise ArgError(f"frame_b: {exc.args[0]}") from None
    if direction not in eb.channel_directions(ch_b):
        raise ArgError(f"frame_b {eb.fid} channel {ch_b!r} has only {eb.channel_directions(ch_b)}")
    align = bool_arg(args, "align", "arguments", default=True)
    mode, hp = flatten_args(args, "arguments", default="highpass")
    region = region_arg(ea, args, "arguments")
    box = _native_box(ea, region)
    za = _flat_frame(archive, ea, ch, direction, mode, hp)
    zb = _flat_frame(archive, eb, ch_b, direction, mode, hp)
    same = _same_geometry(ea.geometry, eb.geometry)
    if same:
        i0, j0, i1, j1 = box
        A, B = za[j0:j1, i0:i1], zb[j0:j1, i0:i1]
        sx, sy = _pixel_nm(ea)
        g = ea.geometry
        cc0, rr0 = (i0 + i1) / 2.0, (j0 + j1) / 2.0
        Xc, Yc = F.native_to_scan_nm(g, cc0, rr0)
        grid = {"cx_nm": float(Xc), "cy_nm": float(Yc), "w_nm": (i1 - i0) * sx, "h_nm": (j1 - j0) * sy,
                "angle_deg": float(g.get("angle_deg") or 0.0), "nx": i1 - i0, "ny": j1 - j0}
        if min(A.shape) < 16:
            raise ArgError("the compared region must be at least 16 x 16 scan pixels")
    else:
        A, B, grid, s = _common_grid(ea, eb, za, zb, box)
        sx = sy = s
    reg = None
    dx_px = dy_px = 0.0
    if align:
        s_nm = math.sqrt(sx * sy)
        prep = _lowpass if mode == "highpass" else _bandpass       # highpass already removed the background
        reg = register(prep(A, s_nm), prep(B, s_nm))
        dx_px, dy_px = reg["dx"], reg["dy"]
    A_sh = _shift_image(A, dy_px, dx_px, order=1) if align else A
    D = B - A_sh
    if np.isfinite(D).sum() < 64:
        raise ArgError("too few pixels overlap after alignment to form a difference")
    offset = float(np.nanmedian(D))
    D = D - offset
    D = D - F._fit_surface(D, 1)
    sigma = _mad_sigma(D)
    fin_a = A[np.isfinite(A)]
    span_a = float(np.percentile(fin_a, 99.5) - np.percentile(fin_a, 0.5)) if fin_a.size else 0.0
    thr = max(5.0 * sigma, 1e-6 * span_a, 1e-30)     # identical frames: no "changes" from rounding
    fmt = archive.fmt(ea, ch)
    factor, unit = fmt.factor, fmt.unit
    min_area = max(4, int(math.ceil((0.2 / math.sqrt(sx * sy)) ** 2)))
    regions = []
    for sign in (1.0, -1.0):
        lab, n = nd_label(np.nan_to_num(sign * D, nan=0.0) > thr, structure=np.ones((3, 3)))
        for k in range(1, n + 1):
            m = lab == k
            npx = int(m.sum())
            if npx < min_area:
                continue
            ys, xs = np.nonzero(m)
            w = np.abs(D[ys, xs])
            c, r = float((w * (xs + 0.5)).sum() / w.sum()), float((w * (ys + 0.5)).sum() / w.sum())
            X, Y = F.native_to_scan_nm(grid, c, r)
            peak = float(sign * np.max(w))
            regions.append({"sign": "+" if sign > 0 else "-", "x_nm": round(float(X), 4), "y_nm": round(float(Y), 4),
                            "area_nm2": round(npx * sx * sy, 4), fmt.key("peak_change"): fmt.value(peak),
                            "_c": c, "_r": r, "_score": abs(peak) * npx, "_n": npx})
    regions.sort(key=lambda t: -t["_score"])
    n_total = len(regions)
    regions = regions[:MAX_REGIONS]
    # derived map with values
    num, den = F.scale_pair(grid["nx"], grid["ny"], archive.display_max)
    fin = np.abs(D[np.isfinite(D)])
    L = max(1.5 * thr, float(np.percentile(fin, 99.5)) if fin.size else 1.0, 1e-30)
    view = F.plan_view(grid["nx"], grid["ny"], num, den, None, archive.display_max)
    small = D if view.down == 1 else F.block_mean(D, view.down)
    rgb = F.upscale(_diverging_rgb(small, L), view.up)
    marks = []
    for i, rg in enumerate(regions, 1):
        rg["id"] = i
        ix, iy = rg["_c"] * view.up / view.down, rg["_r"] * view.up / view.down
        rad = max(7.0, math.sqrt(rg["_n"] / math.pi) * view.up / view.down + 4.0)
        marks.append((ix, iy, rad, str(i)))
    png = _draw_marks(rgb, marks, colour=(20, 20, 20))
    attrs = {"channel": ch, "direction": direction, "map": "b minus aligned a",
             "color": f"red +, blue -, white 0, full color at +-{F.fmt_num(L * factor, fmt.decimals)} {unit}",
             "scale": F.scale_text(num, den), "marks": f"{len(marks)} numbered regions"}
    de, block = _register_derived(archive, png, tool="diff", sources=[ea.fid, eb.fid],
                                  description=f"{eb.fid} minus aligned {ea.fid} ({ch} {direction})",
                                  label_attrs=attrs, arrays={"diff/forward": D}, geometry=grid,
                                  units={"diff": ea.units.get(ch, "")})
    body: dict[str, Any] = {
        "instrument_unchanged": True, "frame_a": ea.fid, "frame_b": eb.fid,
        "channel": ch, "direction": direction, "flatten": mode}
    if mode == "highpass":
        body["highpass_nm"] = hp
    body.update({
        "same_geometry": same,
        "grid": {"nm_per_px": [round(sx, 6), round(sy, 6)], "size_px": [grid["nx"], grid["ny"]],
                 "field_nm": [round(grid["w_nm"], 4), round(grid["h_nm"], 4)],
                 "center_nm": [round(grid["cx_nm"], 4), round(grid["cy_nm"], 4)],
                 "resampled": not same},
        "align": align,
    })
    if reg is not None:
        a_rad = math.radians(float(grid["angle_deg"]))

        def scan_nm(dxp: float, dyp: float) -> list[float]:
            du, dv = dxp * sx, -dyp * sy
            return [round(math.cos(a_rad) * du - math.sin(a_rad) * dv, 4),
                    round(math.sin(a_rad) * du + math.cos(a_rad) * dv, 4)]

        conf = reg["confidence"]
        second_px = None if reg["second_dx"] is None else [reg["second_dx"], reg["second_dy"]]
        body["registration"] = {
            "shift_nm": scan_nm(dx_px, dy_px), "shift_px": [round(dx_px, 3), round(dy_px, 3)],
            "ncc_peak": round(reg["ncc_peak"], 4),
            "second_peak": None if not math.isfinite(reg["second_peak"]) else round(reg["second_peak"], 4),
            "second_peak_shift_nm": None if second_px is None else scan_nm(*second_px),
            "second_peak_shift_px": second_px,
            "second_peak_min_distance_px": round(reg["exclusion_px"], 1),
            "confidence": None if not math.isfinite(conf) else round(conf, 3),
            "search_limit_px": reg["limit_px"], "peak_at_search_limit": reg["at_search_edge"]}
    body["difference"] = {
        "frame": de.fid, fmt.key("offset_removed"): fmt.value(offset), "plane_removed": True,
        fmt.key("noise"): fmt.value(sigma), fmt.key("threshold"): fmt.value(thr),
        "n_regions": n_total, "regions": [{k: v for k, v in rg.items() if not k.startswith("_")}
                                         for rg in regions]}
    if n_total > len(regions):
        body["difference"]["regions_listed"] = len(regions)
    return json_reply(body, images=[block], meta={"tool": "diff", "derived": [de.fid]})


# ── stats ───────────────────────────────────────────────────────────────────
def make_stats_tool(display_max: int = DISPLAY_MAX_DEFAULT) -> ToolDef:
    schema = {"type": "object", "additionalProperties": False, "required": ["frame"],
              "properties": {"frame": _frame_schema(), "region": region_schema(int(display_max)),
                             "channel": _CHANNEL_SCHEMA, "direction": _DIRECTION_SCHEMA}}
    desc = (
        "Summary statistics of one archived scan frame, or of a region of it (display pixels of the frame's "
        "full image), without changing the instrument: mean, standard deviation, min, max and percentiles of "
        "the raw values; RMS roughness, mean absolute deviation and peak-to-valley after subtracting a plane "
        "fitted to the region; the correlation and RMS difference between the forward and backward passes "
        "(both plane-flattened) when both exist; a line-noise index (median absolute offset between "
        "consecutive row medians divided by the plane-flattened standard deviation); and the fraction of "
        "rows acquired. Keys carry their unit: _nm, _deg, _px, and for channel values _pm (Z), _pa (current), _hz (frequency shift), _v (voltage)."
    )
    return ToolDef(name="stats", title="Frame statistics", description=desc, input_schema=schema,
                   read_only=True, idempotent=True)


@never_raises("stats")
def stats(archive: Archive, args: dict) -> ToolReply:
    check_keys(args, {"frame", "region", "channel", "direction"}, {"frame"}, "arguments")
    e = _image_entry(archive, args)
    ch, direction = channel_direction_args(archive, e, args, "arguments")
    region = region_arg(e, args, "arguments")
    i0, j0, i1, j1 = _native_box(e, region)
    fmt = archive.fmt(e, ch)
    z = np.asarray(archive.values(e, ch, direction), dtype=float)[j0:j1, i0:i1]
    fin = z[np.isfinite(z)]
    if fin.size < 4:
        raise ArgError("the region has fewer than 4 acquired pixels")

    def u(v: float) -> float | None:
        return fmt.value(v)

    zp = z - F._fit_surface(z, 1)
    zp_f = zp[np.isfinite(zp)]
    std_p = float(np.std(zp_f))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        rowmed = np.nanmedian(zp, axis=1)
    rowmed = rowmed[np.isfinite(rowmed)]
    line_noise = float(np.median(np.abs(np.diff(rowmed))) / std_p) if rowmed.size > 2 and std_p > 0 else None
    rows_acq = int(np.isfinite(z).any(axis=1).sum())
    pct = (1, 5, 25, 50, 75, 95, 99)
    body: dict[str, Any] = {
        "instrument_unchanged": True, "frame": e.fid, "channel": ch, "direction": direction,
        "region_px": None if region is None else list(region), "native_px": [i0, j0, i1, j1],
        "n_pixels": int(z.size), "n_not_acquired": int(z.size - fin.size), "decimals": fmt.decimals,
        fmt.key("mean"): u(fin.mean()), fmt.key("std"): u(fin.std()),
        fmt.key("min"): u(fin.min()), fmt.key("max"): u(fin.max()),
        fmt.key("percentiles"): {f"p{q}": u(v) for q, v in zip(pct, np.percentile(fin, pct), strict=True)},
        "plane_subtracted": {fmt.key("rms_roughness"): u(math.sqrt(float(np.mean(zp_f ** 2)))),
                             fmt.key("mean_abs_deviation"): u(np.mean(np.abs(zp_f))),
                             fmt.key("peak_to_valley"): u(zp_f.max() - zp_f.min())},
        "line_noise_index": None if line_noise is None else round(line_noise, 4),
        "rows_acquired_fraction": round(rows_acq / max(1, z.shape[0]), 4),
        "rows_acquired": None if e.acquired_rows is None else int(e.acquired_rows), "rows_total": int(e.ny),
    }
    other = "backward" if direction == "forward" else "forward"
    if other in e.channel_directions(ch):
        zo = np.asarray(archive.values(e, ch, other), dtype=float)[j0:j1, i0:i1]
        a = zp
        b = zo - F._fit_surface(zo, 1)
        ok = np.isfinite(a) & np.isfinite(b)
        if ok.sum() > 8 and np.std(a[ok]) > 0 and np.std(b[ok]) > 0:
            r = float(np.corrcoef(a[ok], b[ok])[0, 1])
            body["forward_backward"] = {"correlation": round(r, 4),
                                        fmt.key("rms_difference"): u(math.sqrt(float(np.mean((a[ok] - b[ok]) ** 2))))}
    return json_reply(body, meta={"tool": "stats"})


# ── registry ────────────────────────────────────────────────────────────────
LINE_PROFILE_TOOL = make_line_profile_tool()
FFT_PEAKS_TOOL = make_fft_peaks_tool()
DETECT_BLOBS_TOOL = make_detect_blobs_tool()
DIFF_TOOL = make_diff_tool()
STATS_TOOL = make_stats_tool()

MEASURE_TOOLS: dict[str, tuple[ToolDef, Callable[[Archive, dict], ToolReply]]] = {
    "line_profile": (LINE_PROFILE_TOOL, line_profile),
    "fft_peaks": (FFT_PEAKS_TOOL, fft_peaks),
    "detect_blobs": (DETECT_BLOBS_TOOL, detect_blobs),
    "diff": (DIFF_TOOL, diff),
    "stats": (STATS_TOOL, stats),
}


def measure_tools(display_max: int = DISPLAY_MAX_DEFAULT) -> dict[str, tuple[ToolDef, Callable[[Archive, dict], ToolReply]]]:
    """``MEASURE_TOOLS`` with schemas sized for another ``display_max``."""
    return {"line_profile": (make_line_profile_tool(display_max), line_profile),
            "fft_peaks": (make_fft_peaks_tool(display_max), fft_peaks),
            "detect_blobs": (make_detect_blobs_tool(display_max), detect_blobs),
            "diff": (make_diff_tool(display_max), diff),
            "stats": (make_stats_tool(display_max), stats)}
