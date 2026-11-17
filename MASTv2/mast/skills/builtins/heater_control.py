"""Heat the sample stage (Lake Shore 335 temperature controller): check, set, hold, heater off.

Until this module MAST only read the 335 (temperatures, and the heater's state for the
settings panel). Everything that decides whether a change may happen lives in
``mast.instruments.heater_335``; these skills only carry the request there and report
back. That module's header lists every gate and the order of the writes.

* ``CheckSampleTemperatureChange``: a dry run. Every gate's verdict, the expected
  duration and what the instrument reports. Writes nothing.
* ``SetSampleTemperature``: if every gate passes, turns the heater on (or changes the
  setpoint) so the stage ramps to the target at the given rate, then follows the change
  until the temperature has settled (or must stop). It holds the instrument token for
  the ramp and the settling, so no scan runs while the temperature moves. After it ends
  the 335 keeps regulating at the target by itself.
* ``HoldSampleTemperature``: stops a ramp near the present temperature; the heater stays
  on. No token, no gate.
* ``HeaterOff``: ``RANGE 0``, read back until it reads 0. No token, no gate; available
  wherever temperature control is set up, and whenever MAST's own heating is on. On a
  station that never set it up MAST writes nothing to the 335 (ALL OFF at the panel).
* ``SetHeaterZoneTable``: writes the zone table of the heater output (``ZONE``), each zone
  read back, with the heater off; on any failure the old table goes back. Zone values are
  tuned during commissioning with the operator present; autotune is not
  used.
* ``SetHeaterControlMode``: switches the heater output between closed-loop PID and zone
  mode (``OUTMODE``), with the heater off; zone mode needs a zone table that validates.

The requirements behind the gates: the tip is withdrawn before
heating unless that was agreed beforehand (the caller says so and the ledger records
it); MAST's heating can always be turned off; the magnet stays readable while heating
(the 335 and the magnet supply are separate links). External agents may heat, a raise
needs the user's words and the temperature they authorized; an E-STOP turns both
outputs off; a MAST shutdown leaves the heater regulating (the 335's TLIMIT stays, and
the next MAST process watches it again). The maximum setpoint is the stage's absolute
maximum (the 335's TLIMIT at or below it), the heater supports zone tables, and heating
and field changes never happen at the same time.
"""

from __future__ import annotations

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.skills.base import BaseSkill

_NO_CONTROLLER = tr(
    "这台机器上没有识别到 Lake Shore 335 温控仪（或识别到不止一台），MAST 不控温。"
    "请用户在 设置→硬件 里「扫描设备接口」。",
    "No Lake Shore 335 temperature controller is recognised on this machine (or more than "
    "one is), so MAST does not control the temperature. Ask the user to run Settings -> "
    "Hardware -> Scan device ports.")

_TARGET_PARAM = ParameterSpec(
    name="target_k", type="float", unit="K", required=True, min_value=1.0,
    description=tr("目标温度，开尔文；上限是初始化里设定的最高设定温度",
                   "target temperature in kelvin; the limit is the maximum setpoint set at "
                   "initialization"))

_RATE_PARAM = ParameterSpec(
    name="rate_k_per_min", type="float", unit="K/min", required=False, default=None,
    min_value=0.1, max_value=100.0,
    description=tr("扫温速率（K/min，0.1 起，步长 0.1）。不填用初始化允许的最大速率。",
                   "ramp rate (K/min, from 0.1 in steps of 0.1). Empty: the maximum rate "
                   "allowed at initialization."))

_RANGE_PARAM = ParameterSpec(
    name="heater_range", type="str", required=False, default=None,
    allowed_values=["1", "2", "3", "low", "medium", "high"],
    description=tr("加热量程：low（最大功率的 1%）/ medium（10%）/ high（100%）。不填用初始化允许的最大量程。"
                   "低温段一般用低量程；温度上不去再加大。",
                   "heater range: low (1 % of the output's power) / medium (10 %) / high "
                   "(100 %). Empty: the highest range allowed at initialization. Low "
                   "temperatures usually need a low range; go higher only if it cannot reach "
                   "the target."))

_TIP_PARAM = ParameterSpec(
    name="require_tip_withdrawn", type="bool", required=False, default=True,
    description=tr(
        "默认 true：针尖必须已退（Z 停在收回端、反馈关）才改变温度。只有事先和用户约定好要带针变温时才传 "
        "false（会记入台账）：温度变化会让针尖大幅漂移。",
        "Default true: the tip must be withdrawn (Z at the retracted end, feedback off) before "
        "the temperature changes. Pass false only when changing it with the tip in was agreed "
        "with the user beforehand (it is written to the ledger): a changing temperature drifts "
        "the tip a long way."))

_AUTH_PARAM = ParameterSpec(
    name="user_authorization", type="str", required=False, default="",
    description=tr(
        "升温（目标高于当前温度，或给开着的加热器加大量程）时必填：用户授权的原话，要说到温度，"
        "例如「可以加热到 20 K」（照抄这句示例会被拒绝）。没有用户的话就不要填，也不要自己编。"
        "降温、停住和关加热器不需要。",
        "Required when raising the temperature (a target above the present one, or a higher "
        "range on a heater that is on): the user's own words authorizing it, naming the "
        "temperature, e.g. \"ok to heat to 20 K\" (this example copied verbatim is refused). "
        "Without the user's words leave it empty; never write it yourself. Cooling, holding and "
        "turning the heater off do not need it."))

_AUTH_TEMP_PARAM = ParameterSpec(
    name="authorized_up_to_k", type="float", unit="K", required=False, default=None,
    description=tr(
        "升温时必填：用户授权到的温度（开尔文），就是用户授权时说出的那个数。目标超过它就拒绝；"
        "原话里提到了温度时，两者必须一致。再往上要重新问用户。",
        "Required when raising: the temperature (kelvin) the user authorized, the number in "
        "their words. A target above it is refused, and when the words name a temperature the "
        "two must agree. Going higher means asking the user again."))


def _bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("false", "0", "no", "off"):
            return False
        if v in ("true", "1", "yes", "on"):
            return True
        return default
    return bool(value)


def _origin(context, skill: str) -> str:
    owner = str(getattr(context, "owner", "") or "").strip()
    return f"{skill} ({owner})" if owner else skill


def _tip_verdict(context):
    def _read():
        from mast.core.tip_park import tip_parked

        return tip_parked(context)
    return _read


def _controller():
    from mast.core import heater_control

    # Skills run on a worker thread: they may wait for the model of an input built from an
    # old config entry to be learned (one *IDN?), right after a restart.
    return heater_control.controller(learn=True)


def _common(params: dict) -> dict:
    return {"rate_k_per_min": params.get("rate_k_per_min"),
            "heater_range": params.get("heater_range"),
            "user_authorization": str(params.get("user_authorization") or ""),
            "authorized_up_to_k": params.get("authorized_up_to_k"),
            "require_tip_withdrawn": _bool(params.get("require_tip_withdrawn"), True)}


class CheckSampleTemperatureChange(BaseSkill):
    """Dry run of a temperature change: every gate's verdict and the expected duration."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CheckSampleTemperatureChange",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "预检：用 Lake Shore 335 把样品台加热 / 控温到 target_k（开尔文）之前，逐项给出每道闸的判决"
                "（初始化设定、上限、仪器的控制模式与输入、温度上限 TLIMIT、读数、扫温状态、磁场没在变、针尖、"
                "升温时的用户授权；分区模式下还有分区表和途经的分区）、预计用时和仪器读数，以及加热输出的"
                "分区表（zones）。**不写仪器。**先跑它，再决定要不要调 SetSampleTemperature、要向用户要什么。",
                "Dry run before heating / regulating the sample stage with the Lake Shore 335 "
                "at target_k (kelvin): every gate's verdict (initialization settings, limits, "
                "the controller's control mode and input, its temperature limit TLIMIT, the "
                "reading, ramp state, the field not changing, the tip, and for a raise the "
                "user's authorization; in zone mode also the zone table and the zones on the "
                "way), the expected duration, what the controller reports, and the heater "
                "output's zone table (zones). **Writes nothing.** Run it first, then decide "
                "whether to call SetSampleTemperature and what to ask the user for."),
            parameters=[_TARGET_PARAM, _RATE_PARAM, _RANGE_PARAM, _AUTH_PARAM, _AUTH_TEMP_PARAM,
                        _TIP_PARAM],
            estimated_duration_s=5.0,
            composition_level=0,
            tags=["temperature", "heater", "cryostat", "read", "check", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        ctl = _controller()
        if ctl is None:
            return SkillResult(skill_name="CheckSampleTemperatureChange", success=False,
                               error=_NO_CONTROLLER, data={"reason": "no_controller"})
        plan = ctl.plan(params.get("target_k"), tip_verdict=_tip_verdict(context), **_common(params))
        pd = plan.as_dict()
        data = {"would_proceed": plan.ok, **pd, "zones": (pd.get("instrument") or {}).get("zones"),
                "control": ctl.status()}
        summary = (tr("所有闸都通过", "every gate passes") if plan.ok else
                   tr("会被拒绝：", "would be refused: ") + plan.refusal_text())
        return SkillResult(skill_name="CheckSampleTemperatureChange", success=True, data=data,
                           summary=summary)


class SetSampleTemperature(BaseSkill):
    """Ramp the sample stage to a target temperature and follow it until it settles."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetSampleTemperature",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.DANGEROUS,
            description=tr(
                "用 Lake Shore 335 把样品台控温到 target_k（开尔文）：全部闸门通过才写仪器。加热器原本关着时，"
                "先把设定值落到当前温度再开加热器，然后按 rate_k_per_min 扫到目标；一直跟到稳定（或必须停下），"
                "整个过程持有仪器令牌。结束后 335 自己保持在目标温度。\n"
                "**升温前必须先问用户**，把用户授权的原话填进 user_authorization、授权到的温度填进 "
                "authorized_up_to_k；没有用户的话就不要升温。降温不需要授权。默认要求针尖已退（先 WithdrawTip）。"
                "MAST 改磁场时不加热。分区模式下量程和途中的速率由分区表决定（heater_range 不用，速率最多到起点"
                "分区的速率）。\n"
                "随时可以用 HoldSampleTemperature 停住扫温（加热器保持），或用 HeaterOff 关加热器（自然降温）；"
                "取消这个作业等于停住。不确定能不能做时先跑 CheckSampleTemperatureChange。",
                "Regulate the sample stage at target_k (kelvin) with the Lake Shore 335. Nothing "
                "is written unless every gate passes. With the heater off, the setpoint is first "
                "brought to the present temperature and only then is the heater turned on; the "
                "stage then ramps to the target at rate_k_per_min. The skill follows the change "
                "until it settles (or must stop), holding the instrument token throughout; "
                "afterwards the 335 keeps the target by itself.\n**Before raising the "
                "temperature, ask the user**, put their own words in user_authorization and the "
                "temperature they authorized in authorized_up_to_k; without them, do not heat. "
                "Cooling needs no authorization. By default the tip must be withdrawn first "
                "(WithdrawTip). No heating while MAST changes the field. In zone mode the zone "
                "table sets the range and the rate on the way (heater_range is ignored; the "
                "rate is at most the start zone's).\nHoldSampleTemperature stops a ramp at any time (the heater "
                "stays on), HeaterOff turns the heater off (the stage cools by itself); "
                "cancelling this job holds. When unsure, run CheckSampleTemperatureChange first."),
            parameters=[_TARGET_PARAM, _RATE_PARAM, _RANGE_PARAM, _AUTH_PARAM, _AUTH_TEMP_PARAM,
                        _TIP_PARAM,
                        ParameterSpec(name="settle_s", type="float", unit="s", required=False,
                                      default=None, min_value=0.0, max_value=3600.0,
                                      description=tr("在稳定带内待多久算稳定，默认 60 s",
                                                     "how long inside the settle band counts as "
                                                     "settled; default 60 s")),
                        ParameterSpec(name="settle_band_k", type="float", unit="K", required=False,
                                      default=None, min_value=0.001, max_value=10.0,
                                      description=tr("稳定带半宽，默认 max(0.1 K, 目标的 1%)",
                                                     "half-width of the settle band; default "
                                                     "max(0.1 K, 1 % of the target)"))],
            estimated_duration_s=1800.0,
            composition_level=0,
            tags=["temperature", "heater", "cryostat", "write", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "SetSampleTemperature"
        ctl = _controller()
        if ctl is None:
            return SkillResult(skill_name=name, success=False, error=_NO_CONTROLLER,
                               data={"reason": "no_controller"})
        abort = getattr(context, "check_abort", None) or (lambda: False)
        started = ctl.start(params.get("target_k"), origin=_origin(context, name),
                            tip_verdict=_tip_verdict(context), abort_check=abort,
                            **_common(params))
        if not started.get("started"):
            if started.get("noop"):
                return SkillResult(skill_name=name, success=True, data=started,
                                   summary=tr("已经在目标温度上调节，没有写任何东西",
                                              "already regulating at the target; nothing written"))
            return SkillResult(skill_name=name, success=False, data=started,
                               error=str(started.get("error") or tr("没有开始变温",
                                                                     "the change did not start")))
        kw = {}
        if params.get("settle_s") is not None:
            kw["settle_s"] = float(params["settle_s"])
        if params.get("settle_band_k") is not None:
            kw["band_k"] = float(params["settle_band_k"])
        ended = ctl.supervise(abort_check=abort, **kw)
        data = {"plan": started.get("plan"), **ended}
        if ended.get("outcome") == "done":
            return SkillResult(skill_name=name, success=True, data=data,
                               summary=tr(f"已稳定在 {started['plan'].get('target_k')} K",
                                          f"settled at {started['plan'].get('target_k')} K"))
        return SkillResult(skill_name=name, success=False, data=data,
                           error=f"[{ended.get('outcome')}] {ended.get('detail')}")


class HoldSampleTemperature(BaseSkill):
    """Stop a setpoint ramp near the present temperature; the heater stays on. Always allowed."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="HoldSampleTemperature",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "停住 Lake Shore 335 的扫温：设定值停在当前温度附近，加热器保持调节。不需要仪器令牌、不过任何闸，"
                "正在跑的 SetSampleTemperature 会随之结束。没有在扫温时什么也不写。要关加热器用 HeaterOff。",
                "Stop the Lake Shore 335's setpoint ramp: the setpoint stays near the present "
                "temperature and the heater keeps regulating. Needs no instrument token and "
                "passes no gate; a running SetSampleTemperature ends with it. Writes nothing when "
                "nothing is ramping. To turn the heater off use HeaterOff."),
            parameters=[],
            estimated_duration_s=3.0,
            composition_level=0,
            tags=["temperature", "heater", "cryostat", "stop", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "HoldSampleTemperature"
        ctl = _controller()
        if ctl is None:
            return SkillResult(skill_name=name, success=False, error=_NO_CONTROLLER,
                               data={"reason": "no_controller"})
        res = ctl.hold(reason=name, origin=_origin(context, name))
        if res.get("held"):
            return SkillResult(skill_name=name, success=True, data=res,
                               summary=tr("扫温已停住，加热器保持", "ramp stopped; the heater holds"))
        if res.get("nothing_to_hold"):
            return SkillResult(skill_name=name, success=True, data=res,
                               summary=tr("没有在扫温，什么也没写", "nothing was ramping; nothing written"))
        return SkillResult(skill_name=name, success=False, data=res,
                           error=tr(f"没停住：{res.get('error')}。要停止加热请用 HeaterOff，或在 335 面板上按 ALL OFF",
                                    f"not held: {res.get('error')}. To stop heating use HeaterOff, "
                                    "or press ALL OFF on the 335"))


class HeaterOff(BaseSkill):
    """The heater off (range 0), read back; where temperature control is set up or MAST heats."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="HeaterOff",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "关掉 Lake Shore 335 的加热器（RANGE 0，读回确认；没读到关闭就在后台一直重试），样品台随后自然降温。"
                "分区模式下设定值还在扫时，还会停住扫温（RAMP 0），免得下一个分区把加热器重新打开。"
                "不需要仪器令牌、不过任何闸；在设置了控温的站上、以及 MAST 自己在加热时随时可用，正在跑的 "
                "SetSampleTemperature 会随之结束。没有设置控温的站上 MAST 不写这台仪器（请用面板 ALL OFF）。"
                "output 不填：初始化设定的那一路，加上 MAST 正在加热的那一路。",
                "Turn the Lake Shore 335's heater off (RANGE 0, read back; retried in the "
                "background until it reads off); the stage then cools by itself. In zone mode a "
                "ramping setpoint is stopped too (RAMP 0), so that the next zone does not turn "
                "the heater back on. Needs no "
                "instrument token and passes no gate; available wherever temperature control is "
                "set up and whenever MAST's own heating is on, and a running SetSampleTemperature "
                "ends with it. Where temperature control is not set up MAST writes nothing to the "
                "instrument (use ALL OFF at the panel). output empty: the output set at "
                "initialization, plus the one MAST is heating."),
            parameters=[ParameterSpec(name="output", type="int", required=False, default=None,
                                      allowed_values=[1, 2],
                                      description=tr("加热输出 1 或 2；不填见上", "heater output 1 or 2; "
                                                     "empty: see above"))],
            estimated_duration_s=2.0,
            composition_level=0,
            tags=["temperature", "heater", "cryostat", "stop", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "HeaterOff"
        ctl = _controller()
        if ctl is None:
            return SkillResult(skill_name=name, success=False, error=_NO_CONTROLLER,
                               data={"reason": "no_controller"})
        raw = params.get("output")
        outputs = None
        if raw is not None and str(raw).strip() in ("1", "2"):
            outputs = (int(str(raw).strip()),)
        res = ctl.heater_off(reason=name, origin=_origin(context, name), outputs=outputs)
        if res.get("confirmed"):
            return SkillResult(skill_name=name, success=True, data=res,
                               summary=tr("加热器已关（读回 RANGE 0）", "heater off (RANGE reads 0)"))
        if res.get("refused"):
            return SkillResult(skill_name=name, success=False, data=res,
                               error=str(res.get("error") or ""))
        return SkillResult(skill_name=name, success=False, data=res,
                           error=tr(f"还没读到加热器关闭：{res.get('error') or res.get('readback')}。MAST 在后台"
                                    "一直重试；请操作员在 335 面板上按 ALL OFF",
                                    f"the heater does not read off yet: {res.get('error') or res.get('readback')}. "
                                    "MAST keeps retrying in the background; ask the operator to "
                                    "press ALL OFF on the 335"))


class SetHeaterZoneTable(BaseSkill):
    """Write the heater output's zone table (commissioning, with the heater off)."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetHeaterZoneTable",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.DANGEROUS,
            description=tr(
                "写 Lake Shore 335 加热输出的分区表（ZONE，每区读回核对）。分区模式下，设定值进入哪一区，"
                "335 就自己换成那一区的 P、I、D、加热量程和扫温速率，所以分区表是调试时和操作员一起定的事，"
                "不要自己编数。只在加热器关着（RANGE 0）、没在扫温、MAST 没在加热也没在改磁场时写。\n"
                "zones：JSON 数组，第 1 区在前，1–10 区，没给的区写成不用（上界 0 K）。每区："
                "upper_bound_k（这一区的上界，开尔文，严格递增）、p（0.1–1000）、i（0.1–1000）、d（0–200 %）、"
                "range（0–3 或 off/low/medium/high，不超过初始化的最大量程）、rate_k_per_min（0.1 到初始化的"
                "最大速率）、可选 input（default 或初始化的控制输入；不换传感器）、可选 mout_pct（只能是 0）。"
                "最后一个在用的分区要覆盖到最高设定温度。例：[{\"upper_bound_k\": 20, \"p\": 50, \"i\": 20, "
                "\"d\": 0, \"range\": \"low\", \"rate_k_per_min\": 0.5}, ...]。\n"
                "表不合格就什么也不写；写入出错就把旧表写回去，并逐区报告仪器里现在是什么。不开加热器。",
                "Write the zone table of the Lake Shore 335's heater output (ZONE, each zone read "
                "back). In zone mode the 335 switches to the P, I, D, heater range and ramp rate of "
                "the zone the setpoint enters, so the table is a commissioning decision made with "
                "the operator; do not invent the numbers. Written only with the heater off (RANGE "
                "0), the setpoint not ramping, and MAST neither heating nor changing the field.\n"
                "zones: a JSON array, zone 1 first, 1 to 10 zones; zones not given are written as "
                "unused (upper bound 0 K). Each zone: upper_bound_k (the zone's upper bound in "
                "kelvin, strictly ascending), p (0.1-1000), i (0.1-1000), d (0-200 %), range (0-3 or "
                "off/low/medium/high, at most the maximum range set at initialization), "
                "rate_k_per_min (0.1 to the maximum rate set at initialization), optionally input "
                "(default, or the configured control input; sensors are never switched) and "
                "mout_pct (0 only). The last zone in use must reach the maximum setpoint. Example: "
                "[{\"upper_bound_k\": 20, \"p\": 50, \"i\": 20, \"d\": 0, \"range\": \"low\", "
                "\"rate_k_per_min\": 0.5}, ...].\nA table that does not pass writes nothing; a "
                "write that fails puts the old table back and reports, zone by zone, what the "
                "instrument now holds. It never turns the heater on."),
            parameters=[ParameterSpec(
                name="zones", type="str", required=True,
                description=tr("分区表，JSON 数组（见说明）", "the zone table, a JSON array (see the description)"))],
            estimated_duration_s=15.0,
            composition_level=0,
            tags=["temperature", "heater", "cryostat", "write", "zone", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "SetHeaterZoneTable"
        ctl = _controller()
        if ctl is None:
            return SkillResult(skill_name=name, success=False, error=_NO_CONTROLLER,
                               data={"reason": "no_controller"})
        abort = getattr(context, "check_abort", None) or (lambda: False)
        res = ctl.write_zone_table(params.get("zones"), origin=_origin(context, name),
                                   abort_check=abort)
        if res.get("written"):
            return SkillResult(skill_name=name, success=True, data=res,
                               summary=tr("分区表已经是这样，什么也没写" if res.get("noop") else
                                          "分区表已写入并逐区读回核对",
                                          "the zone table already reads like this; nothing written"
                                          if res.get("noop") else
                                          "zone table written, every zone read back"))
        return SkillResult(skill_name=name, success=False, data=res,
                           error=str(res.get("error") or tr("分区表没有写入", "the zone table was not written")))


class SetHeaterControlMode(BaseSkill):
    """Switch the heater output between closed-loop PID and zone mode (heater off)."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SetHeaterControlMode",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.DANGEROUS,
            description=tr(
                "把 Lake Shore 335 的加热输出在闭环 PID（closed_loop）和分区（zone）之间切换（OUTMODE，读回核对）。"
                "只改模式：控制输入保持初始化设定的那一路、断电后不自启。只在加热器关着（RANGE 0）、没在扫温、"
                "MAST 没在加热也没在改磁场时写；切到分区模式还要求仪器里存的分区表合格（先 SetHeaterZoneTable）。"
                "切换后加热器若读到开着就立即关掉并改回原模式。不开加热器。",
                "Switch the Lake Shore 335's heater output between closed-loop PID (closed_loop) and "
                "zone mode (zone) (OUTMODE, read back). Only the mode changes: the control input "
                "stays the one set at initialization, and the heater stays off after a power cut. "
                "Written only with the heater off (RANGE 0), the setpoint not ramping, and MAST "
                "neither heating nor changing the field; zone mode also needs the stored zone table "
                "to pass (SetHeaterZoneTable first). If the heater reads on after the switch, it "
                "is turned off at once and the old mode goes back. It never turns the heater on."),
            parameters=[ParameterSpec(
                name="mode", type="str", required=True,
                allowed_values=["closed_loop", "zone", "1", "2"],
                description=tr("closed_loop（闭环 PID，1）或 zone（分区，2）",
                               "closed_loop (closed-loop PID, 1) or zone (2)"))],
            estimated_duration_s=5.0,
            composition_level=0,
            tags=["temperature", "heater", "cryostat", "write", "zone", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "SetHeaterControlMode"
        ctl = _controller()
        if ctl is None:
            return SkillResult(skill_name=name, success=False, error=_NO_CONTROLLER,
                               data={"reason": "no_controller"})
        res = ctl.set_control_mode(params.get("mode"), origin=_origin(context, name))
        if res.get("set"):
            return SkillResult(skill_name=name, success=True, data=res,
                               summary=tr("已经是这个模式，什么也没写" if res.get("noop") else
                                          "控制模式已切换并读回核对",
                                          "already in that mode; nothing written" if res.get("noop")
                                          else "control mode switched and read back"))
        return SkillResult(skill_name=name, success=False, data=res,
                           error=str(res.get("error") or tr("控制模式没有切换", "the control mode was not changed")))


__all__ = ["CheckSampleTemperatureChange", "HeaterOff", "HoldSampleTemperature",
           "SetHeaterControlMode", "SetHeaterZoneTable", "SetSampleTemperature"]
