"""Magnet field and liquid-helium level — read-only.

Two questions an experiment on a magnet cryostat keeps asking — "what field is the
sample in" and "how much helium is left" — had no answer inside MAST: the Lake
Shore 625 magnet supply and the Cryomagnetics LM-510 level monitor were not
connected. These skills report what the environment monitor reads from them.

These skills are READ-ONLY. Changing the field is a separate module since 7.2.0:
``magnet_control.RampMagnetField`` / ``StopMagnetRamp``, gated in
``instruments/magnet_625.py``. MAST still cannot switch the persistent-switch
heater, and it never starts a helium-level measurement: the drivers here have no
setter at all (``environment/lakeshore_625.py``, ``environment/cryomag_lm510.py``).
No instrument token, no Nanonis command.
"""

from __future__ import annotations

from mast.core.types import SafetyLevel, SkillCategory, SkillMetadata, SkillResult
from mast.i18n import tr
from mast.skills.base import BaseSkill

_NO_SOURCE = "no_source"
_NO_INSTRUMENT = "no_instrument"

_READ_ONLY_NOTE_MAGNET = tr(
    "本技能只读。要改变磁场用 RampMagnetField（先跑 CheckMagnetFieldChange 看每道闸）："
    "需要初始化里设好最大磁场与场常数；升场要先问用户，把用户授权的原话填进 user_authorization、"
    "授权到的磁场填进 authorized_up_to_t；反向场（负目标）还要用户的话明确写出负向磁场，且扫场不跨零；"
    "默认要求针尖已退。StopMagnetRamp 随时冻结扫场（不是回零）。"
    "`can_set_field`=false 表示初始化还没设好，MAST 现在不改变磁场。",
    "This skill only reads. To change the field use RampMagnetField (run "
    "CheckMagnetFieldChange first to see every gate). It needs the maximum field and the "
    "field constant set at initialization. Before raising, ask the user, put their own "
    "words in user_authorization and the field they authorized in authorized_up_to_t; a "
    "reverse (negative) target also needs their words to name the negative field, and a ramp "
    "never crosses zero. By default the tip must be withdrawn. StopMagnetRamp freezes a ramp at any time; it does "
    "not go to zero. `can_set_field`=false means initialization is not complete, so MAST "
    "does not change the field now.")

_READ_ONLY_NOTE_HELIUM = tr(
    "MAST 只读取液面计**上一次完成的测量**(MEAS?),从不触发测量 —— 每次测量都要给超导"
    "液面探头通电加热、蒸发液氦。液面多久刷新一次由仪器面板上的 Mode/Interval 决定;"
    "Mode=Off 时这个数不会自己更新。",
    "MAST reads the level monitor's **last completed measurement** (MEAS?) and never "
    "starts one: every measurement heats the superconducting level sensor and boils "
    "off helium. How often the level refreshes is set by Mode/Interval on the "
    "instrument; with Mode=Off the number does not update by itself.")

_WHAT_TO_DO = {
    _NO_SOURCE: tr("MAST 这一侧没接上环境监控(独立 API 进程或监控未建起来),与仪器无关。",
                   "MAST's own side has no environment monitor wired (standalone API "
                   "process, or the monitor was not built); unrelated to the instrument."),
    _NO_INSTRUMENT: tr("这台机器上没有接入这种仪器(或没有被识别)。等下去不会有读数;"
                       "请用户在 设置→硬件 里「扫描设备接口」。",
                       "No such instrument is connected (or recognised) on this machine. "
                       "Waiting will not produce a reading; ask the user to run "
                       "Settings → Hardware → Scan device ports."),
}


def _rows(states: "list[dict]") -> tuple[list[dict], list[dict]]:
    readings, instruments = [], []
    for s in states:
        readings.append({k: s.get(k) for k in ("sensor", "value", "unit", "status",
                                                 "timestamp", "port")})
        if s.get("instrument") is not None:
            instruments.append(s["instrument"])
    return readings, instruments


class GetMagnetState(BaseSkill):
    """Read the superconducting magnet's field, current and supply state."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetMagnetState",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "读超导磁体电源(Lake Shore 625)的**当前状态**:磁场(由电流×场常数算出,T)、"
                "输出电流、设定值、扫场速率、是否在扫场、持续开关、失超检测与错误位、"
                "面板是否本地模式,以及 MAST 调场的状态(`control`:是否已在初始化里设好、"
                "正在跑的扫场)。只读,不取仪器令牌。\n"
                "要改变磁场用 RampMagnetField / StopMagnetRamp(见 `note`)。\n"
                "`reason`=no_instrument 表示这台机器没接磁体电源;no_source 表示 MAST 侧没接上。",
                "Read the superconducting magnet supply (Lake Shore 625): field (computed "
                "from current x field constant, T), output current, setpoints, ramp rate, "
                "whether it is ramping, persistent switch, quench detection and error bits, "
                "whether the front panel is in local mode, and MAST's field-control state "
                "(`control`: set up at initialization or not, the ramp in progress). "
                "Read-only, no instrument token.\nTo change the field use RampMagnetField / "
                "StopMagnetRamp (see `note`).\n`reason`=no_instrument means no magnet supply "
                "on this machine; no_source means MAST's side is not wired."),
            parameters=[],
            estimated_duration_s=2.0,
            composition_level=0,
            tags=["magnet", "field", "cryostat", "environment", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        from mast.core import cryostat

        from mast.core import magnet_control

        states = cryostat.magnet_states()
        ctl = magnet_control.controller()
        control = None
        if ctl is not None:
            try:
                control = ctl.status()
            except Exception:  # noqa: BLE001 - status is a convenience, never a failure
                control = None
        data: dict = {"can_set_field": bool(control and control.get("can_set_field")),
                      "control": control, "note": _READ_ONLY_NOTE_MAGNET}
        if states is None:
            data.update(reason=_NO_SOURCE, what_to_do=_WHAT_TO_DO[_NO_SOURCE])
        elif not states:
            data.update(reason=_NO_INSTRUMENT, what_to_do=_WHAT_TO_DO[_NO_INSTRUMENT])
        else:
            readings, instruments = _rows(states)
            data["readings"] = readings
            data["instruments"] = instruments
            for r in readings:
                if r.get("sensor") and r.get("unit") in ("T", "kG"):
                    data.setdefault("field", r.get("value"))
                    data.setdefault("field_unit", r.get("unit"))
                    data.setdefault("field_status", r.get("status"))
                elif r.get("unit") == "A":
                    # 不叫 current_a：那是隧道电流的名字（仪器状态缓存的字段）。7.2.0 叫 current_a 时，
                    # 结果写回把 625 的输出电流写成了隧道电流（另见 core/state.NO_STATE_WRITEBACK_TAGS）。
                    data.setdefault("magnet_current_a", r.get("value"))
        # A query that ran is a success; "no magnet here" is an answer, not a fault.
        return SkillResult(skill_name="GetMagnetState", success=True, data=data)


class GetHeliumLevel(BaseSkill):
    """Read the liquid-helium level (last completed measurement)."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="GetHeliumLevel",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "读液氦液面(Cryomagnetics LM-510)**上一次完成的测量**,带单位(cm/in/%)、"
                "状态(探头开路/烧断时 status=error,数不可信)、仪器的采样模式与间隔。"
                "只读,**从不触发测量**(测量会加热探头、蒸发液氦)。\n"
                "`instruments[].updates_by_itself`=false 表示仪器 Mode=Off,这个数不会自己"
                "更新 —— 需要新读数时请操作员在面板上测一次。",
                "Read the liquid-helium level (Cryomagnetics LM-510): the **last completed "
                "measurement**, with unit (cm/in/%), status (status=error when the sensor "
                "is open or burnt out: the number is not a level), and the instrument's "
                "sample mode and interval. Read-only, **never starts a measurement** (a "
                "measurement heats the sensor and boils off helium).\n"
                "`instruments[].updates_by_itself`=false means Mode=Off on the instrument: "
                "the number will not refresh by itself; ask the operator to take a reading "
                "at the front panel when a fresh one is needed."),
            parameters=[],
            estimated_duration_s=1.5,
            composition_level=0,
            tags=["helium", "level", "cryostat", "environment", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        from mast.core import cryostat

        states = cryostat.helium_states()
        data: dict = {"starts_measurements": False, "note": _READ_ONLY_NOTE_HELIUM}
        if states is None:
            data.update(reason=_NO_SOURCE, what_to_do=_WHAT_TO_DO[_NO_SOURCE])
        elif not states:
            data.update(reason=_NO_INSTRUMENT, what_to_do=_WHAT_TO_DO[_NO_INSTRUMENT])
        else:
            readings, instruments = _rows(states)
            data["readings"] = readings
            data["instruments"] = instruments
            first = readings[0]
            data.update(level=first.get("value"), unit=first.get("unit"),
                        status=first.get("status"))
        return SkillResult(skill_name="GetHeliumLevel", success=True, data=data)


__all__ = ["GetMagnetState", "GetHeliumLevel"]
