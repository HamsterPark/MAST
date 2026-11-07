"""mast.vista archive: frame ids, lossless storage, labels, observation images, reopening, origin tags."""
from __future__ import annotations

import io
import json
import threading

import numpy as np
import pytest

from mast.vista import frames as F
from mast.vista.archive import Archive, FrameEntry, sample_axis
from mast.vista.protocol import ImageBlock

from .test_frames import write_dat, write_sxm

GEOM = {"cx_nm": 10.0, "cy_nm": -4.0, "w_nm": 20.0, "h_nm": 10.0, "angle_deg": 12.0}


def _arrays(ny=64, nx=128, seed=0):
    rng = np.random.default_rng(seed)
    z = rng.normal(0, 3e-12, (ny, nx)) + 1e-9
    cur = rng.normal(1e-10, 1e-12, (ny, nx))
    return {"Z/forward": z, "Z/backward": z + 1e-12, "Current/forward": cur}


def _png_rgb(png: bytes) -> np.ndarray:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        return np.asarray(im.convert("RGB"))


def test_add_partial_store_and_reopen(tmp_path):
    arch = Archive(tmp_path / "a")
    arr = _arrays()
    arr["Z/forward"][40:] = np.nan                       # rows not yet acquired
    e = arch.add_partial(3, arr, geometry=GEOM, meta={"bias_v": 0.2, "setpoint_a": 5e-11, "scan_dir": "down",
                                                      "rec_time": "t"}, sim_s=12.5)
    assert e.fid == "t0003.p0" and e.kind == "p" and e.turn == 3 and e.index == 0
    assert (e.nx, e.ny) == (128, 64) and e.display_scale == 6.0 and e.display_size == [768, 384]
    assert e.acquired_rows == 40 and e.default_channel == "Z" and e.sim_s == 12.5
    summ = e.summary()
    assert (summ["rows_acquired"], summ["rows_total"], summ["display_scale"]) == (40, 64, 6)
    assert "display_scale_frac" not in summ and summ["kind"] == "partial" and summ["sim_s"] == 12.5
    assert e.channel_directions("Current") == ["forward"] and e.directions == ["forward", "backward"]
    for suffix in (".npz", ".png"):
        assert (tmp_path / "a" / f"t0003.p0{suffix}").exists()
    # lossless: float32 values come back exactly
    got = arch.array(e.fid, "Z", "forward")
    np.testing.assert_array_equal(got, arr["Z/forward"].astype(np.float32))
    assert not got.flags.writeable
    # re-open: the index and the values survive
    again = Archive(tmp_path / "a")
    assert [x.fid for x in again.frames()] == ["t0003.p0"] and again.load_warnings == []
    assert again.get("t0003.p0").summary() == e.summary()
    np.testing.assert_array_equal(again.array("t0003.p0", "current"), arr["Current/forward"].astype(np.float32))
    assert again.current_turn == 3


def test_frame_ids_per_turn_and_kind(tmp_path):
    arch = Archive(tmp_path)
    ids = [arch.add_partial(t, _arrays(16, 16, i), geometry=GEOM, meta={}).fid
           for i, t in enumerate([0, 0, 1, 1, 1])]
    assert ids == ["t0000.p0", "t0000.p1", "t0001.p0", "t0001.p1", "t0001.p2"]
    m = arch.add_derived(1, F.encode_png(np.zeros((10, 20), np.uint8)), meta={"tool": "x", "sources": ["t0001.p2"]})
    assert m.fid == "t0001.m0" and m.display_size == [20, 10] and not m.is_image
    assert [e.fid for e in arch.frames(turn=1, kind="p")] == ["t0001.p0", "t0001.p1", "t0001.p2"]
    assert arch.latest("p").fid == "t0001.p2" and arch.latest("s") is None and arch.latest("m").fid == "t0001.m0"
    idx = arch.index()
    assert len(idx) == 6 and idx[-1]["tool"] == "x"
    # one vocabulary: the id under "frame", the kind as the frame-id letter, no duplicate "type"
    assert (idx[0]["frame"], idx[0]["kind"]) == ("t0000.p0", "partial") and "type" not in idx[0] and "fid" not in idx[0]
    assert (idx[-1]["frame"], idx[-1]["kind"], idx[-1]["frames"]) == ("t0001.m0", "derived", ["t0001.p2"])
    assert arch.get("t0001.m0").kind == "m" and arch.get("t0000.p0").kind == "p"   # the attribute keeps the letter


def test_get_unknown_frame_message(tmp_path):
    arch = Archive(tmp_path)
    arch.add_partial(2, _arrays(16, 16), geometry=GEOM, meta={})
    with pytest.raises(KeyError, match="turn 2 has"):
        arch.get("t0002.p5")
    with pytest.raises(KeyError, match="not a frame id"):
        arch.get("latest")
    with pytest.raises(KeyError, match="turns 0..2"):
        arch.get("t0009.s0")


def test_array_errors(tmp_path):
    arch = Archive(tmp_path)
    e = arch.add_partial(0, _arrays(16, 16), geometry=GEOM, meta={})
    with pytest.raises(KeyError, match="no backward pass"):
        arch.array(e.fid, "Current", "backward")
    with pytest.raises(KeyError, match="this frame has"):
        arch.array(e.fid, "dIdV")
    with pytest.raises(ValueError):
        arch.array(e.fid, "Z", "sideways")


def test_default_render_label_and_observation(tmp_path):
    arch = Archive(tmp_path)
    arr = _arrays()
    arr["Z/forward"][60:] = np.nan
    e = arch.add_partial(7, arr, geometry=GEOM, meta={})
    blocks = arch.observation(e.fid)
    assert len(blocks) == 1 and isinstance(blocks[0], ImageBlock)
    lab = blocks[0].label
    assert lab.startswith('<visual kind="current" frame="t0007.p0" channel="Z" direction="forward" flatten="plane"')
    for part in ('scale="6"', 'size="768x384"', 'region="0,0,768,384"', 'field_nm="20x10"', "z_range_pm=",
                 'rows_acquired="60/64"'):
        assert part in lab, part
    rgb = _png_rgb(blocks[0].png)
    assert rgb.shape == (384, 768, 3)
    assert tuple(rgb[-1, 0]) == F.NAN_RGB
    assert blocks[0].png == (tmp_path / "t0007.p0.png").read_bytes()
    assert 'kind="historical"' in arch.observation(e.fid, kind="historical")[0].label


def test_grid_archive_draws_and_labels_the_grid(tmp_path):
    geom = {"cx_nm": 0.0, "cy_nm": 0.0, "w_nm": 70.0, "h_nm": 70.0, "angle_deg": 0.0}
    arr = _arrays(ny=256, nx=256)
    plain, lined = Archive(tmp_path / "plain"), Archive(tmp_path / "grid", grid=True)
    e1 = plain.add_partial(1, arr, geometry=geom, meta={})
    e2 = lined.add_partial(1, arr, geometry=geom, meta={})
    (b1,), (b2,) = plain.observation(e1.fid), lined.observation(e2.fid)
    assert "grid_nm" not in b1.label and 'grid_nm="5"' in b2.label
    a, b = _png_rgb(b1.png).astype(int), _png_rgb(b2.png).astype(int)
    lines = F.grid_lines(0, 256, 5.0 / (70 / 256), 3, 1, 768)
    on = np.zeros(a.shape[:2], dtype=bool)
    on[:, lines] = True
    on[lines, :] = True
    assert (a[~on] == b[~on]).all() and (b[on] == (3 * a[on] + np.asarray(F.GRID_RGB)) // 4).all()
    # a zoomed render gets its own, finer grid; the meta says which
    _, meta = lined.render(e2.fid, region={"x": 300, "y": 300, "width": 96, "height": 96}, label_kind="inspection")
    assert meta["grid_nm"] == 1.0 and 'grid_nm="1"' in meta["label"]
    assert plain.render(e1.fid)[1]["grid_nm"] is None
    # the colour scale is the data's, not the grid's
    assert plain.render(e1.fid)[1]["black"] == lined.render(e2.fid)[1]["black"]


def test_render_region_channel_and_meta(tmp_path):
    arch = Archive(tmp_path)
    e = arch.add_partial(0, _arrays(), geometry=GEOM, meta={})
    png, meta = arch.render(e.fid, channel="current", region={"x": 13, "y": 7, "width": 60, "height": 30},
                            flatten="none", clip_pct=1.0, label_kind="inspection", label_attrs={"index": 1})
    # k = 6: the region widens to whole scan pixels [2, 13) x [1, 7)
    assert meta["native_px"] == [2, 1, 13, 7] and meta["region_px"] == [12, 6, 66, 36]
    assert meta["image_size_px"] == [11 * 69, 6 * 69] and meta["image_px_per_scan_px"] == 69
    assert meta["unit"] == "pA" and meta["channel"] == "Current" and meta["flatten"] == "none"
    assert meta["fmt"].key("black") == "black_pa" and meta["black"] <= meta["white"]
    assert meta["label"].startswith('<visual kind="inspection" index="1" frame="t0000.p0" channel="Current"')
    assert "range_pa=" in meta["label"] and 'magnification="11.5"' in meta["label"]
    X, Y = F.display_to_scan_nm(e, 12, 6)
    assert meta["corners_nm"]["top_left"] == pytest.approx([round(X, 4), round(Y, 4)])
    assert _png_rgb(png).shape == (6 * 69, 11 * 69, 3)
    with pytest.raises(ValueError, match="leaves the frame"):
        arch.render(e.fid, region={"x": 760, "y": 0, "width": 20, "height": 20})


def test_derived_with_values(tmp_path):
    arch = Archive(tmp_path / "d")
    d = np.random.default_rng(0).normal(0, 1e-12, (32, 48))
    png = F.encode_png(np.zeros((32 * 16, 48 * 16), np.uint8))
    g = {"cx_nm": 0.0, "cy_nm": 0.0, "w_nm": 4.8, "h_nm": 3.2, "angle_deg": 0.0, "nx": 48, "ny": 32}
    e = arch.add_derived(4, png, meta={"tool": "diff", "sources": ["t0001.s0", "t0004.s0"], "geometry": g,
                                       "units": {"diff": "m"}, "label_attrs": {"map": "b minus a"}},
                         arrays={"diff/forward": d})
    assert e.is_image and e.channels == ["diff"] and e.display_scale == 16.0
    np.testing.assert_array_equal(arch.array(e.fid, "diff"), d.astype(np.float32))
    lab = arch.observation(e.fid)[0].label
    assert lab.startswith('<visual kind="derived" frame="t0004.m0" derived="diff" frames="t0001.s0,t0004.s0"')
    again = Archive(tmp_path / "d")
    np.testing.assert_array_equal(again.array(e.fid, "diff"), d.astype(np.float32))
    png2, meta = again.render(e.fid, region={"x": 0, "y": 0, "width": 100, "height": 50})
    assert meta["magnification"] == 7 and _png_rgb(png2).shape == (350, 700, 3)


def test_text_grid(tmp_path):
    arch = Archive(tmp_path)
    ny, nx = 64, 64
    yy, xx = np.mgrid[0:ny, 0:nx]
    cur = (xx * 1.0 + yy * 100.0) * 1e-12            # constant height: Z flat, Current is the default
    cur[48:] = np.nan
    e = arch.add_partial(0, {"Z": np.full((ny, nx), 1e-9), "Current": cur}, geometry=GEOM, meta={})
    assert e.default_channel == "Current"
    txt = arch.text_grid(e.fid, n=8)
    lines = txt.splitlines()
    assert lines[0].startswith('<visual kind="text_grid" frame="t0000.p0" channel="Current"')
    assert 'unit="pA"' in lines[0] and 'flatten="none"' in lines[0] and 'decimals="1"' in lines[0]
    rows = [ln.split() for ln in lines[2:]]
    assert len(rows) == 8 and all(len(r) == 8 for r in rows)
    assert rows[-1] == ["."] * 8 and rows[-2] == ["."] * 8          # never acquired
    # k = 12: cell c samples display px 48(2c+1) -> scan px 4 + 8c (bin centers)
    for r in range(6):
        assert [float(v) for v in rows[r]] == [(4 + 8 * c) + 100 * (4 + 8 * r) for c in range(8)]
        assert all(v.endswith(".0") for v in rows[r])
    # Z frames are plane-flattened, in pm
    z = (np.random.default_rng(0).normal(0, 2e-12, (32, 32)) + 5e-12 * np.mgrid[0:32, 0:32][1])
    ez = arch.add_partial(1, {"Z": z}, geometry=GEOM, meta={})
    head = arch.text_grid(ez.fid, n=4).splitlines()[0]
    assert 'channel="Z"' in head and 'flatten="plane"' in head and 'unit="pm"' in head and 'decimals="0"' in head
    with pytest.raises(ValueError):
        arch.text_grid(e.fid, n=0)


def test_sample_axis_bin_centres():
    disp, nat = sample_axis(10, 100, 4, 2, 1, 384)
    assert disp.tolist() == [10 + (2 * c + 1) * 100 // 8 for c in range(4)]
    assert nat.tolist() == [d // 2 for d in disp.tolist()]
    disp, nat = sample_axis(0, 10, 3, 1, 3, 30)           # block-mean display: native under the centre
    assert disp.tolist() == [1, 5, 8] and nat.tolist() == [5, 15, 25]


def test_corrupt_index_line_and_missing_files(tmp_path):
    arch = Archive(tmp_path)
    e1 = arch.add_partial(0, _arrays(8, 8), geometry=GEOM, meta={})
    e2 = arch.add_partial(1, _arrays(8, 8, 1), geometry=GEOM, meta={})
    with open(tmp_path / "index.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"fid": "t0002.p0", "turn": 2\n')        # a torn write
    (tmp_path / f"{e2.fid}.npz").unlink()
    again = Archive(tmp_path)
    assert [e.fid for e in again.frames()] == [e1.fid]
    assert len(again.load_warnings) == 2


def test_concurrent_adds_get_unique_ids(tmp_path):
    arch = Archive(tmp_path)
    png = F.encode_png(np.zeros((4, 4), np.uint8))
    out: list[str] = []
    lock = threading.Lock()

    def work():
        for _ in range(10):
            fid = arch.add_derived(5, png, meta={"tool": "t"}).fid
            with lock:
                out.append(fid)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 60 and len(set(out)) == 60
    lines = (tmp_path / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 60 and all(json.loads(ln)["fid"] in out for ln in lines)


def test_entry_record_roundtrip():
    e = FrameEntry(fid="t0001.s0", turn=1, kind="s", index=0, channels=["Z"], geometry=dict(GEOM, nx=4, ny=4),
                   nm_per_px=5.0, display_scale=0.5, extra={"x": np.float32(1.5)})
    rec = json.loads(json.dumps(e.to_record()))
    back = FrameEntry.from_record(rec)
    assert back.scale_pair == (1, 2) and back.extra["x"] == 1.5 and back.geometry == e.geometry
    with pytest.raises(ValueError):
        FrameEntry.from_record({"fid": "t0001.s0"})


def test_add_scan_and_spectrum_from_files(tmp_path):
    ny, nx = 32, 48
    z = np.random.default_rng(0).normal(0, 5e-12, (ny, nx)) + 2e-9
    cur = np.full((ny, nx), 1e-10)
    sxm = write_sxm(tmp_path / "f.sxm", {"Z": (z, z), "Current": (cur, cur)}, w_nm=48.0, h_nm=32.0,
                    cx_nm=1.0, cy_nm=2.0, scan_dir="up")
    arch = Archive(tmp_path / "arch")
    e = arch.add_scan(2, sxm, sim_s=3.0)
    assert e.fid == "t0002.s0" and e.source_name == "f.sxm" and e.scan_dir == "up"
    assert e.units == {"Z": "m", "Current": "A"} and e.bias_v == pytest.approx(0.1)
    summ = e.summary()
    assert summ["setpoint_pa"] == pytest.approx(100.0) and summ["center_nm"] == [1.0, 2.0]
    assert summ["display_scale"] == 16 and summ["display_size_px"] == [768, 512] and "rows_acquired" not in summ
    np.testing.assert_allclose(arch.array(e.fid, "Z"), z.astype(np.float32))
    assert e.display_scale == 16.0 and e.display_size == [768, 512]
    d = arch.add_spectrum(2, write_dat(tmp_path / "s.dat"))
    assert d.fid == "t0002.d0" and d.channels[0] == "Bias calc (V)" and d.nx == 21
    assert (tmp_path / "arch" / "t0002.d0.dat").read_bytes() == (tmp_path / "s.dat").read_bytes()
    assert d.summary()["position_nm"] == [1.25, -2.0]
    lab = arch.observation(d.fid)[0].label
    assert 'plot="spectrum"' in lab and 'x="Bias calc [V]"' in lab
    png, meta = arch.render(d.fid, y_col="Current", x_range=(-0.1, 0.1))
    assert meta["n_points"] == 5 and meta["y"] == [{"column": "Current (A)", "unit": "pA"}]
    assert d.summary()["setpoint_pa"] == pytest.approx(100.0)
    with pytest.raises(ValueError, match="x_range instead of region"):
        arch.render(d.fid, region={"x": 0, "y": 0, "width": 5, "height": 5})
    assert "Bias calc (V)" in arch.text_grid(d.fid, n=4)
    again = Archive(tmp_path / "arch")
    assert [x.fid for x in again.frames()] == ["t0002.s0", "t0002.d0"]


# ── origin tag (MAST 7.0: what caused a frame) ─────────────────────────────
def test_origin_tag_is_kept_summarized_and_survives_reopen(tmp_path):
    z = np.random.default_rng(0).normal(0, 5e-12, (16, 16))
    sxm = write_sxm(tmp_path / "f.sxm", {"Z": (z, z)}, w_nm=16.0, h_nm=16.0)
    dat = write_dat(tmp_path / "s.dat")
    plain, tagged = Archive(tmp_path / "plain"), Archive(tmp_path / "tagged")
    ps, pd = plain.add_scan(1, sxm), plain.add_spectrum(1, dat)
    ts, td = tagged.add_scan(1, sxm, origin="act"), tagged.add_spectrum(1, dat, origin="external")
    assert (ts.extra["origin"], td.extra["origin"]) == ("act", "external")
    assert "origin" not in ps.extra and "origin" not in pd.extra
    assert not any("source" in e.extra for e in (ps, pd, ts, td))
    for p, t, origin in ((ps, ts, "act"), (pd, td, "external")):
        ps_sum, ts_sum = p.summary(), t.summary()
        assert "origin" not in ps_sum
        assert list(ts_sum)[:4] == ["frame", "turn", "kind", "origin"] and ts_sum["origin"] == origin
        assert ts_sum["source"] == p.source_name                   # "source" stays the file name
        # the tag only adds "origin": everything else the model reads is byte-identical
        assert json.dumps({k: v for k, v in ts_sum.items() if k != "origin"}) == json.dumps(ps_sum)
    # no tag: None and "" leave the entry as before
    assert "origin" not in tagged.add_scan(2, sxm, origin=None).extra
    assert "origin" not in tagged.add_scan(2, sxm, origin="").summary()
    back = FrameEntry.from_record(json.loads(json.dumps(td.to_record())))
    assert back.extra["origin"] == "external" and back.summary() == td.summary()
    again = Archive(tmp_path / "tagged")
    assert again.load_warnings == []
    assert again.get(ts.fid).extra["origin"] == "act" and again.get(td.fid).summary()["origin"] == "external"
    assert again.get(ts.fid).summary() == ts.summary()
    assert "origin" not in again.get("t0002.s0").summary()


# ── reopening after a restart ──────────────────────────────────────────────
def test_reopen_continues_frame_ids_and_the_turn(tmp_path):
    png = F.encode_png(np.zeros((4, 4), np.uint8))
    arch = Archive(tmp_path)
    first = [arch.add_partial(t, _arrays(16, 16, t), geometry=GEOM, meta={}).fid for t in (0, 3, 3)]
    first.append(arch.add_derived(arch.current_turn, png, meta={"tool": "t"}).fid)
    assert first == ["t0000.p0", "t0003.p0", "t0003.p1", "t0003.m0"]
    again = Archive(tmp_path)
    assert again.current_turn == 3 and again.load_warnings == []
    more = [again.add_partial(3, _arrays(16, 16, 9), geometry=GEOM, meta={}).fid,
            again.add_derived(again.current_turn, png, meta={"tool": "t"}).fid,
            again.add_partial(5, _arrays(16, 16, 5), geometry=GEOM, meta={}).fid]
    assert more == ["t0003.p2", "t0003.m1", "t0005.p0"] and again.current_turn == 5
    third = Archive(tmp_path)
    assert [e.fid for e in third.frames()] == first + more and third.current_turn == 5
    np.testing.assert_array_equal(third.array("t0003.p0", "Z"), _arrays(16, 16, 3)["Z/forward"].astype(np.float32))


def test_reopen_after_a_torn_last_line_keeps_the_next_record(tmp_path):
    """A torn last index line (the writer died mid-line) used to swallow the next record: it was
    appended to the torn line, and that line was dropped at the following reopen."""
    arch = Archive(tmp_path)
    arch.add_partial(0, _arrays(8, 8), geometry=GEOM, meta={})
    with open(tmp_path / "index.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"fid":"t0001.p0","turn":1,"kind":"p"')     # no newline
    torn = (tmp_path / "index.jsonl").read_bytes()
    again = Archive(tmp_path)
    assert len(again.load_warnings) == 1
    assert (tmp_path / "index.jsonl").read_bytes() == torn       # opening alone writes nothing
    e = again.add_partial(1, _arrays(8, 8, 1), geometry=GEOM, meta={})
    third = Archive(tmp_path)
    assert [x.fid for x in third.frames()] == ["t0000.p0", e.fid] and third.get(e.fid).summary() == e.summary()
    assert len(third.load_warnings) == 1                          # only the torn line itself


def test_reopen_never_reuses_the_id_of_a_record_whose_files_are_gone(tmp_path):
    """The agent may have seen a frame whose files were deleted later: its id is not handed out
    again, and its turn still counts."""
    arch = Archive(tmp_path)
    arch.add_partial(2, _arrays(8, 8), geometry=GEOM, meta={})
    gone = arch.add_partial(4, _arrays(8, 8, 1), geometry=GEOM, meta={})
    (tmp_path / f"{gone.fid}.npz").unlink()
    again = Archive(tmp_path)
    assert [x.fid for x in again.frames()] == ["t0002.p0"] and len(again.load_warnings) == 1
    assert again.current_turn == 4
    assert again.add_partial(4, _arrays(8, 8, 2), geometry=GEOM, meta={}).fid == "t0004.p1"


def test_reopen_survives_odd_index_lines(tmp_path):
    """JSON that is not an object is skipped (it used to raise from the constructor), and a record
    whose strings hold U+2028 / U+0085 is read back whole (splitlines() split it in two)."""
    arch = Archive(tmp_path)
    e = arch.add_partial(0, _arrays(8, 8), geometry=GEOM, meta={"source_name": "scan\u2028one\x85two"})
    with open(tmp_path / "index.jsonl", "a", encoding="utf-8") as fh:
        fh.write('[1, 2]\n"text"\n')
    again = Archive(tmp_path)
    assert [x.fid for x in again.frames()] == [e.fid] and len(again.load_warnings) == 2
    assert again.get(e.fid).summary()["source"] == "scan\u2028one\x85two"
