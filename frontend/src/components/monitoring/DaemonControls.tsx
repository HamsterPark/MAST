import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import type { components } from "@/api/schema";
import { Badge } from "@/components/ui";
import { Button } from "@/components/controls";
import { tr } from "@/lib/i18n";
import { fmtBytes, fmtSeconds, stateView } from "@/lib/monitoring";

type MonitoringStatus = components["schemas"]["MonitoringStatus"];

// Acquisition daemon status and start/stop.
//
// The stop button is not a hazard: stopping only releases the oscilloscope
// subscription, and the store stays fully readable afterwards — browsing history
// with the daemon down is a supported state, not a degraded one.

function Chip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="inline-flex items-baseline gap-1.5 rounded-mast-badge border border-mast-border bg-mast-panel-2 px-2 py-1 text-xs">
      <span className="text-mast-muted">{label}</span>
      <span className="font-mono text-mast-text">{children}</span>
    </span>
  );
}

export function DaemonControls({
  status,
  onStart,
  onStop,
  starting,
  stopping,
  wsWarn,
}: {
  status: MonitoringStatus | undefined;
  onStart: () => void;
  onStop: () => void;
  starting: boolean;
  stopping: boolean;
  /** /ws/events is down — push unavailable, polling carrying the page. */
  wsWarn: boolean;
}) {
  const degraded = status?.degraded ?? false;
  const sv = stateView(status?.state);
  const running = status?.running ?? false;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={sv.tone}>{sv.label}</Badge>
        {status?.connected === false && (
          <Badge tone="DANGEROUS">{tr("未连接仪器", "Instrument not connected")}</Badge>
        )}
        {status?.enabled_in_settings === false && (
          <Badge tone="default">{tr("设置中已关闭", "Disabled in settings")}</Badge>
        )}
        {status?.alerts_enabled === false && <Badge tone="default">{tr("告警已关闭", "Alerts off")}</Badge>}

        <span className="ml-auto flex items-center gap-2">
          <Button
            variant="primary"
            onClick={onStart}
            loading={starting}
            disabled={degraded || running || starting || stopping}
          >
            {tr("启动采集", "Start acquisition")}
          </Button>
          <Button onClick={onStop} loading={stopping} disabled={degraded || !running || starting || stopping}>
            {tr("停止采集", "Stop acquisition")}
          </Button>
          <Link
            to="/settings/general"
            className="rounded-mast-ctl px-2 py-1 text-xs text-mast-accent hover:bg-mast-accent-soft"
          >
            {tr("阈值与保留策略设置 →", "Threshold & retention settings →")}
          </Link>
        </span>
      </div>

      <p className="text-xs text-mast-muted">
        {sv.hint}
        {status?.detail ? ` — ${status.detail}` : ""}
        {status?.retry_in_s ? tr(`（${fmtSeconds(status.retry_in_s)} 后重试）`, ` (retry in ${fmtSeconds(status.retry_in_s)})`) : ""}
      </p>

      {status?.alerts_enabled === false && (
        <p className="rounded-mast-card border border-mast-border bg-mast-panel-2 p-2 text-xs leading-relaxed text-mast-muted">
          {tr(
            "自动告警默认关闭：默认阈值尚未针对通用场景标定。当前的采集与记录照常进行；独立的硬件安全看门狗仍然有效。可在",
            "Automatic alerts are off by default: the default thresholds are not yet calibrated for general use. Acquisition and logging continue as usual, and the separate hardware safety watchdog stays active. You can change this in the ",
          )}
          <Link to="/settings/general" className="text-mast-accent hover:underline">
            {tr("设置", "settings")}
          </Link>
          {tr("中更改。", ".")}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <Chip label={tr("采集策略", "Strategy")}>{status?.strategy ?? "—"}</Chip>
        <Chip label={tr("采样率", "Sample rate")}>{status?.fs_hz ? `${status.fs_hz.toFixed(0)} Hz` : "—"}</Chip>
        <Chip label={tr("通道", "Channel")}>{status?.channel_name || "—"}</Chip>
        <Chip label={tr("段长", "Segment length")}>
          {status?.segment_seconds ? fmtSeconds(status.segment_seconds) : "—"}
        </Chip>
        <Chip label={tr("本次已采", "Collected this run")}>
          {status?.segments_done ?? 0} {tr("段", "segments")}
        </Chip>
        <Chip label={tr("累计缺口", "Total gaps")}>{fmtSeconds(status?.gaps_total_s ?? 0)}</Chip>
        <Chip label={tr("库内段数", "Segments in store")}>
          {status?.segments_total ?? 0}
          {status?.segments_on_disk != null &&
            tr(`（${status.segments_on_disk} 段留有原始数据）`, ` (${status.segments_on_disk} with raw data kept)`)}
        </Chip>
        <Chip label={tr("已钉住", "Pinned")}>
          {status?.pinned_count ?? 0} {tr("段", "segments")}
        </Chip>
        <Chip label={tr("占用", "Disk usage")}>
          {fmtBytes(status?.store_bytes)}
          {status?.retention_gb ? ` / ${status.retention_gb} GB` : ""}
        </Chip>
        <Chip label={tr("保留", "Retention")}>
          {status?.retention_hours ? tr(`${status.retention_hours} 小时`, `${status.retention_hours} h`) : "—"}
        </Chip>
      </div>

      {wsWarn && (
        <p className="text-xs text-mast-warn">
          {tr(
            "实时推送不可用，已回退为轮询——数据仍在更新，只是最多慢几秒。",
            "Live push is unavailable and the page fell back to polling; data is still updating, just a few seconds slower.",
          )}
        </p>
      )}
    </div>
  );
}
