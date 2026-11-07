"""手动控制面板（契约 §13.1）：``GET /api/manual/devices`` 的描述、卫星设备的新技能。

真枢纽 + 真 WebSocket + 参考卫星（10 倍速），本机 ``optical_instruments.json`` 登记卫星设备
（与 tests/v2/instruments/test_remote_integration.py 同一份 bench），技能注册表用真的
``SkillRegistry().discover()`` —— 钉住测试要对着真注册的技能核对，替身会让它恒绿。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from mast.instruments.registry import InstrumentRegistry, reset_instrument_registry
from mast.instruments.remote import set_hub_provider

from tests.v2.satellite.conftest import (  # noqa: F401 — fixtures
    hub,
    sat_root,
    server,
    sims,
    wait_for,
    write_sat_config,
)


def _bench(root: Path, *, skip: tuple[str, ...] = ()) -> InstrumentRegistry:
    devices = [
        {"id": "ccd", "type": "satellite_spectrometer", "satellite": "sim1", "device": "ccd",
         "role": "raman_ccd"},
        {"id": "pol", "type": "satellite_rotation", "satellite": "sim1", "device": "pol",
         "axes": [{"name": "angle", "channel": 1, "min_pos": -360, "max_pos": 720,
                   "unit": "deg", "role": "polarizer"}]},
        {"id": "xyz", "type": "satellite_motion", "satellite": "sim1", "device": "xyz",
         "role": "focus_xyz", "max_step_mm": 0.5,
         "axes": [{"name": a, "channel": a, "min_pos": lo, "max_pos": hi, "unit": "mm"}
                  for a, lo, hi in (("x", -2, 13.3), ("y", 0, 5), ("z", 0, 9.6))]},
        {"id": "pico", "type": "satellite_openloop", "satellite": "sim1", "device": "pico",
         "max_steps_per_cmd": 1000,
         "axes": [{"name": "2", "channel": 2, "min_pos": -20000, "max_pos": 20000,
                   "unit": "steps", "role": "beam_steer"}]},
    ]
    p = root / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [d for d in devices if d["id"] not in skip],
                             "delay_line": None}), encoding="utf-8")
    reg = InstrumentRegistry(p)
    reset_instrument_registry(reg)
    return reg


@pytest.fixture(scope="module")
def skills():
    from mast.core.registry import SkillRegistry

    reg = SkillRegistry()
    reg.discover()
    return reg


@pytest.fixture
def online(hub, server, sat_root, sims, tmp_path):
    token = write_sat_config(sat_root, "sim1")
    hub._load_config(force=True)
    sim = sims(server.ws_url, token, watch_dir=tmp_path / "watch")
    assert sim.wait_connected(10)
    wait_for(lambda: hub.link_state("sim1") == "connected", msg="connected")
    wait_for(lambda: all((d.get("axis") is not None or d["kind"] not in
                          ("rotation_stage", "openloop_stepper", "xyz_stage"))
                         and d.get("latest") is not None and not d.get("stale")
                         for s in hub.list_satellites() for d in s["devices"]),
             msg="first device_state / axis_state")
    set_hub_provider(None)
    yield sim
    reset_instrument_registry(None)


@pytest.fixture
def no_local_or_camera(monkeypatch):
    """本机与相机两类提供者（devices / web 在写）换成空清单，只看卫星这一类。"""
    import mast.manual as M

    monkeypatch.setitem(M.PROVIDERS, "local", lambda: ([], []))
    monkeypatch.setitem(M.PROVIDERS, "camera", lambda: ([], []))


def _client(skills_registry):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mast.api.context import AppContext
    from mast.api.routes import manual

    app = FastAPI()
    ctx = AppContext()
    if skills_registry is not None:
        ctx.wire(skill_registry=skills_registry)
    app.state.ctx = ctx
    app.include_router(manual.router, prefix="/api")
    return TestClient(app)


def _get(skills_registry) -> dict:
    r = _client(skills_registry).get("/api/manual/devices")
    assert r.status_code == 200, r.text
    return r.json()


def _by_id(body: dict) -> dict[str, dict]:
    return {d["id"]: d for d in body["devices"]}


def _actions(dev: dict) -> dict[str, dict]:
    return {a["id"]: a for a in dev["actions"]}


# ── 描述端点 ────────────────────────────────────────────────────────────────

def test_satellite_devices_get_buttons_for_their_caps(online, sat_root, skills, no_local_or_camera):
    _bench(sat_root)
    body = _get(skills)
    assert body["degraded"] is False and body["degraded_sources"] == []
    devs = _by_id(body)
    assert set(devs) == {"sim1/ccd", "sim1/pol", "sim1/pico", "sim1/xyz"}   # spe（文件监视）不进面板
    pol = devs["sim1/pol"]
    assert pol["kind"] == "rotation_stage" and pol["machine"] == "sim1" and pol["available"] is True
    acts = _actions(pol)
    assert set(acts) == {"jog", "move_rel", "move_abs", "home", "set_velocity", "identify", "stop"}
    jog = acts["jog"]
    assert jog["skill"] == "SetPolarizerAngle" and jog["fixed"] == {"device_id": "pol", "relative": True}
    assert jog["direction"] == "signed" and jog["direction_param"] == "angle_deg"
    assert jog["hold_repeat_ms"] and jog["stop_on_release"] is True
    (angle,) = jog["params"]                                        # 逐字取自技能的 ParameterSpec
    assert (angle["name"], angle["unit"], angle["min_value"], angle["max_value"]) == \
        ("angle_deg", "deg", -1080.0, 1080.0)
    assert jog["safety"] == "confirm" and jog["bypass_lock"] is False
    stop = acts["stop"]
    assert stop["skill"] == "StopSatelliteDevice" and stop["fixed"] == {"satellite": "sim1", "device": "pol"}
    assert stop["safety"] == "auto"
    assert pol["readout"] == {"kind": "live", "stream": "sim1/pol/angle", "interval_s": None}
    (ax,) = pol["axes"]
    assert ax["unit"] == "deg" and ax["min"] == -360 and ax["max"] == 720 and ax["homed"] is False

    pico = devs["sim1/pico"]
    pacts = _actions(pico)
    assert set(pacts) == {"jog", "move_abs", "set_velocity", "set_accel", "set_zero", "abort", "stop"}
    assert all(pacts[a]["per_axis"] for a in ("jog", "move_abs", "set_velocity", "set_accel", "set_zero"))
    assert pacts["abort"]["hint"] == "立即停所有轴，不减速" and pacts["abort"]["bypass_lock"] is True
    assert [a["name"] for a in pico["axes"]] == ["2"] and pico["axes"][0]["extra"]["channel"] == "2"
    assert pico["readout"]["stream"] == "sim1/pico/steps"

    xyz = devs["sim1/xyz"]
    assert set(_actions(xyz)) == {"move_abs", "move_rel", "stop"}
    assert [a["name"] for a in xyz["axes"]] == ["x", "y", "z"]

    ccd = devs["sim1/ccd"]
    cacts = _actions(ccd)
    assert set(cacts) == {"set_grating", "set_center", "set_exit_port", "stop"}
    assert cacts["set_grating"]["params"][0]["allowed_values"]            # 这台实际装的光栅
    # 读数不走技能执行入口：没有轴流的设备看 device_state 的数值流
    assert ccd["readout"] == {"kind": "live", "stream": "sim1/ccd/state", "interval_s": None}


def test_every_action_names_a_registered_skill_and_its_real_parameters(
        online, sat_root, skills, no_local_or_camera):
    """钉住：提供者给出的**原样**动作（端点核对之前）—— skill 已注册，fixed / params /
    direction_param 的名字都是该技能的参数，per_axis 的技能有 axis 参数。端点自己会丢掉坏动作，
    所以要在端点之前查，否则这条测试永远是绿的。"""
    from mast.manual import collect_manual_devices

    _bench(sat_root)
    raw, degraded, _notes = collect_manual_devices()
    assert degraded == [] and raw
    checked = 0
    for dev in raw:
        for a in dev["actions"]:
            meta = skills._get_metadata(skills.get(a["skill"]))      # KeyError = 没注册
            names = {p.name for p in meta.parameters}
            used = set(a["fixed"]) | {p["name"] for p in a["params"]}
            if a.get("direction_param"):
                used.add(a["direction_param"])
            if a.get("per_axis"):
                used.add("axis")
            assert used <= names, (dev["id"], a["id"], a["skill"], used - names)
            checked += 1
    assert checked >= 20
    # 端点那一层也没丢任何动作
    body = _get(skills)
    assert not [n for n in body["notes"] if "不显示" in n], body["notes"]


def test_each_source_degrades_on_its_own(online, sat_root, skills, monkeypatch):
    import mast.manual as M

    _bench(sat_root)

    def boom():
        raise RuntimeError("驱动没装")

    def slow():
        time.sleep(5)
        return [], []

    monkeypatch.setitem(M.PROVIDERS, "local", boom)
    monkeypatch.setitem(M.PROVIDERS, "camera", slow)
    monkeypatch.setattr(M, "PROVIDER_TIMEOUT_S", 0.3)
    t0 = time.monotonic()
    body = _get(skills)
    assert time.monotonic() - t0 < 3.0                       # 慢的那一类没拖住端点
    assert body["degraded"] is True and body["degraded_sources"] == ["local", "camera"]
    assert any("驱动没装" in n for n in body["notes"]) and any("没有返回" in n for n in body["notes"])
    assert "sim1/pol" in _by_id(body)                        # 卫星那一类照常


def test_a_malformed_provider_entry_is_dropped_not_fatal(online, sat_root, skills, monkeypatch):
    import mast.manual as M

    _bench(sat_root)
    good = {"id": "local/stage/zep", "machine": "local", "kind": "linear_stage", "label": "Z",
            "available": True, "actions": [
                {"id": "stop", "label": "停", "skill": "StopSatelliteDevice",
                 "fixed": {"satellite": "x", "device": "y"}, "hint": "只停这一台"},
                {"id": "ghost", "label": "幽灵", "skill": "NoSuchSkill"},
                {"id": "typo", "label": "参数拼错", "skill": "StopSatelliteDevice",
                 "fixed": {"satelite": "x"}}]}
    bad = {"id": "local/stage/bad", "machine": "local", "kind": "teleporter", "label": "?"}
    monkeypatch.setitem(M.PROVIDERS, "local", lambda: ([good, bad], []))
    monkeypatch.setitem(M.PROVIDERS, "camera", lambda: ([], []))
    body = _get(skills)
    devs = _by_id(body)
    assert "local/stage/bad" not in devs and body["degraded_sources"] == ["local"]
    assert set(_actions(devs["local/stage/zep"])) == {"stop"}        # 幽灵按钮与拼错的参数都没给
    assert sum("不显示" in n for n in body["notes"]) == 2
    assert _actions(devs["local/stage/zep"])["stop"]["bypass_lock"] is True
    assert _actions(devs["local/stage/zep"])["stop"]["hint"] == "只停这一台"


def test_offline_and_operator_hold_are_reported_and_stop_stays(online, sat_root, skills,
                                                               no_local_or_camera, hub):
    _bench(sat_root)
    online.sat.set_operator_hold("pol", True)
    wait_for(lambda: any(d["id"] == "pol" and d.get("operator_hold")
                         for d in hub.satellite("sim1")["devices"]), msg="hold visible")
    pol = _by_id(_get(skills))["sim1/pol"]
    assert pol["available"] is False and "人工接管" in pol["unavailable_reason"]
    assert pol["occupied_by"] and "stop" in _actions(pol)
    online.sat.set_operator_hold("pol", False)

    online.stop()                                              # 卫星走了
    wait_for(lambda: hub.link_state("sim1") != "connected", timeout=10, msg="link down")
    devs = _by_id(_get(skills))
    assert set(devs) >= {"sim1/pol", "sim1/pico"}               # 最后已知的设备照列
    for dev in devs.values():
        assert dev["available"] is False and "sim1" in dev["unavailable_reason"]
        assert "stop" in _actions(dev)
        assert all(a.get("position") is None for a in dev["axes"])   # 断线时不报陈旧读数


def test_unregistered_device_only_gets_stop_and_identify(online, sat_root, skills,
                                                        no_local_or_camera):
    _bench(sat_root, skip=("pol",))
    pol = _by_id(_get(skills))["sim1/pol"]
    assert pol["available"] is False and "optical_instruments.json" in pol["unavailable_reason"]
    assert set(_actions(pol)) == {"identify", "stop"}


def test_caps_gate_the_buttons_and_the_placeholder_xyz_says_why():
    from mast.satellite.manual import describe_device

    xyz = {"id": "xyz", "kind": "xyz_stage", "driver": "motion_manipulator", "sim": False,
           "state": "ready", "caps": ["status", "stop", "safe"], "info": {}, "stale": False,
           "axis": {"x_mm": 1.0, "y_mm": 2.0, "z_mm": 3.0, "moving": False}}
    entry = {"id": "ters_xyz", "type": "satellite_motion", "satellite": "s", "device": "xyz"}
    d = describe_device("s", "connected", xyz, entry)
    assert d["available"] is False and d["unavailable_reason"] == "等厂家协议（Motion_Manipulator）"
    assert [a["id"] for a in d["actions"]] == ["stop"]
    assert [a["position"] for a in d["axes"]] == [1.0, 2.0, 3.0]     # 只读坐标照样显示

    rot = {"id": "pol", "kind": "rotation_stage", "driver": "kinesis_kdc101", "state": "ready",
           "caps": ["status", "stop", "safe", "move_abs", "move_rel", "home"], "info": {}}
    d = describe_device("s", "connected", rot, {"id": "p", "satellite": "s", "device": "pol"})
    assert {a["id"] for a in d["actions"]} == {"jog", "move_rel", "move_abs", "home", "stop"}
    assert describe_device("s", "connected", {"id": "pmt", "kind": "pmt", "caps": []}, None) is None

    # 真 8742 走 usb 时卫星只报 jog / move_abs / set_zero / stop：面板上就只有这几个，枢纽不补
    pico = {"id": "pico", "kind": "openloop_stepper", "driver": "picomotor8742", "state": "ready",
            "caps": ["status", "stop", "safe", "jog", "move_abs", "set_zero"], "info": {"axes": ["1"]}}
    d = describe_device("s", "connected", pico, {"id": "pk", "satellite": "s", "device": "pico",
                                                 "axes": [{"name": "1", "channel": 1}]})
    assert [a["id"] for a in d["actions"]] == ["jog", "move_abs", "set_zero", "stop"]


# ── 卫星设备的新技能 ─────────────────────────────────────────────────────────

class _Ctx:
    def check_abort(self) -> bool:
        return False


def test_stop_and_abort_reach_the_satellite(online, sat_root):
    from mast.skills.builtins.optics_satellite_manual import PicomotorAbort, StopSatelliteDevice

    _bench(sat_root)
    pico = online.sat.devices["pico"]
    online.sat.call_threadsafe(lambda: setattr(pico, "velocity", {a: 5.0 for a in pico.axes}))
    from mast.skills.builtins.optics_alignment import PicomotorJog

    box: dict = {}
    th = threading.Thread(target=lambda: box.setdefault("r", PicomotorJog().execute(
        _Ctx(), {"axis": "2", "steps": 900})))
    th.start()
    wait_for(lambda: pico.busy, msg="jogging")
    r = StopSatelliteDevice().execute(_Ctx(), {"satellite": "sim1", "device": "pico", "axis": "2"})
    assert r.success and r.data["confirmed"] is True, r.error
    th.join(10)
    assert not pico.busy and box["r"].success is False           # 被停掉的点动如实报失败

    th = threading.Thread(target=lambda: PicomotorJog().execute(_Ctx(), {"axis": "2", "steps": 900}))
    th.start()
    wait_for(lambda: pico.busy, msg="jogging again")
    r = PicomotorAbort().execute(_Ctx(), {})
    assert r.success and r.data["via"] == "abort", r.error
    th.join(10)
    assert not pico.busy

    r = StopSatelliteDevice().execute(_Ctx(), {"satellite": "nosuch", "device": "pol"})
    assert not r.success and "nosuch" in r.error


def test_identify_status_and_polarizer_velocity(online, sat_root):
    from mast.skills.builtins.optics_satellite_manual import (
        GetSatelliteDeviceStatus,
        IdentifySatelliteDevice,
        SetPolarizerVelocity,
    )

    _bench(sat_root)
    pol = online.sat.devices["pol"]
    r = IdentifySatelliteDevice().execute(_Ctx(), {"satellite": "sim1", "device": "pol"})
    assert r.success and pol.identify_count == 1, r.error
    r = GetSatelliteDeviceStatus().execute(_Ctx(), {"satellite": "sim1", "device": "pol"})
    assert r.success and r.data["status"]["state"] == "ready"
    r = SetPolarizerVelocity().execute(_Ctx(), {"deg_s": 12.5, "accel_deg_s2": 4.0})
    assert r.success and pol.velocity == 12.5 and pol.accel == 4.0, r.error
    r = SetPolarizerVelocity().execute(_Ctx(), {"deg_s": 40.0})       # 卫星拒收，不截断
    assert not r.success and pol.velocity == 12.5


def test_picomotor_move_abs_velocity_and_zero(online, sat_root):
    from mast.skills.builtins.optics_satellite_manual import (
        PicomotorMoveAbs,
        PicomotorSetZero,
        SetPicomotorVelocity,
    )

    _bench(sat_root)
    pico = online.sat.devices["pico"]
    r = PicomotorMoveAbs().execute(_Ctx(), {"axis": "2", "steps": 800})
    assert r.success and r.data["counter_after"] == 800, r.error
    r = PicomotorMoveAbs().execute(_Ctx(), {"axis": "2", "steps": -400})   # 要走 1200 > 上限 1000
    assert not r.success and "cap" in r.error and pico.axes["2"] == 800
    r = SetPicomotorVelocity().execute(_Ctx(), {"axis": "2", "steps_s": 1500})
    assert r.success and pico.velocity["2"] == 1500.0, r.error
    r = PicomotorSetZero().execute(_Ctx(), {"axis": "2"})
    assert r.success and r.data["counter_after"] == 0 and pico.axes["2"] == 0, r.error


def test_only_the_stops_skip_the_instrument_token(skills):
    from mast.core.instrument_lock import needs_token

    def meta(name):
        return skills._get_metadata(skills.get(name))

    assert not needs_token(meta("StopSatelliteDevice"), "StopSatelliteDevice")
    assert not needs_token(meta("PicomotorAbort"), "PicomotorAbort")
    assert not needs_token(meta("GetSatelliteDeviceStatus"), "GetSatelliteDeviceStatus")   # 只读
    for name in ("SetPolarizerVelocity", "PicomotorMoveAbs", "SetPicomotorVelocity",
                 "SetPicomotorAccel", "PicomotorSetZero", "IdentifySatelliteDevice"):
        assert needs_token(meta(name), name), name


def test_readouts_of_devices_without_axes_come_from_device_state(online, hub):
    """光谱仪这类没有轴流的设备，device_state 的数值字段进 ``<sat>/<dev>/state``；
    运动类设备不重复发（它们的读数在轴流里）。"""
    live = hub.test_live
    wait_for(lambda: live.frames.get("sim1/ccd/state"), msg="ccd state stream")
    values = live.frames["sim1/ccd/state"][-1]["values"]
    assert values["temperature_c"] == -120.0 and "exposure_ms" in values
    assert all(isinstance(v, float) for v in values.values())        # 字符串、布尔不进标量流
    assert "sim1/pol/state" not in live.frames and "sim1/spe/state" not in live.frames


# ── 本机读数端点 GET /api/manual/readouts ─────────────────────────────────────

def test_readouts_endpoint_answers_every_id_and_never_fails(monkeypatch):
    import mast.manual as M

    def provider(ids):
        return {"local/stage/zep": {"t": 1.0, "error": None, "axes": [
            {"name": "x", "position": 1.5, "moving": False, "homed": True, "extra": {"connected": True}}]}}

    monkeypatch.setattr(M, "READOUT_PROVIDER", provider)
    monkeypatch.setattr(M, "SATELLITE_READOUT_PROVIDER", lambda ids: {
        "sim1/pol": {"t": 2.0, "error": None, "axes": [{"name": "angle", "position": 45.0}]}})
    c = _client(None)
    body = c.get("/api/manual/readouts", params={
        "ids": "local/stage/zep,local/stage/gone,sim1/pol,local/camera/cam0,garbage"}).json()["readouts"]
    assert body["local/stage/zep"]["axes"][0]["position"] == 1.5
    assert body["local/stage/gone"]["error"]                          # 提供者没给的本机 id
    assert body["sim1/pol"]["axes"][0]["position"] == 45.0            # 卫星：枢纽缓存
    assert "/api/camera/" in body["local/camera/cam0"]["error"]       # 相机不走这里
    assert "认不出" in body["garbage"]["error"]

    def boom(ids):
        raise RuntimeError("串口在别人手里")

    monkeypatch.setattr(M, "READOUT_PROVIDER", boom)
    body = c.get("/api/manual/readouts", params={"ids": "local/stage/zep"}).json()["readouts"]
    assert "串口在别人手里" in body["local/stage/zep"]["error"]

    monkeypatch.setattr(M, "READOUT_PROVIDER", lambda ids: time.sleep(5))
    monkeypatch.setattr(M, "PROVIDER_TIMEOUT_S", 0.3)
    t0 = time.monotonic()
    body = c.get("/api/manual/readouts", params={"ids": "local/stage/zep"}).json()["readouts"]
    assert time.monotonic() - t0 < 3.0 and "没有返回" in body["local/stage/zep"]["error"]
    assert c.get("/api/manual/readouts").json() == {"readouts": {}}


def test_readouts_never_execute_a_skill_take_the_token_or_open_a_device(monkeypatch, tmp_path):
    """钉住「读数不走技能执行入口」：真提供者（devices 的 local_manual_readouts）+ 一台没打开的
    本机台子。执行入口、仪器令牌、打开设备，三样哪一样被碰到都直接失败。"""
    import mast.api.direct_exec as de
    from mast.core import instrument_lock as il

    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [{
        "id": "zep", "type": "pztc_nm003", "port": "COM19",
        "axes": [{"name": "x", "channel": 1, "min_pos": 0, "max_pos": 10, "unit": "mm"}]}],
        "delay_line": None}), encoding="utf-8")
    reg = InstrumentRegistry(p)
    reset_instrument_registry(reg)

    def forbidden(*a, **k):
        raise AssertionError("读数端点碰了不该碰的东西")

    monkeypatch.setattr(de, "run_and_record", forbidden)
    monkeypatch.setattr(il.InstrumentLock, "hold", forbidden)
    monkeypatch.setattr(InstrumentRegistry, "controller", forbidden)
    try:
        body = _client(None).get("/api/manual/readouts", params={"ids": "local/stage/zep"}).json()
    finally:
        reset_instrument_registry(None)
    v = body["readouts"]["local/stage/zep"]
    assert v["error"] is None and v["axes"][0]["extra"] == {"connected": False}


def test_the_real_local_and_camera_providers_pass_the_pin(skills, tmp_path, monkeypatch):
    """devices 与 camera 的真提供者（本机没有硬件、没有相机配置也照样描述）：每个原样动作的
    技能已注册、参数名都是技能的参数。提供者模块还不在的话那一类跳过（只查在的）。"""
    import mast.manual as M

    p = tmp_path / "optical_instruments.json"
    p.write_text(json.dumps({"devices": [
        {"id": "zep", "type": "pztc_nm003", "port": "COM19",
         "axes": [{"name": "x", "channel": 1, "min_pos": 0, "max_pos": 10, "unit": "mm"}]},
        {"id": "rot", "type": "sim_rotation",
         "axes": [{"name": "angle", "channel": 1, "min_pos": -360, "max_pos": 360, "unit": "deg"}]},
        {"id": "ol", "type": "sim_openloop", "max_steps_per_cmd": 500,
         "axes": [{"name": "1", "channel": 1, "min_pos": -5000, "max_pos": 5000, "unit": "steps"}]},
    ], "delay_line": None}), encoding="utf-8")
    reset_instrument_registry(InstrumentRegistry(p))
    monkeypatch.setitem(M.PROVIDERS, "satellite", lambda: ([], []))
    try:
        raw, degraded, notes = M.collect_manual_devices()
        body = _get(skills)
    finally:
        reset_instrument_registry(None)
    checked = 0
    for dev in raw:
        for a in dev.get("actions") or []:
            meta = skills._get_metadata(skills.get(a["skill"]))
            names = {pp.name for pp in meta.parameters}
            used = set(a.get("fixed") or {}) | {pp["name"] for pp in a.get("params") or []}
            if a.get("direction_param"):
                used.add(a["direction_param"])
            if a.get("per_axis"):
                used.add("axis")
            assert used <= names, (dev["id"], a["id"], a["skill"], used - names)
            checked += 1
    assert {"local/stage/zep", "local/stage/rot", "local/stage/ol"} <= {d["id"] for d in raw}
    assert checked >= 10, checked
    bad = [n for n in body["notes"] if "不显示" in n or "不合契约" in n]
    assert not bad, bad


def test_satellite_readouts_come_from_the_hub_cache_and_send_nothing(online, hub, sat_root,
                                                                     monkeypatch):
    """卫星设备的读数只回枢纽缓存：一条命令都不发（submit / call 一碰就失败）。"""
    _bench(sat_root)

    def forbidden(*a, **k):
        raise AssertionError("读数端点向卫星发了命令")

    monkeypatch.setattr(hub, "submit", forbidden)
    monkeypatch.setattr(hub, "call", forbidden)
    c = _client(None)
    body = c.get("/api/manual/readouts",
                 params={"ids": "sim1/pol,sim1/pico,sim1/ccd,sim1/nope"}).json()["readouts"]
    assert body["sim1/pol"]["error"] is None
    assert body["sim1/pol"]["axes"] == [{"name": "angle", "position": 0.0, "moving": False,
                                         "homed": False, "extra": None}]
    assert body["sim1/pico"]["axes"][0]["name"] == "2" and body["sim1/pico"]["axes"][0]["position"] == 0.0
    assert body["sim1/ccd"] == {"t": body["sim1/ccd"]["t"], "axes": [], "error": None}
    assert "不知道" in body["sim1/nope"]["error"]
    online.stop()
    wait_for(lambda: hub.link_state("sim1") != "connected", timeout=10, msg="link down")
    body = c.get("/api/manual/readouts", params={"ids": "sim1/pol"}).json()["readouts"]
    assert body["sim1/pol"]["axes"][0]["position"] is None and "没有新读数" in body["sim1/pol"]["error"]


def test_tip_requirement_shown_is_the_one_the_skill_enforces(online, sat_root, skills,
                                                            no_local_or_camera):
    """卡上显示的针尖要求 = 运动技能实际判的（optical_instruments.json 那一条）。PicomotorMoveAbs
    在读不到 STM 状态时拒绝运动（读不到 ≠ 没问题）；写错的要求让设备不可用。"""
    from mast.skills.builtins.optics_satellite_manual import PicomotorMoveAbs

    _bench(sat_root)
    entries = json.loads((sat_root / "optical_instruments.json").read_text(encoding="utf-8"))
    for e in entries["devices"]:
        if e["id"] == "pico":
            e["tip_requirement"] = "tip_withdrawn"
        if e["id"] == "xyz":
            e["tip_requirement"] = "tip_retracted"                       # 拼错的值
    (sat_root / "optical_instruments.json").write_text(json.dumps(entries), encoding="utf-8")
    reset_instrument_registry(InstrumentRegistry(sat_root / "optical_instruments.json"))
    devs = _by_id(_get(skills))
    assert devs["sim1/pico"]["tip_requirement"] == "tip_withdrawn"
    assert devs["sim1/pol"]["tip_requirement"] == "none"
    assert devs["sim1/xyz"]["available"] is False and "tip_retracted" in devs["sim1/xyz"]["unavailable_reason"]
    pico = online.sat.devices["pico"]
    r = PicomotorMoveAbs().execute(_Ctx(), {"axis": "2", "steps": 100})   # _Ctx 没有 state
    assert not r.success and "读不到 STM 状态" in r.error and pico.axes["2"] == 0


def test_move_abs_counts_from_the_satellite_not_a_cached_copy(online, sat_root, monkeypatch):
    """「当前计数」每次现读卫星（面板上显示的那个数）：卫星的计数被别处改了（卫星重启、有人在
    Picomotor 软件里手动走过）时，走到计数按卫星的数算差值，不按 MAST 手上那份旧读数。"""
    from mast.instruments import remote
    from mast.instruments.registry import get_instrument_registry
    from mast.skills.builtins.optics_satellite_manual import PicomotorMoveAbs

    _bench(sat_root)
    monkeypatch.setattr(remote._RemoteMotionBase, "STATUS_TTL_S", 3600.0)   # 旧读数永不过期
    ctrl = get_instrument_registry().controller("pico")
    assert ctrl.status()["axes"]["2"]["steps"] == 0                         # MAST 手上记着 0
    pico = online.sat.devices["pico"]
    online.sat.call_threadsafe(lambda: pico.axes.__setitem__("2", 500))     # 卫星那边已经是 500
    r = PicomotorMoveAbs().execute(_Ctx(), {"axis": "2", "steps": 600})
    assert r.success, r.error
    assert r.data["counter_before"] == 500 and pico.axes["2"] == 600        # 只走了 100 步


def test_picomotor_accel_reaches_the_satellite(online, sat_root):
    from mast.skills.builtins.optics_satellite_manual import SetPicomotorAccel

    _bench(sat_root)
    pico = online.sat.devices["pico"]
    r = SetPicomotorAccel().execute(_Ctx(), {"axis": "2", "steps_s2": 50_000})
    assert r.success and pico.accel["2"] == 50_000.0, r.error
    r = SetPicomotorAccel().execute(_Ctx(), {"axis": "2", "steps_s2": 300_000})   # 卫星拒收，不截断
    assert not r.success and pico.accel["2"] == 50_000.0

