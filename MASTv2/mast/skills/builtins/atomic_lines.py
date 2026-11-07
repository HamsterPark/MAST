"""扫描进行中的逐行纹理提示。

只分析已完成的扫描行，返回 advisory；一维周期起伏不能区分二维晶格与条纹，
最终晶格判定仍交给 AssessAtomicResolution。此技能不需要停扫。

通道参数必须使用 Scan_BufferGet 给出的信号索引，而不是缓冲区中的位置。
尺度由当前扫描框与缓冲区像素数计算，不能沿用其他扫描设置。
恒流模式的 Z 信号反映反馈维持电流时的位移，电流通道主要反映反馈误差；
两者物理含义不同，不能把某一通道上的周期信号直接当成原子分辨结论。
"""
from __future__ import annotations

import logging
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

class AssessAtomicLines(BaseSkill):
    """扫描进行中，给已扫出来的线打分：有没有规则的跳动。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AssessAtomicLines",
            version="1.0.0",
            category=SkillCategory.ANALYSIS,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("对正在进行的扫描中**已经扫完**的那些线打分，看有没有规则的"
                "起伏（即检查扫描线这一步）。"
                "返回的是一条 ADVISORY —— **单条线分不出 2-D 晶格与 1-D 波纹**；"
                "下判定的仍然是 AssessAtomicResolution。"
                "它读 Z 通道（比电流更灵敏），而且**不用停扫**就能做，"
                "所以一轮只花几十秒，而不是等一整帧。",
                "Score the lines of the scan in progress that have **already been scanned**, looking for regular undulation (the scan-line check). Returns an ADVISORY - **a single line cannot tell a 2-D lattice from 1-D ripple**; the decision still belongs to AssessAtomicResolution. It reads the Z channel (more sensitive than current) and **needs no scan stop**, so a round costs tens of seconds, not a whole frame.")
            ),
            parameters=[
                ParameterSpec(
                    name="direction", type="int",
                    description=tr("1 = 正扫，0 = 反扫（与 .sxm 的 forward / backward 对应）。", "1 = forward scan, 0 = backward scan (corresponding to forward / backward of the .sxm)."),
                    required=False, default=1, min_value=0, max_value=1),
                ParameterSpec(
                    name="n_recent_lines", type="int",
                    description=(
                        tr("只给最近扫出来的这么多行打分；0 = 全部已扫出的行。"
                        "打磨环里要的是**刚扫的那几行**，整帧的中位会被扰动之前"
                        "那些行拖住。",
                        "Score only this many of the most recently scanned rows; 0 = all rows scanned so far. A polishing loop wants **the rows just scanned**; the median of the whole frame is dragged by the rows from before the disturbance.")),
                    required=False, default=0, min_value=0, max_value=4096),
                ParameterSpec(
                    name="advisory_snr", type="float",
                    description=(
                        tr("建议线。出厂 80，是未充分标定的工作点。"
                        "**样本很薄，它只建议不否决。**",
                        "Advisory line. Factory value 80, a working point that is not fully calibrated. **The sample is thin; it only advises and does not veto.**")),
                    required=False, default=80.0,
                    min_value=1.0, max_value=100000.0),
                ParameterSpec(
                    name="channel_index", type="int",
                    description=(
                        tr("强行指定信号索引（不是缓冲位）。-1 = 自己去 Scan_BufferGet "
                        "找 Z。填错的通道会得到一个对不齐的回包。",
                        "Force a signal index (not a buffer slot). -1 = look for Z yourself via Scan_BufferGet. A wrongly chosen channel gives a reply that does not line up.")),
                    required=False, default=-1, min_value=-1, max_value=127),
            ],
            estimated_duration_s=2.0,
            composition_level=0,
            tags=["atomic", "scan", "line", "advisory", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        import numpy as np

        from mast.skills.builtins._scan_readout import resolve_readout
        from mast.vision.atomic_lines import frame_line_advisory, usable_rows

        direction = int(params.get("direction", 1))
        n_recent = int(params.get("n_recent_lines", 0) or 0)
        adv_snr = float(params.get("advisory_snr", 80.0))
        forced = int(params.get("channel_index", -1))

        calls: list = []
        # 解通道与尺度只有一份实现（_scan_readout）—— 两个技能各写一份的下场是
        # 两边迟早只有一边对。
        ro = resolve_readout(context, calls, forced_channel=forced)
        if not ro.ok:
            return SkillResult(skill_name="AssessAtomicLines", success=False,
                               error=ro.why, data=ro.as_dict(), nanonis_calls=calls)
        ch, nm_per_px, chans = ro.z_index, ro.nm_per_px, ro.channels

        from mast.skills.builtins._scan_readout import grab_frame
        arr, why = grab_frame(context, ch, direction, calls)
        if arr is None:
            return SkillResult(
                skill_name="AssessAtomicLines", success=False,
                error=(tr("取不到通道 %d 的帧：%s%s", "Cannot get the frame of channel %d: %s%s")
                       % (ch, why,
                          (tr("　（扫描缓冲里的通道是 %s —— 这个参数要的是**信号索引**，"
                           "不是缓冲位）",
                           " (the channels in the scan buffer are %s - this parameter wants a **signal index**, not a buffer slot)") % chans) if chans else "")),
                nanonis_calls=calls)

        img = np.asarray(arr, dtype=float)
        if img.ndim != 2:
            return SkillResult(skill_name="AssessAtomicLines", success=False,
                               error=tr("帧不是二维：shape=%s", "Frame is not 2-D: shape=%s") % (img.shape,),
                               nanonis_calls=calls)

        filled = usable_rows(img)
        rows = filled
        if n_recent > 0:
            idx = np.flatnonzero(filled)
            if idx.size:
                # 扫描方向决定新行在哪一头，所以两头都不假设：取**紧邻未扫区**
                # 的那 n 行。未扫区在低号一侧（direction="up"）时是 idx 的前 n 个。
                unscanned_low = int(filled[:1].sum()) == 0
                take = idx[:n_recent] if unscanned_low else idx[-n_recent:]
                rows = np.zeros_like(filled)
                rows[take] = True

        out = frame_line_advisory(img, nm_per_px, rows=rows, advisory_snr=adv_snr)
        out.update({
            "channel_index": int(ch),
            "channel_source": ro.z_source,
            "direction": direction,
            "nm_per_px": nm_per_px,
            "n_rows_filled": int(filled.sum()),
            "n_rows_total": int(img.shape[0]),
        })
        if not out.get("ok"):
            return SkillResult(skill_name="AssessAtomicLines", success=False,
                               error=out.get("why") or tr("线级判读没算出结果", "The line-level reading produced no result"),
                               data=out, nanonis_calls=calls)
        return SkillResult(
            skill_name="AssessAtomicLines", success=True, data=out,
            summary=out["advisory"], nanonis_calls=calls)


def make_tools(context_provider):
    from mast.agents._shared.skill_adapter import wrap_skill

    return [wrap_skill(AssessAtomicLines, context_provider)]


__all__ = ["AssessAtomicLines"]
