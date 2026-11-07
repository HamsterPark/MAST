"""The PNG of a layout: it is made with matplotlib's Agg canvas alone, so it needs no display.

Pinned: the file is a PNG of the asked size (clamped), every fixture draws, the picture changes
when the layout or the zoom does, Chinese labels never produce an exception (and never empty boxes
when the machine has no CJK font), rendering is repeatable and thread-safe, and a fresh process with
no display and a bogus backend setting renders without ever importing ``pyplot``.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mast.optical_layout import render as R
from mast.optical_layout.model import Layout
from mast.optical_layout.render import render_png, wavelength_color

from ._layouts import budget_layout, comp, dense_field, det, laser, layout, mirror

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "optical_layout"
REPO = Path(__file__).resolve().parents[4]


def size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", png[16:24])


def simple() -> dict:
    return layout([laser("las1", 50, 62.5), mirror("m1", 112.5, 62.5, 135), det("d", 112.5, 300, 270)], title="Simple")


def test_the_picture_is_a_png_of_the_width_asked_for_in_the_proportions_of_the_table():
    png = render_png(simple(), width_px=800)
    w, h = size(png)
    assert w == 800
    # a 1200 x 900 mm table (+ 6 % padding each side and the margins of the axes): 3 : 4
    assert 0.72 < h / w < 0.95


@pytest.mark.parametrize("asked, got", [(50, 200), (200, 200), (3000, 3000), (99999, 3000), ("700", 700)])
def test_the_width_is_clamped(asked, got):
    assert size(render_png(simple(), width_px=asked))[0] == got


def test_every_fixture_draws():
    for path in sorted(FIXTURES.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        png = render_png(doc["layout"], doc["expected_trace"], width_px=500)
        assert size(png)[0] == 500 and len(png) > 2000, path.name


def test_a_different_layout_gives_a_different_picture_and_the_same_one_the_same_bytes():
    a, b = render_png(simple(), width_px=600), render_png(simple(), width_px=600)
    assert a == b, "rendering is deterministic"
    moved = simple()
    moved["components"][1]["angle_deg"] = 100
    assert render_png(moved, width_px=600) != a
    assert render_png(simple(), width_px=600, fit="content") != a
    assert render_png(simple(), width_px=600, labels=False) != a


def test_content_zoom_shows_the_work_not_the_empty_table():
    wide = size(render_png(simple(), width_px=800, fit="table"))
    zoom = size(render_png(simple(), width_px=800, fit="content"))
    assert wide[0] == zoom[0] == 800
    # the work is a tall strip (x 50-112, y 62-300): the zoom takes its proportions, not the table's
    assert zoom[1] / zoom[0] > 1.5 > wide[1] / wide[0]
    flat = layout([laser("las1", 50, 100), mirror("m1", 450, 100, 135), det("d", 450, 140, 270)])
    assert size(render_png(flat, width_px=800, fit="content"))[1] / 800 < 0.7
    # a layout with nothing in it falls back to the whole table
    assert size(render_png(layout([]), width_px=500, fit="content")) == size(render_png(layout([]), width_px=500, fit="table"))


def test_the_view_of_a_zoom_covers_everything_and_stays_inside_the_drawing():
    lay = Layout.model_validate(simple())
    x0, x1, y0, y1 = R._view(lay, [], "content")
    for c in lay.components:
        assert x0 <= c.x_mm <= x1 and y0 <= c.y_mm <= y1
    wx0, wx1, wy0, wy1 = R._view(lay, [], "table")
    assert wx0 <= x0 and x1 <= wx1 and wy0 <= y0 and y1 <= wy1
    far = Layout.model_validate(layout([laser("las1", 50, 62.5), det("d", 1190, 890, 180)]))
    fx0, fx1, fy0, fy1 = R._view(far, [], "content")
    assert (fx1 - fx0) > 1000 and fx0 >= wx0 and fx1 <= wx1


def test_chinese_and_odd_labels_never_raise(monkeypatch):
    lay = layout([laser("las1", 50, 62.5, label="激光器 L1 λ=532"), mirror("m1", 112.5, 62.5, 135, label="反射镜<&>$"),
                  det("d", 112.5, 300, 270, label="")], title="光路 SHG 激发",
                 texts=[{"id": "t1", "x_mm": 300, "y_mm": 400, "text": "安全门 / door", "size": 2}])
    assert size(render_png(lay, width_px=600))[0] == 600
    # a machine without any CJK font: the characters become "?" instead of empty boxes
    from matplotlib import font_manager

    def no_font(*a, **k):
        raise ValueError("no such font")

    R._font.cache_clear()
    try:
        with monkeypatch.context() as m:
            m.setattr(font_manager, "findfont", no_font)
            assert R._font()[1] is False
            assert R._text("激光器 L1 λ") == "??? L1 λ"
            assert size(render_png(lay, width_px=600))[0] == 600
    finally:
        R._font.cache_clear()


@pytest.mark.parametrize("label", ["$\frac{$", "$$", "$x$ and $\badcommand$", "{{}}", "\\", "a_b^c", "100% $"])
def test_a_label_is_plain_text_and_never_mathtext(label):
    """matplotlib reads ``$...$`` as math and raises on a broken one: a label must not be able to
    make the whole drawing fail."""
    lay = layout([laser("las1", 50, 62.5, label=label), mirror("m1", 112.5, 62.5, 135, label=label)], title=label,
                 texts=[{"id": "t1", "x_mm": 10, "y_mm": 10, "text": label, "size": 1}])
    assert size(render_png(lay, width_px=400))[0] == 400


def test_the_beam_colours_follow_the_wavelength():
    for nm, expect in ((450, "b"), (532, "g"), (633, "r")):
        rgb, style = wavelength_color(nm)
        assert style == "-" and rgb["rgb".index(expect)] == max(rgb), (nm, rgb)
    assert wavelength_color(1064)[1] == "--" and wavelength_color(355)[1] == "--"
    assert wavelength_color(None) == ((0.35, 0.35, 0.35), "-")
    assert all(0.0 <= v <= 1.0 for nm in range(300, 1100, 7) for v in wavelength_color(nm)[0])


def test_a_big_layout_renders_in_reasonable_time():
    comps = [laser(f"l{i}", 20 + 30 * i, 50) for i in range(30)]
    comps += [comp(f"c{i}", ("mirror", "lens", "beam_splitter", "iris", "detector")[i % 5], 30 + (i * 37) % 1100, 100 + (i * 53) % 780, (i * 29) % 360)
              for i in range(370)]
    started = time.perf_counter()
    png = render_png(layout(comps), width_px=1600)
    assert size(png)[0] == 1600 and time.perf_counter() - started < 20


def test_renders_from_several_threads_are_safe_and_identical():
    expect = render_png(simple(), width_px=500)
    out: list[bytes] = []
    threads = [threading.Thread(target=lambda: out.append(render_png(simple(), width_px=500))) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(out) == 6 and all(o == expect for o in out)


def test_a_fresh_headless_process_renders_without_pyplot_even_if_the_backend_setting_needs_a_display(tmp_path):
    """No display and ``MPLBACKEND`` naming a GUI backend (it would fail the moment pyplot made a
    window): still a PNG, and ``matplotlib.pyplot`` was never loaded."""
    code = (
        "import sys\n"
        "from mast.optical_layout.render import render_png\n"
        "png = render_png({'schema': 1, 'id': 'x', 'components': [\n"
        "  {'id': 'a', 'type': 'laser', 'x_mm': 50, 'y_mm': 60, 'props': {'wavelength_nm': 532}},\n"
        "  {'id': 'b', 'type': 'mirror', 'x_mm': 110, 'y_mm': 60, 'angle_deg': 135}]}, width_px=400)\n"
        "assert png[:8] == b'\\x89PNG\\r\\n\\x1a\\n', png[:8]\n"
        "assert 'matplotlib.pyplot' not in sys.modules, 'pyplot was imported'\n"
        "print('ok', len(png))\n")
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "MPLBACKEND", "MAST_LANG")}
    env.update(PYTHONPATH=str(REPO / "MASTv2"), MPLBACKEND="TkAgg", MPLCONFIGDIR=str(tmp_path))
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120)
    assert done.returncode == 0 and done.stdout.startswith("ok "), done.stderr[-1500:]


# ── a trace the budget cut ───────────────────────────────────────────────────────────────────


def changed_pixels(a: bytes, b: bytes) -> int:
    """How many pixels differ between two PNGs of the same size."""
    import io

    import matplotlib.image as mpimg

    x, y = mpimg.imread(io.BytesIO(a)), mpimg.imread(io.BytesIO(b))
    assert x.shape == y.shape
    return int((x != y).any(axis=-1).sum())


def test_a_cut_trace_is_marked_on_the_picture():
    """The same trace with only its last ``end`` changed from ``budget`` to something else draws the
    same beams and none of the mark: the red square where it stopped and the note."""
    import copy

    from mast.optical_layout.trace import trace_layout

    lay = budget_layout()
    cut = trace_layout(lay, max_trace_events=3)
    assert cut[-1]["branches"][-1]["end"] == "budget"
    unmarked = copy.deepcopy(cut)
    unmarked[-1]["branches"][-1]["end"] = "left_table"
    marked_png, plain_png = render_png(lay, cut, width_px=900), render_png(lay, unmarked, width_px=900)
    assert size(marked_png) == size(plain_png)
    assert changed_pixels(marked_png, plain_png) > 500
    assert render_png(lay, unmarked, width_px=900) == plain_png          # and drawing is repeatable


def test_a_trace_of_thousands_of_events_draws_its_beams_in_a_few_collections(monkeypatch):
    """One matplotlib artist per branch made a budget-sized trace take 5 s; the beams go into two
    collections (solid and dashed) now."""
    from matplotlib.axes import Axes

    from mast.optical_layout.trace import trace_layout

    lay = dense_field()
    trace = trace_layout(lay, max_trace_events=1500)
    assert sum(len(t["branches"]) for t in trace) > 1500
    calls: list[int] = []
    real = Axes.add_collection
    with monkeypatch.context() as m:
        m.setattr(Axes, "add_collection", lambda self, *a, **k: (calls.append(1), real(self, *a, **k))[1])
        started = time.perf_counter()
        png = render_png(lay, trace, width_px=1200)
        elapsed = time.perf_counter() - started
    assert size(png)[0] == 1200
    assert len(calls) <= 6, f"{len(calls)} collections"
    assert elapsed < 15


def test_a_cut_trace_draws_no_near_miss_marks():
    """A component a beam passes just outside of is a miss only when no beam hits it; a trace that stops
    short cannot say that (the beam that would hit it may be the one that was not traced)."""
    import copy

    doc = json.loads((FIXTURES / "miss_aperture.json").read_text(encoding="utf-8"))
    lay, full = doc["layout"], doc["expected_trace"]
    assert doc["expected_misses"]
    cut = copy.deepcopy(full)
    cut[-1]["branches"][-1]["end"] = "budget"
    real = R.near_misses
    try:
        R.near_misses = lambda *a, **k: []
        full_without, cut_without = render_png(lay, full, width_px=700), render_png(lay, cut, width_px=700)
    finally:
        R.near_misses = real
    full_with, cut_with = render_png(lay, full, width_px=700), render_png(lay, cut, width_px=700)
    assert full_with != full_without, "the complete trace draws the miss"
    assert cut_with == cut_without, "the cut one does not"
