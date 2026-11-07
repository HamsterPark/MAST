# -*- coding: utf-8 -*-
"""A 件 —— PrepareSiteMarkingPage：从一帧生成标点页。

用假帧 monkeypatch read_sxm、把 artifacts 根指到 tmp；渲染与填模板真跑（numpy/PIL 都在）。
不碰仪器。
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from mast.i18n import use_lang
from mast.skills.builtins import site_marking as sm
from mast.skills.builtins.site_marking import PrepareSiteMarkingPage


def _fake_frame():
    # 12 nm 帧、中心 (-76, 150) nm、64²、0.1 V、角度 0；scan_range/offset 是米制原始串。
    ny = nx = 64
    z = np.tile(np.linspace(0, 1e-10, nx), (ny, 1))  # 一点倾斜，拉平后非全零
    header = {
        "scan_range": "1.2E-8 1.2E-8",
        "scan_offset": "-7.6E-8 1.5E-7",
        "scan_angle": "0",
        "scan_pixels": [nx, ny],
        "bias": "0.1",
        "rec_date": "26.10.2026", "rec_time": "12:00:00",
        "scan_dir": "down",
    }
    return {"header": header, "channels": {"Z": {"forward": z}}}


@pytest.fixture
def _patched(monkeypatch, tmp_path):
    monkeypatch.setattr("mast.io.nanonis_files.read_sxm", lambda path: _fake_frame())
    # artifacts 根 → tmp（site_marking_dir 用 _runtime_paths.project_root）
    monkeypatch.setattr("mast._runtime_paths.project_root", lambda: tmp_path)
    return tmp_path


def test_page_dir_matches_repo_artifacts_convention(_patched):
    # 路径契约：project_root()/artifacts/site_marking/<id>，**不带 MASTv2 段**
    # （全仓主流约定；A/B/C 三方必须一致，否则 B 404、ReadSiteMarks 找不到 marks.json）。
    d = sm.site_marking_dir("abc")
    assert d.parts[-3:] == ("artifacts", "site_marking", "abc")
    assert "MASTv2" not in d.parts


def test_builds_page_and_empty_marks(_patched):
    res = PrepareSiteMarkingPage().execute(
        None, {"frame_path": "C:/x/Au0002.sxm", "page_id": "test1",
               "auto_detect_molecules": False, "title": "Demo 0001"})
    assert res.success, res.error
    base = sm.site_marking_dir("test1")
    page = (base / "page.html").read_text(encoding="utf-8")
    # 占位符全部填掉
    for ph in ("__IMG__", "__META__", "__MOLS__", "__PAGE_ID__", "__TITLE__", "__SCALEBAR__"):
        assert ph not in page, f"{ph} 没被替换"
    assert "Demo 0001" in page and "test1" in page
    # 空 marks 文档已建，形状对
    doc = json.loads((base / "marks.json").read_text(encoding="utf-8"))
    assert doc["sites"] == {} and "plan/marking" in doc
    # META 几何
    m = res.data["meta"]
    assert m["w"] == 12.0 and m["px"] == 64 and m["angle"] == 0.0
    assert m["cx"] == -76.0 and m["cy"] == 150.0
    assert m["x0"] == -82.0 and m["y1"] == 156.0
    assert m["bias_mv"] == 100.0
    assert res.data["page_url"] == "/api/site-marking/test1/page"


@pytest.mark.parametrize("bad", ["../evil", "a/b", "sm x", "sm.x", "c" * 65])
def test_rejects_bad_page_id(_patched, bad):
    res = PrepareSiteMarkingPage().execute(
        None, {"frame_path": "x.sxm", "page_id": bad})
    assert not res.success and "page_id" in (res.error or "")


def test_trailing_whitespace_in_page_id_is_stripped_not_rejected(_patched):
    # A 先 .strip() 再 fullmatch：尾随空白被去掉而非放过（strip + fullmatch 双保险）。
    res = PrepareSiteMarkingPage().execute(
        None, {"frame_path": "x.sxm", "page_id": "sm_x\n ", "auto_detect_molecules": False})
    assert res.success and res.data["page_id"] == "sm_x"


def test_requires_frame_path(_patched):
    res = PrepareSiteMarkingPage().execute(None, {"frame_path": ""})
    assert not res.success and "frame_path" in (res.error or "")


def test_auto_page_id_and_meta_defaults(_patched):
    res = PrepareSiteMarkingPage().execute(
        None, {"frame_path": "x.sxm", "auto_detect_molecules": False})
    assert res.success
    assert res.data["page_id"].startswith("sm_")
    # setpoint 不在头里 → 占位「—」，不假造
    assert res.data["meta"]["sp_pa"] == "—"


def test_does_not_clobber_existing_marks(_patched):
    base = sm.site_marking_dir("keep")
    base.mkdir(parents=True, exist_ok=True)
    (base / "marks.json").write_text(
        json.dumps({"sites": {"a": {"n": 1, "x": -76, "y": 150}}}), encoding="utf-8")
    res = PrepareSiteMarkingPage().execute(
        None, {"frame_path": "x.sxm", "page_id": "keep", "auto_detect_molecules": False})
    assert res.success
    doc = json.loads((base / "marks.json").read_text(encoding="utf-8"))
    assert "a" in doc["sites"], "重建页面不该抹掉操作员已标的点"


# ── i18n：模板正文的纯展示文案外置成 __T_*__，由 skill 按请求语言用 tr() 填 ──────────
# 英文是公开发布的默认；中文在 MAST_LANG=zh（conftest）下逐字节不变。这里用 use_lang 显式切。

def _page(page_id, lang):
    with use_lang(lang):
        res = PrepareSiteMarkingPage().execute(
            None, {"frame_path": "x.sxm", "page_id": page_id,
                   "auto_detect_molecules": False, "title": "Demo 0001"})
    assert res.success, res.error
    return (sm.site_marking_dir(page_id) / "page.html").read_text(encoding="utf-8")


def test_english_page_has_english_ui_and_no_placeholders(_patched):
    page = _page("en1", "en")
    # 任何占位符都必须填干净（skill 的新 _unfilled 闸门也会在漏填时让 execute 失败）
    assert "__T_" not in page, "有 __T_* 文案占位符没被替换"
    for ph in ("__IMG__", "__META__", "__MOLS__", "__PAGE_ID__", "__TITLE__",
               "__EYEBROW__", "__H1__", "__SCALEBAR__", "__API_BASE__"):
        assert ph not in page, f"{ph} 没被替换"
    # 正文 + JS 的英文文案都在
    for s in ("Measurement progress", "Site list", "Task card", "Site coordinates JSON",
              "Confirm task card, start measuring", "Spectra to measure", "Wide STS + IETS",
              "Molecule centre", "Bare Au reference", "Connected", "Sync lost, please refresh"):
        assert s in page, f"缺英文文案：{s!r}"
    assert "Demo 0001" in page
    # kind / status 的「值」（被 JS 比较）保持不变，只有 label 翻译了
    assert '["auto", "Auto"]' in page
    assert '["hbond", "H-bond site (between two molecules)"]' in page
    assert 'todo: ["To do", ""]' in page and 'done: ["Done", "ok"]' in page
    # 英文页里不该再有这些中文 UI 短语
    for zh in ("测量进度", "位点列表", "确认任务卡，开始测量", "自动判断", "宽谱 STS"):
        assert zh not in page, f"英文页残留中文 UI：{zh!r}"


def test_chinese_page_strings_unchanged(_patched):
    page = _page("zh1", "zh")
    assert "__T_" not in page
    # 中文 UI 文案逐字出现（与历史模板一致）
    for zh in ("测量进度", "位点列表", "任务卡", "确认任务卡，开始测量",
               "要测的谱", "宽谱 STS + IETS", "自动判断", "裸 Au 参考",
               "把鼠标移到图上可读坐标", "已连接 · 自动保存", "同步中断，请刷新"):
        assert zh in page, f"缺中文文案：{zh!r}"
    # 值保持中文 label
    assert '["auto", "自动判断"]' in page
    assert 'todo: ["待测", ""]' in page
    # 中文页里不该出现英文 label
    assert "Measurement progress" not in page and "Confirm task card" not in page


def test_kind_and_status_values_are_never_translated(_patched):
    # 决定程序行为的「值」(auto/mol/hbond/... 与 todo/running/...) 两种语言下都原样
    for lang in ("en", "zh"):
        page = _page(f"val_{lang}", lang)
        for v in ('"auto"', '"mol"', '"hbond"', '"edge"', '"au"', '"fixed"'):
            assert f"[{v}, " in page, f"{lang}: kind 值 {v} 不该被改"
        for v in ("todo:", "running:", "done:", "skipped:", "failed:"):
            assert v in page, f"{lang}: status 键 {v} 不该被改"
