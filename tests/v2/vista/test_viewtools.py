"""mast.vista visual-memory tools: inspect (crop / enlarge / labels) and read_values (bin centres, caps)."""
from __future__ import annotations

import io
import json

import numpy as np
import pytest

from mast.vista import frames as F
from mast.vista import viewtools as V
from mast.vista.archive import Archive
from mast.vista.protocol import MAX_INSPECT_VIEWS, MAX_READ_SAMPLES, MAX_READ_VIEWS, ToolReply, compact_json

from .test_frames import write_dat
from .test_measure import _vocabulary_violations

GEOM = {"cx_nm": 0.0, "cy_nm": 0.0, "w_nm": 38.4, "h_nm": 38.4, "angle_deg": 0.0}


def _size(png: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        return im.size


@pytest.fixture(scope="module")
def arch(tmp_path_factory) -> Archive:
    a = Archive(tmp_path_factory.mktemp("vista_view") / "arch")
    yy, xx = np.mgrid[0:384, 0:384]
    z = (xx + 1000.0 * yy) * 1e-12                 # Z in m: value encodes the scan pixel (pm)
    cur = np.full((384, 384), 2.5e-10)
    part = z.copy()
    part[200:] = np.nan
    a.add_partial(0, {"Z": z, "Z/backward": z + 1e-12, "Current": cur}, geometry=GEOM, meta={})   # t0000.p0
    a.add_partial(1, {"Z": part}, geometry=GEOM, meta={})                                          # t0001.p0
    big = np.random.default_rng(0).normal(0, 1e-11, (1024, 1024))
    a.add_partial(2, {"Z": big}, geometry=dict(GEOM, w_nm=100.0, h_nm=100.0), meta={})            # t0002.p0
    a.add_derived(2, F.encode_png(np.zeros((300, 400), np.uint8)), meta={"tool": "fft_peaks",
                                                                      "sources": ["t0002.p0"]})   # t0002.m0
    return a


def _ok(r: ToolReply) -> dict:
    assert isinstance(r, ToolReply)
    assert not r.is_error, r.text
    return json.loads(r.text)


def _err(r: ToolReply) -> str:
    assert isinstance(r, ToolReply) and r.is_error
    return json.loads(r.text)["error"]


# ── schemas ─────────────────────────────────────────────────────────────────
def test_tool_defs_are_complete():
    for td in (V.INSPECT_TOOL, V.READ_VALUES_TOOL):
        mcp = td.to_mcp()
        s = mcp["inputSchema"]
        assert s["additionalProperties"] is False and s["required"] == ["question", "views"]
        assert mcp["annotations"]["readOnlyHint"] is True and mcp["annotations"]["destructiveHint"] is False
        item = s["properties"]["views"]["items"]
        assert item["additionalProperties"] is False and item["required"] == ["label", "frame"]
        assert item["properties"]["region"]["additionalProperties"] is False
        assert item["properties"]["region"]["properties"]["width"]["maximum"] == 768
        assert "does not" in td.description or "without changing the instrument" in td.description
        json.dumps(mcp)
    assert V.INSPECT_TOOL.input_schema["properties"]["views"]["maxItems"] == MAX_INSPECT_VIEWS
    assert V.READ_VALUES_TOOL.input_schema["properties"]["views"]["maxItems"] == MAX_READ_VIEWS
    assert V.make_inspect_tool(512).input_schema["properties"]["views"]["items"]["properties"]["region"][
        "properties"]["x"]["maximum"] == 511
    assert set(V.VIEW_TOOLS) == {"inspect", "read_values"}
    assert set(V.view_tools(1024)) == {"inspect", "read_values"}


@pytest.mark.parametrize("dm", [256, 512, 1024])
def test_view_tools_are_sized_for_the_archive_display_max(dm):
    """A server whose archive renders at another display_max builds its schemas with view_tools(dm);
    the module-level VIEW_TOOLS keep the 768 default."""
    tools = V.view_tools(dm)
    for name, (td, fn) in tools.items():
        assert fn is V.VIEW_TOOLS[name][1]
        region = td.input_schema["properties"]["views"]["items"]["properties"]["region"]["properties"]
        assert (region["x"]["maximum"], region["width"]["maximum"]) == (dm - 1, dm)
    assert f"fits {dm}x{dm}" in tools["inspect"][0].description
    for td, _ in V.VIEW_TOOLS.values():
        region = td.input_schema["properties"]["views"]["items"]["properties"]["region"]["properties"]
        assert region["width"]["maximum"] == 768


def test_inspect_describes_the_grid_only_when_the_archive_draws_one():
    plain, lined = V.make_inspect_tool(), V.make_inspect_tool(grid=True)
    assert plain.description == V.INSPECT_TOOL.description and "nothing is drawn on them" in plain.description
    assert "grid" not in plain.description
    for part in ("amber lines draw a square grid every grid_nm nm", "top-left corner of the frame's full image",
                 "the grid spacing (grid_nm)"):
        assert part in lined.description, part
    assert V.view_tools(768, grid=True)["inspect"][0].description == lined.description


def test_inspect_with_grid_reports_the_spacing_of_each_view(tmp_path):
    a = Archive(tmp_path / "g", grid=True)
    a.add_partial(0, {"Z": np.zeros((384, 384))}, geometry=GEOM, meta={})         # 38.4 nm, 2 image px/scan px
    r = V.inspect(a, {"question": "q", "views": [
        {"label": "whole", "frame": "t0000.p0"},
        {"label": "zoom", "frame": "t0000.p0", "region": {"x": 100, "y": 100, "width": 64, "height": 64}}]})
    whole, zoom = _ok(r)["views"]
    assert (whole["grid_nm"], zoom["grid_nm"]) == (5.0, 0.2)        # 38.4/16 -> 5 nm; 3.2 nm/16 = 0.2 nm
    assert list(whole)[list(whole).index("corners_nm") + 1] == "grid_nm"
    assert 'grid_nm="5"' in r.images[0].label and 'grid_nm="0.2"' in r.images[1].label
    plain = Archive(tmp_path / "p")
    e = plain.add_partial(0, {"Z": np.zeros((8, 8))}, geometry=GEOM, meta={})
    view = _ok(V.inspect(plain, {"question": "q", "views": [{"label": "w", "frame": e.fid}]}))["views"][0]
    assert "grid_nm" not in view


# ── inspect ─────────────────────────────────────────────────────────────────
def test_inspect_crop_and_enlarge_sizes(arch):
    r = V.inspect(arch, {"question": "what is there?", "views": [
        {"label": "whole", "frame": "t0000.p0"},
        {"label": "zoom", "frame": "t0000.p0", "region": {"x": 101, "y": 51, "width": 63, "height": 31}},
        {"label": "bwd", "frame": "t0000.p0", "direction": "backward", "region": {"x": 0, "y": 0, "width": 10, "height": 10}},
    ]})
    body = _ok(r)
    assert body["instrument_unchanged"] is True and body["view_count"] == 3 and "visual_kind" not in body
    v1, v2, v3 = body["views"]
    assert v1["image_size_px"] == [768, 768] and v1["magnification"] == 1.0 and v1["image_px_per_scan_px"] == 2
    assert v1["kind"] == "partial" and v1["frame"] == "t0000.p0"
    # odd region at k = 2 widens to whole scan pixels, then 24 image px per scan px (768 // 32)
    assert v2["region_px"] == [100, 50, 64, 32] and v2["native_px"] == [50, 25, 82, 41]
    assert v2["image_size_px"] == [768, 384] and v2["magnification"] == 12.0 and v2["image_px_per_scan_px"] == 24
    assert v2["nm_per_image_px"] == pytest.approx(0.1 / 24, rel=1e-3)
    X, Y = F.display_to_scan_nm(arch.get("t0000.p0"), 100, 50)
    assert v2["corners_nm"]["top_left"] == pytest.approx([X, Y], abs=1e-4)
    assert {"black_pm", "white_pm", "min_pm", "max_pm"} <= set(v2) and v2["black_pm"] <= v2["white_pm"]
    assert v3["direction"] == "backward" and v3["image_size_px"] == [765, 765]
    assert [_size(im.png) for im in r.images] == [(768, 768), (768, 384), (765, 765)]
    labels = [im.label for im in r.images]
    assert labels[1].startswith('<visual kind="inspection" index="2" count="3" label="zoom" frame="t0000.p0"')
    assert 'region="100,50,64,32"' in labels[1] and 'magnification="12"' in labels[1]


def test_inspect_rerenders_from_values_not_png(arch):
    r = V.inspect(arch, {"question": "q", "views": [
        {"label": "a", "frame": "t0000.p0", "flatten": "none", "clip_pct": 0},
        {"label": "b", "frame": "t0000.p0", "channel": "current"},
        {"label": "c", "frame": "t0000.p0", "flatten": "highpass", "highpass_nm": 2.0},
    ]})
    body = _ok(r)
    assert body["views"][0]["black_pm"] == pytest.approx(0.0)
    assert body["views"][0]["white_pm"] == pytest.approx(383.0 + 383000.0)
    assert body["views"][1]["channel"] == "Current" and "black_pa" in body["views"][1]
    assert body["views"][2]["flatten"] == "highpass" and body["views"][2]["highpass_nm"] == 2.0
    assert r.images[0].png != arch.observation("t0000.p0")[0].png


def test_inspect_large_frame_region_shows_native_pixels(arch):
    e = arch.get("t0002.p0")
    assert e.display_scale == 0.5 and e.display_size == [512, 512]
    summ = e.summary()
    assert summ["display_scale"] == 0.5 and summ["display_scale_frac"] == "1/2"
    body = _ok(V.inspect(arch, {"question": "q", "views": [
        {"label": "a", "frame": "t0002.p0", "region": {"x": 0, "y": 0, "width": 128, "height": 64}}]}))
    v = body["views"][0]
    assert v["native_px"] == [0, 0, 256, 128] and v["image_px_per_scan_px"] == 3 and v["image_size_px"] == [768, 384]
    body = _ok(V.inspect(arch, {"question": "q", "views": [{"label": "w", "frame": "t0002.p0"}]}))
    assert body["views"][0]["image_size_px"] == [512, 512] and body["views"][0]["image_px_per_scan_px"] == 0.5


def test_inspect_partial_frame_and_derived(arch):
    r = V.inspect(arch, {"question": "q", "views": [
        {"label": "p", "frame": "t0001.p0"},
        {"label": "m", "frame": "t0002.m0", "region": {"x": 0, "y": 0, "width": 100, "height": 50}}]})
    body = _ok(r)
    assert body["views"][0]["rows_acquired"] == 200 and body["views"][0]["rows_total"] == 384
    assert 'rows_acquired="200/384"' in r.images[0].label
    assert body["views"][1]["kind"] == "derived" and body["views"][1]["magnification"] == 7
    assert _size(r.images[1].png) == (700, 350)
    assert r.images[1].label.startswith('<visual kind="inspection" index="2" count="2" label="m" frame="t0002.m0"')


@pytest.mark.parametrize("args,needle", [
    ({"question": "q", "views": [{"label": "a", "frame": "t0009.p0"}]}, "no frame"),
    ({"question": "q", "views": [{"label": "a", "frame": "t0000.p0",
                                  "region": {"x": 700, "y": 0, "width": 100, "height": 10}}]}, "leaves"),
    ({"question": "q", "views": [{"label": "a", "frame": "t0000.p0", "zoom": 2}]}, "unknown field"),
    ({"question": "q", "views": [{"label": "a", "frame": "t0000.p0", "channel": "dIdV"}]}, "this frame has"),
    ({"question": "q", "views": [{"label": "a", "frame": "t0000.p0", "channel": "Current",
                                  "direction": "backward"}]}, "has only"),
    ({"question": "q", "views": [{"label": "a", "frame": "t0000.p0", "flatten": "magic"}]}, "must be one of"),
    ({"question": "q", "views": [{"label": "a", "frame": "t0000.p0", "highpass_nm": 2}]}, "only applies"),
    ({"question": "q", "views": [{"label": "a", "frame": "t0002.m0", "channel": "Z"}]}, "do not apply"),
    ({"question": "q", "views": [{"label": "", "frame": "t0000.p0"}]}, "label"),
    ({"question": "", "views": [{"label": "a", "frame": "t0000.p0"}]}, "question"),
    ({"question": "q", "views": []}, "non-empty"),
    ({"question": "q", "views": [{"label": "a", "frame": "t0000.p0"}] * (MAX_INSPECT_VIEWS + 1)}, "at most"),
    ({"question": "q", "views": [{"label": "a", "frame": "t0000.p0",
                                  "region": {"x": 0, "y": 0, "width": 0, "height": 10}}]}, ">= 1"),
    ({"question": "q", "views": [{"label": "a", "frame": "t0000.p0",
                                  "region": {"x": 0.5, "y": 0, "width": 10, "height": 10}}]}, "integer"),
    ({"question": "q"}, "missing"),
    ({"question": "q", "views": [{"label": "a", "frame": "t0000.p0"}], "extra": 1}, "unknown field"),
])
def test_inspect_errors_never_raise(arch, args, needle):
    assert needle in _err(V.inspect(arch, args))


def test_tools_never_raise_on_garbage(arch):
    for bad in (None, "x", 3, [], {"question": 5, "views": "no"}, {"question": "q", "views": [7]}):
        assert V.inspect(arch, bad).is_error
        assert V.read_values(arch, bad).is_error


# ── read_values ─────────────────────────────────────────────────────────────
def test_read_values_bin_center_sampling(arch):
    body = _ok(V.read_values(arch, {"question": "values", "views": [
        {"label": "a", "frame": "t0000.p0", "region": {"x": 100, "y": 40, "width": 90, "height": 31},
         "rows": 3, "columns": 4}]}))
    v = body["views"][0]
    xs = [100 + ((2 * c + 1) * 90) // 8 for c in range(4)]
    ys = [40 + ((2 * r + 1) * 31) // 6 for r in range(3)]
    assert v["sample_x_px"] == xs and v["sample_y_px"] == ys
    expected = [[float(x // 2 + 1000 * (y // 2)) for x in xs] for y in ys]   # k = 2
    assert v["values_pm"] == expected and v["decimals"] == 1 and "values" not in v
    X, Y = F.display_to_scan_nm(arch.get("t0000.p0"), xs[0] + 0.5, ys[0] + 0.5)
    assert v["first_sample_nm"] == pytest.approx([X, Y], abs=1e-4)
    assert body["sample_count"] == 12 and body["sampling"].startswith("cell centers")


def test_read_values_units_nan_and_whole_frame(arch):
    body = _ok(V.read_values(arch, {"question": "q", "views": [
        {"label": "cur", "frame": "t0000.p0", "channel": "Current", "rows": 2, "columns": 2},
        {"label": "part", "frame": "t0001.p0", "rows": 4, "columns": 1}]}))
    cur, part = body["views"]
    assert cur["values_pa"] == [[250.0, 250.0], [250.0, 250.0]]
    assert cur["region_px"] == [0, 0, 768, 768]
    assert part["values_pm"][2:] == [[None], [None]] and part["null_count"] == 2
    assert part["values_pm"][0][0] is not None


def test_read_values_large_frame_reads_native_under_cell_center(arch):
    z = arch.array("t0002.p0", "Z")
    body = _ok(V.read_values(arch, {"question": "q", "views": [
        {"label": "a", "frame": "t0002.p0", "region": {"x": 10, "y": 20, "width": 3, "height": 1},
         "rows": 1, "columns": 3}]}))
    # display px 10, 11, 12 cover scan px 20-21, 22-23, 24-25; the cell centers sit at 21, 23, 25
    dec = body["views"][0]["decimals"]
    want = [F.round_to(float(z[41, c]) * 1e12, dec) for c in (21, 23, 25)]
    assert body["views"][0]["values_pm"] == [want]


def test_read_values_caps(arch):
    msg = _err(V.read_values(arch, {"question": "q", "views": [
        {"label": "a", "frame": "t0000.p0", "rows": 65, "columns": 64}]}))
    assert f"at most {MAX_READ_SAMPLES} samples" in msg
    msg = _err(V.read_values(arch, {"question": "q", "views": [
        {"label": "a", "frame": "t0000.p0", "rows": 32, "columns": 64}] * 3}))
    assert "views 1..3 already ask for 6144" in msg
    assert "at most 64 views" in _err(V.read_values(arch, {"question": "q", "views": [
        {"label": "a", "frame": "t0000.p0", "rows": 1, "columns": 1}] * 65}))
    assert "needs rows and columns" in _err(V.read_values(arch, {"question": "q", "views": [
        {"label": "a", "frame": "t0000.p0"}]}))
    assert "without values" in _err(V.read_values(arch, {"question": "q", "views": [
        {"label": "a", "frame": "t0002.m0", "rows": 1, "columns": 1}]}))
    _ok(V.read_values(arch, {"question": "q", "views": [
        {"label": "a", "frame": "t0000.p0", "rows": 64, "columns": 64}]}))


def test_null_optional_arguments_are_read_as_absent(arch):
    """An explicit null takes the default. An earlier version answered direction: null with an unrelated refusal and
    failed internally on clip_pct: null (TypeError)."""
    base = {"label": "a", "frame": "t0000.p0"}
    plain = _ok(V.inspect(arch, {"question": "q", "views": [base]}))
    nulls = _ok(V.inspect(arch, {"question": "q", "views": [dict(
        base, channel=None, direction=None, region=None, flatten=None, highpass_nm=None, clip_pct=None,
        x_col=None, y_col=None, x_range=None)]}))
    assert nulls == plain
    view = _ok(V.read_values(arch, {"question": "q", "views": [dict(
        base, rows=2, columns=2, channel=None, direction=None, region=None, n=None)]}))["views"][0]
    assert view["direction"] == "forward" and view["channel"] == "Z" and view["region_px"] == [0, 0, 768, 768]


def _strict_json(text: str):
    """Parse as standard JSON: NaN / Infinity are refused (``json.loads`` accepts them by default)."""
    def refuse(constant):
        raise ValueError(f"not JSON: {constant}")
    return json.loads(text, parse_constant=refuse)


def test_compact_json_writes_null_for_non_finite_numbers():
    """NaN and +-Infinity are not JSON; an earlier version wrote them as NaN / Infinity, so such a reply did not parse."""
    s = compact_json({"a": float("nan"), "b": [float("inf"), 1.5], "c": np.float32("nan"),
                      "d": np.array([1.0, np.nan, -np.inf]), "e": np.float32(0.25)})
    assert s == '{"a":null,"b":[null,1.5],"c":null,"d":[1.0,null,null],"e":0.25}'
    _strict_json(s)


def test_inspect_spectrum_without_a_finite_x_value_replies_in_json(tmp_path):
    rows = ["Experiment\tbias spectroscopy\t", "X (m)\t1.250000E-09\t", "Y (m)\t-2.000000E-09\t", "",
            "[DATA]", "Bias calc (V)\tCurrent (A)"] + [f"NaN\t{1e-11 * i:.6E}" for i in range(5)]
    dat = tmp_path / "nan.dat"
    dat.write_text("\n".join(rows) + "\n", encoding="ascii")
    a = Archive(tmp_path / "s")
    d = a.add_spectrum(0, dat)
    r = V.inspect(a, {"question": "q", "views": [{"label": "plot", "frame": d.fid}]})
    assert not r.is_error, r.text
    assert _strict_json(r.text)["views"][0]["x_shown_v"] == [None, None]


def test_inspect_spectrum_default_plots_the_first_four_columns(tmp_path):
    """The y_col description says what the default plots (an earlier version said "every column")."""
    names = ["Bias calc (V)", "Current (A)", "LI Demod 1 X (A)", "LI Demod 1 Y (A)", "Z (m)", "Current [bwd] (A)"]
    rows = ["Experiment\tbias spectroscopy\t", "X (m)\t0.000000E+00\t", "Y (m)\t0.000000E+00\t", "", "[DATA]",
            "\t".join(names)] + ["\t".join(f"{(i + k) * 1e-11:.6E}" for k in range(6)) for i in range(7)]
    dat = tmp_path / "six.dat"
    dat.write_text("\n".join(rows) + "\n", encoding="ascii")
    a = Archive(tmp_path / "s")
    d = a.add_spectrum(0, dat)
    view = _ok(V.inspect(a, {"question": "q", "views": [{"label": "p", "frame": d.fid}]}))["views"][0]
    assert view["y_cols"] == names[1:5]
    desc = V.INSPECT_TOOL.input_schema["properties"]["views"]["items"]["properties"]["y_col"]["description"]
    assert "first four columns other than x" in desc


def test_read_values_spectrum(tmp_path):
    a = Archive(tmp_path / "s")
    d = a.add_spectrum(0, write_dat(tmp_path / "x.dat", n=21))
    body = _ok(V.read_values(a, {"question": "q", "views": [
        {"label": "s", "frame": d.fid, "x_range": [-0.1, 0.1], "n": 3, "y_cols": ["Current"]}]}))
    v = body["views"][0]
    assert v["column_names"] == ["Bias calc (V)", "Current (A)"] and v["column_units"] == ["V", "pA"]
    assert v["points_in_range"] == 5 and v["n"] == 3 and v["x_range_v"] == [-0.1, 0.1]
    assert v["sample_index"] == [8, 10, 12]               # bin centers of the 5 points in range
    assert v["values"][0] == [-0.1, 0.0, 0.1] and v["values"][1] == [-20.0, 0.0, 20.0]
    assert v["column_decimals"] == [3, 1]
    assert body["sample_count"] == 6
    body = _ok(V.read_values(a, {"question": "q", "views": [{"label": "all", "frame": d.fid}]}))
    assert _ok(V.read_values(a, {"question": "q", "views": [{"label": "all", "frame": d.fid, "n": None,
                                                             "x_range": None, "y_cols": None}]})) == body
    assert body["views"][0]["n"] == 21 and len(body["views"][0]["values"]) == 3
    assert "contains no samples" in _err(V.read_values(a, {"question": "q", "views": [
        {"label": "s", "frame": d.fid, "x_range": [900, 1000]}]}))
    assert "do not apply" in _err(V.read_values(a, {"question": "q", "views": [
        {"label": "s", "frame": d.fid, "rows": 2, "columns": 2}]}))
    r = V.inspect(a, {"question": "q", "views": [{"label": "plot", "frame": d.fid, "y_col": "Current",
                                                  "x_range": [-0.2, 0.2]}]})
    body = _ok(r)
    assert body["views"][0]["n_points"] == 9 and body["views"][0]["x_unit"] == "V"
    assert body["views"][0]["y_cols"] == ["Current (A)"] and body["views"][0]["y_units"] == ["pA"]
    assert _vocabulary_violations(body) == []                   # an earlier version replied y: [{column, unit}]
    assert body["views"][0]["x_shown_v"] == [-0.2, 0.2] and body["views"][0]["kind"] == "spectrum"
    assert _size(r.images[0].png)[0] >= 600
