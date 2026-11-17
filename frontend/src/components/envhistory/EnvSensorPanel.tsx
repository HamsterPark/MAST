import { Badge, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { EnvSeriesChart } from "@/components/envhistory/EnvSeriesChart";
import {
  coverageOf,
  isDutyCycleSeries,
  seriesPanelState,
  statusLabel,
  statusTone,
  worstOf,
  type EnvPointLike,
} from "@/lib/envHistory";
import { tr } from "@/lib/i18n";

// One sensor's strip on the environment-history trends page.
//
// Everything a lone chart used to carry is kept per strip: the worst-status badge, the counted /
// excluded numbers (for two channels on one serial port they come out complementary — only one
// holds the handle at a time — and with the strips stacked on one time axis that is visible at a
// glance), and the "no valid readings in this period" statement.

export interface EnvSensorPanelProps {
  sensor: string;
  /** Unit from the sensor list; the series response's own unit wins once it has arrived. */
  unit: string;
  loading: boolean;
  error: unknown;
  points: readonly EnvPointLike[];
  unitFromSeries?: string;
  bucketS?: number;
  thinned?: boolean;
  syncKey: string;
}

export function EnvSensorPanel({
  sensor,
  unit,
  loading,
  error,
  points,
  unitFromSeries,
  bucketS,
  thinned,
  syncKey,
}: EnvSensorPanelProps) {
  const u = unitFromSeries ?? unit;
  const duty = isDutyCycleSeries(sensor);
  const worst = worstOf(points);
  const cover = coverageOf(points);
  const state = seriesPanelState(points);
  return (
    <div className="rounded-lg border border-mast-border bg-mast-bg p-2" data-env-sensor={sensor}>
      <div className="mb-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className="text-sm font-medium text-mast-text">{sensor}</span>
        <span className="text-mast-faint">{duty ? "%" : u || tr("（无单位）", "(no unit)")}</span>
        <div className="ml-auto flex items-center gap-2">
          {/* Tone follows severity: "unavailable" (the mildest non-ok) is not drawn as red as "alarm".
              Not shown when nothing in the window is drawable: the badge's wording ("no reading for part
              of the window") would understate it, and the note below says exactly what happened. */}
          {worst !== "ok" && state !== "undrawable" && <Badge tone={statusTone(worst)}>{statusLabel(worst)}</Badge>}
          {cover.buckets > 0 && (
            <span
              className="text-mast-faint"
              title={tr(
                "被排除的读数：传感器当时没给出有效值（多个通道共用一个串口时，" +
                  "同一时刻只有一个拿得到句柄，另一个就是空的），" +
                  "或者（隧道电流）仪器当时正在被驱动 —— 那种情况下这个信号是形貌不是环境。" +
                  "它们不进统计。",
                "Excluded readings: the sensor returned no valid value at the time " +
                  "(when several channels share one serial port only one can hold the handle " +
                  "at a time and the other reads empty), or (tunnelling current) the instrument " +
                  "was being driven, in which case the signal is topography, not environment. " +
                  "They are not counted in the statistics.",
              )}
            >
              {tr(
                `已计入 ${cover.counted} 条 · 未计入 ${cover.excluded} 条`,
                `${cover.counted} counted · ${cover.excluded} excluded`,
              )}
            </span>
          )}
        </div>
      </div>
      {loading && <Spinner />}
      {!loading && error != null && <ErrorNote error={error} />}
      {/* Buckets exist but nothing is drawable: "this sensor gave no valid reading in this
          period" is a fact to state, not an empty state. */}
      {!loading && error == null && state === "undrawable" && (
        <EmptyNote
          label={tr(
            `${sensor} 在这个时间范围内没有任何有效读数（${cover.buckets} 个统计桶，` +
              `${cover.excluded} 条读数全部被排除）。记录器一直在跑 —— 是这个传感器没读到值。`,
            `${sensor} has no valid readings in this time range (${cover.buckets} statistics buckets, ` +
              `all ${cover.excluded} readings excluded). The recorder is running; the sensor simply returned no values.`,
          )}
        />
      )}
      {!loading && error == null && state !== "undrawable" && (
        <EnvSeriesChart
          sensor={sensor}
          unit={u}
          points={points}
          bucketS={bucketS ?? 60}
          thinned={!!thinned}
          syncKey={syncKey}
        />
      )}
    </div>
  );
}
