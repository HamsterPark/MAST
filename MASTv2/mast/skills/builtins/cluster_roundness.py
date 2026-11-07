"""用独立的形状特征联合判断团簇圆度。

像素边计数的 4πA/P² 存在方向性偏差：轴对齐正方形为 π/4，
而数字化圆盘受栅格边界影响，旧标度不能直接用作几何圆度。
当前 equivalent_axis_ratio 表示径向离散等效的椭圆轴比，
aspect_ratio 检查整体拉长；两项必须同时满足阈值，一项高分不能补另一项低分。
实现与几何推导见 vision.roundness。

小团簇受像素化限制，is_round 可为 None，并附 roundness_undecidable 理由；
无法判定不等于不圆，也不能当成通过。

分割仍独立于 ExtractClusters：此处使用平面处理后的 mean/std，
后者默认 RAW 下的 median/scaled-MAD，因此同一帧可能得到不同连通域。
比较判据结果时必须先确认分割与预处理口径一致。
"""

from __future__ import annotations

import logging
from pathlib import Path

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


#: 多针尖判据的两个数。**都不是拟合出来的**:
#: 25% —— 一个顶点扎出 500 px、另一个 5 px,后者是碎屑不是顶点;
#: 6 nm —— 针尖顶点间距只有几 nm。放宽到不限:误杀 3→4 而只多抓 1 张;
#:         收到 4 nm:只抓到 7/18。**6 nm 是这个权衡的拐点,不是最优值**。
#: 出处与成绩见 ``execute`` 里 multi_tip 那一段。
MULTI_TIP_SIZE_FRAC = 0.25
MULTI_TIP_SPAN_NM = 6.0


class AssessClusterRoundness(BaseSkill):
    """Compute roundness of the largest protrusion in a small .sxm scan."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AssessClusterRoundness",
            version="1.0.0",
            category=SkillCategory.READ,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("把一张小 .sxm 里扎出来的那个团簇坑分割出来，说出它有多圆。"
                "主输出是 equivalent_axis_ratio ∈ (0,1]：「这个团簇的不规则"
                "程度，相当于一个短轴/长轴等于该比值的椭圆」—— 1.0 = 完美圆盘；"
                "由于像素化本底已经被减掉，它在任何团簇尺寸上含义都一样。"
                "is_round 是一个**合取**（CONJUNCTION：equivalent_axis_ratio "
                "与二阶矩长宽比两条都要过），团簇小到判不了时它是 null —— "
                "**不是 false**。",
                "Segment the cluster pit poked into a small .sxm and say how round it is. The main output is equivalent_axis_ratio ∈ (0,1]: 'the irregularity of this cluster is equivalent to an ellipse whose minor/major axis equals this ratio' - 1.0 = a perfect disc; since the pixelation floor has been subtracted, it means the same at any cluster size. is_round is a **CONJUNCTION** (both equivalent_axis_ratio and the second-moment aspect ratio must pass), and when the cluster is too small to judge it is null - **not false**.")
            ),
            parameters=[
                ParameterSpec(
                    name="scan_path",
                    type="str",
                    description=tr("那张团簇坑 .sxm 扫描图的路径。", "Path of the cluster-pit .sxm scan image."),
                    required=True,
                ),
                ParameterSpec(
                    name="threshold_sigma",
                    type="float",
                    description=(
                        tr("一个像素要高出局部均值多少个标准差，才算进团簇。"
                        "越大越严。",
                        "How many standard deviations above the local mean a pixel must be to count as part of the cluster. Larger is stricter.")
                    ),
                    required=False,
                    default=1.5,
                    min_value=0.5,
                    max_value=5.0,
                ),
                ParameterSpec(
                    name="polarity",
                    type="str",
                    description=(
                        tr("'bright' 对应向上的凸包（多数金属团簇），'dark' "
                        "对应一个坑，'auto' 用两个极端里更大的那一侧。"
                        "⚠️ 'auto' 在大团簇上会翻:簇占的像素多过背景时选中背景。"
                        "评刚扎出来的簇一律传 'bright'(auto 在大团簇上可能选到暗侧,"
                        "导致误判)。",
                        "'bright' means an upward protrusion (most metal clusters), 'dark' means a pit, 'auto' uses whichever of the two extremes is larger. WARNING: 'auto' flips on large clusters: when the cluster occupies more pixels than the background it picks the background. Always pass 'bright' when judging a freshly poked cluster (auto can pick the dark side on large clusters and misjudge them).")
                    ),
                    required=False,
                    default="auto",
                ),
                ParameterSpec(
                    name="threshold_mode",
                    type="str",
                    description=(
                        tr("'sigma'(出厂,向后兼容)= mean + threshold_sigma×std ——"
                        "**自指**:簇越大 std 越大、阈值越高、切掉的簇越多。"
                        "'physical' = 背景**众数** + physical_threshold_pm,"
                        "用外部标尺,与簇多大无关。sigma 模式下同一个"
                        "「1.5σ」切在离背景的高度随帧而变;physical 在阈值偏高的帧上"
                        "能多切出可观的面积。",
                        "'sigma' (factory, backward compatible) = mean + threshold_sigma×std - **self-referential**: the larger the cluster, the larger the std, the higher the threshold, the more of the cluster is cut off. 'physical' = background **mode** + physical_threshold_pm, using an external ruler, independent of cluster size. In sigma mode the same '1.5σ' cuts at a different height above the background from frame to frame; on frames where the threshold is too high, physical cuts out considerably more area.")
                    ),
                    required=False,
                    default="sigma",
                ),
                ParameterSpec(
                    name="physical_threshold_pm",
                    type="float",
                    description=(
                        tr("threshold_mode='physical' 时,高出背景多少算团簇(pm)。"
                        "出厂 117.7 = **半个 Au(111) 单原子台阶**(235.4 pm = a/√3)。"
                        "这是外部标尺不是拟合值 —— 换材料要换这个数。",
                        "When threshold_mode='physical', how far above the background counts as cluster (pm). Factory 117.7 = **half a single-atom step of Au(111)** (235.4 pm = a/√3). This is an external ruler, not a fitted value - change this number when changing material.")
                    ),
                    required=False,
                    default=117.7,
                    min_value=1.0,
                    max_value=5000.0,
                ),
                ParameterSpec(
                    name="shape_mode",
                    type="str",
                    description=(
                        tr("'boundary'(出厂)= 二值化后量边界像素到质心的径向离散 ——"
                        "**阈值以上每一点的高度全被丢掉**,结果由阈值切在哪儿决定。"
                        "'weighted' = 高度加权二阶矩,每点按高出背景多少计权。"
                        "阈值挪 ±20% 时轴比变化:weighted 比 boundary 小得多;"
                        "在阈值偏高的帧上差距更大。"
                        "两个数**永远都报**(weighted_axis_ratio / boundary_axis_ratio),"
                        "这个开关只决定谁驱动 is_round。",
                        "'boundary' (factory) = after binarisation, measure the radial dispersion of the boundary pixels from the centroid - **the height of every point above the threshold is discarded**, and the result is decided by where the threshold cuts. 'weighted' = height-weighted second moment, each point weighted by how far it is above the background. When the threshold is moved ±20% the axis ratio changes: the weighted ratio changes far less than the boundary ratio, and the gap is larger on frames where the threshold is too high. Both numbers are **always reported** (weighted_axis_ratio / boundary_axis_ratio); this switch only decides which one drives is_round.")
                    ),
                    required=False,
                    default="boundary",
                ),
                ParameterSpec(
                    name="channel",
                    type="str",
                    description=tr("通道名（默认 'Z'）。", "Channel name (default 'Z')."),
                    required=False,
                    default="Z",
                ),
                ParameterSpec(
                    name="select",
                    type="str",
                    description=(
                        tr("一帧里有好几坨时，评的是哪一坨："
                        "'largest'（默认）或 'center' —— 后者是离帧中心最近的"
                        "那一坨，也就是调用方刚刚在它把这次扫描对准的那个点上"
                        "扎出来的坑。一轮修针会留下好几个坑，"
                        "而最大的那个往往是更早留下的。",
                        "Which blob to judge when a frame has several: 'largest' (default) or 'center' - the latter is the blob nearest the frame centre, i.e. the pit the caller just poked at the point this scan was aimed at. A round of tip conditioning leaves several pits, and the largest is often one left earlier.")
                    ),
                    required=False,
                    default="largest",
                    allowed_values=["largest", "center"],
                ),
                ParameterSpec(
                    name="min_axis_ratio",
                    type="float",
                    description=(
                        tr("圆到什么程度才算够圆，用**等效轴比**"
                        "（EQUIVALENT AXIS RATIO）表述：0.75 的意思是"
                        "「不比一个长短轴相差 25% 的椭圆更不规则」。它是从边界的 "
                        "r(theta) 相对离散读出来的（已减掉像素化本底），"
                        "所以在任何团簇尺寸上含义都一样。"
                        "它**取代** round_threshold=0.65 —— 那个阈值所在的标度上，"
                        "任何圆都够不着（见模块 docstring）；"
                        "两个数**不可比**。",
                        "How round counts as round enough, expressed as the **EQUIVALENT AXIS RATIO**: 0.75 means 'no more irregular than an ellipse whose axes differ by 25%'. It is read from the relative dispersion of the boundary r(theta) (with the pixelation floor subtracted), so it means the same at any cluster size. It **replaces** round_threshold=0.65 - at the scale where that threshold sits, no circle can reach it (see the module docstring); the two numbers are **not comparable**.")
                    ),
                    required=False,
                    default=0.75,
                    min_value=0.0,
                    max_value=1.0,
                ),
                ParameterSpec(
                    name="min_aspect",
                    type="float",
                    description=(
                        tr("二阶矩长宽比的下限，也就是这个合取的**另一半**。"
                        "它抓的是单纯的拉长 —— 而那件事 r(theta) "
                        "离散看得没这么清；"
                        "反过来，瓣和缺口这类被二阶矩判成完美圆的形状，"
                        "靠离散来抓。"
                        "**两条都要过** —— 一条轴上的高分**不能**"
                        "去补另一条轴上的低分。",
                        "Lower bound of the second-moment aspect ratio, i.e. the **other half** of this conjunction. It catches plain elongation - which the r(theta) dispersion sees less clearly; conversely, shapes like lobes and notches that the second moment judges a perfect circle are caught by the dispersion. **Both must pass** - a high score on one axis **cannot** make up for a low score on the other.")
                    ),
                    required=False,
                    default=0.6,
                    min_value=0.0,
                    max_value=1.0,
                ),
            ],
            estimated_duration_s=1.0,
            composition_level=2,
            tags=["scan", "analysis", "cluster", "roundness", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        scan_path = params["scan_path"]
        threshold_sigma = float(params.get("threshold_sigma", 1.5))
        polarity = (params.get("polarity") or "auto").lower()
        channel_name = params.get("channel", "Z")
        threshold_mode = (params.get("threshold_mode") or "sigma").lower()
        shape_mode = (params.get("shape_mode") or "boundary").lower()
        #: 半个 Au(111) 单原子台阶(235.4 pm = a/√3)。**外部标尺**,不是拟合出来的。
        physical_threshold_m = float(
            params.get("physical_threshold_pm", 117.7)) * 1e-12
        if threshold_mode not in ("sigma", "physical"):
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error=tr(f"threshold_mode 只能是 sigma / physical,收到 {threshold_mode!r}", f"threshold_mode must be sigma / physical, received {threshold_mode!r}"))
        if shape_mode not in ("boundary", "weighted"):
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error=tr(f"shape_mode 只能是 boundary / weighted,收到 {shape_mode!r}", f"shape_mode must be boundary / weighted, received {shape_mode!r}"))

        # ── `round_threshold` **作废**,而且不做静默别名 ──────────────────
        #
        # 旧参数比的是 `0.6*circularity + 0.4*aspect`,那个标度上**完美圆盘最高只有
        # 0.770、轴对齐正方形能拿 0.871**,而阈值是 0.65。新参数比的是等效轴比,
        # 完美圆盘 = 1.0。两个数**方向相同、量纲不同** —— 正因为方向相同,
        # 一个漏改的 0.65 会**照跑不误**并悄悄把闸门放宽到「长短轴差 35%」。
        # 所以这里当场报错,把处方一起给,而不是默默接受。
        if params.get("round_threshold") is not None:
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error=(tr("`round_threshold` 已作废:它比的是 "
                       "`0.6*circularity + 0.4*aspect`,而那个 circularity = "
                       "4πA/P² 在像素化边界上的**上确界只有 0.617**(轴对齐正方形却是 "
                       "0.785)—— 阈值 0.65 卡在两者之间,**圆的一律不合格、"
                       "方的一律合格**。圆形团簇因此被判否。"
                       "请改用 `min_axis_ratio`(等效轴比,完美圆 = 1.0;"
                       "0.75 = 「不比长短轴差 25% 的椭圆更不规则」)。"
                       "**两个数不可互换**:直接把 0.65 填进 min_axis_ratio "
                       "等于把闸门放宽到「长短轴差 35%」。",
                       "`round_threshold` is obsolete: it compared `0.6*circularity + 0.4*aspect`, and that circularity = 4πA/P² has an **upper bound of only 0.617** on a pixelated boundary (an axis-aligned square gets 0.785) - a threshold of 0.65 sits between the two, **so every circle fails and every square passes**. Round clusters were rejected because of this. Use `min_axis_ratio` instead (equivalent axis ratio, perfect circle = 1.0; 0.75 = 'no more irregular than an ellipse whose axes differ by 25%'). **The two numbers are not interchangeable**: putting 0.65 straight into min_axis_ratio loosens the gate to 'axes differing by 35%'.")),
                data={"retired_parameter": "round_threshold",
                      "use_instead": "min_axis_ratio",
                      "suggested": {"min_axis_ratio": 0.75, "min_aspect": 0.6},
                      "why_not_a_silent_alias":
                          tr("两者方向相同(都是越大越圆),所以一个漏改的旧值会照跑不误", "They point the same way (both: larger = rounder), so a forgotten old value would run without complaint")})

        min_axis_ratio = float(params.get("min_axis_ratio", 0.75))
        min_aspect = float(params.get("min_aspect", 0.6))

        if not Path(scan_path).exists():
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error=f"scan_path not found: {scan_path}",
            )

        try:
            import math

            import numpy as np
            from scipy.ndimage import label, find_objects
            from mast.io.nanonis_files import read_sxm
            from mast.data.processors import plane_subtract
            from mast.vision.frame_validity import judge_frame
        except ImportError as e:
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error=f"missing dependency: {e}",
            )

        try:
            scan = read_sxm(scan_path)
        except Exception as e:
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error=f"failed to read .sxm: {e}",
            )

        channels = scan.get("channels", {})
        ch = channels.get(channel_name) or next(iter(channels.values()), None)
        if ch is None:
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error="no usable channel",
            )
        img = ch.get("forward")
        if img is None:
            img = ch.get("backward")
        if img is None:
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error="no forward/backward frame",
            )
        img = np.asarray(img, dtype=np.float64)

        # 标准差非零不足以证明帧具有有效形貌信息，舍入图样也可能产生伪连通域。
        # 统一使用 mast.vision.frame_validity 的前置判据。
        verdict = judge_frame(img)
        if not verdict.usable:
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error=verdict.reason,
                data=verdict.meta(scan_path=scan_path,
                                  rows=int(img.shape[0]), cols=int(img.shape[1])),
            )

        try:
            leveled = plane_subtract(img)
        except Exception:
            leveled = img - np.nanmean(img)

        mean = float(np.nanmean(leveled))
        std = float(np.nanstd(leveled))
        if std == 0 or np.isnan(std):
            # 留着:上面的前置管「整帧没信息」,这一条管「plane_subtract 这条路
            # 自己退化了」。两者判的不是同一件事,而这条便宜。
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error="zero/NaN std — flat or unreadable scan",
                data=verdict.meta(scan_path=scan_path),
            )

        # 若阈值由整帧 σ 推导，强目标本身也会抬高阈值，产生自我影响。
        # 应区分背景估计与显式物理阈值，并报告实际采用的分割口径；默认参数未作样品标定。
        base = None
        if threshold_mode == "physical":
            from mast.vision.roundness import background_level

            base = background_level(leveled)
            if base is None:
                return SkillResult(
                    skill_name="AssessClusterRoundness", success=False,
                    error="physical threshold needs a background mode — frame too small",
                    data=verdict.meta(scan_path=scan_path),
                )
            delta = physical_threshold_m
            up_mask = leveled > base + delta
            dn_mask = leveled < base - delta
        else:
            up_mask = leveled > mean + threshold_sigma * std
            dn_mask = leveled < mean - threshold_sigma * std

        # polarity='auto' picks whichever extreme is bigger.
        if polarity == "auto":
            mask = up_mask if up_mask.sum() >= dn_mask.sum() else dn_mask
            chosen = "bright" if up_mask.sum() >= dn_mask.sum() else "dark"
        elif polarity == "dark":
            mask, chosen = dn_mask, "dark"
        else:
            mask, chosen = up_mask, "bright"

        if not mask.any():
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error=(
                    "no pixels exceeded threshold — lower threshold_sigma or "
                    "check the scan window covers the crater"
                ),
                data={"polarity_used": chosen, "mean": mean, "std": std},
            )

        labelled, n_components = label(mask)
        if n_components == 0:
            return SkillResult(
                skill_name="AssessClusterRoundness", success=False,
                error="segmentation produced 0 components",
            )
        sizes = np.bincount(labelled.ravel())
        sizes[0] = 0  # background
        # Select the intended component explicitly. A centered scan can use the
        # nearest-center component to avoid grading an older, larger feature.
        # Keep largest as the compatibility default; callers that know the target
        # location opt into center selection.
        select = str(params.get("select", "largest") or "largest").lower()
        if select == "center" and n_components > 1:
            cy0, cx0 = (mask.shape[0] - 1) / 2.0, (mask.shape[1] - 1) / 2.0
            best, best_d = 0, float("inf")
            for lab in range(1, n_components + 1):
                if sizes[lab] <= 0:
                    continue
                ys_l, xs_l = np.where(labelled == lab)
                d = float((ys_l.mean() - cy0) ** 2 + (xs_l.mean() - cx0) ** 2)
                if d < best_d:
                    best, best_d = lab, d
            chosen_label = best or int(sizes.argmax())
        else:
            chosen_label = int(sizes.argmax())
        blob = labelled == chosen_label
        area_px = int(blob.sum())

        # 圆度:**共用的那一份**(`mast.vision.roundness`),与 ExtractClusters
        # 同一个表达式、同一个标度。2026-08-11 之前这里有第二份 4πA/P²,
        # 而 ExtractClusters 里那份的口径在 08-10 已经改过 —— **同一个词、两个数**。
        from mast.vision.roundness import assess_mask

        rnd = assess_mask(blob)

        # Major/minor axes = **全长**(等效椭圆的 4σ),经**共用的那一份**。
        #
        # 2026-08-11:这里原来有第二份拷贝,和 `ExtractClusters._axes` 犯同一个错
        # (报 σ 当轴长,尺寸低报 4 倍)。两份同样的错误要修两次,而其中一份总会
        # 被漏掉 —— 所以顺手合并成一份,别只把数字改对。
        from mast.skills.builtins.cluster_extract import ExtractClusters

        ys, xs = np.where(blob)
        major, minor, aspect_ratio = ExtractClusters._axes(xs, ys, np)

        # 拉长与边界不规则是互补特征，使用合取避免加权平均掩盖其中一项失败。
        # 加权矩用于降低分割阈值对二阶矩的影响，但不能替代对分割结果的检查。
        from mast.vision.roundness import background_level, weighted_axis_ratio

        w_base = base if base is not None else background_level(leveled)
        w_axis = (weighted_axis_ratio(leveled, blob, w_base)
                  if w_base is not None else None)
        if chosen == "dark" and w_base is not None:
            # 暗侧:把高度翻过来再加权,否则权重全被 clip 成 0。
            w_axis = weighted_axis_ratio(2.0 * w_base - leveled, blob, w_base)

        undecidable = None if rnd.ok else rnd.reason
        axis_ratio = rnd.axis_ratio
        if shape_mode == "weighted":
            # 判决改由加权轴比驱动。它**判不了就是判不了** —— 不回退到边界离散,
            # 那会让「用了哪个算法」取决于数据,而报文只写一个 shape_mode。
            axis_ratio = w_axis
            undecidable = (None if w_axis is not None else
                           tr("高度加权二阶矩算不出来(有效像素不足)", "height-weighted second moment cannot be computed (too few valid pixels)"))
        if undecidable:
            is_round = None
        else:
            is_round = bool(axis_ratio >= min_axis_ratio
                            and aspect_ratio >= min_aspect)

        # 连通域数量或形状异常本身不能证明双针尖，既有结构和分割误差也会造成相似结果。
        # 缺少适合比较的同状态多方向图像时保留 None。面积单位需要按像素尺度换算。
        header = scan.get("header", {})
        try:
            w_m, h_m = (float(v) for v in str(header.get("scan_range", "1e-7 1e-7")).split()[:2])
            ny, nx = img.shape
            px_size_x = w_m / nx
            px_size_y = h_m / ny
        except Exception:
            px_size_x = px_size_y = float("nan")

        # ── 多针尖:**几坨体量相当的东西,离得不远** ─────────────────────────
        #
        # 只量 ``select`` 选中的**那一块**看不出多针尖:每个顶点各自扎出一坨
        # **正常的**簇,单看一坨,形状尺寸和好针尖没有任何区别(实测逐维完全
        # 重叠)。证据不在任何一坨里面,**在它们之间的关系里**。
        #
        # 判据不看形状,只问「有没有第二坨体量相当的东西,离得够近」:
        #   · 体量比 **25%** —— 一个顶点扎出 500 px、另一个 5 px,后者是碎屑不是顶点;
        #   · 间距 **≤ 6 nm** —— 针尖顶点间距只有几 nm。放宽到不限,误杀会上升
        #     而只多抓极少几张;收得太紧又会漏抓。6 nm 是这个权衡的拐点,
        #     **不是扫出来的最优值**。
        #
        # 一批盲标(界面不显示任何判据数字、顺序打乱、夹重复帧)上的成绩:
        #   边界轴比 <0.75 单独        漏掉的**全是多针尖**
        #   加上这一条                几乎全部抓到
        #   代价:能用帧的误杀略有增加
        # 取舍:多扔掉一点不要紧,大不了再扎一次;但不能把坏针尖当成好针尖。
        #
        # ⚠️ **要推翻这条,需要回答**:被误杀的能用帧里,有几张是「针尖其实
        # 只有一个顶点,只是画面里本来就有别的东西」?根治办法是扎针前后各扫一张
        # 做差(差值图里只有这次扎出来的东西)—— 暂未实现。
        #
        # ═══════════════════════════════════════════════════════════════════
        # 复核:上面那一问,在一批帧上**答出来了** —— 答案是「全是」
        # ═══════════════════════════════════════════════════════════════════
        #
        # 扎针后分析出多针尖时,分不清多出来的连通区是之前扎出来的
        # 坑还是真的多针尖。把一整跑的簇图从仪器上取回来,
        # 用**本技能自己**(同一组参数)复跑,与当时旁白逐字一致 ——
        # 所以下面的结论说的就是当时判的那一批:
        #
        #   · 判成 ``multi_tip=True`` 的那些帧上,「多出来的」连通块,
        #     全部是坑量级;它们到**这一跑自己记录过的旧落点**的距离
        #     多在几 nm 以内。
        #
        #   · 其中有几帧不是「距离像」,是**同一个物体**:
        #        先前在**同一个帧心**扫的那一帧(那个坑还没扎)—— **只有一坨**;
        #        扎出那个坑的那一帧里,坑的位置、面积与高度;
        #        后续某一帧里的第二坨,位置、面积与高度几乎相同
        #        —— 位置差很小、面积比与高度比都接近 1。
        #     而**扎出它的那一帧只有一坨**:若那时针尖真有两个相距几 nm 的
        #     顶点,那一帧本该是两坨。所以第二坨是坑,不是顶点。**不需要任何阈值。**
        #
        # ── 但**别**据此加一条「离旧落点近就不算」的判据 ────────────────────
        #
        # 这些距离要全收进来,容差得开到 ≥ 3 nm(扎针落点到坑质心本身就有
        # 一定的系统偏移:蠕变 + 旁白坐标取整)。而真顶点间距按**本判据自己**
        # 的定义可以到 ``MULTI_TIP_SPAN_NM`` = 6 nm —— 两个区间重叠,
        # 而且在已有数据里**没有一个确认的真多针尖正例**。没有正例的阈值不是阈值。
        #
        # ⇒ 治法在**上游**:``_tip_phases.flat_poke_sites`` 现在把这一跑的落点表
        #   经 ``exclude_used_spots`` 交给 ``FindFlatRegion``。
        #   排除半径 30 nm > 簇图视野 10 nm ⇒ 旧坑**不可能**再进画面。
        #   这是算术,不是阈值。上面那些帧之所以存在,是因为流程连续多针扎在
        #   同一位置一位不差 —— 而那本身是个 bug。
        multi_tip = None
        multi_tip_reason = None
        px_nm = float(px_size_x) * 1e9 if np.isfinite(px_size_x) else None
        if px_nm and px_nm > 0 and n_components >= 1:
            sizes = np.bincount(labelled.ravel())
            sizes[0] = 0
            biggest = int(sizes.argmax())
            keep = [k for k in range(1, n_components + 1)
                    if sizes[k] >= MULTI_TIP_SIZE_FRAC * sizes[biggest]]
            cent = {k: (float(np.where(labelled == k)[0].mean()),
                        float(np.where(labelled == k)[1].mean())) for k in keep}
            by, bx = cent[biggest]
            near = [k for k in keep
                    if math.hypot(cent[k][0] - by, cent[k][1] - bx) * px_nm
                    <= MULTI_TIP_SPAN_NM]
            multi_tip = bool(len(near) >= 2)
            if multi_tip:
                far = max(math.hypot(cent[k][0] - by, cent[k][1] - bx) * px_nm
                          for k in near)
                multi_tip_reason = None
                multi_tip_detail = (
                    tr(f"{len(near)} 坨体量相当(≥{MULTI_TIP_SIZE_FRAC:.0%} 最大块)"
                    f"的东西,最远 {far:.1f} nm(≤{MULTI_TIP_SPAN_NM:g} nm)",
                    f"{len(near)} objects of comparable size (>={MULTI_TIP_SIZE_FRAC:.0%} of the largest), the farthest {far:.1f} nm apart (<={MULTI_TIP_SPAN_NM:g} nm)"))
            else:
                multi_tip_detail = (
                    tr(f"只有 1 坨够格的东西(共 {int(n_components)} 个连通域,"
                    f"其余不足最大块的 {MULTI_TIP_SIZE_FRAC:.0%} 或超出 "
                    f"{MULTI_TIP_SPAN_NM:g} nm)",
                    f"Only 1 qualifying object (of {int(n_components)} connected components in total; the rest are below {MULTI_TIP_SIZE_FRAC:.0%} of the largest or beyond {MULTI_TIP_SPAN_NM:g} nm)"))
        else:
            # 像素尺寸读不到 ⇒ 间距算不出 ⇒ **判不了**,不是「没有多针尖」。
            multi_tip_detail = None
            multi_tip_reason = (
                tr("判不了:读不到像素物理尺寸(scan_range),算不出连通域之间的间距。", "Cannot judge: the physical pixel size (scan_range) cannot be read, so the spacing between connected components cannot be computed."))

        return SkillResult(
            skill_name="AssessClusterRoundness", success=True,
            data={
                "scan_path": scan_path,
                # 判决:三态。None = **判不了**(团簇太小),不是「不圆」。
                "is_round": is_round,
                "roundness_undecidable": undecidable,
                # 主输出:能直接读的物理量 ——「这个团簇的不规则程度相当于一个
                # 短轴/长轴 = q 的椭圆」。用户的用法是**比较相继几次哪次更圆**,
                # 所以这个连续量比 is_round 更重要。
                "equivalent_axis_ratio": axis_ratio,
                # 两个算法**都报**,并说清这一次是谁在判 —— 只报一个数而不说
                # 它从哪来,下一个人就没法对账(89 张对不上的教训)。
                "weighted_axis_ratio": w_axis,
                "boundary_axis_ratio": rnd.axis_ratio,
                "shape_mode": shape_mode,
                "threshold_mode": threshold_mode,
                "background_level_m": w_base,
                "threshold_above_background_m": (
                    physical_threshold_m if threshold_mode == "physical"
                    else (mean + threshold_sigma * std - w_base)
                    if w_base is not None else None),
                # 多针尖:**三态**,出厂就是判不了。见上面那段论证。
                "multi_tip": multi_tip,
                "multi_tip_undecidable": multi_tip_reason,
                "multi_tip_detail": multi_tip_detail,
                "multi_tip_size_frac": MULTI_TIP_SIZE_FRAC,
                "multi_tip_span_nm": MULTI_TIP_SPAN_NM,
                "radial_dispersion": rnd.dispersion,
                "radial_dispersion_floor": rnd.floor,
                "radial_dispersion_excess": rnd.excess,
                "aspect_ratio": aspect_ratio,
                "min_axis_ratio": min_axis_ratio,
                "min_aspect": min_aspect,
                "area_px": area_px,
                "major_axis_px": major,
                "minor_axis_px": minor,
                "polarity_used": chosen,
                "n_components": int(n_components),
                "pixel_size_x_m": px_size_x,
                "pixel_size_y_m": px_size_y,
            },
        )
