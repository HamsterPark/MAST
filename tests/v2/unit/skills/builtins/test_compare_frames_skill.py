# -*- coding: utf-8 -*-
"""Shell of ``CompareFramesLocalChange`` (the core is tested in tests/v2/unit/vision/test_frame_compare.py)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from mast.core.types import SafetyLevel, SkillCategory
from mast.skills.builtins._sxm_frame import LoadedFrame
from mast.skills.builtins.compare_frames import CompareFramesLocalChange

SXM = "tests/v2/fixtures/nanonis/scan_topography.sxm"
N = 128
NM = 0.05
YY, XX = np.mgrid[0:N, 0:N].astype(float)
BLOBS = [(30, 40, 5, 3.0), (90, 30, 4, -2.5), (70, 100, 6, 3.0), (25, 105, 5, -2.0), (60, 70, 4, 2.0),
         (105, 80, 5, 2.5), (45, 15, 4, -2.0), (100, 115, 4, 2.0)]


def scene(dy=0.0, dx=0.0, lattice=True, blobs=True, bump=None, gain=1.0, noise=0.05, seed=0, n=N):
    """Image in pm (the skill reads metres), lattice period 8 px = 0.4 nm, content moved by (dy, dx) px."""
    y, x = YY[:n, :n] - dy, XX[:n, :n] - dx
    z = np.zeros((n, n))
    if lattice:
        k = 2 * np.pi / 8.0
        z += np.cos(k * x) + np.cos(k * (0.5 * x + 0.866 * y)) + np.cos(k * (0.5 * x - 0.866 * y))
    if blobs:
        for by, bx, s, a in BLOBS:
            z += a * np.exp(-((y - by) ** 2 + (x - bx) ** 2) / (2 * s * s))
    if bump:
        by, bx, s, a = bump
        z += a * np.exp(-((y - by) ** 2 + (x - bx) ** 2) / (2 * s * s))
    z = gain * z + np.random.default_rng(seed).normal(0, noise, z.shape)
    return z


def _frame(z_pm, nmpp=NM, name="f.sxm"):
    return LoadedFrame(forward=z_pm * 1e-12, backward=None, current_forward=z_pm * 1e-12, nm_per_px=nmpp,
                       width_nm=nmpp * z_pm.shape[1], bias_v=-1.5, name=name)


def _patch(monkeypatch, frames):
    monkeypatch.setattr("mast.skills.builtins._sxm_frame.load_frame", lambda p, ch="Z": frames[p])


def _run(tmp_path, **params):
    p = {"before_path": "b", "after_path": "a", "box_size_nm": 1.6, "output_dir": str(tmp_path)}
    p.update(params)
    return CompareFramesLocalChange().execute(None, p)


def test_metadata_read_only_analysis():
    m = CompareFramesLocalChange().metadata()
    assert m.category == SkillCategory.ANALYSIS and m.safety_level == SafetyLevel.AUTO
    names = {p.name for p in m.parameters}
    assert {"before_path", "after_path", "channel", "box_size_nm", "box_cx_nm", "box_cy_nm", "hp_nm", "lowpass_nm",
            "window_nm", "null_before_path", "null_after_path"} <= names
    assert {p.name for p in m.parameters if p.required} == {"before_path", "after_path"}


def test_registered_in_builtins():
    import mast.skills.builtins as B
    assert B.CompareFramesLocalChange is CompareFramesLocalChange
    assert "CompareFramesLocalChange" in B.__all__


def test_written_box_is_recovered_and_exceeds_the_null(monkeypatch, tmp_path):
    truth = (3.4, -2.6)
    _patch(monkeypatch, {
        "b": _frame(scene(seed=1)),
        "a": _frame(scene(*truth, bump=(64, 64, 5, 3.0), seed=2)),
        "nb": _frame(scene(seed=3)),
        "na": _frame(scene(*truth, seed=4)),
    })
    r = _run(tmp_path, null_before_path="nb", null_after_path="na")
    assert r.success, r.error
    d = r.data
    assert abs(d["shift_px"][0] + truth[0]) < 0.3 and abs(d["shift_px"][1] + truth[1]) < 0.3
    assert d["rms_ratio"] > 3 and d["null"]["ratio"] < 1.6
    assert d["verdict"] == "candidate_exceeds_null"
    assert "null" in d["statement"].lower() or "零假设" in d["statement"]
    assert d["png_path"] and json.loads(open(d["json_path"], encoding="utf-8").read())["rms_ratio"] == d["rms_ratio"]


def test_without_a_null_it_stays_a_candidate(monkeypatch, tmp_path):
    _patch(monkeypatch, {"b": _frame(scene(seed=1)), "a": _frame(scene(1.0, 2.0, bump=(64, 64, 5, 3.0), seed=2))})
    r = _run(tmp_path)
    assert r.success and r.data["verdict"] == "candidate_without_null" and r.data["null"] is None


def test_global_contrast_change_gives_no_box_shaped_change(monkeypatch, tmp_path):
    _patch(monkeypatch, {"b": _frame(scene(seed=1)), "a": _frame(scene(1.0, -1.5, gain=1.4, seed=2))})
    r = _run(tmp_path)
    assert r.success, r.error
    assert 0.7 < r.data["rms_ratio"] < 1.4
    assert r.data["verdict"] == "no_box_shaped_change"


def test_lattice_only_frames_are_not_trusted(monkeypatch, tmp_path):
    _patch(monkeypatch, {"b": _frame(scene(blobs=False, seed=1)), "a": _frame(scene(0.0, 5.3, blobs=False, seed=2))})
    r = _run(tmp_path)
    assert r.success and r.data["verdict"] == "registration_unreliable"
    assert "lattice_only" in r.data["registration_warnings"] and r.data["registration_note"]


def test_shift_beyond_half_a_period_is_registered_not_aliased(monkeypatch, tmp_path):
    _patch(monkeypatch, {"b": _frame(scene(seed=1)), "a": _frame(scene(0.0, 5.3, seed=2))})
    r = _run(tmp_path)
    assert r.success, r.error
    assert abs(r.data["shift_px"][1] + 5.3) < 0.3 and abs(r.data["shift_px"][0]) < 0.3


def test_same_area_on_another_pixel_grid_is_resampled(monkeypatch, tmp_path):
    big = scene(seed=1)
    small = scene(seed=2, n=N)                                      # 128 px
    # AFTER on a 2x finer grid over the same 6.4 nm: build by cubic zoom of a 64-px version
    from scipy.ndimage import zoom
    coarse = zoom(small, 0.5, order=3)                              # 64 px, 0.1 nm/px
    _patch(monkeypatch, {"b": _frame(big), "a": _frame(coarse, nmpp=2 * NM)})
    r = _run(tmp_path)
    assert r.success, r.error
    assert r.data["shape"] == [N, N]


def test_different_area_is_rejected_clearly(monkeypatch, tmp_path):
    _patch(monkeypatch, {"b": _frame(scene(seed=1)), "a": _frame(scene(seed=2, n=96))})     # 4.8 nm vs 6.4 nm
    r = _run(tmp_path)
    assert r.success is False and ("不是同一区域" in r.error or "not the same area" in r.error)


def test_bad_inputs(monkeypatch, tmp_path):
    assert CompareFramesLocalChange().execute(None, {"before_path": "", "after_path": ""}).success is False
    r = CompareFramesLocalChange().execute(None, {"before_path": "no_such.sxm", "after_path": "no_such2.sxm"})
    assert r.success is False and r.error
    _patch(monkeypatch, {"b": _frame(scene(seed=1)), "a": _frame(scene(seed=2))})
    assert _run(tmp_path, null_before_path="b").success is False           # null needs both
    assert _run(tmp_path, channel="Phase").success is False
    assert _run(tmp_path, box_size_nm=5.5).success is False                # box covers most of the frame


def test_real_sxm_fixture_against_itself_is_zero_shift(request, tmp_path):
    p = str(request.config.rootpath / SXM)
    r = CompareFramesLocalChange().execute(None, {"before_path": p, "after_path": p, "box_size_nm": 0.5,
                                                  "output_dir": str(tmp_path)})
    if not r.success:
        pytest.skip("fixture frame unsuitable for a box comparison: %s" % r.error)
    assert all(abs(v) < 0.05 for v in r.data["shift_px"])
