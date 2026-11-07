"""Operator-tunable knobs for the current monitor — a live-read holder.

Same shape as :mod:`mast.vision.classical_thresholds`: a process-level snapshot
that the daemon reads on every segment, written at startup hydration and on each
``POST /api/settings``. A change takes effect on the very next segment; nothing
reloads, nothing restarts. The monitoring layer never imports settings — the
wiring is one-way.

These are NOT in ``MASTConfig``. Everything here is meant to be retuned while
the instrument runs, and a config section would both require a restart and
create a second source of truth for the same numbers.

The shipped defaults are deliberately conservative on the CRITICAL side: the
three rules that can halt a running composite skill (saturation, frozen readout,
giant spike) all need consecutive confirmation and a cool-down, because an alert
that stops a ten-minute scan has to be right. The WARN rules are advisory and
can afford to be chattier.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, fields, replace

from mast.i18n import tr

#: Keys surfaced in the 设置 UI. The frontend renders these dynamically from
#: :func:`knob_catalog`, so adding a knob here is the only step needed.
EDITABLE_KEYS: tuple[str, ...] = (
    "cm_enabled",
    "cm_alerts_enabled",
    "cm_segment_s",
    "cm_target_fs_hz",
    "cm_use_osci2t",
    "cm_2t_window_s",
    "cm_rms_warn_a",
    "cm_rms_ratio_warn",
    "cm_spike_sigma_warn",
    "cm_spike_sigma_crit",
    "cm_sat_current_a",
    "cm_sat_frac_crit",
    "cm_crit_consecutive",
    "cm_afterglow_s",
    "cm_alert_cooldown_s",
    "cm_rtn_score_warn",
    "cm_line_ratio_warn",
    "cm_jump_rate_warn_hz",
    "cm_keep_hours",
    "cm_keep_gb",
    "cm_pin_preroll_s",
    "cm_pin_postroll_s",
    "cm_evidence_min_interval_s",
    # Z 与振幅阈值按各自量纲配置，与电流阈值独立。
    "cm_aux_enabled",
    "cm_aux_alerts_enabled",
    "cm_aux_interval_s",
    "cm_aux_window_s",
    "cm_aux_keep_hours",
    "cm_z_drift_warn_m_per_s",
    "cm_z_step_warn_m",
    "cm_z_headroom_warn_frac",
    "cm_z_jump_k",
    "cm_amp_zero_frac",
    "cm_amp_blank_s",
)

#: 辅助通道那一组，供 UI 分栏与标定报告点名。EDITABLE_KEYS 仍是唯一的展示清单。
AUX_KEYS: tuple[str, ...] = (
    "cm_aux_enabled", "cm_aux_alerts_enabled", "cm_aux_interval_s",
    "cm_aux_window_s", "cm_aux_keep_hours",
    "cm_z_drift_warn_m_per_s", "cm_z_step_warn_m", "cm_z_headroom_warn_frac",
    "cm_z_jump_k", "cm_amp_zero_frac", "cm_amp_blank_s",
)

FIELD_BOUNDS: dict[str, tuple[float, float]] = {
    "cm_enabled": (0.0, 1.0),
    "cm_alerts_enabled": (0.0, 1.0),
    "cm_segment_s": (0.2, 10.0),
    "cm_target_fs_hz": (0.0, 1e6),
    "cm_use_osci2t": (0.0, 1.0),
    # 时窗必须落在目标仪器可提供的档位范围内；取档位时以实时回包为准。
    "cm_2t_window_s": (0.128, 60.0),
    "cm_rms_warn_a": (1e-13, 1e-6),
    # 下界留出基线自身散布的余量；上界支持只关注严重偏离的用法。
    "cm_rms_ratio_warn": (1.5, 100.0),
    "cm_spike_sigma_warn": (3.0, 100.0),
    "cm_spike_sigma_crit": (5.0, 1000.0),
    # Lower bound is the smallest plausible preamp full-scale, NOT the smallest
    # measurable current. At 1 pA every ordinary tunnelling current counts as
    # railed, and three seconds later that is a CRITICAL halting a running
    # composite skill — a denial-of-service lever reachable through a settings
    # write that deliberately carries no PIN (the PIN guards handing dangerous
    # capabilities to agents, not retuning a read-only monitor).
    "cm_sat_current_a": (1e-9, 1e-3),
    "cm_sat_frac_crit": (0.01, 1.0),
    "cm_crit_consecutive": (1.0, 20.0),
    "cm_afterglow_s": (0.0, 120.0),
    "cm_alert_cooldown_s": (10.0, 3600.0),
    "cm_rtn_score_warn": (0.1, 1.0),
    "cm_line_ratio_warn": (2.0, 1000.0),
    "cm_jump_rate_warn_hz": (0.1, 1000.0),
    "cm_keep_hours": (1.0, 720.0),
    "cm_keep_gb": (0.1, 500.0),
    "cm_pin_preroll_s": (0.0, 600.0),
    "cm_pin_postroll_s": (0.0, 600.0),
    "cm_evidence_min_interval_s": (10.0, 3600.0),
    # ── 辅助通道 ───────────────────────────────────────────────────────
    "cm_aux_enabled": (0.0, 1.0),
    "cm_aux_alerts_enabled": (0.0, 1.0),
    # 采样间隔下界与 aux_channels._MIN_INTERVAL_S 同步。采样机会来自泵的空闲预算，配置间隔只限制频率，不保证实际吞吐量。
    "cm_aux_interval_s": (0.1, 60.0),
    # 窗口至少 30 s，否则 12 点的最小样本数都凑不满；上界 1 h。
    "cm_aux_window_s": (30.0, 3600.0),
    "cm_aux_keep_hours": (1.0, 8760.0),
    # 1 fm/s ～ 1 µm/s。下界不设成 0：0 会让每一段都触发。
    "cm_z_drift_warn_m_per_s": (1e-15, 1e-6),
    # 1 pm ～ 10 µm。下界是压电 DAC 的量化台阶量级。
    "cm_z_step_warn_m": (1e-12, 1e-5),
    "cm_z_headroom_warn_frac": (0.0, 0.5),
    "cm_z_jump_k": (3.0, 100.0),
    # 上界 0.8：再高就不叫「归零」了，而这条判据的全部价值就在于它判的是一个
    # 二值事件（≈本底 / →0）。下界 0.01 留给本底极干净的机器。
    "cm_amp_zero_frac": (0.01, 0.8),
    # 下界 0：允许用户把脉冲屏蔽整个关掉（他知道自己没在打脉冲）。
    "cm_amp_blank_s": (0.0, 600.0),
}

#: Chinese label + one-line hint per knob, for the settings UI.
KNOB_LABELS: dict[str, tuple[str, str]] = {
    "cm_enabled": (tr("常开采集",
               "Always-on acquisition"),
           tr("1=连上 Nanonis 就自动开始分段采集,0=完全关闭",
               "1 = start segmented acquisition automatically once Nanonis is connected, 0 = fully off")),
    "cm_alerts_enabled": (tr("启用告警",
               "Enable alerts"),
           tr("0 时仍采集记录,但不发任何告警",
               "When 0, data is still recorded but no alerts are sent")),
    "cm_segment_s": (tr("段长(秒)",
               "Segment length (s)"),
           tr("每段的目标时长;越长特征越稳,延迟越大",
               "Target duration of each segment; longer gives steadier features but more latency")),
    "cm_target_fs_hz": (tr("目标采样率(Hz)",
               "Target sample rate (Hz)"),
           tr("0=用示波器最快时基",
               "0 = use the fastest scope timebase")),
    "cm_use_osci2t": (tr("用双通道示波器(Osci2T)",
               "Use the dual-channel scope (Osci2T)"),
           tr("1=用 Osci2T 采集(出厂)。**采样率完全一样**(2 kHz,由 Signals Period 决定),阈值不用重标;换来的是一次 TCP 往返拿回 50 倍的数据,data 角色的占用直线下降。模块没加载会自动退回 Osci1T,状态里会说。0=强制 Osci1T",
               "1 = acquire with Osci2T (factory). **The sample rate is exactly the same** (2 kHz, set by Signals Period), so thresholds need no recalibration; what you get is 50x the data per TCP round trip and much lower data-role occupancy. If the module is not loaded it falls back to Osci1T automatically and the status says so. 0 = force Osci1T")),
    "cm_2t_window_s": (tr("Osci2T 一屏时长(秒)",
               "Osci2T screen length (s)"),
           tr("取不超过此值的最长一档。出厂 6.4 s = 12800 点。⚠️ 它同时是**告警延迟**:一屏采满才取得回来,所以 6.4 s 意味着最坏情况下过 6.4 s 才知道刚才发生了什么(从前是 0.128 s)。要延迟回到从前的量级就设 1.28,往返仍然少 10 倍",
               "Picks the longest step not exceeding this value. Factory 6.4 s = 12800 points. ⚠️ It is also the **alert latency**: a screen can only be fetched once full, so 6.4 s means in the worst case you learn what just happened 6.4 s later (it used to be 0.128 s). Set 1.28 to bring latency back to the old order of magnitude; round trips are still 10x fewer")),
    "cm_rms_warn_a": (tr("噪声 RMS 警告阈(A)",
               "Noise RMS warning threshold (A)"),
           tr("去趋势后的交流噪声超过此值提示。**只在没有活跃基线时**生效——有基线时判的是「相对基线的倍数」，见 cm_rms_ratio_warn",
               "Warn when the detrended AC noise exceeds this value. **Only in effect when there is no active baseline**; with a baseline the rule judges the \"multiple of the baseline\", see cm_rms_ratio_warn")),
    "cm_rms_ratio_warn": (tr("噪声相对基线倍数阈",
               "Noise-to-baseline ratio threshold"),
           tr("当前噪声 RMS 是基线在同一电流下预期值的多少倍。有活跃基线且不在扫描时，rms_high 用这一条而不是绝对值——因为噪声本来就随电流变（20 pA 到 1 nA 之间可差一个数量级以上），一个固定的 pA 数不可能在整个电流范围上都对",
               "How many times the baseline's expected value at the same current the present noise RMS is. With an active baseline and no scan running, rms_high uses this instead of the absolute value, because noise varies with current anyway (20 pA to 1 nA can differ by more than an order of magnitude), so no fixed pA number can be right over the whole current range")),
    "cm_spike_sigma_warn": (tr("尖峰警告(σ)",
               "Spike warning (sigma)"),
           tr("最大偏离超过背景噪声的多少倍",
               "How many times the background noise the largest deviation exceeds")),
    "cm_spike_sigma_crit": (tr("尖峰严重(σ)",
               "Spike critical (sigma)"),
           tr("达到此倍数且步幅够大才可能升级为严重",
               "Only at this multiple and with a large enough step can it escalate to critical")),
    "cm_sat_current_a": (tr("饱和电流(A)",
               "Saturation current (A)"),
           tr("前置放大器满量程;超过即视为贴轨",
               "Full scale of the preamplifier; above it the signal counts as railed")),
    "cm_sat_frac_crit": (tr("饱和占比阈",
               "Saturation fraction threshold"),
           tr("段内贴轨样本占比超过此值算饱和",
               "Saturation if the share of railed samples in a segment exceeds this")),
    "cm_crit_consecutive": (tr("严重告警确认段数",
               "Critical confirmation segments"),
           tr("连续多少段同类才升级为严重(防误报)",
               "How many consecutive segments of the same kind before escalating to critical (false-alarm guard)")),
    "cm_afterglow_s": (tr("技能余波期(秒)",
               "Skill afterglow (s)"),
           tr("进针/退针/脉冲等技能结束后,电流还会抖这么久;这段时间内仍按抑制处理。设 0 = 关闭余波期(回到只看当下令牌)。**默认 5 s 是占位值,不是实测**——用监控历史标定:取每次进针/退针完成后最后一个含尖峰段的 (t_尖峰 − t_完成) 的 95 分位。",
               "After approach / withdraw / pulse skills end, the current keeps jittering for this long; it is still treated as suppressed meanwhile. 0 = no afterglow (only the current token counts). **The default 5 s is a placeholder, not a measurement**: calibrate from monitor history by taking the 95th percentile of (t_spike - t_done) of the last spike-containing segment after each approach/withdraw.")),
    "cm_alert_cooldown_s": (tr("告警冷却(秒)",
               "Alert cooldown (s)"),
           tr("同一规则在此时间内不重复告警",
               "The same rule does not alert again within this time")),
    "cm_rtn_score_warn": (tr("双稳态跳变阈",
               "Bistable jump threshold"),
           tr("RTN 判分超过此值提示针尖不稳",
               "An RTN score above this indicates an unstable tip")),
    "cm_line_ratio_warn": (tr("工频污染比阈",
               "Mains contamination ratio threshold"),
           tr("50 Hz 峰值相对邻频的倍数",
               "Ratio of the 50 Hz peak to neighbouring frequencies")),
    "cm_jump_rate_warn_hz": (tr("跳变率阈(Hz)",
               "Jump rate threshold (Hz)"),
           tr("每秒突跳次数超过此值提示",
               "Warn when jumps per second exceed this")),
    "cm_keep_hours": (tr("原始波形保留(小时)",
               "Raw waveform retention (hours)"),
           tr("超期的未钉住段只保留包络与特征",
               "Unpinned segments past this keep only the envelope and features")),
    "cm_keep_gb": (tr("原始波形上限(GB)",
               "Raw waveform cap (GB)"),
           tr("超出后从最旧的未钉住段开始清理",
               "Beyond this, cleanup starts from the oldest unpinned segment")),
    "cm_pin_preroll_s": (tr("事件前保留(秒)",
               "Pre-event retention (s)"),
           tr("修针/进针/告警发生前多久的段永久保留",
               "How long before a tip-conditioning / approach / alert event segments are kept permanently")),
    "cm_pin_postroll_s": (tr("事件后保留(秒)",
               "Post-event retention (s)"),
           tr("事件之后多久的段永久保留",
               "How long after an event segments are kept permanently")),
    "cm_evidence_min_interval_s": (tr("证据图最小间隔(秒)",
               "Minimum evidence-plot interval (s)"),
           tr("限制告警配图的渲染频率",
               "Limits how often alert figures are rendered")),
    # ── 辅助通道 ───────────────────────────────────────────────────────
    "cm_aux_enabled": (tr("记录辅助通道",
               "Record auxiliary channels"),
           tr("1=同时记录 Z 位置/qPlus 振幅/频率偏移(不占示波器)",
               "1 = also record Z position / qPlus amplitude / frequency shift (does not use the scope)")),
    "cm_aux_alerts_enabled": (tr("辅助通道告警",
               "Auxiliary channel alerts"),
           tr("出厂 0:阈值尚未在本机标定,先跑标定报告再打开",
               "Factory 0: thresholds are not calibrated on this machine yet, run the commissioning report first and then turn on")),
    "cm_aux_interval_s": (tr("辅助采样间隔(秒)",
               "Auxiliary sampling interval (s)"),
           tr("多久取一次 Z/振幅;出厂 0.2 s(5 Hz)。每次一个 TCP 往返,搭在电流泵等缓冲的空隙里,不抢锁",
               "How often Z / amplitude are read; factory 0.2 s (5 Hz). One TCP round trip each, slotted into gaps while the current pump waits for its buffer, without contending for the lock")),
    "cm_aux_window_s": (tr("辅助统计窗口(秒)",
               "Auxiliary statistics window (s)"),
           tr("漂移率/台阶/振幅统计量在多长的窗口里算",
               "Window length over which drift rate / step / amplitude statistics are computed")),
    "cm_aux_keep_hours": (tr("辅助读数保留(小时)",
               "Auxiliary reading retention (hours)"),
           tr("超期的 aux 行滚删(它们很小,可以留很久)",
               "Aux rows past this are rolled off (they are small and can be kept long)")),
    "cm_z_drift_warn_m_per_s": (tr("Z 漂移率阈(m/s)",
               "Z drift rate threshold (m/s)"),
           tr("窗口内 Z 的线性漂移速率超过此值提示",
               "Warn when the linear Z drift rate in the window exceeds this")),
    "cm_z_step_warn_m": (tr("Z 台阶阈(m)",
               "Z step threshold (m)"),
           tr("相邻样本间的稳健突跳超过此值提示",
               "Warn when the robust jump between adjacent samples exceeds this")),
    "cm_z_headroom_warn_frac": (tr("Z 量程余量阈",
               "Z range headroom threshold"),
           tr("Z 距离行程边缘的相对余量低于此值提示",
               "Warn when the relative headroom of Z to the travel edge falls below this")),
    "cm_z_jump_k": (tr("Z 台阶判据 k",
               "Z step criterion k"),
           tr("阈值 = 差分中位数 + k×稳健σ;越大越保守",
               "Threshold = difference median + k x robust sigma; larger is more conservative")),
    "cm_amp_zero_frac": (tr("振幅归零判据",
               "Amplitude-zero criterion"),
           tr("低于「未接触本底」的这个比例算归零;**只在进针期间判**",
               "Below this fraction of the \"non-contact baseline\" counts as zero; **judged only during approach**")),
    "cm_amp_blank_s": (tr("扰动后振幅屏蔽(秒)",
               "Post-disturbance amplitude blanking (s)"),
           tr("扎针/电脉冲会让振幅大起振;恢复时间须按目标仪器验证",
               "Tip pokes / electrical pulses make the amplitude ring up; verify the recovery time on the target instrument")),
}

#: Knobs that are really booleans (0/1) — the UI renders these as switches.
BOOL_KEYS: frozenset[str] = frozenset({
    "cm_enabled", "cm_alerts_enabled", "cm_aux_enabled", "cm_aux_alerts_enabled",
    "cm_use_osci2t",
})


@dataclass(frozen=True)
class MonitorThresholds:
    """Immutable snapshot of the monitor's knobs.

    ``cm_sat_current_a`` defaults to 90 nA, just under the usual 100 nA preamp
    range — leave it at the actual full-scale of the installed preamp, since the
    saturation rule is one of the three that can halt a scan.

    ``cm_keep_hours`` / ``cm_keep_gb`` are a first-hit-wins pair. Storage use
    scales with sample rate and sample width; either limit may expire first.
    """

    cm_enabled: float = 1.0
    cm_alerts_enabled: float = 0.0
    cm_segment_s: float = 1.0
    cm_target_fs_hz: float = 0.0
    # 优先使用 2T 以减少缓冲读取的往返次数；模块不可用时回退到 1T。
    # 采样率来自仪器回包，不能由缓冲大小推断。较长窗口也会增加告警延迟，
    # 多段确认规则可能在一次回包后同时收到各段，应一起考虑响应时间。
    cm_use_osci2t: float = 1.0
    cm_2t_window_s: float = 6.4
    cm_rms_warn_a: float = 20e-12
    #: 有活跃基线时 rms_high 的判据。3.0 是出厂值，不是标定值；
    #: 应通过目标仪器积累的健康段比值分布复核。
    cm_rms_ratio_warn: float = 3.0
    cm_spike_sigma_warn: float = 8.0
    cm_spike_sigma_crit: float = 30.0
    cm_sat_current_a: float = 90e-9
    cm_sat_frac_crit: float = 0.2
    cm_crit_consecutive: float = 3.0
    cm_afterglow_s: float = 5.0
    cm_alert_cooldown_s: float = 120.0
    cm_rtn_score_warn: float = 0.7
    cm_line_ratio_warn: float = 10.0
    cm_jump_rate_warn_hz: float = 5.0
    cm_keep_hours: float = 24.0
    cm_keep_gb: float = 4.0
    cm_pin_preroll_s: float = 30.0
    cm_pin_postroll_s: float = 30.0
    cm_evidence_min_interval_s: float = 60.0

    # 辅助通道默认记录但不告警。先在目标仪器上采集本底并运行 commission，验证阈值后再启用。Z 漂移、台阶和振幅屏蔽期都是待验证的配置，不能当作现场标定；稀疏采样无法恢复完整 ring-down，屏蔽期还会延迟持续异常的报告。
    cm_aux_enabled: float = 1.0
    cm_aux_alerts_enabled: float = 0.0
    # 5 Hz（原为 1 Hz）。
    # 保守一档，没有一步到 10 Hz：τ 是几百毫秒，5 Hz 已经把振幅过采到 2-3×。
    cm_aux_interval_s: float = 0.2
    cm_aux_window_s: float = 300.0
    cm_aux_keep_hours: float = 168.0
    cm_z_drift_warn_m_per_s: float = 50e-12
    cm_z_step_warn_m: float = 500e-12
    cm_z_headroom_warn_frac: float = 0.05
    cm_z_jump_k: float = 8.0
    cm_amp_zero_frac: float = 0.30
    cm_amp_blank_s: float = 30.0

    # ── convenience accessors (the daemon reads these, not the raw floats) ──
    @property
    def enabled(self) -> bool:
        return self.cm_enabled >= 0.5

    @property
    def alerts_enabled(self) -> bool:
        return self.cm_alerts_enabled >= 0.5

    @property
    def aux_enabled(self) -> bool:
        return self.cm_aux_enabled >= 0.5

    @property
    def aux_alerts_enabled(self) -> bool:
        """辅助通道的告警开关。

        与 ``alerts_enabled`` **相互独立**：电流告警开着不代表 Z/振幅的未标定阈值
        也该往外发。反过来也一样。
        """
        return self.cm_aux_alerts_enabled >= 0.5

    @property
    def acquisition_strategy(self) -> str:
        """要哪个示波器方言。**只是期望值** —— 模块没加载时 ``make_pump()``
        会退回 ``osci1t``,以泵实例上的 ``STRATEGY`` 为准。"""
        return "osci2t" if self.cm_use_osci2t >= 0.5 else "osci1t"

    @property
    def crit_consecutive(self) -> int:
        return max(1, int(round(self.cm_crit_consecutive)))

    def to_mapping(self) -> dict[str, float]:
        return {k: float(v) for k, v in asdict(self).items()}

    @classmethod
    def from_mapping(cls, m: dict | None) -> "MonitorThresholds":
        """Build from a (partial) mapping — tolerant of persisted settings.
        Unknown keys ignored, missing keys fall back to default, recognised
        numeric values clamped to :data:`FIELD_BOUNDS`. Booleans are accepted
        for the 0/1 knobs so a JSON ``true`` from the UI does the right thing.
        """
        if not m:
            return cls()
        known = {f.name for f in fields(cls)}
        clean: dict[str, float] = {}
        for k, v in m.items():
            if k not in known:
                continue
            if isinstance(v, bool):
                val = 1.0 if v else 0.0
            elif isinstance(v, (int, float)):
                val = float(v)
            else:
                continue
            lo, hi = FIELD_BOUNDS.get(k, (float("-inf"), float("inf")))
            clean[k] = float(min(hi, max(lo, val)))
        return replace(cls(), **clean)


_LOCK = threading.Lock()
_ACTIVE = MonitorThresholds()


def get_monitor_thresholds() -> MonitorThresholds:
    """Return the active immutable snapshot (lock-free atomic reference read)."""
    return _ACTIVE


def set_monitor_thresholds(m: "dict | MonitorThresholds | None") -> MonitorThresholds:
    """Swap the active snapshot. ``None`` / empty resets to defaults."""
    global _ACTIVE
    new = m if isinstance(m, MonitorThresholds) else MonitorThresholds.from_mapping(m)
    with _LOCK:
        _ACTIVE = new
    return new


def knob_catalog() -> list[dict]:
    """Describe every knob for the settings UI: bounds, default, current value.

    Shipped as data so the frontend does not hard-code a key list — adding a
    knob to :data:`EDITABLE_KEYS` is enough to make it appear.
    """
    active = get_monitor_thresholds().to_mapping()
    defaults = MonitorThresholds().to_mapping()
    out: list[dict] = []
    for key in EDITABLE_KEYS:
        lo, hi = FIELD_BOUNDS.get(key, (0.0, 0.0))
        label, hint = KNOB_LABELS.get(key, (key, ""))
        out.append({
            "key": key,
            "label_zh": label,
            "hint_zh": hint,
            "min": float(lo),
            "max": float(hi),
            "step": 0.0,
            "default": float(defaults.get(key, 0.0)),
            "value": float(active.get(key, 0.0)),
            "is_bool": key in BOOL_KEYS,
            # 分组只是给 UI 分栏用的。两组阈值的量纲不同（安培 vs 米/赫兹），
            # 混在一张长表里读的人很容易以为它们是一套。
            "group": "aux" if key in AUX_KEYS else "current",
        })
    return out


__all__ = [
    "MonitorThresholds",
    "get_monitor_thresholds",
    "set_monitor_thresholds",
    "knob_catalog",
    "EDITABLE_KEYS",
    "AUX_KEYS",
    "FIELD_BOUNDS",
    "BOOL_KEYS",
]
