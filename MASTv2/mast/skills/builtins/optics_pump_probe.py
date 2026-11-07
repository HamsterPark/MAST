"""PumpProbeScan — the pump-probe delay scan (replaces ``Pump probe*.vi``).

One skill drives the whole measurement loop the lab previously ran in
LabVIEW: step the optical delay line, let the stage settle, read the
Nanonis signals (tunnel current and/or a lock-in demod channel, averaged
over a per-point sampling window), then write a CSV + preview PNG to
``artifacts/pump_probe/``.

Signal access is the same software-polling approach as
CaptureSignalBuffer: ``Current_Get`` for tunnel current and
``Signals_ValGet(index, 0)`` for the lock-in demod output. The demod
signal index can be given explicitly (``lockin_signal_index``) or
auto-discovered from ``Signals_NamesGet`` by matching the demodulator
number against names like "LI Demod 1 X (A)".

Motion goes through the instruments registry (delay-line binding in
``config/optical_instruments.json``); Nanonis goes through
``context.safe_call``. The two stacks never touch each other's transport.

Live view (contract §7): each point is also published to LiveHub as
``local/pump_probe/<run_id>`` — an ``xy`` stream (delay → signals) for a 1D
sweep, a ``map2d`` stream (``ix`` = delay index, ``iy`` = 2nd-stage index) for
the two-stage map. The CSV/PNG output is unchanged.
"""

from __future__ import annotations

import csv
import logging
import time
from datetime import datetime
from pathlib import Path

from mast._runtime_paths import project_root
from mast.core.types import (
    NanonisCallRecord,
    ParameterSpec,
    SafetyLevel,
    SkillCategory,
    SkillMetadata,
    SkillResult,
)
from mast.i18n import tr
from mast.instruments.base import InstrumentError, InstrumentUnavailable
from mast.instruments.registry import get_instrument_registry
from mast.skills.base import BaseSkill
# Per-point acquisition helpers: single source in optics_acquire, shared with
# OpticalStageScan / AcquireSignalPoint. Re-exported under the historical
# _-prefixed names the tests import.
from mast.skills.builtins.optics_acquire import (
    DEFAULT_TRIGGER_LINE,
    DEFAULT_TRIGGER_PORT,
    mean_std as _mean_std,
    nanonis_scalar as _nanonis_scalar,
    parse_indices as _parse_indices,
    signal_names as _signal_names,
    trigger_port_refusal,
    value_units,
)
from mast.skills.builtins.optics_common import (
    LiveStream,
    abort_probe,
    new_run_id,
    stage_tip_refusal,
)

logger = logging.getLogger(__name__)

__all__ = ["PumpProbeScan"]


class PumpProbeScan(BaseSkill):
    """Delay-line sweep with Nanonis signal acquisition at every point."""

    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="PumpProbeScan",
            version="1.0.0",
            category=SkillCategory.WRITE,
            # CONFIRM (not DANGEROUS): motion is bounded by the delay stage's
            # soft limits and the tip is untouched; one confirmation before a
            # potentially minutes-long sweep is the right friction.
            safety_level=SafetyLevel.CONFIRM,
            description=(
                tr("泵浦-探测延时扫描：把光学延时线从 delay_start_ps 扫到 delay_stop_ps，"
                "分 `points` 步；每一点先等台子稳定，再把隧道电流（Current_Get）和／或一路锁相解调信号（Signals_ValGet）"
                "在 samples_per_point 次读数上取平均。CSV + 预览 PNG 写到 artifacts/pump_probe/。"
                "需要 config/optical_instruments.json 里的 delay_line 绑定。",
                   "Pump-probe delay scan: sweep the optical delay line from delay_start_ps to delay_stop_ps in `points` steps; at each point wait for the stage to settle, then average the tunneling current (Current_Get) and/or one lock-in demodulated signal (Signals_ValGet) "
                   "over samples_per_point readings. CSV + preview PNG are written to artifacts/pump_probe/. "
                   "Requires a delay_line binding in config/optical_instruments.json.")
            ),
            parameters=[
                ParameterSpec(
                    name="delay_start_ps",
                    type="float",
                    description=tr("起始光学延时（ps）", "Start optical delay (ps)"),
                    unit="ps",
                    required=True,
                ),
                ParameterSpec(
                    name="delay_stop_ps",
                    type="float",
                    description=tr("终止光学延时（ps）", "Stop optical delay (ps)"),
                    unit="ps",
                    required=True,
                ),
                ParameterSpec(
                    name="points",
                    type="int",
                    description=tr("延时点数（含首尾两端）", "Number of delay points (both ends included)"),
                    required=True,
                    min_value=2,
                    max_value=10000,
                ),
                ParameterSpec(
                    name="samples_per_point",
                    type="int",
                    description=tr("每个延时点上平均多少次信号读数", "How many signal readings to average at each delay point"),
                    required=False,
                    default=10,
                    min_value=1,
                    max_value=1000,
                ),
                ParameterSpec(
                    name="sample_interval_s",
                    type="float",
                    description=tr("同一点内两次读数之间的间隔", "Interval between two readings at the same point"),
                    unit="s",
                    required=False,
                    default=0.01,
                    min_value=0.0,
                    max_value=1.0,
                ),
                ParameterSpec(
                    name="settle_extra_s",
                    type="float",
                    description=tr("台子报告已到位之后额外驻留的时间", "Extra dwell time after the stage reports it is in position"),
                    unit="s",
                    required=False,
                    default=0.05,
                    min_value=0.0,
                    max_value=10.0,
                ),
                ParameterSpec(
                    name="read_current",
                    type="bool",
                    description=tr("采集隧道电流（Current_Get）", "Acquire the tunneling current (Current_Get)"),
                    required=False,
                    default=True,
                ),
                ParameterSpec(
                    name="read_lockin",
                    type="bool",
                    description=tr("采集一路锁相解调信号", "Acquire one lock-in demodulated signal"),
                    required=False,
                    default=True,
                ),
                ParameterSpec(
                    name="lockin_signal_index",
                    type="int",
                    description=(
                        tr("锁相输出所在的信号槽位（0-127）；-1 = 经 lockin_demod 从信号名里自动发现",
                           "Signal slot of the lock-in output (0-127); -1 = discover automatically from the signal names via lockin_demod")
                    ),
                    required=False,
                    default=-1,
                    min_value=-1,
                    max_value=127,
                ),
                ParameterSpec(
                    name="lockin_demod",
                    type="int",
                    description=tr("用于自动发现的解调器编号", "Demodulator number used for auto-discovery"),
                    required=False,
                    default=1,
                    min_value=1,
                    max_value=8,
                ),
                ParameterSpec(
                    name="return_to_start",
                    type="bool",
                    description=(tr("扫完之后回到 delay_start_ps；被中止时不回（中止之后不再发起新的"
                                 "运动），结果里 return_to_start 写明",
                                    "Return to delay_start_ps after the scan; not done when aborted (no new "
                                    "motion is started after an abort); return_to_start in the result says which")),
                    required=False,
                    default=True,
                ),
                ParameterSpec(
                    name="move_timeout_s",
                    type="float",
                    description=tr("每点运动的时限", "Per-point motion time limit"),
                    unit="s",
                    required=False,
                    default=60.0,
                    min_value=0.1,
                    max_value=600.0,
                ),
                ParameterSpec(
                    name="tag",
                    type="str",
                    description=tr("可选标签，附加在输出文件名上", "Optional label appended to the output file name"),
                    required=False,
                    default="",
                ),
                ParameterSpec(
                    name="extra_signal_indices",
                    type="str",
                    description=(
                        tr("每点还要额外读取并平均的 Nanonis 信号槽位（0-127），逗号分隔（例如 '14' 代表 Z）"
                        "。每加一路就多出 sig<idx>_mean/std 两列"
                        "。",
                           "Extra Nanonis signal slots (0-127) to read and average at every point, comma-separated (e.g. '14' for Z). "
                           "Each one adds sig<idx>_mean/std columns"
                           ".")
                    ),
                    required=False,
                    default="",
                ),
                ParameterSpec(
                    name="trigger_enable",
                    type="bool",
                    description=(
                        tr("每一点上（稳定之后、读数之前）在一条 Nanonis 数字输出线上打一个脉冲，用来触发泵浦激光／光谱仪。",
                           "At every point (after settling, before the reading) send a pulse on one Nanonis digital output line to trigger the pump laser / spectrometer.")
                    ),
                    required=False,
                    default=False,
                ),
                ParameterSpec(
                    name="trigger_port",
                    type="int",
                    description=(tr("触发用的数字端口：1=B,2=C,3=D,4+=DIO。Port A（0）被拒绝："
                                 "PMD4 机器上它是粗动控制器的控制口",
                                    "Digital port for the trigger: 1=B, 2=C, 3=D, 4+=DIO. Port A (0) is refused: "
                                    "on PMD4 machines it is the control port of the coarse-motion controller")),
                    required=False, default=DEFAULT_TRIGGER_PORT, min_value=1, max_value=8,
                ),
                ParameterSpec(
                    name="trigger_line",
                    type="int",
                    description=(tr("要打脉冲的数字线，1..8",
                                    "Digital line to pulse, 1..8")),
                    required=False, default=DEFAULT_TRIGGER_LINE, min_value=1, max_value=8,
                ),
                ParameterSpec(
                    name="trigger_width_s",
                    type="float",
                    description=tr("触发脉冲宽度（s）", "Trigger pulse width (s)"),
                    unit="s", required=False, default=1e-4,
                    min_value=1e-6, max_value=1.0,
                ),
                ParameterSpec(
                    name="secondary_device_id",
                    type="str",
                    description=(
                        tr("可选的第二台台子，在扫描开始前一次性停到 secondary_position；"
                        "'' = 不用。",
                           "Optional second stage, parked once at secondary_position before the scan starts; "
                           "'' = not used.")
                    ),
                    required=False, default="",
                ),
                ParameterSpec(
                    name="secondary_axis",
                    type="str",
                    description=tr("第二台设备上的轴名", "Axis name on the second device"),
                    required=False, default="",
                ),
                ParameterSpec(
                    name="secondary_position",
                    type="float",
                    description=(tr("secondary_scan=false 时，把第二条轴**停**在哪里（用它的原生单位）",
                                    "Where to **park** the second axis when secondary_scan=false (in its native unit)")),
                    required=False, default=0.0,
                ),
                # -- 双台同时扫 (2D delay map): raster the 2nd stage too --------
                ParameterSpec(
                    name="secondary_scan",
                    type="bool",
                    description=(
                        tr("把第二台台子当作**第二条**延时轴来扫，而不是把它停住：从 secondary_start→secondary_stop 取 secondary_points 个位置，"
                        "每个位置上都把主延时线整条扫一遍 —— 得到一张 2D 图（主延时 × 第二台台子位置）"
                        "。false = 一次性停在 secondary_position（1D，默认）。",
                           "Scan the second stage as a **second** delay axis instead of parking it: take secondary_points positions from secondary_start to secondary_stop, "
                           "and at each position sweep the whole primary delay line - giving a 2D map (primary delay x second stage position)"
                           ". false = park once at secondary_position (1D, default).")
                    ),
                    required=False, default=False,
                ),
                ParameterSpec(
                    name="secondary_start",
                    type="float",
                    description=tr("第二台台子的扫描起点（其原生单位），secondary_scan 时有效",
                                   "Scan start of the second stage (native unit); used when secondary_scan"),
                    required=False, default=0.0,
                ),
                ParameterSpec(
                    name="secondary_stop",
                    type="float",
                    description=tr("第二台台子的扫描终点（其原生单位），secondary_scan 时有效",
                                   "Scan end of the second stage (native unit); used when secondary_scan"),
                    required=False, default=0.0,
                ),
                ParameterSpec(
                    name="secondary_points",
                    type="int",
                    description=tr("第二台台子的点数（外层／慢轴），secondary_scan 时有效",
                                   "Number of points of the second stage (outer / slow axis); used when secondary_scan"),
                    required=False, default=2, min_value=2, max_value=1000,
                ),
                ParameterSpec(
                    name="dry_run",
                    type="bool",
                    description=(
                        tr("**试跑**：让延时线走遍每一个点并稳定，但跳过触发、信号读取与存盘。用来在真正（会打激光的）"
                        "测量之前，验证台子能干净地扫完整个范围。会报告实际到达的位置。",
                           "**Dry run**: step the delay line through every point and let it settle, but skip the trigger, signal readout and saving. Use it to verify that the stage can sweep the whole range cleanly before the real "
                           "(laser-firing) measurement. Reports the positions actually reached.")
                    ),
                    required=False,
                    default=False,
                ),
            ],
            estimated_duration_s=120.0,
            composition_level=0,
            tags=["optics", "pump_probe", "delay_line", "acquisition", "write"],
        )

    # -- helpers ---------------------------------------------------------------

    def _resolve_lockin_index(self, context, index: int, demod: int) -> int:
        """Explicit index wins; otherwise match 'demod <n> … x' in the
        Signals_NamesGet channel names."""
        if index >= 0:
            return index
        record = context.safe_call("Signals_NamesGet")
        if record.error:
            raise InstrumentError(
                f"Signals_NamesGet failed while auto-discovering the lock-in "
                f"signal: {record.error}; pass lockin_signal_index explicitly"
            )
        names = _signal_names(record)
        needle = f"demod {demod}"
        for i, name in enumerate(names):
            low = " ".join(name.lower().split())
            if needle in low and (" x" in low.split(needle, 1)[1][:4]):
                return i
        raise InstrumentError(
            f"no signal name matches 'Demod {demod} … X' among "
            f"{len(names)} channels — pass lockin_signal_index explicitly "
            "(use ListSignalChannels to inspect)"
        )

    @staticmethod
    def _artifact_paths(tag: str) -> tuple[Path, Path]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_tag = "".join(c for c in tag if c.isalnum() or c in "-_")[:40]
        base = f"pump_probe_{stamp}" + (f"_{safe_tag}" if safe_tag else "")
        out_dir = project_root() / "artifacts" / "pump_probe"
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{base}.csv", out_dir / f"{base}.png"

    @staticmethod
    def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _write_plot(path: Path, rows: list[dict], series: list[tuple]) -> bool:
        """Plot one panel per (mean_col, std_col, label) series vs. delay."""
        if not series:
            return False
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except Exception as exc:  # noqa: BLE001 - plot is best-effort
            logger.warning("pump-probe plot skipped (matplotlib): %s", exc)
            return False
        delays = [r["delay_ps"] for r in rows]
        fig, axes = plt.subplots(
            len(series), 1, sharex=True, figsize=(7, 3 * len(series)), squeeze=False
        )
        for row_i, (mean_col, std_col, label) in enumerate(series):
            ax = axes[row_i][0]
            ax.errorbar(
                delays,
                [r.get(mean_col) for r in rows],
                yerr=[r.get(std_col) for r in rows] if std_col else None,
                fmt=".-", lw=0.8, ms=3, capsize=2,
            )
            ax.set_ylabel(label)
            ax.grid(alpha=0.3)
        axes[-1][0].set_xlabel("Optical delay (ps)")
        fig.suptitle("Pump-probe delay scan")
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return True

    @staticmethod
    def _write_heatmap(path: Path, rows: list[dict], value_col: str, label: str,
                       p1: int, p2: int, start: float, stop: float,
                       sec_positions: list, ax2) -> bool:
        """2D map of value_col over (main delay = columns, 2nd-stage pos = rows)."""
        if not value_col:
            return False
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except Exception as exc:  # noqa: BLE001 - plot is best-effort
            logger.warning("pump-probe heatmap skipped (matplotlib): %s", exc)
            return False
        grid = [[float("nan")] * p1 for _ in range(p2)]
        for r in rows:
            rr, cc = int(r.get("row", 0)), int(r.get("col", 0))
            if 0 <= rr < p2 and 0 <= cc < p1:
                v = r.get(value_col)
                grid[rr][cc] = float(v) if isinstance(v, (int, float)) else float("nan")
        svals = [s for s in sec_positions if s is not None]
        y0, y1 = (svals[0], svals[-1]) if svals else (0.0, 1.0)
        try:
            unit = ax2.config.unit if ax2 is not None else ""
        except Exception:  # noqa: BLE001
            unit = ""
        fig, ax = plt.subplots(figsize=(6.8, 5.2))
        im = ax.imshow(grid, origin="lower", aspect="auto",
                       extent=(start, stop, y0, y1), interpolation="nearest")
        ax.set_xlabel("Optical delay (ps)")
        ax.set_ylabel(f"2nd stage ({unit})" if unit else "2nd stage")
        ax.set_title(f"Pump-probe 2D delay map · {label}")
        fig.colorbar(im, ax=ax, label=label)
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return True

    # -- main ---------------------------------------------------------------

    def execute(self, context, params: dict) -> SkillResult:
        dry_run = bool(params.get("dry_run", False))
        read_current = params.get("read_current", True)
        read_lockin = params.get("read_lockin", True)
        extra_indices = _parse_indices(params.get("extra_signal_indices", ""))
        if not dry_run and not read_current and not read_lockin and not extra_indices:
            return SkillResult(
                skill_name="PumpProbeScan",
                success=False,
                error=("nothing to acquire: enable read_current / read_lockin, add "
                       "extra_signal_indices, or set dry_run=true to test motion only"),
            )

        # -- delay line + range pre-flight --------------------------------
        try:
            dl = get_instrument_registry().delay_line()
            lo, hi = dl.delay_range_ps
        except (InstrumentError, InstrumentUnavailable) as exc:
            return SkillResult(
                skill_name="PumpProbeScan",
                success=False,
                error=f"delay line unavailable: {exc}",
            )
        start = float(params["delay_start_ps"])
        stop = float(params["delay_stop_ps"])
        points = int(params["points"])
        for label, val in (("delay_start_ps", start), ("delay_stop_ps", stop)):
            if not (lo <= val <= hi):
                return SkillResult(
                    skill_name="PumpProbeScan",
                    success=False,
                    error=(
                        f"{label}={val} ps outside reachable range "
                        f"[{lo:.2f}, {hi:.2f}] ps"
                    ),
                )
        delays = [
            start + (stop - start) * i / (points - 1) for i in range(points)
        ]

        # -- lock-in channel resolution ------------------------------------
        nanonis_calls: list[NanonisCallRecord] = []
        lockin_index: int | None = None
        if read_lockin and not dry_run:
            try:
                lockin_index = self._resolve_lockin_index(
                    context,
                    int(params.get("lockin_signal_index", -1)),
                    int(params.get("lockin_demod", 1)),
                )
            except (InstrumentError, ValueError) as exc:
                return SkillResult(
                    skill_name="PumpProbeScan", success=False, error=str(exc)
                )

        samples = int(params.get("samples_per_point", 10))
        interval = float(params.get("sample_interval_s", 0.01))
        settle_extra = float(params.get("settle_extra_s", 0.05))
        move_timeout = float(params.get("move_timeout_s", 60.0))

        # -- optional 2nd stage --------------------------------
        # secondary_scan=false → park it once here (legacy 1D behaviour).
        # secondary_scan=true  → raster it as a SECOND delay axis: the sweep
        #   below moves it to each secondary position and sweeps the whole main
        #   delay line there, producing a 2D map (main delay × 2nd-stage pos).
        sec_id = str(params.get("secondary_device_id", "") or "")
        sec_axis = str(params.get("secondary_axis", "") or "")
        # the moved devices' tip_requirement (absent = none = no check), before the
        # secondary stage is parked below; dry runs move the stages just the same
        why, tip_info = stage_tip_refusal(
            context, [dl.config.device_id, sec_id if sec_axis else ""])
        if why:
            return SkillResult(skill_name="PumpProbeScan", success=False, error=why,
                               data={"tip_requirement": tip_info})
        two_d = bool(sec_id and sec_axis and params.get("secondary_scan", False))
        ax2 = None
        sec_positions: list = [None]
        if sec_id and sec_axis:
            try:
                ax2 = get_instrument_registry().axis(sec_id, sec_axis)
            except (InstrumentError, InstrumentUnavailable) as exc:
                return SkillResult(
                    skill_name="PumpProbeScan", success=False,
                    error=f"secondary stage {sec_id}/{sec_axis} unavailable: {exc}",
                )
            if two_d:
                s_pts = int(params.get("secondary_points", 2))
                s0 = float(params.get("secondary_start", 0.0))
                s1 = float(params.get("secondary_stop", 0.0))
                sec_positions = (
                    [s0 + (s1 - s0) * j / (s_pts - 1) for j in range(s_pts)]
                    if s_pts > 1 else [s0]
                )
            else:
                # legacy: park once at secondary_position before the sweep.
                try:
                    ax2.move_abs(float(params.get("secondary_position", 0.0)),
                                 wait=True, timeout=move_timeout)
                except (InstrumentError, InstrumentUnavailable) as exc:
                    return SkillResult(
                        skill_name="PumpProbeScan", success=False,
                        error=f"secondary stage {sec_id}/{sec_axis} park failed: {exc}",
                    )

        # -- trigger line: configure as active-high output once ------------
        trigger_enable = bool(params.get("trigger_enable", False)) and not dry_run
        trig_port = int(params.get("trigger_port", DEFAULT_TRIGGER_PORT))
        trig_line = int(params.get("trigger_line", DEFAULT_TRIGGER_LINE))
        trig_width = float(params.get("trigger_width_s", 1e-4))
        if trigger_enable:
            refusal = trigger_port_refusal(trig_port)
            if refusal:
                return SkillResult(skill_name="PumpProbeScan", success=False, error=refusal)
            rec = context.safe_call("DigLines_PropsSet", trig_line, trig_port, 1, 1)
            nanonis_calls.append(rec)
            if rec.error:
                return SkillResult(
                    skill_name="PumpProbeScan", success=False,
                    error=(f"trigger line {trig_port}/{trig_line} config failed: "
                           f"{rec.error}"),
                    nanonis_calls=nanonis_calls,
                )

        # -- sweep ------------------------------------------------------------
        # Outer loop = 2nd-stage positions ([None] in 1D mode); inner loop = the
        # main delay line. In 2D mode each row also carries row/col + sec_position
        # so the heat-map + CSV expose both axes. check_abort() only RETURNS a
        # bool (never raises) — honour it via stop_sweep to unwind both loops,
        # keeping partial data (dead-check fixed 2026-07-11).
        live: LiveStream | None = None
        if not dry_run:
            run_id = new_run_id("pp")
            channels = ((["current_a"] if read_current else [])
                        + (["lockin_v"] if read_lockin else [])
                        + [f"sig{xi}_mean" for xi in extra_indices])
            if two_d:
                s_unit = ax2.config.unit if ax2 is not None else ""
                live = LiveStream(
                    f"local/pump_probe/{run_id}", "map2d",
                    title=tr("泵浦-探测 双台二维图", "Pump-probe two-stage 2D map"), source="PumpProbeScan",
                    x_label="Delay", x_unit="ps", y_label="2nd stage", y_unit=s_unit,
                    # LiveHub refuses a zero pixel pitch; a degenerate axis gets 1.0.
                    meta={"nx": points, "ny": len(sec_positions), "x0": start,
                          "y0": sec_positions[0],
                          "dx": ((stop - start) / (points - 1) if points > 1 else 0.0) or 1.0,
                          "dy": (((sec_positions[-1] - sec_positions[0]) / (len(sec_positions) - 1)
                                  if len(sec_positions) > 1 else 0.0) or 1.0),
                          "x_unit": "ps", "y_unit": s_unit,
                          "channels": channels, "units": value_units(channels)})
            else:
                live = LiveStream(
                    f"local/pump_probe/{run_id}", "xy", title=tr("泵浦-探测 延时扫描", "Pump-probe delay scan"),
                    source="PumpProbeScan", x_label="Delay", x_unit="ps", y_label="Signal",
                    meta={"channels": channels, "units": value_units(channels)})

        rows: list[dict] = []
        aborted = ""
        idx = 0
        stop_sweep = False
        probe = abort_probe(context)
        t0 = time.monotonic()
        try:
            for si, sec_pos in enumerate(sec_positions):
                if stop_sweep:
                    break
                if two_d and sec_pos is not None:
                    ax2.move_abs(sec_pos, wait=True, timeout=move_timeout, check_abort=probe)
                    if settle_extra > 0:
                        time.sleep(settle_extra)
                for i, delay in enumerate(delays):
                    check_abort = getattr(context, "check_abort", None)
                    if callable(check_abort) and check_abort():
                        aborted = (f"aborted by operator after {len(rows)} of "
                                   f"{len(delays) * len(sec_positions)} points")
                        logger.warning("PumpProbeScan: %s", aborted)
                        stop_sweep = True
                        break

                    status = dl.move_to_delay_ps(delay, wait=True, timeout=move_timeout,
                                                 check_abort=probe)
                    if settle_extra > 0:
                        time.sleep(settle_extra)

                    row: dict = {
                        "index": idx,
                        "delay_ps": dl.position_to_delay(status.position),
                        "stage_position": status.position,
                    }
                    if two_d:
                        row["sec_position"] = sec_pos
                        row["row"] = si
                        row["col"] = i

                    # dry run stops here: motion + settle verified, nothing acquired.
                    if not dry_run:
                        if trigger_enable:
                            rec = context.safe_call(
                                "DigLines_Pulse", trig_port, [trig_line], trig_width,
                                0.0, 1, 1,
                            )
                            nanonis_calls.append(rec)
                            if rec.error:
                                raise InstrumentError(f"trigger pulse failed: {rec.error}")

                        cur_vals: list[float] = []
                        li_vals: list[float] = []
                        extra_vals: dict[int, list[float]] = {x: [] for x in extra_indices}
                        for k in range(samples):
                            if k and interval > 0:
                                time.sleep(interval)
                            if read_current:
                                rec = context.safe_call("Current_Get")
                                nanonis_calls.append(rec)
                                if rec.error:
                                    raise InstrumentError(f"Current_Get failed: {rec.error}")
                                cur_vals.append(_nanonis_scalar(rec))
                            if read_lockin:
                                rec = context.safe_call("Signals_ValGet", lockin_index, 0)
                                nanonis_calls.append(rec)
                                if rec.error:
                                    raise InstrumentError(
                                        f"Signals_ValGet({lockin_index}) failed: {rec.error}"
                                    )
                                li_vals.append(_nanonis_scalar(rec))
                            for xi in extra_indices:
                                rec = context.safe_call("Signals_ValGet", xi, 0)
                                nanonis_calls.append(rec)
                                if rec.error:
                                    raise InstrumentError(
                                        f"Signals_ValGet({xi}) failed: {rec.error}"
                                    )
                                extra_vals[xi].append(_nanonis_scalar(rec))

                        if read_current:
                            row["current_a"], row["current_std"] = _mean_std(cur_vals)
                        if read_lockin:
                            row["lockin_v"], row["lockin_std"] = _mean_std(li_vals)
                        for xi in extra_indices:
                            row[f"sig{xi}_mean"], row[f"sig{xi}_std"] = _mean_std(
                                extra_vals[xi]
                            )
                        if live is not None:
                            vals = {k: float(v) for k, v in row.items()
                                    if k in ("current_a", "lockin_v")
                                    or (k.startswith("sig") and k.endswith("_mean"))}
                            live.publish({"ix": i, "iy": si, "values": vals} if two_d
                                         else {"x": row["delay_ps"], "values": vals})
                    rows.append(row)
                    idx += 1
        except Exception as exc:  # noqa: BLE001 - keep partial data on any abort
            aborted = f"{type(exc).__name__}: {exc}"
            logger.warning("pump-probe sweep interrupted at point %d: %s",
                           len(rows), aborted)
        finally:
            if live is not None:
                live.end()
            # No new motion once the operator has aborted (contract §10.3): the
            # delay line stays where the stop left it, and the result says so.
            parked = "not requested"
            if params.get("return_to_start", True):
                if probe is not None and probe():
                    parked = "skipped: aborted"
                else:
                    try:
                        dl.move_to_delay_ps(start, wait=False)
                        if two_d and sec_positions and sec_positions[0] is not None:
                            ax2.move_abs(sec_positions[0], wait=False)
                        parked = "sent"
                    except Exception as exc:  # noqa: BLE001 - best-effort park
                        parked = f"failed: {type(exc).__name__}: {exc}"

        if not rows:
            return SkillResult(
                skill_name="PumpProbeScan",
                success=False,
                error=aborted or "sweep produced no data",
                nanonis_calls=nanonis_calls,
            )

        elapsed = time.monotonic() - t0
        total = points * len(sec_positions)

        # -- dry run: motion verified, nothing recorded --------------------
        if dry_run:
            data = {
                "dry_run": True,
                "n_points": len(rows),
                "requested_points": total,
                "delay_start_ps": start,
                "delay_stop_ps": stop,
                "elapsed_s": round(elapsed, 3),
                "reached_delays_ps": [round(r["delay_ps"], 4) for r in rows],
                "stage_positions": [r["stage_position"] for r in rows],
                "return_to_start": parked,
            }
            if two_d:
                data["two_d"] = True
                data["shape"] = [len(sec_positions), points]
                data["secondary_positions"] = list(sec_positions)
            if aborted:
                data["interrupted_after_points"] = len(rows)
            shape_txt = f"2D {len(sec_positions)}×{points}" if two_d else f"{points}"
            summary = (
                f"[DRY RUN] delay sweep stepped {len(rows)}/{total} points "
                f"({shape_txt}; {start:g}→{stop:g} ps) in {elapsed:.1f}s — motion OK, "
                f"no data saved"
            )
            if aborted:
                summary += f" [INTERRUPTED: {aborted}]"
            return SkillResult(
                skill_name="PumpProbeScan", success=not aborted, data=data,
                error=aborted, nanonis_calls=nanonis_calls, summary=summary,
            )

        # -- artifacts ----------------------------------------------------------
        fieldnames = list(rows[0].keys())
        csv_path, png_path = self._artifact_paths(str(params.get("tag", "")))
        self._write_csv(csv_path, rows, fieldnames)
        # In 2D mode draw a heat-map of the primary signal over both delay axes;
        # in 1D mode keep the per-signal line panels.
        if two_d:
            if read_current:
                vcol, vlabel = "current_a", "Current (A)"
            elif read_lockin:
                vcol, vlabel = "lockin_v", "Lock-in"
            elif extra_indices:
                vcol, vlabel = f"sig{extra_indices[0]}_mean", f"Signal {extra_indices[0]}"
            else:
                vcol, vlabel = "", ""
            plotted = self._write_heatmap(
                png_path, rows, vcol, vlabel, points, len(sec_positions),
                start, stop, sec_positions, ax2,
            )
        else:
            series: list[tuple] = []
            if read_current:
                series.append(("current_a", "current_std", "Current (A)"))
            if read_lockin:
                series.append(("lockin_v", "lockin_std", "Lock-in"))
            for xi in extra_indices:
                series.append((f"sig{xi}_mean", f"sig{xi}_std", f"Signal {xi}"))
            plotted = self._write_plot(png_path, rows, series)

        data: dict = {
            "path": str(csv_path),          # picked up as scan artifact
            "plot_path": str(png_path) if plotted else "",
            "n_points": len(rows),
            "requested_points": total,
            "delay_start_ps": start,
            "delay_stop_ps": stop,
            "samples_per_point": samples,
            "elapsed_s": round(elapsed, 3),
            "return_to_start": parked,
        }
        if live is not None and live.active:
            data["live_stream"] = live.stream_id
        if two_d:
            data["two_d"] = True
            data["shape"] = [len(sec_positions), points]
            data["secondary_device"] = f"{sec_id}/{sec_axis}"
            data["secondary_range"] = [float(params.get("secondary_start", 0.0)),
                                       float(params.get("secondary_stop", 0.0))]
        if read_lockin:
            data["lockin_signal_index"] = lockin_index
        if extra_indices:
            data["extra_signal_indices"] = extra_indices
        if trigger_enable:
            data["triggered_line"] = {"port": trig_port, "line": trig_line}
        if read_current:
            cs = [r["current_a"] for r in rows]
            data["current_min_a"], data["current_max_a"] = min(cs), max(cs)
        if read_lockin:
            ls = [r["lockin_v"] for r in rows]
            data["lockin_min"], data["lockin_max"] = min(ls), max(ls)
        if aborted:
            data["interrupted_after_points"] = len(rows)

        shape_txt = f"2D {len(sec_positions)}×{points}" if two_d else f"{points}"
        summary = (
            f"pump-probe scan: {len(rows)}/{total} points "
            f"({shape_txt}; {start:g}→{stop:g} ps) in {elapsed:.1f}s → {csv_path.name}"
        )
        if aborted:
            summary += f" [INTERRUPTED: {aborted}]"

        return SkillResult(
            skill_name="PumpProbeScan",
            success=not aborted,
            data=data,
            error=aborted,
            nanonis_calls=nanonis_calls,
            summary=summary,
        )
