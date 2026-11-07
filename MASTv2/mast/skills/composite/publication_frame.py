"""采集用于展示的高分辨图像。

在已有原子分辨的前提下执行调平、设置扫描参数、移动至起点、等待稳定并采集。
展示质量需要使用者独立判断，技能通过不代表论文质量已经得到标定。
MoveToXY 移动针尖，改变扫描框不能替代它；开始扫描还可能移动到扫描起点。
扫描速度单独设置并回读确认，ConfigureScan 使用 set_scan_speed=False，避免后续覆盖。
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from mast.core.types import (
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
)
from mast.skills.composite._tip_phases import _data, _ok, _scan_path
from mast.skills.composite.graph_executor import CompositeStep, GraphExecutor
from mast.skills.composite.prepare_noble_tip import _TipComposite
from mast.i18n import tr

logger = logging.getLogger(__name__)

# 入口集中度阈值用于决定是否投入展示帧的采集预算。
# 它不是论文质量的标定；拒绝时报告当前值，供使用者依据目标仪器调整。
DEFAULT_MIN_CONCENTRATION = 300.0

_OUTCOME_CN = {
    "publication_frame_ready": tr("发表级图已扫完并保存。", "Publication-grade frame scanned and saved."),
    "tip_not_good_enough": (tr("针尖没到那个水平 —— **不进入那一小时**。"
                            "先回 MakeAtomicResolutionTip 修，修好了再来。",
                               "The tip is not good enough: **not entering that hour**. Go back to "
                               "MakeAtomicResolutionTip and repair it first, then come back.")),
    "tip_check_unavailable": (tr("验不了针尖（拿不到评估帧或判据没跑成）—— "
                              "**「验不了」不是「够好」**，所以照样不进入。",
                                 "Cannot verify the tip (no evaluation frame, or the criterion did "
                                 "not run): **'unverifiable' is not 'good enough'**, so it is not "
                                 "entered either.")),
    "scan_failed": tr("发表级扫描没能完成，见 reason。",
                      "The publication-grade scan could not be completed, see reason."),
    "incomplete": tr("流程未走完。", "The flow did not finish."),
}


class ScanPublicationFrame(_TipComposite):
    """针尖够好才进：调慢 → 精调平 → 加分辨率 → 移到起点静置 → 扫一张。"""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="ScanPublicationFrame",
            version="1.0.0",
            category=SkillCategory.COMPOSITE,
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("扫**一张**发表级的帧 —— 这跟「把针尖调到原子分辨」是两件事。"
                "**先**过针尖质量这道闸，不够就拒绝，因为这一次扫描本身要花掉"
                "大约一个小时，不能拿它去赌。然后：把扫描调慢，把 terrace 精确"
                "调平（AutoTilt），把像素数提上去，把针尖移到扫描的**起点**那个角"
                "并让它**静置**，好让 piezo 蠕变在第一行**之前**衰减掉、"
                "而不是被记录进第一行里，到这一步才开扫。顺序是要紧的：改帧、"
                "改速度、改像素都会让针尖动，所以静置必须排在最后。",
                   "Scan **one** publication-grade frame. This is a different thing from 'tuning the "
                   "tip to atomic resolution'. **First** pass the tip-quality gate and refuse if it "
                   "is not good enough, because this scan itself takes about an hour and must not be "
                   "a gamble. Then: slow the scan down, level the terrace precisely (AutoTilt), "
                   "raise the pixel count, move the tip to the **start** corner of the scan and let "
                   "it **rest**, so that piezo creep decays **before** the first line instead of "
                   "being recorded into it, and only then start scanning. The order matters: "
                   "changing the frame, the speed or the pixel count all move the tip, so the rest "
                   "must come last.")
            ),
            parameters=[
                ParameterSpec(
                    name="min_angular_concentration", type="float",
                    description=(
                        tr("入口闸：针尖的角向集中度低于它就**拒绝**，不进入那一小时。"
                        f"出厂 {DEFAULT_MIN_CONCENTRATION:g}。"
                        "这个数没标定过「多好才印得"
                        "出来」，只是「明显不是勉强算有」的一条线。拒绝时会把实测值报出来。",
                           "Entry gate: **refuse** if the tip's angular concentration is below this "
                           "value, and do not enter that hour. Factory default "
                           f"{DEFAULT_MIN_CONCENTRATION:g}. "
                           "This number "
                           "is not calibrated for 'how good is good enough to print', only a line "
                           "for 'clearly not barely passing'. On refusal the measured value is "
                           "reported.")),
                    required=False, default=DEFAULT_MIN_CONCENTRATION,
                    min_value=0.0, max_value=100000.0),
                ParameterSpec(
                    name="skip_tip_check", type="bool",
                    description=(tr("跳过入口闸。**只有在刚刚已经验过针尖**（比如上一步就是"
                                 "MakeAtomicResolutionTip 的确认帧）时才该用；"
                                 "否则这一小时就是在赌。",
                                    "Skip the entry gate. Use it **only when the tip was just "
                                    "verified** (for example the previous step was the confirmation "
                                    "frame of MakeAtomicResolutionTip); otherwise that hour is a "
                                    "gamble.")),
                    required=False, default=False),
                ParameterSpec(
                    name="frame_nm", type="float", unit="nm",
                    description=tr("发表帧视野。留空 = 沿用当前扫描框，不自作主张换视野。",
                                   "Publication frame field of view. Empty = keep the current scan "
                                   "frame, do not change the field of view on its own."),
                    required=False, min_value=0.2, max_value=1000.0),
                ParameterSpec(
                    name="pixels", type="int", unit="px",
                    description=tr("发表帧像素数（第 4 步「加分辨率」）。出厂 512。",
                                   "Pixel count of the publication frame (step 4, 'raise the "
                                   "resolution'). Factory 512."),
                    required=False, default=512, min_value=64, max_value=4096),
                ParameterSpec(
                    name="line_time_s", type="float", unit="s",
                    description=(tr("发表帧每线时间（第 2 步「调慢」）。出厂 3 s —— "
                                 "512 线约 51 分钟，即「那一小时」。",
                                    "Time per line of the publication frame (step 2, 'slow down'). "
                                    "Factory 3 s: 512 lines take about 51 minutes, which is exactly "
                                    "that hour.")),
                    required=False, default=3.0, min_value=0.05, max_value=120.0),
                ParameterSpec(
                    name="settle_s", type="float", unit="s",
                    description=(tr("在扫描起点静置多久（第 5 步）。出厂 120 s。"
                                 "**这是为了让蠕变衰减在扫描开始之前**，"
                                 "而不是被记录进前几行。",
                                    "How long to rest at the scan start (step 5). Factory 120 s. "
                                    "**This lets creep decay before the scan starts** instead of "
                                    "being recorded into the first lines.")),
                    required=False, default=120.0, min_value=0.0, max_value=3600.0),
                ParameterSpec(
                    name="level_first", type="bool",
                    description=tr("扫之前先精确调平（AutoTilt）。出厂 True。",
                                   "Level precisely (AutoTilt) before scanning. Factory True."),
                    required=False, default=True),
                ParameterSpec(
                    name="scan_timeout_s", type="float", unit="s",
                    description=tr("发表帧的等待上限。留空 = 按线时间×像素数的 1.5 倍算。",
                                   "Wait limit for the publication frame. Empty = 1.5 times line "
                                   "time x pixel count."),
                    required=False, min_value=60.0, max_value=86400.0),
            ],
            estimated_duration_s=3600.0,
            composition_level=2,
            tags=["scan", "publication", "atomic", "slow", "settle"],
        )

    # ──────────────────────────────────────────────────────────────────
    def plan_dynamic(self, params: dict,
                     executor: GraphExecutor) -> Iterator[CompositeStep]:
        phases: list[dict] = []
        outcome = "incomplete"

        pixels = int(params.get("pixels") or 512)
        line_t = float(params.get("line_time_s") or 3.0)
        settle_s = float(params.get("settle_s") if params.get("settle_s") is not None
                         else 120.0)
        min_conc = float(params.get("min_angular_concentration")
                         if params.get("min_angular_concentration") is not None
                         else DEFAULT_MIN_CONCENTRATION)
        timeout_s = float(params.get("scan_timeout_s")
                          or max(600.0, line_t * pixels * 1.5))

        # ── 0) 当前扫描框：不自作主张换视野 ──
        yield CompositeStep(
            step_id="P:frame", skill_name="GetScanFrame", params={},
            optional=False, checkpoint_after=False, tags=("setup",))
        fr = _data(executor, "P:frame")
        cx = fr.get("center_x_m")
        cy = fr.get("center_y_m")
        w = fr.get("width_m")
        if cx is None or cy is None or not w:
            self._phase_out = _wrap(
                [{"phase": "setup",
                  "reason": tr("读不到当前扫描框 —— 不去猜一个视野就开扫。",
                               "Cannot read the current scan frame: not starting a scan with a "
                               "guessed field of view.")}],
                "scan_failed")
            return
        want_nm = params.get("frame_nm")
        size_m = float(want_nm) * 1e-9 if want_nm else float(w)

        # ── 1) 入口闸：针尖真到那个水平了吗 ──────────────────────────
        # 「我会花 1h 来扫一张真正的完美图像，**但是前提是针尖真的已经达到那个
        #  水平了**。」—— 所以这里花几分钟去验，而不是拿一小时去赌。
        if not bool(params.get("skip_tip_check", False)):
            yield CompositeStep(
                step_id="P:check_scan", skill_name="ScanAt",
                params={"center_x_m": cx, "center_y_m": cy, "size_m": size_m,
                        "wait_timeout_s": 900.0},
                optional=False, checkpoint_after=False, tags=("gate",))
            yield CompositeStep(
                step_id="P:check_save", skill_name="SaveScan", params={},
                optional=True, checkpoint_after=False, tags=("gate",))
            yield CompositeStep(
                step_id="P:check_latest", skill_name="GetLatestScanFile", params={},
                optional=True, checkpoint_after=False, tags=("gate",))
            chk_path = _scan_path(executor, "P:check_save", "P:check_latest")
            if not chk_path:
                phases.append({"phase": "gate",
                               "reason": tr("验针尖那一帧拿不到文件路径 —— "
                                         "**「验不了」不是「够好」**，不进入那一小时。",
                                            "The tip-check frame has no file path: **'unverifiable' "
                                            "is not 'good enough'**, not entering that hour.")})
                self._phase_out = _wrap(phases, "tip_check_unavailable")
                return
            yield CompositeStep(
                step_id="P:check_assess", skill_name="AssessAtomicResolution",
                params={"scan_path": chk_path},
                optional=True, checkpoint_after=True, tags=("gate",))
            v = _data(executor, "P:check_assess")
            conc = v.get("angular_concentration")
            if not _ok(executor, "P:check_assess") or conc is None:
                phases.append({"phase": "gate", "scan_path": chk_path,
                               "reason": tr("针尖判据没跑成 —— **「验不了」不是「够好」**。",
                                            "The tip criterion did not run: **'unverifiable' is not "
                                            "'good enough'**.")})
                self._phase_out = _wrap(phases, "tip_check_unavailable")
                return
            conc = float(conc)
            phases.append({"phase": "gate", "scan_path": chk_path,
                           "angular_concentration": conc,
                           "passed": bool(v.get("passed")),
                           "min_required": min_conc})
            if not v.get("passed") or conc < min_conc:
                phases[-1]["reason"] = (
                    tr("针尖角向集中度 %.1f，要求 ≥ %.0f（判据 passed=%s）—— "
                    "**不进入那一小时**。先回 MakeAtomicResolutionTip 修；"
                    "确实想按这个针尖扫，就显式调低 min_angular_concentration，"
                    "那是一个决定，不该由这里替你做。",
                       "Tip angular concentration %.1f, required >= %.0f (criterion passed=%s): "
                       "**not entering that hour**. Go back to MakeAtomicResolutionTip and repair "
                       "it; if you really want to scan with this tip, explicitly lower "
                       "min_angular_concentration, which is a decision that must not be made for you "
                       "here.")
                    % (conc, min_conc, v.get("passed")))
                self._phase_out = _wrap(phases, "tip_not_good_enough")
                return
            phases[-1]["reason"] = (tr("针尖角向集中度 %.1f ≥ %.0f —— 值得花这一小时。",
                                       "Tip angular concentration %.1f >= %.0f: worth spending this "
                                       "hour.")
                                    % (conc, min_conc))

        # ── 2) 调慢 ────────────────────────────────────────────────
        speed = size_m / line_t
        yield CompositeStep(
            step_id="P:speed", skill_name="SetScanSpeed",
            params={"fwd_speed": speed, "bwd_speed": speed,
                    "fwd_line_time": line_t, "bwd_line_time": line_t,
                    "keep_const": 0, "speed_ratio": 1.0},
            optional=False, checkpoint_after=False, tags=("setup",))
        # 回读确认 —— ConfigureScan 会把速度改回档位表，这里必须看见它没被改掉
        yield CompositeStep(
            step_id="P:speed_back", skill_name="GetScanSpeed", params={},
            optional=True, checkpoint_after=False, tags=("setup",))
        got = _data(executor, "P:speed_back").get("fwd_time_s")

        # ── 3) 加分辨率 + 定框（**不许动速度**）────────────────────
        yield CompositeStep(
            step_id="P:buffer", skill_name="SetScanBuffer",
            params={"pixels": pixels, "lines": pixels},
            optional=True, checkpoint_after=False, tags=("setup",))
        yield CompositeStep(
            step_id="P:configure", skill_name="ConfigureScan",
            params={"center_x_m": cx, "center_y_m": cy,
                    "width_m": size_m, "height_m": size_m,
                    "set_scan_speed": False},
            optional=False, checkpoint_after=False, tags=("setup",))
        yield CompositeStep(
            step_id="P:speed_back2", skill_name="GetScanSpeed", params={},
            optional=True, checkpoint_after=False, tags=("setup",))
        got2 = _data(executor, "P:speed_back2").get("fwd_time_s")
        phases.append({"phase": "setup", "pixels": pixels,
                       "line_time_s_requested": line_t,
                       "line_time_s_after_speed_set": got,
                       "line_time_s_after_configure": got2,
                       "frame_m": size_m,
                       "reason": (tr("视野 %.3g nm / %d px / %.3g s 每线（约 %.0f 分钟一帧）。"
                                  "ConfigureScan 之后回读线时间 %s —— 它会把速度改回"
                                  "档位表，所以这里必须看一眼。",
                                     "Field of view %.3g nm / %d px / %.3g s per line (about %.0f "
                                     "minutes per frame). Line time read back after ConfigureScan: "
                                     "%s. It resets the speed back to the preset tier, so it must be "
                                     "checked here.")
                                  % (size_m * 1e9, pixels, line_t,
                                     line_t * pixels / 60.0, got2))})

        # ── 4) 精确调平 ────────────────────────────────────────────
        if bool(params.get("level_first", True)):
            yield CompositeStep(
                step_id="P:level", skill_name="AutoTilt", params={},
                optional=True, checkpoint_after=True, tags=("setup", "level"))
            phases.append({"phase": "level", "ok": _ok(executor, "P:level"),
                           "reason": (tr("已精确调平。", "Precisely levelled.") if _ok(executor, "P:level")
                                      else tr("调平没成 —— 继续扫，但图会带着倾斜，"
                                           "事后拉平会连原子起伏一起拉。",
                                              "Levelling failed: scanning on, but the image will "
                                              "carry a tilt, and flattening afterwards would flatten "
                                              "the atomic corrugation along with it."))})

        # ── 5) 移到起点 + 静置（**必须排在 2–4 之后**）─────────────
        # 改视野 / 改速度 / 改像素每一样都会动针尖；先静置再改设置等于白等。
        # MoveToXY 移的是针尖不是框，而 StartScan 会把针尖拉回框的起点 ——
        # 先把针尖挪到那个起点，就不会在起扫时再跳一次。
        start_x = cx - size_m / 2.0
        start_y = cy - size_m / 2.0
        yield CompositeStep(
            step_id="P:goto_start", skill_name="MoveToXY",
            params={"x_m": start_x, "y_m": start_y, "wait": True},
            optional=True, checkpoint_after=False, tags=("settle",))
        if settle_s > 0:
            self.abortable_sleep(getattr(executor, "_context", None), settle_s)
        phases.append({
            "phase": "settle", "start_xy": [start_x, start_y],
            "settle_s": settle_s,
            "reason": (tr("已把针尖移到扫描起点 (%.1f, %.1f) nm 并静置 %.0f s —— "
                       "让压电蠕变衰减在**扫描开始之前**，而不是记录进前几行。",
                          "Tip moved to the scan start (%.1f, %.1f) nm and rested for %.0f s, so "
                          "that piezo creep decays **before the scan starts** instead of being "
                          "recorded into the first lines.")
                       % (start_x * 1e9, start_y * 1e9, settle_s))})

        # ── 6) 扫一张 ──────────────────────────────────────────────
        yield CompositeStep(
            step_id="P:scan", skill_name="StartScan",
            params={"direction": "up"},
            optional=False, checkpoint_after=False, tags=("scan",))
        yield CompositeStep(
            step_id="P:wait", skill_name="WaitScanComplete",
            params={"timeout": timeout_s},
            optional=True, checkpoint_after=False, tags=("scan",))
        yield CompositeStep(
            step_id="P:save", skill_name="SaveScan", params={},
            optional=False, checkpoint_after=True, tags=("scan",))
        yield CompositeStep(
            step_id="P:latest", skill_name="GetLatestScanFile", params={},
            optional=True, checkpoint_after=False, tags=("scan",))
        path = _scan_path(executor, "P:save", "P:latest")
        if not path:
            phases.append({"phase": "scan",
                           "reason": tr("扫完了但拿不到文件路径（检查 Nanonis 自动保存）。",
                                        "Scan finished but no file path is available (check Nanonis "
                                        "auto-save).")})
            self._phase_out = _wrap(phases, "scan_failed")
            return

        # 顺带把这张图自己判一遍 —— 交出去的时候要带着它的成色，
        # 而不是让下一层再去扫一次才知道好不好。
        yield CompositeStep(
            step_id="P:assess", skill_name="AssessAtomicResolution",
            params={"scan_path": path},
            optional=True, checkpoint_after=True, tags=("scan", "assess"))
        av = _data(executor, "P:assess")
        phases.append({
            "phase": "scan", "scan_path": path,
            "angular_concentration": av.get("angular_concentration"),
            "passed": av.get("passed"),
            "period_fast_axis_nm": av.get("period_fast_axis_nm"),
            "reason": (tr("发表帧已保存：%s（角向集中度 %s）", "Publication frame saved: %s (angular concentration %s)")
                       % (path, av.get("angular_concentration")))})
        outcome = "publication_frame_ready"
        self._phase_out = _wrap(phases, outcome)
        self._phase_out["frame_path"] = path
        self._phase_out["angular_concentration"] = av.get("angular_concentration")
        executor.set_partial("outcome", outcome)

    def aggregate(self, sub_results: dict, progress) -> dict:
        data = dict(progress.partial_data)
        for key in ("phases", "outcome", "summary_cn", "frame_path",
                    "angular_concentration"):
            if key in getattr(self, "_phase_out", {}):
                data[key] = self._phase_out[key]
        return data

    _phase_out: dict[str, Any] = {}


def _wrap(phases: list[dict], outcome: str) -> dict:
    bits = [_OUTCOME_CN.get(outcome, outcome)]
    for p in phases:
        if p.get("reason"):
            bits.append(str(p["reason"]))
    return {"phases": phases, "outcome": outcome, "summary_cn": " ".join(bits)}


def make_tools(context_provider):
    from mast.agents._shared.skill_adapter import wrap_skill

    return [wrap_skill(ScanPublicationFrame, context_provider)]


__all__ = ["ScanPublicationFrame"]
