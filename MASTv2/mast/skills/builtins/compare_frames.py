# -*- coding: utf-8 -*-
"""``CompareFramesLocalChange`` -- did something done inside a box change the surface *there*?

Read-only analysis (never touches the instrument).  The numerical core is
:mod:`mast.vision.frame_compare`; read its docstring for *why* AFTER is registered onto BEFORE on
low-passed (lattice-free) images in a bounded window: on a periodic lattice a whitened phase
correlation has near-equal peaks at every lattice translation (it can return shifts of tens of
pixels where the true drift is only a few pixels), and small frames have the fewest non-lattice features.

What the output does and does not say:

* ``rms_in / rms_out`` of the difference is large for a change with the shape of the box.  A change
  of the whole tip state shows up everywhere and keeps the ratio near 1 -- that is the test.
* It is **not** proof.  Two *unmanipulated* frames also differ (drift residue, tip changes).  A
  real local change has to exceed that null: give ``null_before_path`` / ``null_after_path`` (two
  frames of the area that were not written) and the same box is measured on them.  Without a null
  the verdict can at most be ``candidate_without_null``.
"""
from __future__ import annotations

import json
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

_NAME = "CompareFramesLocalChange"

__all__ = ["CompareFramesLocalChange"]


def _load_pair(p_before, p_after, channel):
    """(before, after, nm_per_px, frame_before, frame_after) in pm / pA, or (error_text, ...)."""
    import numpy as np

    from mast.skills.builtins._sxm_frame import load_frame
    from mast.vision.frame_compare import resample_to

    fb, fa = load_frame(p_before, channel), load_frame(p_after, channel)
    for fr, p in ((fb, p_before), (fa, p_after)):
        if fr.error:
            return "%s: %s" % (p, fr.error), None, None, None, None
    cur = channel.lower() == "current"
    zb = fb.current_forward if cur else fb.forward
    za = fa.current_forward if cur else fa.forward
    if zb is None or za is None:
        return tr("没有通道 %s 的正扫数据", "No forward data for channel %s") % channel, None, None, None, None
    scale = 1e12                                  # m -> pm, A -> pA
    zb, za = np.asarray(zb, float) * scale, np.asarray(za, float) * scale
    if not (np.isfinite(zb).all() and np.isfinite(za).all()):
        return tr("图像含非有限值（扫描未完成？）", "Image contains non-finite values (unfinished scan?)"), None, None, None, None
    if zb.shape != za.shape:
        # same physical area on another pixel grid -> resample AFTER onto BEFORE's grid; anything else is a different area
        wb, wa = fb.nm_per_px * zb.shape[1], fa.nm_per_px * za.shape[1]
        hb, ha = fb.nm_per_px * zb.shape[0], fa.nm_per_px * za.shape[0]
        if abs(wb - wa) > 0.02 * wb or abs(hb - ha) > 0.02 * hb:
            return (tr("两帧尺寸不同（%s 像素，%.2f x %.2f nm 对 %s 像素，%.2f x %.2f nm），不是同一区域，拒绝比较。",
                       "Frames differ in size (%s px, %.2f x %.2f nm vs %s px, %.2f x %.2f nm): not the same area, "
                       "refusing to compare.") % (zb.shape, wb, hb, za.shape, wa, ha)), None, None, None, None
        za = resample_to(za, zb.shape)
    return zb, za, float(fb.nm_per_px), fb, fa


def _write_png(path, res, nmpp, box, unit, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    y0, y1, x0, x1 = res.crop
    ext = [x0 * nmpp, x1 * nmpp, y1 * nmpp, y0 * nmpp]
    cx, cy, size = box
    fig, ax = plt.subplots(1, 3, figsize=(14, 4.6), dpi=120)
    both = np.r_[res.before.ravel(), res.after.ravel()]
    lo, hi = np.percentile(both, (1, 99))
    dl = max(float(np.percentile(np.abs(res.diff), 99)), 1e-12)
    panels = ((ax[0], res.before, "before", "afmhot", lo, hi, "c"),
              (ax[1], res.after, "after (registered, shift %+.2f, %+.2f px)" % (res.reg.shift[1], res.reg.shift[0]),
               "afmhot", lo, hi, "c"),
              (ax[2], res.diff, "after - before (high-passed); rms in %.2f / out %.2f %s" % (res.rms_in, res.rms_out, unit),
               "RdBu_r", -dl, dl, "k"))
    for a, img, t, cm, v0, v1, ec in panels:
        im = a.imshow(img, cmap=cm, vmin=v0, vmax=v1, extent=ext)
        a.add_patch(plt.Rectangle((cx - size / 2, cy - size / 2), size, size, fill=False, ec=ec, lw=1.0, ls="--"))
        a.set_title(t, fontsize=8)
        a.set_xlabel("nm")
        plt.colorbar(im, ax=a, fraction=0.046)
    fig.suptitle(title, fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


class CompareFramesLocalChange(BaseSkill):
    """Before/after difference of one area with the manipulated box marked; rms inside vs outside."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name=_NAME,
            version="1.0.0",
            category=SkillCategory.ANALYSIS,
            safety_level=SafetyLevel.AUTO,
            description=tr(
                "比较操作前后两帧 .sxm 有没有局部变化（针尖电场写入、脉冲之后用）：每张去行中位数 + 平面，"
                "把「后」配准到「前」——配准用低通（默认 0.4 nm，滤掉晶格）后的图，在有限窗口内找峰，亚像素细化被限幅；"
                "裁到重叠区，作差，分别算操作框内外差值的 rms 及其比值，出 PNG（前 | 后 | 差值，画框）和 JSON。"
                "框内/框外比值大才说明变化有框的形状（整根针尖状态变化会让整帧都变，比值接近 1）；"
                "但真实的局部变化必须超过零假设——给两张没写过的帧（null_*），用同一个框量一遍。"
                "不用白化相位相关：周期晶格上它在每个晶格平移处都有近乎相等的峰，可能给出几十像素的位移，"
                "而真实漂移只有几个像素。只读，不碰仪器。",
                "Compare the .sxm frames before and after an operation (tip-field write, pulse) for a local change: "
                "each frame gets row-median + plane removal, AFTER is registered onto BEFORE on low-passed images "
                "(default 0.4 nm, which removes the lattice) with a bounded search window and a clamped sub-pixel "
                "refinement; crop to the overlap, difference, rms of the difference inside and outside the box and "
                "their ratio; PNG (before | after | difference, box drawn) and JSON. A large inside/outside ratio "
                "means the change has the shape of the box (a global tip-state change shows everywhere and keeps the "
                "ratio near 1) -- but a real local change must exceed the null: give two frames that were not written "
                "(null_*) and the same box is measured on them. A whitened phase correlation is not used: on a "
                "periodic lattice it has near-equal peaks at every lattice translation; it can return shifts of tens of "
                "pixels where the true drift is only a few pixels. Read-only, never touches the instrument."),
            parameters=[
                ParameterSpec(name="before_path", type="str", required=True,
                              description=tr("操作前的 .sxm。", "The .sxm before the operation.")),
                ParameterSpec(name="after_path", type="str", required=True,
                              description=tr("操作后的 .sxm（同一区域；像素数不同但物理范围相同会被重采样，范围不同则拒绝）。",
                                             "The .sxm after the operation (same area; a different pixel count over "
                                             "the same range is resampled, a different range is rejected).")),
                ParameterSpec(name="channel", type="str", required=False, default="Z",
                              allowed_values=["Z", "Current"],
                              description=tr("通道：Z（pm）或 Current（pA）。", "Channel: Z (pm) or Current (pA).")),
                ParameterSpec(name="box_size_nm", type="float", required=False, default=3.0, unit="nm",
                              min_value=0.1, max_value=200.0,
                              description=tr("操作框边长。", "Side of the manipulated box.")),
                ParameterSpec(name="box_cx_nm", type="float", required=False, default=None, unit="nm",
                              description=tr("框中心 x，从「前」帧左边量起；留空 = 帧中心。",
                                             "Box centre x from the left of the BEFORE frame; empty = frame centre.")),
                ParameterSpec(name="box_cy_nm", type="float", required=False, default=None, unit="nm",
                              description=tr("框中心 y，从「前」帧上边（数组第 0 行）量起；留空 = 帧中心。",
                                             "Box centre y from the top (array row 0) of the BEFORE frame; empty = frame centre.")),
                ParameterSpec(name="hp_nm", type="float", required=False, default=1.0, unit="nm",
                              min_value=0.0, max_value=50.0,
                              description=tr("显示用高通 σ（0 = 不高通）。rms 另用不小于 2 倍框边长的高通，免得变化自己漏进框外。",
                                             "High-pass sigma for display (0 = off). The rms numbers use a high-pass of "
                                             "at least twice the box size so the change does not leak outside the box.")),
                ParameterSpec(name="lowpass_nm", type="float", required=False, default=0.4, unit="nm",
                              min_value=0.0, max_value=5.0,
                              description=tr("配准用低通 σ，默认 0.4 nm（约一个晶格周期，滤掉晶格）。",
                                             "Low-pass sigma for the registration, default 0.4 nm (about one lattice "
                                             "period: removes the lattice).")),
                ParameterSpec(name="window_nm", type="float", required=False, default=1.0, unit="nm",
                              min_value=0.05, max_value=50.0,
                              description=tr("配准峰的搜索半径：只在 |位移| <= window_nm 内找。应大于帧间真实漂移。",
                                             "Search radius of the registration peak: |shift| <= window_nm only. "
                                             "Larger than the real drift between the frames.")),
                ParameterSpec(name="null_before_path", type="str", required=False, default="",
                              description=tr("零假设：一对没写过的帧中的前一张（与 null_after_path 同时给）。",
                                             "Null: first of a pair of unmanipulated frames (give together with null_after_path).")),
                ParameterSpec(name="null_after_path", type="str", required=False, default="",
                              description=tr("零假设：一对没写过的帧中的后一张。", "Null: second of the unmanipulated pair.")),
                ParameterSpec(name="output_dir", type="str", required=False, default="",
                              description=tr("输出目录；留空则存在「后」帧旁边。", "Output directory; empty = next to the AFTER file.")),
            ],
            estimated_duration_s=10.0,
            composition_level=2,
            tags=["compare", "before-after", "manipulation", "registration", "analysis", "read", "scan"],
        )

    def execute(self, context, params: dict) -> SkillResult:
        from mast.vision.frame_compare import NULL_MARGIN, RATIO_CANDIDATE, compare_local

        pb, pa = str(params.get("before_path") or ""), str(params.get("after_path") or "")
        if not pb or not pa:
            return SkillResult(skill_name=_NAME, success=False,
                               error=tr("需要 before_path 和 after_path", "before_path and after_path are required"))
        channel = str(params.get("channel") or "Z")
        if channel.lower() not in ("z", "current"):
            return SkillResult(skill_name=_NAME, success=False,
                               error=tr("channel 只能是 Z 或 Current", "channel must be Z or Current"))
        channel = "Z" if channel.lower() == "z" else "Current"
        unit = "pm" if channel == "Z" else "pA"
        size = float(params.get("box_size_nm") or 3.0)
        hp = float(params.get("hp_nm", 1.0) if params.get("hp_nm") is not None else 1.0)
        lp = float(params.get("lowpass_nm", 0.4) if params.get("lowpass_nm") is not None else 0.4)
        win_nm = float(params.get("window_nm") or 1.0)
        npb, npa = str(params.get("null_before_path") or ""), str(params.get("null_after_path") or "")
        if bool(npb) != bool(npa):
            return SkillResult(skill_name=_NAME, success=False,
                               error=tr("null_before_path 与 null_after_path 要成对给出",
                                        "null_before_path and null_after_path must be given together"))

        zb, za, nmpp, fb, fa = _load_pair(pb, pa, channel)
        if nmpp is None:
            return SkillResult(skill_name=_NAME, success=False, error=str(zb))
        ny, nx = zb.shape
        cx = params.get("box_cx_nm")
        cy = params.get("box_cy_nm")
        cx = nx * nmpp / 2 if cx is None else float(cx)
        cy = ny * nmpp / 2 if cy is None else float(cy)
        window_px = max(1, int(round(win_nm / nmpp)))
        kw = dict(hp_nm=hp, lp_nm=lp, window_px=window_px)
        try:
            res = compare_local(zb, za, nmpp, cx, cy, size, **kw)
        except ValueError as exc:
            return SkillResult(skill_name=_NAME, success=False, error=str(exc))

        null = None
        if npb:
            nzb, nza, nn, _f1, _f2 = _load_pair(npb, npa, channel)
            if nn is None:
                return SkillResult(skill_name=_NAME, success=False, error=tr("零假设对：", "Null pair: ") + str(nzb))
            if nzb.shape != zb.shape or abs(nn - nmpp) > 0.02 * nmpp:
                return SkillResult(
                    skill_name=_NAME, success=False,
                    error=tr("零假设对的像素网格与前后对不同，同一个框无法直接量；请用同样范围、同样像素数的两帧。",
                             "The null pair is on another pixel grid than the before/after pair, so the same box "
                             "cannot be measured on it; use two frames with the same range and pixel count."))
            try:
                nres = compare_local(nzb, nza, nn, cx, cy, size, **kw)
            except ValueError as exc:
                return SkillResult(skill_name=_NAME, success=False, error=tr("零假设对：", "Null pair: ") + str(exc))
            null = {"ratio": round(nres.ratio, 3), "rms_in": round(nres.rms_in, 4), "rms_out": round(nres.rms_out, 4),
                    "shift_px": [round(float(v), 3) for v in nres.reg.shift],
                    "registration_warnings": list(nres.reg.warnings)}

        warns = list(res.reg.warnings)
        if "lattice_only" in warns:
            msg = tr("两帧里除晶格外几乎没有别的特征（缺陷、吸附物、台阶）：位移只在晶格平移意义下确定，结果不可信。",
                     "Apart from the lattice the frames hold almost no feature (defect, adsorbate, step): the shift is "
                     "only defined modulo a lattice vector and the result cannot be trusted.")
        else:
            msg = ""
        reg_ok = not ({"lattice_only", "peak_at_window_edge", "weak_correlation"} & set(warns))
        if not reg_ok:
            verdict = "registration_unreliable"
        elif res.ratio < RATIO_CANDIDATE:
            verdict = "no_box_shaped_change"
        elif null is None:
            verdict = "candidate_without_null"
        elif res.ratio > NULL_MARGIN * null["ratio"]:
            verdict = "candidate_exceeds_null"
        else:
            verdict = "not_above_null"

        out_dir = Path(str(params["output_dir"])) if params.get("output_dir") else Path(pa).resolve().parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = "cmp_%s_vs_%s_%s" % (Path(pb).stem, Path(pa).stem, channel)
        png, js = out_dir / (stem + ".png"), out_dir / (stem + ".json")
        statement = tr(
            "框内/框外 rms 比值 %.2f（%s）。比值大只说明差值有框的形状；真实的局部变化还必须超过零假设"
            "（两张没写过的帧用同一个框量出的比值）。" % (res.ratio, "已与零假设比较" if null else "没有零假设对，未比较"),
            "Inside/outside rms ratio %.2f (%s). A large ratio only says the difference has the shape of the box; a "
            "real local change must also exceed the null (the ratio of two unmanipulated frames measured with the "
            "same box)." % (res.ratio, "compared with a null pair" if null else "no null pair given, not compared"))
        data = {
            "channel": channel, "unit": unit, "nm_per_px": nmpp, "shape": list(zb.shape),
            "shift_px": [round(float(v), 3) for v in res.reg.shift],
            "shift_lowpass_px": [round(float(v), 3) for v in res.reg.shift_lowpass],
            "registration_ncc": round(res.reg.ncc, 3), "registration_warnings": warns,
            "feature_ratio": round(res.reg.feature_ratio, 4), "window_px": window_px,
            "crop_yyxx": list(res.crop),
            "box": {"cx_nm": cx, "cy_nm": cy, "size_nm": size, "pixels": res.box_px},
            "rms_in": round(res.rms_in, 4), "rms_out": round(res.rms_out, 4), "rms_ratio": round(res.ratio, 3),
            "null": null, "verdict": verdict, "statement": statement,
            "png_path": "", "json_path": str(js),
            "before_path": pb, "after_path": pa,
        }
        if msg:
            data["registration_note"] = msg
        title = "%s%s; box %.1f nm at (%.1f, %.1f) nm; shift %+.2f, %+.2f px; rms in/out %.2f/%.2f %s = %.2f%s -> %s" % (
            channel, "" if fb.bias_v is None else " %+.2f V" % fb.bias_v, size, cx, cy,
            res.reg.shift[1], res.reg.shift[0], res.rms_in, res.rms_out, unit, res.ratio,
            "" if null is None else "; null ratio %.2f" % null["ratio"], verdict)
        try:
            _write_png(png, res, nmpp, (cx, cy, size), unit, title)
            data["png_path"] = str(png)
        except Exception as exc:  # noqa: BLE001 -- the numbers are still valid without the picture
            data["png_error"] = str(exc)
        js.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(
            skill_name=_NAME, success=True, data=data,
            summary=tr("操作前后差值：框内 rms %.2f %s，框外 %.2f %s，比值 %.2f；零假设比值 %s；判定 %s。%s",
                       "Before/after difference: rms inside the box %.2f %s, outside %.2f %s, ratio %.2f; null ratio %s; "
                       "verdict %s. %s")
            % (res.rms_in, unit, res.rms_out, unit, res.ratio, "n/a" if null is None else "%.2f" % null["ratio"],
               verdict, statement))


def make_tool(context_provider):
    from mast.agents._shared.skill_adapter import wrap_skill
    return wrap_skill(CompareFramesLocalChange, context_provider)
