"""压电范围对账 —— **配置说的和仪器说的是不是同一个数**。

## 为什么需要它

``config.SafetyLimits.xy_max_m`` 出厂 **1.5 µm**,是一个**写死的常数,从不与
仪器核对**。而扫描器的实际 XY 半程可能更小
(``GetPiezoConfig`` 的 ``range``,即标定系数 × ±10 V)。

配置比实际大时,后果会一路传下去:

    xy_max_m
      → AnalysisConfig.piezo_half_range_m
      → 选点器提议的 Y 越过真实限位
      → 针尖夹在限位上,MoveToXY 报「超时」
      → 外环 optional=False ⇒ 中止,已做的修针工作全丢

另一半原因:**下液氦之后没有重置扫描框范围** ——
换温度、换扫描器、改压电标定都会动这个数,而**没有任何东西会去看一眼**。

## 这个技能只报数,不改任何设置

改压电范围是会影响全仪器的动作;这里只负责**让两边的数字见面**。
要改,人自己去 Nanonis 里改,或者调 ``SetPiezoRange``。

## 三态

  ``ok``        两边一致(相对差 ≤ tolerance_frac)
  ``mismatch``  两边都读到了,但不一致 —— 报出两个数和差多少
  ``unknown``   **仪器那边读不到**(模块不可用/断链)⇒ 判不了,
                **不是**「一致」。这是一类常见的缺陷。
"""

from __future__ import annotations

import logging

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.skills.base import BaseSkill

logger = logging.getLogger(__name__)

#: 相对差超过它就算不一致。0.02 = 2% —— 比读数抖动大得多,
#: 比一次真实失配的量级小得多。
DEFAULT_TOLERANCE_FRAC = 0.02


class CheckPiezoRange(BaseSkill):
    """Compare the configured XY half-range against what the scanner reports."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CheckPiezoRange",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("读扫描器真实的 XY/Z 压电量程，并与配置里的安全限值（SafetyLimits.xy_max_m）"
                "比对。报 ok / mismatch / unknown —— 从不改变任何东西。换过制冷剂、"
                "换过扫描器、或做过任何一次压电重标定之后都跑一下：配置里的量程比真实量程大，会让选点器提出针尖根本到不了的目标，"
                "接着那次移动会以一个重试也修不好的超时告终。",
                   "Read the scanner's real XY/Z piezo range and compare it with the configured safety limit (SafetyLimits.xy_max_m). "
                   "Reports ok / mismatch / unknown - never changes anything. Run it after changing the coolant, "
                   "changing the scanner, or any piezo recalibration: a configured range larger than the real one makes the point picker propose targets the tip can never reach, "
                   "and the move then ends in a timeout that no retry can fix.")
            ),
            parameters=[
                ParameterSpec(
                    name="tolerance_frac",
                    type="float",
                    description=(
                        tr("相对差超过它就报 mismatch。出厂 0.02(2%) —— 比读数抖动"
                        "大得多,比一次真实失配的量级小得多。",
                           "A relative difference above this is reported as mismatch. Factory 0.02 (2%) - much larger than the reading jitter, "
                           "much smaller than a real mismatch.")
                    ),
                    required=False,
                    default=DEFAULT_TOLERANCE_FRAC,
                    min_value=0.0,
                    max_value=1.0,
                ),
            ],
            tags=["piezo", "range", "safety", "read", "selfcheck"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        tol = float(params.get("tolerance_frac", DEFAULT_TOLERANCE_FRAC))
        calls: list = []

        # ── 仪器那边 ────────────────────────────────────────────────────
        rec = context.safe_call("Piezo_RangeGet")
        calls.append(rec)
        inst_half_x = inst_half_y = inst_half_z = None
        if not rec.error:
            # 复用解码器读取协议信封中 parsed 列表的数值。
            # 仅扫描顶层会漏掉范围数据，使自检无法回答问题。
            from mast.skills.builtins.readback import _decode_nanonis

            decoded = _decode_nanonis(getattr(rec, "return_value", None))
            vals = [float(v) for v in (decoded or ())
                    if isinstance(v, (int, float))] if isinstance(
                        decoded, (list, tuple)) else []
            # Piezo_RangeGet 回 (x_range, y_range, z_range) —— **全程**,不是半程。
            if len(vals) >= 2:
                inst_half_x = abs(float(vals[0])) / 2.0
                inst_half_y = abs(float(vals[1])) / 2.0
            if len(vals) >= 3:
                inst_half_z = abs(float(vals[2])) / 2.0

        # ── 配置那边 ────────────────────────────────────────────────────
        cfg_half = None
        cfg_err = ""
        # 配置侧通过 core.safety._get_effective_limits 获取配置、管理员覆盖与硬件收紧后的有效值。
        # 直接读取类默认值会绕过覆盖，使核对对象偏离实际生效值。
        try:
            from mast.config import SafetyLimits
            from mast.core.safety import _get_effective_limits

            # 与 ``api/safety_view.py:80`` / ``core/runtime.py:61`` **同一个用法**:
            # 类默认值 → 合并管理员覆写 → 按仪器事实收紧。
            # 直接读 ``SafetyLimits().xy_max_m`` 会**绕过覆写**,报出来的数就不是
            # 实际生效的那个 —— 一个对账工具报错数字比不对账更坏。
            lim = _get_effective_limits(SafetyLimits())
            v = getattr(lim, "xy_max_m", None)
            if v:
                cfg_half = abs(float(v))
            else:
                cfg_err = tr("生效的 SafetyLimits 里没有 xy_max_m", "xy_max_m is missing from the effective SafetyLimits")
        except Exception as exc:  # noqa: BLE001
            cfg_err = f"{type(exc).__name__}: {exc}"
            logger.debug("CheckPiezoRange: 读不到生效的 SafetyLimits: %s", exc)

        data = {
            "instrument_half_x_m": inst_half_x,
            "instrument_half_y_m": inst_half_y,
            "instrument_half_z_m": inst_half_z,
            "configured_half_xy_m": cfg_half,
            "tolerance_frac": tol,
        }

        # ── 三态 ────────────────────────────────────────────────────────
        #
        # 仪器读不到 ⇒ **判不了**。折成「一致」会让这个自检变成一句永远为真的
        # 安慰话 —— 那正是它要防的东西。
        if inst_half_x is None or inst_half_y is None:
            return SkillResult(
                skill_name="CheckPiezoRange", success=True,
                data={**data, "verdict": "unknown",
                      "undecidable": (tr("读不到扫描器的压电范围"
                                      f"(Piezo_RangeGet: {rec.error or '返回值里没有数字'})"
                                      " —— **判不了,不是「一致」**。",
                                         "cannot read the scanner's piezo range "
                                         f"(Piezo_RangeGet: {rec.error or 'no number in the return value'})"
                                         " - **cannot decide, this is not 'consistent'**."))},
                summary=tr("压电范围对账:**判不了** —— 仪器那边读不到。",
                           "Piezo range reconciliation: **cannot decide** - the instrument could not be read."),
                nanonis_calls=calls)
        if cfg_half is None:
            return SkillResult(
                skill_name="CheckPiezoRange", success=True,
                data={**data, "verdict": "unknown",
                      "undecidable": (tr("读不到 SafetyLimits.xy_max_m", "cannot read SafetyLimits.xy_max_m")
                                      + (f"({cfg_err})" if cfg_err else "")
                                      + tr(" —— **判不了**。", " - **cannot decide**."))},
                summary=tr("压电范围对账:**判不了** —— 读不到配置里的 xy_max_m。",
                           "Piezo range reconciliation: **cannot decide** - xy_max_m in the config could not be read."),
                nanonis_calls=calls)

        inst_half = min(inst_half_x, inst_half_y)   # 两轴取小的,包络要保守
        rel = abs(cfg_half - inst_half) / max(inst_half, 1e-15)
        data["min_instrument_half_m"] = inst_half
        data["relative_difference"] = rel
        data["configured_exceeds_instrument"] = bool(cfg_half > inst_half)

        if rel <= tol:
            return SkillResult(
                skill_name="CheckPiezoRange", success=True,
                data={**data, "verdict": "ok"},
                summary=(tr(f"压电范围对账 **一致**:仪器半程 {inst_half * 1e9:.1f} nm,"
                         f"配置 {cfg_half * 1e9:.1f} nm(差 {rel * 100:.1f}%)。",
                            f"Piezo range reconciliation **consistent**: instrument half-range {inst_half * 1e9:.1f} nm, "
                            f"config {cfg_half * 1e9:.1f} nm (difference {rel * 100:.1f}%).")),
                nanonis_calls=calls)

        worse = cfg_half > inst_half
        return SkillResult(
            skill_name="CheckPiezoRange", success=True,
            data={**data, "verdict": "mismatch"},
            summary=(
                tr(f"压电范围 **对不上**:仪器半程 {inst_half * 1e9:.1f} nm"
                f"(X {inst_half_x * 1e9:.1f} / Y {inst_half_y * 1e9:.1f}),"
                f"而配置 xy_max_m = {cfg_half * 1e9:.1f} nm —— 差 {rel * 100:.1f}%。",
                   f"Piezo range **mismatch**: instrument half-range {inst_half * 1e9:.1f} nm "
                   f"(X {inst_half_x * 1e9:.1f} / Y {inst_half_y * 1e9:.1f}), "
                   f"while config xy_max_m = {cfg_half * 1e9:.1f} nm - difference {rel * 100:.1f}%.")
                + (tr(" **配置比仪器大**:选点器会提议针尖到不了的目标,"
                   "移动会以「超时」失败,而重试没有用 —— 换温度/换扫描器/改压电"
                   "标定之后最常见的就是这一种。请把 xy_max_m 改成 "
                   f"≤ {inst_half * 1e9:.0f} nm,或在 Nanonis 里把压电范围调回去。",
                      " **The config is larger than the instrument**: the point picker will propose targets the tip cannot reach, "
                      "the move will fail with a 'timeout', and retrying does not help - this is the most common case after changing temperature / scanner / piezo "
                      "calibration. Change xy_max_m to "
                      f"≤ {inst_half * 1e9:.0f} nm, or set the piezo range back in Nanonis.")
                   if worse else
                   tr(" 配置比仪器小 —— 不会撞限位,但会白白浪费可用面积。",
                      " The config is smaller than the instrument - it will not hit the limit, but wastes usable area."))),
            nanonis_calls=calls)
