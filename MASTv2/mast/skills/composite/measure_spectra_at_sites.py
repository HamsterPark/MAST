"""在标注页标出的位点上逐点测谱（可选漂移追踪）。

``MeasureSpectraAtSites``：读 ``ReadSiteMarks``（页面上人标的位点，已换算成扫描器坐标）→ 按
测量顺序 n 逐点 ``MoveToXY`` → ``AcquireSTS``（复用其 ramp_before/restore_after，偏压/
电流从成像值平滑过渡到测谱值、测完回来）；每点把 status/结果经 **``apply_patch``** 回写进
marks.json（页面轮询 /doc 实时显示进度），并刷新 ``status/live``。

蓝本：一份外部 campaign 脚本 ``run_campaign.py``。
与蓝本的取舍：

* **漂移追踪有两种**（``drift_method``，仅 ``track_every>0`` 时用）：

  - ``molecules``（默认）：每 N 个位点（含第一个之前）用 ``ScanAt`` 重扫参考帧那一块 →
    ``RegisterDriftByMolecules`` 把新帧的分子对到参考帧的分子上 → 漂移（平移）、质量、每个分子的
    位移；之后每个位点的目标 = 名义位置 + 该位点的位移（吸附到分子的位点用锚点分子的当前位置，
    其余用附近分子的局部位移，再不行用全局漂移），再按漂移速度外推到「现在」。配准质量 <0.5 就
    不信这次配准、用外推；**连续 3 次**不可信就停（「漂移丢失」）。数值与门限全在
    ``mast.skills.builtins.drift_register``，这里只排步骤。**这些
    是照一份外部脚本的摘要移植的**（偏差见那个模块的说明）。
  - ``reference_scan``：旧的 ``TrackDrift_ReferenceScan``（整像素整帧互相关，每 N 点插一次，
    第一个位点之前不插）。保留是为了不回退。

* **停机**走 MAST 的 ``mast_cancel`` + 连续失败计数 + 漂移丢失，不做 run_campaign 的目录
  STOP/PAUSE 文件。
* **不把 ``plan/confirm`` 当操作员批准**（端点分不出人/脚本）——本技能是被显式调用的，调用本身即
  授权；``plan/confirm`` 一概不读。可选用 ``plan/marking.done`` 当「标注完成」前置（默认要求），
  避免在半标注的页面上开测。自主性边界照旧由 ``AcquireSTS`` 的 SafetyGate / SAFE 模式管。
* 回写**只走 ``apply_patch``**（锁在 ``api.routes.site_marking``）——别自己开 marks.json 写，否则会
  和页面的拖动互相覆盖。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Iterator

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.skills.builtins import drift_register as DR
from mast.skills.composite._base import CompositeSkillGraph
from mast.skills.composite.graph_executor import (
    CompositeProgress,
    CompositeStep,
    GraphExecutor,
)
from mast.agents._shared.skill_adapter import wrap_skill

logger = logging.getLogger(__name__)

#: 位点数上限（每点的记账进 partial_data / 每个 checkpoint；超过就只留计数）。
_MAX_SITES = 400

#: 参考帧与页面几何「一致」的容差（同 ReadSiteMarks 的 _GEO_TOL_*）。
_GEO_TOL_NM = 0.01
_GEO_TOL_DEG = 0.01
#: 锚点分子下标对不对得上的容差：ReadSiteMarks 给的坐标是 4 位小数。
_ANCHOR_TOL_NM = 0.02


def _apply_patch_quiet(page_id: str, patch: dict) -> None:
    """回写 marks.json（走锁内 apply_patch）。失败只告警——回写是善后，不该拖垮测谱。

    惰性 import：skill → api.routes 是倒挂依赖，只在真回写时才引入（也避免无 FastAPI 的上下文
    在导入期就炸）。"""
    try:
        from mast.api.routes.site_marking import apply_patch
        apply_patch(page_id, patch)
    except Exception as exc:  # noqa: BLE001 — 回写失败不影响已采的谱
        logger.warning("MeasureSpectraAtSites: 回写页面失败（%s）：%s", patch.get("op"), exc)


def _find_tracking_frame(context, since_ts: float) -> str | None:
    """刚扫完的追踪帧（Nanonis 自动保存的那个）。独立成函数，测试里替换它就不必有真文件系统。

    扫描结束到文件落盘有一小段时间，所以最多等 3 s；找不到再由调用方 ``SaveScan``。"""
    return DR.find_fresh_frame(context, since_ts, wait_s=3.0)


def _geometry_diffs(a: dict, b: dict) -> list[str]:
    """页面的帧几何（ReadSiteMarks 的 ``frame``）与参考 .sxm 文件头的几何不一致处。"""
    diffs = []
    for k in ("cx_nm", "cy_nm", "w_nm", "h_nm"):
        if abs(float(a[k]) - float(b[k])) > _GEO_TOL_NM:
            diffs.append(f"{k}: page {float(a[k]):.4f} vs file {float(b[k]):.4f}")
    if abs(float(a["angle_deg"]) - float(b["angle_deg"])) > _GEO_TOL_DEG:
        diffs.append(f"angle: page {float(a['angle_deg']):.3f} vs file {float(b['angle_deg']):.3f}")
    return diffs


class MeasureSpectraAtSites(CompositeSkillGraph):
    """在「位点标注页」标出的位点上逐点测谱，结果实时回写页面。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="MeasureSpectraAtSites",
            version="1.1.0",
            category=SkillCategory.COMPOSITE,
            safety_level=SafetyLevel.CONFIRM,
            description=tr(
                "在位点标注页（PrepareSiteMarkingPage 生成、人在上面点好）的位点上逐点测谱："
                "按编号顺序移针、测 STS，结果实时回写页面；偏压/电流平滑过渡（防伤针），可选漂移追踪"
                "（默认按分子配准：每 N 个位点重扫参考帧、把分子对上、按位点修正目标）。",
                "Measure spectra at the sites a person marked on a site-marking page: move and run STS "
                "point by point in order, writing results back to the page live; bias/current ramp "
                "smoothly (tip-safe), with optional drift tracking (by default by molecular "
                "registration: re-scan the reference frame every N sites, match the molecules, and "
                "correct each site's target)."),
            parameters=[
                ParameterSpec(
                    name="page_id", type="str", required=True,
                    description=tr("标注页 id（PrepareSiteMarkingPage 返回的那个）。",
                                   "The marking-page id (as returned by PrepareSiteMarkingPage)."),
                ),
                ParameterSpec(
                    name="frame_path", type="str", required=False, default="",
                    description=tr("用于精确化/角度核对的参考帧 .sxm（留空＝不精确化，直接用页面坐标）。"
                                   "分子漂移追踪（track_every>0）也以它为参考，必须给。",
                                   "Reference .sxm for refinement/angle check (empty = no refine, use page coords). "
                                   "Molecular drift tracking (track_every>0) also uses it as the reference, so it is required then."),
                ),
                ParameterSpec(
                    name="refine", type="bool", required=False, default=True,
                    description=tr("是否把位点吸附到分子中心/氢键（需 frame_path）。",
                                   "Snap sites to molecule centre / H-bond (needs frame_path)."),
                ),
                ParameterSpec(
                    name="start_v", type="float", required=False, default=-0.4,
                    unit="V", min_value=-10.0, max_value=10.0,
                    description=tr("STS 起始偏压。", "STS start bias."),
                ),
                ParameterSpec(
                    name="end_v", type="float", required=False, default=0.4,
                    unit="V", min_value=-10.0, max_value=10.0,
                    description=tr("STS 终止偏压。", "STS end bias."),
                ),
                ParameterSpec(
                    name="num_points", type="int", required=False, default=256,
                    min_value=10, max_value=10000,
                    description=tr("每条谱的采样点数。", "Points per spectrum."),
                ),
                ParameterSpec(
                    name="num_sweeps", type="int", required=False, default=0,
                    min_value=0, max_value=1000,
                    description=tr("平均扫掠数（0＝保持操作员当前设置，见 ConfigureSTS）。",
                                   "Sweeps to average (0 = keep the operator's current setting)."),
                ),
                ParameterSpec(
                    name="spectro_setpoint_a", type="float", required=False, default=None,
                    unit="A",
                    description=tr("测谱用电流 setpoint（给了才在测谱前后 ramp setpoint）。",
                                   "Spectroscopy current setpoint (ramped before/after only if given)."),
                ),
                ParameterSpec(
                    name="ramp_slew_rate_v_per_s", type="float", required=False, default=None,
                    unit="V/s", min_value=0.01, max_value=100.0,
                    description=tr("偏压 ramp 速率（V/s，同 SetBias 语义；留空＝瞬时）。防伤针建议给一个值。",
                                   "Bias ramp rate (V/s, SetBias semantics; empty = instant). Give a value to protect the tip."),
                ),
                ParameterSpec(
                    name="require_marking_done", type="bool", required=False, default=True,
                    description=tr("要求页面已点「标注完成」(plan/marking.done) 才开测。",
                                   "Require the page's 'marking done' (plan/marking.done) before measuring."),
                ),
                ParameterSpec(
                    name="require_coords_trustworthy", type="bool", required=False, default=True,
                    description=tr("坐标不可信（非方形帧 / 读不到角度 / 页面与 frame_path 不是同一帧）时拒绝自动批量测。"
                                   "关掉＝自担风险照测。",
                                   "Refuse autonomous bulk measuring when coordinates are not trustworthy "
                                   "(non-square frame / unknown angle / page and frame_path are not the same frame). Off = measure anyway."),
                ),
                ParameterSpec(
                    name="track_every", type="int", required=False, default=0,
                    min_value=0, max_value=1000,
                    description=tr("每 N 个位点做一次漂移追踪；0＝关。drift_method=molecules 时第一个位点之前"
                                   "也会先配准一次（参考帧可能已经是很久以前的了）；N 不小于位点数就只在开始前配一次。"
                                   "分子排布接近完美周期时参考帧要新（漂移小于约半个分子间距），否则会差整数个分子 ——"
                                   "结果里的 drift_warnings 会点名（competing_peak_ratio）。",
                                   "Run a drift-tracking pass every N sites; 0 = off. With drift_method=molecules "
                                   "a registration is also done before the first site (the reference frame may be old); "
                                   "N >= the number of sites means a single registration before the first site. On a "
                                   "near-perfectly periodic arrangement the reference frame must be fresh (drift below "
                                   "about half a molecular spacing), otherwise the result is a whole number of molecules "
                                   "off - drift_warnings names it (competing_peak_ratio)."),
                ),
                ParameterSpec(
                    name="drift_method", type="str", required=False, default="molecules",
                    allowed_values=["molecules", "reference_scan"],
                    description=tr("漂移追踪方式（track_every>0 时才用）。molecules＝重扫参考帧并按分子配准"
                                   "（亚像素，给每个位点修正目标，质量不够就停）；reference_scan＝旧的 "
                                   "TrackDrift_ReferenceScan（整帧互相关，整像素）。",
                                   "Drift-tracking method (used only when track_every>0). molecules = re-scan the "
                                   "reference frame and register by molecules (sub-pixel, corrects each site's "
                                   "target, stops when the quality is too low); reference_scan = the old "
                                   "TrackDrift_ReferenceScan (whole-frame cross-correlation, whole-pixel)."),
                ),
                ParameterSpec(
                    name="track_pixels", type="int", required=False, default=0,
                    min_value=0, max_value=4096,
                    description=tr("追踪帧的每行像素数（0＝由扫图策略表决定）。分子配准要能分开分子："
                                   "像素 < 约 8 个/分子间距就不行了。",
                                   "Pixels per line of the tracking frame (0 = decided by the scan policy table). "
                                   "Registration must resolve molecules: fewer than ~8 pixels per molecular spacing will not do."),
                ),
                ParameterSpec(
                    name="track_line_time_s", type="float", required=False, default=0.0,
                    unit="s", min_value=0.0, max_value=600.0,
                    description=tr("追踪帧每条扫描线的时间（0＝由扫图策略表决定）。",
                                   "Time per scan line of the tracking frame (0 = decided by the scan policy table)."),
                ),
                ParameterSpec(
                    name="stop_after_weak_registrations", type="int", required=False, default=DR.WEAK_STOP_N,
                    min_value=0, max_value=100,
                    description=tr("连续这么多次分子配准不可信（质量<0.5 / 匹配<3 / 追踪步骤本身失败）就停，"
                                   "报「漂移丢失」（0＝不因此停）。",
                                   "Stop with 'drift lost' after this many consecutive untrustworthy molecular "
                                   "registrations (quality<0.5 / <3 matches / the tracking step itself failed) (0 = never)."),
                ),
                ParameterSpec(
                    name="stop_after_consecutive_failures", type="int", required=False, default=3,
                    min_value=0, max_value=100,
                    description=tr("连续失败这么多次就停（0＝不因失败停）。",
                                   "Stop after this many consecutive failures (0 = never stop on failure)."),
                ),
            ],
            preconditions=["z_controller_on"],
            estimated_duration_s=600.0,
            composition_level=3,
            tags=["spectroscopy", "sites", "sts", "drift", "composite"],
        )

    # ── 动态 plan（generator）──────────────────────────────────────────────
    # 为什么是 generator 而不是静态列表：逐点步骤是 optional（一个点坏不该毙掉整批），
    # 而**静态 plan + optional 步骤无法中途停**（执行器对 optional 失败一律继续）。
    # 要「连续失败 N 次就停」，唯一正确的做法是**不再 yield**（return）——执行器在
    # yield 之间跑 generator 体，on_step_failed 已把 streak 写进 partial_data，这里读它决定停不停
    # （同 spectroscopy_at_positions 的 stop_after_consecutive_discard）。站位由 run_composite
    # 经 ReadSiteMarks 读出后塞进 params["_sites"]。分子漂移追踪同理：目标要等前面的配准跑完才算得
    # 出来，所以也得在 yield 之间算。

    def plan_dynamic(self, params: dict, executor) -> "Iterator[CompositeStep]":
        sites: list[dict] = list(params.get("_sites") or [])
        start_v = params.get("start_v", -0.4)
        end_v = params.get("end_v", 0.4)
        num_points = params.get("num_points", 256)
        num_sweeps = int(params.get("num_sweeps", 0) or 0)
        setpoint = params.get("spectro_setpoint_a")
        slew = params.get("ramp_slew_rate_v_per_s")
        track_every = int(params.get("track_every", 0) or 0)
        method = str(params.get("drift_method") or "molecules")
        stop_n = int(params.get("stop_after_consecutive_failures", 3) or 0)

        track = params.get("_track") if (track_every and method == "molecules") else None
        if track_every and method == "molecules" and track is None:
            # run_composite 在 molecules 模式下会先备好 _track（备不好就直接失败）。走到这里只可能
            # 是有人绕过入口直接调 plan_dynamic —— 没有参考就没法配准，如实说，别装作在追踪。
            logger.warning("MeasureSpectraAtSites: drift_method=molecules 但没有参考信息（_track），"
                           "本次不做漂移追踪")
        tracker = self._tracker_for(params, executor) if track is not None else None

        cfg = {"start_v": start_v, "end_v": end_v, "num_points": num_points}
        if num_sweeps >= 1:
            cfg["num_sweeps"] = num_sweeps
        yield CompositeStep(
            step_id="configure", skill_name="ConfigureSTS", params=cfg,
            optional=False, checkpoint_after=True, tags=("setup",))

        for i, s in enumerate(sites):
            # 连续失败到阈值 ⇒ 不再往下发（别在坏状态上继续撞针）。streak 由 on_step_failed
            # 维护、成功时清零；在**发下一个点之前**读，所以是「连续」而不是「累计」。
            if stop_n:
                streak = int(executor.progress.partial_data.get("streak", 0))
                if streak >= stop_n:
                    executor.set_partial("stop_reason", tr(
                        f"连续 {streak} 次失败，停止（可能针尖坏了或漂移失配）。",
                        f"Stopped after {streak} consecutive failures (tip may be bad, or drift lost)."))
                    return
            sid = str(s.get("id") or f"i{i}")
            tx, ty = s["scanner_x_m"], s["scanner_y_m"]
            if tracker is not None:
                if i % track_every == 0:
                    yield from self._track_until_usable(i, params, executor, tracker, track)
                    if tracker.lost:
                        executor.set_partial("stop_reason", self._drift_lost_text(tracker))
                        executor.set_partial("drift_lost", True)
                        return
                tx, ty, info = self._corrected_target(s, tracker, track["frame"]["angle_deg"])
                self._note_site_drift(executor, sid, info)
            elif track_every and method == "reference_scan" and i > 0 and i % track_every == 0:
                # 旧方法：复用现成的参考帧漂移追踪；拿不到/失败也不中断（站位仍按绝对坐标测）。
                yield CompositeStep(
                    step_id=f"drift_{i}", skill_name="TrackDrift_ReferenceScan",
                    params={}, optional=True, checkpoint_after=False, tags=("drift",))
            yield CompositeStep(
                step_id=f"move_{sid}", skill_name="MoveToXY",
                params={"x_m": tx, "y_m": ty, "wait": True},
                optional=True, checkpoint_after=False, tags=("move", f"site={sid}"))
            yield CompositeStep(
                step_id=f"sts_{sid}", skill_name="AcquireSTS",
                params={
                    "ramp_before": True, "restore_after": True,
                    "spectro_setpoint_a": setpoint,
                    "ramp_slew_rate_v_per_s": slew,
                    "save_basename": f"{params.get('page_id', 'site')}_{s.get('n', i)}",
                },
                optional=True, checkpoint_after=True, tags=("sts", f"site={sid}"))

    # ── 分子漂移追踪 ───────────────────────────────────────────────────────

    def _tracker_for(self, params: dict, executor) -> "DR.DriftTracker":
        """本次运行的追踪器。run_composite 已放进 params；直接调 plan_dynamic（测试）时在这里补。

        续跑（sidecar / 上下文里有既往进度）时从 partial_data["drift"] 恢复 —— 那里不存 matches，
        所以恢复后到下一次配准之前锚点 / 局部位移不可用，退到全局漂移。"""
        tracker = params.get("_tracker")
        if tracker is None:
            saved = executor.progress.partial_data.get("drift")
            tracker = (DR.DriftTracker.from_dict(saved) if saved else DR.DriftTracker(
                weak_limit=int(params.get("stop_after_weak_registrations", DR.WEAK_STOP_N) or 0)))
            params["_tracker"] = tracker
        self._tracker = tracker
        return tracker

    def _track_until_usable(self, i: int, params: dict, executor, tracker, track: dict):
        """一次追踪。还没有任何可信配准时不在未校正的位置上测谱：立刻重来，直到有一次可信或判丢。

        后续的弱配准不重来 —— 规矩是「用外推漂移、连续三次弱才停」，重来只会白烧扫描时间
        （一帧坏了多半是针尖的事，不是再扫一遍就好）。``weak_limit=0``（不因此停）时也只扫一次，
        否则没有出口。"""
        attempt = 0
        # 次数本身有上限(= weak_limit):出口不能只靠追踪器记的账 —— 记账出了岔子就是无限重扫。
        max_attempts = max(1, tracker.weak_limit)
        while True:
            yield from self._track_event(f"{i}" if attempt == 0 else f"{i}r{attempt}",
                                         params, executor, tracker, track)
            attempt += 1
            if (tracker.last_good is not None or tracker.lost or tracker.weak_limit <= 0
                    or attempt >= max_attempts):
                return

    def _track_event(self, tag: str, params: dict, executor, tracker, track: dict):
        """一次「重扫参考帧 → 找到那一帧 → 配准」。失败（任一步）都记成一次弱配准。"""
        sid = f"track_{tag}"
        reg_id = f"{sid}:register"
        if _is_completed(executor, reg_id):
            return                       # 续跑：这一轮已经配过了，状态在 partial_data["drift"]
        executor.set_partial("track_events",
                             int(executor.progress.partial_data.get("track_events", 0)) + 1)
        first = tracker.last_good is None
        scan_id = f"{sid}:scan"
        t0 = DR._now()
        yield CompositeStep(step_id=scan_id, skill_name="ScanAt", params=dict(track["scan"]),
                            optional=True, checkpoint_after=False, tags=("drift", "scan"))
        t1 = DR._now()
        if not self._step_ok(executor, scan_id):
            self._note_weak(executor, tracker, self._why(executor, scan_id, tr(
                "追踪扫描没成功", "the tracking scan did not succeed")), t1)
            return
        path = _find_tracking_frame(getattr(executor, "context", None), t0)
        if not path:
            save_id = f"{sid}:save"
            yield CompositeStep(step_id=save_id, skill_name="SaveScan", params={},
                                optional=True, checkpoint_after=False, tags=("drift", "save"))
            res = (getattr(executor, "sub_results", None) or {}).get(save_id)
            path = (getattr(res, "data", None) or {}).get("saved_path") if res is not None else None
        if not path:
            self._note_weak(executor, tracker, tr("找不到刚扫的追踪帧", "could not find the tracking frame just scanned"),
                            DR._now())
            return
        t_mid = (t0 + t1) / 2.0
        prev = tracker.predict(t_mid)["drift_nm"]
        # 没有先验的第一次配准直接用宽窗口：参考帧可能已经是很久以前的了。
        window = DR.WIDE_WINDOW_NM if first else DR.WINDOW_NM
        executor.set_partial("track_t_mid", t_mid)
        yield CompositeStep(
            step_id=reg_id, skill_name="RegisterDriftByMolecules",
            params={"frame_path": path, "reference_frame_path": track["frame_path"],
                    "prev_drift_x_nm": float(prev[0]), "prev_drift_y_nm": float(prev[1]),
                    "window_nm": float(window), "wide_window_nm": float(max(window, DR.WIDE_WINDOW_NM)),
                    "min_quality": float(tracker.min_quality)},
            optional=True, checkpoint_after=True, tags=("drift", "register"))
        if not self._step_ok(executor, reg_id):
            self._note_weak(executor, tracker, self._why(executor, reg_id, tr(
                "配准技能没给出结果", "the registration skill gave no result")), DR._now())

    @staticmethod
    def _step_ok(executor, step_id: str) -> bool:
        return step_id in (getattr(executor, "sub_results", None) or {}) or _is_completed(executor, step_id)

    @staticmethod
    def _why(executor, step_id: str, default: str) -> str:
        return str((getattr(executor.progress, "failed_reasons", None) or {}).get(step_id) or default)

    def _note_weak(self, executor, tracker, reason: str, t: float) -> None:
        tracker.note_failure(reason, t)
        executor.set_partial("drift", tracker.to_dict(include_matches=False))
        logger.warning("MeasureSpectraAtSites: 漂移追踪这一轮没配出来（%s），连续弱配准 %d/%s",
                       reason, tracker.weak_streak, tracker.weak_limit or "∞")
        self._push_live_safely()

    @staticmethod
    def _drift_lost_text(tracker) -> str:
        last = next((str(r.get("reason")) for r in reversed(tracker.history) if r.get("reason")), "")
        return tr(
            f"漂移丢失：连续 {tracker.weak_streak} 次分子配准不可信（质量<{tracker.min_quality:g} / "
            "匹配太少 / 追踪步骤失败），停止测谱 —— 再测只会打在错位的位点上。请检查针尖和成像"
            "（扫出来还能不能看清分子），必要时重做参考帧。" + (f"最近一次的原因：{last}。" if last else ""),
            f"Drift lost: {tracker.weak_streak} consecutive molecular registrations were not "
            f"trustworthy (quality<{tracker.min_quality:g} / too few matches / tracking step failed). "
            "Stopped measuring - continuing would land on the wrong sites. Check the tip and the "
            "imaging (are the molecules still resolved?) and redo the reference frame if needed."
            + (f" Latest reason: {last}." if last else ""))

    @staticmethod
    def _corrected_target(s: dict, tracker, angle_deg) -> tuple[float, float, dict]:
        """位点的目标（扫描器米）= 名义位置 + 该位点的位移（帧轴 → 扫描器轴）。

        换算经 ``frame_delta_to_scanner_m``（单一来源的 ``px_to_m``），和位点坐标本身的换算同一套
        符号，所以 θ≠0 的帧上位移也转得对。"""
        nx = float(s["rx_nm"]) if s.get("rx_nm") is not None else float(s.get("x_nm", 0.0))
        ny = float(s["ry_nm"]) if s.get("ry_nm") is not None else float(s.get("y_nm", 0.0))
        info = tracker.displacement_for_site(
            {"x_nm": nx, "y_nm": ny, "kind": s.get("kind"), "rkind": s.get("rkind"),
             "anchor": s.get("anchor") if s.get("_anchors_ok", True) else []},
            DR._now())
        dx_m, dy_m = DR.frame_delta_to_scanner_m(info["delta_nm"][0], info["delta_nm"][1], angle_deg)
        return s["scanner_x_m"] + dx_m, s["scanner_y_m"] + dy_m, info

    def _note_site_drift(self, executor, sid: str, info: dict) -> None:
        try:
            seen = dict(executor.progress.partial_data.get("site_drift") or {})
            if len(seen) >= _MAX_SITES and sid not in seen:
                return
            seen[sid] = {"delta_nm": [round(info["delta_nm"][0], 4), round(info["delta_nm"][1], 4)],
                         "mode": info["mode"], "source": info["source"]}
            executor.set_partial("site_drift", seen)
        except Exception:  # noqa: BLE001 — 记账不该拖垮测谱
            pass

    # ── hooks：回写页面 + 连续失败停机 ──────────────────────────────────

    def on_step_result(self, step: CompositeStep, sub_result) -> None:
        pd = self._executor.progress.partial_data
        page_id = pd.get("page_id")
        if step.step_id.endswith(":register") and step.step_id.startswith("track_"):
            self._on_registration(sub_result)
            return
        if step.step_id.startswith("move_"):
            # 这一步到位了；记一下，谱若紧接着失败才好区分「没走到」vs「谱本身坏」。
            return
        if not step.step_id.startswith("sts_"):
            return
        sid = step.step_id[len("sts_"):]
        failed_moves = pd.get("failed_moves") or []
        if sid in failed_moves:
            # 移针失败 ⇒ 这条谱在错位置上，不算好点，也不标 done。
            self._executor.set_partial("suspect", int(pd.get("suspect", 0)) + 1)
            self._note(sid, "suspect")
            if page_id:
                _apply_patch_quiet(page_id, {"target": "sites", "id": sid, "op": "update",
                                             "data": {"status": "skipped",
                                                      "result": {"why": "move_failed"}}})
            return
        self._executor.set_partial("streak", 0)   # 成功清零连续失败
        self._executor.set_partial("succeeded", int(pd.get("succeeded", 0)) + 1)
        self._note(sid, "done")
        if page_id:
            data = getattr(sub_result, "data", None) or {}
            res = {"num_points": data.get("num_points"), "path": data.get("path"),
                   "working_point_restored": data.get("working_point_restored")}
            sd = (pd.get("site_drift") or {}).get(sid)
            if sd:                                  # 这一点用的漂移修正，留给事后核对
                res["drift_nm"] = sd["delta_nm"]
                res["drift_mode"] = sd["mode"]
            _apply_patch_quiet(page_id, {"target": "sites", "id": sid, "op": "update",
                                         "data": {"status": "done",
                                                  "result": {k: v for k, v in res.items() if v is not None}}})
            self._push_live(page_id)

    def _on_registration(self, sub_result) -> None:
        tracker = getattr(self, "_tracker", None)
        if tracker is None:
            return
        data = getattr(sub_result, "data", None) or {}
        t_mid = float(self._executor.progress.partial_data.get("track_t_mid") or DR._now())
        trusted = tracker.note_registration(data, t_mid)
        self._executor.set_partial("drift", tracker.to_dict(include_matches=False))
        dn = data.get("drift_nm") or [float("nan")] * 2
        logger.info("MeasureSpectraAtSites: 分子配准 drift=(%.3f, %.3f) nm 质量 %.2f 匹配 %s/%s rms %s → %s；"
                    "连续弱配准 %d/%s",
                    dn[0], dn[1], float(data.get("quality") or 0.0), data.get("n_matched"),
                    data.get("n_expected"), data.get("rms_nm"), "可信" if trusted else "不可信",
                    tracker.weak_streak, tracker.weak_limit or "∞")
        seen = list(self._executor.progress.partial_data.get("drift_warnings") or [])
        for w in data.get("warnings") or []:
            if str(w) not in seen and len(seen) < 10:
                seen.append(str(w))
                logger.warning("MeasureSpectraAtSites: 配准提示：%s", w)
        self._executor.set_partial("drift_warnings", seen)
        self._push_live_safely()

    def on_step_failed(self, step: CompositeStep, msg: str) -> bool:
        pd = self._executor.progress.partial_data
        page_id = pd.get("page_id")
        if step.step_id.startswith("move_"):
            sid = step.step_id[len("move_"):]
            fm = list(pd.get("failed_moves") or [])
            if sid not in fm:
                fm.append(sid)
                self._executor.set_partial("failed_moves", fm)
        if step.step_id.startswith(("move_", "sts_")):
            self._executor.set_partial("failed", int(pd.get("failed", 0)) + 1)
            # 连续失败计数（move 或 sts 都算一次「不顺」）；成功时 on_step_result 清零。
            # 真正的「停」由 plan_dynamic 在发下一个点前读这个 streak 决定 —— on_step_failed
            # 的返回值挡不住 optional 步骤（执行器对 optional 失败一律继续）。
            self._executor.set_partial("streak", int(pd.get("streak", 0)) + 1)
            if step.step_id.startswith("sts_"):
                sid = step.step_id[len("sts_"):]
                self._note(sid, "failed")
                if page_id:
                    _apply_patch_quiet(page_id, {"target": "sites", "id": sid, "op": "update",
                                                 "data": {"status": "failed",
                                                          "result": {"error": str(msg)[:200]}}})
        # track_* 步骤的失败不进上面的 streak（那是谱的连续失败）；它们由 _track_event 在步骤之后
        # 记成一次弱配准。这里只放行（optional）。
        return step.optional

    def _note(self, sid: str, status: str) -> None:
        try:
            seen = dict(self._executor.progress.partial_data.get("site_status") or {})
            if len(seen) >= _MAX_SITES and sid not in seen:
                return
            seen[sid] = status
            self._executor.set_partial("site_status", seen)
        except Exception:  # noqa: BLE001 — 记账不该拖垮测谱
            pass

    def _drift_text(self) -> str:
        """页面进度条里附带的一句漂移状态；没在追踪或还没有可信配准就是空串。"""
        tracker = getattr(self, "_tracker", None)
        lg = tracker.last_good if tracker is not None else None
        if tracker is None or lg is None:
            return ""
        flag = "" if tracker.latest_ok else tr("（最近一次不可信，用外推）", " (latest not trustworthy, extrapolating)")
        return tr(f" · 漂移 ({lg['drift_nm'][0]:+.2f}, {lg['drift_nm'][1]:+.2f}) nm，质量 {lg['quality']:.2f}{flag}",
                  f" · drift ({lg['drift_nm'][0]:+.2f}, {lg['drift_nm'][1]:+.2f}) nm, quality {lg['quality']:.2f}{flag}")

    def _push_live_safely(self) -> None:
        ex = getattr(self, "_executor", None)
        page_id = ex.progress.partial_data.get("page_id") if ex is not None else None
        if page_id:
            self._push_live(page_id)

    def _push_live(self, page_id: str) -> None:
        pd = self._executor.progress.partial_data
        done = int(pd.get("succeeded", 0))
        total = int(pd.get("total_sites", 0))
        import datetime
        _apply_patch_quiet(page_id, {"target": "doc", "path": "status/live", "op": "set", "data": {
            "done": done, "total": total, "phase": "measuring",
            "text": tr(f"测谱中：已完成 {done}/{total}", f"Measuring: {done}/{total} done") + self._drift_text(),
            "at": datetime.datetime.now().astimezone().isoformat(),
        }})

    def aggregate(self, sub_results: dict, progress: CompositeProgress) -> dict:
        pd = progress.partial_data
        out = {
            "page_id": pd.get("page_id"),
            "total_sites": int(pd.get("total_sites", 0)),
            "succeeded": int(pd.get("succeeded", 0)),
            "failed": int(pd.get("failed", 0)),
            "suspect": int(pd.get("suspect", 0)),
            "site_status": dict(pd.get("site_status") or {}),
        }
        tracker = getattr(self, "_tracker", None)
        if tracker is not None:
            out["drift_method"] = "molecules"
            out["drift"] = tracker.summary()
            out["site_drift"] = dict(pd.get("site_drift") or {})
            out["tracking_scan_frame"] = pd.get("tracking_scan_frame")
            out["drift_warnings"] = list(pd.get("drift_warnings") or [])
        return out

    # ── 入口 ───────────────────────────────────────────────────────────

    def run_composite(self, context, params: dict) -> SkillResult:
        page_id = str(params.get("page_id", "") or "").strip()
        if not page_id:
            return self.fail(tr("缺 page_id。", "page_id is required."))
        self._tracker = None            # 实例可能被复用，别带着上一次的追踪状态

        # 1) 读站位（ReadSiteMarks 是只读技能，直接调）。
        try:
            from mast.skills.builtins.site_marking_read import ReadSiteMarks
            read = ReadSiteMarks().execute(context, {
                "page_id": page_id,
                "refine": bool(params.get("refine", True)),
                "frame_path": str(params.get("frame_path", "") or ""),
            })
        except Exception as exc:  # noqa: BLE001
            return self.fail(tr(f"读位点失败：{exc}", f"Failed to read sites: {exc}"))
        if not getattr(read, "success", False):
            return self.fail(tr(f"读不到位点：{getattr(read, 'error', '')}",
                                f"Could not read sites: {getattr(read, 'error', '')}"),
                             read=getattr(read, "data", None))
        rd = read.data or {}
        sites = [s for s in (rd.get("sites") or [])
                 if s.get("scanner_x_m") is not None and s.get("scanner_y_m") is not None]

        # 2) 闸门：标注完成、坐标可信（可关）。
        doc = rd.get("doc") or {}
        marking = (doc.get("plan/marking") or {}) if isinstance(doc, dict) else {}
        if bool(params.get("require_marking_done", True)):
            # doc 里没带 marking 时，单独读一次（ReadSiteMarks 可能没透传 doc）。
            done_flag = marking.get("done")
            if done_flag is None:
                try:
                    from mast.api.routes.site_marking import read_doc
                    done_flag = ((read_doc(page_id).get("plan/marking") or {}) or {}).get("done")
                except Exception:  # noqa: BLE001
                    done_flag = None
            if not done_flag:
                return self.fail(tr(
                    "页面还没点「标注完成」(plan/marking.done)——先让操作员标完，或把 require_marking_done 关掉。",
                    "The page is not marked done (plan/marking.done) yet — finish marking, or turn off require_marking_done."))
        if bool(params.get("require_coords_trustworthy", True)) and not rd.get("coords_trustworthy", False):
            warns = rd.get("warnings") or []
            return self.fail(tr(
                "坐标不可信，拒绝自动批量测谱（非方形帧 / 角度未知 / 页面与 frame_path 不是同一帧 / 有站位在帧外）。"
                "先按 ReadSiteMarks 的提示核对，或把 require_coords_trustworthy 关掉自担风险。"
                + ("；".join(str(w) for w in warns[:2]) if warns else ""),
                "Coordinates are not trustworthy; refusing autonomous bulk spectroscopy (non-square frame / "
                "unknown angle / page and frame_path are not the same frame / sites outside the frame). "
                "Check as ReadSiteMarks suggests, or turn off require_coords_trustworthy to proceed at your own risk."
                + ("; ".join(str(w) for w in warns[:2]) if warns else "")),
                read=rd)
        if not sites:
            return self.fail(tr("没有可用的位点（坐标都换算不出来，或页面上没点）。",
                                "No usable sites (no convertible coordinates, or nothing marked)."),
                             read=rd)
        if len(sites) > _MAX_SITES:
            sites = sites[:_MAX_SITES]

        # 3) 分子漂移追踪的前置：有参考、参考与页面是同一帧、能检出分子。备不好就直接说，
        #    不要等扫了几帧、测了几条谱之后才发现追踪根本没在工作。
        params = {**params, "_sites": sites, "page_id": page_id}
        track_every = int(params.get("track_every", 0) or 0)
        method = str(params.get("drift_method") or "molecules")
        if track_every and method == "molecules":
            track, err = self._prepare_molecule_tracking(params, rd, sites)
            if err:
                return self.fail(err, read=rd)
            params["_track"] = track
            self._tracker = DR.DriftTracker(
                weak_limit=int(params.get("stop_after_weak_registrations", DR.WEAK_STOP_N) or 0))
            params["_tracker"] = self._tracker

        # 4) 跑。站位塞进 params 供 plan() 用。
        executor = GraphExecutor(
            composite_name=self._skill_name(), context=context,
            on_step_result=self.on_step_result, on_step_failed=self.on_step_failed)
        executor.set_partial("page_id", page_id)
        executor.set_partial("total_sites", len(sites))
        executor.set_partial("stop_after_consecutive_failures",
                             int(params.get("stop_after_consecutive_failures", 3) or 0))
        executor.set_partial_default("succeeded", 0)
        executor.set_partial_default("failed", 0)
        executor.set_partial_default("streak", 0)
        self._executor = executor
        if "_track" in params:
            # ScanAt 把仪器的扫描框改成了参考帧的位置 / 大小 / 角度,测完不会自己改回去 —— 记下来。
            executor.set_partial("tracking_scan_frame", dict(params["_track"]["scan"]))
            executor.set_partial("drift_warnings", list(params["_track"].get("warnings") or []))
        saved = executor.progress.partial_data.get("drift")
        if self._tracker is not None and saved:          # 续跑：把追踪状态接上（不含 matches）
            self._tracker = DR.DriftTracker.from_dict(saved)
            self._tracker.weak_limit = int(params.get("stop_after_weak_registrations", DR.WEAK_STOP_N) or 0)
            params["_tracker"] = self._tracker

        executor.run_plan(self.plan_dynamic(params, executor))
        data = self.aggregate(executor.sub_results, executor.progress)
        data["_progress"] = executor.progress.to_dict()
        stop_reason = executor.progress.partial_data.get("stop_reason") or ""
        if not executor.progress.aborted:
            executor.clear_sidecar()
            # 收尾把进度推成 finished（中止时保留最后状态，让人看到停在哪）。
            self._push_final_live(page_id, data, stop_reason)

        if data.get("succeeded", 0) > 0:
            if stop_reason:
                data["stopped_early"] = stop_reason
            return self.ok(**data)
        reason = executor.progress.aborted_reason or stop_reason or tr(
            f"{data['total_sites']} 个位点无一测出可用谱。",
            f"None of the {data['total_sites']} sites produced a usable spectrum.")
        return self.fail(reason, **data)

    def _prepare_molecule_tracking(self, params: dict, rd: dict, sites: list[dict]) -> tuple[dict | None, str]:
        """分子漂移追踪的前置检查 → ``(track, error)``。

        ``track`` 是 plan_dynamic 要用的一切：追踪帧怎么扫（沿用参考帧的中心 / 大小 / **扫描角**，
        否则两帧不是同一个平移）、参考帧路径、锚点下标是否对得上。"""
        frame_path = str(params.get("frame_path", "") or "").strip()
        if not frame_path:
            return None, tr(
                "分子漂移追踪要有参考帧：请给 frame_path（标注页所基于的那张 .sxm），或把 track_every 设 0 / "
                "drift_method 设 reference_scan。",
                "Molecular drift tracking needs a reference frame: give frame_path (the .sxm the marking "
                "page was made from), or set track_every to 0 / drift_method to reference_scan.")
        fr = rd.get("frame") or {}
        if not fr or not fr.get("angle_known") or fr.get("angle_deg") is None:
            return None, tr("页面的帧几何里没有扫描角，没法按参考帧的轴重扫追踪帧。",
                            "The page's frame geometry has no scan angle; cannot re-scan the tracking "
                            "frame in the reference frame's axes.")
        w, h = float(fr["w_nm"]), float(fr["h_nm"])
        if abs(w - h) > 0.02 * w:
            return None, tr("参考帧不是方形（追踪帧用 ScanAt，只能扫方形）。",
                            "The reference frame is not square (the tracking frame uses ScanAt, which scans squares only).")
        ref = DR.molecules_in_frame(frame_path)
        if ref.error or ref.geo is None:
            return None, tr(f"参考帧读不了：{ref.error}", f"Cannot read the reference frame: {ref.error}")
        if len(ref.pts) < DR.MIN_MOLECULES:
            return None, tr(f"参考帧上只检出 {len(ref.pts)} 个分子，没法做分子配准。",
                            f"Only {len(ref.pts)} molecule(s) detected on the reference frame; molecular "
                            f"registration is not possible.")
        gd = ref.geo.as_dict()
        if gd.get("angle_deg") is None:
            return None, tr("参考帧的文件头里没有扫描角。", "The reference frame's header has no scan angle.")
        diffs = _geometry_diffs(fr, gd)
        if diffs:
            return None, tr(
                "frame_path 与标注页不是同一帧（" + "; ".join(diffs) + "）——按它做分子配准会对到错的分子上。",
                "frame_path is not the frame the page was marked on (" + "; ".join(diffs) + ") - "
                "registering against it would match the wrong molecules.")
        anchors_ok = True
        for k, v in (rd.get("anchor_molecules") or {}).items():
            try:
                i = int(k)
                if i >= len(ref.pts) or math.hypot(float(ref.pts[i][0]) - float(v["x_nm"]),
                                                   float(ref.pts[i][1]) - float(v["y_nm"])) > _ANCHOR_TOL_NM:
                    anchors_ok = False
                    break
            except (KeyError, TypeError, ValueError):
                anchors_ok = False
                break
        if not anchors_ok:
            logger.warning("MeasureSpectraAtSites: 参考帧上重新检出的分子与 ReadSiteMarks 的锚点分子对不上，"
                           "锚点位移不可用，位点退到局部 / 全局漂移")
            for s in sites:
                s["_anchors_ok"] = False
        warns: list[str] = []
        page_file = str(fr.get("file") or "").strip()
        if page_file and Path(frame_path).name.lower() != page_file.lower():
            warns.append(tr(
                f"frame_path（{Path(frame_path).name}）与标注页记录的帧（{page_file}）文件名不同：几何一致所以照用，"
                "但漂移是相对 frame_path 这张图量的 —— 它必须就是标注时的那一帧，否则每个位点都会带上两张图之间的差。",
                f"frame_path ({Path(frame_path).name}) has a different file name from the frame the page "
                f"records ({page_file}): used because the geometry matches, but drift is measured against "
                "frame_path - it must be the frame the marks were made on, otherwise every site inherits "
                "the difference between the two images."))
            logger.warning("MeasureSpectraAtSites: %s", warns[-1])
        scan = {"center_x_m": float(fr["cx_nm"]) * 1e-9, "center_y_m": float(fr["cy_nm"]) * 1e-9,
                "size_m": w * 1e-9, "angle_deg": float(fr["angle_deg"])}
        if int(params.get("track_pixels", 0) or 0) > 0:
            scan["pixels"] = int(params["track_pixels"])
        if float(params.get("track_line_time_s", 0.0) or 0.0) > 0:
            scan["line_time_s"] = float(params["track_line_time_s"])
        return {"frame_path": frame_path, "scan": scan, "n_ref": int(len(ref.pts)),
                "anchors_ok": anchors_ok, "warnings": warns,
                "frame": {"angle_deg": float(fr["angle_deg"]), "cx_nm": float(fr["cx_nm"]),
                          "cy_nm": float(fr["cy_nm"]), "w_nm": w, "h_nm": h}}, ""

    def _push_final_live(self, page_id: str, data: dict, stop_reason: str = "") -> None:
        import datetime
        done, total = int(data.get("succeeded", 0)), int(data.get("total_sites", 0))
        text = tr(f"测谱结束：{done}/{total} 成功，{int(data.get('failed', 0))} 失败。",
                  f"Done: {done}/{total} succeeded, {int(data.get('failed', 0))} failed.")
        if stop_reason:
            text += " " + stop_reason
        _apply_patch_quiet(page_id, {"target": "doc", "path": "status/live", "op": "set", "data": {
            "done": done, "total": total, "phase": "finished",
            "text": text,
            "at": datetime.datetime.now().astimezone().isoformat(),
        }})


def _is_completed(executor, step_id: str) -> bool:
    """续跑时已经跑完的步骤（真执行器有 ``is_completed``，测试用的桩没有）。"""
    fn = getattr(executor, "is_completed", None)
    try:
        return bool(fn(step_id)) if callable(fn) else False
    except Exception:  # noqa: BLE001
        return False


# v2 tool export
def make_tool(context_provider):
    return wrap_skill(MeasureSpectraAtSites, context_provider)
