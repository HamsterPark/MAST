"""Visual-memory tools: ``inspect`` (look again) and ``read_values`` (read exact values).

README
------
Both tools are read-only: they never touch the instrument, only the workspace's
:class:`~.archive.Archive`. VISTA's ``inspect`` / ``read_pixels`` ported to STM frames:

* ``inspect(question, views[1..16])`` re-renders each view from the archived values (not
  from the stored PNG): channel, direction, flattening, contrast and an exact region in
  display pixels of the frame's full image. A region is widened to whole scan pixels,
  enlarged by the largest whole number of image pixels per scan pixel that fits
  ``display_max`` (never smoothed, never resampled unevenly), one image per view, in
  request order. With ``Archive(grid=True)`` each scan view carries the metric grid sized
  for that view (``frames.view_grid``) and the reply gives ``grid_nm``. Spectra render as
  plots; derived images are cropped as stored.
* ``read_values(question, views[1..64])`` samples archived physical values at bin centers
  (``x + ((2c+1)*w)//(2*C)``, VISTA's rule), at most 4096 samples per call, in the model
  unit carried by the key (``values_pm``, ``values_pa``, ...), rounded to the channel's
  decimals (about 1/100 of its value span), ``null`` where nothing was acquired.

Model-facing JSON follows one vocabulary across all tools: a frame id is under ``frame``
(lists: ``frames``), units are key suffixes (``_nm``, ``_pm``, ``_pa``, ``_v``, ``_hz``,
``_deg``, ``_px``), numbers are JSON numbers.

Every reply text is compact JSON (``protocol.json_reply``); invalid arguments give an
``error_reply`` that says what to change. Implementations never raise.

The argument helpers (:class:`ArgError`, :func:`frame_arg`, :func:`region_arg`, ...) are
shared with ``measure.py``.
"""
from __future__ import annotations

import functools
import math
from typing import Any, Callable

import numpy as np

from . import frames as F
from .archive import Archive, FrameEntry, sample_axis
from .protocol import (DISPLAY_MAX_DEFAULT, MAX_INSPECT_VIEWS, MAX_LABEL_CHARS, MAX_QUESTION_CHARS,
                       MAX_READ_SAMPLES, MAX_READ_VIEWS, ImageBlock, ToolDef, ToolReply, error_reply,
                       json_reply)

FRAME_ID_PATTERN = r"^t\d{4,}\.[spdm]\d+$"


# ── argument validation (shared with measure.py) ────────────────────────────
class ArgError(ValueError):
    """An invalid argument; the message says what to change."""


def never_raises(tool: str) -> Callable:
    """Decorator: ``ArgError`` / ``KeyError`` / ``ValueError`` become an error reply with
    the message; anything else becomes an internal-error reply naming the exception."""

    def wrap(fn: Callable[[Archive, dict], ToolReply]) -> Callable[[Archive, dict], ToolReply]:
        @functools.wraps(fn)
        def inner(archive: Archive, args: dict) -> ToolReply:
            try:
                return fn(archive, args if isinstance(args, dict) else _not_dict(args))
            except (ArgError, KeyError, ValueError) as exc:
                msg = exc.args[0] if exc.args else str(exc)
                return error_reply(f"{tool}: {msg}", instrument_unchanged=True)
            except Exception as exc:  # noqa: BLE001 — a tool must answer, not crash the server
                return error_reply(f"{tool}: internal error {type(exc).__name__}: {exc}",
                                   instrument_unchanged=True)
        return inner
    return wrap


def _not_dict(args: Any) -> dict:
    raise ArgError(f"arguments must be a JSON object, got {type(args).__name__}")


def check_keys(obj: dict, allowed: set[str], required: set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise ArgError(f"{where} must be an object")
    extra = sorted(set(obj) - allowed)
    if extra:
        raise ArgError(f"{where}: unknown field(s) {extra}; allowed: {sorted(allowed)}")
    missing = sorted(k for k in required if k not in obj or obj[k] is None)
    if missing:
        raise ArgError(f"{where}: missing required field(s) {missing}")


def _given(obj: dict, key: str, default: Any) -> Any:
    """The value of an optional argument: ``default`` when it is absent or explicitly null (a model
    may write null for "not given"; it should get the default, not an unrelated refusal)."""
    v = obj.get(key)
    return default if v is None else v


def str_arg(obj: dict, key: str, where: str, *, default: str | None = None, max_len: int = 256,
            min_len: int = 1, choices: tuple[str, ...] | None = None) -> str | None:
    v = _given(obj, key, default)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ArgError(f"{where}.{key} must be a string")
    if not min_len <= len(v) <= max_len:
        raise ArgError(f"{where}.{key} must have {min_len}..{max_len} characters")
    if choices is not None and v not in choices:
        raise ArgError(f"{where}.{key} must be one of {list(choices)}, not {v!r}")
    return v


def num_arg(obj: dict, key: str, where: str, *, default: float | None = None, lo: float | None = None,
            hi: float | None = None, integer: bool = False, lo_exclusive: bool = False) -> Any:
    v = _given(obj, key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ArgError(f"{where}.{key} must be a number")
    if integer:
        if isinstance(v, float) and not float(v).is_integer():
            raise ArgError(f"{where}.{key} must be an integer")
        v = int(v)
    elif not math.isfinite(float(v)):
        raise ArgError(f"{where}.{key} must be finite")
    if lo is not None and (v <= lo if lo_exclusive else v < lo):
        raise ArgError(f"{where}.{key} must be {'>' if lo_exclusive else '>='} {lo}")
    if hi is not None and v > hi:
        raise ArgError(f"{where}.{key} must be <= {hi}")
    return v


def bool_arg(obj: dict, key: str, where: str, *, default: bool) -> bool:
    v = _given(obj, key, default)
    if not isinstance(v, bool):
        raise ArgError(f"{where}.{key} must be true or false")
    return v


def question_arg(args: dict) -> str:
    q = args.get("question")
    if not isinstance(q, str) or not q.strip():
        raise ArgError("question must be a non-empty string")
    if len(q) > MAX_QUESTION_CHARS:
        raise ArgError(f"question is longer than {MAX_QUESTION_CHARS} characters")
    return q


def views_arg(args: dict, max_views: int) -> list[dict]:
    views = args.get("views")
    if not isinstance(views, list) or not views:
        raise ArgError("views must be a non-empty list")
    if len(views) > max_views:
        raise ArgError(f"at most {max_views} views per call (got {len(views)})")
    return views


def label_arg(view: dict, where: str) -> str:
    lab = view.get("label")
    if not isinstance(lab, str) or not lab.strip():
        raise ArgError(f"{where}.label must be a non-empty string")
    if len(lab) > MAX_LABEL_CHARS:
        raise ArgError(f"{where}.label is longer than {MAX_LABEL_CHARS} characters")
    return lab


def frame_arg(archive: Archive, value: Any, where: str, *, need_values: bool = False,
              kinds: tuple[str, ...] | None = None) -> FrameEntry:
    if not isinstance(value, str) or not value.strip():
        raise ArgError(f"{where} must be a frame id like 't0007.s0'")
    try:
        e = archive.get(value.strip())
    except KeyError as exc:
        raise ArgError(f"{where}: {exc.args[0]}") from None
    if kinds is not None and e.kind not in kinds:
        allowed = ", ".join(sorted({F_KIND_WORDS[k] for k in kinds}))
        raise ArgError(f"{where}: {e.fid} is a {e.kind_word}; this needs a {allowed} frame")
    if need_values and e.kind == "m" and not e.is_image:
        raise ArgError(f"{where}: {e.fid} is a derived image without values; use the frame it was "
                       f"derived from ({', '.join(e.extra.get('sources') or []) or 'see history'})")
    return e


F_KIND_WORDS = {"s": "scan", "p": "partial scan", "d": "spectrum", "m": "derived map"}


def channel_direction_args(archive: Archive, e: FrameEntry, obj: dict, where: str) -> tuple[str, str]:
    ch_req = str_arg(obj, "channel", where, max_len=64)
    try:
        ch = archive.resolve_channel(e, ch_req)
    except KeyError as exc:
        raise ArgError(f"{where}.channel: {exc.args[0]}") from None
    direction = str_arg(obj, "direction", where, default="forward", choices=F.DIRECTIONS)
    if direction not in e.channel_directions(ch):
        raise ArgError(f"{where}.direction: {e.fid} channel {ch!r} has only {e.channel_directions(ch)}")
    return ch, direction


def region_arg(e: FrameEntry, obj: dict, where: str) -> tuple[int, int, int, int] | None:
    reg = obj.get("region")
    if reg is None:
        return None
    check_keys(reg, {"x", "y", "width", "height"}, {"x", "y", "width", "height"}, f"{where}.region")
    x = num_arg(reg, "x", f"{where}.region", lo=0, integer=True)
    y = num_arg(reg, "y", f"{where}.region", lo=0, integer=True)
    w = num_arg(reg, "width", f"{where}.region", lo=1, integer=True)
    h = num_arg(reg, "height", f"{where}.region", lo=1, integer=True)
    W, H = e.display_size
    if x + w > W or y + h > H:
        raise ArgError(f"{where}.region x={x}, y={y}, width={w}, height={h} leaves {e.fid}'s "
                       f"{W}x{H} display image (need x + width <= {W} and y + height <= {H})")
    return x, y, w, h


def flatten_args(obj: dict, where: str, *, default: str = "plane") -> tuple[str, float]:
    mode = str_arg(obj, "flatten", where, default=default, choices=F.FLATTEN_MODES)
    hp = num_arg(obj, "highpass_nm", where, default=F.HIGHPASS_NM_DEFAULT, lo=0, hi=10000, lo_exclusive=True)
    if obj.get("highpass_nm") is not None and mode != "highpass":
        raise ArgError(f"{where}.highpass_nm only applies with flatten='highpass'")
    return mode, float(hp)


def x_range_arg(obj: dict, where: str) -> tuple[float, float] | None:
    xr = obj.get("x_range")
    if xr is None:
        return None
    if (not isinstance(xr, list) or len(xr) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in xr)):
        raise ArgError(f"{where}.x_range must be [low, high] numbers")
    lo, hi = float(xr[0]), float(xr[1])
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo == hi:
        raise ArgError(f"{where}.x_range needs two different finite numbers")
    return (min(lo, hi), max(lo, hi))


# ── schemas ─────────────────────────────────────────────────────────────────
def region_schema(display_max: int) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["x", "y", "width", "height"],
            "description": ("Rectangle in display pixels of the frame's full image: origin at the top-left "
                            "corner, x to the right, y down (the coordinates act accepts with a frame id)."),
            "properties": {"x": {"type": "integer", "minimum": 0, "maximum": display_max - 1},
                           "y": {"type": "integer", "minimum": 0, "maximum": display_max - 1},
                           "width": {"type": "integer", "minimum": 1, "maximum": display_max},
                           "height": {"type": "integer", "minimum": 1, "maximum": display_max}}}


def _common_view_props() -> dict:
    return {
        "label": {"type": "string", "minLength": 1, "maxLength": MAX_LABEL_CHARS,
                  "description": "A short name for this view."},
        "frame": {"type": "string", "pattern": FRAME_ID_PATTERN,
                  "description": "Archived frame id, e.g. 't0007.s0' (scan), 't0007.p0' (partial scan), "
                                 "'t0007.d0' (spectrum), 't0007.m0' (derived image)."},
        "channel": {"type": "string", "minLength": 1, "maxLength": 64,
                    "description": "Scan channel, e.g. 'Z' or 'Current'. Default: the frame's default "
                                   "channel (Z; Current for a constant-height frame)."},
        "direction": {"type": "string", "enum": list(F.DIRECTIONS),
                      "description": "Scan pass. Default forward."},
    }


QUESTION_SCHEMA = {"type": "string", "minLength": 1, "maxLength": MAX_QUESTION_CHARS,
                   "description": "The visual question these views should answer."}


def make_inspect_tool(display_max: int = DISPLAY_MAX_DEFAULT, grid: bool = False) -> ToolDef:
    dm = int(display_max)
    view = {"type": "object", "additionalProperties": False, "required": ["label", "frame"],
            "properties": {
                **_common_view_props(),
                "region": region_schema(dm),
                "flatten": {"type": "string", "enum": list(F.FLATTEN_MODES),
                            "description": "Background removed before display, over the whole frame: none; "
                                           "plane (default); line (plane, then each row's median offset); "
                                           "poly2 (2nd-order surface); highpass (median background of "
                                           "highpass_nm)."},
                "highpass_nm": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000,
                                "description": "Background width in nm for flatten='highpass'. Default 3."},
                "clip_pct": {"type": "number", "minimum": 0, "maximum": 25,
                             "description": "Contrast: black and white at this percentile and 100 minus it "
                                            "of the selected region. Default 0.5."},
                "x_col": {"type": "string", "minLength": 1, "maxLength": 64,
                          "description": "Spectrum only: x column (default the sweep column)."},
                "y_col": {"type": "string", "minLength": 1, "maxLength": 64,
                          "description": "Spectrum only: one y column (default: the first four columns other than x, "
                                         "one panel each)."},
                "x_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
                            "description": "Spectrum only: [low, high] of the x axis, in the unit the plot "
                                           "shows (bias in V, Z in pm)."},
            }}
    schema = {"type": "object", "additionalProperties": False, "required": ["question", "views"],
              "properties": {"question": QUESTION_SCHEMA,
                             "views": {"type": "array", "minItems": 1, "maxItems": MAX_INSPECT_VIEWS,
                                       "items": view}}}
    drawn = ("dark blue; nothing is drawn on them." if not grid else
             f"dark blue. Faint {F.GRID_COLOUR_NAME} lines draw a square grid every grid_nm nm, counted from "
             "the top-left corner of the frame's full image along its own x and y, finer when you zoom in; "
             "nothing else is drawn on them.")
    spacing = "" if not grid else " the grid spacing (grid_nm),"
    desc = (
        "Look again at archived frames without changing the instrument. Each view re-renders one frame "
        "from its archived values: a saved scan, a partial scan, a spectrum or a derived image. For a scan, "
        "choose the channel, the direction (forward or backward), the flattening and the contrast. Omit "
        "region to see the whole frame, or select a rectangle in display pixels of that frame's full image "
        "(origin at the top-left corner, x to the right, y down: the coordinates act accepts with a frame "
        "id). The region is cropped exactly, widened to whole scan pixels, and enlarged by the largest whole "
        f"number of image pixels per scan pixel that fits {dm}x{dm}, without smoothing. Images are gray "
        "(black = low, white = high, contrast from the selected region); pixels never acquired are flat "
        f"{drawn} A spectrum view is a line plot (x_col, y_col, x_range instead "
        "of region); a derived image is cropped as stored. State the visual question these views should "
        "answer and give each view a short label. The views are returned in request order, one image each; "
        "the reply text gives each view's region in display pixels (region_px), the scan-frame nm of its "
        f"corners (corners_nm), the magnification, nm per image pixel,{spacing} and the values shown as black "
        "and white with the unit in the key (black_pm / white_pm for Z, black_pa / white_pa for current). "
        "Your question and labels remain in this call."
    )
    return ToolDef(name="inspect", title="Inspect archived frames", description=desc, input_schema=schema,
                   read_only=True, idempotent=True)


def make_read_values_tool(display_max: int = DISPLAY_MAX_DEFAULT) -> ToolDef:
    dm = int(display_max)
    view = {"type": "object", "additionalProperties": False, "required": ["label", "frame"],
            "properties": {
                **_common_view_props(),
                "region": region_schema(dm),
                "rows": {"type": "integer", "minimum": 1, "maximum": MAX_READ_SAMPLES,
                         "description": "Scan frames: number of equal rows the region is divided into."},
                "columns": {"type": "integer", "minimum": 1, "maximum": MAX_READ_SAMPLES,
                            "description": "Scan frames: number of equal columns the region is divided into."},
                "x_col": {"type": "string", "minLength": 1, "maxLength": 64,
                          "description": "Spectrum only: x column (default the sweep column)."},
                "y_cols": {"type": "array", "minItems": 1, "maxItems": 8,
                           "items": {"type": "string", "minLength": 1, "maxLength": 64},
                           "description": "Spectrum only: columns to return besides x (default all)."},
                "x_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
                            "description": "Spectrum only: [low, high] in the x column's unit (bias in V)."},
                "n": {"type": "integer", "minimum": 1, "maximum": MAX_READ_SAMPLES,
                      "description": "Spectrum only: number of samples at bin centers (default: every point)."},
            }}
    schema = {"type": "object", "additionalProperties": False, "required": ["question", "views"],
              "properties": {"question": QUESTION_SCHEMA,
                             "views": {"type": "array", "minItems": 1, "maxItems": MAX_READ_VIEWS,
                                       "items": view}}}
    desc = (
        "Read exact archived values from one or more frames without changing the instrument. For a scan "
        "view, the region (display pixels of that frame's full image; omit it for the whole frame) is "
        "divided into equal rows and columns and the archived pixel at the center of every cell is read: "
        "display pixel x + ((2c+1)*width)//(2*columns), likewise for y. Values are physical and unflattened, "
        "in one unit per quantity carried by the key: values_pm for Z, values_pa for current, values_hz for "
        "frequency shift, values_v for voltages; rounded to the view's decimals (about 1/100 of the "
        "channel's value range); null where nothing was acquired. The reply gives the sampled display "
        "pixels and the scan-frame nm of the first and last sample. For a spectrum view, give x_range (in "
        "the x column's unit), n and y_cols instead; values come back per column with column_units. At most "
        f"{MAX_READ_VIEWS} views and {MAX_READ_SAMPLES} samples per call. This tool only reads values; it "
        "does not flatten, align, compare or interpret them. State the question and give each view a short "
        "label; those remain in this call."
    )
    return ToolDef(name="read_values", title="Read archived values", description=desc, input_schema=schema,
                   read_only=True, idempotent=True)


INSPECT_TOOL = make_inspect_tool()
READ_VALUES_TOOL = make_read_values_tool()


# ── inspect ─────────────────────────────────────────────────────────────────
_INSPECT_SCAN_KEYS = {"label", "frame", "channel", "direction", "region", "flatten", "highpass_nm", "clip_pct"}
_INSPECT_SPEC_KEYS = {"label", "frame", "x_col", "y_col", "x_range"}
_INSPECT_DERIVED_KEYS = {"label", "frame", "region"}
_INSPECT_ALL_KEYS = _INSPECT_SCAN_KEYS | _INSPECT_SPEC_KEYS


@never_raises("inspect")
def inspect(archive: Archive, args: dict) -> ToolReply:
    check_keys(args, {"question", "views"}, {"question", "views"}, "arguments")
    question_arg(args)
    views = views_arg(args, MAX_INSPECT_VIEWS)
    plans = []
    for i, v in enumerate(views, 1):
        where = f"views[{i}]"
        check_keys(v, _INSPECT_ALL_KEYS, {"label", "frame"}, where)
        label = label_arg(v, where)
        e = frame_arg(archive, v.get("frame"), f"{where}.frame")
        if e.kind in ("s", "p"):
            _only(v, _INSPECT_SCAN_KEYS, where, "a scan")
            ch, direction = channel_direction_args(archive, e, v, where)
            mode, hp = flatten_args(v, where)
            clip = num_arg(v, "clip_pct", where, default=F.CLIP_PCT_DEFAULT, lo=0, hi=25)
            plans.append((label, e, dict(channel=ch, direction=direction, flatten=mode, highpass_nm=hp,
                                         clip_pct=float(clip), region=_region_dict(region_arg(e, v, where)))))
        elif e.kind == "d":
            _only(v, _INSPECT_SPEC_KEYS, where, "a spectrum")
            plans.append((label, e, dict(x_col=str_arg(v, "x_col", where, max_len=64),
                                         y_col=str_arg(v, "y_col", where, max_len=64),
                                         x_range=x_range_arg(v, where))))
        else:
            _only(v, _INSPECT_DERIVED_KEYS, where, "a derived image")
            plans.append((label, e, dict(region=_region_dict(region_arg(e, v, where)))))
    n = len(plans)
    out_views, images = [], []
    for i, (label, e, kw) in enumerate(plans, 1):
        try:
            png, meta = archive.render(e.fid, label_kind="inspection",
                                       label_attrs={"index": i, "count": n, "label": label}, **kw)
        except (KeyError, ValueError) as exc:
            raise ArgError(f"views[{i}]: {exc.args[0] if exc.args else exc}") from None
        images.append(ImageBlock(png=png, label=meta["label"]))
        out_views.append(_view_summary(i, label, e, meta))
    return json_reply({"instrument_unchanged": True, "view_count": n, "views": out_views},
                      images=images, meta={"tool": "inspect"})


def _only(v: dict, allowed: set[str], where: str, what: str) -> None:
    extra = sorted(k for k in set(v) - allowed if v[k] is not None)       # null = not given
    if extra:
        raise ArgError(f"{where}: {extra} do not apply to {what} frame; allowed: {sorted(allowed)}")


def _region_dict(r: tuple[int, int, int, int] | None) -> dict | None:
    if r is None:
        return None
    return {"x": r[0], "y": r[1], "width": r[2], "height": r[3]}


def _view_summary(i: int, label: str, e: FrameEntry, meta: dict) -> dict:
    out: dict[str, Any] = {"index": i, "label": label, "frame": e.fid, "kind": e.kind_name}
    if e.kind in ("s", "p"):
        fmt = meta["fmt"]
        out.update({k: meta[k] for k in ("channel", "direction", "flatten")})
        if meta.get("highpass_nm") is not None:
            out["highpass_nm"] = meta["highpass_nm"]
        out.update({k: meta[k] for k in ("region_px", "native_px", "image_size_px", "magnification",
                                          "image_px_per_scan_px", "nm_per_image_px", "corners_nm")})
        if meta.get("grid_nm") is not None:
            out["grid_nm"] = meta["grid_nm"]
        out[fmt.key("black")] = meta["black"]
        out[fmt.key("white")] = meta["white"]
        out[fmt.key("min")] = meta["min"]
        out[fmt.key("max")] = meta["max"]
        if e.acquired_rows is not None and e.acquired_rows < e.ny:
            out["rows_acquired"] = int(e.acquired_rows)
            out["rows_total"] = int(e.ny)
    elif e.kind == "d":
        out.update({"x_col": meta["x_col"], "x_unit": meta["x_unit"],
                    "y_cols": [d["column"] for d in meta["y"]], "y_units": [d["unit"] for d in meta["y"]],
                    "n_points": meta["n_points"],
                    F.unit_key("x_shown", meta["x_unit"]): [float(f"{v:.6g}") for v in meta["x_shown"]],
                    "image_size_px": meta["image_size_px"]})
    else:
        out.update({k: meta[k] for k in ("tool", "region_px", "image_size_px", "magnification")})
    return out


# ── read_values ─────────────────────────────────────────────────────────────
_READ_SCAN_KEYS = {"label", "frame", "channel", "direction", "region", "rows", "columns"}
_READ_SPEC_KEYS = {"label", "frame", "x_col", "y_cols", "x_range", "n"}


@never_raises("read_values")
def read_values(archive: Archive, args: dict) -> ToolReply:
    check_keys(args, {"question", "views"}, {"question", "views"}, "arguments")
    question_arg(args)
    views = views_arg(args, MAX_READ_VIEWS)
    plans = []
    total = 0
    for i, v in enumerate(views, 1):
        where = f"views[{i}]"
        check_keys(v, _READ_SCAN_KEYS | _READ_SPEC_KEYS, {"label", "frame"}, where)
        label = label_arg(v, where)
        e = frame_arg(archive, v.get("frame"), f"{where}.frame", need_values=True)
        if e.kind == "d":
            _only(v, _READ_SPEC_KEYS, where, "a spectrum")
            plan = _plan_spectrum_read(archive, e, v, where)
            total += plan["n"] * (1 + len(plan["y_cols"]))
        else:
            _only(v, _READ_SCAN_KEYS, where, "a scan")
            ch, direction = channel_direction_args(archive, e, v, where)
            rows = num_arg(v, "rows", where, lo=1, hi=MAX_READ_SAMPLES, integer=True)
            cols = num_arg(v, "columns", where, lo=1, hi=MAX_READ_SAMPLES, integer=True)
            if rows is None or cols is None:
                raise ArgError(f"{where}: a scan view needs rows and columns")
            region = region_arg(e, v, where) or (0, 0, e.display_size[0], e.display_size[1])
            plan = {"ch": ch, "direction": direction, "rows": rows, "cols": cols, "region": region}
            total += rows * cols
        if total > MAX_READ_SAMPLES:
            raise ArgError(f"at most {MAX_READ_SAMPLES} samples per call; views 1..{i} already ask for {total}")
        plans.append((label, e, plan))
    out_views = []
    for i, (label, e, plan) in enumerate(plans, 1):
        if e.kind == "d":
            out_views.append(_read_spectrum(archive, i, label, e, plan))
        else:
            out_views.append(_read_scan(archive, i, label, e, plan))
    return json_reply({"instrument_unchanged": True,
                       "sampling": "cell centers: x + ((2c+1)*width)//(2*columns), y likewise",
                       "sample_count": total, "view_count": len(out_views), "views": out_views},
                      meta={"tool": "read_values"})


def _read_scan(archive: Archive, i: int, label: str, e: FrameEntry, plan: dict) -> dict:
    x, y, w, h = plan["region"]
    num, den = e.scale_pair
    xs_disp, cols = sample_axis(x, w, plan["cols"], num, den, e.nx)
    ys_disp, rows = sample_axis(y, h, plan["rows"], num, den, e.ny)
    z = archive.values(e, plan["ch"], plan["direction"])
    sub = np.asarray(z, dtype=float)[np.ix_(rows, cols)]
    fmt = archive.fmt(e, plan["ch"])
    values = fmt.values(sub)
    first = F.display_to_scan_nm(e, xs_disp[0] + 0.5, ys_disp[0] + 0.5)
    last = F.display_to_scan_nm(e, xs_disp[-1] + 0.5, ys_disp[-1] + 0.5)
    out = {"index": i, "label": label, "frame": e.fid, "channel": plan["ch"], "direction": plan["direction"],
           "region_px": [x, y, w, h], "rows": plan["rows"], "columns": plan["cols"],
           "sample_x_px": xs_disp.tolist(), "sample_y_px": ys_disp.tolist(),
           "first_sample_nm": [round(float(first[0]), 4), round(float(first[1]), 4)],
           "last_sample_nm": [round(float(last[0]), 4), round(float(last[1]), 4)],
           "decimals": fmt.decimals, fmt.key("values"): values}
    n_null = sum(v is None for row in values for v in row)
    if n_null:
        out["null_count"] = n_null
    return out


def _plan_spectrum_read(archive: Archive, e: FrameEntry, v: dict, where: str) -> dict:
    names = list(e.channels)
    xc_req = str_arg(v, "x_col", where, max_len=64)
    try:
        xc = names[0] if xc_req is None else F.resolve_channel(xc_req, names, e.units)
        yreq = v.get("y_cols")
        if yreq is None:
            ycs = [c for c in names if c != xc]
        else:
            if not isinstance(yreq, list) or not yreq or not all(isinstance(c, str) for c in yreq):
                raise ArgError(f"{where}.y_cols must be a list of column names")
            ycs = [F.resolve_channel(c, names, e.units) for c in yreq]
    except KeyError as exc:
        raise ArgError(f"{where}: {exc.args[0]}") from None
    xs_si = np.asarray(archive.values(e, xc), dtype=float)
    xfmt = archive.fmt(e, xc)
    fx, ux = xfmt.factor, xfmt.unit
    xs = xs_si * fx
    sel = np.arange(xs.size)
    xr = x_range_arg(v, where)
    if xr is not None:
        sel = np.nonzero(F.in_range(xs, xr[0], xr[1]))[0]
        if sel.size == 0:
            raise ArgError(f"{where}.x_range {xr[0]}..{xr[1]} {ux} contains no samples; x spans "
                           f"{np.nanmin(xs):.6g}..{np.nanmax(xs):.6g} {ux}")
    n = num_arg(v, "n", where, default=int(sel.size), lo=1, hi=MAX_READ_SAMPLES, integer=True)
    n = min(int(n), int(sel.size))
    return {"x_col": xc, "y_cols": ycs, "sel": sel, "n": n, "x_range": xr}


def _read_spectrum(archive: Archive, i: int, label: str, e: FrameEntry, plan: dict) -> dict:
    sel = plan["sel"]
    N, n = int(sel.size), int(plan["n"])
    idx = sel[((2 * np.arange(n) + 1) * N) // (2 * n)]
    cols = [plan["x_col"]] + list(plan["y_cols"])
    units, decimals, values = [], [], []
    for c in cols:
        fmt = archive.fmt(e, c)
        units.append(fmt.unit)
        decimals.append(fmt.decimals)
        values.append(fmt.values(np.asarray(archive.values(e, c), dtype=float)[idx]))
    out = {"index": i, "label": label, "frame": e.fid, "kind": e.kind_name, "column_names": cols,
           "column_units": units, "column_decimals": decimals, "n": n, "points_in_range": N,
           "sample_index": idx.tolist(), "values": values}
    if plan["x_range"] is not None:
        out[F.unit_key("x_range", units[0])] = list(plan["x_range"])
    return out


VIEW_TOOLS: dict[str, tuple[ToolDef, Callable[[Archive, dict], ToolReply]]] = {
    "inspect": (INSPECT_TOOL, inspect),
    "read_values": (READ_VALUES_TOOL, read_values),
}


def view_tools(display_max: int = DISPLAY_MAX_DEFAULT,
               grid: bool = False) -> dict[str, tuple[ToolDef, Callable[[Archive, dict], ToolReply]]]:
    """``VIEW_TOOLS`` with schemas sized for another ``display_max``, and with ``inspect``
    describing the grid when the archive draws one (``Archive(grid=True)``)."""
    return {"inspect": (make_inspect_tool(display_max, grid), inspect),
            "read_values": (make_read_values_tool(display_max), read_values)}
