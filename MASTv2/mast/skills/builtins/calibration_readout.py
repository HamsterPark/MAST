"""只读展示流程正在依赖的仪器标定值。

使用 instrument_profile 已有 getter，不写配置、不发 TCP、不解除拦截。
返回完整标定内容、年龄和适用范围，使调用方能核对实际使用的值。
未标定返回 None，与已保存但字段为空明确区分。
条件数仅描述矩阵形状，不能证明方向、符号或逆变换正确。"""

from __future__ import annotations

import logging
import time
from typing import Any

from mast.i18n import tr
from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.skills.base import BaseSkill

logger = logging.getLogger(__name__)

#: 超过它 ``set_tilt_calibration`` 会**拒绝写入**。这里只是复述,不是第二份真源。
_COND_REFUSE_ABOVE = 10.0


def _age_note(updated_at: Any) -> dict[str, Any]:
    """标定的年龄。读不到时间戳是**一句话**,不是 0。

    「没有时间戳」和「刚刚标定的」在报文里绝不能长得一样 —— 后者是结论,
    前者是「这个问题答不了」。
    """
    try:
        ts = float(updated_at)
    except (TypeError, ValueError):
        return {"updated_at": None, "age_s": None,
                "age_note": tr("**这条标定没有时间戳** —— 无法判断它是什么时候做的。", "**This calibration has no timestamp** - there is no way to tell when it was made.")}
    if ts <= 0:
        return {"updated_at": None, "age_s": None,
                "age_note": tr("**这条标定没有时间戳** —— 无法判断它是什么时候做的。", "**This calibration has no timestamp** - there is no way to tell when it was made.")}
    age = max(0.0, time.time() - ts)
    days = age / 86400.0
    if days >= 1.0:
        human = tr(f"{days:.1f} 天前", f"{days:.1f} days ago")
    elif age >= 3600:
        human = tr(f"{age / 3600.0:.1f} 小时前", f"{age / 3600.0:.1f} hours ago")
    else:
        human = tr(f"{age / 60.0:.0f} 分钟前", f"{age / 60.0:.0f} minutes ago")
    return {
        "updated_at": ts,
        "age_s": age,
        "age_note": (
            tr(f"标定于 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}"
            f"({human})。",
            f"Calibrated at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))} ({human}).")
            + (tr("**换过样品/托架之后这条就失效了** —— 倾斜响应是样品与托架的属性。", "**This calibration is void after the sample/holder is changed** - the tilt response is a property of the sample and holder.")
               if days >= 1.0 else "")
        ),
    }


def _cond_note(cond: Any) -> str:
    """同时说明条件数及其不能证明的性质。
    
    响应矩阵描述斜率对倾斜的变化，控制更新还需要正确的符号与逆变换。
    条件数接近 1 不保证控制方向正确；不能仅据此声明标定良好。"""
    try:
        c = float(cond)
    except (TypeError, ValueError):
        return (tr("条件数读不到 —— **这不等于标定良好**,是这一项答不了。", "The condition number cannot be read - **this does not mean well calibrated**, this item simply cannot answer."))
    verdict = (tr("形状健全", "shape is sound") if c <= _COND_REFUSE_ABOVE
               else tr(f"**形状可疑**(超过拒绝写入线 {_COND_REFUSE_ABOVE:g})", f"**shape is suspect** (exceeds the write-refusal line {_COND_REFUSE_ABOVE:g})"))
    return (
        tr(f"条件数 {c:.4g} —— {verdict}。"
        "⚠️ **条件数只管形状,不管方向**:它说的是两个轴的响应有没有几乎共线,"
        "**对符号与求逆一无所知**。曾出现条件数很漂亮(接近 1)而 "
        "AutoTilt 仍然发散,根因是用了 M 而不是 −M⁻¹。"
        "**别把一个好看的条件数读成「标定良好」。**",
        f"Condition number {c:.4g} - {verdict}. WARNING: **the condition number only covers shape, not direction**: it says whether the responses of the two axes are nearly collinear, and **knows nothing about sign or inversion**. A condition number close to 1 (very pretty) has been seen with AutoTilt still diverging, the root cause being the use of M rather than −M⁻¹. **Do not read a nice-looking condition number as 'well calibrated'.**")
    )


def _tilt_block() -> dict[str, Any]:
    """倾斜响应标定。没标定过 ⇒ ``available=False`` + 一句为什么,**不是空对象**。"""
    try:
        from mast.core.instrument_profile import get_tilt_calibration
        cal = get_tilt_calibration()
    except Exception as exc:  # noqa: BLE001 — 只读窗口不许自己炸
        return {"available": False, "why": tr(f"读不到仪器档案: {exc}", f"Cannot read the instrument profile: {exc}")}
    if cal is None:
        return {
            "available": False,
            # ⚠️ 「没标定过」是一个**结论**,不是「读失败」。两者必须分开 ——
            # 合成一句会让「该去标定」和「该去查为什么读不到」变成同一件事。
            "why": (tr("**从未标定过倾斜响应**(不是读取失败)。"
                    "AutoTilt 因此一律跳过 —— 它绝不带着猜来的符号去动硬件。"
                    "要标定:运行 TiltCalibrate。",
                    "**The tilt response has never been calibrated** (not a read failure). AutoTilt is therefore always skipped - it never moves the hardware with a guessed sign. To calibrate: run TiltCalibrate.")),
        }
    out: dict[str, Any] = {"available": True, "why": "", "g": cal.get("g"),
                           "cond": cal.get("cond")}
    out.update(_age_note(cal.get("updated_at")))
    out["cond_note"] = _cond_note(cal.get("cond"))
    # ⚠️ 2026-08-11 更正:这句话在 08-10 修完之后就变成了假话,而它正是写来防
    # 那次事故的 —— 它说存的是 M(=∂斜率/∂倾斜),而 `auto_tilt.py:286-289` 从那次
    # 起存的已经是 **G = −M⁻¹**。任何一个信了旧措辞、自己再做一次 −M⁻¹ 的消费方
    # 会**二次求逆**,把 08-10 那次发散原样请回来。
    #
    # **修好之后旧的理由会静静变成假话,而它比没有理由更危险:下一个人会信它。**
    out["convention_note"] = (
        tr("存的是 **G = −M⁻¹**(按行的 2×2),其中 M = ∂斜率/∂倾斜。"
        "**直接用**:要抵消测到的斜率 s,施加 `Δtilt = G·s` —— 不要再求逆、不要再取负。"
        "(曾有一次发散正是「存的是 M、需要的是 −M⁻¹」;修的是存储端,"
        "而这句说明是事后才跟上的。)",
        "What is stored is **G = −M⁻¹** (row-wise 2x2), where M = ∂slope/∂tilt. **Use it directly**: to cancel a measured slope s, apply `Δtilt = G·s` - do not invert again and do not negate again. (One past divergence was exactly 'M stored, −M⁻¹ needed'; the fix was on the storage side, and this note only caught up afterwards.)")
    )
    return out


def _didv_block() -> dict[str, Any]:
    """接触点 dI/dV 标定(进针学到的)。空 dict = 从未标定。"""
    try:
        from mast.core.instrument_profile import get_calibration
        cal = get_calibration() or {}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "why": tr(f"读不到仪器档案: {exc}", f"Cannot read the instrument profile: {exc}")}
    if not cal.get("didv_at_contact_v"):
        return {"available": False,
                "why": tr("**从未学到过接触点 dI/dV**(不是读取失败)。一次成功进针会写入它。", "**The contact-point dI/dV has never been learned** (not a read failure). A successful engage writes it.")}
    out: dict[str, Any] = {
        "available": True, "why": "",
        "didv_at_contact_v": cal.get("didv_at_contact_v"),
        # 条件绑定:一个 dI/dV 值离开它的偏压/设定点就没有意义。
        "measured_at_bias_v": cal.get("didv_cal_bias_v"),
        "measured_at_setpoint_a": cal.get("didv_cal_setpoint_a"),
        "mod_amp_v": cal.get("didv_cal_mod_amp_v"),
    }
    out.update(_age_note(cal.get("didv_cal_updated_at")))
    out["condition_note"] = (
        tr("这个值**绑定在它被测出来的那个偏压/设定点上** —— 换了条件就不可比。", "This value is **bound to the bias/setpoint at which it was measured** - it is not comparable once the conditions change.")
    )
    return out


def _qplus_block() -> dict[str, Any]:
    """qPlus 实测共振(``AcquirePLLFreqSweep`` 写入)。

    刻意与针尖行上的**标称** ``qplus_f0_hz`` / ``qplus_q`` 分开报:
    标称是铭牌值跟着针尖走,实测是当前装机这一支扫出来的真值,**换针即失效**。
    合成一个数会立刻产生「这个数是谁的、什么时候的」的二义。
    """
    try:
        from mast.core.instrument_profile import get_config
        f0 = get_config("qplus_f0_measured_hz", None)
        q = get_config("qplus_q_measured", None)
        ts = get_config("qplus_fq_updated_at", None)
        nominal_f0 = get_config("qplus_f0_hz", None)
        nominal_q = get_config("qplus_q", None)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "why": tr(f"读不到仪器档案: {exc}", f"Cannot read the instrument profile: {exc}")}
    if f0 is None and q is None:
        return {
            "available": False,
            "why": (tr("**从未实测过 qPlus 共振**(不是读取失败)。"
                    "运行 AcquirePLLFreqSweep 会写入它。",
                    "**The qPlus resonance has never been measured** (not a read failure). Running AcquirePLLFreqSweep writes it.")),
            "nominal_f0_hz": nominal_f0, "nominal_q": nominal_q,
            "nominal_note": tr("上面两个是**铭牌/标称**值,跟着针尖走,不是这一支的实测。", "The two above are **nameplate/nominal** values that follow the tip, not measurements of this one."),
        }
    out: dict[str, Any] = {
        "available": True, "why": "",
        "f0_measured_hz": f0, "q_measured": q,
        "nominal_f0_hz": nominal_f0, "nominal_q": nominal_q,
        "nominal_note": tr("标称值跟着针尖走;实测值**换针即失效**。两者不可互相替代。", "Nominal values follow the tip; measured values are **void after a tip change**. The two cannot substitute for each other."),
    }
    out.update(_age_note(ts))
    try:
        # ring-down 时间常数 τ = Q/(π f₀)。给出来是因为「等振幅回到基线」的
        # 超时该按它定,而 Q 在这台机器上跨了 40 倍(5e3 ~ 2e5),
        # **所以固定时长是错的** —— 这一句就是为了让读的人别去写一个固定值。
        if f0 and q:
            tau = float(q) / (3.141592653589793 * float(f0))
            out["ring_down_tau_s"] = tau
            out["ring_down_note"] = (
                tr(f"τ = Q/(π·f₀) ≈ {tau:.3g} s。**别据此写一个固定等待时长** —— "
                "Q 随针尖状态变化很大(τ 可差一个数量级以上),要等就等**振幅回到基线**(带超时)。",
                f"τ = Q/(π·f₀) ≈ {tau:.3g} s. **Do not hard-code a fixed wait based on this** - Q varies widely with the tip state (τ can differ by more than an order of magnitude); if you must wait, wait for **the amplitude to return to baseline** (with a timeout).")
            )
    except Exception:  # noqa: BLE001
        pass
    return out


class ReadCalibrations(BaseSkill):
    """只读:仪器档案里那些**会驱动硬件的标定值**,连同它们的年龄与适用边界。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ReadCalibrations",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("读取仪器档案里的标定值:①倾斜响应矩阵 G(AutoTilt 依赖它)"
                "②接触点 dI/dV ③qPlus 实测共振 f₀/Q。"
                "每一项都带**年龄**与**适用边界**。**纯读,不动任何硬件。**\n"
                "\n"
                "⚠️ **被「未标定」挡住时先调本工具**——它会告诉你到底是"
                "「从未标定过」还是「读不到档案」,这两件事的处置完全不同。\n"
                "\n"
                "⚠️ **条件数只管形状不管方向**:一个好看的条件数**不等于**标定可用 "
                "(曾见 cond≈1.1 而 AutoTilt 仍发散,根因是符号/求逆)。",
                "Read the calibration values in the instrument profile: (1) the tilt response matrix G (AutoTilt depends on it), (2) the contact-point dI/dV, (3) the measured qPlus resonance f₀/Q. Every item carries its **age** and **applicability bounds**. **Pure read, moves no hardware.**\n\nWARNING: **when blocked by 'not calibrated', call this tool first** - it tells you whether it is 'never calibrated' or 'profile unreadable'; the two call for entirely different handling.\n\nWARNING: **the condition number only covers shape, not direction**: a nice-looking condition number does **not** mean the calibration is usable (seen once: cond≈1.1 and AutoTilt still diverged, the root cause being sign/inversion).")
            ),
            parameters=[
                ParameterSpec(
                    name="which",
                    type="str",
                    description=(tr("只看某一项:tilt / didv / qplus。留空返回全部。", "Look at only one item: tilt / didv / qplus. Empty returns all.")),
                    required=False,
                    default="",
                ),
            ],
            estimated_duration_s=0.02,
            composition_level=0,
            tags=["read", "calibration", "diagnostic", "instrument_profile"],
        )

    def execute(self, context, params: dict) -> SkillResult:  # noqa: ARG002
        which = str(params.get("which") or "").strip().lower()
        blocks = {"tilt": _tilt_block, "didv": _didv_block, "qplus": _qplus_block}
        wanted = [which] if which in blocks else list(blocks)

        data: dict[str, Any] = {}
        for key in wanted:
            try:
                data[key] = blocks[key]()
            except Exception as exc:  # noqa: BLE001 — 一项读不到不影响其余
                data[key] = {"available": False, "why": tr(f"读取失败: {exc}", f"Read failed: {exc}")}

        have = [k for k in wanted if (data.get(k) or {}).get("available")]
        missing = [k for k in wanted if k not in have]
        if not have:
            summary = (tr("这些标定**一个都没有**:", "**None** of these calibrations exist:") + "、".join(missing)
                       + tr("(已确认读到档案,不是读取失败 —— 逐项 why 里写了原因)。", "(confirmed that the profile was read, not a read failure - the reason for each item is in its why)."))
        else:
            summary = tr("已标定:", "Calibrated:") + "、".join(have)
            if missing:
                summary += tr(";**未标定**:", "; **not calibrated**:") + "、".join(missing)
            tilt = data.get("tilt") or {}
            if tilt.get("available"):
                summary += tr(f"。倾斜:{tilt.get('age_note') or ''}", f". Tilt: {tilt.get('age_note') or ''}")
        data["read_at"] = time.time()
        return SkillResult(skill_name="ReadCalibrations", success=True,
                           data=data, summary=summary)
