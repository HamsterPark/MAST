# -*- coding: utf-8 -*-
"""``AverageFramesDriftCorrected`` -- average of repeated .sxm frames of one area, drift corrected.

Read-only analysis (never touches the instrument).  The numerical core is
:mod:`mast.vision.frame_average`; read its docstring for *why* the registration is a plain,
Hann-windowed cross-correlation searched only within ``|shift| <= rmax_px`` and not a
whitened phase correlation (on a periodic lattice the latter has near-equal peaks at every
lattice translation, so the global maximum is decided by noise and can lie far from the true drift).

The result is **an average of N drift-corrected images** -- say so in any figure caption.
It is a presentation aid: it raises the signal-to-noise of a periodic structure that the
single frames already show; it cannot create contrast the single frames do not contain, and
a changing tip (different contrast per frame) will be averaged into a blur, not a fix.
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
from mast.i18n import tr
from mast.skills.base import BaseSkill

logger = logging.getLogger(__name__)

_NAME = "AverageFramesDriftCorrected"

__all__ = ["AverageFramesDriftCorrected"]


def _fft_amp(z, nm_per_px):
    import numpy as np
    z = np.asarray(z, dtype=float)
    z = z - z.mean()
    w = np.outer(np.hanning(z.shape[0]), np.hanning(z.shape[1]))
    A = np.abs(np.fft.fftshift(np.fft.fft2(z * w)))
    ky = np.fft.fftshift(np.fft.fftfreq(z.shape[0], d=nm_per_px))
    kx = np.fft.fftshift(np.fft.fftfreq(z.shape[1], d=nm_per_px))
    return kx, ky, A


def _write_png(path, one, avg, nm_per_px, unit, n, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig, ax = plt.subplots(1, 4, figsize=(16, 4.3), dpi=120)
    ext = [0, avg.shape[1] * nm_per_px, 0, avg.shape[0] * nm_per_px]
    for a, z, t in ((ax[0], one, "single frame"),
                    (ax[1], avg, "average of %d drift-corrected images" % n)):
        lo, hi = np.percentile(z, (1, 99))
        a.imshow(z, cmap="afmhot", vmin=lo, vmax=hi, extent=ext)
        a.set_title("%s   rms %.2f %s" % (t, z.std(), unit), fontsize=9)
        a.set_xlabel("nm")
    for a, z, t in ((ax[2], one, "single |FFT|"), (ax[3], avg, "average |FFT|")):
        kx, ky, A = _fft_amp(z, nm_per_px)
        kmax = min(3.5, float(np.abs(kx).max()))
        sx, sy = np.abs(kx) <= kmax, np.abs(ky) <= kmax
        L = np.log10(A[np.ix_(sy, sx)] + 1e-6)
        lo, hi = np.percentile(L, (50, 99.8))
        a.imshow(L, cmap="gray", vmin=lo, vmax=hi,
                 extent=[kx[sx][0], kx[sx][-1], ky[sy][-1], ky[sy][0]])
        a.set_title(t, fontsize=9)
        a.set_xlabel("1/nm")
    fig.suptitle(title, fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


class AverageFramesDriftCorrected(BaseSkill):
    """Average repeated frames of the same area after sub-pixel drift correction."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=_NAME,
            version="1.0.0",
            category=SkillCategory.ANALYSIS,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "把同一区域的一批 .sxm 帧做漂移校正平均（出图用）：每张先去行中位数 + 拟合平面，"
                "再用加汉宁窗的普通（非白化）互相关向第一张配准，峰只在 |位移| <= rmax_px 内找，"
                "抛物线亚像素细化；傅里叶平移后只屏蔽绕回的那一边，裁到公共矩形再平均。"
                "输出 .npy、PNG（单帧 | 平均 | 两者 |FFT|）、单帧与平均的 rms、各张位移。"
                "结果是「N 张漂移校正图的平均」，图注必须这样写。只读，不碰仪器。"
                "不用白化相位相关：周期晶格上它在每个晶格平移处都有近乎相等的峰，全局最大值由噪声决定，可能远离真实漂移。",
                "Drift-corrected average of a batch of .sxm frames of the same area (for figures): each "
                "image gets row-median subtraction + a plane fit, is registered to the first by a plain "
                "(not whitened) Hann-windowed cross-correlation whose peak is searched only within "
                "|shift| <= rmax_px, with parabolic sub-pixel refinement; after the Fourier shift only the "
                "wrapped-around edge is masked, and the average is cropped to the common rectangle. "
                "Outputs a .npy, a PNG (single | average | |FFT| of both), the rms of single vs average and "
                "the shifts. The result is an \"average of N drift-corrected images\" and the caption must "
                "say so. Read-only, never touches the instrument. A whitened phase correlation is not used: "
                "on a periodic lattice it has near-equal peaks at every lattice translation; the global maximum "
                "is then decided by noise and can lie far from the true drift."),
            parameters=[
                ParameterSpec(
                    name="scan_paths", type="str", required=True,
                    description=tr("同一区域的 .sxm 路径，逗号或换行分隔，至少一个（含反扫时一个文件就有两张图）。",
                                   "Paths of .sxm files of the same area, comma or newline separated, at least one "
                                   "(with backward scans one file already gives two images).")),
                ParameterSpec(
                    name="channel", type="str", required=False, default="Z",
                    description=tr("通道名，默认 Z（单位按 pm 显示）。", "Channel name, default Z (shown in pm).")),
                ParameterSpec(
                    name="include_backward", type="bool", required=False, default=True,
                    description=tr("是否把反扫图也加入平均，默认是。", "Whether to add the backward images too, default true.")),
                ParameterSpec(
                    name="rmax_px", type="int", required=False, default=20,
                    min_value=1, max_value=200,
                    description=tr("配准峰的搜索半径（像素）：只在 |位移| <= rmax_px 内找最大值。"
                                   "应大于帧间真实漂移、小于一个晶格周期之外的别名。",
                                   "Search radius of the registration peak (px): the maximum is looked for only within "
                                   "|shift| <= rmax_px. Larger than the real drift between frames, and small enough to "
                                   "exclude lattice-translation aliases.")),
                ParameterSpec(
                    name="output_dir", type="str", required=False, default="",
                    description=tr("输出目录；留空则存在第一个输入文件旁边。",
                                   "Output directory; empty = next to the first input file.")),
            ],
            estimated_duration_s=10.0,
            composition_level=1,
            tags=["average", "drift", "analysis", "read", "scan", "figure"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        import numpy as np

        from mast.skills.builtins._sxm_frame import load_frame, split_paths
        from mast.vision.frame_average import average_registered

        paths = split_paths(str(params.get("scan_paths") or ""))
        channel = str(params.get("channel") or "Z")
        use_bwd = bool(params.get("include_backward", True))
        rmax = int(params.get("rmax_px", 20))
        if not paths:
            return SkillResult(skill_name=_NAME, success=False,
                               error=tr("没有给 scan_paths", "No scan_paths given"))

        scale = 1e12 if channel.lower() == "z" else 1.0
        unit = "pm" if channel.lower() == "z" else "a.u."
        imgs, labels, rejected, first = [], [], [], None
        for p in paths:
            fr = load_frame(p, channel)
            if fr.error:
                rejected.append({"path": p, "reason": fr.error})
                continue
            first = first or fr
            for tag, arr in (("fwd", fr.forward), ("bwd", fr.backward if use_bwd else None)):
                if arr is None:
                    continue
                arr = np.asarray(arr, dtype=float)
                if not np.isfinite(arr).all():
                    rejected.append({"path": "%s[%s]" % (fr.name, tag),
                                     "reason": tr("含非有限值（扫描未完成？）",
                                                  "contains non-finite values (unfinished scan?)")})
                    continue
                imgs.append(arr * scale)
                labels.append("%s[%s]" % (fr.name, tag))
        if not imgs:
            return SkillResult(
                skill_name=_NAME, success=False,
                error=tr("没有可用的图像：", "No usable image: ") + "; ".join(r["reason"] for r in rejected[:3]),
                data={"rejected": rejected})
        sizes = sorted({i.shape for i in imgs})
        if len(sizes) > 1:
            return SkillResult(
                skill_name=_NAME, success=False,
                error=tr("图像尺寸不一致 %s —— 不同像素网格的帧不能直接平均（那是不同区域或不同分辨率）。",
                         "Image sizes differ %s -- frames on different pixel grids cannot be averaged directly "
                         "(different areas or resolutions).") % sizes,
                data={"rejected": rejected, "sizes": [list(s) for s in sizes]})
        if len(imgs) < 2:
            return SkillResult(
                skill_name=_NAME, success=False,
                error=tr("只有 1 张图像，没有可平均的东西（给更多帧，或开启 include_backward）。",
                         "Only 1 image -- nothing to average (give more frames or enable include_backward)."),
                data={"rejected": rejected})

        res = average_registered(imgs, rmax_px=rmax)
        nmpp = float(first.nm_per_px)
        if params.get("output_dir"):
            out_dir = Path(str(params["output_dir"]))
        else:
            out_dir = Path(paths[0]).resolve().parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = "%s_avg%d" % (Path(paths[0]).stem, res.n_images)
        npy, png = out_dir / (stem + ".npy"), out_dir / (stem + ".png")
        np.save(npy, res.average)
        title = "average of %d drift-corrected images; %s%s; shifts px %s" % (
            res.n_images, channel,
            "" if first.bias_v is None else ", %+.2f V" % first.bias_v, res.shifts)
        png_err = ""
        try:
            _write_png(png, res.reference, res.average, nmpp, unit, res.n_images, title)
        except Exception as exc:  # noqa: BLE001 -- the numbers are still valid without the picture
            png_err = str(exc)
        rms_single = float(res.reference.std())
        rms_avg = float(res.average.std())
        big = [s for s in res.shifts if max(abs(s[0]), abs(s[1])) > 0.8 * rmax]
        data = {
            "n_images": res.n_images, "labels": labels, "shifts_px": res.shifts,
            "crop_yxyx": list(res.crop), "shape": list(res.average.shape),
            "nm_per_px": nmpp, "unit": unit, "channel": channel,
            "rms_single": round(rms_single, 4), "rms_average": round(rms_avg, 4),
            "npy_path": str(npy), "png_path": "" if png_err else str(png),
            "rejected": rejected,
            "statement": "average of %d drift-corrected images" % res.n_images,
        }
        if png_err:
            data["png_error"] = png_err
        if big:
            data["search_edge_warning"] = tr(
                "有位移接近搜索半径边缘（%s px，rmax_px=%d）：真实漂移可能更大或峰被别名抢走，"
                "请核对该配准或调大 rmax_px 后比较。",
                "Some shifts are near the edge of the search window (%s px, rmax_px=%d): the real drift may be larger "
                "or an alias won; check that registration or compare with a larger rmax_px.") % (big, rmax)
        return SkillResult(
            skill_name=_NAME, success=True, data=data,
            summary=tr("%d 张漂移校正图的平均（%s）：rms 单帧 %.2f %s -> 平均 %.2f %s；已存 %s。",
                       "Average of %d drift-corrected images (%s): rms single %.2f %s -> average %.2f %s; saved %s.")
            % (res.n_images, channel, rms_single, unit, rms_avg, unit, npy))


def make_tool(context_provider):
    from mast.agents._shared.skill_adapter import wrap_skill
    return wrap_skill(AverageFramesDriftCorrected, context_provider)
