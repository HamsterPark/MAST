"""``GET /data/search``：按元数据检索数据记录（图库索引 + 索引之外的现场读取）。

每条测试只碰 tmp：图库状态目录由 ``tests/v2/conftest.py::_gallery_state_is_isolated``
（autouse）指到 tmp，实验根由 ``world`` 用的 ``documents_root`` 指到 tmp，数据搜索目录是
``MAST2_PROJECT_ROOT``（tmp）下的 ``working-sessions``。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from _ext_world import H, write_sxm

EXP_ID = "aca3d174-3287-4b0d-a3e8-745f6bc86892"
EXP_V2 = "01M4ZBW4ABCDEFGHJKMNPQRSTV"
SAMPLE_ID = "b7a910a6-e6ed-4af5-89a9-5cebcd0b16a8"
EXP_DIR = "2026-10-15__MAST7_trial_Mimea__aca3d174"
SAMPLE_DIR = "S01__Mimea_simulated_surface__b7a910"


# ─────────────────────────────────────────────────────────────────────
# 造数据
# ─────────────────────────────────────────────────────────────────────

def _sessions(world) -> Path:
    d = world.tmp / "proj" / "working-sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _exp_root(world) -> Path:
    return world.tmp / "experiments"


def _make_experiment(world) -> Path:
    """实验根下的一个实验文件夹（两份清单 + 一个样品目录），返回样品的 raw/sxm 目录。"""
    e = _exp_root(world) / EXP_DIR
    s = e / "samples" / SAMPLE_DIR
    (s / "raw" / "sxm").mkdir(parents=True, exist_ok=True)
    (e / "experiment.json").write_text(json.dumps({
        "id": EXP_ID, "title": "MAST7_ext_agent_trial_Mimea_sim", "dir_name": EXP_DIR,
        "provenance": {"v1_row_id": EXP_ID, "v2_experiment_id": EXP_V2}}), encoding="utf-8")
    (s / "sample.json").write_text(json.dumps({
        "id": SAMPLE_ID, "name": "Mimea_simulated_surface", "dir_name": SAMPLE_DIR}),
        encoding="utf-8")
    return s / "raw" / "sxm"


def _dat(path: Path, *, start: str = "15.10.2026 17:03:23", v0=-1.5, v1=1.5, n=16,
         module_params: bool = True) -> Path:
    """最小但真实的 Nanonis ``.dat``（偏压谱）。``module_params=False``：像 Mimea 存的那样，
    头里没有偏压与设定点。"""
    v = np.linspace(v0, v1, n)
    rows = "\n".join(f"{a:.6E}\t{a * 2e-11:.6E}" for a in v)
    params = "Bias>Bias (V)\t2E+0\nZ-Controller>Setpoint\t50E-12\n" if module_params else ""
    path.write_text(
        "Experiment\tbias spectroscopy\n"
        f"Start time\t{start}\n"
        "X (m)\t-3.984375E-10\nY (m)\t-6.015625E-10\nZ offset (m)\t0E+0\n"
        f"{params}\n"
        "[DATA]\nBias calc (V)\tCurrent (A)\n" + rows + "\n", encoding="utf-8")
    return path


def _epoch(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


def _write_index(items: list[dict], *, channels: dict | None = None,
                 marks: dict | None = None) -> Path:
    """图库状态目录（tmp）里写一份索引 —— 与构建写的同一种格式。"""
    from mast.gallery import paths as gpaths
    from mast.gallery.index import make_doc, write_index

    lay = gpaths.layout()
    write_index(make_doc(items, [{"name": "data", "path": "x", "enabled": True}],
                         "2026-10-15T17:00:00"), lay)
    if channels is not None:
        gpaths.atomic_write_json(lay.cache_file("inventory.json"),
                                 {k: {"channels": v} for k, v in channels.items()})
    if marks is not None:
        gpaths.atomic_write_json(lay.marks, {"items": marks})
    return lay.state


def _frame_item(iid: str, path: Path, *, t: str, w=100.0, b=2.0, sp=50.0, nx=256, cpl=None) -> dict:
    it = {"id": iid, "k": "f", "d": iid.rsplit("/", 1)[0], "fn": Path(path).name, "p": str(path),
          "pf": "", "t": _epoch(t), "mt": _epoch(t) + 600, "ad": "b1", "th": "/x.jpg",
          "w": w, "hn": w, "b": b, "sp": sp, "nx": nx, "ny": nx, "ang": 0.0, "cx": 0.0, "cy": 0.0,
          "sd": "d", "acq": 512.0, "rows": nx, "rall": nx, "r0": 0, "r1": nx,
          "at": 0.0, "ar": "", "hf": 0.0, "hl": "", "li": ""}
    if cpl:
        it["cp"], it["cpl"] = len(cpl), [str(p) for p in cpl]
    return it


def _search(world, **params):
    r = world.client.get("/data/search", params=params, headers=H())
    assert r.status_code == 200, r.text
    return r.json()


def _names(body) -> list[str]:
    return [i["name"] for i in body["items"]]


# ─────────────────────────────────────────────────────────────────────
# 1. 索引里的元数据与操作员标记
# ─────────────────────────────────────────────────────────────────────

def test_indexed_frames_are_found_by_size_bias_setpoint_and_rating(world):
    d = _sessions(world)
    big = d / "big_100nm.sxm"
    small = d / "small_6nm.sxm"
    neg = d / "neg_bias.sxm"
    for p in (big, small, neg):
        p.write_bytes(b"x")                     # 只走索引：文件在不在不影响检索
    _write_index(
        [_frame_item("data/big_100nm.sxm", big, t="2026-10-15 16:48:09"),
         _frame_item("data/small_6nm.sxm", small, t="2026-10-15 16:58:34", w=6.0),
         _frame_item("data/neg_bias.sxm", neg, t="2026-10-14 10:00:00", b=-2.5, sp=100.0)],
        channels={"data/small_6nm.sxm": ["Z", "Current"]},
        marks={"data/small_6nm.sxm": {"r": 2, "tags": ["原子分辨"], "note": "球顶 STS 之前"}})

    body = _search(world, ext="sxm", size_max_nm=10, live="false")
    assert _names(body) == ["small_6nm.sxm"]
    rec = body["items"][0]
    assert rec["width_nm"] == 6.0 and rec["pixels"] == [256, 256] and rec["bias_v"] == 2.0
    assert rec["setpoint_pa"] == 50.0 and rec["channels"] == ["Z", "Current"]
    assert rec["rating"] == 2 and rec["tags"] == ["原子分辨"] and rec["note"] == "球顶 STS 之前"
    assert rec["source"] == "index" and rec["time"] == "2026-10-15T16:58:34"
    assert rec["experiment"] is None and rec["sample"] is None and rec["frame"] is None

    assert _names(_search(world, bias_max_v=0, live="false")) == ["neg_bias.sxm"]
    assert _names(_search(world, setpoint_min_pa=60, live="false")) == ["neg_bias.sxm"]
    assert _names(_search(world, min_rating=1, live="false")) == ["small_6nm.sxm"]
    assert _names(_search(world, tag="原子分辨", live="false")) == ["small_6nm.sxm"]
    assert _names(_search(world, text="球顶", live="false")) == ["small_6nm.sxm"]
    # 默认新的在前；分页
    page = _search(world, live="false", n=2)
    assert _names(page) == ["small_6nm.sxm", "big_100nm.sxm"] and page["has_more"] is True
    assert page["total"] == 3 and _names(_search(world, live="false", n=2, offset=2)) == ["neg_bias.sxm"]
    assert _names(_search(world, live="false", order="oldest"))[0] == "neg_bias.sxm"
    assert page["index"]["available"] is True and page["index"]["items"] == 3


def test_a_date_alone_bounds_a_whole_day(world):
    d = _sessions(world)
    a, b = d / "a.sxm", d / "b.sxm"
    for p in (a, b):
        p.write_bytes(b"x")
    _write_index([_frame_item("data/a.sxm", a, t="2026-10-15 23:59:00"),
                  _frame_item("data/b.sxm", b, t="2026-10-16 00:00:30")])
    assert _names(_search(world, since="2026-10-15", until="2026-10-15", live="false")) == ["a.sxm"]
    assert _names(_search(world, since="2026-10-16 00:00", live="false")) == ["b.sxm"]


@pytest.mark.parametrize("params, word", [({"since": "yesterday"}, "时间"),
                                          ({"ext": "png"}, "类型")])
def test_a_query_it_cannot_read_is_a_422_not_an_empty_answer(world, params, word):
    r = world.client.get("/data/search", params=params, headers=H())
    assert r.status_code == 422 and r.json()["error"] == "bad_query" and word in r.json()["detail"]


def test_corrupt_marks_are_degraded_not_silently_empty(world):
    from mast.gallery import paths as gpaths

    d = _sessions(world)
    (d / "a.sxm").write_bytes(b"x")
    _write_index([_frame_item("data/a.sxm", d / "a.sxm", t="2026-10-15 12:00:00")])
    gpaths.layout().marks.write_text("[1, 2", encoding="utf-8")
    body = _search(world, live="false")
    assert body["degraded"] is True and any("标记" in n for n in body["notes"])
    assert _names(body) == ["a.sxm"]


# ─────────────────────────────────────────────────────────────────────
# 2. 索引之外：现场读头
# ─────────────────────────────────────────────────────────────────────

def test_files_the_index_does_not_have_are_read_live(world):
    d = _sessions(world)
    indexed = d / "indexed.sxm"
    indexed.write_bytes(b"x")
    _write_index([_frame_item("data/indexed.sxm", indexed, t="2026-10-15 12:00:00")])
    write_sxm(d / "fresh_0003.sxm", np.zeros((8, 8)), np.ones((8, 8)), range_m=6e-9)
    _dat(d / "Bias-Spectroscopy00018.dat")

    body = _search(world)
    by = {i["name"]: i for i in body["items"]}
    assert by["indexed.sxm"]["source"] == "index"
    fresh = by["fresh_0003.sxm"]
    assert fresh["source"] == "live" and fresh["width_nm"] == pytest.approx(6.0)
    assert fresh["pixels"] == [8, 8] and fresh["bias_v"] == pytest.approx(-1.2)
    assert fresh["setpoint_pa"] == pytest.approx(50.0) and fresh["channels"] == ["Z"]
    assert fresh["time"] == "2026-08-14T12:00:00" and fresh["fetchable"] is True
    spec = by["Bias-Spectroscopy00018.dat"]
    assert spec["kind"] == "spectrum" and spec["sweep_v"] == [-1.5, 1.5] and spec["points"] == 16
    assert spec["position_nm"] == [pytest.approx(-0.3984), pytest.approx(-0.6016)]
    assert body["live"]["added"] == 2 and body["live"]["truncated"] is False
    assert _names(_search(world, ext="dat")) == ["Bias-Spectroscopy00018.dat"]
    assert "fresh_0003.sxm" not in _names(_search(world, live="false"))


def test_a_spectrum_without_module_parameters_reports_no_setpoint_rather_than_zero(world):
    """图库的 .dat 摘要把缺的设定点写成 0.0；反馈的设定点不会是 0，所以那是「文件没说」。"""
    d = _sessions(world)
    _dat(d / "with.dat", start="15.10.2026 17:00:00")
    _dat(d / "without.dat", start="15.10.2026 17:03:23", module_params=False)
    by = {i["name"]: i for i in _search(world, ext="dat")["items"]}
    assert by["with.dat"]["setpoint_pa"] == pytest.approx(50.0) and by["with.dat"]["bias_v"] == 2.0
    assert "setpoint_pa" not in by["without.dat"] and "bias_v" not in by["without.dat"]
    assert _names(_search(world, ext="dat", setpoint_max_pa=100)) == ["with.dat"]


def test_without_an_index_the_search_still_answers_and_writes_nothing(world):
    from mast.gallery import paths as gpaths

    write_sxm(_sessions(world) / "only.sxm", np.zeros((4, 4)), np.ones((4, 4)))
    state = gpaths.layout().state
    body = _search(world)
    assert body["index"]["available"] is False and "图库" in body["index"]["detail"]
    assert _names(body) == ["only.sxm"] and body["items"][0]["source"] == "live"
    assert not state.exists(), "检索是只读的：不许建图库的状态目录"


def test_an_unreadable_file_is_listed_with_why(world):
    (_sessions(world) / "broken.sxm").write_bytes(b"no header here")
    rec = _search(world)["items"][0]
    assert rec["name"] == "broken.sxm" and "unreadable" in rec and rec["source"] == "live"


# ─────────────────────────────────────────────────────────────────────
# 3. 归属：实验与样品
# ─────────────────────────────────────────────────────────────────────

def test_experiment_and_sample_come_from_the_folder_and_from_a_folded_copy(world):
    raw = _make_experiment(world)
    world.ctx.config = SimpleNamespace(experiments_dir=str(_exp_root(world)))
    d = _sessions(world)
    origin = write_sxm(d / "trial_0002.sxm", np.zeros((4, 4)), np.ones((4, 4)))
    copy = raw / "trial_0002.sxm"
    copy.write_bytes(origin.read_bytes())
    os.utime(copy, ns=(origin.stat().st_atime_ns, origin.stat().st_mtime_ns))
    write_sxm(d / "unrelated.sxm", np.zeros((4, 4)), np.ones((4, 4)))

    body = _search(world)
    by = {i["name"]: i for i in body["items"]}
    rec = by["trial_0002.sxm"]
    assert rec["experiment"] == {"id": EXP_ID, "name": "MAST7_ext_agent_trial_Mimea_sim",
                                 "dir": EXP_DIR}
    assert rec["sample"]["id"] == SAMPLE_ID and rec["sample"]["name"] == "Mimea_simulated_surface"
    assert by["unrelated.sxm"]["experiment"] is None
    for q in (EXP_ID, EXP_V2, "aca3d174", "mast7_ext", "Mimea_sim"):
        assert _names(_search(world, experiment=q)) == ["trial_0002.sxm"], q
    assert _names(_search(world, sample="mimea_simulated")) == ["trial_0002.sxm"]
    assert _names(_search(world, sample=SAMPLE_ID[:6])) == ["trial_0002.sxm"]
    assert _search(world, experiment="nosuch")["total"] == 0


def test_an_indexed_original_is_attributed_through_its_copies(world):
    raw = _make_experiment(world)
    d = _sessions(world)
    origin, copy = d / "t.sxm", raw / "t.sxm"
    origin.write_bytes(b"x")
    copy.write_bytes(b"x")
    _write_index([_frame_item("data/t.sxm", origin, t="2026-10-15 16:58:34", cpl=[origin, copy])])
    rec = _search(world, live="false")["items"][0]
    assert rec["experiment"]["id"] == EXP_ID and rec["sample"]["id"] == SAMPLE_ID
    assert rec["copies"] == [str(copy)]


# ─────────────────────────────────────────────────────────────────────
# 4. 路径约束不变
# ─────────────────────────────────────────────────────────────────────

def test_a_file_outside_the_data_roots_is_listed_but_not_fetchable(world, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("operator_raw_root") / "far.sxm"
    elsewhere.write_bytes(b"x")
    inside = _sessions(world) / "near.sxm"
    inside.write_bytes(b"x")
    outside_only = tmp_path_factory.mktemp("other") / "only_far.sxm"
    outside_only.write_bytes(b"x")
    _write_index([_frame_item("raw/far.sxm", elsewhere, t="2026-10-15 10:00:00",
                              cpl=[elsewhere, inside]),
                  _frame_item("raw/only_far.sxm", outside_only, t="2026-10-15 09:00:00")])
    by = {i["name"]: i for i in _search(world, live="false")["items"]}
    assert by["far.sxm"]["fetchable"] is True and by["far.sxm"]["path"] == str(inside), (
        "有一份副本在允许的根里时，给的就是那一份")
    assert by["only_far.sxm"]["fetchable"] is False
    r = world.client.get("/data/file", params={"path": by["only_far.sxm"]["path"]}, headers=H())
    assert r.status_code == 403, "检索没有放宽数据出口"


# ─────────────────────────────────────────────────────────────────────
# 5. VISTA 帧号
# ─────────────────────────────────────────────────────────────────────

class _Entry:
    def __init__(self, fid, src):
        self.fid, self.extra = fid, {"source_path": str(src)}


class _Svc:
    def __init__(self, archive_root: Path, entries: list):
        archive = SimpleNamespace(root=archive_root, frames=lambda: list(entries))
        self._ws = SimpleNamespace(id="exp-test", archive=archive)

    def workspace(self):
        return self._ws


def test_frames_of_the_active_experiment_carry_their_frame_id(world, monkeypatch):
    world.ctx.config = SimpleNamespace(experiments_dir=str(_exp_root(world)))
    d = _sessions(world)
    scan = write_sxm(d / "trial_0002.sxm", np.zeros((4, 4)), np.ones((4, 4)))
    spec = _dat(d / "Bias-Spectroscopy00018.dat")
    archive = _exp_root(world) / "vista" / "exp-test" / "archive"
    archive.mkdir(parents=True)
    arch_copy = archive / "t0005.d0.dat"
    arch_copy.write_bytes(spec.read_bytes())
    monkeypatch.setattr(world.app.state, "vista",
                        _Svc(archive, [_Entry("t0003.s0", scan), _Entry("t0005.d0", spec)]))

    body = _search(world)
    by = {i["name"]: i for i in body["items"]}
    assert by["trial_0002.sxm"]["frame"] == "t0003.s0"
    assert by["Bias-Spectroscopy00018.dat"]["frame"] == "t0005.d0"
    assert "t0005.d0.dat" not in by, "档案目录里的拷贝并进它的来源，同一帧不列两次"
    assert str(arch_copy) in by["Bias-Spectroscopy00018.dat"]["copies"]
    assert body["frames"]["workspace"] == "exp-test"

    files = world.client.get("/data/files", params={"n": 20}, headers=H()).json()["files"]
    frames = {Path(f["path"]).name: f.get("frame") for f in files}
    assert frames["trial_0002.sxm"] == "t0003.s0" and frames["t0005.d0.dat"] == "t0005.d0"


# ─────────────────────────────────────────────────────────────────────
# 6. 纯函数
# ─────────────────────────────────────────────────────────────────────

def test_time_parsing():
    from mast.api.ext.data_search import parse_time

    day = parse_time("2026-10-15")
    assert parse_time("2026-10-15", end=True) - day == pytest.approx(86400.0)
    assert parse_time("2026-10-15 16:58:34") == pytest.approx(_epoch("2026-10-15 16:58:34"))
    assert parse_time("2026-10-15T08:58:34+00:00") == pytest.approx(
        datetime.fromisoformat("2026-10-15T08:58:34+00:00").timestamp())
    assert parse_time("1792054620") == 1792054620.0
    assert parse_time("") is None and parse_time(None) is None
    with pytest.raises(ValueError):
        parse_time("last tuesday")


def test_a_root_that_ends_in_a_separator_still_contains_its_files():
    """盘符根 ``C:\\`` 规范化后自带分隔符：拼 ``root + os.sep`` 会让它什么都不包含。"""
    from mast.api.ext.data_search import _norm, _under

    anchor = Path.cwd().anchor                       # e.g. 'D:\\'
    inside = _norm(Path(anchor) / "MAST-data" / "x.sxm")
    assert _under(inside, _norm(anchor))
    assert _under(inside, _norm(Path(anchor) / "MAST-data"))
    assert not _under(_norm(Path(anchor) / "MAST-datafoo" / "x.sxm"), _norm(Path(anchor) / "MAST-data"))


def test_identifiers_match_whole_prefix_or_name():
    from mast.api.ext.data_search import _ident_matches

    ids = [EXP_ID, EXP_V2]
    assert _ident_matches(EXP_ID.upper(), ids, "x")
    assert _ident_matches("aca3d1", ids, "x") and not _ident_matches("aca3d", ids, "x"), (
        "少于 6 个十六进制字符的前缀太宽，不当 id 用")
    assert _ident_matches("trial", ids, "MAST7 trial")
    assert not _ident_matches("other", ids, "MAST7 trial")
