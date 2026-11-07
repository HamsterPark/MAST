"""AnalyzeFrameTilt —— 从一张已保存的 .sxm 量倾斜、台阶主导与表面起伏(只读)。

设计文档:``docs/v2/design/scan_intelligence_scripted_rfc.md`` §5.1-5.2

``mast.vision.tilt`` 里的帧法判据(``estimate_tilt`` / ``assess_steps``)一直没有
agent 可调的入口 —— 于是两件事做不了:

1. **AutoTilt 的 ``surface_rms_m`` 没人能算**。那个参数是「斜坡有没有把形貌淹没」
   这条触发判据的分母,也就是「扫出的图明显是倾斜的」。缺了它,AutoTilt
   只剩下「吃掉多少 Z 量程」这一条安全判据,而后者在大多数机器上要很大的倾斜才
   触发。
2. **真机上的阈值重标定没有数据来源**。台阶主导比的 1.4 是在合成数据上定的;要知道
   它在真表面上成不成立,得先能在真帧上把这个比值算出来。

本技能只读文件、不碰硬件。

**它给出的倾斜只有快扫方向可信** —— 一帧 512 线 × 2 s/线要扫半个多小时,慢扫轴上
图像顶部与底部相隔那么久,期间的热漂移会原样表现为视在倾斜,与真实倾斜无法区分。
要两个方向都可信,用 ``TiltProbeCircle``(恒流内接圆,几秒跑完)。
"""

from __future__ import annotations

import logging
from pathlib import Path

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


class AnalyzeFrameTilt(BaseSkill):
    """Measure tilt, step dominance and surface roughness from a saved .sxm."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="AnalyzeFrameTilt",
            version="1.0.0",
            category=SkillCategory.ANALYSIS,
            safety_level=SafetyLevel.AUTO,
            description=(
                tr("量出样品倾斜、台阶是不是主导了高度分布、以及表面自身的粗糙度 —— "
                "全都只从一份已保存的 .sxm 文件里读。只读,不碰硬件。"
                "用它来拿到 AutoTilt 需要的 surface_rms_m(也就是「斜坡有没有把形貌"
                "淹没」那条判据),以及在决定要不要调平之前先看看图是不是肉眼可见地"
                "倾斜。**注意**:它报出来的倾斜**只有沿快扫轴那一个方向可信**:"
                "慢扫轴横跨整帧的采集时长,那个方向上的热漂移与真实倾斜无法区分。"
                "要两个方向都可信的倾斜测量,用 TiltProbeCircle。", "Measure the sample tilt, whether steps dominate the height distribution, and the surface's own roughness -- "
"all read only from a saved .sxm file. Read-only, never touches hardware. "
"Use it to get the surface_rms_m that AutoTilt needs (the criterion for \"has the slope "
"swamped the topography\"), and to check whether the image is visibly tilted before deciding to level. "
"**Note**: the tilt it reports is **trustworthy only along the fast-scan axis**: "
"the slow axis spans the acquisition time of the whole frame, and in that direction thermal drift cannot be told from real tilt. "
"For a tilt measurement trustworthy in both directions, use TiltProbeCircle.")
            ),
            parameters=[
                ParameterSpec(
                    name="scan_path",
                    type="str",
                    description=tr("要分析的 .sxm 文件路径。", "Path of the .sxm file to analyse."),
                    required=True,
                ),
                ParameterSpec(
                    name="channel",
                    type="str",
                    description=tr("形貌通道('Z' 是标准选择)。", "Topography channel ('Z' is the standard choice)."),
                    required=False,
                    default="Z",
                ),
                ParameterSpec(
                    name="check_steps",
                    type="bool",
                    description=(
                        tr("跑「台阶主导」这道否决闸。台阶主导的时候,倾斜拟合量到的"
                        "是台阶包络而不是表面,所以倾斜会被报成**无效**,而不是给出"
                        "一个数。只有在想看原始拟合结果时才关掉它。", "Run the \"step-dominated\" veto gate. When steps dominate, the tilt fit measures "
"the step envelope rather than the surface, so the tilt is reported as **invalid** instead of giving "
"a number. Turn it off only when you want to see the raw fit result.")
                    ),
                    required=False,
                    default=True,
                ),
            ],
            estimated_duration_s=3.0,
            composition_level=2,
            tags=["scan", "tilt", "analysis", "read"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        scan_path = str(params["scan_path"])
        channel_name = params.get("channel") or "Z"
        check_steps = bool(params.get("check_steps", True))

        if not Path(scan_path).exists():
            return SkillResult(
                skill_name="AnalyzeFrameTilt", success=False,
                error=tr(f"文件不存在: {scan_path}", f"File does not exist: {scan_path}"))

        try:
            import numpy as np

            from mast.io.mosaic import parse_xy_meta
            from mast.io.nanonis_files import read_sxm
            from mast.vision.tilt import (
                detrend_quadratic,
                estimate_tilt,
                noise_floor,
                step_dominance_multiscale,
                structure_dominance,
            )
        except ImportError as exc:
            return SkillResult(skill_name="AnalyzeFrameTilt", success=False,
                               error=tr(f"缺依赖: {exc}", f"Missing dependency: {exc}"))

        try:
            scan = read_sxm(scan_path)
        except Exception as exc:  # noqa: BLE001
            return SkillResult(skill_name="AnalyzeFrameTilt", success=False,
                               error=tr(f".sxm 读取失败: {exc}", f"Failed to read .sxm: {exc}"))

        channels = scan.get("channels", {}) or {}
        ch = channels.get(channel_name) or next(iter(channels.values()), None)
        if ch is None:
            return SkillResult(
                skill_name="AnalyzeFrameTilt", success=False,
                error=tr(f"文件里没有可用通道(要的是 {channel_name!r})", f"No usable channel in the file (wanted {channel_name!r})"))
        img = ch.get("forward")
        if img is None:
            img = ch.get("backward")
        if img is None:
            return SkillResult(skill_name="AnalyzeFrameTilt", success=False,
                               error=tr("通道里没有正扫/反扫数据", "No forward/backward scan data in the channel"))

        arr = np.asarray(img, dtype=np.float64)

        meta = None
        try:
            meta = parse_xy_meta(scan.get("header", {}) or {})
        except Exception:  # noqa: BLE001
            meta = None
        if not meta:
            return SkillResult(
                skill_name="AnalyzeFrameTilt", success=False,
                error=(tr("从 .sxm 头里解析不出扫描几何(offset/range)—— 没有物理尺寸"
                       "就换算不出角度,拒绝给出无意义的数字", "Cannot parse the scan geometry (offset/range) from the .sxm header -- without a physical size "
"the angle cannot be computed, and a meaningless number is refused")),
                data={"scan_path": scan_path})

        width_m, height_m = float(meta["w"]), float(meta["h"])
        angle_deg = float(meta.get("angle") or 0.0)
        ny, nx = arr.shape
        nm_per_px = (width_m / nx) * 1e9 if nx else None

        est = estimate_tilt(arr, width_m=width_m, height_m=height_m,
                            scan_angle_deg=angle_deg, nm_per_px=nm_per_px,
                            check_steps=check_steps)

        # surface_rms_m 在扣除倾斜与曲率后描述整帧起伏，作为 AutoTilt 的表面幅度尺度。
        # 若不去趋势，倾斜会同时抬高分子与分母，使判据自我抵消。
        # 二阶面也可能吸收单个台阶的一部分，因此分母可能偏小，应结合图像解释。
        # local_texture_rms_m 是分块局部纹理尺度，回答不同问题，不能直接替代前者作为 AutoTilt 输入。
        flat = detrend_quadratic(arr)
        finite = flat[np.isfinite(flat)]
        surface_rms = float(np.std(finite)) if finite.size else 0.0
        try:
            _g, local_sigma, _r = (0.0, 0.0, 1.0)
            tiles = []
            h, w = flat.shape
            tile = 32
            for i in range(h // tile):
                for j in range(w // tile):
                    blk = flat[i * tile:(i + 1) * tile, j * tile:(j + 1) * tile]
                    blk = blk[np.isfinite(blk)]
                    if blk.size >= 2:
                        tiles.append(float(blk.std()))
            local_sigma = float(np.median(tiles)) if tiles else surface_rms
        except Exception:  # noqa: BLE001
            local_sigma = surface_rms

        ratio_max, by_tile = step_dominance_multiscale(flat)

        data = {
            "scan_path": scan_path,
            "channel": channel_name,
            "geometry": {
                "width_m": width_m, "height_m": height_m,
                "angle_deg": angle_deg,
                "pixels": [int(nx), int(ny)],
                "nm_per_px": nm_per_px,
            },
            # —— 喂给 AutoTilt 的就是这一个 ——
            "surface_rms_m": surface_rms,
            "local_texture_rms_m": local_sigma,
            "noise_floor_m": noise_floor(arr),
            "step": {
                "ratio_multiscale": ratio_max,
                "ratio_by_tile": by_tile,
                "ratio_single_tile32": structure_dominance(flat, 32),
            },
            "tilt": est.as_dict(),
        }

        if est.valid:
            summary = (
                tr(f"倾斜 {est.slope_mag_deg:.4f}°(快扫轴 {est.tilt_fast_deg:+.4f}°,"
                f"慢扫轴 {est.tilt_slow_deg:+.4f}° ← 混漂移不可信);"
                f"帧内吃掉 Z {est.z_span_m * 1e9:.2f} nm;"
                f"表面起伏 {surface_rms * 1e12:.1f} pm", f"Tilt {est.slope_mag_deg:.4f}° (fast axis {est.tilt_fast_deg:+.4f}°, "
f"slow axis {est.tilt_slow_deg:+.4f}° <- mixed with drift, not trustworthy); "
f"Z consumed within the frame {est.z_span_m * 1e9:.2f} nm; "
f"surface corrugation {surface_rms * 1e12:.1f} pm"))
        else:
            summary = (
                tr(f"倾斜无法测定({est.invalid_reason});"
                f"表面起伏 {surface_rms * 1e12:.1f} pm,"
                f"台阶主导比 {ratio_max:.2f}", f"Tilt cannot be determined ({est.invalid_reason}); "
f"surface corrugation {surface_rms * 1e12:.1f} pm, "
f"step-dominance ratio {ratio_max:.2f}"))

        return SkillResult(skill_name="AnalyzeFrameTilt", success=True,
                           data=data, summary=summary)
