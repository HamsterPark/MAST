"""原子分辨的三个层次：判别、晶格测量与扫描器标定。

AssessAtomicResolution 使用 vision.atomic_phase 的角向集中度等判据，
区分离散倒空间峰与弥散纹理；单纯存在强谱峰不足以证明原子分辨。
AnalyseAtomicLattice 在通过判别后测量周期、六重对称与取向，可与已知表面比对。
CalibratePiezoFromLattice 还要求六重对称成立、方向间周期散布足够小；
单帧的非正交结果可能混有热漂移，需结合慢轴反向扫描进一步分离。

surface 参数选择参考表面，默认 Au(111)；其他参考来自
vision.lattice_calibration.SURFACE_LATTICE_NM。共用算法避免不同入口之间漂移。
"""
from __future__ import annotations

import logging
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


def _frame_conditions(path: str, channel: str = "Z") -> tuple[Any, Any]:
    """从 .sxm 头里取 ``(bias_v, setpoint_a)``；取不到就 ``(None, None)``。

    判读条件取自帧头，以便修针或改工作点之后仍按该帧实际采集条件解释结果。
    """
    try:
        from mast.io.nanonis_files import read_sxm, sxm_oriented_frames
        fr = sxm_oriented_frames(read_sxm(path), channel)
        return fr.get("bias_v"), fr.get("setpoint_a")
    except Exception:  # noqa: BLE001 — 读不到条件不该让判读整个失败
        return None, None


def _load_frame(path: str, channel: str) -> tuple[Any, Any, float | None, str]:
    """``(forward, backward, nm_per_px, error)``。走 ``sxm_oriented_frames``。

    **必须走它**：Nanonis 把 backward 镜像存储，把 ``:SCAN_DIR: up`` 的帧上下
    翻转存储。不还原就拿去比正反扫，比的是一张图和它自己的镜像 —— 而镜像会
    翻转剪切的符号，产生虚假的「漂移 vs 压电」分离。
    """
    from pathlib import Path

    if not Path(path).exists():
        return None, None, None, tr(f"文件不存在: {path}", f"File does not exist: {path}")
    try:
        import numpy as np

        from mast.io.nanonis_files import read_sxm, sxm_oriented_frames
    except ImportError as exc:  # noqa: BLE001
        return None, None, None, tr(f"缺依赖: {exc}", f"Missing dependency: {exc}")
    try:
        scan = read_sxm(path)
    except Exception as exc:  # noqa: BLE001
        return None, None, None, tr(f".sxm 读取失败: {exc}", f".sxm read failed: {exc}")
    fr = sxm_oriented_frames(scan, channel)
    f = fr.get("forward")
    if f is None:
        return None, None, None, tr(f"文件里没有通道 {channel!r} 的正扫数据", f"The file has no forward-scan data for channel {channel!r}")
    f = np.asarray(f, dtype=np.float64)
    b = fr.get("backward")
    b = (np.asarray(b, dtype=np.float64)
         if b is not None and np.asarray(b).shape == f.shape else None)
    nmpp = fr.get("nm_per_px")
    return f, b, (float(nmpp) if nmpp else None), ""


def _coverage(arr) -> float:
    import numpy as np
    a = np.asarray(arr)
    return float(np.isfinite(a).mean()) if a.size else 0.0


class AssessAtomicResolution(BaseSkill):
    """这一帧上有没有原子分辨。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AssessAtomicResolution",
            version="1.0.0",
            category=SkillCategory.ANALYSIS,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("判断一张已保存的 .sxm 帧上有没有原子分辨。只读，不碰仪器。"
                "核心判据是**角向集中度** —— 真晶格在倒空间是离散布拉格点、"
                "针尖抖动是弥散环（真晶格的角向集中度远高于带通抖动）。"
                "「FFT 里有个峰」不够：峰强度类判据分不开准周期抖动，多个抖动"
                "种子全部产生了合格谱峰。"
                "verdict 三态：atomic / absent / undetermined。**undetermined 是"
                "「这一帧回答不了」**（像素太粗、视场太大、帧不完整），不是"
                "「针尖不好」—— 换个尺度重扫，别据此去修针。",
                "Judge whether a saved .sxm frame shows atomic resolution. Read-only, does not touch the instrument. The core criterion is the **angular concentration**: a true lattice is a set of discrete Bragg points in reciprocal space, while tip jitter is a diffuse ring (a true lattice scores far above band-passed jitter). 'There is a peak in the FFT' is not enough: peak-strength criteria cannot separate quasi-periodic jitter, and all of the jitter seeds produced qualifying spectral peaks. The verdict has three states: atomic / absent / undetermined. **undetermined means 'this frame cannot answer'** (pixels too coarse, field of view too large, frame incomplete), not 'the tip is bad' - rescan at another scale, do not condition the tip on this basis.")
            ),
            parameters=[
                ParameterSpec(name="scan_path", type="str", required=True,
                              description=tr("要判的 .sxm 帧路径。", "Path of the .sxm frame to judge.")),
                ParameterSpec(name="channel", type="str", required=False,
                              default="Z",
                              description=tr("形貌通道，通常是 'Z'。", "Topography channel, usually 'Z'.")),
                ParameterSpec(
                    name="surface", type="str", required=False, default="",
                    description=(tr("已知表面名（如 'Au(111)'），用来把测到的周期"
                                 "与理论值比对。留空则只报测量值不做比对。",
                                 "Known surface name (e.g. 'Au(111)'), used to compare the measured period with the theoretical value. If empty, only the measured value is reported, with no comparison."))),
                # 角向集中度检查谱峰是否集中在离散方向，补充峰强度无法区分的准周期纹理。
                # 默认值是算法工作点；用于不同成像条件时应结合数据验证，不能当成通用标定结论。
                ParameterSpec(
                    name="concentration_min", type="float", required=False,
                    default=None, min_value=1.0, max_value=1e5,
                    description=tr("角向集中度下限；不填用模块默认", "Lower limit of the angular concentration; if omitted the module default is used"),
                ),
                ParameterSpec(
                    name="allow_reduced_scale", type="bool", required=False,
                    default=False,
                    description=(tr("放宽尺度门：0.02–0.05 nm/px 的帧也允许给正面"
                                 "结论。默认不放 —— 在那个尺度上「有原子相」"
                                 "这句话的证据强度撑不住一次针尖验收。",
                                 "Relax the scale gate: frames at 0.02-0.05 nm/px may also give a positive conclusion. Off by default - at that scale the evidence for 'atomic phase present' is not strong enough to accept a tip."))),
            ],
            estimated_duration_s=3.0,
            composition_level=0,
            tags=["atomic", "resolution", "fft", "analysis", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "AssessAtomicResolution"
        path = str(params["scan_path"])
        ch = str(params.get("channel") or "Z")
        f, b, nmpp, err = _load_frame(path, ch)
        if err:
            return SkillResult(skill_name=name, success=False, error=err)

        from mast.vision.atomic_phase import assess_atomic_phase
        from mast.vision.lattice_calibration import first_order_period_nm

        surface = str(params.get("surface") or "").strip()
        expect = first_order_period_nm(surface) if surface else None
        cov = _coverage(f)
        _cmin = params.get("concentration_min")
        _kw = {} if _cmin in (None, "") else {"concentration_min": float(_cmin)}
        res = assess_atomic_phase(
            f, nm_per_px=nmpp, expected_a_nm=expect,
            allow_reduced_scale=bool(params.get("allow_reduced_scale", False)),
            **_kw)

        # 正扫过了但反扫没过 = 结构只在一个方向上出现 = 多半不是表面结构。
        res_b = None
        if b is not None:
            res_b = assess_atomic_phase(
                b, nm_per_px=nmpp, expected_a_nm=expect,
                allow_reduced_scale=bool(params.get("allow_reduced_scale", False)), **_kw)

        verdict = "atomic" if res.passed else "absent"
        notes: list[str] = []

        # 成像条件不足时当前帧无法回答原子分辨问题，应返回 undetermined。
        # 不能把工作点或采样条件导致的不可判定解释为晶格缺失，更不能据此推断需要修针。
        from mast.vision.imaging_window import check_atomic_window
        _bias, _sp = _frame_conditions(path, ch)
        _win = check_atomic_window(_bias, _sp, surface or None)
        if not _win.ok:
            verdict = "undetermined"
            notes.append(_win.detail_zh)

        if not res.passed and any(r in ("unknown_pixel_size", "scale_gate",
                                        "insufficient_data", "scale_reduced",
                                        "too_few_periods")
                                  for r in res.reasons):
            verdict = "undetermined"
            notes.append(tr("这一帧回答不了（%s），换尺度或扫完整再判，"
                         "**不要读成「针尖不好」**",
                         "This frame cannot answer (%s); rescan at another scale or complete the frame before judging, **do not read it as 'the tip is bad'**") % ",".join(res.reasons))
        if cov < 0.5:
            verdict = "undetermined"
            notes.append(tr("只有 %.0f%% 的像素有数据 —— 扫了几行就停的帧判不了", "Only %.0f%% of the pixels have data - a frame stopped after a few rows cannot be judged") % (100 * cov))
        if res.passed and res_b is not None and not res_b.passed:
            notes.append(tr("反扫没通过（reasons=%s）—— 结构只在一个扫描方向上出现，"
                         "多半是针尖或反馈的产物，不是表面结构",
                         "The backward scan did not pass (reasons=%s) - the structure appears in only one scan direction, most likely an artifact of the tip or feedback, not surface structure") % (res_b.reasons,))

        data = {
            "verdict": verdict,
            "passed_forward": bool(res.passed),
            "passed_backward": (bool(res_b.passed) if res_b else None),
            "scan_path": path, "channel": ch,
            "nm_per_px": res.nm_per_px, "scale_gate": res.scale,
            "coverage": cov,
            "period_fast_axis_nm": res.period_fast_axis_nm,
            "period_radial_nm": res.period_nm,
            "snr": res.snr,
            "angular_concentration": res.angular_concentration,
            "fft_sharpness": res.fft_sharpness,
            "order_ratio": res.order_ratio,
            "expected_period_nm": expect, "surface": surface or None,
            "reasons": list(res.reasons), "warnings": list(res.warnings) + notes,
            # 成像条件的结论要**机器可读**：只放在 warnings 里，调用方就得靠
            # 匹配中文散文才能知道「这一帧为什么判不了」。
            "imaging_window": {
                "ok": _win.ok, "reason": _win.reason,
                "bias_v": _win.bias_v, "setpoint_a": _win.setpoint_a,
                "bias_max_v": _win.bias_max_v,
                "setpoint_min_a": _win.setpoint_min_a,
            },
        }
        summary = (tr("%s：角向集中度 %.0f（真晶格 97-7645 / 抖动 1.8-3.3）", "%s: angular concentration %.0f (true lattice 97-7645 / jitter 1.8-3.3)")
                   % (verdict, res.angular_concentration))
        return SkillResult(skill_name=name, success=True, data=data, summary=summary)


#: 「只是尺度不够」的出局词。落在这一集里 ⇒ 这一帧**没被判成没有原子相**，
#: 只是像素太粗，判据不肯在这个证据强度上签字。两者要人做的事完全不同：
#: 前者去修针，后者换个尺度重扫、或明说「我知道，继续」。
#:
#: ⚠️ 与 :data:`mast.vision.atomic_phase.REASONS` 是子集关系。那边加新出局词时
#: 要想一想属不属于这里 —— 漏判的方向是安全的（照旧拒绝），错判进来才危险。
_SCALE_ONLY_REASONS = frozenset({"scale_gate", "scale_reduced", "unknown_pixel_size"})


class AnalyseAtomicLattice(BaseSkill):
    """晶格周期、六重对称、取向；可与已知表面比对。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AnalyseAtomicLattice",
            version="1.0.0",
            category=SkillCategory.ANALYSIS,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("从一张原子分辨的 .sxm 帧里量出晶格：三个方向的周期、六重对称"
                "是否成立、晶格取向；给了 surface 就与理论值比对并报偏差。"
                "只读。**默认 surface='Au(111)'，所以不填参数时它就是 Au(111) "
                "的专用分析；填别的表面名就是通用的**（表在 "
                "vision.lattice_calibration.SURFACE_LATTICE_NM）。"
                "⚠️ 报的周期是**二维谱峰位置**给的，不是一维快扫投影 —— 后者"
                "取决于晶格与扫描方向的夹角，同一块样品换个角度扫就变。"
                "⚠️ 三个方向的周期**本该相等**，不等的程度就是扫描器的畸变，"
                "要定标去用 CalibratePiezoFromLattice。",
                "Measure the lattice from an atomically resolved .sxm frame: the period in three directions, whether sixfold symmetry holds, and the lattice orientation; if a surface is given, compare with the theoretical value and report the deviation. Read-only. **The default is surface='Au(111)', so with no parameters this is a dedicated Au(111) analysis; give another surface name and it is general** (table in vision.lattice_calibration.SURFACE_LATTICE_NM). WARNING: the reported period comes from **2D spectral peak positions**, not a 1D fast-scan projection - the latter depends on the angle between lattice and scan direction and changes if the same sample is scanned at another angle. WARNING: the three directional periods **should be equal**; how far they differ is the scanner distortion; to calibrate, use CalibratePiezoFromLattice.")
            ),
            parameters=[
                ParameterSpec(name="scan_path", type="str", required=True,
                              description=tr("要分析的 .sxm 帧路径。", "Path of the .sxm frame to analyse.")),
                ParameterSpec(name="channel", type="str", required=False,
                              default="Z", description=tr("形貌通道。", "Topography channel.")),
                ParameterSpec(
                    name="surface", type="str", required=False,
                    default="Au(111)",
                    description=(tr("表面名。默认 Au(111)。传 'none' 则只报测量值、"
                                 "不与任何理论值比对。",
                                 "Surface name. Default Au(111). Pass 'none' to report only the measured value, without comparing to any theoretical value."))),
                ParameterSpec(
                    name="allow_reduced_scale", type="bool", required=False,
                    default=False,
                    description=(tr("把同名开关透传给前置的原子相判别。**没有它，"
                                 "0.02–0.05 nm/px 的帧只剩「整道关闸」一条路** "
                                 "—— 而常用的 8 nm/256 px 正是 "
                                 "0.03125 nm/px，每一帧都落在里面。用了它，"
                                 "`scale_reduced` 会作为警告跟着结果走，"
                                 "而不是被拿去换掉整道闸。",
                                 "Pass the same-named switch through to the preceding atomic-phase check. **Without it, frames at 0.02-0.05 nm/px have only one option, 'turn the whole gate off'** - yet the common 8 nm/256 px is exactly 0.03125 nm/px, so every frame falls inside. With it, `scale_reduced` goes along with the result as a warning, instead of being traded for the whole gate."))),
                ParameterSpec(
                    name="require_atomic", type="bool", required=False,
                    default=True,
                    description=(tr("先跑原子相判别，没通过就拒绝出数。默认开 —— "
                                 "在没有原子相的帧上量周期，量到的是针尖抖动的"
                                 "周期，而那个数看起来完全正常。",
                                 "Run the atomic-phase check first and refuse to give numbers if it fails. On by default - measuring the period on a frame without atomic phase gives the period of tip jitter, and that number looks perfectly normal."))),
            ],
            estimated_duration_s=4.0,
            composition_level=0,
            tags=["atomic", "lattice", "fft", "analysis", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "AnalyseAtomicLattice"
        path = str(params["scan_path"])
        ch = str(params.get("channel") or "Z")
        f, b, nmpp, err = _load_frame(path, ch)
        if err:
            return SkillResult(skill_name=name, success=False, error=err)

        from mast.vision.atomic_phase import assess_atomic_phase
        from mast.vision.lattice_calibration import (
            find_lattice_peaks, first_order_period_nm,
        )

        surface = str(params.get("surface") or "Au(111)").strip()
        if surface.lower() in ("none", "无", ""):
            surface = ""
        expect = first_order_period_nm(surface) if surface else None
        if surface and expect is None:
            return SkillResult(
                skill_name=name, success=False,
                error=(tr("不认识表面 %r。已知的：%s。也可以传 'none' 只测不比。", "Unknown surface %r. Known: %s. You can also pass 'none' to measure only, without comparing.")
                       % (surface, ", ".join(sorted(
                           __import__("mast.vision.lattice_calibration",
                                      fromlist=["x"]).SURFACE_LATTICE_NM)))))

        allow_reduced = bool(params.get("allow_reduced_scale", False))
        pre = None
        if bool(params.get("require_atomic", True)):
            pre = assess_atomic_phase(f, nm_per_px=nmpp,
                                      allow_reduced_scale=allow_reduced)
            if not pre.passed:
                # 尺度不足时应返回无法判定，而不是把采样限制解释为针尖伪影。
                # 同样不能因此绕过整个原子相检测；需要更合适的视场或像素密度。
                only_scale = all(r in _SCALE_ONLY_REASONS for r in pre.reasons)
                if only_scale:
                    tail = (tr("这**不是**「没有原子相」——角向集中度 %.1f"
                            "（真晶格 97–7645 / 抖动 1.8–3.3）。要在这个尺度上"
                            "出数，传 allow_reduced_scale=true："
                            "警告会跟着结果一起给出，而角向集中度那道闸仍然在。",
                            "This is **not** 'no atomic phase' - angular concentration %.1f (true lattice 97-7645 / jitter 1.8-3.3). To get numbers at this scale, pass allow_reduced_scale=true: a warning accompanies the result, and the angular-concentration gate still applies.")
                            % pre.angular_concentration)
                else:
                    tail = (tr("在它上面量出来的周期是针尖抖动的周期。要强行量就把"
                            "require_atomic 设 false，但那个数不要拿去定标。",
                            "The period measured on it is the period of tip jitter. To force the measurement set require_atomic to false, but do not use that number for calibration."))
                return SkillResult(
                    skill_name=name, success=False,
                    error=(tr("这一帧没有通过原子相判别（%s，角向集中度 %.1f）——%s", "This frame did not pass the atomic-phase check (%s, angular concentration %.1f) - %s")
                           % (",".join(pre.reasons) or "-",
                              pre.angular_concentration, tail)),
                    data={"angular_concentration": pre.angular_concentration,
                          "reasons": list(pre.reasons),
                          "scale_only": only_scale})

        lat = find_lattice_peaks(f, nmpp)
        if not lat.ok:
            return SkillResult(skill_name=name, success=False,
                               error=tr("量不了晶格：%s", "Cannot measure the lattice: %s") % lat.reason,
                               data={"reason": lat.reason,
                                     "warnings": list(lat.warnings)})

        dev = None
        if expect and lat.period_mean_nm:
            dev = lat.period_mean_nm / expect - 1.0
        lat_b = find_lattice_peaks(b, nmpp) if b is not None else None

        data = {
            "scan_path": path, "channel": ch, "nm_per_px": nmpp,
            "surface": surface or None, "expected_period_nm": expect,
            "hexagonal": lat.hexagonal,
            "periods_nm": list(lat.periods_nm),
            "period_mean_nm": lat.period_mean_nm,
            "period_spread": lat.period_spread,
            "deviation_from_expected": dev,
            "angles_deg": list(lat.angles_deg),
            "lattice_angle_deg": lat.lattice_angle_deg,
            "n_peaks": lat.n_peaks,
            "backward_period_mean_nm": (lat_b.period_mean_nm if lat_b and lat_b.ok else None),
            "warnings": list(lat.warnings) + (
                # 用了尺度逃生门就必须**说出来**，而且是跟着数走 —— 一个
                # 0.03 nm/px 的帧上量到的周期，和一个 0.01 nm/px 上量到的，
                # 不该在结果里长得一模一样。
                [tr("scale_reduced：像素尺度在过渡带 (0.02, 0.05] nm/px，"
                 "这个周期的证据强度撑不住一次定标，够用来看趋势",
                 "scale_reduced: the pixel scale is in the transition band (0.02, 0.05] nm/px; the evidence for this period is not strong enough to support a calibration, but is good enough to see trends")]
                if (allow_reduced and pre is not None
                    and "scale_reduced" in pre.warnings) else []),
            "scale_gate": (pre.scale if pre is not None else None),
        }
        s = tr("周期 %.4f nm（三向散布 %.1f%%），六重=%s", "Period %.4f nm (three-direction spread %.1f%%), sixfold=%s") % (
            lat.period_mean_nm or 0, 100 * (lat.period_spread or 0), lat.hexagonal)
        if dev is not None:
            s += tr("，比 %s 理论值 %+.2f%%", ", versus the %s theoretical value %+.2f%%") % (surface, 100 * dev)
        return SkillResult(skill_name=name, success=True, data=data, summary=s)


class CalibratePiezoFromLattice(BaseSkill):
    """由原子分辨图反推 XY 压电的尺度因子。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="CalibratePiezoFromLattice",
            version="1.0.0",
            category=SkillCategory.ANALYSIS,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("拿一张原子分辨图当尺子，反推 XY 压电的尺度因子与扫描的非正交。"
                "只读、只**建议**，不写任何仪器参数 —— 改压电常数是用户的决定。"
                "输出 x_scale/y_scale = 实际尺度÷标称：1.10 意味着仪器实际扫过的"
                "范围比它以为的大 10%，量出来的长度都偏小 10%，压电常数该乘 1.10。"
                "⚠️ **非正交那一项里混着慢轴热漂移，单一慢轴方向上分不开** —— "
                "要拆开就再扫一帧 SCAN_DIR 反向的，漂移随慢轴翻号、压电不随。"
                "⚠️ 单帧结论不要直接写进仪器：换扫描角、换视场各测一次，"
                "尺度因子该是同一个数。",
                "Use an atomically resolved image as a ruler to infer the XY piezo scale factors and the scan non-orthogonality. Read-only and **advice only**, writes no instrument parameter - changing piezo constants is the user's decision. Output x_scale/y_scale = actual scale / nominal: 1.10 means the range the instrument really scanned is 10% larger than it thought, all measured lengths are 10% too small, and the piezo constant should be multiplied by 1.10. WARNING: **the non-orthogonality term is mixed with slow-axis thermal drift, which cannot be separated with a single slow-axis direction** - to separate them, scan one more frame with SCAN_DIR reversed; drift flips sign with the slow axis, the piezo does not. WARNING: do not write a single-frame result straight into the instrument: measure once each at a different scan angle and field of view; the scale factors should be the same number.")
            ),
            parameters=[
                ParameterSpec(name="scan_path", type="str", required=True,
                              description=tr("原子分辨的 .sxm 帧。", "Atomically resolved .sxm frame.")),
                ParameterSpec(name="channel", type="str", required=False,
                              default="Z", description=tr("形貌通道。", "Topography channel.")),
                ParameterSpec(name="surface", type="str", required=False,
                              default="Au(111)",
                              description=tr("已知晶格的表面，用作长度基准。", "Surface with a known lattice, used as the length reference.")),
                ParameterSpec(
                    name="max_period_spread", type="float", required=False,
                    default=0.25,
                    description=(tr("三方向周期相对散布的上限，超过就拒绝定标。"
                                 "散布大意味着不是单一晶格（moiré/双畴/多重针尖），"
                                 "或畸变已超出线性模型 —— 两种情况下拟合都能算出"
                                 "一个数，但那个数没有意义。",
                                 "Upper limit of the relative spread of the three-direction periods; above it, calibration is refused. A large spread means it is not a single lattice (moire/twin domains/multiple tips), or the distortion exceeds the linear model - in both cases the fit still produces a number, but the number is meaningless."))),
            ],
            estimated_duration_s=5.0,
            composition_level=0,
            tags=["atomic", "lattice", "piezo", "calibration", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        name = "CalibratePiezoFromLattice"
        path = str(params["scan_path"])
        ch = str(params.get("channel") or "Z")
        f, b, nmpp, err = _load_frame(path, ch)
        if err:
            return SkillResult(skill_name=name, success=False, error=err)

        from mast.vision.atomic_phase import assess_atomic_phase
        from mast.vision.lattice_calibration import (
            calibrate_forward_backward, calibrate_from_lattice,
        )

        surface = str(params.get("surface") or "Au(111)").strip()
        pre = assess_atomic_phase(f, nm_per_px=nmpp)
        if not pre.passed:
            return SkillResult(
                skill_name=name, success=False,
                error=(tr("这一帧没有通过原子相判别（%s）——不能拿它当尺子。", "This frame did not pass the atomic-phase check (%s) - it cannot be used as a ruler.")
                       % (",".join(pre.reasons) or tr("角向集中度 %.1f", "angular concentration %.1f") % pre.angular_concentration)))

        spread = float(params.get("max_period_spread", 0.25) or 0.25)
        if b is not None:
            r = calibrate_forward_backward(f, b, nmpp, surface)
            if not r.ok:
                return SkillResult(skill_name=name, success=False,
                                   error=tr("定标失败：%s", "Calibration failed: %s") % r.reason,
                                   data={"warnings": list(r.warnings)})
            data = {
                "scan_path": path, "surface": surface, "nm_per_px": nmpp,
                "x_scale": r.x_scale, "y_scale": r.y_scale,
                "x_correction_pct": (r.x_scale - 1) * 100,
                "y_correction_pct": (r.y_scale - 1) * 100,
                "shear_deg": r.shear_deg,
                "shear_attribution": tr("压电几何 + 慢轴热漂移之和，单慢轴方向分不开", "Sum of piezo geometry + slow-axis thermal drift; cannot be separated with a single slow-axis direction"),
                "forward_backward_scale_agreement": r.scale_agreement,
                "shear_disagreement_deg": r.shear_disagreement_deg,
                "used": "forward+backward",
                "warnings": list(r.warnings),
            }
            s = (tr("X x%.4f (%+.2f%%)，Y x%.4f (%+.2f%%)，剪切 %+.2f°（归属未定）", "X x%.4f (%+.2f%%), Y x%.4f (%+.2f%%), shear %+.2f deg (attribution undetermined)")
                 % (r.x_scale, (r.x_scale - 1) * 100, r.y_scale,
                    (r.y_scale - 1) * 100, r.shear_deg))
        else:
            c = calibrate_from_lattice(f, nmpp, surface, max_spread=spread)
            if not c.ok:
                return SkillResult(skill_name=name, success=False,
                                   error=tr("定标失败：%s", "Calibration failed: %s") % c.reason,
                                   data={"warnings": list(c.warnings)})
            data = {
                "scan_path": path, "surface": surface, "nm_per_px": nmpp,
                "x_scale": c.x_scale, "y_scale": c.y_scale,
                "x_correction_pct": (c.x_scale - 1) * 100,
                "y_correction_pct": (c.y_scale - 1) * 100,
                "shear_deg": c.nonorthogonality_deg,
                "period_spread_before": c.spread_before,
                "period_spread_after": c.spread_after,
                "lattice_angle_deg": c.lattice_angle_deg,
                "used": tr("forward only（没有反扫，少一道自检）", "forward only (no backward scan, one fewer self-check)"),
                "warnings": list(c.warnings),
            }
            s = (tr("X x%.4f，Y x%.4f（只有正扫）", "X x%.4f, Y x%.4f (forward scan only)") % (c.x_scale, c.y_scale))
        data["applied"] = False
        data["note"] = (tr("本技能只给建议，不写仪器。要落实就去改 XY 压电常数，"
                        "改完重扫一张验证周期回到理论值。",
                        "This skill only gives advice and writes nothing to the instrument. To apply it, change the XY piezo constants, then rescan one frame to verify the period returns to the theoretical value."))
        return SkillResult(skill_name=name, success=True, data=data, summary=s)


__all__ = ["AssessAtomicResolution", "AnalyseAtomicLattice",
           "CalibratePiezoFromLattice"]
