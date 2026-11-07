import type { components } from "@/api/schema";
import { tr } from "@/lib/i18n";

type Pulse = components["schemas"]["VisionPulseResponse"];

// Concise vision-activity ribbon (dino_score + alert + trend + counters),
// mirrors gui.vision_pulse. Pure presentational; the page owns the polling.

const ALERT_TONE: Record<Pulse["alert_level"], string> = {
  idle: "bg-mast-panel text-mast-muted border-mast-border",
  info: "bg-mast-info-bg text-mast-info border-mast-info-border",
  warn: "bg-mast-warn-bg text-mast-warn border-mast-warn-border",
  critical: "bg-mast-danger-bg text-mast-danger border-mast-danger-border",
};

const ALERT_LABEL: Record<Pulse["alert_level"], string> = {
  idle: tr("空闲", "Idle"),
  info: tr("正常", "Normal"),
  warn: tr("注意", "Warning"),
  critical: tr("严重", "Critical"),
};

const TREND_LABEL: Record<Pulse["trend"], string> = {
  idle: tr("— 静止", "— Idle"),
  rising: tr("▲ 上升", "▲ Rising"),
  steady: tr("■ 平稳", "■ Steady"),
  falling: tr("▼ 下降", "▼ Falling"),
};

const QUALITY_LABEL: Record<string, string> = {
  good: tr("良好", "Good"),
  degraded: tr("退化", "Degraded"),
  bad: tr("差", "Bad"),
  unknown: tr("未知", "Unknown"),
};

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[11px] uppercase tracking-wide text-mast-muted">{label}</span>
      <span className="tabular-nums text-sm font-medium">{value}</span>
    </div>
  );
}

export function PulseRibbon({ pulse }: { pulse: Pulse }) {
  const score = pulse.dino_score;
  const scorePct = score == null ? null : Math.round(score * 100);

  return (
    <div className={`flex flex-wrap items-center gap-x-8 gap-y-3 rounded-lg border p-4 ${ALERT_TONE[pulse.alert_level]}`}>
      <div className="flex items-center gap-3">
        <span className="rounded px-2 py-1 text-xs font-semibold uppercase tracking-wide">
          {ALERT_LABEL[pulse.alert_level]}
        </span>
        <span className="text-sm">{TREND_LABEL[pulse.trend]}</span>
      </div>

      <Stat
        label={tr("针尖置信度", "Tip confidence")}
        value={scorePct == null ? "—" : `${scorePct}%`}
      />
      <Stat
        label={tr("针尖质量", "Tip quality")}
        value={pulse.tip_quality ? QUALITY_LABEL[pulse.tip_quality] ?? pulse.tip_quality : "—"}
      />
      {/* SAFE mode rewrites every tip verdict to "good" at the source, so the two
          stats above are manufactured. Never let an operator read them as measured. */}
      {pulse.safe_mode && (
        <span
          className="rounded border border-mast-warn-border bg-mast-warn-bg px-2 py-1 text-[11px] font-medium text-mast-warn"
          title={tr(
            "安全模式：针尖判定被统一覆写为「良好」，上面两项不是实测值。真实判定见视觉事件里的 safe_mode_raw。",
            "Safe mode: every tip verdict is overridden to \"good\", so the two values above are not measured. The raw verdict is in safe_mode_raw in the vision events.",
          )}
        >
          {tr("安全模式覆写中", "Safe-mode override")}
        </span>
      )}
      <Stat label={tr("近期事件", "Recent events")} value={String(pulse.recent_count)} />
      <Stat label={tr("严重事件", "Critical events")} value={String(pulse.critical_count)} />
      <Stat label={tr("丢弃(最旧)", "Dropped (oldest)")} value={String(pulse.dropped_oldest)} />
      <Stat label={tr("累计发布", "Total published")} value={String(pulse.events_published)} />
      <Stat label={tr("速率 (/s)", "Rate (/s)")} value={pulse.rate_per_s.toFixed(3)} />
    </div>
  );
}
