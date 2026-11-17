# -*- coding: utf-8 -*-
"""Shell of ``AverageFramesDriftCorrected`` (core is tested in tests/v2/unit/vision/test_frame_average.py)."""
from __future__ import annotations

import numpy as np
import pytest

from mast.core.types import SafetyLevel, SkillCategory
from mast.skills.builtins._sxm_frame import LoadedFrame
from mast.skills.builtins.average_frames import AverageFramesDriftCorrected

SXM = "tests/v2/fixtures/nanonis/scan_topography.sxm"
N = 96
YY, XX = np.mgrid[0:N, 0:N].astype(float)


def _lat(dy=0.0, dx=0.0, n=N):
    y, x = YY[:n, :n] + dy, XX[:n, :n] + dx
    k = 2 * np.pi / 8.0
    z = np.cos(k * x) + np.cos(k * (0.5 * x + 0.866 * y)) + np.cos(k * (0.5 * x - 0.866 * y))
    return (z + 5 * np.exp(-((y - 45) ** 2 + (x - 50) ** 2) / 72.0)) * 1e-11


def _frame(arr, bwd=None, name="f.sxm"):
    return LoadedFrame(forward=arr, backward=bwd, nm_per_px=0.1, width_nm=0.1 * arr.shape[1],
                       bias_v=0.5, name=name)


def test_metadata_read_only_analysis():
    m = AverageFramesDriftCorrected().metadata()
    assert m.category == SkillCategory.ANALYSIS and m.safety_level == SafetyLevel.AUTO
    names = {p.name for p in m.parameters}
    assert {"scan_paths", "channel", "include_backward", "rmax_px"} <= names


def test_registered_in_builtins():
    import mast.skills.builtins as B
    assert B.AverageFramesDriftCorrected is AverageFramesDriftCorrected
    assert "AverageFramesDriftCorrected" in B.__all__


def test_end_to_end_with_synthetic_frames(monkeypatch, tmp_path):
    rng = np.random.default_rng(0)
    truth = [(0.0, 0.0), (1.5, -2.0), (-2.2, 1.1)]
    frames = {"a.sxm": _frame(_lat(*truth[0]) + rng.normal(0, 1.5e-11, (N, N)), name="a.sxm"),
              "b.sxm": _frame(_lat(*truth[1]) + rng.normal(0, 1.5e-11, (N, N)), name="b.sxm"),
              "c.sxm": _frame(_lat(*truth[2]) + rng.normal(0, 1.5e-11, (N, N)), name="c.sxm")}
    monkeypatch.setattr("mast.skills.builtins._sxm_frame.load_frame", lambda p, ch="Z": frames[p])
    out = tmp_path / "out"
    r = AverageFramesDriftCorrected().execute(None, {"scan_paths": "a.sxm,b.sxm,c.sxm", "output_dir": str(out)})
    assert r.success, r.error
    d = r.data
    assert d["n_images"] == 3
    assert d["statement"] == "average of 3 drift-corrected images"
    assert "3 drift-corrected" in r.summary or "3 张漂移校正" in r.summary
    for got, t in zip(d["shifts_px"], truth):
        assert abs(got[0] - t[0]) < 0.25 and abs(got[1] - t[1]) < 0.25
    assert d["rms_average"] < d["rms_single"]
    assert np.load(d["npy_path"]).shape == tuple(d["shape"])
    assert d["png_path"] and (out / d["png_path"].split("\\")[-1].split("/")[-1]).exists()


def test_backward_images_are_included_by_default(monkeypatch, tmp_path):
    f = _frame(_lat(), bwd=_lat(0.7, 0.4))
    monkeypatch.setattr("mast.skills.builtins._sxm_frame.load_frame", lambda p, ch="Z": f)
    r = AverageFramesDriftCorrected().execute(None, {"scan_paths": "x.sxm", "output_dir": str(tmp_path)})
    assert r.success and r.data["n_images"] == 2
    r = AverageFramesDriftCorrected().execute(None, {"scan_paths": "x.sxm", "include_backward": False,
                                                     "output_dir": str(tmp_path)})
    assert not r.success                      # one image: nothing to average


def test_mismatched_sizes_rejected(monkeypatch, tmp_path):
    fr = {"a": _frame(_lat()), "b": _frame(_lat(n=80))}
    monkeypatch.setattr("mast.skills.builtins._sxm_frame.load_frame", lambda p, ch="Z": fr[p])
    r = AverageFramesDriftCorrected().execute(None, {"scan_paths": "a,b", "output_dir": str(tmp_path)})
    assert r.success is False and "sizes" in r.error.lower() or "尺寸" in r.error


def test_missing_file_and_empty_input():
    r = AverageFramesDriftCorrected().execute(None, {"scan_paths": "no_such.sxm"})
    assert r.success is False and r.data["rejected"]
    assert AverageFramesDriftCorrected().execute(None, {"scan_paths": ""}).success is False


def test_real_sxm_fixture_twice_gives_zero_shift(request, tmp_path):
    p = str(request.config.rootpath / SXM)
    r = AverageFramesDriftCorrected().execute(None, {"scan_paths": "%s,%s" % (p, p), "output_dir": str(tmp_path),
                                                     "rmax_px": 5, "include_backward": False})
    assert r.success, r.error
    assert all(abs(v) < 0.05 for s in r.data["shifts_px"] for v in s)
