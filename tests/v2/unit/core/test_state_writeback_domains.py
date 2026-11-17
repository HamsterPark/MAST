"""技能结果写回仪器状态缓存：只写隧道结的状态，不写别的仪器的量。

曾出现过：``GetMagnetState`` 的结果里有 ``current_a``
（Lake Shore 625 的输出电流，例如 0.5 T 时 1.4715 A）。``patch_state_from_result`` 按键名把它写成了
**隧道电流**，``/api/hardware/live-readings`` 会返回这个值，直到下一次 1 Hz 刷新。

这里钉三层：
1. 收侧：带低温 / 磁体 / 环境标签的技能，结果一律不写回（走**真** ``ExecutionContext.run``）；
2. 收侧兜底：``apply_patch`` 拒收超过 1 mA 的「隧道电流」；
3. 产侧：GetMagnetState 不再用 ``current_a`` 这个名字。
"""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)

import pytest  # noqa: E402

from mast.core.state import (  # noqa: E402
    MAX_PLAUSIBLE_TUNNEL_CURRENT_A,
    NO_STATE_WRITEBACK_TAGS,
    InstrumentState,
    patch_state_from_result,
)
from mast.core.types import HardwareState  # noqa: E402

TUNNEL_A = 1.0e-9       # +2 V / 1 nA，示意工作点
MAGNET_A = 1.4715       # 0.5 T 时 625 的输出电流


def _state(**kw) -> InstrumentState:
    st = InstrumentState.__new__(InstrumentState)
    st._cache = HardwareState()
    for k, v in kw.items():
        setattr(st._cache, k, v)
    return st


class _Pool:
    def safe_call(self, m, *a, role="main"):
        from mast.core.types import NanonisCallRecord
        return NanonisCallRecord(method=m, args=a)


@pytest.fixture()
def magnet_source():
    """一台 625：0.5 T、1.4715 A。测完把来源复位。"""
    from mast.core import cryostat, magnet_control

    cryostat.set_magnet_source(lambda: [
        {"sensor": "magnet_field", "value": 0.5012, "unit": "T", "status": "ok",
         "port": "COM13", "instrument": {"model": "MODEL625", "field": 0.5012}},
        {"sensor": "magnet_current", "value": MAGNET_A, "unit": "A", "status": "ok",
         "port": "COM13"},
    ])
    magnet_control.set_source(None)
    yield
    cryostat.set_magnet_source(None)
    magnet_control.set_source(None)


# ── 1. ⭐ 真路径：GetMagnetState 跑完，隧道电流不变 ─────────────────────────────
def test_get_magnet_state_through_the_real_context_leaves_the_tunnel_current_alone(magnet_source):
    from mast.core.execution_context import ExecutionContext
    from mast.core.registry import SkillRegistry
    from mast.skills.builtins.cryostat import GetMagnetState

    reg = SkillRegistry()
    reg.register(GetMagnetState)
    st = _state(current_a=TUNNEL_A, setpoint_a=1.0e-10)
    res = ExecutionContext(pool=_Pool(), state=st, registry=reg).run("GetMagnetState", {})

    assert res.success, res
    assert res.data.get("magnet_current_a") == MAGNET_A, "前提：技能确实读到了磁体电流"
    assert st._cache.current_a == TUNNEL_A, (
        f"GetMagnetState 之后隧道电流成了 {st._cache.current_a!r} A —— 磁体电流又被写回了状态缓存")
    assert st._cache.setpoint_a == 1.0e-10


def test_a_cryostat_skill_that_returns_current_a_is_stopped_on_the_real_path():
    """上一条在 GetMagnetState 改了键名之后，没有标签闸门也会过 —— 这一条才钉住**收侧**：
    一个带 cryostat 标签、结果里仍叫 ``current_a`` 的技能，走真 ``ExecutionContext.run``。"""
    from mast.core.execution_context import ExecutionContext
    from mast.core.registry import SkillRegistry
    from mast.core.types import SafetyLevel, SkillCategory, SkillMetadata, SkillResult
    from mast.skills.base import BaseSkill

    class _CryoDouble(BaseSkill):
        def metadata(self):
            return SkillMetadata(
                name="_CryoDouble", version="1.0.0", category=SkillCategory.READ,
                safety_level=SafetyLevel.AUTO, description="test double",
                estimated_duration_s=0.0, composition_level=0, tags=["cryostat", "read"])

        def execute(self, context, params):
            return SkillResult(skill_name="_CryoDouble", success=True,
                               data={"current_a": 3.0e-10, "setpoint_a": 2.0})

    reg = SkillRegistry()
    reg.register(_CryoDouble)
    st = _state(current_a=TUNNEL_A, setpoint_a=1.0e-10)
    res = ExecutionContext(pool=_Pool(), state=st, registry=reg).run("_CryoDouble", {})
    assert res.success
    assert (st._cache.current_a, st._cache.setpoint_a) == (TUNNEL_A, 1.0e-10)


def test_both_write_back_sites_pass_the_skill_tags():
    """结构闸门：agent 工具边界与 composite 子步骤两处都把 ``tags=`` 交给写回函数。
    少传一处，那条路径上的低温技能结果就又会写进隧道结的状态。"""
    import ast

    root = Path(_MASTV2_ROOT) / "mast"
    for path in (root / "agents" / "_shared" / "skill_adapter.py",
                 root / "core" / "execution_context.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "patch_state_from_result"]
        assert calls, f"{path.name} 不再调用 patch_state_from_result"
        for c in calls:
            assert "tags" in {k.arg for k in c.keywords}, (
                f"{path.name}:{c.lineno} 调 patch_state_from_result 没传 tags=")


# ── 2. 收侧：标签决定写不写回，与键名无关 ────────────────────────────────────
@pytest.mark.parametrize("tag", sorted(NO_STATE_WRITEBACK_TAGS))
def test_a_skill_tagged_as_another_instrument_never_writes_back(tag):
    """哪怕结果里的键名和量级都像隧道结（产侧将来又起了个撞名的键），也不写。"""
    st = _state(current_a=TUNNEL_A, bias_v=2.0, setpoint_a=1.0e-10)
    patch_state_from_result(st, {"current_a": 3.0e-10, "bias_v": 0.1, "setpoint_a": 5e-11},
                            what="SomeCryostatSkill", tags=["read", tag])
    assert (st._cache.current_a, st._cache.bias_v, st._cache.setpoint_a) == (TUNNEL_A, 2.0, 1.0e-10)


def test_an_spm_skill_still_writes_back():
    """反向：Nanonis 技能的写回（composite 路径「先开反馈再移动」的修复）不能被这次改动带走。"""
    st = _state(bias_v=2.0, z_controller_on=False)
    patch_state_from_result(st, {"bias_v": 0.5, "z_controller_on": True}, what="SetBias",
                            tags=["bias", "write"])
    assert st._cache.bias_v == 0.5 and st._cache.z_controller_on is True
    st2 = _state(bias_v=2.0)
    patch_state_from_result(st2, {"bias_v": 0.7}, what="SetBias")          # 没带标签：照旧写
    assert st2._cache.bias_v == 0.7


# ── 3. 收侧兜底：不可能的隧道电流不进缓存 ───────────────────────────────────
def test_apply_patch_refuses_a_current_no_tunnel_junction_can_carry():
    st = _state(current_a=TUNNEL_A)
    st.apply_patch(current_a=MAGNET_A)
    assert st._cache.current_a == TUNNEL_A
    st.apply_patch(current_a=-0.01)
    assert st._cache.current_a == TUNNEL_A
    st.apply_patch(current_a=2.5e-9)
    assert st._cache.current_a == 2.5e-9
    st.apply_patch(current_a=MAX_PLAUSIBLE_TUNNEL_CURRENT_A)        # 边界本身照收
    assert st._cache.current_a == MAX_PLAUSIBLE_TUNNEL_CURRENT_A


def test_the_bound_does_not_touch_a_df_setpoint():
    """df 反馈下 setpoint_a 存的是赫兹（例如 -5 Hz）：不能被电流的物理上限拒掉。"""
    st = _state(setpoint_a=-2.0)
    st.apply_patch(setpoint_a=-5.0)
    assert st._cache.setpoint_a == -5.0


# ── 4. 产侧守卫：读别的仪器的技能都带着不写回的标签 ─────────────────────────
def test_every_cryostat_magnet_and_temperature_skill_carries_a_no_writeback_tag():
    """新加一个低温 / 磁体技能而忘了标签，结果写回就又把别的量写进隧道结的状态。"""
    import inspect

    from mast.skills.base import BaseSkill
    from mast.skills.builtins import cryostat, heater_control, magnet_control, temperature

    found = []
    for mod in (cryostat, heater_control, magnet_control, temperature):
        for _name, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, BaseSkill) and cls is not BaseSkill and cls.__module__ == mod.__name__:
                meta = cls().metadata()
                found.append(meta.name)
                assert not NO_STATE_WRITEBACK_TAGS.isdisjoint(meta.tags or []), (
                    f"{meta.name} 读的不是隧道结，却没有 {sorted(NO_STATE_WRITEBACK_TAGS)} 里的任何标签")
    assert {"GetMagnetState", "GetHeliumLevel", "RampMagnetField", "StopMagnetRamp",
            "CheckMagnetFieldChange", "CheckSampleTemperatureChange", "SetSampleTemperature",
            "HoldSampleTemperature", "HeaterOff", "SetHeaterZoneTable",
            "SetHeaterControlMode"} <= set(found), found
