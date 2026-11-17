"""Change the superconducting magnet's field (Lake Shore 625): check, ramp, stop.

Field control arrived in 7.2.0. Until then MAST only read the magnet supply
(``cryostat.GetMagnetState``). Everything that decides whether a change may happen
lives in ``mast.instruments.magnet_625``; these skills only carry the request there
and report back. That module's header lists every gate.

* ``CheckMagnetFieldChange``: a dry run. It runs every gate and returns each
  verdict and the expected duration. It writes nothing.
* ``RampMagnetField``: sends the new set point (``SETI``) if every gate passes, then
  follows the ramp until it arrives or must stop. It holds the instrument token for
  the whole ramp, so no scan or other instrument command runs at the same time.
  Raising ``|B|`` needs ``user_authorization``, the user's own words; this is the
  operator's condition, because the helium level cannot be read.
* ``StopMagnetRamp``: ``STOP`` (freeze the output where it is). It needs no token
  and passes no gate, so it runs while a ramp holds the token.

The design rules behind the gates:
* external agents may change the field;
* the maximum field |B| is entered at instrument initialization;
* a negative target (reverse field) is allowed, but a ramp never crosses zero (ramp to 0
  first, then start a second ramp), the user's words must name the negative field, and
  until the operator has verified that the ramp segments act on ``|I|`` on the negative
  side, a negative target may not go beyond the first segment's upper current;
* raising needs the user's verbal authorization;
* raising needs the magnet temperature below a limit entered at initialization;
* the tip is withdrawn by default, and an external agent may waive that.

Heating and field changes never happen at the same
time. No field change while a Lake Shore 335 heater output reads on or MAST's own heating
is on (``HeaterOff`` first); with no 335 here the gate passes and says so.
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

_NO_MAGNET = tr(
    "这台机器上没有识别到 Lake Shore 625 磁体电源（或识别到不止一台），MAST 不改变磁场。"
    "请用户在 设置→硬件 里「扫描设备接口」。",
    "No Lake Shore 625 magnet supply is recognised on this machine (or more than one is), "
    "so MAST does not change the field. Ask the user to run Settings -> Hardware -> Scan "
    "device ports.")

_TIP_PARAM = ParameterSpec(
    name="require_tip_withdrawn", type="bool", required=False, default=True,
    description=tr(
        "默认 true：针尖必须已退（Z 停在收回端、反馈关）才改变磁场。只有确需带针改变磁场时才传 "
        "false（外部 agent 可以这样做，后果自负：磁场变化会让针尖横向、纵向漂移）。",
        "Default true: the tip must be withdrawn (Z at the retracted end, feedback off) "
        "before the field changes. Pass false only when the change must be made with the "
        "tip in (an external agent may; a changing field drifts the tip laterally and "
        "vertically)."))

_AUTH_PARAM = ParameterSpec(
    name="user_authorization", type="str", required=False, default="",
    description=tr(
        "升场（|B| 变大）时必填：用户授权的原话，例如「液面够，可以升到 1 T」（照抄这句示例会被拒绝）。"
        "液面计读不到，这是操作员定的条件 —— 没有用户的话就不要填，也不要自己编。降场和停止不需要。"
        "目标是反向场（负）时，原话里必须明确写出负向 / 反向的磁场，如「-2 T」「负2T」「反向2T」"
        "「reverse 2 T」；只说了正的磁场不算授权反向场，只说了反向场也不算授权正向的目标。"
        "话里有没贴着数字的「反向 / reverse / minus」时，正向目标按符号不明被拒，要写成「+2 T」或「-2 T」。",
        "Required when raising |B|: the user's own words authorizing it, e.g. \"helium "
        "is fine, go to 1 T\" (this example copied verbatim is refused). The level meter "
        "cannot be read; this is the operator's condition. Without the user's words leave it "
        "empty; never write it yourself. Lowering and stopping do not need it. For a reverse "
        "(negative) target the words must name a negative or reverse field explicitly, e.g. "
        "\"-2 T\", \"negative 2 T\", \"reverse 2 T\"; words that name only a positive field do "
        "not authorize it, and words that name only a reverse field do not authorize a "
        "positive target. A sign word that touches no number (\"reverse\", \"minus\") makes a positive "
        "target unclear and refused unless the field is written \"+2 T\" or \"-2 T\"."))

_AUTH_FIELD_PARAM = ParameterSpec(
    name="authorized_up_to_t", type="float", unit="T", required=False, default=None,
    description=tr(
        "升场时必填：用户授权到的磁场（特斯拉，取绝对值，反向场也填正数），就是用户授权时说出的那个数。"
        "目标的 |B| 超过它就拒绝；原话里提到了同一侧的磁场时，两者必须一致。授权只覆盖到这个值，"
        "再往上要重新问用户。",
        "Required when raising: the field (tesla, a magnitude: a reverse field is given as a "
        "positive number) the user authorized, the number in their words. A target whose |B| "
        "is above it is refused, and when the words name a field on the target's side of zero "
        "the two must agree. The authorization covers that far; going higher means asking the "
        "user again."))


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
    from mast.core import magnet_control

    return magnet_control.controller()


class CheckMagnetFieldChange(BaseSkill):
    """Dry run of a field change: every gate's verdict and the expected duration."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CheckMagnetFieldChange",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "预检：把超导磁体（Lake Shore 625）的磁场改到 target_t（特斯拉）之前，逐项给出每道闸的"
                "判决（初始化设定、上限、场常数核对、错误位、分段扫场、不跨零、负向分段是否已验证、样品台加热器关着、"
                "针尖、升场时的用户授权与磁体温度）和预计用时。负的 target_t 是反向场。**不写仪器。**先跑它，"
                "再决定要不要调 RampMagnetField、要向用户要什么。",
                "Dry run before changing the superconducting magnet's field (Lake Shore 625) to "
                "target_t (tesla; negative = reverse field): every gate's verdict (initialization "
                "settings, limit, field-constant cross-check, error bits, segment ramping, no "
                "crossing of zero, negative-side segments verified, the sample-stage heater off, "
                "tip, and for a raise the user's authorization and the magnet temperature) and "
                "the expected duration. **Writes nothing.** Run it first, then decide whether to "
                "call RampMagnetField and what to ask the user for."),
            parameters=[
                ParameterSpec(name="target_t", type="float", unit="T", required=True,
                              description=tr("目标磁场，特斯拉；负值是反向场，|B| 不超过初始化里设定的上限",
                                             "target field in tesla; negative is a reverse field, "
                                             "|B| up to the limit set at initialization")),
                _AUTH_PARAM,
                _AUTH_FIELD_PARAM,
                _TIP_PARAM,
            ],
            estimated_duration_s=5.0,
            composition_level=0,
            tags=["magnet", "field", "cryostat", "read", "check", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        ctl = _controller()
        if ctl is None:
            return SkillResult(skill_name="CheckMagnetFieldChange", success=False,
                               error=_NO_MAGNET, data={"reason": "no_magnet"})
        plan = ctl.plan(params.get("target_t"),
                        user_authorization=str(params.get("user_authorization") or ""),
                        authorized_up_to_t=params.get("authorized_up_to_t"),
                        require_tip_withdrawn=_bool(params.get("require_tip_withdrawn"), True),
                        tip_verdict=_tip_verdict(context))
        data = {"would_proceed": plan.ok, **plan.as_dict(), "control": ctl.status()}
        summary = (tr("所有闸都通过", "every gate passes") if plan.ok else
                   tr("会被拒绝：", "would be refused: ") + plan.refusal_text())
        # The dry run itself succeeded; whether the change would go ahead is in the data.
        return SkillResult(skill_name="CheckMagnetFieldChange", success=True, data=data,
                           summary=summary)


class RampMagnetField(BaseSkill):
    """Ramp the superconducting magnet to a target field, following the ramp to the end."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="RampMagnetField",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.DANGEROUS,
            description=tr(
                "把超导磁体（Lake Shore 625）的磁场改到 target_t（特斯拉；负值是反向场，|B| 不超过"
                "初始化里设定的上限）。"
                "全部闸门通过才发设定值（SETI），速率由仪器自己的分段程序决定；然后一直跟到到位"
                "（或必须停下），整个过程持有仪器令牌——0→5 T 约需一小时。\n"
                "**升场（|B| 变大）前必须先问用户**，把用户授权的原话填进 user_authorization、"
                "用户授权到的磁场（绝对值）填进 authorized_up_to_t；没有用户的话就不要升场。降场不需要授权。"
                "**反向场**：用户的话里必须明确写出负向 / 反向的磁场（如「-2 T」「反向2T」）；"
                "扫场**不跨零**——要从正到负（或反过来）先扫到 0，再另起一次；"
                "初始化里「负向扫场分段已验证」没打开时，负向目标的 |I| 不得超过仪器第一段分段的上限"
                "（被拒时去问操作员，不要自己绕）。"
                "默认要求针尖已退（先 WithdrawTip）。加热和改磁场不同时进行：Lake Shore 335 的加热器开着或"
                "MAST 在加热时一律拒绝，先 HeaterOff。\n"
                "随时可以用 StopMagnetRamp 停住（冻结在当前值，**不是回零**）；取消这个作业也会发 STOP。"
                "不确定能不能做时先跑 CheckMagnetFieldChange。",
                "Change the superconducting magnet's field (Lake Shore 625) to target_t (tesla; "
                "negative is a reverse field, |B| up to the limit set at initialization). The set "
                "point (SETI) is sent only if every gate "
                "passes, and the ramp speed is the instrument's own segment program; the skill "
                "then follows the ramp until it arrives (or must stop), holding the instrument "
                "token throughout. 0->5 T takes about an hour.\n**Before raising |B|, ask the "
                "user**, put their own words in user_authorization and the field they "
                "authorized (a magnitude) in authorized_up_to_t; without them, do not raise. "
                "Lowering needs no authorization. **Reverse field**: the user's words must name "
                "the negative or reverse field explicitly (e.g. \"-2 T\", \"reverse 2 T\"); a "
                "ramp **never crosses zero** (to go from positive to negative, or back, ramp to 0 "
                "first, then start a second ramp); and until \"Negative-side ramp segments "
                "verified\" is on in the initialization, the |I| of a negative target may not "
                "exceed the upper current of the instrument's first ramp segment (when refused, "
                "ask the operator; do not work around it). "
                "By default the tip must be withdrawn first (WithdrawTip). "
                "Heating and field changes never overlap: refused while a Lake Shore 335 heater "
                "is on or MAST is heating; turn it off first (HeaterOff).\n"
                "StopMagnetRamp stops it at any time (freezes the present "
                "value; it does **not** go to zero); cancelling this job sends STOP too. When "
                "unsure, run CheckMagnetFieldChange first."),
            parameters=[
                ParameterSpec(name="target_t", type="float", unit="T", required=True,
                              description=tr("目标磁场，特斯拉；负值是反向场；|B| 的上限是初始化里设定的值",
                                             "target field in tesla; negative is a reverse field; "
                                             "the limit on |B| is the one set at initialization")),
                _AUTH_PARAM,
                _AUTH_FIELD_PARAM,
                _TIP_PARAM,
            ],
            estimated_duration_s=3600.0,
            composition_level=0,
            tags=["magnet", "field", "cryostat", "write", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "RampMagnetField"
        ctl = _controller()
        if ctl is None:
            return SkillResult(skill_name=name, success=False, error=_NO_MAGNET,
                               data={"reason": "no_magnet"})
        abort = getattr(context, "check_abort", None) or (lambda: False)
        started = ctl.start(params.get("target_t"), origin=_origin(context, name),
                            user_authorization=str(params.get("user_authorization") or ""),
                            authorized_up_to_t=params.get("authorized_up_to_t"),
                            require_tip_withdrawn=_bool(params.get("require_tip_withdrawn"), True),
                            tip_verdict=_tip_verdict(context), abort_check=abort)
        if not started.get("started"):
            if started.get("noop"):
                return SkillResult(skill_name=name, success=True, data=started,
                                   summary=tr("已经在目标磁场上，没有发任何命令",
                                              "already at the target field; nothing sent"))
            return SkillResult(skill_name=name, success=False, data=started,
                               error=str(started.get("error") or tr("没有开始扫场",
                                                                     "the ramp did not start")))
        ended = ctl.supervise(abort_check=abort)
        data = {"plan": started.get("plan"), **ended}
        if ended.get("outcome") == "done":
            return SkillResult(skill_name=name, success=True, data=data,
                               summary=tr(f"已到位：{started['plan'].get('target_t')} T",
                                          f"arrived at {started['plan'].get('target_t')} T"))
        return SkillResult(skill_name=name, success=False, data=data,
                           error=f"[{ended.get('outcome')}] {ended.get('detail')}")


class StopMagnetRamp(BaseSkill):
    """STOP: freeze the magnet output at its present value. Always allowed."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="StopMagnetRamp",
            version="1.0.0",
            category=SkillCategory.WRITE,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "停住超导磁体的扫场：给 Lake Shore 625 发 STOP，输出冻结在当前值（2 秒内）。"
                "**不是回零**——要降场请用 RampMagnetField(target_t=…)。不需要仪器令牌、不过任何闸，"
                "正在跑的 RampMagnetField 会随之结束。仪器没在扫场时发它也无害。",
                "Stop the superconducting magnet's ramp: send STOP to the Lake Shore 625; the "
                "output freezes at its present value (within 2 s). It does **not** go to zero; "
                "to lower the field use RampMagnetField(target_t=...). Needs no instrument "
                "token and passes no gate; a running RampMagnetField ends with it. Harmless "
                "when the supply is not ramping."),
            parameters=[],
            estimated_duration_s=1.0,
            composition_level=0,
            tags=["magnet", "cryostat", "stop", "optional-hardware"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "StopMagnetRamp"
        ctl = _controller()
        if ctl is None:
            return SkillResult(skill_name=name, success=False, error=_NO_MAGNET,
                               data={"reason": "no_magnet"})
        res = ctl.stop(reason="StopMagnetRamp", origin=_origin(context, name))
        if res.get("sent"):
            return SkillResult(skill_name=name, success=True, data=res,
                               summary=tr("已发 STOP：输出冻结在当前值",
                                          "STOP sent: output frozen at its present value"))
        return SkillResult(skill_name=name, success=False, data=res,
                           error=tr(f"STOP 没发出去：{res.get('error')}。请操作员在 625 面板上按 "
                                    "Stop Output",
                                    f"STOP was not sent: {res.get('error')}. Ask the operator to "
                                    "press Stop Output on the 625 front panel"))


__all__ = ["CheckMagnetFieldChange", "RampMagnetField", "StopMagnetRamp"]
