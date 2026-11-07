"""M02 线扫与网格 TERS 成像（S 级）—— TERSLineProfile / TERSGridMap；M03 外触发成像 —— TERSTriggeredMap
（规格 docs/v2/design/ters_modes_spec.md M02 / M03；契约 §8.3）。

每个像素 = 一次 M01 的守卫式采集（判稳 → 曝光 → 监视 → 复核），像素之间 MoveToXY 移针。
「漂移与针尖变化是第一类误差」（M02.11），所以：

* 参考像素：每 ``reference_every_n`` 个像素回到参考点再取一条谱，与第一条参考谱在谱带内做皮尔逊
  相关，< ``reference_corr_min`` ⇒ ``tip_changed``，按 ``on_reference_fail`` 停（默认）或只标记；
* 扫描前后各一幅形貌（ScanAt；它自动保存的那一帧就是形貌图，看不到时才 SaveScan），结束时
  MeasureFrameDrift 给出两帧之间的横向漂移；
* 判稳失败的像素记空（NaN），不阻塞整幅；空像素超过 10 %（至少看过 10 个像素之后）⇒ 暂停等人。

偏差（报告里列明）：``geometry`` 不作参数 —— 线扫与网格是两个技能；``drift_mode`` 只实现了
``none``（atom_track / reference_scan 暂未实现；选它们会被拒绝）。
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Iterator

import numpy as np

from mast.i18n import tr as _tr_lang
from mast.core.types import ParameterSpec, SkillMetadata
from mast.skills.composite import ters_analysis as ta
from mast.skills.composite.graph_executor import CompositeStep
from mast.skills.composite.ters_common import (
    GUARD_HOLD_S as tc_GUARD_HOLD_S,
    GuardSpec,
    Needs,
    TersComposite,
    TersRun,
    common_extra,
    cp_a,
    cp_b,
    cp_c,
    ters_metadata,
    tip_lift_refusal,
    ttl_params,
    ttl_refusals,
)
from mast.skills.composite.ters_point import TERSPointSpectrum

__all__ = ["TERSLineProfile", "TERSGridMap", "TERSTriggeredMap"]

_XY_LIMIT_M = 1.5e-6          # MoveToXY 的参数界
_EMPTY_FRAC_PAUSE = 0.10
_EMPTY_MIN_SEEN = 10
_INF = _tr_lang("【默认值可调】", "[default adjustable]")

_MAP_OVERRIDES = {"exposure_ms": 1000.0, "settle_s": 0.3, "stable_window_s": 0.2,
                  "stable_timeout_s": 10.0}


def _common_map_params() -> list[ParameterSpec]:
    return [
        ParameterSpec(name="hold_mode", type="str", required=False, default="feedback",
                      allowed_values=["feedback", "hold"],
                      description=_tr_lang("同 M01：feedback = 恒流；hold = 每个像素关反馈恒高曝光，曝光后立即重新闭合"
                                  "（像素之间一定先闭合反馈再移动）。恒高在起伏大的分子上有撞针风险。", "Same as M01: feedback = constant current; hold = feedback is opened at each pixel for a constant-height exposure and closed again right after exposure (feedback is always closed before moving between pixels). Constant height risks a tip crash on strongly corrugated molecules.")),
        ParameterSpec(name="bands_cm1", type="str", required=False, default="",
                      description=_tr_lang("地图显示与参考相关用的谱带 '名称:lo-hi,…'，如 'ring:1540-1620'；"
                                  "空 = 总计数。", "Band for map display and reference correlation, 'name:lo-hi,...', e.g. 'ring:1540-1620'; empty = total counts.") + _INF),
        ParameterSpec(name="baseline_cm1", type="str", required=False, default="",
                      description=_tr_lang("基线窗口；空 = 谱带两侧各 20 cm⁻¹。", "Baseline window; empty = 20 cm⁻¹ on each side of the band.")),
        ParameterSpec(name="drift_mode", type="str", required=False, default="none",
                      allowed_values=["none", "atom_track", "reference_scan"],
                      description=_tr_lang("漂移补偿方式。目前只实现 none（atom_track / reference_scan 暂未实现，"
                                  "选了会被拒绝）；扫描前后形貌仍给出漂移报告。", "Drift compensation mode. Only none is implemented (atom_track / reference_scan are not implemented yet; choosing them is refused); the pre/post topography still gives a drift report.") + _INF),
        ParameterSpec(name="drift_every_n", type="int", required=False, default=0, min_value=0,
                      max_value=10000, description=_tr_lang("每 n 个像素补偿一次；0 = 不补偿。", "Compensate every n pixels; 0 = no compensation.") + _INF),
        ParameterSpec(name="pre_post_scan", type="bool", required=False, default=True,
                      description=_tr_lang("运行前后各扫一幅形貌（ScanAt + SaveScan），结束时 MeasureFrameDrift 报漂移。", "Scan one topography before and after the run (ScanAt + SaveScan); MeasureFrameDrift reports the drift at the end.") + _INF),
        ParameterSpec(name="scan_pixels", type="int", required=False, default=64, min_value=32,
                      max_value=256, description=_tr_lang("形貌图像素数。", "Topography pixel count.") + _INF),
        ParameterSpec(name="scan_line_time_s", type="float", unit="s", required=False, default=0.1,
                      min_value=0.01, max_value=2.0, description=_tr_lang("形貌图每行时间。", "Topography time per line.") + _INF),
        ParameterSpec(name="reference_x_m", type="float", unit="m", required=False, default=None,
                      min_value=-1.5e-6, max_value=1.5e-6,
                      description=_tr_lang("参考像素 X；不给 = 第一个像素。", "Reference pixel X; unset = first pixel.") + _INF),
        ParameterSpec(name="reference_y_m", type="float", unit="m", required=False, default=None,
                      min_value=-1.5e-6, max_value=1.5e-6, description=_tr_lang("参考像素 Y。", "Reference pixel Y.") + _INF),
        ParameterSpec(name="reference_every_n", type="int", required=False, default=50, min_value=0,
                      max_value=10000, description=_tr_lang("每 n 个像素回参考点一次；0 = 不做。", "Return to the reference point every n pixels; 0 = never.") + _INF),
        ParameterSpec(name="reference_corr_min", type="float", required=False, default=0.7,
                      min_value=0.0, max_value=1.0,
                      description=_tr_lang("参考谱与首条参考谱在谱带内的皮尔逊相关下限。", "Lower limit of the Pearson correlation, within the band, between a reference spectrum and the first reference spectrum.") + _INF),
        ParameterSpec(name="on_reference_fail", type="str", required=False, default="stop",
                      allowed_values=["stop", "flag"],
                      description=_tr_lang("参考相关不达标：停止（needs_operator）或仅标记。", "If the reference correlation falls short: stop (needs_operator) or only flag.") + _INF),
        ParameterSpec(name="max_pixels", type="int", required=False, default=4096, min_value=1,
                      max_value=65536, description=_tr_lang("像素数硬上限。", "Hard upper limit on the pixel count.") + _INF),
    ]


class _TersMapBase(TersComposite):
    """线扫与网格共用：像素表 → 逐像素守卫式采集 → 构建中的图 / 曲线。"""

    MODE_ID = "M02"
    MODE_KEY = "ters_map"
    NEEDS = Needs(tip="tunneling")
    GEOMETRY = "grid"

    # subclasses: _offsets(run) -> list of (key, ix, iy, u_m, v_m); _origin(run) -> (x, y) or None
    def _layout(self, run: TersRun) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    def prepare(self, run: TersRun) -> list[str]:
        p = run.p
        out = []
        if p["drift_mode"] != "none":
            out.append(_tr_lang(f"drift_mode={p['drift_mode']} 尚未实现（漂移补偿方式暂未实现）—— 用 none，"
                       "pre_post_scan 仍会给出漂移报告", f"drift_mode={p['drift_mode']} is not implemented (drift compensation is not implemented yet) - use none, pre_post_scan still gives a drift report"))
        try:
            pix = self._layout(run)
        except ValueError as exc:
            return [str(exc)]
        if len(pix) > int(p["max_pixels"]):
            out.append(_tr_lang(f"{len(pix)} 个像素超过 max_pixels={p['max_pixels']}", f"{len(pix)} pixels exceed max_pixels={p['max_pixels']}"))
        per = (0.3 + float(p["settle_s"]) + float(p["stable_window_s"])
               + float(p["exposure_ms"]) / 1000.0 * int(p["frames"]) + 0.5)
        n_ref = len(pix) // int(p["reference_every_n"]) + 1 if int(p["reference_every_n"]) else 0
        run.total_points = len(pix) + n_ref     # points_done counts every acquisition, references too
        scan = (2 * 2 * int(p["scan_pixels"]) * float(p["scan_line_time_s"]) + 20.0
                if p["pre_post_scan"] else 0.0)
        est = 20.0 + (len(pix) + n_ref) * per + scan
        run.plan.update({"geometry": self.GEOMETRY, "pixels": len(pix), "references": n_ref,
                         "estimated_s": round(est, 1)})
        if est > float(p["max_total_s"]):
            out.append(_tr_lang(f"预计 {est:.0f} s 超过 max_total_s={float(p['max_total_s']):.0f} s", f"Estimated {est:.0f} s exceeds max_total_s={float(p['max_total_s']):.0f} s"))
        self._pix = pix
        return out

    def preflight_extra(self, run: TersRun) -> Iterator[CompositeStep]:
        """像素绝对坐标（中心缺省 = 当前位置）都要在 MoveToXY 的范围内。"""
        p = run.p
        xy = run.read_xy()
        origin = self._origin(run, xy)
        if origin is None:
            return [_tr_lang("读不到当前 XY（FolMe_XYPosGet），无法确定网格中心", "Cannot read the current XY (FolMe_XYPosGet), so the grid centre cannot be determined")]
        bad = 0
        for px in self._pix:
            px["x"], px["y"] = origin[0] + px["u"], origin[1] + px["v"]
            if abs(px["x"]) > _XY_LIMIT_M or abs(px["y"]) > _XY_LIMIT_M:
                bad += 1
        if bad:
            return [_tr_lang(f"{bad} 个像素落在 MoveToXY 范围 ±{_XY_LIMIT_M:g} m 之外", f"{bad} pixels fall outside the MoveToXY range of ±{_XY_LIMIT_M:g} m")]
        rx, ry = p.get("reference_x_m"), p.get("reference_y_m")
        self._ref_xy = ((float(rx), float(ry)) if rx is not None and ry is not None
                        else (self._pix[0]["x"], self._pix[0]["y"]))
        run.plan["origin_m"] = list(origin)
        run.plan["reference_xy_m"] = list(self._ref_xy)
        run.plan["first_pixels"] = [{k: px[k] for k in ("key", "x", "y")} for px in self._pix[:5]]
        if p.get("hold_mode") == "hold":       # checked before anything moves, dry_run included
            r = tip_lift_refusal(run)
            if r:
                return [r]
        return []
        yield  # noqa: B901

    def _origin(self, run: TersRun, xy) -> tuple[float, float] | None:  # pragma: no cover
        raise NotImplementedError

    def dry_plan(self, run: TersRun) -> None:
        run.plan["note"] = _tr_lang("dry_run：未移针、未曝光；first_pixels 是前几个目标点", "dry_run: tip not moved, no exposure; first_pixels are the first target points")

    # -- sequence --
    def _open_live(self, run: TersRun) -> None:  # pragma: no cover
        raise NotImplementedError

    def _publish_pixel(self, run: TersRun, px: dict, vals: dict) -> None:  # pragma: no cover
        raise NotImplementedError

    def ters_plan(self, run: TersRun) -> Iterator[CompositeStep]:
        p = run.p
        self._open_live(run)
        run.live.open_extra("ref", "xy", title=_tr_lang("参考像素：强度与相关", "Reference pixel: intensity and correlation"), x_label="pixel index")
        self._vals: dict[str, dict] = {}
        self._ref_spec: np.ndarray | None = None
        self._refs: list[dict] = []
        frac = float(p["exposure_guard_current_frac"])
        hold = p["hold_mode"] == "hold"
        if p["pre_post_scan"] and not run.dry_run:
            self._pre = yield from self._topo(run, "pre")
            if self._pre is None:
                return
        if int(p["reference_every_n"]) > 0 or run.resume_of:
            ok = yield from self._reference(run, 0, first=True)
            if not ok:
                return
        seen = empty = 0
        for n, px in enumerate(self._pix):
            if px["key"] in run.resume_done:
                continue
            if run.check():
                break
            res = yield from run.step(f"{px['key']}:move", "MoveToXY",
                                      {"x_m": px["x"], "y_m": px["y"], "wait": True})
            if not res.success:
                run.record(None, key=px["key"], status="move_failed",
                           values={**self._px_vals(px), "error": res.error})
                run.stop("needs_operator", _tr_lang(f"{px['key']}: MoveToXY 失败：{res.error}", f"{px['key']}: MoveToXY failed: {res.error}"))
                break
            tr = run.ready_or_policy(label=px["key"], key=px["key"], i_target=run.i_set,
                                     feedback=True)
            seen += 1
            if run.stopped:
                break
            if not tr.passed:
                empty += 1
                run.record(None, key=px["key"], status="unstable_tip", tip=tr,
                           values=self._px_vals(px))
                if seen >= _EMPTY_MIN_SEEN and empty / seen > _EMPTY_FRAC_PAUSE:
                    run.stop("needs_operator", _tr_lang(f"空像素 {empty}/{seen} 超过 10 % —— 暂停，请操作员判断针尖 / 判据", f"Empty pixels {empty}/{seen} exceed 10 % - pausing, operator please judge the tip / criterion"))
                    break
                continue
            # dry_run_motion: the hold's feedback off / on runs as for real; only the exposure is skipped
            if hold:
                ok = yield from self._hold_off(run, px["key"])
                if not ok:
                    break
            out = yield from run.acquire(
                px["key"], px["key"],
                guard=GuardSpec(i_ref_a=run.i_set, frac=frac, expect_feedback=not hold),
                extra={**self._px_vals(px), "order_index": n, "tip_ready": tr.as_meta(),
                       "hold_mode": p["hold_mode"]})
            if hold:
                yield from TERSPointSpectrum._reclose(run, px["key"])
            vals = self._out_vals(out)
            run.record(out, key=px["key"], tip=tr, values={**self._px_vals(px), "order_index": n,
                                                           **{f"map_{k}": v for k, v in vals.items()}})
            if out.ok:
                self._vals[px["key"]] = vals
                self._publish_pixel(run, px, vals)
            if not run.handle(out):
                break
            every = int(p["reference_every_n"])
            if every and (n + 1) % every == 0 and n + 1 < len(self._pix):
                ok = yield from self._reference(run, n + 1)
                if not ok:
                    break
        if p["pre_post_scan"] and not run.dry_run and not run._aborted() and getattr(self, "_pre", None):
            post = yield from self._topo(run, "post")
            if post:
                yield from self._drift_report(run, self._pre, post)
        run.analysis["map"] = {"valid": len(self._vals), "pixels": len(self._pix),
                               "missing": [px["key"] for px in self._pix if px["key"] not in self._vals][:200],
                               "references": self._refs}

    # -- helpers --
    def _px_vals(self, px: dict) -> dict:
        return {k: px.get(k) for k in ("ix", "iy", "x", "y", "s_nm")}

    def _out_vals(self, out) -> dict:
        if not out.ok:
            return {}
        vals = {name: (m.get("area") if m.get("valid") else None) for name, m in out.bands.items()}
        if not vals:
            vals["total"] = out.intensity
        if out.window.get("current_mean_a") is not None:
            vals["I_mean"] = out.window.get("current_mean_a")
        z = out.stm_before.get("z_pos_m")
        if isinstance(z, (int, float)):
            vals["z"] = float(z)
        return vals

    def _hold_off(self, run: TersRun, key: str) -> Iterator[CompositeStep]:
        lift_refusal = tip_lift_refusal(run)
        if lift_refusal:
            run.stop("needs_operator", lift_refusal)
            return False
        res = yield from run.step(f"{key}:hold", "ZControllerOnOff", {"enable": False})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"{key}: 关反馈失败：{res.error}", f"{key}: failed to turn feedback off: {res.error}"))
            return False
        run.note_change("z_feedback", original=True, current=False, how="hold_mode=hold")
        return True

    def _reference(self, run: TersRun, at: int, *, first: bool = False) -> Iterator[CompositeStep]:
        p = run.p
        # a resumed run numbers its references apart: the original run's ref0 stays the
        # yardstick (read from its file) and is never overwritten by the resume's own
        key = f"ref{len(self._refs)}" + ("_resume" if run.resume_of else "")
        res = yield from run.step(f"{key}:move", "MoveToXY",
                                  {"x_m": self._ref_xy[0], "y_m": self._ref_xy[1], "wait": True})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"{key}: 回参考点失败：{res.error}", f"{key}: failed to return to the reference point: {res.error}"))
            return False
        tr = run.ready_or_policy(label=key, key=key, i_target=run.i_set, feedback=True)
        if not tr.passed:
            if not run.stopped:
                run.stop("needs_operator", _tr_lang(f"{key}: 参考点判稳失败（{tr.reason}）", f"{key}: reference point stabilisation failed ({tr.reason})"))
            return False
        out = yield from run.acquire(key, key, guard=GuardSpec(
            i_ref_a=run.i_set, frac=float(p["exposure_guard_current_frac"]), expect_feedback=True),
            extra={"reference": True, "at_pixel_index": at, "x": self._ref_xy[0],
                   "y": self._ref_xy[1], "tip_ready": tr.as_meta()})
        run.record(out, key=key, tip=tr, values={"reference": True, "at_pixel_index": at})
        if not run.handle(out):
            return False
        corr = None
        if self._ref_spec is None:
            self._ref_spec = self._first_reference(run, out)
        if self._ref_spec is not None and out.spectrum is not None and out.x_cm1 is not None:
            corr = ta.band_correlation(out.x_cm1, self._ref_spec, out.spectrum, run.bands)
        entry = {"key": key, "at_pixel_index": at, "corr": corr, "intensity": out.band_value()}
        self._refs.append(entry)
        vals = {k: v for k, v in (("intensity", entry["intensity"]), ("corr", corr)) if v is not None}
        if vals:
            run.live.publish({"x": float(at), "values": vals}, suffix="ref")
        if corr is not None and corr < float(p["reference_corr_min"]) and not (first and not run.resume_of):
            run.qc["tip_changed"] += 1
            msg = (_tr_lang(f"tip_changed：{key} 与首条参考谱的相关 {corr:.2f} < {p['reference_corr_min']}"
                   "—— 建议操作员重新判断针尖", f"tip_changed: {key} correlation with the first reference spectrum {corr:.2f} < {p['reference_corr_min']} - operator should re-assess the tip"))
            if p["on_reference_fail"] == "stop":
                run.stop("needs_operator", msg)
                return False
            run.warnings.append(msg)
        return True

    def _first_reference(self, run: TersRun, out) -> np.ndarray | None:
        """首条参考谱。续跑时取原运行的 ref0（从它的谱文件读），这样续跑前的这一条就是「针尖变没变」的检验。"""
        if run.resume_checkpoint:
            ref0 = (run.resume_checkpoint.get("points") or {}).get("ref0") or {}
            try:
                arr = np.load(ref0.get("file")) if ref0.get("file") else None
                if arr is not None and arr.shape[0] > 1:
                    return np.nanmean(arr[1:], axis=0)
            except Exception as exc:  # noqa: BLE001
                run.warnings.append(_tr_lang(f"续跑：原运行的参考谱读不了（{exc}）—— 以本次第一条为参考", f"Resume: the original run's reference spectrum cannot be read ({exc}) - using this run's first one as reference"))
        return None if out.spectrum is None else np.array(out.spectrum)

    def _topo(self, run: TersRun, which: str) -> Iterator[CompositeStep]:
        p = run.p
        cx, cy, size, angle = self._topo_frame(run)
        t_scan = time.time()
        res = yield from run.step(f"topo_{which}:scan", "ScanAt",
                                  {"center_x_m": cx, "center_y_m": cy, "size_m": size,
                                   "pixels": int(p["scan_pixels"]),
                                   "line_time_s": float(p["scan_line_time_s"]),
                                   **({"angle_deg": angle} if angle is not None else {})})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"扫描{('前' if which == 'pre' else '后')}形貌失败：{res.error}", f"{('pre' if which == 'pre' else 'post')}-scan topography failed: {res.error}"))
            return None
        run.note_change("scan_frame", original=None, current={"center": [cx, cy], "size": size},
                        restore=False, how="pre/post topography")
        # ScanAt's ConfigureScan sets autosave=All, so Nanonis has usually written the frame already
        # (a SaveScan after it would store every pre/post frame twice). SaveScan only when no
        # fresh .sxm is visible — autosave off, or the session dir not reachable from this machine.
        path = _autosaved_frame(t_scan)
        sv = None
        if not path:
            sv = yield from run.step(f"topo_{which}:save", "SaveScan", {})
            path = (sv.data or {}).get("saved_path") if sv.success else None
        run.facts[f"topo_{which}"] = {"saved_path": path, "saved_by": "SaveScan" if sv else "autosave",
                                      "scan": {k: (res.data or {}).get(k) for k in (
                                          "tier_name", "center_x_m", "center_y_m", "size_m", "line_time_s")}}
        if not path:
            run.warnings.append(_tr_lang(f"扫描{which}形貌没拿到保存路径（SaveScan：{(sv.error if sv else '') or '无 saved_path'}）", f"{which}-scan topography has no saved path (SaveScan: {(sv.error if sv else '') or 'no saved_path'})"))
            return {"path": None}
        return {"path": path}

    def _drift_report(self, run: TersRun, pre: dict, post: dict) -> Iterator[CompositeStep]:
        if not (pre.get("path") and post.get("path")):
            run.analysis["drift_report"] = {"available": False, "reason": _tr_lang("形貌图路径缺失", "topography path missing")}
            return
        res = yield from run.step("topo:drift", "MeasureFrameDrift",
                                  {"scan_paths": f"{pre['path']},{post['path']}"})
        run.analysis["drift_report"] = ({"available": True, **{k: v for k, v in (res.data or {}).items()
                                                                if k in ("pairs", "n_measured", "rejected",
                                                                         "hysteresis", "net")}}
                                        if res.success else {"available": False, "reason": res.error})

    def product_arrays(self, run: TersRun) -> dict:
        pix = getattr(self, "_pix", [])
        if not pix:
            return {}
        spectra = dict(run.spectra)
        names = sorted({k for v in getattr(self, "_vals", {}).values() for k in v})
        out: dict = {"x_cmd": np.asarray([px.get("x", np.nan) for px in pix], dtype=np.float64),
                     "y_cmd": np.asarray([px.get("y", np.nan) for px in pix], dtype=np.float64),
                     "pixel_keys": np.asarray([px["key"] for px in pix])}
        for name in names:
            out[f"value_{name}"] = np.asarray(
                [(self._vals.get(px["key"]) or {}).get(name, np.nan) or np.nan for px in pix],
                dtype=np.float64)
        width = max((len(s) for s in spectra.values()), default=0)
        if width:
            cube = np.full((len(pix), width), np.nan, dtype=np.float32)
            for i, px in enumerate(pix):
                s = spectra.get(px["key"])
                if s is not None:
                    cube[i, :len(s)] = s
            out["pixel_spectra"] = cube
        out["valid"] = np.asarray([px["key"] in getattr(self, "_vals", {}) for px in pix])
        return self._shape_arrays(out)

    def _shape_arrays(self, out: dict) -> dict:
        return out


class TERSGridMap(_TersMapBase):
    GEOMETRY = "grid"

    def metadata(self) -> SkillMetadata:
        geo = [
            ParameterSpec(name="center_x_m", type="float", unit="m", required=False, default=None,
                          min_value=-1.5e-6, max_value=1.5e-6, description=_tr_lang("网格中心 X；不给 = 当前位置。", "Grid centre X; unset = current position.")),
            ParameterSpec(name="center_y_m", type="float", unit="m", required=False, default=None,
                          min_value=-1.5e-6, max_value=1.5e-6, description=_tr_lang("网格中心 Y；不给 = 当前位置。", "Grid centre Y; unset = current position.")),
            ParameterSpec(name="width_m", type="float", unit="m", required=False, default=5e-9,
                          min_value=1e-10, max_value=2e-7, description=_tr_lang("网格宽。", "Grid width.")),
            ParameterSpec(name="height_m", type="float", unit="m", required=False, default=5e-9,
                          min_value=1e-10, max_value=2e-7, description=_tr_lang("网格高。", "Grid height.")),
            ParameterSpec(name="nx", type="int", required=False, default=16, min_value=2, max_value=64,
                          description=_tr_lang("X 方向像素数（像素间距 = width / nx）。", "Pixel count along X (pixel pitch = width / nx).")),
            ParameterSpec(name="ny", type="int", required=False, default=16, min_value=2, max_value=64,
                          description=_tr_lang("Y 方向像素数。", "Pixel count along Y.")),
            ParameterSpec(name="angle_deg", type="float", unit="deg", required=False, default=0.0,
                          min_value=-180.0, max_value=180.0, description=_tr_lang("网格相对扫描坐标的旋转角。", "Rotation angle of the grid relative to the scan coordinates.")),
            ParameterSpec(name="order", type="str", required=False, default="serpentine",
                          allowed_values=["raster", "serpentine"],
                          description=_tr_lang("像素顺序；蛇形减少空程与回滞。", "Pixel order; serpentine reduces dead travel and hysteresis.") + _INF),
        ]
        return ters_metadata(
            name="TERSGridMap",
            description=(
                _tr_lang("网格 TERS 成像（M02，S 级）：在 nx × ny 的网格上逐像素 MoveToXY → 判稳 → 守卫式曝光（同 M01），"
                "构建谱带强度图；每 reference_every_n 个像素回参考点取参考谱，与首条参考谱的谱带相关 < "
                "reference_corr_min ⇒ tip_changed（默认停下）；前后各扫一幅形貌用于配准与漂移报告；判稳失败的像素"
                "记空不阻塞，空像素 > 10 % 暂停。LiveHub：local/ters_map/<run_id>（map2d，可切谱带 / I_mean / z）、"
                "…/ref（参考像素）、…/status。dry_run=true 只规划像素与检查范围，不动硬件；dry_run_motion=true "
                "真走每个像素（移针、判稳、hold 的关开反馈），不曝光、不扫形貌、不写数据。", "Grid TERS imaging (M02, S level): on an nx × ny grid, per pixel MoveToXY -> stabilise -> guarded exposure (as M01), building a band-intensity map; every reference_every_n pixels it returns to the reference point for a reference spectrum, and a band correlation with the first reference spectrum < reference_corr_min => tip_changed (stops by default); one topography before and after is scanned for registration and the drift report; pixels that fail stabilisation are left empty without blocking, and more than 10 % empty pixels pauses. LiveHub: local/ters_map/<run_id> (map2d, switchable between band / I_mean / z), .../ref (reference pixel), .../status. dry_run=true only plans the pixels and checks ranges, hardware untouched; dry_run_motion=true really walks every pixel (move, stabilise, hold's feedback off/on) without exposing, scanning topography or writing data.")),
            parameters=[*geo, *_common_map_params(), *cp_a(overrides={"exposure_ms": 1000.0}),
                        *cp_b(overrides={k: v for k, v in _MAP_OVERRIDES.items() if k != "exposure_ms"}),
                        *cp_c(), *common_extra()],
            tags=["optics", "ters", "map", "imaging", "spectrum", "acquisition"],
            duration_s=1800.0,
        )

    def _layout(self, run: TersRun) -> list[dict]:
        p = run.p
        nx, ny = int(p["nx"]), int(p["ny"])
        w, h = float(p["width_m"]), float(p["height_m"])
        dx, dy = w / nx, h / ny
        th = math.radians(float(p["angle_deg"] or 0.0))
        c, s = math.cos(th), math.sin(th)
        self._grid = {"nx": nx, "ny": ny, "dx": dx, "dy": dy}
        out = []
        for iy in range(ny):
            cols = range(nx)
            if p["order"] == "serpentine" and iy % 2:
                cols = reversed(range(nx))
            for ix in cols:
                gu = (ix + 0.5) * dx - w / 2
                gv = (iy + 0.5) * dy - h / 2
                out.append({"key": f"px_{ix}_{iy}", "ix": ix, "iy": iy,
                            "u": gu * c - gv * s, "v": gu * s + gv * c, "gu": gu, "gv": gv})
        return out

    def _origin(self, run: TersRun, xy) -> tuple[float, float] | None:
        """网格中心：给了的坐标用给的，没给的用当前位置（两个坐标各自独立）。"""
        p = run.p
        cx, cy = p.get("center_x_m"), p.get("center_y_m")
        if (cx is None or cy is None) and xy is None:
            return None
        return (float(cx) if cx is not None else float(xy[0]),
                float(cy) if cy is not None else float(xy[1]))

    def _topo_frame(self, run: TersRun):
        p = run.p
        o = run.plan.get("origin_m") or [0.0, 0.0]
        return (float(o[0]), float(o[1]), 1.2 * max(float(p["width_m"]), float(p["height_m"])),
                float(p["angle_deg"] or 0.0))

    def _open_live(self, run: TersRun) -> None:
        g = self._grid
        # no "building" in the title: the page shows running / ended itself, and a title that still
        # says building next to an "ended" badge reads as a contradiction
        run.live.open_main("map2d", title=_tr_lang("TERS 网格图", "TERS grid map"), x_label="X", x_unit="nm",
                           y_label="Y", y_unit="nm",
                           meta={"nx": g["nx"], "ny": g["ny"],
                                 "x0": (-float(run.p["width_m"]) / 2 + g["dx"] / 2) * 1e9,
                                 "y0": (-float(run.p["height_m"]) / 2 + g["dy"] / 2) * 1e9,
                                 "dx": g["dx"] * 1e9, "dy": g["dy"] * 1e9,
                                 "x_unit": "nm", "y_unit": "nm",
                                 "frame": "grid (relative to the centre, before rotation)",
                                 "center_m": run.plan.get("origin_m"),
                                 "angle_deg": float(run.p["angle_deg"] or 0.0)})

    def _publish_pixel(self, run: TersRun, px: dict, vals: dict) -> None:
        clean = {k: v for k, v in vals.items() if v is not None}
        if clean:
            run.live.publish({"ix": px["ix"], "iy": px["iy"], "values": clean})

    def _shape_arrays(self, out: dict) -> dict:
        g = self._grid
        nx, ny = g["nx"], g["ny"]
        order = [(px["iy"], px["ix"]) for px in self._pix]
        shaped: dict = {}
        for k, v in out.items():
            arr = np.asarray(v)
            if arr.shape[:1] == (len(order),) and k != "pixel_keys":
                tgt = np.full((ny, nx) + arr.shape[1:], np.nan if arr.dtype.kind == "f" else 0,
                              dtype=arr.dtype if arr.dtype.kind != "b" else bool)
                for i, (iy, ix) in enumerate(order):
                    tgt[iy, ix] = arr[i]
                shaped[k] = tgt
            else:
                shaped[k] = arr
        shaped["grid_dx_m"] = np.asarray(g["dx"])
        shaped["grid_dy_m"] = np.asarray(g["dy"])
        return shaped


def _autosaved_frame(t_scan: float, slack_s: float = 2.0) -> str | None:
    """The newest .sxm in the known scan directories written after ``t_scan`` (minus a little clock
    slack), or None. Reading only: nothing is saved, moved or registered here."""
    try:
        from mast.core.scan_registry import known_scan_dirs

        dirs = known_scan_dirs()
    except Exception:  # noqa: BLE001
        return None
    best: tuple[float, Path] | None = None
    for d in dirs:
        try:
            for f in Path(d).glob("*.sxm"):
                m = f.stat().st_mtime
                if m >= t_scan - slack_s and (best is None or m > best[0]):
                    best = (m, f)
        except OSError:
            continue
    return str(best[1]) if best else None


class TERSTriggeredMap(TERSGridMap):
    """M03 硬件触发 TERS 成像 —— 只实现 S+TTL：LightField 预设 N 帧 ReadoutPerTrigger，MAST 在每个像素判稳后
    经 Nanonis 的触发线打一个脉冲（每像素只发一个；判稳不过的像素不发，对账按 skip 记）。H 级（Nanonis Grid +
    谱学 TTL Sync / 扫描像素脉冲）要重接线，选它会被拒绝。"""

    MODE_ID = "M03"
    TRIGGER_RESPONSE = "NoResponse"     # dark frame first; the triggered configure comes after

    def metadata(self) -> SkillMetadata:
        base = super().metadata()
        drop = {"hold_mode", "reference_every_n", "reference_corr_min", "on_reference_fail"}
        params = [s for s in base.parameters if s.name not in drop]
        for s in params:
            if s.name == "shutter_mode":
                s.default = "AlwaysOpen"
                s.description += _tr_lang("（本模式默认 AlwaysOpen：高重复率下 Normal 会快速消耗 Prontor 快门寿命）", "(this mode defaults to AlwaysOpen: at high repetition rates Normal quickly consumes the Prontor shutter life)")
            if s.name == "exposure_ms":
                s.description += _tr_lang("（S+TTL 可低至 10 ms）", "(S+TTL can go as low as 10 ms)")
        trig = [
            ParameterSpec(name="trigger_scheme", type="str", required=False, default="software_pulse",
                          allowed_values=["software_pulse", "grid_ttl_sync", "scan_pixel_pulse"],
                          description=_tr_lang("software_pulse = S+TTL；grid_ttl_sync / scan_pixel_pulse = H 级，"
                                      "要重接线，选了会被拒绝。", "software_pulse = S+TTL; grid_ttl_sync / scan_pixel_pulse = H level, need rewiring, choosing them is refused.")),
            ParameterSpec(name="trigger_port", type="int", required=False, default=1, min_value=1,
                          max_value=3,
                          description=_tr_lang("触发数字端口：1 = Port B。Port A（0）不可选：它接着 PMD4 "
                                      "粗动控制器；本机配置登记的保留口同样拒绝。", "Trigger digital port: 1 = Port B. Port A (0) cannot be chosen: it is connected to the PMD4 coarse motion controller; ports reserved in this machine's configuration are refused as well.")),
            ParameterSpec(name="trigger_line", type="int", required=False, default=2, min_value=1,
                          max_value=8,
                          description=_tr_lang("触发数字线（编号起点待真机核实，见 trigger_line_verified）。", "Trigger digital line (numbering origin to be verified on the real instrument, see trigger_line_verified).")),
            ParameterSpec(name="trigger_width_s", type="float", unit="s", required=False, default=0.005,
                          min_value=1e-6, max_value=60.0, description=_tr_lang("脉冲宽度。", "Pulse width.")),
            ParameterSpec(name="trigger_edge", type="str", required=False, default="rising",
                          allowed_values=["rising", "falling"],
                          description=_tr_lang("相机触发沿。PP_ 用 Rising；PyLoN 默认 Falling（待核对）。", "Camera trigger edge. PP_ uses Rising; PyLoN defaults to Falling (to be verified).")),
            ParameterSpec(name="trigger_response", type="str", required=False, default="ReadoutPerTrigger",
                          allowed_values=["ReadoutPerTrigger", "ExposeDuringTriggerPulse",
                                          "StartOnSingleTrigger"],
                          description=_tr_lang("LightField TriggerResponse（ExposeDuringTriggerPulse 待核实）。", "LightField TriggerResponse (ExposeDuringTriggerPulse to be verified).")),
            ParameterSpec(name="pixel_margin_s", type="float", unit="s", required=False, default=0.05,
                          min_value=0.0, max_value=5.0,
                          description=_tr_lang("像素周期裕量：周期 = 曝光 + 读出（22.4 ms）+ 裕量。", "Pixel period margin: period = exposure + readout (22.4 ms) + margin.") + _INF),
            ParameterSpec(name="reference_frames", type="int", required=False, default=0, min_value=0,
                          max_value=1000, description=_tr_lang("开头在参考点额外触发的帧数（同一点重复触发）。", "Extra frames triggered at the reference point at the start (repeated triggers at the same point).") + _INF),
            ParameterSpec(name="frame_tracking", type="bool", required=False, default=True,
                          description=_tr_lang("开启 FrameTracking 与曝光时间戳（对账必需，不能关）。", "Enable FrameTracking and exposure timestamps (required for reconciliation, cannot be turned off).")),
            ParameterSpec(name="on_count_mismatch", type="str", required=False, default="truncate",
                          allowed_values=["truncate", "abort"],
                          description=_tr_lang("帧数 ≠ 触发数：truncate = 保留错位点之前的像素；abort = 整幅作废。", "Frame count != trigger count: truncate = keep the pixels before the misaligned point; abort = discard the whole map.") + _INF),
            ParameterSpec(name="ttl_in_signal_index", type="int", required=False, default=-1,
                          min_value=-1, max_value=127,
                          description=_tr_lang("相机 TTL OUT 回接的 Nanonis 信号槽位；-1 = 未接线（A5）。给了就在曝光窗口里读它记录。", "Nanonis signal slot the camera TTL OUT is wired back to; -1 = not wired (A5). If given, it is read and recorded during the exposure window.")),
            ParameterSpec(name="ttl_out_signal", type="str", required=False, default="Exposing",
                          allowed_values=["None", "WaitingForTrigger", "Exposing", "ReadingOut"],
                          description=_tr_lang("相机 TTL OUT 输出什么（只在 ttl_in_signal_index ≥ 0 时记录）。", "What the camera TTL OUT outputs (recorded only when ttl_in_signal_index >= 0).")),
            ParameterSpec(name="dwell_s", type="float", unit="s", required=False, default=1.2,
                          min_value=0.01, max_value=60.0, description=_tr_lang("H 级每点驻留（暂不用）。", "H level dwell per point (currently unused).")),
            ParameterSpec(name="ttl_sync_hs_line", type="int", required=False, default=3, min_value=1,
                          max_value=4, description=_tr_lang("H 级谱学 TTL Sync 的 HS 输出线（暂不用）。", "HS output line for H level spectroscopy TTL Sync (currently unused).")),
            ParameterSpec(name="ttl_sync_width_s", type="float", unit="s", required=False, default=0.0001,
                          min_value=1e-6, max_value=0.01, description=_tr_lang("H 级 TTL Sync 脉宽（暂不用）。", "H level TTL Sync pulse width (currently unused).")),
            *ttl_params(),
        ]
        return ters_metadata(
            name="TERSTriggeredMap",
            description=(
                _tr_lang("硬件触发 TERS 成像（M03，S+TTL）：网格同 TERSGridMap，但「每个像素起曝光」换成 TTL 边沿 —— LightField "
                "预设 N 帧 ReadoutPerTrigger，一次采集；MAST 每像素到位、判稳后在 Nanonis 的触发线打一个脉冲，"
                "等这一帧到达再移下一个像素。结束时三重对账（帧数 = 触发数、帧号连续、曝光间隔 ≈ 触发间隔），对不上就从"
                "错位点起标 invalid、绝不修补。Port A 一律拒绝（PMD4 粗动控制器）；线号起点与电平 / 触发沿没在真机"
                "核实之前（trigger_line_verified / trigger_level_verified）只允许 dry_run —— dry_run_motion 走像素、"
                "判稳，不配置数字线、不打脉冲。H 级（Grid + TTL Sync）未实现。"
                "LiveHub：local/ters_map/<run_id>（map2d）、…/trig（触发数 vs 帧数）、…/status。", "Hardware-triggered TERS imaging (M03, S+TTL): the grid is the same as TERSGridMap, but 'start the exposure at each pixel' is replaced by a TTL edge - LightField presets N frames with ReadoutPerTrigger for one acquisition; at each pixel, once in place and stable, MAST fires one pulse on the Nanonis trigger line and waits for that frame to arrive before moving to the next pixel. At the end a triple reconciliation runs (frame count = trigger count, frame numbers continuous, exposure interval ≈ trigger interval); on a mismatch everything from the misaligned point on is marked invalid, never patched. Port A is always refused (PMD4 coarse motion controller); until the line numbering origin and level / trigger edge are verified on the real instrument (trigger_line_verified / trigger_level_verified) only dry_run is allowed - dry_run_motion walks the pixels and stabilises, without configuring digital lines or firing pulses. H level (Grid + TTL Sync) is not implemented. LiveHub: local/ters_map/<run_id> (map2d), .../trig (trigger count vs frame count), .../status.")),
            parameters=[*trig, *params],
            tags=["optics", "ters", "map", "imaging", "ttl", "trigger", "spectrum", "acquisition"],
            duration_s=1800.0,
        )

    def prepare(self, run: TersRun) -> list[str]:
        p = run.p
        p.setdefault("hold_mode", "feedback")
        p["reference_every_n"] = 0
        out = []
        if p["trigger_scheme"] != "software_pulse":
            out.append(_tr_lang(f"trigger_scheme={p['trigger_scheme']} 是 H 级（Nanonis Grid / 扫描像素脉冲作主时钟），"
                       "要重接线与电平核对 —— 目前只实现 software_pulse（S+TTL）；扫描像素脉冲也不得配到 "
                       "Port A", f"trigger_scheme={p['trigger_scheme']} is H level (Nanonis Grid / scan pixel pulse as master clock), needs rewiring and level checks - only software_pulse (S+TTL) is implemented; scan pixel pulses must not be assigned to Port A either"))
        full, hard = ttl_refusals(p, dry_run=False), ttl_refusals(p, dry_run=True)
        if run.dry_run:
            out += hard
            run.plan["real_run_needs"] = [r for r in full if r not in hard]
        else:
            out += full
        if not p["frame_tracking"]:
            out.append(_tr_lang("frame_tracking=false：对账需要帧号与曝光时间戳，不能关", "frame_tracking=false: reconciliation needs frame numbers and exposure timestamps, it cannot be turned off"))
        if run.resume_of:
            out.append(_tr_lang("S+TTL 不能续跑（卫星上的 N 帧外触发序列随中断丢失）—— 请重新开始", "S+TTL cannot be resumed (the N-frame external trigger sequence on the satellite is lost on interruption) - please start over"))
        out += super().prepare(run)
        run.total_points = len(getattr(self, "_pix", None) or []) + int(p["reference_frames"])
        return out

    def attestation_lines(self, run: TersRun) -> list[str]:
        from mast.skills.composite.ters_common import TTL_SAFE_NOTE

        return [TTL_SAFE_NOTE]

    def ters_plan(self, run: TersRun) -> Iterator[CompositeStep]:
        from mast.skills.composite.ters_common import TriggeredAcquisition
        from mast.skills.composite.ters_pump_probe import PYLON_READOUT_S

        p = run.p
        self._open_live(run)
        run.live.open_extra("trig", "xy", title=_tr_lang("触发数 vs 帧数", "Trigger count vs frame count"), x_label="time", x_unit="s")
        self._vals = {}
        self._refs = []
        n_ref = int(p["reference_frames"])
        n_total = len(self._pix) + n_ref
        targets = [("ref", {"key": f"ref{j}", "x": self._ref_xy[0], "y": self._ref_xy[1],
                            "ix": None, "iy": None}) for j in range(n_ref)]
        targets += [("px", px) for px in self._pix]
        if p["pre_post_scan"] and not run.dry_run:
            self._pre = yield from self._topo(run, "pre")
            if self._pre is None:
                return
        if run.dry_run:
            # dry_run_motion: every move and judgement of the real loop, same policy; the DIO line is
            # not configured and nothing is pulsed (its numbering is unverified on the instrument)
            for kind, px in targets:
                if run.check():
                    return
                res = yield from run.step(f"{px['key']}:move", "MoveToXY",
                                          {"x_m": px["x"], "y_m": px["y"], "wait": True})
                if not res.success:
                    run.stop("needs_operator", _tr_lang(f"{px['key']}: MoveToXY 失败：{res.error}", f"{px['key']}: MoveToXY failed: {res.error}"))
                    return
                tr = run.ready_or_policy(label=px["key"], key=px["key"], i_target=run.i_set,
                                         feedback=True)
                if run.stopped:
                    return
                run.record(None, key=px["key"], status="dry_run" if tr.passed else "unstable_tip", tip=tr,
                           values={**self._px_vals(px), "triggered": False, "kind": kind})
            return
        ok = yield from run.configure_spectrometer(
            trigger_response=str(p["trigger_response"]),
            trigger_edge={"rising": "RisingEdge", "falling": "FallingEdge"}[p["trigger_edge"]],
            frames=n_total, shutter_mode=str(p["shutter_mode"]))
        if not ok:
            return
        res = yield from run.step("trigger:line", "ConfigureDigitalLine",
                                  {"line": int(p["trigger_line"]), "port": int(p["trigger_port"]),
                                   "direction": 1, "polarity": 1})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"触发线配置失败：{res.error}", f"Trigger line configuration failed: {res.error}"))
            return
        exp_s = float(p["exposure_ms"]) / 1000.0
        period = exp_s + PYLON_READOUT_S + float(p["pixel_margin_s"])
        tries = 1 + (int(p["retry_max"]) if p["on_unstable"] == "retry" else 0)
        # worst case between two pulses: move, settle, every judging attempt timing out, one period
        per_trigger = 3.0 + float(p["settle_s"]) + tries * float(p["stable_timeout_s"]) + period + 2.0
        acq = TriggeredAcquisition(run, n_frames=n_total, exposure_ms=float(p["exposure_ms"]),
                                   label="map", per_trigger_s=per_trigger,
                                   extra={"pixels": len(self._pix), "reference_frames": n_ref})
        err = acq.start()
        if err:
            run.stop("needs_operator", err)
            return
        t_start = None
        k = 0
        try:
            if not acq.wait_running(5.0):
                run.stop("needs_operator", _tr_lang(f"外触发采集没进入等待触发：{acq.error or '超时'}", f"External-trigger acquisition did not enter wait-for-trigger: {acq.error or 'timeout'}"))
                return
            frac = float(p["exposure_guard_current_frac"])
            for kind, px in targets:
                if run.check():
                    break
                res = yield from run.step(f"{px['key']}:move", "MoveToXY",
                                          {"x_m": px["x"], "y_m": px["y"], "wait": True})
                if not res.success:
                    run.stop("needs_operator", _tr_lang(f"{px['key']}: MoveToXY 失败：{res.error}", f"{px['key']}: MoveToXY failed: {res.error}"))
                    break
                tr = run.ready_or_policy(label=px["key"], key=px["key"], i_target=run.i_set,
                                         feedback=True)
                if run.stopped:
                    break
                if not tr.passed:
                    run.record(None, key=px["key"], status="unstable_tip", tip=tr,
                               values={**self._px_vals(px), "triggered": False})
                    continue
                t_trig = time.time()
                t_start = t_start or t_trig
                perr = run.nanonis_write("DigLines_Pulse", int(p["trigger_port"]),
                                         [int(p["trigger_line"])], float(p["trigger_width_s"]), 0.0, 1, 1)
                acq.triggers.append({"index": k, "t": t_trig, "key": px["key"], "error": perr})
                if perr:
                    run.stop("needs_operator", _tr_lang(f"触发脉冲失败：{perr}", f"Trigger pulse failed: {perr}"))
                    break
                ttl = []
                over_since = None
                with run.interlock.window(px["key"]):
                    while time.time() - t_trig < period:
                        if int(p["ttl_in_signal_index"]) >= 0:
                            v = run.read_float("Signals_ValGet", int(p["ttl_in_signal_index"]), 0)
                            if v is not None:
                                ttl.append(v)
                        cur = run.read_float("Current_Get")
                        now = time.time()
                        if cur is not None and abs(cur) > frac * abs(float(run.i_set)):
                            over_since = over_since or now
                        else:
                            over_since = None
                        if over_since is not None and now - over_since >= tc_GUARD_HOLD_S:
                            run.qc["guard_abort"] += 1
                            run.stop("needs_operator", _tr_lang(f"{px['key']}: 曝光期间电流 {cur:.3g} A 越限持续 "
                                                       f"≥ {tc_GUARD_HOLD_S * 1000:.0f} ms —— 停发触发", f"{px['key']}: current {cur:.3g} A stayed over the limit during exposure for >= {tc_GUARD_HOLD_S * 1000:.0f} ms - trigger stopped"))
                            break
                        if run.sleep(0.02):
                            run.stop("partial", "aborted by operator")
                            break
                    got = acq.wait_frame(k, 2.0 + exp_s) if not run.stopped else False
                run.live.publish({"x": time.time() - t_start,
                                  "values": {"triggers": len(acq.triggers), "frames": len(acq.frames)}},
                                 suffix="trig")
                if run.stopped:
                    break
                if not got:
                    self._mismatch(run, k)
                    break
                spec = acq.frames[k]
                x = (ta.nm_to_shift_cm1(acq.axis_nm, float(p["laser_line_nm"]))
                     if acq.axis_nm is not None else None)
                vals = {}
                if x is not None and run.bands:
                    for b in run.bands:
                        m = ta.band_metrics(x, spec, b, baselines=run.baselines or None)
                        vals[b.name] = m.get("area") if m.get("valid") else None
                else:
                    vals["total"] = ta.total_counts(spec)
                row = {**self._px_vals(px), "frame_index": k, "trigger_t": t_trig, "triggered": True,
                       "ttl_in_max": max(ttl) if ttl else None, "kind": kind,
                       **{f"map_{n}": v for n, v in vals.items()}}
                run.spectra.append((px["key"], np.asarray(spec, dtype=np.float32)))
                if run.axis_nm is None and acq.axis_nm is not None:
                    run.axis_nm = np.array(acq.axis_nm)
                run.record(None, key=px["key"], status="ok", tip=tr, values=row)
                if kind == "px":
                    self._vals[px["key"]] = vals
                    self._publish_pixel(run, px, vals)
                k += 1
        finally:
            # skipped pixels sent no trigger: the frames planned for them will never come
            if run._aborted() or run.stopped or len(acq.triggers) < acq.n:
                acq.cancel()
            acq.finish(timeout_s=10.0 + 2 * period)
            rec = acq.reconcile()
            run.analysis["reconciliation"] = rec
            if rec["status"] != "ok" and not run.stopped:
                self._mismatch(run, rec.get("first_invalid_index") or 0)
            stored = acq.write_files(rec) if acq.frames else {}
            paths = stored.get("paths") or []
            for row in run.points:
                if row.get("status") in ("ok", "invalid"):
                    row["file"] = paths[0] if paths else ""
                    row["sidecar"] = paths[1] if len(paths) > 1 else ""
                    row["sha256"] = stored.get("sha256", "")
        if p["pre_post_scan"] and not run._aborted() and getattr(self, "_pre", None):
            post = yield from self._topo(run, "post")
            if post:
                yield from self._drift_report(run, self._pre, post)
        run.analysis["map"] = {"valid": len(self._vals), "pixels": len(self._pix),
                               "missing": [px["key"] for px in self._pix if px["key"] not in self._vals][:200]}

    def _mismatch(self, run: TersRun, k: int) -> None:
        from mast.skills.composite.ters_pump_probe import PumpProbeSpectral

        PumpProbeSpectral._mismatch(self, run, k)
        for key in [r["key"] for r in run.points if r.get("status") == "invalid"]:
            self._vals.pop(key, None)


class TERSLineProfile(_TersMapBase):
    GEOMETRY = "line"

    def metadata(self) -> SkillMetadata:
        geo = [
            ParameterSpec(name="x0_m", type="float", unit="m", required=True, min_value=-1.5e-6,
                          max_value=1.5e-6, description=_tr_lang("线扫起点 X。", "Line scan start X.")),
            ParameterSpec(name="y0_m", type="float", unit="m", required=True, min_value=-1.5e-6,
                          max_value=1.5e-6, description=_tr_lang("线扫起点 Y。", "Line scan start Y.")),
            ParameterSpec(name="x1_m", type="float", unit="m", required=True, min_value=-1.5e-6,
                          max_value=1.5e-6, description=_tr_lang("线扫终点 X。", "Line scan end X.")),
            ParameterSpec(name="y1_m", type="float", unit="m", required=True, min_value=-1.5e-6,
                          max_value=1.5e-6, description=_tr_lang("线扫终点 Y。", "Line scan end Y.")),
            ParameterSpec(name="points", type="int", required=False, default=32, min_value=2,
                          max_value=512, description=_tr_lang("线扫点数（含两端）。", "Number of line scan points (both ends included).") + _INF),
        ]
        return ters_metadata(
            name="TERSLineProfile",
            description=(
                _tr_lang("线扫 TERS（M02，S 级）：从 (x0, y0) 到 (x1, y1) 等距取 points 个点，逐点 MoveToXY → 判稳 → 守卫式"
                "曝光（同 M01），得到谱带强度 / I_mean / z 随位置的曲线；参考像素、形貌配准、空点规则同 TERSGridMap。"
                "LiveHub：local/ters_map/<run_id>（xy，x = 沿线距离 nm）、…/ref、…/status。"
                "dry_run=true 只规划点位与检查范围。", "Line-scan TERS (M02, S level): from (x0, y0) to (x1, y1) take `points` equally spaced points, per point MoveToXY -> stabilise -> guarded exposure (as M01), giving band intensity / I_mean / z as curves along the position; reference pixels, topography registration and empty-point rules are the same as TERSGridMap. LiveHub: local/ters_map/<run_id> (xy, x = distance along the line in nm), .../ref, .../status. dry_run=true only plans the points and checks ranges.")),
            parameters=[*geo, *_common_map_params(), *cp_a(overrides={"exposure_ms": 1000.0}),
                        *cp_b(overrides={k: v for k, v in _MAP_OVERRIDES.items() if k != "exposure_ms"}),
                        *cp_c(), *common_extra()],
            tags=["optics", "ters", "map", "imaging", "spectrum", "acquisition"],
            duration_s=600.0,
        )

    def _layout(self, run: TersRun) -> list[dict]:
        p = run.p
        x0, y0, x1, y1 = (float(p[k]) for k in ("x0_m", "y0_m", "x1_m", "y1_m"))
        n = int(p["points"])
        length = math.hypot(x1 - x0, y1 - y0)
        if length <= 0:
            raise ValueError(_tr_lang("线扫起点与终点重合", "Line scan start and end points coincide"))
        out = []
        for k in range(n):
            f = k / (n - 1)
            out.append({"key": f"pt_{k}", "ix": k, "iy": 0, "u": x0 + (x1 - x0) * f,
                        "v": y0 + (y1 - y0) * f, "s_nm": length * f * 1e9})
        return out

    def _origin(self, run: TersRun, xy) -> tuple[float, float] | None:
        return (0.0, 0.0)

    def _topo_frame(self, run: TersRun):
        p = run.p
        x0, y0, x1, y1 = (float(p[k]) for k in ("x0_m", "y0_m", "x1_m", "y1_m"))
        size = max(1.2 * math.hypot(x1 - x0, y1 - y0), 2e-9)
        return (x0 + x1) / 2, (y0 + y1) / 2, size, None

    def _open_live(self, run: TersRun) -> None:
        run.live.open_main("xy", title=_tr_lang("TERS 线扫", "TERS line scan"), x_label="distance", x_unit="nm",
                           y_label="intensity", y_unit="counts")

    def _publish_pixel(self, run: TersRun, px: dict, vals: dict) -> None:
        clean = {k: v for k, v in vals.items() if v is not None}
        if clean:
            run.live.publish({"x": px["s_nm"], "values": clean})
