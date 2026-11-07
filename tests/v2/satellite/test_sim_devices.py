"""参考卫星的设备语义（不经网络）：光谱仪按 LightField 的名字校验设置、运动类 axis_state
带枢纽命令 id。

参考卫星比真机宽松 ⇒ 拼错的名字在这里测得通、到真机才 invalid_args（曾有
"EffectiveExposure" 这样一路测绿到 C# 卫星才被拒）。所以这里把参考卫星的名单钉在
LightField 自己的 API 元数据和 C# 卫星的 ``SpectrometerArgs`` 上（这两份文件不在这个
checkout 里就跳过），并逐个验证 MAST 技能会发的每个名字参考卫星都收。
"""

from __future__ import annotations

import asyncio
import copy
import re
from pathlib import Path

import pytest

from mast.satellite.sim import devices as D
from mast.satellite.sim.devices import ActionCtx, DeviceError

_REPO = Path(__file__).resolve().parents[3]
_LF_API = (_REPO / "local-samples" / "team" / "lightfield" / "raw" / "api"
           / "api_LightFieldAddInSupportServices.txt")
_CS_ARGS = _REPO / "satellite" / "MastSatellite" / "Devices" / "SimDevices.cs"


class _Sat:
    """设备只用到卫星的 emit / note_file_meta。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def emit(self, device: str, kind: str, data: dict) -> None:
        self.events.append((device, kind, copy.deepcopy(data)))

    def note_file_meta(self, path, meta) -> None:
        pass

    def of(self, kind: str) -> list[dict]:
        return [d for _, k, d in self.events if k == kind]


def _run(dev, action: str, args: dict | None = None, *, origin: str = "hub",
         cmd_id: str | None = "c1"):
    return asyncio.run(dev.run(action, args or {}, ActionCtx(cmd_id, origin)))


def _ccd() -> D.SimSpectrometer:
    return D.SimSpectrometer(_Sat(), "ccd", speed=1000.0)


# ── 光谱仪设置：LightField 的名字、全有或全无 ──────────────────────────────

def test_configure_refuses_a_name_lightfield_does_not_have_and_changes_nothing():
    ccd = _ccd()
    before = copy.deepcopy(ccd.settings)
    with pytest.raises(DeviceError) as ei:
        _run(ccd, "configure", {"exposure_ms": 500, "output_signal": "EffectiveExposure"})
    assert ei.value.code == "invalid_args"
    assert "EffectivelyExposing" in ei.value.message        # 报错里列出正确的名字
    assert ccd.settings == before                            # 曝光也没改：不留半套设置


def test_configure_matches_names_case_insensitively_and_stores_lightfields_spelling():
    ccd = _ccd()
    _run(ccd, "configure", {"output_signal": "effectivelyexposing", "shutter_mode": "alwaysopen",
                            "trigger": {"edge": "rising"},
                            "timestamps": "ExposureEnded, ExposureStarted"})
    s = ccd.settings
    assert s["output_signal"] == "EffectivelyExposing" and s["shutter_mode"] == "AlwaysOpen"
    assert s["trigger"] == {"response": "NoResponse", "edge": "RisingEdge"}   # 合并，不是替换
    assert s["timestamps"] == "ExposureStarted,ExposureEnded"
    _run(ccd, "configure", {"timestamps": False})
    assert ccd.settings["timestamps"] == "None"


@pytest.mark.parametrize("args", [
    {"exposure": 100},                                      # 拼错的键
    {"trigger": {"resp": "NoResponse"}},                    # 拼错的子键
    {"trigger": "NoResponse"},                              # 子项不是对象
    {"trigger": {"response": "ExternalTrigger"}},           # 不存在的名字
    {"adc_quality": "LowNoise "},
    {"timestamps": "None,ExposureStarted"},
    {"frame_tracking": "yes"},
    {"frames": 2.5}, {"frames": 0}, {"frames": float("inf")},
    {"exposure_ms": True}, {"exposure_ms": float("nan")}, {"exposure_ms": "abc"},
    {"adc_mhz": 2.0},
    {"roi": {"mode": "line", "rows": 0}},
    {"roi": {"mode": "line", "rows": 101}},                 # 传感器 1340 × 100
    {"roi": {"mode": "custom", "x": 1300, "y": 0, "width": 100, "height": 10}},
    {"roi": {"mode": "custom", "x": 0, "y": 0, "width": 10, "height": 10, "regions": []}},
    {"roi": {"mode": "custom", "regions": [{"x": 0, "y": 0, "width": 10, "height": 10,
                                            "x_bin": 20}]}},
    {"roi": {"mode": "box"}},
    {"file": {"base_name": ""}},
    {"file": {"attach_date": "maybe"}},
])
def test_configure_rejects_bad_values_with_invalid_args(args):
    ccd = _ccd()
    before = copy.deepcopy(ccd.settings)
    with pytest.raises(DeviceError) as ei:
        _run(ccd, "configure", args)
    assert ei.value.code == "invalid_args"
    assert ccd.settings == before


def test_custom_roi_without_y_bin_is_binned_to_one_row():
    """与 C# 卫星一致：custom 区域不给 y_bin ⇒ 硬件里合并成一行（y_bin = height）。"""
    ccd = _ccd()
    _run(ccd, "configure", {"roi": {"mode": "custom", "x": 0, "y": 40, "width": 1340,
                                    "height": 20}})
    assert ccd.settings["roi"] == {"mode": "custom", "regions": [
        {"x": 0, "y": 40, "width": 1340, "height": 20, "x_bin": 1, "y_bin": 20}]}


def test_set_settings_validates_like_configure():
    ccd = _ccd()
    with pytest.raises(DeviceError) as ei:
        _run(ccd, "set_settings", {"values": {"output_signal": "EffectiveExposure"}})
    assert ei.value.code == "invalid_args"
    with pytest.raises(DeviceError) as ei:
        _run(ccd, "set_settings", {"values": {"roi": {"mode": "full"}}})    # 不在白名单
    assert ei.value.code == "invalid_args"
    assert _run(ccd, "set_settings", {"values": {"adc_quality": "highcapacity"}}) == {
        "adc_quality": "HighCapacity"}


def test_acquire_keeps_its_exposure_and_frames_like_lightfield():
    """acquire 带的 exposure_ms / frames 改的是实验本身的设置（LightField、C# 卫星都这样），
    之后 get_settings 看得到；越界照样 invalid_args。"""
    ccd = _ccd()
    out = _run(ccd, "acquire", {"exposure_ms": 1.0, "frames": 2, "save": False})
    assert out["frames"] == 2
    assert _run(ccd, "get_settings", {"keys": ["exposure_ms", "frames"]}) == {
        "exposure_ms": 1.0, "frames": 2}
    with pytest.raises(DeviceError) as ei:
        _run(ccd, "acquire", {"frames": 0, "save": False})
    assert ei.value.code == "invalid_args"


def test_every_option_mast_offers_is_accepted_by_the_reference_satellite():
    """MAST 技能（optics_spectro）会发的每一个名字，参考卫星都要收 —— 否则技能测试就只能
    绕开参考卫星。"""
    from mast.skills.builtins import optics_spectro as sp

    ccd = _ccd()
    for key, values in (("shutter_mode", sp.SHUTTER_MODES), ("adc_quality", sp.ADC_QUALITIES),
                        ("output_signal", sp.OUTPUT_SIGNALS)):
        for v in values:
            _run(ccd, "configure", {key: v})
            assert ccd.settings[key] == v
    for v in sp.TRIGGER_RESPONSES:
        _run(ccd, "configure", {"trigger": {"response": v}})
        assert ccd.settings["trigger"]["response"] == v
    for v in sp.TRIGGER_EDGES:
        _run(ccd, "configure", {"trigger": {"edge": v}})
        assert ccd.settings["trigger"]["edge"] == v


def _lf_enum(text: str, name: str) -> set[str]:
    """API 元数据里 ``enum PrincetonInstruments.LightField.AddIns.<name>`` 的成员名。"""
    out: set[str] = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"enum PrincetonInstruments\.LightField\.AddIns\.{name}\b", line):
            for member in lines[i + 1:]:
                m = re.match(r"\s+(\w+)\s*=\s*-?\d+", member)
                if not m:
                    break
                out.add(m.group(1))
            break
    out.discard("UninitializedEnum")
    return out


@pytest.mark.skipif(not _LF_API.is_file(), reason="LightField API dump not in this checkout")
def test_name_tables_are_lightfields_own_enums():
    text = _LF_API.read_text(encoding="utf-8", errors="replace")
    for enum, ours in (("TriggerResponse", D._LF["trigger.response"]),
                       ("TriggerDetermination", D._LF["trigger.edge"]),
                       ("ShutterTimingMode", D._LF["shutter_mode"]),
                       ("AdcQuality", D._LF["adc_quality"]),
                       ("OutputSignal", D._LF["output_signal"]),
                       ("TimeStamps", D._STAMP_NAMES)):
        members = _lf_enum(text, enum)
        assert members, f"API 元数据里找不到 enum {enum}"
        assert set(ours) == members, f"{enum}：差 {set(ours) ^ members}"


def _cs_string_lists(text: str) -> dict[str, list[str]]:
    out = {m.group(1): re.findall(r'"([^"]+)"', m.group(2)) for m in re.finditer(
        r"static readonly string\[\] (\w+)\s*=\s*\{(.*?)\}", text, re.S)}
    m = re.search(r"ConfigureKeys\s*=\s*new HashSet<string>\([^)]*\)\s*\{(.*?)\}", text, re.S)
    if m:
        out["ConfigureKeys"] = re.findall(r'"([^"]+)"', m.group(1))
    return out


@pytest.mark.skipif(not _CS_ARGS.is_file(), reason="C# satellite source not in this checkout")
def test_name_tables_match_the_csharp_satellite():
    cs = _cs_string_lists(_CS_ARGS.read_text(encoding="utf-8"))
    for name, ours in (("TriggerResponses", D._LF["trigger.response"]),
                       ("TriggerEdges", D._LF["trigger.edge"]),
                       ("ShutterModes", D._LF["shutter_mode"]),
                       ("AdcQualities", D._LF["adc_quality"]),
                       ("OutputSignals", D._LF["output_signal"]),
                       ("StampNames", D._STAMP_NAMES),
                       ("ConfigureKeys", D._CONFIGURE_KEYS),
                       ("TriggerKeys", D._SUB_KEYS["trigger"]),
                       ("FileKeys", D._SUB_KEYS["file"]),
                       ("RoiKeys", D._SUB_KEYS["roi"])):
        assert name in cs, f"C# 的 SpectrometerArgs.{name} 不见了（改名了？）"
        assert set(cs[name]) == set(ours), f"{name}：差 {set(cs[name]) ^ set(ours)}"


# ── 运动类：axis_state 带枢纽命令 id ───────────────────────────────────────

def _moving(kind: str, d: dict) -> bool:
    if kind == "openloop_stepper":
        return any(a["moving"] for a in d["axes"].values())
    return bool(d["moving"])


@pytest.mark.parametrize("make, action, args", [
    (lambda s: D.SimRotationStage(s, "pol", speed=100.0, require_home=False),
     "move_rel", {"deg": 30.0}),
    (lambda s: D.SimPicomotor(s, "pico", speed=100.0), "jog", {"axis": "1", "steps": 200}),
    (lambda s: D.SimXYZ(s, "xyz", speed=100.0), "move_rel", {"dx": 0.3}),
])
def test_axis_state_names_the_hub_command_only_while_it_moves(make, action, args):
    """枢纽靠 axis_state.data.cmd_id 把 move 类命令从 accepted 推到 running；停下之后那条
    axis_state（moving=false）立刻补发、不带 cmd_id；人工运动从来不带。"""
    sat = _Sat()
    dev = make(sat)
    _run(dev, action, args, cmd_id="c-hub")
    ax = sat.of("axis_state")
    moving = [d for d in ax if _moving(dev.kind, d)]
    assert moving and all(d.get("cmd_id") == "c-hub" for d in moving)
    assert not _moving(dev.kind, ax[-1]) and "cmd_id" not in ax[-1]

    sat.events.clear()
    # 人工动作的走法同 SimSatellite 的人工入口：直接进作业，origin=manual、没有命令 id
    neg = {k: (-v if isinstance(v, (int, float)) else v) for k, v in args.items()}
    manual = ActionCtx(None, "manual")
    asyncio.run(dev._run_job(action, getattr(dev, f"act_{action}")(neg, manual), manual))
    ax = sat.of("axis_state")
    assert any(_moving(dev.kind, d) for d in ax)
    assert not any("cmd_id" in d for d in ax)


# ── 手动面板的新动作（契约 §13.1 末尾）────────────────────────────────────────

def test_rotation_set_velocity_and_identify():
    pol = D.SimRotationStage(_Sat(), "pol", speed=100.0)
    assert {"set_velocity", "identify"} <= set(pol.caps)
    st = _run(pol, "set_velocity", {"deg_s": 10.0, "accel_deg_s2": 5.0})
    assert st["velocity_deg_s"] == 10.0 and st["accel_deg_s2"] == 5.0
    st = _run(pol, "set_velocity", {"deg_s": 25.0})                 # 只改速度，加速度不动
    assert st["velocity_deg_s"] == 25.0 and st["accel_deg_s2"] == 5.0
    for bad in ({"deg_s": 0}, {"deg_s": 25.5}, {"deg_s": True}, {"deg_s": 5, "accel_deg_s2": 30},
                {}):
        with pytest.raises(DeviceError) as ei:
            _run(pol, "set_velocity", bad)
        assert ei.value.code == "invalid_args", bad
    _run(pol, "identify")
    assert pol.identify_count == 1
    pol.operator_hold = True                                         # 人工接管：不是只读动作
    for action, args in (("identify", {}), ("set_velocity", {"deg_s": 1.0})):
        with pytest.raises(DeviceError) as ei:
            _run(pol, action, args)
        assert ei.value.code == "operator_hold"


def test_picomotor_move_abs_respects_the_per_command_cap():
    pico = D.SimPicomotor(_Sat(), "pico", speed=100.0, max_steps_per_cmd=500)
    assert {"move_abs", "set_velocity", "set_accel", "abort"} <= set(pico.caps)
    assert _run(pico, "move_abs", {"axis": "2", "steps": 400})["axes"]["2"]["steps"] == 400
    assert _run(pico, "move_abs", {"axis": "2", "steps": -50})["axes"]["2"]["steps"] == -50  # 走 450 步
    with pytest.raises(DeviceError) as ei:
        _run(pico, "move_abs", {"axis": "2", "steps": 600})          # 要走 650 步
    assert ei.value.code == "limit" and pico.axes["2"] == -50
    for bad in ({"axis": "2", "steps": 1.5}, {"axis": "9", "steps": 1}, {"axis": "2"}):
        with pytest.raises(DeviceError) as ei:
            _run(pico, "move_abs", bad)
        assert ei.value.code == "invalid_args", bad
    _run(pico, "set_zero", {"axis": "2"})
    assert _run(pico, "move_abs", {"axis": "2", "steps": 300})["axes"]["2"]["steps"] == 300


def test_picomotor_velocity_and_accel_follow_the_8742_ranges():
    pico = D.SimPicomotor(_Sat(), "pico", speed=100.0)
    st = _run(pico, "set_velocity", {"axis": "1", "steps_s": 2000})
    assert st["axes"]["1"]["velocity_steps_s"] == 2000.0
    st = _run(pico, "set_accel", {"axis": "1", "steps_s2": 200_000})
    assert st["axes"]["1"]["accel_steps_s2"] == 200_000.0
    assert st["axes"]["2"]["velocity_steps_s"] == 1000.0              # 每个轴各自一份
    for action, bad in (("set_velocity", {"axis": "1", "steps_s": 0}),
                        ("set_velocity", {"axis": "1", "steps_s": 2001}),
                        ("set_velocity", {"axis": "1", "steps_s": 10.5}),
                        ("set_accel", {"axis": "1", "steps_s2": 200_001}),
                        ("set_accel", {"axis": "x", "steps_s2": 10})):
        with pytest.raises(DeviceError) as ei:
            _run(pico, action, bad)
        assert ei.value.code == "invalid_args", (action, bad)


def test_picomotor_abort_stops_at_once_and_passes_operator_hold():
    """abort 不走 stop 的减速延迟（stop_latency_s），人工接管期间也放行。"""
    import time as _time

    sat = _Sat()
    pico = D.SimPicomotor(sat, "pico", speed=0.01, stop_latency_s=5.0)   # 一步要走很久

    async def _go():
        job = asyncio.ensure_future(pico.run("jog", {"axis": "1", "steps": 100}, ActionCtx("c1", "hub")))
        while not pico.busy:
            await asyncio.sleep(0.01)
        pico.operator_hold = True
        t0 = _time.monotonic()
        st = await pico.run("abort", {}, ActionCtx("c2", "hub"))
        took = _time.monotonic() - t0
        with pytest.raises(asyncio.CancelledError):
            await job
        return st, took

    st, took = asyncio.run(_go())
    assert took < 1.0, f"abort 等了 {took:.2f} s（走了 stop 的延迟？）"
    assert not pico.busy and st["axes"]["1"]["moving"] is False
