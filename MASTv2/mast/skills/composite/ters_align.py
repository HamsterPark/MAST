"""M12 激光聚焦与光路对准（栅格）—— LaserFocusMap（规格 docs/v2/design/ters_modes_spec.md M12；契约 §8.3）。

以拉曼谱带 / STML 积分 / Nanonis AI / 电流为目标函数，对一或两条光学轴做栅格扫描，给出目标函数的二维分布、
峰位（三点抛物线细化）与「到最优点再验 3 次」的结论；没有显著改进就回起点（restore_on_fail）。
收尾：没有有效峰值（按 restore_on_fail）、``go_to_best=false``（只出图）、``dry_run_motion``（演练）都回起点；
被中止 / 停下则原地不动，各轴作为「未还原」连同起点位置报出来。

轴的写法 ``axis1`` / ``axis2``：

* ``'<device_id>:<axis>'`` —— optical_instruments.json 里的设备：闭环 / 旋转轴走 OpticalStageMove（原生单位，
  绝对位置），开环轴（Picomotor）走 PicomotorJog（相对步数，「位置」是卫星记账的步数，**不是位置反馈**）；
* ``'nanonis:<group>:<axis>'`` —— Nanonis PMD4 上标为光学用途的电机组，走 NanonisMotorGroupMove（相对步数，
  只记命令过的步数）。STM 粗动组那个技能自己会拒绝；``drive_policy = write_and_restore`` 的组在前置检查就拒绝
  （写驱动值只许人工，组合技能的子步骤会被硬闸拦下），只接受 ``verify_only`` 的组。

状态约束是本模式的核心（M12.1）：默认 ``tip_state_policy = withdrawn``（针尖已撤出才动）；``feedback_on_ok``
只许配置里标了 ``tunnel_safe: true`` 的轴（光学电机组看它的 ``requires``），每次移动后重新判稳；任何时刻只有一个轴在动。
``algorithm = hill_climb``（OptimizeOpticalSignal）尚未实现。
"""

from __future__ import annotations

import math
from typing import Iterator

import numpy as np

from mast.i18n import tr as _tr_lang
from mast.core.types import ParameterSpec, SkillMetadata
from mast.skills.composite import ters_analysis as ta
from mast.skills.composite.graph_executor import CompositeStep
from mast.skills.composite.ters_common import (
    GuardSpec,
    Needs,
    TersComposite,
    TersRun,
    common_extra,
    cp_a,
    cp_b,
    cp_c,
    ters_metadata,
)

__all__ = ["LaserFocusMap"]

_INF = _tr_lang("【默认值可调】", "[default adjustable]")


class _Axis:
    """一条参与对准的轴：怎么动、怎么读、单位、是否允许在隧穿态动。"""

    def __init__(self, spec: str) -> None:
        self.spec = str(spec).strip()
        parts = self.spec.split(":")
        self.kind = ""
        self.device = self.axis = ""
        self.group = ""
        self.unit = ""
        self.is_steps = False
        self.tunnel_safe = False
        self.limits: tuple[float | None, float | None] = (None, None)
        self.commanded = 0.0
        if len(parts) == 3 and parts[0].lower() == "nanonis":
            self.kind, self.group, self.axis = "nanonis_group", parts[1], parts[2]
            self.is_steps, self.unit = True, "steps"
        elif len(parts) == 2:
            self.kind, self.device, self.axis = "registry", parts[0], parts[1]
        else:
            raise ValueError(_tr_lang(f"轴 {spec!r} 写成 'device_id:axis' 或 'nanonis:<组>:<轴>'", f"Axis {spec!r} must be written as 'device_id:axis' or 'nanonis:<group>:<axis>'"))

    def resolve(self) -> str:
        """查配置（不动硬件）。返回拒绝原因，空串 = 可以。"""
        if self.kind == "nanonis_group":
            try:
                from mast.instruments.motor_groups import optical_group

                grp = optical_group(self.group)       # refuses the STM group / unknown / disabled
                grp.axis_letter(self.axis)
                self.tunnel_safe = str(grp.requires) in ("z_feedback_on", "none")
            except Exception as exc:  # noqa: BLE001 — the move skill re-checks everything
                return _tr_lang(f"Nanonis 电机组 {self.group}/{self.axis}：{exc}", f"Nanonis motor group {self.group}/{self.axis}: {exc}")
            if str(getattr(grp, "drive_policy", "verify_only")) != "verify_only":
                return (_tr_lang(f"Nanonis 电机组 {self.group} 的 drive_policy={grp.drive_policy}：写驱动值只许人工操作，"
                        "组合技能的子步骤在自主路径上会被硬闸拒绝 —— 只接受 verify_only 的组（或人工对准）", f"Nanonis motor group {self.group} has drive_policy={grp.drive_policy}: writing drive values is manual-only, and sub-steps of composite skills are refused by the hard gate on autonomous paths - only groups with verify_only are accepted (or align manually)"))
            return ""
        try:
            from mast.instruments.config import device_kind
            from mast.instruments.registry import get_instrument_registry

            reg = get_instrument_registry()
            entry = next((d for d in reg.find_devices() if str(d.get("id")) == self.device), None)
            if entry is None:
                return _tr_lang(f"设备 {self.device!r} 不在 optical_instruments.json 里（或没启用）", f"Device {self.device!r} is not in optical_instruments.json (or not enabled)")
            k = device_kind(str(entry.get("type")))
            if k == "spectrometer":
                return _tr_lang(f"{self.device} 是光谱仪，不是轴", f"{self.device} is a spectrometer, not an axis")
            ax = next((a for a in entry.get("axes") or [] if str(a.get("name")) == self.axis), None)
            if ax is None:
                return _tr_lang(f"{self.device} 没有轴 {self.axis!r}", f"{self.device} has no axis {self.axis!r}")
            self.is_steps = k == "openloop"
            self.unit = "steps" if self.is_steps else str(ax.get("unit") or "")
            self.limits = (ax.get("min_pos"), ax.get("max_pos"))
            self.tunnel_safe = bool(ax.get("tunnel_safe", entry.get("tunnel_safe", False)))
        except Exception as exc:  # noqa: BLE001
            return f"{self.spec}: {type(exc).__name__}: {exc}"
        return ""

    def read(self, run: TersRun) -> Iterator[CompositeStep]:
        if self.kind == "nanonis_group":
            return self.commanded
        if self.is_steps:
            res = yield from run.step(f"read:{self.spec}", "GetPicomotorSteps", {"device_id": self.device})
            axes = (res.data or {}).get("axes") or {}
            v = (axes.get(self.axis) or {}).get("steps")
            return float(v) if res.success and v is not None else None
        res = yield from run.step(f"read:{self.spec}", "OpticalStageGetPos",
                                  {"device_id": self.device, "axis": self.axis})
        return float((res.data or {}).get("position")) if res.success else None

    def move_to(self, run: TersRun, key: str, target: float, current: float) -> Iterator[CompositeStep]:
        """移到 ``target``（原生单位或步数）。返回新位置（读回 / 记账）或 None（失败，run 已停）。"""
        if self.kind == "registry" and not self.is_steps:
            res = yield from run.step(f"{key}:move", "OpticalStageMove",
                                      {"device_id": self.device, "axis": self.axis,
                                       "position": float(target), "wait": True, "timeout_s": 60.0})
            if not res.success:
                run.stop("needs_operator", _tr_lang(f"{key}: {self.spec} 移到 {target:g} 失败：{res.error}", f"{key}: {self.spec} move to {target:g} failed: {res.error}"))
                return None
            return float((res.data or {}).get("position", target))
        delta = int(round(target - current))
        if delta == 0:
            return current
        if self.kind == "registry":
            res = yield from run.step(f"{key}:jog", "PicomotorJog",
                                      {"device_id": self.device, "axis": self.axis, "steps": delta,
                                       "timeout_s": 60.0})
            if not res.success:
                run.stop("needs_operator", _tr_lang(f"{key}: {self.spec} 走 {delta:+d} 步失败：{res.error}", f"{key}: {self.spec} stepping {delta:+d} steps failed: {res.error}"))
                return None
            after = (res.data or {}).get("counter_after")
            return float(after) if after is not None else current + delta
        res = yield from run.step(f"{key}:motor", "NanonisMotorGroupMove",
                                  {"group": self.group, "axis": self.axis,
                                   "direction": "+" if delta > 0 else "-", "steps": abs(delta)})
        if not res.success:
            run.stop("needs_operator", _tr_lang(f"{key}: 电机组 {self.group}/{self.axis} 走 {delta:+d} 步失败：{res.error}", f"{key}: motor group {self.group}/{self.axis} stepping {delta:+d} steps failed: {res.error}"))
            return None
        self.commanded += delta
        return self.commanded


class LaserFocusMap(TersComposite):
    MODE_ID = "M12"
    MODE_KEY = "align"

    def metadata(self) -> SkillMetadata:
        mode = [
            ParameterSpec(name="algorithm", type="str", required=False, default="grid",
                          allowed_values=["grid", "hill_climb"],
                          description=_tr_lang("栅格扫描 / 爬山。目前只实现 grid（hill_climb = OptimizeOpticalSignal）。", "Grid scan / hill climb. Only grid is implemented (hill_climb = OptimizeOpticalSignal).")),
            ParameterSpec(name="objective", type="str", required=False, default="raman_band",
                          allowed_values=["raman_band", "stml_total", "adc_channel", "current_mean"],
                          description=_tr_lang("目标函数：拉曼谱带面积 / STML 积分强度 / Nanonis AI 均值 / 电流均值。", "Objective function: Raman band area / STML integrated intensity / Nanonis AI mean / current mean.") + _INF),
            ParameterSpec(name="band_cm1", type="str", required=False, default="",
                          description=_tr_lang("raman_band 的积分区间 'lo-hi'（cm⁻¹）；stml_total 时按 nm 理解；"
                                      "空 = 总计数。", "Integration range 'lo-hi' (cm⁻¹) for raman_band; interpreted as nm for stml_total; empty = total counts.") + _INF),
            ParameterSpec(name="adc_index", type="int", required=False, default=-1, min_value=-1,
                          max_value=127, description=_tr_lang("adc_channel 的 Nanonis 信号槽位（必填）。", "Nanonis signal slot for adc_channel (required).")),
            ParameterSpec(name="axis1", type="str", required=True,
                          description=_tr_lang("栅格第一轴：'device_id:axis'（ListOpticalDevices）或 'nanonis:<组>:<轴>'。", "First grid axis: 'device_id:axis' (ListOpticalDevices) or 'nanonis:<group>:<axis>'.")),
            ParameterSpec(name="axis2", type="str", required=False, default="",
                          description=_tr_lang("栅格第二轴；空 = 一维。", "Second grid axis; empty = one-dimensional.")),
            ParameterSpec(name="span1", type="float", required=True, min_value=0.0, max_value=1e6,
                          description=_tr_lang("第一轴相对起点的 ± 范围（轴原生单位：mm / µm / 步）。", "± range of the first axis relative to the start (axis native unit: mm / µm / steps).") + _INF),
            ParameterSpec(name="span2", type="float", required=False, default=0.0, min_value=0.0,
                          max_value=1e6, description=_tr_lang("第二轴 ± 范围。", "± range of the second axis.") + _INF),
            ParameterSpec(name="n1", type="int", required=False, default=9, min_value=3, max_value=51,
                          description=_tr_lang("第一轴点数。", "Number of points on the first axis.") + _INF),
            ParameterSpec(name="n2", type="int", required=False, default=9, min_value=1, max_value=51,
                          description=_tr_lang("第二轴点数（axis2 为空时忽略）。", "Number of points on the second axis (ignored when axis2 is empty).") + _INF),
            ParameterSpec(name="axes", type="str", required=False, default="",
                          description=_tr_lang("爬山轴列表（hill_climb 用；尚未实现）。", "List of hill-climb axes (used by hill_climb; not implemented).")),
            ParameterSpec(name="step_shrink", type="float", required=False, default=0.5, min_value=0.1,
                          max_value=0.9, description=_tr_lang("爬山失败后步长收缩因子（hill_climb）。", "Step shrink factor after a failed hill-climb step (hill_climb).")),
            ParameterSpec(name="margin_sigma", type="float", required=False, default=2.0, min_value=0.5,
                          max_value=10.0,
                          description=_tr_lang("「最优显著优于起点」所需的改进（σ_J 的倍数）；不够 ⇒ restore_on_fail。", "Improvement required for the optimum to be significantly better than the start (multiple of σ_J); if not enough, restore_on_fail applies.") + _INF),
            ParameterSpec(name="max_evals", type="int", required=False, default=60, min_value=5,
                          max_value=500, description=_tr_lang("爬山最大评估次数（hill_climb）。", "Maximum number of hill-climb evaluations (hill_climb).")),
            ParameterSpec(name="repeats_per_eval", type="int", required=False, default=2, min_value=1,
                          max_value=20,
                          description=_tr_lang("每个位置的重复评估次数（谱目标函数 = 一次采 repeats 帧取平均；电信号 = 读 repeats×10 次）。", "Evaluations per position (spectral objective = acquire `repeats` frames and average; electrical signal = read repeats×10 times).") + _INF),
            ParameterSpec(name="n_noise", type="int", required=False, default=3, min_value=2,
                          max_value=20, description=_tr_lang("起点噪声估计的评估次数 ⇒ J_start、σ_J。", "Number of evaluations at the start for the noise estimate => J_start, σ_J.") + _INF),
            ParameterSpec(name="max_noise_cv", type="float", required=False, default=0.5,
                          min_value=0.01, max_value=10.0,
                          description=_tr_lang("σ_J / J_start 超过它 ⇒ 拒绝继续（目标函数不稳定，对准无意义；M12.9）。"
                                      "规格没给阈值，这是实现加的。", "If σ_J / J_start exceeds this, refuse to continue (objective unstable, alignment meaningless; M12.9). The spec gives no threshold; this is an implementation addition.") + _INF),
            ParameterSpec(name="unidirectional", type="bool", required=False, default=True,
                          description=_tr_lang("每行都从低端单向趋近（先退一格再走过来），吃掉开环台的反向间隙。", "Approach every row unidirectionally from the low end (step back one point, then come forward) to absorb the backlash of open-loop stages.")),
            ParameterSpec(name="tip_state_policy", type="str", required=False, default="withdrawn",
                          allowed_values=["withdrawn", "feedback_on_ok", "any"],
                          description=_tr_lang("运动期间针尖状态要求：withdrawn = 已撤针；feedback_on_ok = 隧穿态，只许 "
                                      "tunnel_safe 的轴，移动后重新判稳；any = 不检查（只记录）。", "Tip state required during motion: withdrawn = tip withdrawn; feedback_on_ok = tunnelling state, only tunnel_safe axes allowed, re-stabilise after each move; any = no check (record only).") + _INF),
            ParameterSpec(name="go_to_best", type="bool", required=False, default=True,
                          description=_tr_lang("结束后移到最优点并验 3 次；false = 只出图，结束回起点。", "After the run, move to the optimum and verify 3 times; false = only produce the map and return to the start.")),
            ParameterSpec(name="restore_on_fail", type="bool", required=False, default=True,
                          description=_tr_lang("最优不显著优于起点、或根本没有有效峰值 ⇒ 回起点；false = 停在原处（报「未还原」）。", "If the optimum is not significantly better than the start, or there is no valid peak, return to the start; false = stay where it is (reported as not restored).")),
            ParameterSpec(name="max_excursion", type="str", required=False, default="",
                          description=_tr_lang("各轴相对起点的最大位移 'device:axis:value,…'（或 'nanonis:组:轴:value'）；"
                                      "空 = 只受配置软限位约束。", "Maximum displacement of each axis from the start 'device:axis:value,...' (or 'nanonis:group:axis:value'); empty = limited only by the configured soft limits.") + _INF),
        ]
        return ters_metadata(
            name="LaserFocusMap",
            description=(
                _tr_lang("激光聚焦与光路对准 · 栅格（M12）：以拉曼谱带 / STML 积分 / Nanonis AI / 电流为目标函数，对 axis1（和 "
                "axis2）在起点 ± span 内做 n1 × n2 栅格，每行单向趋近、每次只动一个轴、到位后等 settle_s（隧穿态再判稳）"
                "再评估；先在起点评估 n_noise 次得 σ_J（太大拒绝继续）。结束移到最优点验 3 次，不显著优于起点就回起点；"
                "没有有效峰值、go_to_best=false、dry_run_motion 演练也都回起点；被中止则原地停，各轴报「未还原」与起点。"
                "默认要求针尖已撤出；隧穿态只许标了 tunnel_safe 的轴。开环轴的「位置」是步数记账，不是位置反馈。"
                "Nanonis 光学电机组只接受 drive_policy=verify_only。"
                "LiveHub：local/align/<run_id>（map2d，轴原生单位）。", "Laser focus and optical path alignment, grid (M12): with Raman band / STML integral / Nanonis AI / current as the objective function, run an n1 × n2 grid over axis1 (and axis2) within start ± span, approaching each row unidirectionally, moving only one axis at a time, waiting settle_s after arrival (re-stabilising in the tunnelling state) before evaluating; first evaluate n_noise times at the start to get σ_J (refuse to continue if too large). At the end move to the optimum and verify 3 times; if it is not significantly better than the start, return to the start; no valid peak, go_to_best=false and dry_run_motion rehearsal also return to the start; if aborted, stay in place and report each axis as 'not restored' together with the start. By default the tip must be withdrawn; the tunnelling state is only allowed for axes marked tunnel_safe. The 'position' of an open-loop axis is step bookkeeping, not position feedback. Nanonis optical motor groups only accept drive_policy=verify_only. LiveHub: local/align/<run_id> (map2d, axis native units).")),
            parameters=[*mode, *cp_a(), *cp_b(notes={"settle_s": _tr_lang("（这里 = 轴运动后的整定时间）", "(here = settling time after axis motion)")}),
                        *cp_c(), *common_extra()],
            tags=["optics", "alignment", "optimization", "stage", "acquisition"],
            duration_s=600.0,
        )

    def needs(self, run: TersRun) -> Needs:
        p = run.p
        spectral = p["objective"] in ("raman_band", "stml_total")
        tip = {"withdrawn": "withdrawn", "feedback_on_ok": "tunneling"}.get(p["tip_state_policy"], "any")
        return Needs(tip=tip, spectrometer=spectral, safetip=tip == "tunneling",
                     lockin_off=False)

    def prepare(self, run: TersRun) -> list[str]:
        p = run.p
        out = []
        if p["algorithm"] != "grid":
            return [_tr_lang("algorithm=hill_climb（OptimizeOpticalSignal）尚未实现 —— 用 grid", "algorithm=hill_climb (OptimizeOpticalSignal) is not implemented - use grid")]
        if p["objective"] == "adc_channel" and int(p["adc_index"]) < 0:
            out.append(_tr_lang("objective=adc_channel 时 adc_index 必填", "adc_index is required when objective=adc_channel"))
        try:
            self._ax = [_Axis(p["axis1"])] + ([_Axis(p["axis2"])] if str(p.get("axis2") or "").strip() else [])
        except ValueError as exc:
            return [str(exc)]
        for ax in self._ax:
            err = ax.resolve()
            if err:
                out.append(err)
        if p["objective"] == "stml_total" and p.get("band_cm1"):
            # the band is in nm for STML: keep it out of the Raman-shift band machinery
            self._stml_band = ta.parse_range(p["band_cm1"], what="band_cm1 (nm)")
            run.bands = []
        else:
            self._stml_band = None
        if p["tip_state_policy"] == "feedback_on_ok":
            unsafe = [ax.spec for ax in self._ax if not ax.tunnel_safe]
            if unsafe:
                out.append(_tr_lang(f"tip_state_policy=feedback_on_ok 只许 tunnel_safe 的轴；{unsafe} 没有登记 "
                           "tunnel_safe（需逐轴登记）", f"tip_state_policy=feedback_on_ok only allows tunnel_safe axes; {unsafe} not registered as tunnel_safe (to be registered per axis)"))
        exc = {}
        for item in (s for s in str(p.get("max_excursion") or "").split(",") if s.strip()):
            *name, val = item.strip().split(":")
            try:
                exc[":".join(name)] = abs(float(val))
            except ValueError:
                out.append(_tr_lang(f"max_excursion 项 {item!r} 写成 'device:axis:value'", f"max_excursion item {item!r} must be written as 'device:axis:value'"))
        n1 = int(p["n1"])
        n2 = int(p["n2"]) if len(self._ax) > 1 else 1
        spans = [float(p["span1"]), float(p["span2"])]
        for i, ax in enumerate(self._ax):
            lim = exc.get(ax.spec)
            if lim is not None and spans[i] > lim:
                out.append(_tr_lang(f"{ax.spec}: span {spans[i]:g} 超过 max_excursion {lim:g}", f"{ax.spec}: span {spans[i]:g} exceeds max_excursion {lim:g}"))
        self._n = (n1, n2)
        run.total_points = n1 * n2 + int(p["n_noise"]) + (3 if p["go_to_best"] else 0)
        t_eval = (float(p["exposure_ms"]) / 1000.0 * int(p["repeats_per_eval"]) + 0.5
                  if run.p["objective"] in ("raman_band", "stml_total") else 0.3)
        est = 20.0 + run.total_points * (float(p["settle_s"]) + t_eval + 1.0)
        run.plan.update({"axes": [a.spec for a in self._ax], "grid": [n1, n2],
                         "estimated_s": round(est, 1), "objective": p["objective"]})
        if est > float(p["max_total_s"]):
            out.append(_tr_lang(f"预计 {est:.0f} s 超过 max_total_s={float(p['max_total_s']):.0f} s", f"Estimated {est:.0f} s exceeds max_total_s={float(p['max_total_s']):.0f} s"))
        return out

    def preflight_extra(self, run: TersRun) -> Iterator[CompositeStep]:
        """读起点；栅格的每个目标点都在配置软限位内（闭环轴）。"""
        p = run.p
        out = []
        self._start = []
        for ax in self._ax:
            v = yield from ax.read(run)
            if v is None:
                out.append(_tr_lang(f"{ax.spec}: 读不到起点位置", f"{ax.spec}: cannot read the start position"))
                self._start.append(0.0)
            else:
                self._start.append(float(v))
        spans = [float(p["span1"]), float(p["span2"])]
        self._grid = []
        for i, ax in enumerate(self._ax):
            n = self._n[i]
            pts = list(np.linspace(self._start[i] - spans[i], self._start[i] + spans[i], n)) if n > 1 \
                else [self._start[i]]
            if ax.is_steps:
                pts = [float(round(v)) for v in pts]
            lo, hi = ax.limits
            pre = pts[0] - (pts[1] - pts[0] if len(pts) > 1 else 0.0)
            if lo is not None and hi is not None and not ax.is_steps:
                if not all(float(lo) <= v <= float(hi) for v in pts + [pre]):
                    out.append(_tr_lang(f"{ax.spec}: 栅格 [{pre:g}, {pts[-1]:g}] 超出软限位 [{lo}, {hi}]", f"{ax.spec}: grid [{pre:g}, {pts[-1]:g}] exceeds the soft limits [{lo}, {hi}]"))
            self._grid.append(pts)
        if len(self._grid) == 1:
            self._grid.append([0.0])
        run.plan["start"] = self._start
        run.plan["grid_axes"] = [g[:60] for g in self._grid]
        return out

    def attestation_lines(self, run: TersRun) -> list[str]:
        return [_tr_lang(f"将移动 {[a.spec for a in self._ax]}（每行单向、一次一轴）；请确认光学台周围没有碰撞风险", f"Will move {[a.spec for a in self._ax]} (unidirectional per row, one axis at a time); make sure there is no collision risk around the optical table"),
                _tr_lang(f"针尖状态要求 {run.p['tip_state_policy']}", f"Tip state requirement {run.p['tip_state_policy']}")]

    def dry_plan(self, run: TersRun) -> None:
        run.plan["note"] = _tr_lang("dry_run：没有移动任何轴；grid_axes 是会走的目标", "dry_run: no axis moved; grid_axes are the targets that would be visited")

    # -- evaluation --
    def _eval(self, run: TersRun, key: str, values: dict) -> Iterator[CompositeStep]:
        """评估一次目标函数。返回 J（float）或 None（失败 / 停了）。"""
        p = run.p
        obj = p["objective"]
        reps = int(p["repeats_per_eval"])
        if obj in ("raman_band", "stml_total"):
            fb = p["tip_state_policy"] == "feedback_on_ok"
            guard = (GuardSpec(i_ref_a=run.i_set, frac=float(p["exposure_guard_current_frac"]),
                               expect_feedback=True) if fb else GuardSpec(expect_feedback=None))
            out = yield from run.acquire(key, key, guard=guard, frames=reps,
                                         extra={"objective": obj, **values})
            run.record(out, key=key, values=values)
            if not run.handle(out):
                return None
            if out.status == "dry_run":
                return float("nan")
            if obj == "stml_total" and self._stml_band is not None and out.axis_nm is not None:
                m = ta.band_metrics(out.axis_nm, out.spectrum,
                                    ta.Band("stml", *self._stml_band))
                return m.get("area") if m.get("valid") else None
            return out.band_value()
        vals = []
        for _ in range(reps * 10):
            if obj == "current_mean":
                v = run.read_float("Current_Get")
            else:
                v = run.read_float("Signals_ValGet", int(p["adc_index"]), 0)
            if v is not None:
                vals.append(abs(v) if obj == "current_mean" else v)
        j = float(np.mean(vals)) if vals else None
        run.record(None, key=key, status="ok" if j is not None else "read_failed",
                   values={**values, "J": j})
        return j

    def _go(self, run: TersRun, key: str, i: int, target: float) -> Iterator[CompositeStep]:
        ax = self._ax[i]
        new = yield from ax.move_to(run, f"{key}:a{i}", target, self._pos[i])
        if new is None:
            return False
        self._pos[i] = new
        # every move is a change until the run puts the axis back (restored) or keeps it (best point);
        # a stop in between leaves it here, reported as unrestored with its start position
        run.note_change(f"axis:{ax.spec}", original=self._start[i], current=new, how="alignment grid")
        return True

    def _final_positions(self) -> dict:
        return {a.spec: (pos, _tr_lang("步数记账，非绝对位置", "step bookkeeping, not an absolute position") if a.is_steps else a.unit)
                for a, pos in zip(self._ax, self._pos)}

    def _restore(self, run: TersRun, steps, why: str) -> Iterator[CompositeStep]:
        """所有轴单向趋近回起点。"""
        for i, s in enumerate(self._start):
            ok = yield from self._approach(run, f"restore:a{i}", i, s, steps[i])
            if not ok:
                return False
            run.note_restored(f"axis:{self._ax[i].spec}", s)
        run.analysis.update({"restored_to_start": True, "restore_reason": why,
                             "final_positions": self._final_positions()})
        return True

    def _approach(self, run: TersRun, key: str, i: int, target: float, step: float) -> Iterator[CompositeStep]:
        """单向趋近：目标之前一格 → 目标（unidirectional=true 时）。"""
        if run.p["unidirectional"] and step and self._pos[i] > target - step / 2:
            ok = yield from self._go(run, key + "pre", i, target - step)
            if not ok:
                return False
        return (yield from self._go(run, key, i, target))

    def _settle(self, run: TersRun, key: str) -> bool:
        if run.sleep(float(run.p["settle_s"])):
            run.stop("partial", "aborted by operator")
            return False
        if run.p["tip_state_policy"] == "feedback_on_ok":
            tr = run.tip_ready(label=key, i_target=run.i_set, feedback=True, settle_s=0.0)
            if not tr.passed:
                run.stop("needs_operator", _tr_lang(f"{key}: 移动后判稳失败（{tr.reason}）", f"{key}: stabilisation failed after the move ({tr.reason})"))
                return False
        return True

    def ters_plan(self, run: TersRun) -> Iterator[CompositeStep]:
        p = run.p
        g1, g2 = self._grid
        n1, n2 = len(g1), len(g2)
        d1 = (g1[1] - g1[0]) if n1 > 1 else 1.0
        d2 = (g2[1] - g2[0]) if n2 > 1 else 1.0
        run.live.open_main("map2d", title=_tr_lang("对准栅格 J(axis1, axis2)", "Alignment grid J(axis1, axis2)"),
                           x_label=self._ax[0].spec, x_unit=self._ax[0].unit,
                           y_label=self._ax[1].spec if len(self._ax) > 1 else "-",
                           y_unit=self._ax[1].unit if len(self._ax) > 1 else "-",
                           meta={"nx": n1, "ny": n2, "x0": float(g1[0]), "y0": float(g2[0]),
                                 "dx": float(d1) or 1.0, "dy": float(d2) or 1.0,
                                 "x_unit": self._ax[0].unit or "-",
                                 "y_unit": (self._ax[1].unit if len(self._ax) > 1 else "-") or "-",
                                 "open_loop_axes": [a.spec for a in self._ax if a.is_steps]})
        self._pos = list(self._start) + ([0.0] if len(self._start) == 1 else [])
        # noise at the start
        noise = []
        for k in range(int(p["n_noise"])):
            if run.check():
                return
            j = yield from self._eval(run, f"noise{k}", {"phase": "noise", "pos": list(self._pos)})
            if j is None:
                return
            noise.append(j)
        jn = [v for v in noise if v is not None and math.isfinite(v)]
        j_start = float(np.mean(jn)) if jn else None
        s_j = float(np.std(jn, ddof=1)) if len(jn) > 1 else None
        run.analysis.update({"J_start": j_start, "sigma_J": s_j})
        if not run.dry_run and j_start and s_j is not None and s_j / abs(j_start) > float(p["max_noise_cv"]):
            run.stop("needs_operator", _tr_lang(f"起点目标函数太不稳定（σ_J/J = {s_j / abs(j_start):.2f} > "
                                       f"{p['max_noise_cv']}）—— 对准没有意义，不继续", f"Start objective is too unstable (σ_J/J = {s_j / abs(j_start):.2f} > {p['max_noise_cv']}) - alignment is meaningless, not continuing"))
            return
        J = np.full((n2, n1), np.nan)
        for iy, y in enumerate(g2):
            if len(self._ax) > 1:
                ok = yield from self._approach(run, f"row{iy}", 1, y, d2)
                if not ok:
                    break
            for ix, x in enumerate(g1):
                if run.check():
                    break
                key = f"g_{ix}_{iy}"
                ok = yield from (self._approach(run, key, 0, x, d1) if ix == 0
                                 else self._go(run, key, 0, x))
                if not ok or not self._settle(run, key):
                    break
                j = yield from self._eval(run, key, {"phase": "grid", "ix": ix, "iy": iy,
                                                     "pos": list(self._pos)})
                if j is None:
                    break
                J[iy, ix] = j
                if math.isfinite(j):
                    run.live.publish({"ix": ix, "iy": iy, "values": {"J": j}})
            if run.stopped:
                break
        self._J = J
        peak = ta.peak_of_grid(J, g1, g2 if len(self._ax) > 1 else None)
        run.analysis["peak"] = peak
        steps = (d1, d2)
        if run.stopped:                  # aborted / needs_operator: stay put, axes reported as unrestored
            return
        if run.dry_run:                  # a rehearsal leaves the optics where it found them
            yield from self._restore(run, steps, _tr_lang("dry_run_motion：演练结束回起点", "dry_run_motion: rehearsal finished, back to start"))
            return
        if not peak.get("valid"):        # nothing to go to: not the grid's last corner either
            if p["restore_on_fail"]:
                yield from self._restore(run, steps, _tr_lang("没有有效峰值（restore_on_fail）", "No valid peak (restore_on_fail)"))
            return
        if not p["go_to_best"]:
            yield from self._restore(run, steps, _tr_lang("go_to_best=false：只出图", "go_to_best=false: map only"))
            return
        best = [peak["grid_axis1"]] + ([peak["grid_axis2"]] if len(self._ax) > 1 else [])
        yield from self._finish(run, best, j_start, s_j, steps)

    def _finish(self, run: TersRun, best, j_start, s_j, steps) -> Iterator[CompositeStep]:
        p = run.p
        for i, b in enumerate(best):
            ok = yield from self._approach(run, f"best:a{i}", i, b, steps[i])
            if not ok:
                return
        if not self._settle(run, "best"):
            return
        verify = []
        for k in range(3):
            j = yield from self._eval(run, f"verify{k}", {"phase": "verify", "pos": list(self._pos)})
            if j is None:
                return
            verify.append(j)
        jb = float(np.mean([v for v in verify if v is not None])) if verify else None
        better = (jb is not None and j_start is not None
                  and jb - j_start > float(p["margin_sigma"]) * float(s_j or 0.0))
        run.analysis.update({"J_best_verified": jb, "significant": bool(better),
                             "final_positions": self._final_positions()})
        if not better and p["restore_on_fail"]:
            yield from self._restore(run, steps, _tr_lang("最优点不显著优于起点（restore_on_fail）", "Optimum not significantly better than the start (restore_on_fail)"))
        else:
            for a in self._ax:
                run.keep_change(f"axis:{a.spec}", how="moved to the best grid point")

    def product_arrays(self, run: TersRun) -> dict:
        J = getattr(self, "_J", None)
        if J is None:
            return {}
        return {"J": J, "axis1": np.asarray(self._grid[0]), "axis2": np.asarray(self._grid[1])}
