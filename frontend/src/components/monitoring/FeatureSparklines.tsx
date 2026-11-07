import type { components } from "@/api/schema";
import { Sparkline } from "@/components/vision/Sparkline";
import { EmptyNote } from "@/components/ui";
import { fmtCurrent } from "@/lib/units";
import { tr } from "@/lib/i18n";
import { METRIC_TILES, metricSeries, type MetricUnit } from "@/lib/monitoring";

type FeatureRow = components["schemas"]["FeatureRow"];

// Feature tiles — six numbers with their recent history.
//
// The series comes from /api/monitoring/features, NOT from status.latest: a
// sparkline needs history and `latest` is one point. It also means a WS push
// (which carries five of the ~35 columns) can never blank a tile — the tiles
// simply follow the REST series at its own, slower cadence.

function fmtMetric(v: number, unit: MetricUnit): string {
  switch (unit) {
    case "A":
      return fmtCurrent(v, { placeholder: "—" });
    case "hz":
      return `${v.toPrecision(3)} Hz`;
    case "ratio":
      return v.toFixed(3);
    default:
      return Math.abs(v) >= 1e4 || (v !== 0 && Math.abs(v) < 1e-3)
        ? v.toExponential(2)
        : v.toPrecision(4);
  }
}

export function FeatureSparklines({
  rows,
  degraded,
  detail,
}: {
  rows: FeatureRow[];
  degraded?: boolean;
  detail?: string | null;
}) {
  // "Not collected yet" and "cannot read" are different states (waiting vs a fault)
  // although `rows` is empty in both. Without the distinction a failed
  // `features_query` would show "available after one segment" and the user would
  // wait for data that never arrives.
  if (degraded) {
    return (
      <EmptyNote
        label={
          detail
            ? tr(`读不到特征数据：${detail}`, `Cannot read feature data: ${detail}`)
            : tr("读不到特征数据（不是「还没采到」）。", "Cannot read feature data (this is not \"not collected yet\").")
        }
      />
    );
  }
  if (!rows.length) {
    return (
      <EmptyNote
        label={tr(
          "尚无特征数据——采集满一段后即可看到。",
          "No feature data yet; it appears once one segment has been collected.",
        )}
      />
    );
  }
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
      {METRIC_TILES.map((tile) => {
        const series = metricSeries(rows, tile.key);
        const last = series.length ? series[series.length - 1]! : null;
        return (
          <div
            key={tile.key}
            className="rounded-mast-card border border-mast-border bg-mast-panel p-3"
            title={tile.hint}
          >
            <div className="text-xs text-mast-muted">{tile.label}</div>
            <div className="mt-1 flex items-end justify-between gap-2">
              <span className="font-mono text-lg text-mast-text">
                {last == null ? "—" : fmtMetric(last, tile.unit)}
              </span>
              <Sparkline values={series} />
            </div>
            <div className="mt-1 text-[11px] leading-snug text-mast-faint">{tile.hint}</div>
          </div>
        );
      })}
    </div>
  );
}
