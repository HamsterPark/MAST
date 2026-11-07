"""``ReadSiteMarks``(C 件):读标注 → 吸附 → 页面坐标换算成扫描器米。

纯软件:假 marks.json + 合成 .sxm,不碰仪器(``execute(None, ...)``)。

几件事被刻意分开断言,别合并:

* **换算对不对** —— θ=0 是蓝本的无旋转式;θ=30° 有一条手算的点(正角顺时针,R(−θ))。
  另有一条**与符号无关**的不变量(离帧心的距离不变、夹角 = |θ|),和一条「结果恒等于
  ``mosaic.px_to_m``」—— 后者是单一来源的可执行版本。
* **「读不到」与「没有」** —— marks.json 缺失 / 坏 → degraded;文档在但没标点 → 空、不 degraded。
* **页面坐标的原点** —— 由页面 META 的 ``x0/y1`` 定义,读它不假设它:A 件的绝对 nm、契约原文的
  帧内写法得到同一个站位;META 没说且标注出框时,必须被点名而不是把偏移再加一次。
* **A↔C 的接口** —— 有一条直接吃 ``PrepareSiteMarkingPage`` 真实输出的测试;A 的 META 变了它先红。
"""
from __future__ import annotations

import json
import logging
import math
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

_MASTV2_ROOT = str(Path(__file__).resolve().parents[5] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _n in list(sys.modules):
    if _n == "mast" or _n.startswith("mast."):
        _f = getattr(sys.modules[_n], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_n]

from mast.core.instrument_lock import needs_token  # noqa: E402
from mast.io.mosaic import px_to_m  # noqa: E402
from mast.skills.builtins import site_marking_read as smr  # noqa: E402
from mast.skills.builtins.site_marking_read import ReadSiteMarks  # noqa: E402

PAGE = "pg-test"
# 蓝本那一类的偏移:帧心离原点很远,所以「再加一次偏移」会错得很明显。
META = {"cx": -75.0, "cy": 153.0, "w": 8.0, "h": 8.0, "angle": 0.0, "file": "ref.sxm"}


# ── 夹具与小工具 ──────────────────────────────────────────────────────────

@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    return tmp_path


def page_dir_of(page=PAGE):
    """页面目录。路径的定义只有一份(A 件);测试也问它,不自己拼 —— 目录布局哪天调整,测试跟着走。"""
    from mast.skills.builtins.site_marking import site_marking_dir

    return site_marking_dir(page)


def put_marks(root, doc, *, meta=None, page=PAGE):
    d = page_dir_of(page)
    d.mkdir(parents=True, exist_ok=True)
    (d / "marks.json").write_text(json.dumps(doc), encoding="utf-8")
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def site(n, x, y, **kw):
    return {"n": n, "x": x, "y": y, "kind": "auto", "wide": True, "low": True,
            "note": "", "created_at": "2026-10-26T12:00:00Z", **kw}


def doc_of(*sites, **extra):
    """页面真实的文档形状:``{"sites": {id: {...}}}``。"""
    return {"sites": {f"id{i}": s for i, s in enumerate(sites)}, **extra}


def run(**params):
    params.setdefault("page_id", PAGE)
    return ReadSiteMarks().execute(None, params)


def by_n(res):
    return {s["n"]: s for s in res.data["sites"]}


def hand(x, y, cx, cy, deg):
    """换算公式,手写在测试里(正 SCAN_ANGLE = 顺时针,R(−θ)),nm → m。

    注意:符号必须是 R(−θ),而非 R(+θ)(逆时针),与图库 ``to_frame`` 同向而非互逆;
    px_to_m 修成 R(−θ) 后这里一起改。``test_px_to_m_rotation`` 用往返恒等钉死符号。"""
    t = math.radians(deg)
    return ((cx + x * math.cos(t) + y * math.sin(t)) * 1e-9,
            (cy - x * math.sin(t) + y * math.cos(t)) * 1e-9)


# ── 合成帧 ────────────────────────────────────────────────────────────────

NPX = 128
W_NM = 8.0

# 帧内 nm(帧心原点、+y 向上)。六聚体环 + 一条链 + 一个孤立分子。
_RING_C = (-1.5, 1.0)
RING = [(_RING_C[0] + 0.7 * math.cos(math.radians(a)), _RING_C[1] + 0.7 * math.sin(math.radians(a)))
        for a in range(0, 360, 60)]
CHAIN = [(x, -1.5) for x in (-1.4, -0.7, 0.0, 0.7, 1.4)]
LONER = (2.6, 1.8)
MOLECULES = RING + CHAIN + [LONER]


def frame_array(mols=MOLECULES, *, tilt=True, noise=0.0, seed=0):
    """行 0 = 帧上沿的 Z 图(米)。每个分子一个高斯,外加一张缓倾的平面。"""
    col = (np.arange(NPX) + 0.5) / NPX * W_NM - W_NM / 2
    row = W_NM / 2 - (np.arange(NPX) + 0.5) / NPX * W_NM
    X, Y = np.meshgrid(col, row)
    z = np.zeros((NPX, NPX))
    for mx, my in mols:
        z += 1e-10 * np.exp(-((X - mx) ** 2 + (Y - my) ** 2) / (2 * 0.15 ** 2))
    if tilt:
        z += 0.5e-12 * np.arange(NPX)[None, :] + 0.3e-12 * np.arange(NPX)[:, None]
    if noise:
        z += np.random.default_rng(seed).normal(0.0, noise, z.shape)
    return z


def write_sxm(path, arr, *, offset_nm=(META["cx"], META["cy"]), w_nm=W_NM, angle_deg=0.0,
              scan_dir="down"):
    ny, nx = arr.shape
    rng_m = w_nm * 1e-9
    header = (
        ":NANONIS_VERSION:\n2\n"
        ":SCANIT_TYPE:\n\t FLOAT            MSBFIRST\n"
        ":REC_DATE:\n 26.10.2026\n:REC_TIME:\n12:00:00\n"
        ":BIAS:\n\t1.000000E-1\n"
        f":SCAN_PIXELS:\n{nx:>10d}{ny:>10d}\n"
        f":SCAN_RANGE:\n{rng_m:>19.6E}{rng_m:>19.6E}\n"
        f":SCAN_OFFSET:\n{offset_nm[0] * 1e-9:>19.6E}{offset_nm[1] * 1e-9:>19.6E}\n"
        f":SCAN_ANGLE:\n{angle_deg:>12.6E}\n"
        f":SCAN_DIR:\n{scan_dir}\n"
        ":Z-CONTROLLER>SETPOINT:\n10.0000E-12\n"
        ":DATA_INFO:\n\tChannel\tName\tUnit\tDirection\tCalibration\tOffset\n"
        "\t14\tZ\tm\tfwd\t9.000E-9\t0.000E+0\n"
        ":SCANIT_END:\n\n"
    )
    # 文件里「up」扫描的第一行是帧底:图是行 0 = 上沿,所以要翻过来写。
    data = np.flipud(arr) if scan_dir == "up" else arr
    flat = data.astype(np.float32).ravel()
    path.write_bytes(header.encode("utf-8") + b"\x1a\x04" + struct.pack(">%df" % flat.size, *flat.tolist()))
    return path


@pytest.fixture()
def frame(tmp_path):
    return write_sxm(tmp_path / "ref.sxm", frame_array())


# ── 换算 ──────────────────────────────────────────────────────────────────

def test_theta_zero_is_the_no_rotation_case(root):
    put_marks(root, doc_of(site(1, 3.0, 4.0), site(2, -2.5, 0.25)), meta=META)
    res = run(refine=False)
    assert res.success and res.data["angle_deg"] == 0.0 and res.data["coords_trustworthy"] is True
    s = by_n(res)
    assert s[1]["scanner_x_m"] == pytest.approx((-75.0 + 3.0) * 1e-9, rel=1e-12)
    assert s[1]["scanner_y_m"] == pytest.approx((153.0 + 4.0) * 1e-9, rel=1e-12)
    assert s[2]["scanner_x_m"] == pytest.approx((-75.0 - 2.5) * 1e-9, rel=1e-12)
    assert s[2]["scanner_y_m"] == pytest.approx((153.0 + 0.25) * 1e-9, rel=1e-12)


def test_theta_30_hand_checked(root):
    """cx=10, cy=-20, θ=30°, 站位 (3, 4) nm(正角顺时针 = R(−θ)):

        Xs = 10 + 3·cos30° + 4·sin30° = 10 + 2.5980762 + 2     = 14.5980762 nm
        Ys = -20 − 3·sin30° + 4·cos30° = -20 − 1.5 + 3.4641016 = -18.0358984 nm

    这条钉的是核实过的符号(正 SCAN_ANGLE = 顺时针)。
    ``px_to_m`` 现在是图库 ``to_frame`` 的逆(``test_px_to_m_rotation`` 往返恒等钉死);
    它红,说明换算的符号被人改回去了,不是约定变了。
    """
    meta = {"cx": 10.0, "cy": -20.0, "w": 12.0, "h": 12.0, "angle": 30.0}
    put_marks(root, doc_of(site(1, 3.0, 4.0), site(2, -2.0, 5.0)), meta=meta)
    res = run(refine=False)
    s = by_n(res)
    assert s[1]["scanner_x_m"] == pytest.approx(14.5980762e-9, abs=1e-15)
    assert s[1]["scanner_y_m"] == pytest.approx(-18.0358984e-9, abs=1e-15)
    assert s[2]["scanner_x_m"] == pytest.approx(10.7679492e-9, abs=1e-15)   # 10 - 1.7320508 + 2.5
    assert s[2]["scanner_y_m"] == pytest.approx(-14.669873e-9, abs=1e-15)   # -20 + 1 + 4.330127
    assert (s[1]["scanner_x_m"], s[1]["scanner_y_m"]) == pytest.approx(hand(3, 4, 10, -20, 30), abs=1e-18)


@pytest.mark.parametrize("deg", [-137.5, -30.0, 12.0, 90.0, 180.0])
def test_conversion_is_px_to_m_and_not_a_private_copy(root, deg):
    """换算恒等于全仓唯一的 ``px_to_m``(用一个真实的 256 px 格去算,与技能内部的虚拟格无关)。"""
    cx, cy, w, h = 40.0, -60.0, 20.0, 10.0
    meta = {"cx": cx, "cy": cy, "w": w, "h": h, "angle": deg}
    pts = [(1.5, -2.0), (-7.0, 3.0), (9.0, 4.5), (0.0, 0.0)]
    put_marks(root, doc_of(*[site(i + 1, x, y) for i, (x, y) in enumerate(pts)]), meta=meta)
    res = run(refine=False)
    assert res.success
    for i, (x, y) in enumerate(pts):
        nx = ny = 256
        px = (x / w + 0.5) * nx - 0.5
        py = (0.5 - y / h) * ny - 0.5
        want = px_to_m(px, py, nx=nx, ny=ny, cx_m=cx * 1e-9, cy_m=cy * 1e-9,
                       w_m=w * 1e-9, h_m=h * 1e-9, angle_deg=deg)
        got = by_n(res)[i + 1]
        assert (got["scanner_x_m"], got["scanner_y_m"]) == pytest.approx(want, abs=1e-16)


@pytest.mark.parametrize("deg", [-90.0, -33.0, 30.0, 75.0, 180.0])
def test_rotation_keeps_distance_and_turns_by_exactly_theta(root, deg):
    """**与符号无关**的不变量:离帧心的距离不变,偏移向量转过的角度的大小 = |θ|。

    符号有争议(见模块说明)时,这一条仍然成立 —— 它守的是「没有缩放、没有错位、没有双计偏移」。
    """
    cx, cy = -75.0, 153.0
    put_marks(root, doc_of(site(1, 3.0, 4.0), site(2, -2.0, 1.0)),
              meta={"cx": cx, "cy": cy, "w": 12.0, "h": 12.0, "angle": deg})
    for s in run(refine=False).data["sites"]:
        dx, dy = s["scanner_x_m"] * 1e9 - cx, s["scanner_y_m"] * 1e9 - cy
        assert math.hypot(dx, dy) == pytest.approx(math.hypot(s["x_nm"], s["y_nm"]), rel=1e-9)
        cosang = (dx * s["x_nm"] + dy * s["y_nm"]) / (math.hypot(dx, dy) * math.hypot(s["x_nm"], s["y_nm"]))
        assert cosang == pytest.approx(math.cos(math.radians(deg)), abs=1e-9)


# ── 页面坐标的原点:读 META 的 x0/y1,不假设 ────────────────────────────────

def meta_a_style(cx=META["cx"], cy=META["cy"], w=META["w"], h=META["h"], angle=0.0, **extra):
    """PrepareSiteMarkingPage 实际写的 META:x0 = cx−w/2、y1 = cy+h/2,页面坐标是绝对 nm(未旋转)。"""
    return {"file": "ref.sxm", "px": NPX, "w": w, "h": h, "cx": cx, "cy": cy, "angle": angle,
            "x0": round(cx - w / 2, 4), "y1": round(cy + h / 2, 4), **extra}


def meta_centred(cx=META["cx"], cy=META["cy"], w=META["w"], h=META["h"], angle=0.0):
    """契约原文的写法:页面坐标就是帧内坐标(x0=-w/2, y1=+h/2)。"""
    return {"w": w, "h": h, "cx": cx, "cy": cy, "angle": angle, "x0": -w / 2, "y1": h / 2}


@pytest.mark.parametrize("deg", [0.0, 30.0])
def test_a_pages_absolute_coordinates_come_out_as_the_right_site(root, deg):
    """A 件的页面吐绝对 nm(帧心 + 沿图像轴的局部偏移,未旋转),旋转由这里做:
    站位 = 帧心 + R(θ)·(页面坐标 − 帧心)。"""
    cx, cy = 10.0, -20.0
    put_marks(root, doc_of(site(1, cx + 3.0, cy + 4.0), site(2, cx - 2.0, cy + 1.5)),
              meta=meta_a_style(cx=cx, cy=cy, angle=deg))
    res = run(refine=False)
    assert res.success
    # 符号已核实:θ=0 和 θ≠0 都可信(几何齐全、角度已知、在框内)。
    assert res.data["angle_unverified"] is False and res.data["coords_trustworthy"] is True
    s = by_n(res)
    assert (s[1]["scanner_x_m"], s[1]["scanner_y_m"]) == pytest.approx(hand(3.0, 4.0, cx, cy, deg), abs=1e-15)
    assert (s[2]["scanner_x_m"], s[2]["scanner_y_m"]) == pytest.approx(hand(-2.0, 1.5, cx, cy, deg), abs=1e-15)
    assert res.data["frame"]["page_origin_nm"] == [cx, cy] and res.data["frame"]["origin_from_meta"] is True
    if deg == 0.0:                                     # 恒等:扫描器坐标 = 页面坐标 × 1e-9
        for v in s.values():
            assert (v["scanner_x_m"], v["scanner_y_m"]) == pytest.approx(
                (v["page_x_nm"] * 1e-9, v["page_y_nm"] * 1e-9), rel=1e-12)


def test_it_reads_what_prepare_site_marking_page_actually_writes(root, tmp_path):
    """A↔C 的接口测试:用 A 件的真实输出(META、meta.json、空 marks.json 的位置),不是臆测的形状。
    A 的 META 改了(键名、原点约定),这条先红。"""
    from mast.skills.builtins.site_marking import PrepareSiteMarkingPage

    f = write_sxm(tmp_path / "ref.sxm", frame_array(), offset_nm=(10.0, -20.0), angle_deg=30.0)
    made = PrepareSiteMarkingPage().execute(
        None, {"frame_path": str(f), "page_id": PAGE, "auto_detect_molecules": False})
    assert made.success, made.error
    marks = Path(made.data["marks_path"])
    doc = json.loads(marks.read_text(encoding="utf-8"))
    assert doc["sites"] == {}                                   # 刚建的页:空,不是 degraded
    assert run(refine=False).data["count"] == 0 and run(refine=False).data["degraded"] is False

    # 页面的行为:点击给出页面 viewBox 里的值 —— 绝对 nm(帧心 (10,-20) + 局部偏移 (3,4))
    doc["sites"]["s1"] = site(1, 13.0, -16.0)
    marks.write_text(json.dumps(doc), encoding="utf-8")
    for frame_path in ("", str(f)):                             # 只靠 A 落的 meta.json / 再给文件头,结果一样
        res = run(frame_path=frame_path, refine=False)
        d = res.data
        assert res.success and d["angle_deg"] == pytest.approx(30.0) and d["geometry_source"] == "page_meta"
        # 帧转了 30° 但符号已核实:页面与文件头一致、没有出框 ⇒ 可信、无警告
        assert d["angle_unverified"] is False and d["coords_trustworthy"] is True
        assert d["warnings"] == []
        s = res.data["sites"][0]
        assert (s["x_nm"], s["y_nm"]) == pytest.approx((3.0, 4.0))
        assert (s["scanner_x_m"], s["scanner_y_m"]) == pytest.approx(hand(3.0, 4.0, 10.0, -20.0, 30.0), abs=1e-15)


def test_the_contract_style_centred_page_gives_the_same_site(root):
    """x0=-w/2 / y1=+h/2:页面坐标就是帧内偏移。同一个站位,两种页面写法得到同一个扫描器坐标。"""
    cx, cy = 10.0, -20.0
    put_marks(root, doc_of(site(1, 3.0, 4.0)), meta=meta_centred(cx=cx, cy=cy, angle=30.0))
    s = run(refine=False).data["sites"][0]
    assert (s["scanner_x_m"], s["scanner_y_m"]) == pytest.approx(hand(3.0, 4.0, cx, cy, 30.0), abs=1e-15)
    assert (s["page_x_nm"], s["page_y_nm"]) == (3.0, 4.0) and (s["x_nm"], s["y_nm"]) == (3.0, 4.0)


def test_page_and_frame_coordinates_are_both_reported(root):
    put_marks(root, doc_of(site(1, -75.0 + 3.0, 153.0 + 4.0)), meta=meta_a_style())
    s = run(refine=False).data["sites"][0]
    assert (s["page_x_nm"], s["page_y_nm"]) == (-72.0, 157.0)       # 页面怎么存的
    assert (s["x_nm"], s["y_nm"]) == pytest.approx((3.0, 4.0))      # 帧内偏移


def test_marks_that_are_absolute_while_meta_says_centred_are_still_caught(root):
    """META 声明了原点(帧心就是原点),可标注明明是绝对坐标 —— META 与页面不符,不能照单全收。"""
    put_marks(root, doc_of(site(1, -75.0 + 1.0, 153.0 + 2.0), site(2, -75.0 - 2.0, 153.0 + 1.0)),
              meta=meta_centred())
    res = run(refine=False)
    assert not res.success and res.data["reason"] == "marks_look_absolute"
    assert all(s["scanner_x_m"] is None for s in res.data["sites"])


def test_a_nonsquare_page_is_not_trusted(root):
    """A 件把非方形帧按方形显示、并在 META 里打 nonsquare:页面的 y 有失真,不能报成可信。"""
    put_marks(root, doc_of(site(1, -75.0 + 1.0, 153.0 + 1.0)),
              meta=meta_a_style(h=6.0, nonsquare=True))
    res = run(refine=False)
    assert res.success and res.data["coords_trustworthy"] is False
    assert res.data["frame"]["nonsquare"] is True and res.data["warnings"]


# ── 读标注:读不到 ≠ 没有 ─────────────────────────────────────────────────

def test_missing_marks_file_is_degraded_not_empty(root):
    res = run()
    assert not res.success
    d = res.data
    assert d["degraded"] is True and d["reason"] == "marks_missing"
    assert d["sites"] == [] and d["count"] == 0


def test_a_saved_page_with_no_marks_is_empty_not_degraded(root):
    put_marks(root, {"sites": {}}, meta=META)
    res = run(refine=False)
    assert res.success
    assert res.data["count"] == 0 and res.data["sites"] == []
    assert res.data["degraded"] is False and res.data["reason"] == ""


def test_a_doc_with_only_plan_entries_is_empty_too(root):
    put_marks(root, {"plan/marking": {"done": False}}, meta=META)
    res = run(refine=False)
    assert res.success and res.data["count"] == 0 and res.data["degraded"] is False


def test_unreadable_json_is_degraded(root):
    d = page_dir_of()
    d.mkdir(parents=True)
    (d / "marks.json").write_text("{not json", encoding="utf-8")
    res = run()
    assert not res.success and res.data["degraded"] is True and res.data["reason"] == "marks_unreadable"


def _flaky_marks_read(monkeypatch, *, fail_times):
    """marks.json 的前 ``fail_times`` 次读取抛 PermissionError(B 件替换文件的那一瞬,Windows 上会这样)。"""
    real, calls = Path.read_text, {"n": 0}

    def read_text(self, *a, **k):
        if self.name == "marks.json":
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise PermissionError("[WinError 32] busy")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(smr, "_RETRY_PAUSE_S", 0.0)
    return calls


def test_a_momentary_sharing_violation_is_retried_once(root, monkeypatch):
    put_marks(root, doc_of(site(1, 1.0, 1.0)), meta=META)
    calls = _flaky_marks_read(monkeypatch, fail_times=1)
    res = run(refine=False)
    assert res.success and res.data["count"] == 1 and calls["n"] == 2


def test_a_persistent_sharing_violation_is_degraded_after_exactly_one_retry(root, monkeypatch):
    put_marks(root, doc_of(site(1, 1.0, 1.0)), meta=META)
    calls = _flaky_marks_read(monkeypatch, fail_times=99)
    res = run(refine=False)
    assert not res.success and res.data["reason"] == "marks_unreadable" and calls["n"] == 2


def test_a_json_scalar_is_malformed_not_empty(root):
    put_marks(root, "hello", meta=META)
    res = run(refine=False)
    assert not res.success and res.data["reason"] == "marks_malformed" and res.data["degraded"] is True


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "", ".hidden", "a.b", "x" * 65, "C:evil",
                                 "con", "NUL", "com1", "lpt9"])
def test_a_bad_page_id_is_refused_before_any_disk_access(root, bad):
    res = ReadSiteMarks().execute(None, {"page_id": bad})
    assert not res.success and res.data["reason"] == "invalid_page_id"
    assert res.data["degraded"] is False              # 是输入错,不是子系统没接线


def test_sites_come_back_sorted_by_n_whatever_the_dict_order(root):
    put_marks(root, {"sites": {"zz": site(5, 1, 1), "aa": site(1, 2, 2), "mm": site(3, 0, 0)}}, meta=META)
    res = run(refine=False)
    assert [s["n"] for s in res.data["sites"]] == [1, 3, 5]
    assert [s["id"] for s in res.data["sites"]] == ["aa", "mm", "zz"]


@pytest.mark.parametrize("shape", ["list", "id_to_site"])
def test_other_doc_shapes_are_accepted(root, shape):
    ss = [site(2, 1.0, 1.0), site(1, -1.0, 2.0)]
    doc = ss if shape == "list" else {"a": ss[0], "b": ss[1]}
    put_marks(root, doc, meta=META)
    res = run(refine=False)
    assert res.success and [s["n"] for s in res.data["sites"]] == [1, 2]


def test_a_bad_entry_is_reported_not_silently_dropped(root):
    put_marks(root, {"sites": {"ok": site(1, 1.0, 1.0), "nox": {"n": 2, "y": 1.0},
                               "nan": site(3, "abc", 1.0), "not_obj": 7}}, meta=META)
    res = run(refine=False)
    assert res.success and res.data["count"] == 1
    assert {k["id"] for k in res.data["skipped"]} == {"nox", "nan", "not_obj"}


def test_all_entries_invalid_is_malformed_not_empty(root):
    put_marks(root, {"sites": {"a": {"n": 1}, "b": {"n": 2, "x": None, "y": 1.0}}}, meta=META)
    res = run(refine=False)
    assert not res.success and res.data["reason"] == "marks_malformed" and res.data["degraded"] is True


def test_missing_and_duplicate_n_are_flagged(root):
    put_marks(root, doc_of(site(1, 1, 1), site(1, 2, 2), {"x": 0.5, "y": 0.5}), meta=META)
    res = run(refine=False)
    assert res.success and res.data["count"] == 3
    assert len(res.data["warnings"]) == 2
    assert res.data["sites"][-1]["n"] is None         # 没有 n 的排最后


def test_marking_done_flag_is_surfaced_and_unknown_is_not_false(root):
    put_marks(root, doc_of(site(1, 1, 1), **{"plan/marking": {"done": True, "count": 1}}), meta=META)
    assert run(refine=False).data["marking_done"] is True
    put_marks(root, doc_of(site(1, 1, 1)), meta=META)
    assert run(refine=False).data["marking_done"] is None


def test_wide_low_note_status_are_passed_through(root):
    put_marks(root, doc_of(site(1, 1, 1, wide=False, low=True, note="x" * 300, status="done")), meta=META)
    s = run(refine=False).data["sites"][0]
    assert s["wide"] is False and s["low"] is True and len(s["note"]) == 200 and s["status"] == "done"


# ── 几何来源 ──────────────────────────────────────────────────────────────

def test_no_geometry_is_degraded_and_keeps_the_raw_marks(root):
    put_marks(root, doc_of(site(1, 1.0, 2.0)))
    res = run()
    assert not res.success and res.data["reason"] == "no_geometry" and res.data["degraded"] is True
    s = res.data["sites"][0]
    assert (s["x_nm"], s["y_nm"]) == (1.0, 2.0)       # 原始标注不丢
    assert s["scanner_x_m"] is None and s["in_frame"] is None


def test_an_unknown_angle_is_not_treated_as_zero(root):
    meta = {k: v for k, v in META.items() if k != "angle"}
    put_marks(root, doc_of(site(1, 1.0, 2.0)), meta=meta)
    res = run(refine=False)
    assert not res.success and res.data["reason"] == "angle_unknown"
    assert res.data["angle_deg"] is None and res.data["angle_known"] is False
    assert res.data["sites"][0]["scanner_x_m"] is None
    assert res.data["coords_trustworthy"] is False


def test_geometry_comes_from_the_frame_header_when_the_page_has_none(root, tmp_path):
    put_marks(root, doc_of(site(1, 3.0, 4.0)))
    f = write_sxm(tmp_path / "g.sxm", frame_array(), offset_nm=(10.0, -20.0), angle_deg=30.0)
    res = run(refine=False, frame_path=str(f))        # 只要几何:不该去读像素
    assert res.success and res.data["geometry_source"] == "frame_header"
    assert res.data["angle_deg"] == pytest.approx(30.0)
    s = res.data["sites"][0]
    assert (s["scanner_x_m"], s["scanner_y_m"]) == pytest.approx(hand(3, 4, 10, -20, 30), abs=1e-15)


def test_meta_without_angle_takes_it_from_a_matching_frame_header(root, tmp_path):
    meta = {"cx": 10.0, "cy": -20.0, "w": W_NM, "h": W_NM}
    put_marks(root, doc_of(site(1, 3.0, 4.0)), meta=meta)
    f = write_sxm(tmp_path / "g.sxm", frame_array(), offset_nm=(10.0, -20.0), angle_deg=30.0)
    res = run(refine=False, frame_path=str(f))
    assert res.success and res.data["angle_deg"] == pytest.approx(30.0)
    assert res.data["geometry_source"] == "page_meta+frame_header"


def test_a_frame_that_is_not_the_pages_frame_is_reported_and_not_used_to_refine(root, tmp_path):
    put_marks(root, doc_of(site(1, 3.0, 4.0)), meta=META)
    f = write_sxm(tmp_path / "other.sxm", frame_array(), offset_nm=(-70.0, 153.0))   # 偏移差 5 nm
    res = run(frame_path=str(f))
    assert res.success and res.data["refined"] is False
    assert any("cx" in w for w in res.data["warnings"])        # 点名是哪个量不一致
    assert res.data["coords_trustworthy"] is False
    s = res.data["sites"][0]                           # 仍按页面自己的几何换算
    assert s["scanner_x_m"] == pytest.approx((-75.0 + 3.0) * 1e-9, rel=1e-12)


def test_a_rotated_frame_is_trusted_after_the_t6_correction(root, caplog):
    """px_to_m 的符号是 R(−θ)(正角顺时针)时,θ≠0 不再因
    「符号未核实」降级 —— angle_unverified=False、coords_trustworthy=True、无 angle_sign_warning、
    不再打那条 unverified 日志。(这条替代了订正前「θ≠0 一律 flagged/untrusted/logged」那条。)"""
    put_marks(root, doc_of(site(1, 1.0, 1.0)), meta={**META, "angle": 30.0})
    with caplog.at_level(logging.WARNING, logger=smr.logger.name):
        d = run(refine=False).data
    assert d["angle_unverified"] is False and d["coords_trustworthy"] is True
    assert d["angle_sign_warning"] == "" and d["warnings"] == []
    assert d["sites"][0]["scanner_x_m"] is not None
    assert not any("unverified" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("deg", [0.0, 0.04, -0.04, 30.0, 90.0, 180.0])
def test_a_known_angle_is_never_flagged_unverified(root, deg):
    """任何**已知**的扫描角都不再触发符号未核实降级(未知角仍不给坐标,另有测试)。"""
    put_marks(root, doc_of(site(1, 1.0, 1.0)), meta={**META, "angle": deg})
    d = run(refine=False).data
    assert d["angle_unverified"] is False and d["angle_sign_warning"] == ""
    assert d["coords_trustworthy"] is True and d["warnings"] == []


# ── 帧内/帧外:绝对坐标不许被再加一次偏移 ───────────────────────────────────

def test_a_site_outside_the_frame_gets_no_scanner_coordinates(root):
    put_marks(root, doc_of(site(1, 1.0, 1.0), site(2, 9.0, 0.0)), meta=META)      # 帧半宽 4 nm
    res = run(refine=False)
    assert res.success
    s = by_n(res)
    assert s[1]["in_frame"] is True and s[1]["scanner_x_m"] is not None
    assert s[2]["in_frame"] is False and s[2]["scanner_x_m"] is None and s[2]["x_nm"] == 9.0
    assert res.data["coords_trustworthy"] is False and res.data["warnings"]


def test_the_frame_edge_has_a_small_tolerance(root):
    put_marks(root, doc_of(site(1, 4.1, -4.1), site(2, 4.6, 0.0)), meta=META)      # 容差 = 2% 帧宽 = 0.16 nm
    s = by_n(run(refine=False))
    assert s[1]["in_frame"] is True and s[2]["in_frame"] is False


def test_absolute_coordinates_are_recognised_and_never_double_counted(root):
    """页面 META 若按旧写法填 x0=cx-w/2,标注就是绝对 nm。再加一次偏移会差 |(cx,cy)| ≈ 170 nm。"""
    absolute = [(-76.3, 154.9), (-74.1, 151.2), (-75.0, 153.0 + 3.0)]
    put_marks(root, doc_of(*[site(i + 1, x, y) for i, (x, y) in enumerate(absolute)]), meta=META)
    res = run(refine=False)
    assert not res.success and res.data["reason"] == "marks_look_absolute"
    assert all(s["scanner_x_m"] is None for s in res.data["sites"])
    assert res.data["degraded"] is True and res.data["coords_trustworthy"] is False


def test_all_out_of_frame_but_not_absolute_is_a_different_reason(root):
    put_marks(root, doc_of(site(1, 40.0, 0.0), site(2, 0.0, -50.0)), meta=META)
    res = run(refine=False)
    assert not res.success and res.data["reason"] == "all_sites_out_of_frame"


def test_a_centred_frame_cannot_tell_absolute_from_internal_and_need_not(root):
    put_marks(root, doc_of(site(1, 1.0, 2.0)), meta={**META, "cx": 0.0, "cy": 0.0})
    res = run(refine=False)
    assert res.success and res.data["sites"][0]["scanner_x_m"] == pytest.approx(1e-9)


# ── 精确化(合成帧) ───────────────────────────────────────────────────────

def refine_marks():
    return doc_of(
        site(1, LONER[0] + 0.05, LONER[1] - 0.04),                     # 孤立分子旁 → mol
        site(2, 0.37, -1.47),                                          # 链上两分子之间 → hbond
        site(3, _RING_C[0] + 0.05, _RING_C[1] + 0.05),                 # 环心 → ring
        site(4, 2.5, -3.0),                                            # 离一切分子 > 1 nm → au
        site(5, 0.0, 3.0, kind="fixed"),                               # 明确按原位
        site(6, -3.0, -3.0, kind="mol"),                               # 明确要 mol,但 3 nm 内没有 → 原位
        site(7, 2.5, -3.2, kind="hbond"),                              # 明确要 hbond,但附近没有 → 原位(蓝本这里 TypeError)
    )


@pytest.mark.parametrize("angle", [0.0, 30.0])
def test_marks_snap_to_molecules_whatever_the_scan_angle(root, tmp_path, angle):
    """吸附发生在帧内(沿图像自己的轴),所以与扫描角无关;角度只影响最后的扫描器坐标。"""
    meta = {**META, "angle": angle}
    put_marks(root, refine_marks(), meta=meta)
    f = write_sxm(tmp_path / "r.sxm", frame_array(), angle_deg=angle)
    res = run(frame_path=str(f))
    assert res.success and res.data["refined"] is True, res.data["refine_note"]
    s = by_n(res)

    assert s[1]["rkind"] == "mol"
    assert (s[1]["rx_nm"], s[1]["ry_nm"]) == pytest.approx(LONER, abs=0.05)
    assert s[2]["rkind"] == "hbond"
    assert (s[2]["rx_nm"], s[2]["ry_nm"]) == pytest.approx((0.35, -1.5), abs=0.05)
    assert s[3]["rkind"] == "ring" and len(s[3]["anchor"]) == 6
    assert (s[3]["rx_nm"], s[3]["ry_nm"]) == pytest.approx(_RING_C, abs=0.05)
    assert s[4]["rkind"] == "au" and s[4]["anchor"] == [] and s[4]["anchor_offset_nm"] is None
    assert (s[4]["rx_nm"], s[4]["ry_nm"]) == (2.5, -3.0)
    assert s[5]["rkind"] == "fixed" and (s[5]["rx_nm"], s[5]["ry_nm"]) == (0.0, 3.0)
    assert s[6]["rkind"] == "fixed" and (s[6]["rx_nm"], s[6]["ry_nm"]) == (-3.0, -3.0)
    assert s[7]["rkind"] == "fixed" and (s[7]["rx_nm"], s[7]["ry_nm"]) == (2.5, -3.2)
    assert all(v["position_source"] == "refined" for v in s.values())

    # 最终坐标 = 精确化后的位置按契约公式换算(帧心 (-75, 153) nm);
    # rx_nm 回显只保留 4 位小数(0.1 pm),所以容差取 0.2 pm。
    for v in s.values():
        want = hand(v["rx_nm"], v["ry_nm"], -75.0, 153.0, angle)
        assert (v["scanner_x_m"], v["scanner_y_m"]) == pytest.approx(want, abs=2e-13)
    assert res.data["snap_profile"]["max_snap_nm"] == 1.0          # 用过的数回显出来


@pytest.mark.parametrize("angle", [0.0, 30.0])
def test_a_pages_absolute_marks_snap_and_convert_end_to_end(root, tmp_path, angle):
    """A 件 → C 的真实接口:页面坐标是绝对 nm,精确化与换算后与「帧内」写法给出同一批站位,
    并且 page_rx_nm 就是页面坐标系里的精确化位置(= 帧内 + 帧心),谁回写页面直接用。"""
    cx, cy = META["cx"], META["cy"]
    shifted = doc_of(*[site(s["n"], s["x"] + cx, s["y"] + cy, kind=s["kind"])
                       for s in refine_marks()["sites"].values()])
    put_marks(root, shifted, meta=meta_a_style(angle=angle))
    f = write_sxm(tmp_path / "r.sxm", frame_array(), angle_deg=angle)
    res = run(frame_path=str(f))
    assert res.success and res.data["refined"] is True, res.data["refine_note"]
    s = by_n(res)
    assert [s[i]["rkind"] for i in range(1, 8)] == ["mol", "hbond", "ring", "au", "fixed", "fixed", "fixed"]
    assert (s[1]["rx_nm"], s[1]["ry_nm"]) == pytest.approx(LONER, abs=0.05)
    for v in s.values():
        assert (v["page_rx_nm"], v["page_ry_nm"]) == pytest.approx((v["rx_nm"] + cx, v["ry_nm"] + cy), abs=2e-4)
        want = hand(v["rx_nm"], v["ry_nm"], cx, cy, angle)
        assert (v["scanner_x_m"], v["scanner_y_m"]) == pytest.approx(want, abs=2e-13)


def test_snapping_is_orientation_correct_for_an_up_scan(root, tmp_path):
    """``:SCAN_DIR: up`` 的文件第一行是帧底;若按「行 0 = 上沿」直接读,y 会整个翻转。"""
    put_marks(root, refine_marks(), meta=META)
    f = write_sxm(tmp_path / "up.sxm", frame_array(), scan_dir="up")
    s = by_n(run(frame_path=str(f)))
    assert s[1]["rkind"] == "mol" and s[2]["rkind"] == "hbond" and s[3]["rkind"] == "ring"
    assert (s[1]["rx_nm"], s[1]["ry_nm"]) == pytest.approx(LONER, abs=0.05)


def test_anchor_molecules_are_returned_for_the_drift_tracker(root, frame):
    put_marks(root, refine_marks(), meta=META)
    d = run(frame_path=str(frame)).data
    used = {i for s in d["sites"] for i in s["anchor"]}
    assert used and set(d["anchor_molecules"]) == {str(i) for i in used}
    geo = smr._geometry_from_meta(META)
    for m in d["anchor_molecules"].values():
        want = smr.frame_nm_to_scanner_m(m["x_nm"], m["y_nm"], geo)
        assert (m["scanner_x_m"], m["scanner_y_m"]) == pytest.approx(want, abs=2e-13)
    # anchor_offset = 精确化位置 − 锚点分子均值
    s3 = by_n(run(frame_path=str(frame)))[3]
    ms = np.array([[d["anchor_molecules"][str(i)]["x_nm"], d["anchor_molecules"][str(i)]["y_nm"]]
                   for i in s3["anchor"]]).mean(0)
    assert s3["anchor_offset_nm"] == pytest.approx([s3["rx_nm"] - ms[0], s3["ry_nm"] - ms[1]], abs=5e-4)


def test_geometry_can_come_from_the_header_alone_while_refining(root, frame):
    put_marks(root, refine_marks())                    # 页里没有 META
    res = run(frame_path=str(frame))
    assert res.success and res.data["geometry_source"] == "frame_header" and res.data["refined"] is True
    assert by_n(res)[1]["rkind"] == "mol"


def test_refine_false_uses_the_marks_exactly_and_ignores_stored_refinement(root, frame):
    stored = site(1, 2.0, 1.0, rx=-1.5, ry=1.0, rkind="ring", anchor=[0, 1])
    put_marks(root, doc_of(stored), meta=META)
    res = run(refine=False, frame_path=str(frame))
    s = res.data["sites"][0]
    assert res.data["refined"] is False and s["position_source"] == "marked" and "rx_nm" not in s
    assert s["scanner_x_m"] == pytest.approx((-75.0 + 2.0) * 1e-9, rel=1e-12)


def test_no_frame_path_is_a_note_not_a_failure(root):
    put_marks(root, refine_marks(), meta=META)
    res = run()                                        # refine 默认 True,但没给帧
    assert res.success and res.data["refined"] is False and res.data["refine_note"]
    assert all(s["position_source"] == "marked" for s in res.data["sites"])
    assert by_n(res)[1]["scanner_x_m"] == pytest.approx((-75.0 + LONER[0] + 0.05) * 1e-9, rel=1e-12)


def test_an_unreadable_frame_degrades_refinement_only(root, tmp_path):
    put_marks(root, refine_marks(), meta=META)
    res = run(frame_path=str(tmp_path / "missing.sxm"))
    assert res.success and res.data["refined"] is False
    assert res.data["sites"][0]["scanner_x_m"] is not None
    bad = tmp_path / "bad.sxm"
    bad.write_bytes(b"not an sxm at all")
    res = run(frame_path=str(bad))
    assert res.success and res.data["refined"] is False


def test_a_frame_without_molecules_is_not_refined(root, tmp_path):
    put_marks(root, refine_marks(), meta=META)
    f = write_sxm(tmp_path / "flat.sxm", frame_array(mols=[], tilt=False))     # 全平:没有可吸附的东西
    res = run(frame_path=str(f))
    assert res.success and res.data["refined"] is False and res.data["refine_note"]
    assert all(s["position_source"] == "marked" for s in res.data["sites"])


def test_a_partly_scanned_frame_still_works(root, tmp_path):
    """没扫完的帧下半是 NaN:不该让调平或检测崩掉,已扫的部分照常吸附。"""
    arr = frame_array()
    arr[NPX // 2 + 20:, :] = np.nan                    # 下面 ~30% 没扫到(链在 y=-1.5,落在没扫的区域)
    put_marks(root, doc_of(site(1, LONER[0] + 0.03, LONER[1]), site(2, _RING_C[0], _RING_C[1] + 0.03)), meta=META)
    res = run(frame_path=str(write_sxm(tmp_path / "half.sxm", arr)))
    assert res.success and res.data["refined"] is True
    s = by_n(res)
    assert s[1]["rkind"] == "mol" and s[2]["rkind"] == "ring"


# ── 纯函数 ────────────────────────────────────────────────────────────────

def test_detect_molecules_finds_every_blob_once_at_subpixel_accuracy():
    """链在 y=-1.5 —— 正好落在两行像素的正中,两行的平滑值逐位相等(并列极大值)。
    蓝本对并列全放行,每个链上的分子都会被报两次;重复点之间距离为 0,会被当成氢键邻居。"""
    M = smr.detect_molecules(smr._level(frame_array()), nm_per_px=W_NM / NPX, w_nm=W_NM, h_nm=W_NM)
    assert len(M) == len(MOLECULES)
    for t in MOLECULES:
        assert np.min(np.hypot(M[:, 0] - t[0], M[:, 1] - t[1])) < 0.03


def test_snap_site_survives_what_the_reference_script_crashed_on():
    """蓝本 ``refine``:明确要 hbond/ring 而没有对/环 → ``best_pair`` 为 None 时 TypeError。"""
    M = np.array([[0.0, 0.0], [3.0, 0.0], [0.0, 3.0]])             # 三个分子,互相 3 nm,没有氢键对
    pairs = smr._neighbour_pairs(M, 0.85)
    assert len(pairs) == 0
    for kind in ("hbond", "ring", "mol", "edge", "fixed", "au", "auto", "nonsense", ""):
        r = smr.snap_site(0.1, 0.1, kind, M, pairs, [])
        assert r["rkind"] in {"mol", "fixed", "edge", "au"} and math.isfinite(r["rx"])
    assert smr.snap_site(0.1, 0.1, "hbond", M, pairs, [])["rkind"] == "fixed"
    assert smr.snap_site(0.1, 0.1, "nonsense", M, pairs, [])["rkind"] == "fixed"
    assert smr.snap_site(0.1, 0.1, "edge", M, pairs, [])["rkind"] == "edge"
    assert smr.snap_site(0.1, 0.1, "auto", M, pairs, [])["rkind"] == "mol"
    assert smr.snap_site(1.5, 1.5, "auto", M, pairs, [])["rkind"] == "au"


def test_a_far_explicit_snap_is_capped_instead_of_dragging_the_mark():
    M = np.array([[0.0, 0.0], [0.7, 0.0]])
    pairs = smr._neighbour_pairs(M, 0.85)
    near = smr.snap_site(0.2, 0.1, "mol", M, pairs, [])
    far = smr.snap_site(5.0, 0.0, "mol", M, pairs, [])
    assert near["rkind"] == "mol" and near["shift_nm"] < 0.25
    assert far["rkind"] == "fixed" and far["shift_nm"] == 0.0


def test_ring_search_is_limited_to_the_neighbourhood_of_the_probes():
    ring = np.array([[0.7 * math.cos(math.radians(a)), 0.7 * math.sin(math.radians(a))]
                     for a in range(0, 360, 60)])
    far = ring + np.array([50.0, 0.0])
    M = np.vstack([ring, far])
    pairs = smr._neighbour_pairs(M, 0.85)
    both = smr._find_rings(M, pairs, np.array([[0.0, 0.0], [50.0, 0.0]]), smr.DEFAULT_PROFILE)
    near_only = smr._find_rings(M, pairs, np.array([[0.0, 0.0]]), smr.DEFAULT_PROFILE)
    assert len(both) == 2 and len(near_only) == 1
    assert near_only[0][1] == pytest.approx([0.0, 0.0], abs=1e-9)


# ── 技能接口 ──────────────────────────────────────────────────────────────

def test_metadata_is_a_pure_read_skill_that_takes_no_instrument_token():
    meta = ReadSiteMarks().metadata()
    assert meta.name == "ReadSiteMarks"
    assert meta.category.value == "read" and meta.safety_level.value == "auto"
    assert [p.name for p in meta.parameters] == ["page_id", "refine", "frame_path"]
    assert [p.required for p in meta.parameters] == [True, False, False]
    assert needs_token(meta, meta.name) is False       # 不排队等仪器,长任务进行中也能读
    assert ReadSiteMarks().validate_params({}) != []
    assert ReadSiteMarks().validate_params({"page_id": "abc", "refine": False, "frame_path": ""}) == []
    assert ReadSiteMarks().validate_params({"page_id": "abc", "frame_path": None}) == []


def test_it_never_touches_the_context(root):
    class Boom:
        def __getattr__(self, name):
            raise AssertionError(f"ReadSiteMarks touched context.{name}")

    put_marks(root, doc_of(site(1, 1.0, 1.0)), meta=META)
    assert ReadSiteMarks().execute(Boom(), {"page_id": PAGE, "refine": False}).success


def test_the_result_is_plain_json(root, frame):
    """numpy 标量进了 SkillResult.data 会在下游(checkpointer / MCP)序列化时炸。"""
    put_marks(root, refine_marks(), meta={**META, "angle": 30.0})
    res = run(frame_path=str(frame))
    assert res.success
    json.dumps(res.data, allow_nan=False)
    assert res.summary


def test_the_same_keys_are_present_on_success_and_on_failure(root):
    put_marks(root, doc_of(site(1, 1, 1)), meta=META)
    good = run(refine=False).data
    bad = run(page_id="never-saved").data
    assert set(good) == set(bad)


def test_the_page_directory_is_pinned_and_a_b_c_agree_on_it(root):
    """A 建页、B 服务并写 marks.json、C 读 —— 三个落点必须是同一个目录:
    ``project_root()/"artifacts"/"site_marking"/<page_id>``(没有 ``MASTv2`` 这一段;全仓 ~20 处产物
    目录是这个约定,wishlist 才是异类)。

    分歧的后果是**静默**的:B 把站位写进一个目录,C 在另一个目录读到一份空文档 —— 0 个站位,
    不报错,看上去就像用户什么也没标。曾出现过 A 与 B 各写了一条路径的情形。
    """
    from mast.api.routes.site_marking import page_dir as b_page_dir

    want = root / "artifacts" / "site_marking" / PAGE
    assert smr.site_marking_dir(PAGE) == want and smr.marks_path(PAGE) == want / "marks.json"
    assert page_dir_of() == want                       # A
    assert b_page_dir(PAGE, create=True) == want       # B
    with pytest.raises(ValueError):
        smr.site_marking_dir("../x")


def test_the_whole_chain_prepare_page_then_patches_then_read(root, tmp_path):
    """A 建页 → 页面经 B 的补丁写进站位(页面坐标 = 绝对 nm)→ C 读。三件各自的测试只管自己,
    这一条管它们**之间**的接口:目录、文档形状、坐标约定。θ=0:扫描器坐标 = 页面坐标 × 1e-9。"""
    from mast.api.routes.site_marking import apply_patch
    from mast.skills.builtins.site_marking import PrepareSiteMarkingPage

    f = write_sxm(tmp_path / "ref.sxm", frame_array(), offset_nm=(10.0, -20.0), angle_deg=0.0)
    made = PrepareSiteMarkingPage().execute(
        None, {"frame_path": str(f), "page_id": PAGE, "auto_detect_molecules": False})
    assert made.success, made.error
    apply_patch(PAGE, {"target": "sites", "id": "b", "op": "set", "data": site(2, 8.0, -18.5)})
    apply_patch(PAGE, {"target": "sites", "id": "a", "op": "set", "data": site(1, 13.0, -16.0)})
    apply_patch(PAGE, {"target": "doc", "path": "plan/marking", "op": "set", "data": {"done": True, "count": 2}})

    for frame_path in ("", str(f)):
        res = run(frame_path=frame_path, refine=False)
        assert res.success and res.data["coords_trustworthy"] is True, res.data["warnings"]
        assert res.data["marking_done"] is True and [s["n"] for s in res.data["sites"]] == [1, 2]
        for s, (px, py) in zip(res.data["sites"], [(13.0, -16.0), (8.0, -18.5)]):
            assert (s["scanner_x_m"], s["scanner_y_m"]) == pytest.approx((px * 1e-9, py * 1e-9), rel=1e-12)
            assert s["in_frame"] is True
