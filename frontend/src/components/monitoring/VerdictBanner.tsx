import clsx from "clsx";
import type { components } from "@/api/schema";
import { fmtCurrent } from "@/lib/units";
import { fmtSeconds, verdictView } from "@/lib/monitoring";
import { langTag, tr } from "@/lib/i18n";

type MonitoringStatus = components["schemas"]["MonitoringStatus"];

// Current verdict banner — the one thing on the page an operator reads without stopping.
//
// Its hardest requirement is negative: when the newest segment has aged past the
// stale threshold this must NOT keep showing the last verdict. A frozen "Normal"
// is indistinguishable from a live "Normal", and the whole point of the monitor is
// to be believed. Stale → neutral grey "No data" with the age spelled out.

const TONE_BOX: Record<string, string> = {
  AUTO: "border-mast-auto-border bg-mast-auto-bg text-mast-auto",
  INFO: "border-mast-info-border bg-mast-info-bg text-mast-info",
  WARN: "border-mast-warn-border bg-mast-warn-bg text-mast-warn",
  DANGEROUS: "border-mast-danger-border bg-mast-danger-bg text-mast-danger",
  default: "border-mast-border bg-mast-panel-2 text-mast-muted",
};

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide opacity-70">{label}</div>
      <div className="font-mono text-sm">{value}</div>
    </div>
  );
}

export function VerdictBanner({
  status,
  stale,
  ageS,
}: {
  status: MonitoringStatus | undefined;
  /** Newest segment older than MONITORING_STALE_AFTER_MS. */
  stale: boolean;
  /** Seconds since the newest segment; null when there has never been one. */
  ageS: number | null;
}) {
  const latest = status?.latest ?? null;
  const noData = !latest || stale;

  if (noData) {
    const why = !latest
      ? tr("还没有采集到任何一段电流", "No current segment has been collected yet")
      : tr(`最近一段已是 ${fmtSeconds(ageS)} 前的数据`, `The latest segment is ${fmtSeconds(ageS)} old`);
    return (
      <div className={clsx("rounded-mast-card border px-4 py-3", TONE_BOX.default)}>
        <div className="flex items-baseline gap-3">
          <span className="text-lg font-semibold">{tr("无数据", "No data")}</span>
          <span className="text-sm">{why}</span>
        </div>
        <p className="mt-1 text-xs opacity-80">
          {tr(
            "横幅不显示已冻结的旧判定——上一次的读数即使还在库里，也不能代表此刻的针尖状态。",
            "The banner does not show a frozen old verdict: even if the last reading is still in the store, it does not represent the tip state right now.",
          )}
        </p>
      </div>
    );
  }

  const v = verdictView(latest.verdict);
  const suppressed = latest.verdict === "suppressed";

  return (
    <div className={clsx("rounded-mast-card border px-4 py-3", TONE_BOX[v.tone] ?? TONE_BOX.default)}>
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="text-lg font-semibold">{v.label}</span>
        <span className="text-sm">
          {tr(`第 ${latest.seg_id} 段`, `Segment ${latest.seg_id}`)} · {new Date(latest.ts * 1000).toLocaleTimeString(langTag(), { hour12: false })}
        </span>
        {suppressed && (
          <span className="text-xs opacity-90">
            {tr(
              "该段落在修针 / 电脉冲窗口内，噪声是我们自己造成的，故不报警",
              "This segment falls inside a tip-conditioning / voltage-pulse window; the noise is self-inflicted, so no alert is raised",
            )}
          </span>
        )}
      </div>
      <div className="mt-2.5 grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
        <Stat label={tr("RMS 噪声", "RMS noise")} value={fmtCurrent(latest.rms_detrended_a, { placeholder: "—" })} />
        <Stat label={tr("平均电流", "Mean current")} value={fmtCurrent(latest.mean_a, { placeholder: "—" })} />
        <Stat label={tr("最小", "Min")} value={fmtCurrent(latest.min_a, { placeholder: "—" })} />
        <Stat label={tr("最大", "Max")} value={fmtCurrent(latest.max_a, { placeholder: "—" })} />
      </div>
    </div>
  );
}
