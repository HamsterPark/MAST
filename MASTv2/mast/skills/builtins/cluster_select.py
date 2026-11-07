"""SelectPokedCluster：在提取出的全部候选中选择与扎针锚点匹配的目标。

先联合检查形状、面积与峰高，再选择距锚点最近且满足容差的候选；无法匹配时弃权。
各阈值必须显式提供，不用缺少依据的默认值代替当前数据的验证。

验证需覆盖完整候选与反例，并保持 RAW 或平面处理等预处理口径一致。
管道中的无效行、极性与坐标变换问题必须先解决，不能通过调阈值掩盖。
缺参数时说明应如何取得阈值；任何判据的调整都需要重新检查误判与漏判。

锚点由执行动作的调用方提供，容差按定位与分割误差验证。
每次返回实际匹配距离，供调用方检查当前容差是否适用。
"""
from __future__ import annotations

import logging
import math

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

#: 拒绝报文里给出的处方 —— 「该怎么填」而不是「你没填」。
_BOX_HINT = (
    tr("请用 ExtractClusters 分析自己的完整数据集，对全部候选及反例的长宽比、像素数与峰高检查分布，再明确给出阈值。"
    "必须保持预处理一致，并验证误判与漏判；这里不提供样品标定值。",
    "Analyse your own full dataset with ExtractClusters, check the distribution of aspect ratio, pixel count and peak height over all candidates and counter-examples, then set the thresholds explicitly. Keep preprocessing consistent and verify false positives and misses; no sample-specific calibration values are provided here.")
)
_ANCHOR_HINT = (
    tr("扎针坐标由**刚扎完的那个调用方**提供(它知道自己扎在哪);留空则退回帧中心 —— "
    # ⚠️ 这里的例子必须写成**带 SI 前缀的字符串**('3n'),不能写 3e-9 ——
    # 这几个米量纲参数是**强制前缀**的,指数写法会被解析器直接拒绝。
    # 在描述里举一个自己会拒绝的例子,等于教模型去撞墙(它不会怀疑文档,只会照抄)。
    # 由 tests/v2/unit/skills/test_strict_param_descriptions.py 把守。
    "只有当扫描框确实对准了扎针点时那才等价。容差必须按当前定位误差提供,"
    "例如 '3n' 仅演示带 SI 前缀的输入格式,不是标定建议。",
    "The poke coordinates are supplied by **the caller that just poked** (it knows where it poked); if empty, falls back to the frame centre - that is only equivalent when the scan frame was really aimed at the poke point. The tolerance must be set from the current positioning error; '3n' is only an example of the SI-prefixed input format, not a calibration recommendation.")
)


class SelectPokedCluster(BaseSkill):
    """从一帧里挑出「我们刚扎的那个」团簇 —— 合取筛 + 按锚点最近 + 够不着弃权。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="SelectPokedCluster",
            version="1.0.0",
            category=SkillCategory.ANALYSIS,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("从一帧可能有很多团簇的图里，挑出**我们刚扎的那一个**。"
                "先跑 ExtractClusters，再过一道合取（圆 且 大 且 高），"
                "然后取离扎针坐标最近的那个。容差之内一个都没有时**弃权**"
                "（selected=null）—— 「我找到的最近的东西」不是一个答案。"
                "每一个阈值都**必填、没有默认值**：拒绝替人猜数，"
                "正是这一层的意义所在。",
                "From a frame that may contain many clusters, pick **the one we just poked**. First run ExtractClusters, then pass a conjunction (round AND large AND tall), then take the one nearest the poke coordinates. If none is within the tolerance it **abstains** (selected=null) - 'the nearest thing I found' is not an answer. Every threshold is **required and has no default**: refusing to guess numbers for people is the whole point of this layer.")
            ),
            parameters=[
                ParameterSpec(
                    name="scan_path", type="str", required=True,
                    description=tr("已保存的那张 .sxm 的路径。", "Path of the saved .sxm."),
                ),
                ParameterSpec(
                    name="min_aspect", type="float", required=True,
                    min_value=0.0, max_value=1.0,
                    description=(tr("筛掉线状伪影。**必填，没有默认值**。", "Filters out line-like artifacts. **Required, no default**.")
                                 + _BOX_HINT),
                ),
                ParameterSpec(
                    name="min_area_px", type="int", required=True,
                    min_value=1, max_value=1000000,
                    description=(tr("连通域的最小面积。**必填，没有默认值**。"
                                 "注意：这一条在有的数据上几乎**不承担"
                                 "分辨力**，但仍需与其他特征一起验证 —— "
                                 "而这恰恰是「把它调松了也没人会注意到」"
                                 "的原因。",
                                 "Minimum area of a connected component. **Required, no default**. Note: on some datasets this one carries almost **no discriminating power**, but it must still be validated together with the other features - which is exactly why 'nobody would notice if it were loosened'.") + _BOX_HINT),
                ),
                ParameterSpec(
                    name="min_peak_height_m", type="float", required=True,
                    unit="m", min_value=0.0, max_value=1e-6,
                    description=(tr("最小峰高。**必填，没有默认值**。"
                                 "这一条才是真正分得开的那一维"
                                 "（需与形状、面积联合验证）。",
                                 "Minimum peak height. **Required, no default**. This is the dimension that really separates them (validate it jointly with shape and area).") + _BOX_HINT),
                ),
                ParameterSpec(
                    name="anchor_tolerance_m", type="float", required=True,
                    unit="m", min_value=0.0, max_value=1e-6,
                    description=(tr("离扎针点多近才算「我们扎的那一个」。"
                                 "**必填，没有默认值**。",
                                 "How close to the poke point counts as 'the one we poked'. **Required, no default**.") + _ANCHOR_HINT),
                ),
                ParameterSpec(
                    name="near_x_m", type="float", required=False, default=None,
                    unit="m", description=tr("扎针点的 X。不填 → 退回帧中心。", "X of the poke point. If not given -> falls back to the frame centre.") + _ANCHOR_HINT,
                ),
                ParameterSpec(
                    name="near_y_m", type="float", required=False, default=None,
                    unit="m", description=tr("扎针点的 Y。不填 → 退回帧中心。", "Y of the poke point. If not given -> falls back to the frame centre."),
                ),
                ParameterSpec(
                    name="channel", type="str", required=False, default="Z",
                    description=tr("通道名（默认 Z）。", "Channel name (default Z)."),
                ),
                ParameterSpec(
                    name="polarity", type="str", required=False, default="auto",
                    allowed_values=["auto", "bright", "dark"],
                    description=tr("原样透传给 ExtractClusters。", "Passed through unchanged to ExtractClusters."),
                ),
                ParameterSpec(
                    name="level", type="str", required=False, default="none",
                    allowed_values=["none", "plane"],
                    description=(tr("原样透传。默认 RAW。阈值必须在与当前数据相同的"
                                 "预处理口径下验证；更换预处理后需重新检查。",
                                 
                                 "Passed through unchanged. Default RAW. Thresholds must be validated under the same preprocessing as the data; re-check them after changing it.")),
                ),
            ],
            estimated_duration_s=2.0,
            composition_level=1,
            tags=["scan", "analysis", "cluster", "select", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "SelectPokedCluster"

        # ── 必填阈值:拒绝时**把处方一起给** ────────────────────────────
        missing = [k for k in ("min_aspect", "min_area_px", "min_peak_height_m",
                               "anchor_tolerance_m")
                   if params.get(k) is None]
        if missing:
            return SkillResult(
                skill_name=name, success=False,
                error=(tr(f"这些阈值必须显式给出,本技能**不替你猜**:{missing}。"
                       f"{_BOX_HINT} 锚点容差:{_ANCHOR_HINT}",
                       f"These thresholds must be given explicitly; this skill **does not guess them for you**: {missing}. {_BOX_HINT} Anchor tolerance: {_ANCHOR_HINT}")),
                # ⚠️ 米量纲那两个必须给**带 SI 前缀的字符串**,不能给 1.5e-10 /
                # 3e-9:它们是强制前缀参数,指数写法会被这个技能**自己**拒绝。
                #
                # 这和描述里举错例子是同一个缺陷,只是挪到了 data 里 —— 而这里更糟:
                # **拒绝报文正是模型用来决定"下次该传什么"的东西**,
                # 一份开不出来的处方比不开处方更坏。
                # (那道闸门只看 description,看不到 data;由
                #  test_cluster_select.py::test_the_prescription_is_actually_fillable 把守。)
                data={"missing_parameters": missing,
                      "suggested_operating_point": {},
                      "calibration_required": True})

        # 米量纲的两个参数要**同时**收得下真 float 和 SI 前缀字符串。
        #
        # agent 路径上 `wrap_skill` 会先跑 `_coerce_si_params`,模型传的 '150p'
        # 到这里已经是 float 了。但**直接 `.execute()` 的调用方绕过那一步** ——
        # 而我们自己的拒绝报文开的处方正是 '150p' / '3n' 这种字符串,
        # 于是「照着处方填」在直接调用路径上会崩在 `float('150p')`。
        #
        # 用共享的 `parse_quantity`(不新写一个),strict=True 与 schema 广告的
        # 强制前缀**同一口径** —— 广告与执行必须由同一个表达式算出来。
        from mast.core.si_quantity import SIParseError, parse_quantity

        def _metre(key: str) -> "float | None":
            v = params[key]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)                      # 内部调用方持有真数
            return parse_quantity(v, strict=True, what=key)

        min_aspect = float(params["min_aspect"])
        min_area = int(params["min_area_px"])
        try:
            min_peak_m = _metre("min_peak_height_m")
            tol_m = _metre("anchor_tolerance_m")
        except SIParseError as exc:
            return SkillResult(skill_name=name, success=False, error=str(exc))

        from mast.skills.builtins.cluster_extract import ExtractClusters
        ext = ExtractClusters().execute(context, {
            "scan_path": params.get("scan_path"),
            "channel": params.get("channel", "Z"),
            "polarity": params.get("polarity", "auto"),
            "level": params.get("level", "none"),
            # 提取层的 min_area 只为列表可读;判定用的是上面那个 min_area。
            "min_area_px": 1,
            "max_clusters": 10000,
        })
        if not ext.success:
            return SkillResult(skill_name=name, success=False,
                               error=tr(f"提取失败:{ext.error}", f"Extraction failed: {ext.error}"), data=ext.data)
        ex = ext.data
        frame = ex["frame"]

        # ── 锚点 ────────────────────────────────────────────────────────
        ax, ay = params.get("near_x_m"), params.get("near_y_m")
        if ax is not None and ay is not None:
            anchor = (float(ax), float(ay))
            anchor_src = "explicit"
        else:
            anchor = (frame["cx_m"], frame["cy_m"])
            anchor_src = "frame_centre"
        # 坐标不可信时**说出来**:角度未知 = 换算没有验证过,
        # 而调用方要拿这个结果去移动针尖。
        coords_trustworthy = bool(frame.get("angle_known", False))

        # ── 合取筛:筛掉谁、为什么,都报出来 ─────────────────────────────
        #
        # 提取层不丢信息;判定层丢信息,但**必须说清丢了什么**,
        # 否则「提取层不吃掉信息」的好处在这里又被吃掉一次,只是换了个地方。
        cands = []
        for c in ex["clusters"]:
            failed = []
            if c["aspect"] < min_aspect:
                failed.append(f"aspect {c['aspect']:.3f} < {min_aspect}")
            if c["area_px"] < min_area:
                failed.append(f"area {c['area_px']}px < {min_area}")
            peak_m = (c["peak_height_pm"] or 0.0) * 1e-12
            if peak_m < min_peak_m:
                failed.append(f"peak {c['peak_height_pm']:.1f}pm "
                              f"< {min_peak_m * 1e12:.1f}pm")
            d = None
            if c["x_m"] is not None and c["y_m"] is not None:
                d = math.hypot(c["x_m"] - anchor[0], c["y_m"] - anchor[1])
            cands.append({**c, "passed_conjunction": not failed,
                          "failed_on": failed, "distance_to_anchor_m": d})

        passing = [c for c in cands if c["passed_conjunction"]
                   and c["distance_to_anchor_m"] is not None]

        out = {
            "scan_path": params.get("scan_path"),
            "anchor": {"x_m": anchor[0], "y_m": anchor[1], "source": anchor_src},
            "coords_trustworthy": coords_trustworthy,
            "thresholds_used": {
                "min_aspect": min_aspect, "min_area_px": min_area,
                "min_peak_height_pm": min_peak_m * 1e12,
                "anchor_tolerance_m": tol_m},
            "frame": frame,
            "leveling_used": ex["leveling_used"],
            "polarity_used": ex["polarity_used"],
            "tilt_warning": ex.get("tilt_warning"),
            "n_extracted": len(cands),
            "n_passed_conjunction": len(passing),
            "candidates": cands[:50],
        }
        if not coords_trustworthy:
            out["coords_warning"] = (
                tr("这一帧的 scan_angle 不可知,像素→米的换算**没有验证过** —— "
                "选出来的坐标不要直接拿去移动针尖。",
                "The scan_angle of this frame is unknown, and the pixel-to-metre conversion has **not been verified** - do not use the selected coordinates to move the tip directly."))

        if not passing:
            # 弃权,并且**把最近的那个的距离也报出来** —— 它是下一版容差的数据。
            nearest_any = min((c for c in cands
                               if c["distance_to_anchor_m"] is not None),
                              key=lambda c: c["distance_to_anchor_m"], default=None)
            out["selected"] = None
            out["selected_reason"] = (
                tr(f"提取到 {len(cands)} 个连通域,没有一个同时满足"
                f"(长宽比≥{min_aspect}、≥{min_area}px、峰高≥{min_peak_m * 1e12:.0f}pm)。"
                "**这是弃权,不是「没有团簇」** —— 可能是阈值不适合这一帧,"
                "也可能这一针真的没扎出东西。看 candidates 里每个是卡在哪一条。",
                f"Extracted {len(cands)} connected components, none satisfies all of (aspect ratio>={min_aspect}, >={min_area}px, peak height>={min_peak_m * 1e12:.0f}pm). **This is an abstention, not 'no cluster'** - the threshold may not suit this frame, or this poke may really have produced nothing. See which condition each entry in candidates is stuck on."))
            if nearest_any is not None:
                out["nearest_rejected"] = {
                    "rank": nearest_any["rank"],
                    "distance_to_anchor_m": nearest_any["distance_to_anchor_m"],
                    "failed_on": nearest_any["failed_on"]}
            return SkillResult(skill_name=name, success=True, data=out)

        best = min(passing, key=lambda c: c["distance_to_anchor_m"])
        out["distance_to_anchor_m"] = best["distance_to_anchor_m"]
        if best["distance_to_anchor_m"] > tol_m:
            # **够不着就弃权。** 把「我找到的最近的东西」当成新扎的那个交出去,
            # 是三态里最常被跳过的那一步。
            out["selected"] = None
            out["selected_reason"] = (
                tr(f"最近的合格团簇距锚点 {best['distance_to_anchor_m'] * 1e9:.2f} nm,"
                f"超过容差 {tol_m * 1e9:.2f} nm —— **判不了这是不是我们扎的那个**。"
                "不把它当成新扎的交出去:那正是「拿最近的东西冒充答案」。"
                "(如果这个距离反复出现在同一个量级上,那就是容差该改的信号 —— "
                "这一版的 3 nm 就是这么来的。)",
                f"The nearest qualifying cluster is {best['distance_to_anchor_m'] * 1e9:.2f} nm from the anchor, beyond the tolerance {tol_m * 1e9:.2f} nm - **cannot tell whether this is the one we poked**. It is not handed over as the newly poked one: that would be 'passing off the nearest thing as the answer'. (If this distance keeps appearing at the same order of magnitude, it is a signal that the tolerance should change - this version's 3 nm came about that way.)"))
            out["nearest_passing"] = {
                "rank": best["rank"],
                "distance_to_anchor_m": best["distance_to_anchor_m"],
                "peak_height_pm": best["peak_height_pm"],
                "aspect": best["aspect"], "area_px": best["area_px"]}
            return SkillResult(skill_name=name, success=True, data=out)

        out["selected"] = best
        out["selected_reason"] = (
            tr(f"合格团簇 {len(passing)} 个,取距锚点最近的一个:"
            f"{best['distance_to_anchor_m'] * 1e9:.2f} nm(容差 {tol_m * 1e9:.2f} nm)。",
            f"{len(passing)} qualifying clusters, taking the one nearest the anchor: {best['distance_to_anchor_m'] * 1e9:.2f} nm (tolerance {tol_m * 1e9:.2f} nm)."))
        return SkillResult(skill_name=name, success=True, data=out)
