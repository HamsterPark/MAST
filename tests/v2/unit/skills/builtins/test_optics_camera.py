"""对光相机技能（契约 §13.1「相机」）：mast.skills.builtins.optics_camera。

* 9 个技能都是 AUTO、只有 GetCameraStatus 是只读；都不取仪器令牌、都不受样品门控；
* 经技能走一遍：连接 → 直播 → 改曝光 → 快照（原始数组 + PNG + 元数据进当前实验文件夹，元数据里
  有曝光、增益、ROI、时间、光斑、光学轴位置）→ 状态 → 停 → 断开；
* 没有样品时快照留在数据根的 artifacts/camera/；连着没直播时快照自己开、自己停；
* 失败都是清楚的 SkillResult：没连接、被 ThorImageCAM 占着、没有这台相机、超范围；
* 快照读光学轴位置有时限，没连的台子不去连。
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[5] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import io
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from mast.core.instrument_lock import needs_token
from mast.core.sample_gate import requires_sample
from mast.core.types import SafetyLevel, SkillCategory
from mast.instruments import camera as C
from mast.instruments import registry as reg_mod
from mast.instruments.camera import CameraConfig, CameraManager, FakeBackend, FakeSpot
from mast.logging import experiment_log as el
from mast.skills.builtins import optics_camera as oc
from mast.skills.builtins.optics_camera import (
    CameraSnapshot,
    CameraStartLive,
    CameraStopLive,
    ConnectCamera,
    DisconnectCamera,
    GetCameraStatus,
    SetCameraExposure,
    SetCameraGain,
    SetCameraROI,
)

ALL = [ConnectCamera, DisconnectCamera, CameraStartLive, CameraStopLive, CameraSnapshot,
       SetCameraExposure, SetCameraGain, SetCameraROI, GetCameraStatus]
BUSY_TEXT = "被 ThorImageCAM 占用，请先关掉它"


class FakeStorage:
    def __init__(self):
        self.exp = {"e1": {"name": "对光", "start_time": "2026-10-20T13:00:00", "dir_name": ""}}
        self.smp = {"s1": {"name": "Au tip", "sample_index": 1, "dir_name": "", "description": "",
                           "sample_type": "", "sample_subtype": "", "start_time": "2026-10-20T13:01:00"}}

    def get_experiment(self, eid):
        return dict(self.exp[eid]) if eid in self.exp else None

    def get_sample(self, sid):
        return dict(self.smp[sid]) if sid in self.smp else None

    def sample_ordinal(self, sid):
        return 1

    def set_experiment_dir_name(self, eid, name):
        self.exp[eid]["dir_name"] = name

    def set_sample_dir_name(self, sid, name, idx):
        self.smp[sid]["dir_name"] = name

    def get_samples(self, eid):
        return [dict(v, id=k) for k, v in self.smp.items()]


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    log = SimpleNamespace(_storage=FakeStorage(), current_experiment_id="e1", current_sample_id="s1")
    prev = el.get_active_log()
    el.set_active_log(log)
    yield tmp_path
    el.set_active_log(prev)


@pytest.fixture
def no_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("MAST_EXPERIMENT_ROOT", str(tmp_path / "experiments"))
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path / "root"))
    prev = el.get_active_log()
    el.set_active_log(None)
    yield tmp_path
    el.set_active_log(prev)


class _Axis:
    def __init__(self, pos, delay=0.0):
        self.pos, self.delay = pos, delay

    def get_position(self):
        time.sleep(self.delay)
        return self.pos


class _Ctrl:
    def __init__(self, axes, connected=True):
        self._axes, self.is_connected = axes, connected

    def axes(self):
        if not self.is_connected:
            raise AssertionError("快照不许去连没连着的台子")
        return dict(self._axes)


class _Registry:
    """optics_positions 与 _optical_axes 用到的那几个方法。"""

    def __init__(self, built):
        self._built = built

    def find_devices(self, **kw):
        return []

    def built_controllers(self):
        return dict(self._built)


@pytest.fixture
def bench(monkeypatch):
    reg = _Registry({"lens_z": _Ctrl({"z": _Axis(12.5)}), "cold": _Ctrl({}, connected=False)})
    monkeypatch.setattr(reg_mod, "get_instrument_registry", lambda: reg)
    return reg


@pytest.fixture
def fake():
    fb = FakeBackend(width=320, height=240, mosaic=True, max_fps=60,
                     spot=FakeSpot(x=161.0, y=119.0, sigma_x=8.0, sigma_y=8.0))
    holders: list[str] = []
    mgr = CameraManager([CameraConfig(camera_id="zelux", backend="fake", enabled=True)],
                        backend_factory=lambda cfg: fb, holder_probe=lambda: list(holders))
    prev = C.install_camera_manager(mgr)
    yield SimpleNamespace(fb=fb, mgr=mgr, holders=holders, cam=mgr.camera("zelux"))
    mgr.close_all()
    C.install_camera_manager(prev)
    deadline = time.monotonic() + 3.0
    while any(t.name.startswith("camera-live-") for t in threading.enumerate()):
        assert time.monotonic() < deadline, "取帧线程没收干净"
        time.sleep(0.02)


def _run(cls, **params):
    return cls().execute(None, params)


# ── 元数据 ────────────────────────────────────────────────────────────────────

def test_nine_skills_all_auto_and_only_status_reads():
    metas = {cls().metadata().name: cls().metadata() for cls in ALL}
    assert set(metas) == set(oc.CAMERA_SKILLS)
    for name, meta in metas.items():
        assert meta.safety_level is SafetyLevel.AUTO, name
        assert meta.category is (SkillCategory.READ if name == "GetCameraStatus" else SkillCategory.WRITE)
        cid = {p.name: p for p in meta.parameters}["camera_id"]
        assert cid.required is False


def test_no_camera_skill_takes_the_instrument_token_or_needs_a_sample():
    from mast.core.instrument_lock import BYPASS_NAMES

    for cls in ALL:
        meta = cls().metadata()
        assert needs_token(meta, meta.name) is False, f"{meta.name} 会去抢 Nanonis 的令牌"
        assert requires_sample(meta, meta.name) is False, f"{meta.name} 被样品门控挡住"
        # 不是补救动作：进了 BYPASS_NAMES，面板会把它画成停止按钮、HITL 会把它当 remedy
        assert meta.name not in BYPASS_NAMES, meta.name


def test_the_camera_skills_belong_to_the_ters_optics_module():
    from mast.skills import hardware_modules as hm

    for cls in ALL:
        meta = cls().metadata()
        assert "optional-hardware" in meta.tags and hm.SKILL_OWNER.get(meta.name) == "ters_optics", meta.name


def test_parameter_bounds_are_checked_before_execute():
    assert SetCameraExposure().validate_params({"exposure_us": 0})
    assert SetCameraExposure().validate_params({"exposure_us": 1000.5})       # 要整数
    assert not SetCameraExposure().validate_params({"exposure_us": 2000})
    assert SetCameraROI().validate_params({"x": 0, "y": 0, "w": 10})          # 缺 h
    assert not SetCameraROI().validate_params({"x": 0, "y": 0, "w": 10, "h": 10, "binning": None})


# ── 经技能走一遍 ──────────────────────────────────────────────────────────────

def test_flow_with_a_snapshot_into_the_experiment(fake, scope, bench):
    r = _run(ConnectCamera)
    assert r.success, r.error
    assert "已连接" in r.summary and r.data["camera"]["state"] == "connected"
    assert _run(CameraStartLive).success
    r = _run(SetCameraExposure, exposure_us=12_000)
    assert r.success and r.data["camera"]["exposure_us"] == 12_000
    assert _run(SetCameraGain, gain_db=3.0).success

    r = _run(CameraSnapshot, tag="lens z+20")
    assert r.success, r.error
    d = r.data
    assert d["stored_in_experiment"] is True
    npy, png, meta_p = (Path(d["files"][k]) for k in ("raw", "png", "metadata"))
    for p in (npy, png, meta_p):
        assert p.is_file() and "experiments" in p.parts
        assert p.name.startswith("camera_zelux_") and "_lens_z_20" in p.name
    assert not any((scope / "root" / "artifacts" / "camera").rglob("*.npy"))  # 暂存的拷贝已删
    raw = np.load(npy)
    assert raw.dtype == np.uint16 and raw.shape == (240, 320)
    from PIL import Image

    with Image.open(png) as im:
        assert im.mode == "L" and im.size == (160, 120)                       # 2×2 合成的灰度
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    assert meta["exposure_us"] == 12_000 and meta["gain_db"] == 3.0 and meta["settled"] is True
    assert meta["roi"] == {"x": 0, "y": 0, "w": 320, "h": 240}
    assert meta["tag"] == "lens z+20" and meta["time"] and meta["t"] > 0
    assert meta["raw"]["file"] == npy.name and meta["png"]["file"] == png.name
    assert meta["spot"]["centroid_px"] == pytest.approx([161.0, 119.0], abs=0.1)
    assert meta["optical_axes"]["local"] == {"lens_z": {"z": 12.5}}            # 只读已连着的台子
    assert not {"error", "incomplete"} & set(meta["optical_axes"]), meta["optical_axes"]
    assert meta["scope"] == {"experiment_id": "e1", "sample_id": "s1"}
    assert r.images == [str(png)]

    r = _run(GetCameraStatus)
    assert r.success and r.data["camera"]["live"] is True
    assert r.data["spot"]["centroid_px"] == pytest.approx([161.0, 119.0], abs=0.1)
    assert "光斑质心" in r.summary
    assert _run(CameraStopLive).data["camera"]["state"] == "connected"
    r = _run(DisconnectCamera)
    assert r.success and r.data["camera"]["state"] == "disconnected"
    assert fake.fb.close_calls == 1 and not fake.fb.is_open


def test_snapshot_without_a_sample_stays_in_staging(fake, no_scope, bench):
    _run(ConnectCamera)
    r = _run(CameraSnapshot)
    assert r.success, r.error
    assert r.data["stored_in_experiment"] is False
    npy = Path(r.data["files"]["raw"])
    assert npy.is_file() and "artifacts" in npy.parts and "camera" in npy.parts
    assert "没有活跃样品" in r.summary


def test_snapshot_while_connected_but_not_live_starts_and_stops_itself(fake, no_scope, bench):
    _run(ConnectCamera)
    r = _run(CameraSnapshot)
    assert r.success, r.error
    assert fake.cam.state == "connected" and not fake.fb.armed


def test_clear_failures_when_not_connected(fake, no_scope):
    for cls, params in [(CameraSnapshot, {}), (CameraStartLive, {}),
                        (SetCameraExposure, {"exposure_us": 1000})]:
        r = _run(cls, **params)
        assert not r.success and "没连接" in r.error, (cls, r.error)
    assert _run(CameraStopLive).success                        # 停 / 断开在没连时什么也不做
    assert _run(DisconnectCamera).success


def test_connect_names_thorimagecam_when_it_holds_the_camera(fake):
    fake.fb.busy = True
    fake.holders.append("ThorImageCAM")
    r = _run(ConnectCamera)
    assert not r.success and BUSY_TEXT in r.error
    r = _run(GetCameraStatus)
    assert r.success and r.data["camera"]["occupied_by"] == "ThorImageCAM"


def test_out_of_range_is_refused_with_the_cameras_own_range(fake):
    _run(ConnectCamera)
    r = _run(SetCameraExposure, exposure_us=10)
    assert not r.success and r.error.startswith("refused:") and "[40, 20000000]" in r.error
    r = _run(SetCameraROI, x=1, y=0, w=100, h=100)
    assert not r.success and "偶数" in r.error
    r = _run(SetCameraROI, x=0, y=0, w=160, h=120)
    assert r.success and r.data["camera"]["roi"] == {"x": 0, "y": 0, "w": 160, "h": 120, "binning": 1}


def test_unknown_camera_and_no_camera_at_all(fake):
    r = _run(ConnectCamera, camera_id="nope")
    assert not r.success and "没有相机" in r.error
    empty = CameraManager([], holder_probe=lambda: [])
    prev = C.install_camera_manager(empty)
    try:
        r = _run(GetCameraStatus)
        assert r.success and r.data["cameras"] == [] and r.summary == "没有配置相机"
        r = _run(ConnectCamera)
        assert not r.success and "没有配置相机" in r.error
    finally:
        C.install_camera_manager(prev)


def test_reading_axis_positions_is_bounded_in_time(monkeypatch):
    reg = _Registry({"slow": _Ctrl({"x": _Axis(1.0, delay=5.0)}), "cold": _Ctrl({}, connected=False)})
    monkeypatch.setattr(reg_mod, "get_instrument_registry", lambda: reg)
    t0 = time.monotonic()
    out = oc._optical_axes(budget_s=0.3)
    assert time.monotonic() - t0 < 1.5
    assert "incomplete" in out
