"""mast.vista measurement tools on synthetic frames with known answers.

FFT recovers a grating's period and scan-frame angle (rotated frame included); blob detection
recovers Gaussian centres to < 0.2 px and finds elongated blobs without false positives on
noise; diff recovers a known sub-pixel shift (same grid and resampled grid); line_profile
finds a step; stats reports known moments.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pytest
from scipy.ndimage import gaussian_filter, map_coordinates

from mast.vista import frames as F
from mast.vista import measure as M
from mast.vista.archive import Archive
from mast.vista.protocol import ToolReply


def _ok(r: ToolReply) -> dict:
    assert not r.is_error, r.text
    return json.loads(r.text)


def _err(r: ToolReply) -> str:
    assert r.is_error
    return json.loads(r.text)["error"]


def _scan_xy(geom: dict) -> tuple[np.ndarray, np.ndarray]:
    """Scan-frame nm of every pixel centre of an oriented frame."""
    ny, nx = geom["ny"], geom["nx"]
    rr, cc = np.mgrid[0:ny, 0:nx]
    return F.native_to_scan_nm(geom, cc + 0.5, rr + 0.5)


def _geom(nx=256, ny=256, w=25.6, h=25.6, cx=0.0, cy=0.0, angle=0.0) -> dict:
    return {"cx_nm": cx, "cy_nm": cy, "w_nm": w, "h_nm": h, "angle_deg": angle, "nx": nx, "ny": ny}


def _add(arch: Archive, turn: int, z: np.ndarray, geom: dict, **more) -> str:
    arrays = {"Z": z}
    arrays.update(more)
    return arch.add_partial(turn, arrays, geometry=geom, meta={}).fid


# ── tool definitions ────────────────────────────────────────────────────────
def test_measure_tool_defs():
    assert set(M.MEASURE_TOOLS) == {"line_profile", "fft_peaks", "detect_blobs", "diff", "stats"}
    for name, (td, fn) in M.MEASURE_TOOLS.items():
        mcp = td.to_mcp()
        assert mcp["name"] == name and callable(fn)
        assert mcp["inputSchema"]["additionalProperties"] is False
        assert mcp["annotations"]["readOnlyHint"] is True
        assert "without changing the instrument" in td.description
        json.dumps(mcp)
    assert set(M.measure_tools(512)) == set(M.MEASURE_TOOLS)
    assert M.make_fft_peaks_tool(512).input_schema["properties"]["region"]["properties"]["width"]["maximum"] == 512


@pytest.mark.parametrize("dm", [256, 512, 1024])
def test_measure_tools_are_sized_for_the_archive_display_max(dm):
    """A server whose archive renders at another display_max builds its schemas with measure_tools(dm)
    (the module-level MEASURE_TOOLS are sized for 768 and stay so); the implementations are the same."""
    tools = M.measure_tools(dm)
    for name, (td, fn) in tools.items():
        assert fn is M.MEASURE_TOOLS[name][1]
        props = td.input_schema["properties"]
        if "region" in props:
            region = props["region"]["properties"]
            assert (region["x"]["maximum"], region["width"]["maximum"]) == (dm - 1, dm), name
    assert {n for n, (td, _) in tools.items() if "region" in td.input_schema["properties"]} == \
        {"fft_peaks", "detect_blobs", "diff", "stats"}
    assert tools["line_profile"][0].input_schema["properties"]["width_px"]["maximum"] == dm
    assert M.MEASURE_TOOLS["line_profile"][0].input_schema["properties"]["width_px"]["maximum"] == 768
    assert M.MEASURE_TOOLS["stats"][0].input_schema["properties"]["region"]["properties"]["width"]["maximum"] == 768


# ── fft_peaks ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("frame_angle,k_angle,period", [(0.0, 30.0, 2.5), (20.0, 30.0, 2.5), (-35.0, 100.0, 6.3)])
def test_fft_grating_period_and_scan_frame_angle(tmp_path, frame_angle, k_angle, period):
    g = _geom(angle=frame_angle, w=45.0, h=45.0)
    X, Y = _scan_xy(g)
    a = math.radians(k_angle)
    z = 1e-11 * np.cos(2 * math.pi * (X * math.cos(a) + Y * math.sin(a)) / period)
    z += np.random.default_rng(0).normal(0, 2e-12, z.shape)
    arch = Archive(tmp_path)
    fid = _add(arch, 0, z, g)
    r = M.fft_peaks(arch, {"frame": fid, "n_peaks": 3})
    body = _ok(r)
    top = body["peaks"][0]
    assert top["period_nm"] == pytest.approx(period, rel=0.01)
    assert (top["angle_deg"] - k_angle + 90) % 180 - 90 == pytest.approx(0.0, abs=0.5)
    assert top["strength"] > 5
    assert body["image"]["frame"].endswith(".m0") and len(r.images) == 1
    assert r.images[0].label.startswith('<visual kind="derived" frame="t0000.m0" derived="fft_peaks"')


def test_fft_zigzag_herringbone_like(tmp_path):
    """The simulator's herringbone: a 6.3 nm cosine whose stripes zigzag (+-8.5 deg arms)."""
    g = _geom(w=45.0, h=45.0)
    X, Y = _scan_xy(g)
    th = math.radians(105.0)
    u = X * math.cos(th) + Y * math.sin(th)
    v = -X * math.sin(th) + Y * math.cos(th)
    zig = np.sign(np.sin(2 * math.pi * v / 30.0))
    z = 1e-11 * np.cos(2 * math.pi * (u + 0.15 * zig * v) / 6.3)
    z += np.random.default_rng(1).normal(0, 2e-12, z.shape)
    arch = Archive(tmp_path)
    body = _ok(M.fft_peaks(arch, {"frame": _add(arch, 0, z, g)}))
    top = body["peaks"][0]
    assert 6.0 < top["period_nm"] < 6.6 and abs((top["angle_deg"] - 105 + 90) % 180 - 90) < 12


def test_fft_region_and_noise(tmp_path):
    g = _geom()
    noise = np.random.default_rng(3).normal(0, 2e-12, (256, 256))
    arch = Archive(tmp_path)
    body = _ok(M.fft_peaks(arch, {"frame": _add(arch, 0, noise, g)}))
    assert all(p["strength"] < 7 for p in body["peaks"])          # noise maxima stay weak
    X, Y = _scan_xy(g)
    z = noise.copy()
    z[:, 128:] += 2e-11 * np.cos(2 * math.pi * X[:, 128:] / 1.6)  # grating only in the right half
    fid = _add(arch, 1, z, g)
    body = _ok(M.fft_peaks(arch, {"frame": fid, "region": {"x": 384, "y": 0, "width": 384, "height": 768}}))
    assert body["native_px"] == [128, 0, 256, 256] and body["peaks"][0]["period_nm"] == pytest.approx(1.6, rel=0.01)
    assert "8 x 8" in _err(M.fft_peaks(arch, {"frame": fid, "region": {"x": 0, "y": 0, "width": 9, "height": 9}}))


@pytest.mark.parametrize("period_px,alias", [(2.3, False), (1.8, True), (14.0, None)])
def test_fft_marks_peaks_near_the_nyquist_frequency(tmp_path, period_px, alias):
    """A period of 2.3 pixels is resolved but sits at 0.87 of the Nyquist frequency; one of 1.8 pixels
    is not resolved and comes back as its alias (2.25 pixels), at 0.89. Both are near_nyquist: the
    reply cannot tell a fine period from an alias, a smaller pixel can. 14 pixels is far from it."""
    g = _geom(w=25.6, h=25.6)                                    # 0.1 nm per pixel
    X, _ = _scan_xy(g)
    z = 1e-11 * np.cos(2 * math.pi * X / (0.1 * period_px)) + np.random.default_rng(2).normal(0, 1e-12, X.shape)
    arch = Archive(tmp_path)
    body = _ok(M.fft_peaks(arch, {"frame": _add(arch, 0, z, g), "n_peaks": 1}))
    (p,) = body["peaks"]
    assert body["near_nyquist_from"] == 0.8
    if alias is None:
        assert p["near_nyquist"] is False and p["nyquist_fraction"] == pytest.approx(2 / 14.0, abs=0.01)
        return
    seen = 1.0 / (1.0 / 0.1 - 1.0 / (0.1 * period_px)) if alias else 0.1 * period_px   # folds about 1/pixel
    assert p["period_nm"] == pytest.approx(seen, rel=0.02)
    assert p["near_nyquist"] is True and p["nyquist_fraction"] == pytest.approx(0.2 / seen, abs=0.01)


# ── detect_blobs ────────────────────────────────────────────────────────────
def _blobs(ny, nx, centres, sx, sy, h, noise, seed, theta=0.0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:ny, 0:nx]
    z = rng.normal(0, noise, (ny, nx))
    ct, st = math.cos(theta), math.sin(theta)
    for (c, r) in centres:                       # native continuous coords (pixel centre i at i + 0.5)
        dx, dy = xx + 0.5 - c, yy + 0.5 - r
        u, v = dx * ct + dy * st, -dx * st + dy * ct
        z += h * np.exp(-0.5 * ((u / sx) ** 2 + (v / sy) ** 2))
    return z + 3e-9 + 1e-12 * xx                 # offset and a slope the plane fit removes


def test_detect_blobs_recovers_subpixel_positions(tmp_path):
    rng = np.random.default_rng(10)
    centres = [(float(rng.uniform(20, 364)), float(rng.uniform(20, 364))) for _ in range(12)]
    centres = [c for i, c in enumerate(centres) if all(math.dist(c, d) > 15 for d in centres[:i])]
    z = _blobs(384, 384, centres, 2.0, 2.0, 50e-12, 2e-12, 11)
    g = _geom(nx=384, ny=384, w=70.0, h=70.0)
    arch = Archive(tmp_path)
    fid = _add(arch, 0, z, g)
    r = M.detect_blobs(arch, {"frame": fid})
    body = _ok(r)
    assert body["n_blobs"] == len(centres) and "unit" not in body and "measurement" not in body
    k = 2.0
    for c, rw in centres:
        d = min(math.hypot(b["x_px"] / k - c, b["y_px"] / k - rw) for b in body["blobs"])
        assert d < 0.2, (c, rw, d)
    b0 = body["blobs"][0]
    assert b0["height_pm"] == pytest.approx(50.0, abs=3.0) and b0["fit"] == "gaussian"
    assert {"noise_pm", "threshold_smoothed_pm", "min_height_pm"} <= set(body["detection"])
    fwhm_nm = 2.3548 * 2.0 * 70.0 / 384
    assert b0["fwhm_x_nm"] == pytest.approx(fwhm_nm, rel=0.1) and b0["elongation"] < 1.2
    X, Y = F.display_to_scan_nm(arch.get(fid), b0["x_px"], b0["y_px"])
    assert (b0["x_nm"], b0["y_nm"]) == pytest.approx((X, Y), abs=1e-3)
    ys = [b["y_px"] for b in body["blobs"]]
    assert all(b >= a - 4.0 for a, b in zip(ys, ys[1:], strict=False))   # numbered in reading order
    assert [b["id"] for b in body["blobs"]] == list(range(1, len(centres) + 1))
    assert len(r.images) == 1 and 'derived="detect_blobs"' in r.images[0].label


def test_detect_blobs_elongated_and_no_false_positives(tmp_path):
    rng = np.random.default_rng(4)
    centres = []
    while len(centres) < 16:
        c = (float(rng.uniform(15, 369)), float(rng.uniform(15, 369)))
        if all(math.dist(c, d) > 20 for d in centres):
            centres.append(c)
    z = _blobs(384, 384, centres, 3.3, 1.2, 35e-12, 2e-12, 5)       # elongated blobs, like a survey frame
    g = _geom(nx=384, ny=384, w=70.0, h=70.0)
    arch = Archive(tmp_path)
    body = _ok(M.detect_blobs(arch, {"frame": _add(arch, 0, z, g)}))
    assert body["n_blobs"] == 16
    for b in body["blobs"]:
        assert 2.2 < b["elongation"] < 3.3
        assert min(b["axis_angle_deg"], 180 - b["axis_angle_deg"]) < 5     # along the fast axis
    noise = np.random.default_rng(6).normal(0, 2e-12, (384, 384))
    body = _ok(M.detect_blobs(arch, {"frame": _add(arch, 1, noise, g)}))
    assert body["n_blobs"] == 0


def test_detect_blobs_lists_at_most_50(tmp_path):
    """max_n is capped at 50 (a cap of 200 gave a 45 KB reply)."""
    assert M.DETECT_BLOBS_TOOL.input_schema["properties"]["max_n"]["maximum"] == 50
    arch = Archive(tmp_path)
    fid = _add(arch, 0, np.random.default_rng(0).normal(0, 1e-12, (64, 64)), _geom(nx=64, ny=64))
    assert "must be <= 50" in _err(M.detect_blobs(arch, {"frame": fid, "max_n": 51}))
    assert _ok(M.detect_blobs(arch, {"frame": fid, "max_n": 50}))["n_blobs"] == 0


def test_detect_blobs_axis_angle_of_round_blobs_is_null(tmp_path):
    """A round blob's fitted axis is set by noise (an earlier version reported it as a number). Below an elongation
    of 1.1 the reply says null; elongated blobs keep their angle."""
    arch = Archive(tmp_path)
    g = _geom(nx=128, ny=128, w=12.8, h=12.8)
    round_ = _ok(M.detect_blobs(arch, {"frame": _add(arch, 0, _blobs(128, 128, [(40.3, 50.6), (90.2, 80.4)],
                                                                         2.0, 2.0, 40e-12, 2e-12, 21), g)}))
    assert round_["n_blobs"] == 2
    assert all(b["elongation"] < 1.1 and b["axis_angle_deg"] is None for b in round_["blobs"])
    long_ = _ok(M.detect_blobs(arch, {"frame": _add(arch, 1, _blobs(128, 128, [(40.3, 50.6)], 2.6, 1.6, 40e-12,
                                                                        2e-12, 22), g)}))
    (b,) = long_["blobs"]
    assert b["elongation"] >= 1.1 and min(b["axis_angle_deg"], 180 - b["axis_angle_deg"]) < 5


def test_detect_blobs_on_a_dense_lattice():
    """A 13 x 13 lattice of 30 pm atoms (period 10 px, sigma 1.3 px) with 1.5 pm noise. An earlier version took the
    noise from the median absolute deviation of the whole residual, which the lattice itself
    dominates; its 6-sigma threshold kept 8 of the 169 atoms (period 8 px: none)."""
    offset, period = 4.3, 10.0
    centres = [(offset + period * i + 0.15 * ((j * 7 + i * 3) % 5), offset + period * j + 0.1 * ((i * 5 + j) % 7))
               for j in range(13) for i in range(13)]
    z = _blobs(128, 128, centres, 1.3, 1.3, 30e-12, 1.5e-12, 68)
    res = M.find_blobs(z - np.mean(z), dx_nm=0.1, dy_nm=0.1, max_n=200)
    assert res["n_candidates"] == len(centres) == len(res["blobs"])
    for c, r in centres:
        assert min(math.hypot(b["col"] - c, b["row"] - r) for b in res["blobs"]) < 0.3
    assert res["noise"] == pytest.approx(1.5e-12, rel=0.25)
    assert res["noise_method"] == "lower_tail"


def _ball_layer(seed: int = 3, n: int = 256, field_nm: float = 6.0, a_nm: float = 0.75, r_nm: float = 0.45,
                h: float = 450e-12, noise: float = 1e-12, n_vacancies: int = 9):
    """A simulator 6 nm frame: balls of radius 0.45 nm close-packed
    0.75 nm apart, so neighbours meet about 200 pm below their tops, on a flat floor 450 pm below the
    tops that shows only where a ball is missing. Returns ``(z, centres, vacancies)`` in native px."""
    rng = np.random.default_rng(seed)
    px = field_nm / n
    th = math.radians(7.0)
    a1 = np.array([math.cos(th), math.sin(th)]) * a_nm / px
    a2 = np.array([math.cos(th + math.pi / 3), math.sin(th + math.pi / 3)]) * a_nm / px
    origin = np.array([0.37, 0.61]) * a_nm / px
    sites = [origin + i * a1 + j * a2 for i in range(-12, 13) for j in range(-12, 13)]
    R = r_nm / px
    sites = [s for s in sites if -R < s[0] < n + R and -R < s[1] < n + R]
    inner = [k for k, s in enumerate(sites) if 2 * R < s[0] < n - 2 * R and 2 * R < s[1] < n - 2 * R]
    gone = set(rng.choice(inner, size=n_vacancies, replace=False).tolist())
    centres = [s for k, s in enumerate(sites) if k not in gone]
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    z = np.zeros((n, n))
    for c, r in centres:
        z = np.maximum(z, h * np.sqrt(np.clip(1.0 - ((xx - c) ** 2 + (yy - r) ** 2) / R ** 2, 0.0, None)))
    z += rng.normal(0, noise, z.shape)
    return z, centres, [s for k, s in enumerate(sites) if k in gone]


def test_detect_blobs_on_balls_whose_gaps_reach_a_floor():
    """The Mimea frame returned no blob at all: its lower tail is the floor seen through the vacancies,
    so the lower-tail noise came out at 210 pm (the frame's pixel noise is 1.1 pm) and the threshold,
    1200 pm, lay above the frame's whole 477 pm range. The noise is now capped at 8 pixel-to-pixel noises."""
    z, centres, vacancies = _ball_layer()
    px = 6.0 / 256
    R = 0.45 / px
    res = M.find_blobs(z - np.mean(z), dx_nm=px, dy_nm=px, max_n=200)
    assert res["noise_method"] == "high_frequency_cap"
    assert res["noise_lower_tail"] > 100e-12                       # what the threshold rested on before
    assert res["noise"] < 15e-12 and res["noise_high_frequency"] < 2e-12
    blobs = [(b["col"], b["row"]) for b in res["blobs"]]
    inner = [c for c in centres if 2 * R < c[0] < 256 - 2 * R and 2 * R < c[1] < 256 - 2 * R]
    found = [c for c in inner if min(math.dist(c, b) for b in blobs) < 0.15 / px]
    assert len(found) >= 0.8 * len(inner), (len(found), len(inner))
    owner = [min(range(len(centres)), key=lambda k: math.dist(centres[k], b)) for b in blobs]
    assert len(owner) == len(set(owner))                           # one blob per ball
    assert all(math.dist(centres[k], b) < 0.3 / px for k, b in zip(owner, blobs, strict=True))
    assert all(min(math.dist(v, b) for b in blobs) > 0.25 / px for v in vacancies)   # nothing on the floor


def test_detect_blobs_tool_reports_the_noise_method(tmp_path):
    z, centres, _ = _ball_layer()
    arch = Archive(tmp_path)
    g = _geom(nx=256, ny=256, w=6.0, h=6.0)
    det = _ok(M.detect_blobs(arch, {"frame": _add(arch, 0, z + 3e-9, g)}))
    assert det["n_blobs"] == 50 and det["truncated_to_max_n"] is True
    d = det["detection"]
    assert d["noise_method"] == "high_frequency_cap"
    assert d["noise_lower_tail_pm"] > 100 and d["noise_high_frequency_pm"] < 2 and d["noise_pm"] < 15
    flat = _ok(M.detect_blobs(arch, {"frame": _add(arch, 1, _blobs(128, 128, [(40.3, 50.6)], 2.0, 2.0, 40e-12,
                                                                          2e-12, 21), _geom(nx=128, ny=128))}))
    assert flat["detection"]["noise_method"] == "lower_tail" and flat["n_blobs"] == 1


@pytest.mark.parametrize("kind", ["correlated", "row_offsets", "fast_axis_lowpass"])
def test_detect_blobs_cap_leaves_noise_frames_alone(tmp_path, kind):
    """Noise that is not white must not set the cap off: correlated (1/f-like) noise, line offsets and a
    feedback that low-passes the fast axis keep the lower-tail noise within 4 pixel-to-pixel noises."""
    rng = np.random.default_rng(12)
    n = 384
    if kind == "correlated":
        z = gaussian_filter(rng.normal(size=(n, n)), 2.0)
        z = z / z.std() * 2e-12 + rng.normal(0, 0.5e-12, (n, n))
    elif kind == "row_offsets":
        z = rng.normal(0, 2e-12, (n, n)) + rng.normal(0, 4e-12, (n, 1))
    else:
        z = gaussian_filter(rng.normal(0, 2e-12, (n, n)), (0.0, 3.0))
        z = z / z.std() * 2e-12
    res = M.find_blobs(z, dx_nm=70.0 / n, dy_nm=70.0 / n)
    assert res["noise_method"] == "lower_tail"
    assert res["noise_lower_tail"] < 4 * res["noise_high_frequency"]
    assert res["blobs"] == []


def test_blob_fit_orientation_does_not_stick_at_a_bound():
    """Single blobs with aspect 2.75 at random orientations. An earlier version started every fit at a circle, where
    the model does not depend on theta, and bounded theta to [-pi, pi]: for seed 116 the fit ran to
    theta = pi and stopped there with the major axis 31 deg off and the wrong aspect."""
    for seed in range(110, 130):
        rng = np.random.default_rng(seed)
        th = float(rng.uniform(-math.pi / 2, math.pi / 2))
        n = 64
        yy, xx = np.mgrid[0:n, 0:n]
        c, r = 32.0 + rng.uniform(-0.5, 0.5), 32.0 + rng.uniform(-0.5, 0.5)
        dx, dy = xx + 0.5 - c, yy + 0.5 - r
        u, v = dx * math.cos(th) + dy * math.sin(th), -dx * math.sin(th) + dy * math.cos(th)
        z = 35e-12 * np.exp(-0.5 * ((u / 3.3) ** 2 + (v / 1.2) ** 2)) + rng.normal(0, 2e-12, (n, n))
        (b,) = M.find_blobs(z, dx_nm=0.1, dy_nm=0.1)["blobs"]
        assert b["fit"] == "gaussian" and -math.pi / 2 <= b["theta"] < math.pi / 2
        major = b["theta"] if b["sa"] >= b["sb"] else b["theta"] + math.pi / 2
        assert abs((math.degrees(major - th) + 90) % 180 - 90) < 3.0, seed
        assert max(b["sa"], b["sb"]) / min(b["sa"], b["sb"]) == pytest.approx(2.75, rel=0.1), seed


def test_detect_blobs_polarity_threshold_region(tmp_path):
    centres = [(40.5, 40.5), (100.25, 60.75)]
    z = -_blobs(128, 128, centres, 2.0, 2.0, 40e-12, 1e-12, 7)
    g = _geom(nx=128, ny=128, w=25.6, h=25.6)
    arch = Archive(tmp_path)
    fid = _add(arch, 0, z, g)
    assert _ok(M.detect_blobs(arch, {"frame": fid}))["n_blobs"] == 0
    body = _ok(M.detect_blobs(arch, {"frame": fid, "polarity": "dark"}))
    assert body["n_blobs"] == 2 and all(b["height_pm"] == pytest.approx(-40.0, abs=3.0) for b in body["blobs"])
    assert _ok(M.detect_blobs(arch, {"frame": fid, "polarity": "dark", "min_height": 60}))["n_blobs"] == 0
    body = _ok(M.detect_blobs(arch, {"frame": fid, "polarity": "dark",
                                     "region": {"x": 0, "y": 0, "width": 384, "height": 384}}))
    assert body["n_blobs"] == 1 and body["blobs"][0]["x_px"] == pytest.approx(40.5 * 6, abs=1.2)
    assert "at least 3 scan pixels" in _err(M.detect_blobs(arch, {"frame": fid, "background_nm": 0.1}))


# ── diff ────────────────────────────────────────────────────────────────────
def _texture(seed: int, n: int = 400) -> np.ndarray:
    return gaussian_filter(np.random.default_rng(seed).normal(size=(n, n)), 3.0)


def _sample(tex: np.ndarray, X: np.ndarray, Y: np.ndarray, s: float) -> np.ndarray:
    """A surface defined in scan nm (texture pixel = s nm, centred), sampled at (X, Y)."""
    n = tex.shape[0]
    col = X / s + n / 2 - 0.5
    row = n / 2 - Y / s - 0.5
    return map_coordinates(tex, [row, col], order=3, mode="nearest") * 2e-11


@pytest.mark.parametrize("angle", [0.0, 25.0])
def test_diff_recovers_subpixel_shift(tmp_path, angle):
    tex = _texture(1)
    g = _geom(nx=200, ny=200, w=20.0, h=20.0, angle=angle)
    X, Y = _scan_xy(g)
    shift = (0.137, -0.262)                            # nm, scan frame: the surface moved by this
    za = _sample(tex, X, Y, 0.1)
    zb = _sample(tex, X - shift[0], Y - shift[1], 0.1)
    yy, xx = np.mgrid[0:200, 0:200]
    zb = zb + 30e-12 * np.exp(-((xx - 150.5) ** 2 + (yy - 50.5) ** 2) / 8.0)     # something new in b
    arch = Archive(tmp_path)
    fa, fb = _add(arch, 0, za, g), _add(arch, 1, zb, g)
    r = M.diff(arch, {"frame_a": fa, "frame_b": fb})
    body = _ok(r)
    reg = body["registration"]
    assert reg["shift_nm"] == pytest.approx(list(shift), abs=0.006)            # 0.06 px
    assert reg["confidence"] > 2 and reg["ncc_peak"] > 0.95 and body["same_geometry"] is True
    regs = body["difference"]["regions"]
    assert regs and regs[0]["sign"] == "+"
    Xn, Yn = F.native_to_scan_nm(g, 151.0, 51.0)
    assert math.hypot(regs[0]["x_nm"] - Xn, regs[0]["y_nm"] - Yn) < 0.3
    assert regs[0]["peak_change_pm"] == pytest.approx(30.0, abs=6.0)
    assert {"offset_removed_pm", "noise_pm", "threshold_pm"} <= set(body["difference"])
    assert "center_nm" in body["grid"] and "meaning" not in reg
    dfid = body["difference"]["frame"]
    assert dfid.startswith("t0001.m") and arch.get(dfid).channels == ["diff"]
    assert np.isfinite(arch.array(dfid, "diff")).sum() > 1000
    assert 'derived="diff"' in r.images[0].label


def test_diff_resampled_geometry_and_identical_frames(tmp_path):
    tex = _texture(2)
    ga = _geom(nx=200, ny=200, w=20.0, h=20.0)
    gb = _geom(nx=160, ny=160, w=16.0, h=16.0, cx=1.5, cy=-1.0, angle=10.0)
    za = _sample(tex, *_scan_xy(ga), 0.1)
    zb = _sample(tex, *_scan_xy(gb), 0.1)
    arch = Archive(tmp_path)
    fa, fb = _add(arch, 0, za, ga), _add(arch, 1, zb, gb)
    body = _ok(M.diff(arch, {"frame_a": fa, "frame_b": fb}))
    assert body["same_geometry"] is False and body["grid"]["resampled"] is True
    assert body["registration"]["shift_nm"] == pytest.approx([0.0, 0.0], abs=0.02)
    assert body["grid"]["field_nm"][0] < 20.0
    # identical frames: zero shift and no "changes" from rounding
    fc = _add(arch, 2, za, ga)
    body = _ok(M.diff(arch, {"frame_a": fa, "frame_b": fc}))
    assert body["registration"]["shift_nm"] == pytest.approx([0.0, 0.0], abs=1e-3)
    assert body["difference"]["n_regions"] == 0
    body = _ok(M.diff(arch, {"frame_a": fa, "frame_b": fc, "align": False}))
    assert "registration" not in body and body["align"] is False
    far = _add(arch, 3, za, _geom(nx=200, ny=200, w=20.0, h=20.0, cx=500.0))
    assert "do not overlap" in _err(M.diff(arch, {"frame_a": fa, "frame_b": far}))


@pytest.mark.parametrize("angle", [0.0, 25.0])
def test_diff_says_where_the_second_peak_lies(tmp_path, angle):
    """Two frames of one periodic surface: confidence near 1 is right (the shift is known only up to a
    repeat), and the reply now gives the alternative's displacement. A pair of frames of a
    periodic surface once said confidence 1.002 and nothing else, leaving its repeat to be found by
    autocorrelation. Here the cell is 2.4 nm x 2.4 nm in the surface's own axes."""
    g = _geom(nx=200, ny=200, w=20.0, h=20.0, angle=angle)
    X, Y = _scan_xy(g)
    P = 2.4
    rng = np.random.default_rng(5)
    surf = (np.cos(2 * math.pi * X / P) + np.cos(2 * math.pi * Y / P)
            + 0.6 * np.cos(2 * math.pi * (X + Y) / P)) * 1e-11
    shift = (0.31, -0.18)
    za = surf + rng.normal(0, 1e-13, X.shape)
    zb = ((np.cos(2 * math.pi * (X - shift[0]) / P) + np.cos(2 * math.pi * (Y - shift[1]) / P)
           + 0.6 * np.cos(2 * math.pi * (X - shift[0] + Y - shift[1]) / P)) * 1e-11 + rng.normal(0, 1e-13, X.shape))
    arch = Archive(tmp_path)
    reg = _ok(M.diff(arch, {"frame_a": _add(arch, 0, za, g), "frame_b": _add(arch, 1, zb, g)}))["registration"]
    assert reg["confidence"] < 1.05
    second = reg["second_peak_shift_nm"]
    assert isinstance(reg["second_peak_shift_px"], list) and all(float(v).is_integer() for v in reg["second_peak_shift_px"])
    d = np.subtract(second, reg["shift_nm"])                     # one repeat: whole multiples of P
    assert math.hypot(*d) > 0.5 * P
    assert np.allclose(d / P, np.round(d / P), atol=0.1), d
    plain = _ok(M.diff(arch, {"frame_a": _add(arch, 2, _sample(_texture(2), X, Y, 0.1), g),
                              "frame_b": _add(arch, 3, _sample(_texture(2), X, Y, 0.1), g)}))["registration"]
    assert plain["confidence"] > 2 and len(plain["second_peak_shift_nm"]) == 2


def test_diff_survives_steps_creep_bow_and_large_shifts(tmp_path):
    """Adatoms on a stepped terrace, a creep bow that changes between frames, and a shift of a
    third of the field: plane flattening lets the step and the bow dominate the correlation; the
    default highpass flattening and the +-40 % search find the shift."""
    rng = np.random.default_rng(7)
    atoms = rng.uniform(-20, 20, (40, 2))
    g = _geom(nx=200, ny=200, w=20.0, h=20.0)
    X, Y = _scan_xy(g)

    def surface(Xs, Ys):
        z = 60e-12 * (0.5 + 0.5 * np.tanh((Xs + 0.3 * Ys - 4.0) / 0.3))          # a tip-broadened step
        for ax, ay in atoms:
            z = z + 60e-12 * np.exp(-((Xs - ax) ** 2 + (Ys - ay) ** 2) / (2 * 0.3 ** 2))
        return z

    u = (np.arange(200) + 0.5) / 200 - 0.5
    bow_a = 400e-12 * np.add.outer(u ** 2, 0.5 * u ** 2)    # piezo creep lives in frame coordinates
    bow_b = 250e-12 * np.add.outer(0.3 * u ** 2, u ** 2) + 40e-12 * np.add.outer(u, u)
    noise = np.random.default_rng(1)
    arch = Archive(tmp_path)
    for i, shift in enumerate([(6.5, -0.3), (-3.37, 5.62)]):
        za = surface(X, Y) + bow_a + noise.normal(0, 2e-12, X.shape)
        zb = surface(X - shift[0], Y - shift[1]) + bow_b + noise.normal(0, 2e-12, X.shape)
        fa, fb = _add(arch, 2 * i, za, g), _add(arch, 2 * i + 1, zb, g)
        body = _ok(M.diff(arch, {"frame_a": fa, "frame_b": fb}))
        assert body["flatten"] == "highpass" and body["highpass_nm"] == 3.0
        assert body["registration"]["shift_nm"] == pytest.approx(list(shift), abs=0.08)   # step residual: <0.5 px
        assert body["registration"]["peak_at_search_limit"] is False and body["registration"]["confidence"] > 1.5
        assert _ok(M.diff(arch, {"frame_a": fa, "frame_b": fb, "flatten": "plane"}))["flatten"] == "plane"


def test_register_pure_function():
    tex = _texture(5, 300)
    a = tex[50:250, 50:250]
    from scipy.ndimage import shift as nd_shift

    b = nd_shift(tex, (-2.4, 3.7), order=3)[50:250, 50:250]
    r = M.register(a, b)
    assert (r["dx"], r["dy"]) == pytest.approx((3.7, -2.4), abs=0.03)


def _steps_and_adatoms(seed: int, shift_nm: tuple[float, float]) -> tuple[np.ndarray, np.ndarray, dict]:
    """96 x 96 frames of 0.1 nm pixels: a tip-broadened step and adatoms, a creep bow that changes
    between the frames, and noise. Frame b shows frame a's surface moved by ``shift_nm`` (scan frame)."""
    rng = np.random.default_rng(seed)
    g = _geom(nx=96, ny=96, w=9.6, h=9.6)
    X, Y = _scan_xy(g)
    atoms = rng.uniform(-8.0, 8.0, (18, 2))

    def surface(Xs, Ys):
        z = 60e-12 * (0.5 + 0.5 * np.tanh((Xs + 0.3 * Ys - 1.5) / 0.3))
        for ax, ay in atoms:
            z = z + 60e-12 * np.exp(-((Xs - ax) ** 2 + (Ys - ay) ** 2) / (2 * 0.3 ** 2))
        return z

    u = (np.arange(96) + 0.5) / 96 - 0.5
    bow_a = 400e-12 * np.add.outer(u ** 2, 0.5 * u ** 2)
    bow_b = 250e-12 * np.add.outer(0.3 * u ** 2, u ** 2) + 40e-12 * np.add.outer(u, u)
    za = surface(X, Y) + bow_a + rng.normal(0, 2e-12, X.shape)
    zb = surface(X - shift_nm[0], Y - shift_nm[1]) + bow_b + rng.normal(0, 2e-12, X.shape)
    return za, zb, g


def test_diff_refinement_objective_has_no_jumps_at_integer_shifts(tmp_path, monkeypatch):
    """The sub-pixel refinement keeps one overlap for every shift it tries. An earlier version recomputed the overlap
    per shift, so a row or column left it whenever a shift component moved off an integer: the
    objective jumped there, and with weak correlation Nelder-Mead stopped on the jump."""
    import scipy.optimize

    seen: dict = {}
    real = scipy.optimize.minimize

    def spy(fun, x0, *args, **kw):
        seen["fun"], seen["x0"] = fun, np.array(x0, dtype=float)
        return real(fun, x0, *args, **kw)

    monkeypatch.setattr(scipy.optimize, "minimize", spy)
    za, zb, g = _steps_and_adatoms(1, (2.937, -0.334))
    arch = Archive(tmp_path)
    _ok(M.diff(arch, {"frame_a": _add(arch, 0, za, g), "frame_b": _add(arch, 1, zb, g)}))
    f, start = seen["fun"], seen["x0"]
    d = 0.01
    for axis in (0, 1):
        for k in (math.floor(start[axis]), math.ceil(start[axis])):
            def at(t, axis=axis):
                p = start.copy()
                p[axis] = t
                return f(p)
            across = abs(at(k + d) - at(k - d))
            beside = max(abs(at(k + 3 * d) - at(k + d)), abs(at(k - d) - at(k - 3 * d)))
            assert across <= 2.0 * beside + 1e-9, (axis, k, across, beside)


def test_diff_shift_does_not_snap_to_whole_pixels(tmp_path):
    """Content moved by (29.37, 3.34) px between weakly correlated frames: an earlier version returned whole-pixel
    components for most seeds (the refinement stopped on the jumps of its objective)."""
    arch = Archive(tmp_path)
    for seed in range(8):
        za, zb, g = _steps_and_adatoms(seed, (2.937, -0.334))
        body = _ok(M.diff(arch, {"frame_a": _add(arch, 2 * seed, za, g), "frame_b": _add(arch, 2 * seed + 1, zb, g)}))
        reg = body["registration"]
        assert reg["peak_at_search_limit"] is False
        for v in reg["shift_px"]:
            assert abs(v - round(v)) > 1e-3, (seed, reg["shift_px"])


# ── line_profile ────────────────────────────────────────────────────────────
def test_line_profile_finds_a_step(tmp_path):
    g = _geom(nx=200, ny=100, w=20.0, h=10.0)
    yy, xx = np.mgrid[0:100, 0:200]
    z = np.where(xx >= 120, 100e-12, 0.0) + np.random.default_rng(0).normal(0, 2e-12, (100, 200))
    arch = Archive(tmp_path)
    fid = _add(arch, 0, z, g)
    # display k = 3: from x=60 (scan px 20) to x=540 (scan px 180), y in the middle
    r = M.line_profile(arch, {"frame": fid, "p1": {"x": 60, "y": 150}, "p2": {"x": 540, "y": 150},
                              "width_px": 9, "flatten": "none"})
    body = _ok(r)
    assert body["length_nm"] == pytest.approx(16.0) and body["direction_deg"] == pytest.approx(0.0)
    assert body["n_samples"] == 161 and len(body["values_pm"]) == 161 and body["plot"]["frame"].startswith("t0000.m")
    assert len(body["steps"]) == 1
    st = body["steps"][0]
    assert st["height_pm"] == pytest.approx(100.0, abs=6.0) and st["position_nm"] == pytest.approx(10.0, abs=0.15)
    assert {"from_nm", "to_nm", "slope_pm_per_nm", "mean_pm"} == set(body["segments"][0])
    X, _ = F.native_to_scan_nm(g, 120.0, 50.0)
    assert st["x_nm"] == pytest.approx(X, abs=0.15)
    assert len(body["segments"]) >= 2 and r.images and 'derived="line_profile"' in r.images[0].label
    # the same line given in scan nm
    X1, Y1 = F.native_to_scan_nm(g, 20.0, 50.0)
    X2, Y2 = F.native_to_scan_nm(g, 180.0, 50.0)
    body2 = _ok(M.line_profile(arch, {"frame": fid, "p1": {"x_nm": X1, "y_nm": Y1}, "p2": {"x_nm": X2, "y_nm": Y2},
                                      "width_px": 9, "flatten": "none"}))
    assert body2["values_pm"] == body["values_pm"]
    assert "outside" in _err(M.line_profile(arch, {"frame": fid, "p1": {"x": 0, "y": 0}, "p2": {"x": 999, "y": 0}}))
    assert "either" in _err(M.line_profile(arch, {"frame": fid, "p1": {"x": 0, "x_nm": 0}, "p2": {"x": 5, "y": 5}}))
    assert "less than one scan pixel" in _err(M.line_profile(arch, {"frame": fid, "p1": {"x": 3, "y": 3},
                                                                    "p2": {"x": 4, "y": 3}}))


def test_line_profile_refusal_gives_the_range_the_frame_covers(tmp_path):
    """130 x 40 pixels shown at k = 1/3: the image is 44 x 14 pixels but the frame covers x <= 43.333.
    An earlier refusal said 0 <= x <= 44, a range that includes the point it refused."""
    arch = Archive(tmp_path, display_max=64)
    fid = _add(arch, 0, np.random.default_rng(0).normal(0, 1e-12, (40, 130)), _geom(nx=130, ny=40, w=13.0, h=4.0))
    assert arch.get(fid).display_size == [44, 14]
    msg = _err(M.line_profile(arch, {"frame": fid, "p1": {"x": 1, "y": 5}, "p2": {"x": 43.5, "y": 5}}))
    assert "display image 44x14, i.e. 0 <= x <= 43.333, 0 <= y <= 13.333" in msg
    _ok(M.line_profile(arch, {"frame": fid, "p1": {"x": 1, "y": 5}, "p2": {"x": 43.333, "y": 5}}))
    k1 = Archive(tmp_path / "k2")                          # integer scale: the image is the frame
    fid = _add(k1, 0, np.zeros((100, 200)), _geom(nx=200, ny=100, w=20.0, h=10.0))
    assert "0 <= x <= 600, 0 <= y <= 300)" in _err(M.line_profile(k1, {"frame": fid, "p1": {"x": 1, "y": 5},
                                                                       "p2": {"x": 601, "y": 5}}))


def test_line_profile_long_lines_take_400_samples(tmp_path):
    """The description says the samples stop at 400 (an earlier description promised at most one scan pixel apart)."""
    assert "up to 400 evenly spaced samples" in M.LINE_PROFILE_TOOL.description
    g = _geom(nx=600, ny=20, w=60.0, h=2.0)
    arch = Archive(tmp_path)
    fid = _add(arch, 0, np.random.default_rng(0).normal(0, 1e-12, (20, 600)), g)
    body = _ok(M.line_profile(arch, {"frame": fid, "p1": {"x": 0, "y": 10}, "p2": {"x": 600, "y": 10}}))
    assert body["n_samples"] == 400 and body["spacing_nm"] == pytest.approx(60.0 / 399, abs=1e-5)


def test_change_points_unit():
    x = np.linspace(0, 10, 201)
    y = np.where(x > 4.0, 2.0, 0.0) + 0.3 * x + np.random.default_rng(1).normal(0, 0.05, x.size)
    steps, sigma, segs = M.change_points(x, y)
    assert len(steps) == 1 and steps[0]["x"] == pytest.approx(4.0, abs=0.1)
    assert steps[0]["height"] == pytest.approx(2.0, abs=0.1) and sigma == pytest.approx(0.05, rel=0.3)
    assert len(segs) == 2 and segs[0]["slope"] == pytest.approx(0.3, abs=0.05)
    flat = M.change_points(x, np.random.default_rng(2).normal(0, 0.05, x.size))
    assert flat[0] == []
    rng = np.random.default_rng(3)
    # a tip-broadened step (0.5 nm wide ramp) is one step of the full height
    ramp = np.clip((x - 6.0) / 0.5, 0.0, 1.0) * 2.35 + rng.normal(0, 0.03, x.size)
    steps, _, _ = M.change_points(x, ramp)
    assert len(steps) == 1 and steps[0]["height"] == pytest.approx(2.35, abs=0.1)
    assert steps[0]["x"] == pytest.approx(6.25, abs=0.2)
    # a bump narrower than the transition window is not a step
    bump = 1.5 * np.exp(-((x - 5.0) / 0.15) ** 2) + rng.normal(0, 0.03, x.size)
    assert M.change_points(x, bump)[0] == []
    # two down-steps on terraces tilted by the plane fit of a staircase
    stair = 0.25 * x - np.where(x > 3.3, 1.0, 0) - np.where(x > 6.7, 1.0, 0) + rng.normal(0, 0.03, x.size)
    steps, _, _ = M.change_points(x, stair)
    assert [round(st["x"], 1) for st in steps] == pytest.approx([3.3, 6.7], abs=0.15)
    assert [st["height"] for st in steps] == pytest.approx([-1.0, -1.0], abs=0.1)


# ── stats ───────────────────────────────────────────────────────────────────
def test_stats_values(tmp_path):
    g = _geom(nx=64, ny=64, w=6.4, h=6.4)
    rng = np.random.default_rng(0)
    z = rng.normal(0, 5e-12, (64, 64)) + 1e-9
    z[48:] = np.nan
    arch = Archive(tmp_path)
    fid = _add(arch, 0, z, g, **{"Z/backward": z.copy()})
    body = _ok(M.stats(arch, {"frame": fid}))
    fin = z[np.isfinite(z)] * 1e12
    assert body["mean_pm"] == pytest.approx(fin.mean(), abs=0.1) and body["std_pm"] == pytest.approx(fin.std(), abs=0.1)
    assert set(body["percentiles_pm"]) == {"p1", "p5", "p25", "p50", "p75", "p95", "p99"}
    assert body["plane_subtracted"]["rms_roughness_pm"] == pytest.approx(5.0, rel=0.1)
    fb = body["forward_backward"]
    assert fb["correlation"] == pytest.approx(1.0) and fb["rms_difference_pm"] == 0.0
    assert body["rows_acquired"] == 48 and body["rows_total"] == 64
    assert body["rows_acquired_fraction"] == 0.75 and body["n_not_acquired"] == 16 * 64
    rows = z.copy()
    rows[:48] += rng.normal(0, 20e-12, (48, 1))                 # line-to-line offsets
    noisy = _ok(M.stats(arch, {"frame": _add(arch, 1, rows, g)}))
    assert noisy["line_noise_index"] > 3 * body["line_noise_index"]
    reg = _ok(M.stats(arch, {"frame": fid, "region": {"x": 0, "y": 0, "width": 120, "height": 120}}))
    assert reg["native_px"] == [0, 0, 10, 10] and reg["n_pixels"] == 100


def test_measure_errors(tmp_path):
    arch = Archive(tmp_path)
    m = arch.add_derived(0, F.encode_png(np.zeros((8, 8), np.uint8)), meta={"tool": "x"})
    for fn, args in ((M.stats, {"frame": m.fid}), (M.fft_peaks, {"frame": m.fid}),
                     (M.detect_blobs, {"frame": "t0042.s0"}), (M.stats, {"frame": "t0000.p0", "bogus": 1}),
                     (M.diff, {"frame_a": m.fid}), (M.line_profile, None)):
        r = fn(arch, args)
        assert r.is_error and "error" in json.loads(r.text)


def test_null_optional_arguments_are_read_as_absent(tmp_path):
    """An explicit null takes the default. An earlier version refused diff's align / flatten / direction: null with an
    unrelated message and failed internally on highpass_nm: null (TypeError)."""
    g = _geom(nx=128, ny=128, w=12.8, h=12.8)
    z = _sample(_texture(4), *_scan_xy(g), 0.1)
    arch = Archive(tmp_path)
    fa, fb = _add(arch, 0, z, g), _add(arch, 1, np.roll(z, 3, axis=1), g)

    def same(tool, args, nulls):
        a, b = _ok(tool(arch, args)), _ok(tool(arch, {**args, **nulls}))
        for body in (a, b):                       # each call archives its own derived image
            for k in ("plot", "image", "difference"):
                if k in body:
                    body[k].pop("frame")
        assert a == b

    same(M.diff, {"frame_a": fa, "frame_b": fb}, {"region": None, "channel": None, "direction": None,
                                                   "align": None, "flatten": None, "highpass_nm": None})
    same(M.line_profile, {"frame": fa, "p1": {"x": 10, "y": 10}, "p2": {"x": 300, "y": 200}},
         {"p1": {"x": 10, "y": 10, "x_nm": None}, "p2": {"x": 300, "y": 200, "y_nm": None}, "width_px": None,
          "flatten": None, "highpass_nm": None, "channel": None, "direction": None})
    same(M.detect_blobs, {"frame": fa}, {"region": None, "polarity": None, "min_height": None,
                                          "min_sep_nm": None, "background_nm": None, "max_n": None})
    same(M.fft_peaks, {"frame": fa}, {"region": None, "n_peaks": None, "flatten": None, "highpass_nm": None})
    same(M.stats, {"frame": fa}, {"region": None, "channel": None, "direction": None})


# ── one vocabulary for everything the model reads ──────────────────────────
_FORBIDDEN_KEYS = {"fid", "type", "visual_kind", "measurement", "unit", "setpoint_a", "centre_nm"}


def _vocabulary_violations(obj, path="$"):
    """Keys / values that break the model-facing JSON rules (frame ids under "frame", American
    spelling, units as key suffixes, numbers never as strings)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            si_suffix = k.endswith(("_a", "_m")) and k not in ("frame_a", "frame_b")   # diff's parameters
            if k in _FORBIDDEN_KEYS or "centre" in k or "colour" in k or si_suffix:
                out.append(f"{path}.{k}")
            out += _vocabulary_violations(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _vocabulary_violations(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        try:
            float(obj)
            out.append(f"{path} = {obj!r} (numeric string)")
        except ValueError:
            pass
    return out


def test_model_facing_json_vocabulary(tmp_path):
    from mast.vista import viewtools as V

    tex = _texture(3)
    g = _geom(nx=128, ny=128, w=12.8, h=12.8, cx=3.0, cy=-2.0, angle=10.0)
    z = _sample(tex, *_scan_xy(g), 0.1)
    z[100:] = np.nan
    arch = Archive(tmp_path)
    fa = arch.add_partial(0, {"Z": z, "Z/backward": z, "Current": np.full((128, 128), 1e-10)}, geometry=g,
                          meta={"bias_v": 0.1, "setpoint_a": 1e-10, "scan_dir": "down"}).fid
    fb = _add(arch, 1, z, g)
    replies = [
        V.inspect(arch, {"question": "q", "views": [{"label": "a", "frame": fa},
                                                    {"label": "b", "frame": fa, "channel": "Current",
                                                     "region": {"x": 10, "y": 10, "width": 50, "height": 40}}]}),
        V.read_values(arch, {"question": "q", "views": [{"label": "a", "frame": fa, "rows": 3, "columns": 3}]}),
        M.line_profile(arch, {"frame": fa, "p1": {"x": 10, "y": 10}, "p2": {"x": 300, "y": 200}}),
        M.fft_peaks(arch, {"frame": fa}),
        M.detect_blobs(arch, {"frame": fa}),
        M.diff(arch, {"frame_a": fa, "frame_b": fb}),
        M.stats(arch, {"frame": fa}),
    ]
    for r in replies:
        assert not r.is_error, r.text
        assert _vocabulary_violations(json.loads(r.text)) == [], r.text[:200]
    for s in arch.index():
        assert _vocabulary_violations(s) == [], s
    summ = arch.get(fa).summary()
    assert summ["frame"] == fa and summ["kind"] == "partial" and summ["setpoint_pa"] == pytest.approx(100.0)
    assert summ["rows_acquired"] == 100 and summ["rows_total"] == 128 and isinstance(summ["display_scale"], int)
    assert set(summ) == {"frame", "turn", "kind", "source", "channels", "directions", "size_px", "field_nm",
                         "center_nm", "angle_deg", "nm_per_px", "display_scale", "display_size_px",
                         "default_channel", "rows_acquired", "rows_total", "bias_v", "setpoint_pa", "scan_dir"}


# ── fonts of the numbered marks (a frozen build may lack TrueType) ─────────
@pytest.fixture
def fresh_font_cache():
    M._font.cache_clear()
    yield
    M._font.cache_clear()


def test_marks_use_matplotlibs_dejavu_sans(fresh_font_cache):
    from PIL import ImageFont

    font = M._font(14)
    assert isinstance(font, ImageFont.FreeTypeFont) and font.path.endswith("DejaVuSans.ttf")


def test_marks_fall_back_to_pillows_default_font(tmp_path, monkeypatch, fresh_font_cache):
    """No DejaVu Sans, then no FreeType at all: the numbers still get drawn, with Pillow's default
    font (Aileron through FreeType, else its bitmap font). Without FreeType, load_default(size=...)
    raises ImportError, which used to escape and turn fft_peaks / detect_blobs / diff into errors."""
    from matplotlib import font_manager
    from PIL import ImageFont
    from PIL._util import DeferredError

    def no_font(*args, **kwargs):
        raise ValueError("Failed to find font DejaVu Sans")

    monkeypatch.setattr(font_manager, "findfont", no_font)
    assert not str(getattr(M._font(14), "path", "")).endswith("DejaVuSans.ttf")
    M._font.cache_clear()
    monkeypatch.setattr(ImageFont, "core", DeferredError.new(ImportError("The _imagingft C module is not installed")))
    assert type(M._font(14)) is ImageFont.ImageFont
    blank = np.zeros((60, 80, 3), np.uint8)
    marks = M._png_to_rgb(M._draw_marks(blank, [(30.0, 40.0, 9.0, "12")]))
    assert marks.shape == (60, 80, 3) and (marks[:30] == M.MARK_RGB).all(axis=2).any()
    arch = Archive(tmp_path)
    z = _blobs(128, 128, [(40.3, 50.6), (90.2, 80.4)], 2.0, 2.0, 40e-12, 2e-12, 21)
    r = M.detect_blobs(arch, {"frame": _add(arch, 0, z, _geom(nx=128, ny=128, w=12.8, h=12.8))})
    assert _ok(r)["n_blobs"] == 2 and len(r.images) == 1
