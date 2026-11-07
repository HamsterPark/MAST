"""A picture of the table: matplotlib's Agg canvas, no pyplot, no display.

``render_png(layout)`` draws the table with its holes, every component as a simple shape of its
own (nothing copied from an icon library: the shapes are lines, polygons and arcs), the beams of the
trace coloured by wavelength (visible light by its approximate colour; infrared and ultraviolet in
a fixed colour and dashed; an unknown wavelength grey) with the line weight following the intensity
of a branch, and the places that deserve a look marked: a ring where a beam hits more than 20 % of
the aperture off centre, a cross where a beam meets the back of something or just misses a
component. Labels are drawn next to the components; a text label of the drawing at its place.

Only the Object-Oriented API is used (``Figure`` + ``FigureCanvasAgg``): no ``pyplot`` global
state, so it is safe in a server thread and without a display. A lock serialises renders (font
caches are shared). Labels are plain text: a ``$`` in one is a dollar sign, never mathtext (``parse_math=False``), so no label can make the
drawing fail. A font with Chinese glyphs is used when the machine has one; otherwise Chinese
characters in labels are drawn as ``?`` rather than as empty boxes.
"""
from __future__ import annotations

import functools
import io
import math
import threading
from typing import Any, Optional

from mast.optical_layout.model import SOURCE_TYPES, Component, Layout, normalize_layout
from mast.optical_layout.summary import OFF_CENTER_SHARE
from mast.optical_layout.trace import near_misses, lit_components, trace_layout, truncation

MIN_WIDTH_PX = 200
MAX_WIDTH_PX = 3000
DEFAULT_WIDTH_PX = 1200
_DPI = 100
_LOCK = threading.Lock()
_CJK_FONTS = ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
              "PingFang SC", "Arial Unicode MS")
_MAX_HOLES = 9000


# ── colours ──────────────────────────────────────────────────────────────────────────────────


def wavelength_color(nm: Optional[float]) -> tuple[tuple[float, float, float], str]:
    """``(rgb, linestyle)`` of a beam: the visible spectrum's approximate colour (solid), a fixed
    dark red / violet (dashed) for infrared / ultraviolet, grey (solid) for an unknown wavelength."""
    if nm is None:
        return (0.35, 0.35, 0.35), "-"
    if nm < 380:
        return (0.45, 0.1, 0.8), "--"
    if nm > 780:
        return (0.6, 0.0, 0.0), "--"
    if nm < 440:
        r, g, b = -(nm - 440) / 60, 0.0, 1.0
    elif nm < 490:
        r, g, b = 0.0, (nm - 440) / 50, 1.0
    elif nm < 510:
        r, g, b = 0.0, 1.0, -(nm - 510) / 20
    elif nm < 580:
        r, g, b = (nm - 510) / 70, 1.0, 0.0
    elif nm < 645:
        r, g, b = 1.0, -(nm - 645) / 65, 0.0
    else:
        r, g, b = 1.0, 0.0, 0.0
    fade = 0.3 + 0.7 * (nm - 380) / 40 if nm < 420 else (0.3 + 0.7 * (780 - nm) / 80 if nm > 700 else 1.0)
    # a little darker than the pure spectral colour so that yellow and green stay visible on white
    return (max(0.0, r * fade * 0.85), max(0.0, g * fade * 0.8), max(0.0, b * fade * 0.85)), "-"


@functools.lru_cache(maxsize=1)
def _font() -> tuple[Any, bool]:
    """``(FontProperties, has_cjk)``."""
    from matplotlib import font_manager as fm

    for family in _CJK_FONTS:
        try:
            path = fm.findfont(fm.FontProperties(family=family), fallback_to_default=False)
            return fm.FontProperties(fname=path), True
        except (ValueError, OSError):
            continue
    return fm.FontProperties(family="DejaVu Sans"), False


def _text(s: str) -> str:
    """*s* as the chosen font can draw it (no CJK font: those characters become ``?``)."""
    if _font()[1]:
        return s
    return "".join("?" if ord(ch) >= 0x2E80 else ch for ch in s)


# ── geometry of one component ────────────────────────────────────────────────────────────────


class _Frame:
    """A component's local frame: ``p(a, b)`` = centre + a * n + b * u (n = local +x, u = local +y)."""

    def __init__(self, c: Component) -> None:
        a = math.radians(c.angle_deg)
        self.n = (math.cos(a), math.sin(a))
        self.u = (-math.sin(a), math.cos(a))
        self.c = (c.x_mm, c.y_mm)
        self.half = c.aperture_mm / 2.0

    def p(self, a: float, b: float) -> tuple[float, float]:
        return (self.c[0] + a * self.n[0] + b * self.u[0], self.c[1] + a * self.n[1] + b * self.u[1])

    def seg(self, a0: float, b0: float, a1: float, b1: float) -> list[tuple[float, float]]:
        return [self.p(a0, b0), self.p(a1, b1)]


def _draw_component(ax: Any, c: Component, fp: Any, label: bool) -> None:
    from matplotlib.patches import Circle, Polygon

    f = _Frame(c)
    h = f.half
    t = max(2.0, min(6.0, h * 0.25))              # a plate's drawn thickness
    ink, fill = "#222222", "#dfe9f5"
    kw: dict[str, Any] = dict(zorder=5)

    def poly(points, face=fill, edge=ink, lw=1.0, **extra):
        ax.add_patch(Polygon(points, closed=True, facecolor=face, edgecolor=edge, linewidth=lw, **kw, **extra))

    def line(points, color=ink, lw=1.2, **extra):
        (x0, y0), (x1, y1) = points
        ax.plot([x0, x1], [y0, y1], color=color, linewidth=lw, solid_capstyle="butt", zorder=5, **extra)

    ty = c.type
    if ty in ("mirror", "grating"):
        line(f.seg(0, -h, 0, h), lw=2.6)
        for k in range(-4, 5):                                       # hatching on the back
            b = h * k / 4.5
            line(f.seg(0, b, -t, b - t), lw=0.8, color="#666666")
        if ty == "grating":
            for k in range(-4, 5):
                b = h * k / 4.5
                line(f.seg(0, b, t * 0.7, b), lw=0.8, color="#a0522d")
    elif ty in ("beam_splitter", "dichroic"):
        poly([f.p(0, -h), f.p(h, 0), f.p(0, h), f.p(-h, 0)], face="#fff4d6" if ty == "dichroic" else "#e8f4e8")
        line(f.seg(0, -h, 0, h), lw=1.6, color="#b8860b" if ty == "dichroic" else "#2e7d32")
    elif ty == "lens":
        top = [f.p(t * (1 - (s / 10) ** 2) * 1.6, h * s / 10) for s in range(-10, 11)]
        bottom = [f.p(-t * (1 - (s / 10) ** 2) * 1.6, h * s / 10) for s in range(10, -11, -1)]
        poly(top + bottom, face="#d9ecff")
    elif ty == "objective":
        poly([f.p(-t * 2, -h), f.p(t * 2, -h * 0.45), f.p(t * 2, h * 0.45), f.p(-t * 2, h)], face="#cfd8dc")
    elif ty in ("waveplate", "polarizer", "window", "nd", "aotf", "periscope", "filter", "custom"):
        face = {"waveplate": "#ede7f6", "polarizer": "#e0f2f1", "filter": "#fff8e1", "nd": "#cfcfcf",
                "aotf": "#fce4ec", "periscope": "#e8eaf6", "custom": "#f5f5f5"}.get(ty, "#e3f2fd")
        poly([f.p(-t / 2, -h), f.p(t / 2, -h), f.p(t / 2, h), f.p(-t / 2, h)], face=face)
        if ty == "waveplate":
            line(f.seg(-t / 2, 0, t / 2, 0), lw=0.9)
        elif ty == "polarizer":
            for k in (-0.5, 0, 0.5):
                line(f.seg(-t / 2, h * k, t / 2, h * k), lw=0.7)
    elif ty == "prism":
        poly([f.p(-h * 0.6, -h), f.p(-h * 0.6, h), f.p(h * 0.6, 0)], face="#e1f5fe")
    elif ty == "iris":
        open_mm = c.props.get("open_mm")
        o = min(h, open_mm / 2.0) if open_mm else h * 0.4
        line(f.seg(0, -h, 0, -o), lw=3.0)
        line(f.seg(0, o, 0, h), lw=3.0)
        ax.add_patch(Circle(f.p(0, -h), t * 0.5, facecolor=ink, edgecolor=ink, **kw))
        ax.add_patch(Circle(f.p(0, h), t * 0.5, facecolor=ink, edgecolor=ink, **kw))
    elif ty == "shutter":
        closed = bool(c.props.get("closed"))
        poly([f.p(-t / 2, -h), f.p(t / 2, -h), f.p(t / 2, h), f.p(-t / 2, h)], face="#e57373" if closed else "#ffffff")
    elif ty == "sample":
        poly([f.p(-t, -h), f.p(t, -h), f.p(t, h), f.p(-t, h)], face="#ffe0b2" if c.props.get("reflects") else "#d7ccc8")
    elif ty in SOURCE_TYPES:
        s = max(5.0, min(14.0, h * 3))
        poly([f.p(-s, -s * 0.6), f.p(s * 0.4, -s * 0.6), f.p(s * 0.4, s * 0.6), f.p(-s, s * 0.6)],
             face={"laser": "#ffd0d0", "fiber_out": "#ffe9c7", "lamp": "#fff6b0", "led": "#e0ffd0"}[ty], lw=1.1)
        poly([f.p(s * 0.4, -s * 0.35), f.p(s * 1.2, 0), f.p(s * 0.4, s * 0.35)], face=ink)
    elif ty in ("beam_dump", "blocker"):
        poly([f.p(-t, -h), f.p(t, -h), f.p(t, h), f.p(-t, h)], face="#424242" if ty == "beam_dump" else "#9e9e9e")
    elif ty == "fiber_in":
        ax.add_patch(Circle(f.c, max(2.5, h), facecolor="#fff3e0", edgecolor=ink, linewidth=1.0, **kw))
        line(f.seg(max(2.5, h) * 1.6, 0, 0, 0), lw=1.2)
    else:                                                         # detector, camera, spectrometer, power meter
        s = max(h, 4.0)
        poly([f.p(-t, -s), f.p(t, -s), f.p(t, s), f.p(-t, s)], face="#d7f0d7")
        line(f.seg(-t, -s, t, s), lw=0.8, color="#2e7d32")
    if label:
        text = _text(c.label or c.id)
        off = f.p(-(max(h, 8.0) + 6.0), 0)
        ax.text(off[0], off[1], text, fontproperties=fp, fontsize=7, color="#111111", ha="center", va="center", zorder=8,
                parse_math=False,
                bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.75))


# ── the picture ──────────────────────────────────────────────────────────────────────────────


def render_png(layout: Any, trace: Optional[list[dict]] = None, *, width_px: int = DEFAULT_WIDTH_PX,
               labels: bool = True, fit: str = "table") -> bytes:
    """The PNG of *layout* (a :class:`Layout` or its dict), *width_px* wide (200-3000).

    ``fit="table"`` shows the whole table; ``fit="content"`` zooms to where the components and the
    beams are (the axes still carry the table's coordinates) - what a reader that cannot zoom
    wants when the work is a corner of a large table."""
    lay = normalize_layout(layout)
    width_px = max(MIN_WIDTH_PX, min(MAX_WIDTH_PX, int(width_px)))
    tr_ = trace if trace is not None else trace_layout(lay)
    with _LOCK:
        return _render(lay, tr_, width_px, labels, fit)


def _view(lay: Layout, trace: list[dict], fit: str) -> tuple[float, float, float, float]:
    """``(x0, x1, y0, y1)`` of what is drawn."""
    t = lay.table
    pad = 0.06 * max(t.width_mm, t.height_mm)
    whole = (-pad, t.width_mm + pad, -pad, t.height_mm + pad)
    if fit != "content" or not lay.components:
        return whole
    xs: list[float] = []
    ys: list[float] = []
    for c in lay.components:
        r = max(c.aperture_mm / 2.0, 10.0)
        xs += [c.x_mm - r, c.x_mm + r]
        ys += [c.y_mm - r, c.y_mm + r]
    for tree in trace:
        for br in tree["branches"]:
            for ev in br["events"]:
                xs.append(ev["x"])
                ys.append(ev["y"])
            if br["end"] == "left_table" and br["segments"]:       # run to the edge of the world: only its start counts
                seg = br["segments"][-1]
                xs.append(seg["x0"])
                ys.append(seg["y0"])
    x_lo, x_hi, y_lo, y_hi = min(xs), max(xs), min(ys), max(ys)
    span = max(x_hi - x_lo, y_hi - y_lo, 100.0)
    margin = max(0.12 * span, 30.0)
    cx, cy = (x_lo + x_hi) / 2.0, (y_lo + y_hi) / 2.0
    half_w, half_h = max((x_hi - x_lo) / 2.0 + margin, 60.0), max((y_hi - y_lo) / 2.0 + margin, 60.0)
    box = (cx - half_w, cx + half_w, cy - half_h, cy + half_h)
    # never wider than the whole drawing
    return (max(box[0], whole[0]), min(box[1], whole[1]), max(box[2], whole[2]), min(box[3], whole[3]))


def _render(lay: Layout, trace: list[dict], width_px: int, labels: bool, fit: str = "table") -> bytes:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.collections import LineCollection
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle

    fp, _ = _font()
    t = lay.table
    x0, x1, y0, y1 = _view(lay, trace, fit)
    dx, dy = x1 - x0, y1 - y0
    left, bottom, right, top = 0.07, 0.07, 0.015, 0.06                     # figure fractions
    plot_w_in = width_px / _DPI * (1 - left - right)
    plot_h_in = plot_w_in * dy / dx
    fig_w_in = width_px / _DPI
    fig_h_in = plot_h_in / (1 - bottom - top)
    fig = Figure(figsize=(fig_w_in, fig_h_in), dpi=_DPI, facecolor="white")
    FigureCanvasAgg(fig)
    ax = fig.add_axes([left, bottom, 1 - left - right, 1 - bottom - top])
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal", adjustable="box")
    ax.tick_params(labelsize=7, length=2)
    for lab in (*ax.get_xticklabels(), *ax.get_yticklabels()):
        lab.set_fontproperties(fp)
    ax.set_xlabel("x (mm)", fontsize=7, fontproperties=fp)
    ax.set_ylabel("y (mm)", fontsize=7, fontproperties=fp)

    ax.add_patch(Rectangle((0, 0), t.width_mm, t.height_mm, facecolor="#f6f6f2", edgecolor="#444444", linewidth=1.2, zorder=0))
    if t.grid != "none" and t.hole_pitch_mm > 0:
        xs = [t.hole_offset_mm + k * t.hole_pitch_mm for k in range(int(max(0, (t.width_mm - t.hole_offset_mm) // t.hole_pitch_mm) + 1))]
        ys = [t.hole_offset_mm + k * t.hole_pitch_mm for k in range(int(max(0, (t.height_mm - t.hole_offset_mm) // t.hole_pitch_mm) + 1))]
        if len(xs) * len(ys) <= _MAX_HOLES:
            ax.scatter([x for y in ys for x in xs], [y for y in ys for _ in xs], s=1.2, c="#c8c8c0", linewidths=0, zorder=1)

    # The beams go into two collections (solid and dashed), the marks into one line per kind: a
    # matplotlib artist costs about a millisecond, so one per branch or per mark would make a
    # trace of thousands of events take seconds.
    beams: dict[str, tuple[list, list, list]] = {"-": ([], [], []), "--": ([], [], [])}
    for tree in trace:
        color, style = wavelength_color(tree["wavelength_nm"])
        segs, colors, widths = beams[style]
        for br in tree["branches"]:
            rgba = (*color, 0.55 + 0.4 * br["fraction"])
            width = 0.8 + 1.6 * br["fraction"]
            for s in br["segments"]:
                segs.append([(s["x0"], s["y0"]), (s["x1"], s["y1"])])
                colors.append(rgba)
                widths.append(width)
    for style, (segs, colors, widths) in beams.items():
        if segs:
            ax.add_collection(LineCollection(segs, colors=colors, linestyles=style, linewidths=widths, zorder=3))

    for c in lay.components:
        if x0 <= c.x_mm <= x1 and y0 <= c.y_mm <= y1:
            _draw_component(ax, c, fp, labels)

    by_id = {c.id: c for c in lay.components}
    back_hits: list[tuple[float, float]] = []
    off_center: list[tuple[float, float]] = []
    for tree in trace:
        for br in tree["branches"]:
            for ev in br["events"]:
                c = by_id.get(ev["component"])
                if c is None:
                    continue
                if ev["action"] == "back_hit":
                    back_hits.append((ev["x"], ev["y"]))
                elif ev["action"] in ("reflect", "transmit", "split", "stop") and abs(ev["offset_mm"]) > OFF_CENTER_SHARE * c.aperture_mm:
                    off_center.append((ev["x"], ev["y"]))
    cut = truncation(trace)
    misses: list[tuple[float, float]] = []
    if trace and not cut:       # a trace that stops short cannot tell which components no beam reaches
        lit = lit_components(trace)
        misses = [(m["x"], m["y"]) for m in near_misses(lay, trace) if m["component"] not in lit]
    if back_hits:
        ax.plot(*zip(*back_hits), linestyle="none", marker="x", color="#d32f2f", markersize=8, markeredgewidth=2, zorder=7)
    if off_center:
        ax.plot(*zip(*off_center), linestyle="none", marker="o", markerfacecolor="none", markeredgecolor="#f9a825",
                markersize=9, markeredgewidth=1.6, zorder=7)
    if misses:
        ax.plot(*zip(*misses), linestyle="none", marker="x", color="#d32f2f", markersize=7, markeredgewidth=1.6, zorder=7)

    if cut:
        # Where the trace stopped, and a note on the picture. English only: the machine may have no
        # font with Chinese glyphs, and the axes are in English as well.
        stops = [br["segments"][-1] for tree in trace for br in tree["branches"] if br["end"] == "budget" and br["segments"]]
        if stops:
            ax.plot([stops[0]["x1"]], [stops[0]["y1"]], linestyle="none", marker="s", markerfacecolor="#d32f2f",
                    markeredgecolor="white", markersize=8, markeredgewidth=1.0, zorder=9)
        ax.text(0.01, 0.99, f"TRACE CUT after {cut['events']} events (the limit): the beams beyond the red square are not drawn",
                transform=ax.transAxes, fontproperties=fp, fontsize=7, color="#b71c1c", ha="left", va="top", zorder=10, parse_math=False,
                bbox=dict(boxstyle="round,pad=0.25", facecolor="white", edgecolor="#d32f2f", alpha=0.9))

    for tx in lay.texts:
        ax.text(tx.x_mm, tx.y_mm, _text(tx.text), fontproperties=fp, fontsize=7 * tx.size, color="#37474f", zorder=8,
                parse_math=False)
    ax.set_title(_text(f"{lay.title or lay.id}  (rev {lay.revision})"), fontproperties=fp, fontsize=9, loc="left", parse_math=False)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=_DPI)
    return buf.getvalue()


__all__ = ["DEFAULT_WIDTH_PX", "MAX_WIDTH_PX", "MIN_WIDTH_PX", "render_png", "wavelength_color"]
