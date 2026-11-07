# -*- coding: utf-8 -*-
"""``MeasureFrameDrift`` —— 一批同框 .sxm 之间的横向漂移，亚像素、米制。

判据本体在 :mod:`mast.vision.frame_drift`。

## 它补的两格

既有的三条路都不做这件事：

* ``ComputeDriftVector`` / ``TrackDrift_ReferenceScan`` —— **实时抓帧** + 一张
  ``.npy`` 参考，**整像素**，而且任何一条异常路径都 ``return 0.0, 0.0``：
  「测不出来」被表达成「没有漂移」。
* ``DiffScans_ChangeDetect`` —— 吃两个文件，但产物是差图与 RMS，不给位移矢量。
* ``AnalyseSlowDrift`` —— 从**帧内**行序列找 0.001–1 Hz 的扰动，问的是
  「有没有周期性的东西在动」，不是「这一小时里样品挪了多远」。

外加一件全仓没人做过的：**上下扫交替时的回程差**。相邻帧一上一下时，
Δy 里装着回程差（正负交替）与净漂移（同号累积）之和；两帧滑动平均消掉前者。
不分开的话，一个纯往复会被读成正负交替的「漂移」。

⚠️ 本技能刻意**不**替调用方筛掉低置信的对：量不出来的那些留在 ``pairs`` 里、
标着 ``ok=False``，并且**不进平均**。理由写在 ``vision.frame_drift`` 的模块注释里
（先筛后统计会把方法的失败伪装成一个小而可信的数）。

## 只比**同一个扫描框**的帧

中心或尺寸不同的两帧之间的位移里混着「你把框挪了多少」，那不是漂移。
本技能按帧的中心/宽度分组，只在同组内相邻比较，跨组的地方断开并说明。
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
from mast.skills.base import BaseSkill
from mast.i18n import tr

logger = logging.getLogger(__name__)

_NAME = "MeasureFrameDrift"

__all__ = ["MeasureFrameDrift"]


class MeasureFrameDrift(BaseSkill):
    """从一批同框帧量横向漂移，并分出上下扫的回程差。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=_NAME,
            version="1.0.0",
            category=SkillCategory.ANALYSIS,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("量一批**同一个扫描框**的 .sxm 之间的横向漂移：亚像素相位相关，"
                "输出米制位移与置信度，并把上下扫交替带来的回程差与净漂移分开。"
                "量不出来时明说量不出来 —— 不把它折叠成「没有漂移」。", "Measure the lateral drift between a batch of .sxm files of the **same scan frame**: sub-pixel phase correlation, "
"giving metric displacements and confidence, and separating the retrace offset caused by alternating up/down scans from the net drift. "
"When it cannot be measured it says so -- it does not fold that into \"no drift\".")
            ),
            parameters=[
                ParameterSpec(
                    name="scan_paths", type="str",
                    description=(tr("按时间先后排好的 .sxm 路径，逗号或换行分隔，至少两个。", "Paths of the .sxm files in time order, comma or newline separated, at least two.")),
                    required=True),
                ParameterSpec(
                    name="channel", type="str",
                    description=tr("形貌通道名，默认 Z。", "Topography channel name, default Z."),
                    required=False, default="Z"),
                ParameterSpec(
                    name="min_confidence", type="float",
                    description=(
                        tr("归一化互相关的下限，低于它就不给这一对的位移。"
                        "默认 0.25 —— 它是一道**结构**门（两帧有没有共同特征），"
                        "不是标定出来的质量阈值。", "Lower limit of the normalised cross-correlation; below it the displacement of that pair is not given. "
"Default 0.25 -- it is a **structural** gate (do the two frames share features at all), "
"not a calibrated quality threshold.")),
                    required=False, default=0.25,
                    min_value=0.0, max_value=1.0),
            ],
            estimated_duration_s=10.0,
            composition_level=1,
            tags=["drift", "analysis", "read", "scan"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        import numpy as np

        from mast.skills.builtins._sxm_frame import load_frame, split_paths
        from mast.vision.frame_drift import pair_displacement, separate_hysteresis
        from mast.vision.scan_prep import poly_subtract

        paths = split_paths(str(params.get("scan_paths") or ""))
        channel = str(params.get("channel") or "Z")
        min_conf = float(params.get("min_confidence", 0.25))
        if len(paths) < 2:
            return SkillResult(skill_name=_NAME, success=False,
                               error=tr("至少要两个 scan_paths 才谈得上帧间漂移", "At least two scan_paths are needed to talk about frame-to-frame drift"))

        frames, rejected = [], []
        for p in paths:
            fr = load_frame(p, channel)
            if fr.error:
                rejected.append({"path": p, "reason": fr.error})
                continue
            frames.append(fr)
        if len(frames) < 2:
            # 原因要出现在 ``error`` 里，不能只躺在 ``data.rejected`` 里 ——
            # 调用方先看到的是前者，而「读不进来」与「读进来了但配不上」
            # 要修的是两件不同的事。
            return SkillResult(
                skill_name=_NAME, success=False,
                error=(tr("能读进来的帧不足两个（%d 个被拒）：%s", "Fewer than two frames could be read (%d rejected): %s")
                       % (len(rejected),
                          tr("；", "; ").join(r["reason"] for r in rejected[:3]))),
                data={"rejected": rejected})

        pairs, dy_seq = [], []
        for a, b in zip(frames, frames[1:]):
            same_frame = (a.width_nm and b.width_nm
                          and abs(a.width_nm - b.width_nm) < 1e-6
                          and a.forward.shape == b.forward.shape)
            if not same_frame:
                pairs.append({"pair": "%s -> %s" % (a.name, b.name),
                              "ok": False, "reason": "different_frame",
                              "note": (tr("两帧的扫描框不同（%.2f nm vs %.2f nm）——"
                                       "它们之间的位移里混着「框被挪了多少」，"
                                       "那不是漂移。", "The two frames have different scan frames (%.2f nm vs %.2f nm) -- "
"the displacement between them is mixed with \"how far the frame was moved\", "
"which is not drift.") % (a.width_nm or 0,
                                                        b.width_nm or 0))})
                dy_seq.append(None)
                continue
            # 去趋势后再配准：整帧的倾斜会主导互相关。
            ia = np.asarray(poly_subtract(a.forward, order=1), dtype=float)
            ib = np.asarray(poly_subtract(b.forward, order=1), dtype=float)
            d = pair_displacement(ia, ib, nm_per_px=a.nm_per_px,
                                  min_confidence=min_conf)
            # ``dx_nm`` / ``dy_nm`` are the estimator's register-shift in ARRAY axes (what
            # shifts the second frame onto the first; rows grow downwards). What an operator
            # wants is how the features moved in the scan frame, x right / y up — that is
            # ``feature_dx_nm`` / ``feature_dy_nm``. Two agents got the x sign backwards from
            # the raw numbers (2026-09-11); say it once, in the data, in physical axes.
            pairs.append({
                "pair": "%s -> %s" % (a.name, b.name),
                "scan_dirs": "%s->%s" % (a.scan_dir, b.scan_dir),
                "ok": bool(d.ok), "reason": d.reason,
                "dx_nm": None if d.dx_nm is None else round(d.dx_nm, 4),
                "dy_nm": None if d.dy_nm is None else round(d.dy_nm, 4),
                "feature_dx_nm": None if d.dx_nm is None else round(-d.dx_nm, 4),
                "feature_dy_nm": None if d.dy_nm is None else round(d.dy_nm, 4),
                "confidence": None if d.confidence is None else round(d.confidence, 3),
                "warnings": list(d.warnings),
            })
            dy_seq.append(d.dy_nm if d.ok else None)

        ok_pairs = [p for p in pairs if p.get("ok")]
        data: dict = {
            "n_frames": len(frames), "n_pairs": len(pairs),
            "n_measured": len(ok_pairs), "pairs": pairs, "rejected": rejected,
        }
        if not ok_pairs:
            data["verdict"] = "undetermined"
            return SkillResult(
                skill_name=_NAME, success=True, data=data,
                summary=(tr("%d 对帧一对都没量出位移 —— 逐对原因在 pairs 里。"
                         "**这不等于没有漂移**：量不出来和位移为零是两件事。", "%d pair(s) of frames yielded no displacement at all -- the per-pair reasons are in pairs. "
"**This does not mean there is no drift**: being unable to measure and zero displacement are two different things.")
                         % len(pairs)))

        data["verdict"] = "measured"
        alt = any(p.get("scan_dirs", "").split("->")[0]
                  != p.get("scan_dirs", "").split("->")[-1] for p in ok_pairs)
        data["alternating_scan_dir"] = alt
        hys = separate_hysteresis(dy_seq)
        data["hysteresis"] = hys
        dys = [p["dy_nm"] for p in ok_pairs]
        data["dx_median_nm"] = round(
            float(np.median([p["dx_nm"] for p in ok_pairs])), 4)
        data["dy_median_nm"] = round(float(np.median(dys)), 4)
        # the same two numbers in the scan frame's physical axes (x right, y up): how far the
        # features — the sample — moved from the earlier frame to the later one
        data["feature_dx_median_nm"] = round(-data["dx_median_nm"], 4)
        data["feature_dy_median_nm"] = round(data["dy_median_nm"], 4)
        data["convention"] = (tr("feature_dx_nm / feature_dy_nm：特征（样品）在扫描系里从前一帧到"
                              "后一帧挪了多远，x 向右、y 向上为正；dx_nm / dy_nm 是配准位移"
                              "（数组轴，行向下），保留给老调用方。补偿漂移要抵消的是 feature 那一对。", "feature_dx_nm / feature_dy_nm: how far the feature (sample) moved in the scan frame from the previous frame to "
"the next, x positive to the right, y positive up; dx_nm / dy_nm are the registration displacement "
"(array axes, rows downward), kept for old callers. What drift compensation must cancel is the feature pair."))
        # 各对读数既要检查幅值散布，也要检查符号一致性。
        # 单看四分位距可能遗漏方向相反的估计；任一检查不通过都应告警。
        if len(dys) >= 3:
            iqr = float(np.percentile(dys, 75) - np.percentile(dys, 25))
            med = data["dy_median_nm"]
            same_sign = float(np.mean([np.sign(v) == np.sign(med) for v in dys]))
            data["dy_iqr_nm"] = round(iqr, 4)
            data["dy_sign_agreement"] = round(same_sign, 3)
            if iqr > 1.5 * max(abs(med), 0.05) or same_sign < 0.7:
                data["consistency_warning"] = (
                    tr("各对帧给出的 Δy 彼此对不上（四分位距 %.2f nm、中位 %.2f nm、"
                    "只有 %.0f%% 的对与中位同号）—— 这时中位数不代表漂移，"
                    "它只是一堆不一致读数的中间值。多半是这批帧上没有可配准的"
                    "非周期特征（同一个台面 + 会动的吸附物 + 针尖在变）。"
                    "**这种情况下改从晶格量漂移**：MeasureLatticeCell 用上下扫的 a₂ "
                    "应变，在同一批帧上是稳的。", "The delta-y values from the pairs of frames do not agree with each other (interquartile range %.2f nm, median %.2f nm, "
"only %.0f%% of pairs have the same sign as the median) -- the median then does not represent drift, "
"it is just the middle of a set of inconsistent readings. Most likely this batch has no registrable "
"non-periodic feature (the same terrace + moving adsorbates + a changing tip). "
"**In that case measure drift from the lattice instead**: MeasureLatticeCell uses the a2 "
"strain of the up/down scans, which is stable on the same batch.")
                    % (iqr, med, 100 * same_sign))
        if not alt:
            data["hysteresis_note"] = (
                tr("这一批帧的扫描方向没有交替，所以回程差与净漂移**分不开** —— "
                "``hysteresis_nm`` 这一栏在这种情况下只是位移的中位幅值。"
                "要分开就让扫描方向一上一下交替（Nanonis 的 SCAN_DIR）。", "The scan directions of this batch do not alternate, so the retrace offset and the net drift **cannot be separated** -- "
"the ``hysteresis_nm`` column is then just the median magnitude of the displacement. "
"To separate them, alternate the scan direction up and down (Nanonis SCAN_DIR)."))
        # 每小时多少：只在拿得到帧间隔时给。
        times = [f.line_time_s * 2.0 * f.forward.shape[0] for f in frames
                 if f.line_time_s]
        if times and hys.get("net_drift_per_frame_nm") is not None:
            per_frame_s = float(np.median(times))
            data["frame_seconds"] = round(per_frame_s, 1)
            data["net_drift_nm_per_h"] = round(
                hys["net_drift_per_frame_nm"] / per_frame_s * 3600.0, 3)
        return SkillResult(skill_name=_NAME, success=True, data=data,
                           summary=_summary(data))


def _summary(data: dict) -> str:
    if data.get("verdict") != "measured":
        return tr("没量出任何一对帧的位移。", "No pair of frames yielded a displacement.")
    h = data.get("hysteresis") or {}
    s = (tr("%d/%d 对帧量出了位移；x 中位 %+.3f nm、y 中位 %+.3f nm", "%d/%d pair(s) of frames yielded a displacement; x median %+.3f nm, y median %+.3f nm")
         % (data["n_measured"], data["n_pairs"],
            data["dx_median_nm"], data["dy_median_nm"]))
    if data.get("alternating_scan_dir"):
        s += (tr("。上下扫交替：回程差中位 %.3f nm/帧，扣掉它之后净漂移 %.3f nm/帧", ". Up/down scans alternate: retrace offset median %.3f nm/frame, net drift after subtracting it %.3f nm/frame")
              % (h.get("hysteresis_nm") or 0.0,
                 h.get("net_drift_per_frame_nm") or 0.0))
        if data.get("net_drift_nm_per_h") is not None:
            s += tr("（%.2f nm/h）", " (%.2f nm/h)") % data["net_drift_nm_per_h"]
    else:
        s += tr("。扫描方向没有交替，回程差与净漂移分不开", ". The scan directions do not alternate, so retrace offset and net drift cannot be separated")
    if h.get("n_unmeasured"):
        s += tr("；另有 %d 对没量出来（不计入平均）", "; another %d pair(s) could not be measured (not counted in the average)") % h["n_unmeasured"]
    s += tr("。", ".")
    if data.get("consistency_warning"):
        s += tr(" ⚠️ 各对之间对不上 —— 这个中位数不该当漂移读，见 consistency_warning。", " ⚠️ The pairs do not agree -- do not read this median as drift, see consistency_warning.")
    return s


def make_tool(context_provider):
    from mast.agents._shared.skill_adapter import wrap_skill
    return wrap_skill(MeasureFrameDrift, context_provider)
