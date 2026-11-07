"""mast.vista frames: display scale, exact coordinate transforms, flattening, rendering, loading.

The coordinate tests pin ``display_to_scan_nm`` to STM-Bench's ``pixel_to_scan_nm`` (the function
its judge's footprint test pins), restated below as :func:`_pixel_to_scan_nm` so MAST needs no
STM-Bench, and to its own inverse, rotated frames included. Loading tests write small ``.sxm`` /
``.dat`` files into ``tmp_path`` and read them with MAST's readers.
"""
from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
import pytest

from mast.vista import frames as F


# ── helpers shared with the other vista tests ──────────────────────────────
def write_sxm(path: Path, channels: dict[str, tuple[np.ndarray, np.ndarray | None]], *, w_nm: float = 20.0,
              h_nm: float = 20.0, cx_nm: float = 0.0, cy_nm: float = 0.0, angle_deg: float = 0.0,
              scan_dir: str = "down", bias_v: float = 0.1, setpoint_a: float = 1e-10,
              units: dict[str, str] | None = None, status_line: str | None = "ON") -> Path:
    """A minimal controller ``.sxm``: ``channels`` maps a name to ORIENTED (forward, backward)
    arrays (row 0 = top edge, backward un-mirrored); the file stores them the way the
    controller does (acquisition order, backward mirrored)."""
    units = dict(units or {})
    first = next(iter(channels.values()))[0]
    ny, nx = first.shape
    lines = [":NANONIS_VERSION:", "2", ":SCANIT_TYPE:", "              FLOAT            MSBFIRST",
             ":REC_DATE:", " 01.09.2026", ":REC_TIME:", "09:49:40", ":SCAN_PIXELS:", f"{nx:>10d}{ny:>10d}",
             ":SCAN_RANGE:", f"{w_nm * 1e-9:>19.6E}{h_nm * 1e-9:>19.6E}",
             ":SCAN_OFFSET:", f"{cx_nm * 1e-9:>19.6E}{cy_nm * 1e-9:>19.6E}",
             ":SCAN_ANGLE:", f"{angle_deg:>19.6E}", ":SCAN_DIR:", scan_dir, ":BIAS:", f"{bias_v:.6E}",
             ":Z-CONTROLLER:", "\tName\ton\tSetpoint\tP-gain\tI-gain\tT-const",
             f"\tlog Current\t{0 if status_line == 'OFF' else 1}\t{setpoint_a:.4E} A\t3.0E-12 m\t5.0E-08 m/s\t6.0E-05 s",
             ":Z-Controller>Setpoint:", f"{setpoint_a:.6E}"]
    if status_line is not None:
        lines += [":Z-Controller>Controller status:", status_line]
    lines += [":DATA_INFO:", "\tChannel\tName\tUnit\tDirection\tCalibration\tOffset"]
    blobs = []
    for i, (name, (fwd, bwd)) in enumerate(channels.items()):
        unit = units.get(name, {"Z": "m", "Current": "A"}.get(name, "V"))
        lines.append(f"\t{i}\t{name}\t{unit}\t{'both' if bwd is not None else 'fwd'}\t1.000E+0\t0.000E+0")
        for is_bwd, arr in ((False, fwd), (True, bwd)):
            if arr is None:
                continue
            a = np.asarray(arr, dtype=float)
            if is_bwd:
                a = np.fliplr(a)
            if scan_dir == "up":
                a = np.flipud(a)
            blobs.append(np.ascontiguousarray(a, dtype=">f4").tobytes())
    lines += [":SCANIT_END:", "", ""]
    path.write_bytes("\n".join(lines).encode("ascii") + b"\x1a\x04" + b"".join(blobs))
    return path


def write_dat(path: Path, *, n: int = 21) -> Path:
    v = np.linspace(-0.5, 0.5, n)
    rows = ["Experiment\tbias spectroscopy\t", "X (m)\t1.250000E-09\t", "Y (m)\t-2.000000E-09\t",
            "Bias>Bias (V)\t5.000000E-01\t", "Z-Controller>Setpoint\t1.000000E-10\t", "", "[DATA]",
            "Bias calc (V)\tCurrent (A)\tLI Demod 1 X (A)"]
    for x in v:
        rows.append(f"{x:.6E}\t{x * 2e-10:.6E}\t{1e-12 + 1e-13 * x:.6E}")
    path.write_text("\n".join(rows) + "\n", encoding="ascii")
    return path


def _decode(png: bytes) -> np.ndarray:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        return np.asarray(im.convert("RGB")).copy()


def _pixel_to_scan_nm(geom: dict, col: float, row: float) -> tuple[float, float]:
    """STM-Bench's ``stmbench.human.render.pixel_to_scan_nm``, restated: oriented-array pixel
    (``col`` along the fast axis, ``row`` 0 = the frame's high-v edge, centres at half-integers,
    fractional allowed) -> scan-frame nm."""
    w, h = float(geom["w_nm"]), float(geom["h_nm"])
    nx, ny = int(geom["nx"]), int(geom["ny"])
    u = -w / 2 + (float(col) + 0.5) * w / nx
    v = h / 2 - (float(row) + 0.5) * h / ny
    a = math.radians(float(geom.get("angle_deg", 0.0) or 0.0))
    c, s = math.cos(a), math.sin(a)
    return float(geom["cx_nm"]) + c * u - s * v, float(geom["cy_nm"]) + s * u + c * v


# ── display scale ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("nx,ny,dm,k,size", [
    (384, 384, 768, 2.0, (768, 768)), (512, 512, 768, 1.0, (512, 512)), (256, 256, 768, 3.0, (768, 768)),
    (96, 96, 768, 8.0, (768, 768)), (128, 128, 768, 6.0, (768, 768)), (200, 100, 768, 3.0, (600, 300)),
    (1024, 1024, 768, 0.5, (512, 512)), (2048, 2048, 768, 1 / 3, (683, 683)), (1000, 10, 768, 0.5, (500, 5)),
    (768, 768, 768, 1.0, (768, 768)), (769, 769, 768, 0.5, (385, 385)), (100, 100, 512, 5.0, (500, 500)),
])
def test_display_scale_rule(nx, ny, dm, k, size):
    assert F.display_scale(nx, ny, dm) == pytest.approx(k)
    assert F.display_size(nx, ny, dm) == size
    num, den = F.scale_pair(nx, ny, dm)
    assert (num == 1 or den == 1) and num >= 1 and den >= 1
    assert max(F.display_size(nx, ny, dm)) <= dm


def test_scale_text():
    assert F.scale_text(2, 1) == "2" and F.scale_text(1, 3) == "1/3"


# ── coordinates ─────────────────────────────────────────────────────────────
def _random_geom(rng) -> dict:
    nx, ny = int(rng.integers(16, 1500)), int(rng.integers(16, 1500))
    return {"cx_nm": float(rng.uniform(-500, 500)), "cy_nm": float(rng.uniform(-500, 500)),
            "w_nm": float(rng.uniform(0.5, 500)), "h_nm": float(rng.uniform(0.5, 500)),
            "angle_deg": float(rng.uniform(-180, 180)), "nx": nx, "ny": ny}


def test_display_scan_roundtrip_property():
    rng = np.random.default_rng(1234)
    for _ in range(300):
        g = _random_geom(rng)
        dm = int(rng.choice([256, 512, 768, 1024]))
        W, H = F.display_size(g["nx"], g["ny"], dm)
        x, y = rng.uniform(0, W, 25), rng.uniform(0, H, 25)
        X, Y = F.display_to_scan_nm(g, x, y, display_max=dm)
        x2, y2 = F.scan_nm_to_display(g, X, Y, display_max=dm)
        scale = max(W, H)
        assert np.max(np.abs(x2 - x)) < 1e-9 * scale
        assert np.max(np.abs(y2 - y)) < 1e-9 * scale
        # and the other way round, from scan nm
        Xs = g["cx_nm"] + rng.uniform(-1, 1, 10) * g["w_nm"]
        Ys = g["cy_nm"] + rng.uniform(-1, 1, 10) * g["h_nm"]
        xd, yd = F.scan_nm_to_display(g, Xs, Ys, display_max=dm)
        X2, Y2 = F.display_to_scan_nm(g, xd, yd, display_max=dm)
        tol = 1e-9 * max(1.0, abs(g["cx_nm"]) + abs(g["cy_nm"]) + g["w_nm"] + g["h_nm"])
        assert np.max(np.abs(X2 - Xs)) < tol and np.max(np.abs(Y2 - Ys)) < tol


def test_display_to_scan_matches_stm_bench_pixel_to_scan_nm():
    rng = np.random.default_rng(7)
    for _ in range(200):
        g = _random_geom(rng)
        num, den = F.scale_pair(g["nx"], g["ny"], 768)
        k = num / den
        W, H = F.display_size(g["nx"], g["ny"], 768)
        x, y = float(rng.uniform(0, W)), float(rng.uniform(0, H))
        X, Y = F.display_to_scan_nm(g, x, y)
        Xr, Yr = _pixel_to_scan_nm(g, x / k - 0.5, y / k - 0.5)
        assert abs(X - Xr) < 1e-9 and abs(Y - Yr) < 1e-9


def test_frame_centre_and_corners():
    g = {"cx_nm": 12.0, "cy_nm": -7.0, "w_nm": 70.0, "h_nm": 35.0, "angle_deg": 30.0, "nx": 384, "ny": 192}
    W, H = F.display_size(384, 192, 768)
    assert (W, H) == (768, 384)
    X, Y = F.display_to_scan_nm(g, W / 2, H / 2)
    assert X == pytest.approx(12.0, abs=1e-12) and Y == pytest.approx(-7.0, abs=1e-12)
    # the top-left corner of the image is the frame's (-w/2, +h/2) corner, rotated
    X0, Y0 = F.display_to_scan_nm(g, 0.0, 0.0)
    a = math.radians(30.0)
    u, v = -35.0, 17.5
    assert X0 == pytest.approx(12.0 + math.cos(a) * u - math.sin(a) * v, abs=1e-12)
    assert Y0 == pytest.approx(-7.0 + math.sin(a) * u + math.cos(a) * v, abs=1e-12)
    # x to the right moves along +u; y down moves along -v
    X1, Y1 = F.display_to_scan_nm(g, 2.0, 0.0)
    assert (X1 - X0) == pytest.approx(math.cos(a) * 70.0 / 384, abs=1e-12)
    Xd, Yd = F.display_to_scan_nm(g, 0.0, 2.0)
    assert (Yd - Y0) == pytest.approx(-math.cos(a) * 35.0 / 192, abs=1e-12)


def test_coordinates_use_entry_display_scale():
    class E:
        geometry = {"cx_nm": 0.0, "cy_nm": 0.0, "w_nm": 10.0, "h_nm": 10.0, "angle_deg": 0.0, "nx": 1024, "ny": 1024}
        display_scale = 0.5

    X, Y = F.display_to_scan_nm(E(), 256.0, 256.0)
    assert (X, Y) == pytest.approx((0.0, 0.0))
    X, Y = F.display_to_scan_nm(E(), 0.0, 0.0)
    assert (X, Y) == pytest.approx((-5.0, 5.0))


def test_no_geometry_is_an_error():
    with pytest.raises(ValueError):
        F.display_to_scan_nm({}, 1.0, 1.0)


def test_frame_geometry_from_header():
    """The mapping inlined from STM-Bench's ``frame_geometry``: header strings or lists, m -> nm."""
    hdr = {"scan_range": "1.600000E-8 1.200000E-8", "scan_offset": "5.0E-9 -3.0E-9",
           "scan_pixels": "16 12", "scan_angle": "1.500000E+1"}
    assert F.frame_geometry(hdr) == pytest.approx({"cx_nm": 5.0, "cy_nm": -3.0, "w_nm": 16.0, "h_nm": 12.0,
                                                   "angle_deg": 15.0, "nx": 16, "ny": 12})
    square = F.frame_geometry({"scan_range": [2e-8], "scan_offset": [0.0, 0.0], "scan_pixels": [64, 32]})
    assert (square["w_nm"], square["h_nm"], square["angle_deg"]) == pytest.approx((20.0, 20.0, 0.0))
    assert (square["nx"], square["ny"]) == (64, 32)
    assert F.frame_geometry({"scan_range": "1e-8", "scan_offset": "0", "scan_pixels": "16 16"}) is None
    assert F.frame_geometry({}) is None


# ── flattening ──────────────────────────────────────────────────────────────
def test_flatten_modes():
    ny, nx = 64, 80
    yy, xx = np.mgrid[0:ny, 0:nx]
    plane = 3.0 + 0.2 * xx - 0.1 * yy
    assert np.max(np.abs(F.flatten(plane, "plane"))) < 1e-9
    rows = plane + np.repeat(np.random.default_rng(0).normal(size=(ny, 1)), nx, axis=1)
    assert np.max(np.abs(F.flatten(rows, "line"))) < 1e-9
    quad = 1.0 + 0.01 * xx ** 2 - 0.02 * xx * yy + 0.03 * yy ** 2 + xx
    assert np.max(np.abs(F.flatten(quad, "poly2"))) < 1e-7
    assert np.allclose(F.flatten(quad, "none"), quad)
    bump = 0.002 * xx * yy + 5.0 * np.exp(-((xx - 40) ** 2 + (yy - 32) ** 2) / 4.0)
    hp = F.flatten(bump, "highpass", nm_per_px=0.1, highpass_nm=2.0)
    assert hp[32, 40] > 4.0                                   # the small bump survives
    assert abs(float(np.median(hp))) < 0.05                    # the broad background is gone


def test_flatten_keeps_nan_rows_and_validates():
    z = np.random.default_rng(1).normal(size=(20, 20))
    z[12:] = np.nan
    for mode in F.FLATTEN_MODES:
        out = F.flatten(z, mode, nm_per_px=0.1)
        assert np.isnan(out[12:]).all() and np.isfinite(out[:12]).all()
    with pytest.raises(ValueError):
        F.flatten(z, "bogus")
    with pytest.raises(ValueError):
        F.flatten(z, "highpass")


# ── rendering ───────────────────────────────────────────────────────────────
def test_render_integer_blocks_nan_colour_and_contrast():
    z = np.arange(12, dtype=float).reshape(3, 4)
    z[2] = np.nan
    view = F.plan_view(4, 3, 2, 1, None, 8)          # display_max 8 -> 2 image px per scan px
    assert view.image_size == (8, 6) and view.up == 2
    png, lo, hi = F.render_values(z, view, clip_pct=0.0)
    img = _decode(png)
    assert img.shape == (6, 8, 3)
    assert (lo, hi) == (0.0, 7.0)
    for r in range(3):
        for c in range(4):
            block = img[2 * r:2 * r + 2, 2 * c:2 * c + 2].reshape(-1, 3)
            assert (block == block[0]).all()                 # nearest neighbour, never smoothed
    assert tuple(img[5, 0]) == F.NAN_RGB                      # rows never acquired
    assert tuple(img[0, 0]) == (0, 0, 0) and tuple(img[3, 7]) == (255, 255, 255)


def test_render_block_mean_for_large_frames():
    z = np.zeros((1024, 1024))
    z[:, 512:] = 1.0
    num, den = F.scale_pair(1024, 1024, 768)
    view = F.plan_view(1024, 1024, num, den, None, 768)
    assert (view.down, view.image_size) == (2, (512, 512))
    img = _decode(F.render_values(z, view)[0])
    assert img[0, 0, 0] == 0 and img[0, 511, 0] == 255


# ── grid (graph paper) ──────────────────────────────────────────────────────
@pytest.mark.parametrize("x, step", [(0.3, 0.5), (0.5, 0.5), (0.51, 1.0), (1.0, 1.0), (1.2, 2.0), (2.0, 2.0),
                                     (2.01, 5.0), (4.375, 5.0), (7.0, 10.0), (0.0137, 0.02), (1e-3, 1e-3),
                                     (1000.0, 1000.0)])
def test_nice_step(x, step):
    assert F.nice_step(x) == step


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_nice_step_rejects_non_positive(bad):
    with pytest.raises(ValueError):
        F.nice_step(bad)


def test_view_grid_spacing_follows_the_view():
    px = 70 / 256
    full = F.plan_view(256, 256, 3, 1, None, 768)
    assert F.view_grid(full, px, px).spacing_nm == 5.0             # 70 nm / 16 = 4.4 -> 5 nm, 14 cells
    zoom = F.plan_view(256, 256, 3, 1, (300, 300, 96, 96), 768)
    assert F.view_grid(zoom, px, px).spacing_nm == 1.0             # 8.75 nm / 16 = 0.55 -> 1 nm
    tiny = F.plan_view(256, 256, 3, 1, (300, 300, 9, 9), 768)      # 3 scan px: cells of >= 2 px (0.55 nm)
    assert F.view_grid(tiny, px, px).spacing_nm == 1.0
    tall = F.plan_view(64, 512, 1, 1, None, 768)                   # the longer side sets the spacing
    assert F.view_grid(tall, 0.1, 0.1).spacing_nm == 5.0           # 51.2 nm / 16 = 3.2 -> 5 nm


def test_grid_lines_are_even_and_counted_from_the_frame_corner():
    px = 70 / 256
    full = F.plan_view(256, 256, 3, 1, None, 768)
    xs = F.grid_lines(0, 256, 5.0 / px, full.up, full.down, 768)
    assert xs[:3] == [0, 54, 109] and len(xs) == 14 and set(np.diff(xs)) <= {54, 55}
    # a zoomed view continues the frame's grid: the region starts at scan px 100 = 27.34 nm, so the
    # first line is 28 nm = scan px 102.4 = image px (102.4 - 100) * 24 = 57.6 -> 57
    zoom = F.plan_view(256, 256, 3, 1, (300, 300, 96, 96), 768)
    i0, _, i1, _ = zoom.native
    xs = F.grid_lines(i0, i1, 1.0 / px, zoom.up, zoom.down, 768)
    assert xs[0] == 57 and set(np.diff(xs)) <= {87, 88}
    # downsampled: a 1024 px, 100 nm frame at half size has 10 nm lines every 51.2 image px
    big = F.plan_view(1024, 1024, 1, 2, None, 768)
    xs = F.grid_lines(0, 1024, 10.0 / (100 / 1024), big.up, big.down, 512)
    assert xs[:3] == [0, 51, 102] and len(xs) == 10 and set(np.diff(xs)) <= {51, 52}


def test_draw_grid_blends_line_pixels_a_quarter_toward_amber_and_nothing_else():
    z = np.ones((32, 32))
    z[:, 16:] = 0.0
    z[30:] = np.nan
    view = F.plan_view(32, 32, 24, 1, None, 768)                   # 24 image px per scan px
    grid = F.Grid(1.0, 0.5, 0.5)                                   # every 2 scan px = 48 image px
    plain = _decode(F.render_values(z, view, clip_pct=0.0)[0]).astype(int)
    lined = _decode(F.render_values(z, view, clip_pct=0.0, grid=grid)[0]).astype(int)
    lines = list(range(0, 768, 48))
    assert F.grid_lines(0, 32, 2.0, 24, 1, 768) == lines
    on = np.zeros((768, 768), dtype=bool)
    on[:, lines] = True
    on[lines, :] = True
    assert (lined[~on] == plain[~on]).all()
    assert (lined[on] == (3 * plain[on] + np.asarray(F.GRID_RGB)) // 4).all()
    assert tuple(lined[0, 0]) == ((3 * 255 + 255) // 4, (3 * 255 + 176) // 4, (3 * 255) // 4)
    assert tuple(lined[48, 400]) == (255 // 4, 176 // 4, 0)                    # on black
    assert tuple(lined[48 * 15, 5]) == tuple((3 * np.asarray(F.NAN_RGB) + np.asarray(F.GRID_RGB)) // 4)
    assert F.render_values(z, view)[0] == F.render_values(z, view, grid=None)[0]


def test_plan_view_region_snapping_and_magnification():
    # 384 px frame at k=2: an odd display region widens to whole scan pixels
    v = F.plan_view(384, 384, 2, 1, (101, 51, 63, 31), 768)
    assert v.native == (50, 25, 82, 41)
    assert v.region_px == (100, 50, 64, 32)
    assert v.up == 768 // 32 and v.image_size == (32 * 24, 16 * 24)
    assert v.magnification == pytest.approx(12.0)
    # whole frame = the observation size
    assert F.plan_view(384, 384, 2, 1, None, 768).image_size == (768, 768)
    # downsampled display: a region maps to native exactly; a small one is shown at full resolution
    v = F.plan_view(2048, 2048, 1, 3, (10, 20, 100, 50), 768)
    assert v.native == (30, 60, 330, 210) and v.up == 2 and v.image_size == (600, 300)
    with pytest.raises(ValueError):
        F.plan_view(384, 384, 2, 1, (700, 0, 100, 10), 768)


def test_visual_label_escaping():
    lab = F.visual_label("inspection", label='a "quoted" <b> & c', frame="t0001.s0", none=None)
    assert lab.startswith('<visual kind="inspection"') and lab.endswith("/>")
    assert "&quot;quoted&quot;" in lab and "&lt;b&gt;" in lab and "&amp;" in lab and "none=" not in lab


# ── channels and units ─────────────────────────────────────────────────────
def test_default_channel_rules():
    flat = np.full((8, 8), 1e-9)
    varied = np.random.default_rng(0).normal(0, 1e-11, (8, 8))
    cur = np.random.default_rng(1).normal(1e-11, 1e-12, (8, 8))
    df = np.random.default_rng(2).normal(-3.0, 0.2, (8, 8))
    assert F.default_channel(["Z", "Current"], {"Z/forward": varied, "Current/forward": cur}) == "Z"
    assert F.default_channel(["Z", "Current"], {"Z/forward": flat, "Current/forward": cur}) == "Current"
    assert F.default_channel(["Z", "OC_M1_Freq._Shift"], {"Z/forward": flat, "OC_M1_Freq._Shift/forward": df}) \
        == "OC_M1_Freq._Shift"
    # sub-pm variation still counts as constant height
    assert F.default_channel(["Z", "Current"], {"Z/forward": flat + varied * 0.01, "Current/forward": cur}) == "Current"


def test_display_units_and_rounding():
    assert F.display_unit("m") == (1e12, "pm")
    assert F.display_unit("A") == (1e12, "pA")          # one unit per quantity, however small
    assert F.display_unit("Hz") == (1.0, "Hz")
    assert F.display_unit("V") == (1.0, "V")
    assert F.display_unit("furlong") == (1.0, "furlong")
    assert [F.unit_suffix(u) for u in ("pm", "pA", "Hz", "V", "deg", "")] == ["pm", "pa", "hz", "v", "deg", ""]
    assert F.unit_key("height", "pm") == "height_pm" and F.unit_key("values", "pA") == "values_pa"
    assert F.unit_key("ratio", "") == "ratio"
    assert [F.decimals_for(v) for v in (500.0, 50.0, 4.0, 0.4, 1e-9, 0.0, None, float("nan"))] == [1, 1, 2, 3, 6, 1, 1, 1]
    assert F.round_to(12.345, 1) == 12.3 and F.round_to(-0.04, 1) == 0.0 and str(F.round_to(-0.04, 1)) == "0.0"
    assert F.round_to(float("nan"), 2) is None and F.round_to(None, 2) is None
    assert F.robust_span(np.array([0.0, 1.0, np.nan]), np.array([2.0])) == pytest.approx(1.99, abs=0.02)
    assert F.range_attr("Z", "pm") == "z_range_pm" and F.range_attr("Current", "pA") == "range_pa"


def test_resolve_channel_aliases():
    chans = ["Z", "Current", "OC_M1_Freq._Shift", "LI Demod 1 X (A)"]
    assert F.resolve_channel("z", chans) == "Z"
    assert F.resolve_channel("current", chans) == "Current"
    assert F.resolve_channel("df", chans) == "OC_M1_Freq._Shift"
    assert F.resolve_channel("oc m1 freq shift", chans) == "OC_M1_Freq._Shift"
    assert F.resolve_channel("LI Demod 1 X", chans) == "LI Demod 1 X (A)"
    with pytest.raises(KeyError, match="this frame has"):
        F.resolve_channel("dIdV", chans)


def test_column_unit():
    assert F.column_unit("Current (A)") == "A"
    assert F.column_unit("Current [bwd] (A)") == "A"
    assert F.column_unit("Index") == ""


def test_scan_from_arrays_validation():
    a = np.zeros((4, 6))
    d = F.scan_from_arrays({"Z": a, "Current/backward": a},
                           geometry={"cx_nm": 0, "cy_nm": 0, "w_nm": 6, "h_nm": 4}, meta={})
    assert d.channels == ["Z", "Current"] and d.directions("Current") == ["backward"]
    assert d.geometry["nx"] == 6 and d.nm_per_px == pytest.approx(1.0)
    with pytest.raises(ValueError):
        F.scan_from_arrays({"Z/sideways": a}, geometry={"cx_nm": 0, "cy_nm": 0, "w_nm": 6, "h_nm": 4})
    with pytest.raises(ValueError):
        F.scan_from_arrays({"Z": a, "I": np.zeros((5, 6))}, geometry={"cx_nm": 0, "cy_nm": 0, "w_nm": 6, "h_nm": 4})
    with pytest.raises(ValueError):
        F.scan_from_arrays({"Z": a}, geometry={"cx_nm": 0, "w_nm": 6, "h_nm": 4})


def test_render_spectrum_plot():
    x = np.linspace(-0.5, 0.5, 51)
    png, meta = F.render_spectrum({"Bias (V)": x, "Current (A)": x * 1e-10}, {"Bias (V)": "V", "Current (A)": "A"},
                                  x_col="Bias (V)", y_cols=["Current (A)"], width_px=640)
    W, H = F.png_size(png)
    assert W >= 600 and meta["x_unit"] == "V" and meta["y"][0]["unit"] == "pA"
    _, meta = F.render_spectrum({"Bias (V)": x, "Current (A)": x * 1e-10}, {"Bias (V)": "V", "Current (A)": "A"},
                                x_col="Bias (V)", y_cols=["Current (A)"], x_range=(0.0, 0.2))
    assert meta["n_points"] == 11
    with pytest.raises(ValueError, match="contains no samples"):
        F.render_spectrum({"Bias (V)": x, "Current (A)": x}, {"Bias (V)": "V", "Current (A)": "A"},
                          x_col="Bias (V)", y_cols=["Current (A)"], x_range=(900.0, 1000.0))


# ── loading real file formats (MAST's reader) ──────────────────────────────
@pytest.mark.parametrize("scan_dir", ["down", "up"])
def test_load_sxm_orientation_units_and_nan_rows(tmp_path, scan_dir):
    ny, nx = 12, 16
    yy, xx = np.mgrid[0:ny, 0:nx]
    z = (yy * 100 + xx) * 1e-12                     # oriented: row 0 = top edge
    zb = z + 5e-12
    cur = np.full((ny, nx), 2e-10)
    z_part, zb_part = z.copy(), zb.copy()
    rows_missing = slice(8, None) if scan_dir == "down" else slice(0, 4)
    z_part[rows_missing] = np.nan
    zb_part[rows_missing] = np.nan
    p = write_sxm(tmp_path / f"f_{scan_dir}.sxm", {"Z": (z_part, zb_part), "Current": (cur, cur)},
                  w_nm=16.0, h_nm=12.0, cx_nm=5.0, cy_nm=-3.0, angle_deg=15.0, scan_dir=scan_dir,
                  units={"Z": "m", "Current": "A"}, status_line=None)
    d = F.load_sxm(p)
    assert d.channels == ["Z", "Current"] and d.units == {"Z": "m", "Current": "A"}
    assert d.geometry == pytest.approx({"cx_nm": 5.0, "cy_nm": -3.0, "w_nm": 16.0, "h_nm": 12.0,
                                        "angle_deg": 15.0, "nx": 16, "ny": 12})
    got = d.arrays["Z/forward"]
    assert got.dtype == np.float32
    np.testing.assert_allclose(got[np.isfinite(got)], z_part[np.isfinite(z_part)], rtol=1e-6)
    assert np.isnan(got[rows_missing]).all()
    np.testing.assert_allclose(d.arrays["Z/backward"][np.isfinite(zb_part)], zb_part[np.isfinite(zb_part)], rtol=1e-6)
    assert d.acquired_rows == 8 and d.scan_dir == scan_dir
    assert d.bias_v == pytest.approx(0.1) and d.setpoint_a == pytest.approx(1e-10)
    assert d.feedback == "ON"                        # from the Z-CONTROLLER table (no status line)
    assert d.nm_per_px == pytest.approx(1.0) and d.setpoint_unit == "A"


def test_load_sxm_constant_height_defaults_to_current(tmp_path):
    flat = np.full((8, 8), 1e-9)
    cur = np.random.default_rng(0).normal(5e-12, 1e-12, (8, 8))
    p = write_sxm(tmp_path / "ch.sxm", {"Z": (flat, flat), "Current": (cur, cur)}, status_line="OFF")
    d = F.load_sxm(p)
    assert F.default_channel(d.channels, d.arrays) == "Current"
    assert d.feedback == "OFF"


def test_load_dat(tmp_path):
    d = F.load_dat(write_dat(tmp_path / "s.dat"))
    assert list(d.columns) == ["Bias calc (V)", "Current (A)", "LI Demod 1 X (A)"]
    assert d.units == {"Bias calc (V)": "V", "Current (A)": "A", "LI Demod 1 X (A)": "A"}
    assert d.x_nm == pytest.approx(1.25) and d.y_nm == pytest.approx(-2.0)
    assert d.experiment == "bias spectroscopy" and d.bias_v == pytest.approx(0.5) and d.setpoint_unit == "A"


def test_missing_files_raise_clear_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        F.load_sxm(tmp_path / "nope.sxm")
    with pytest.raises(FileNotFoundError):
        F.load_dat(tmp_path / "nope.dat")
