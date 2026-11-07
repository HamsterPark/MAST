import { useMemo, useState } from "react";
import UplotReact from "uplot-react";
import type uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { useUiStore } from "@/store";
import { tr } from "@/lib/i18n";
import { EmptyNote } from "@/components/ui";
import { useElementWidth, useViewportHeight } from "@/hooks/useElementWidth";
import { TREND_RATIO, sizeChart } from "@/lib/chartSize";
import {
  axisLabel,
  fmtSpan,
  fmtTs,
  isDutyCycleSeries,
  preferLogScale,
  toAlignedData,
  type EnvPointLike,
} from "@/lib/envHistory";

// Environment trend — one sensor's aggregated history.
//
// A mean line inside a min/max band, because that is exactly what a bucket is.
// Drawing the mean alone would hide the excursion the bucket was built to keep:
// a 1-minute bucket over a 30-day window is one pixel wide, and the whole point
// of storing min/max is that the pixel still shows the spike.
//
// The band is NOT an error bar. min/max are true extremes of the raw readings
// in that bucket, so the band never lies about how far the value went.

// uPlot paints to <canvas>, which cannot resolve CSS vars — read the token's
// computed value off <html> and feed concrete colors, keyed on the active theme.
//
// The names must carry the `--mast-` prefix (index.css). Unprefixed names such
// as `--accent` or `--border` are not defined anywhere in the app, so the lookup
// would return "" and only the fallback literals below would be painted on both
// themes, making the `theme` dependency a no-op.
function token(name: string): string {
  if (typeof window === "undefined") return "";
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/**
 * Optional second series drawn over the first.
 *
 * Same axis, so the units MUST match — the caller checks. This is deliberate,
 * and it is the same rule AuxChannels states for its small multiples: two
 * curves on two Y axes sit at whatever relative height the person drawing them
 * chose, which invites the reader to see a correlation nobody measured. Two
 * temperatures on one Kelvin axis are genuinely comparable; a temperature and a
 * pressure are not.
 */
export interface CompareSeries {
  sensor: string;
  points: readonly EnvPointLike[];
}

export function EnvSeriesChart({
  sensor,
  unit,
  points,
  bucketS,
  thinned,
  compare,
}: {
  sensor: string;
  unit: string;
  points: readonly EnvPointLike[];
  bucketS: number;
  thinned: boolean;
  compare?: CompareSeries | null;
}) {
  const theme = useUiStore((s) => s.theme);
  const [boxRef, measuredW] = useElementWidth<HTMLDivElement>();
  const viewportH = useViewportHeight();
  const { width, height } = sizeChart(measuredW, 860, {
    ratio: TREND_RATIO,
    minHeight: 240,
    maxHeight: 520,
    viewportH,
  });
  const [hover, setHover] = useState<{ t: number; mean: number | null; lo: number | null; hi: number | null } | null>(
    null,
  );
  const duty = isDutyCycleSeries(sensor);

  const data = useMemo<uPlot.AlignedData>(() => {
    const [t, mean, lo, hi] = toAlignedData(points, bucketS);
    const scale = duty ? 100 : 1;
    const cols: (number | null)[][] = [
      hi.map((v) => (v == null ? null : v * scale)),
      lo.map((v) => (v == null ? null : v * scale)),
      mean.map((v) => (v == null ? null : v * scale)),
    ];
    if (compare) {
      // Resampled onto the PRIMARY's timestamps by exact bucket match, not by
      // index: the two series can have different numbers of buckets (one channel
      // may have gaps the other does not), and lining them up by position would
      // slide one curve along the time axis and invent an offset that is not there.
      const byTs = new Map<number, number | null>();
      for (const p of compare.points) {
        if (p && Number.isFinite(p.ts)) {
          byTs.set(p.ts, typeof p.mean === "number" && Number.isFinite(p.mean) ? p.mean : null);
        }
      }
      cols.push(t.map((ts) => {
        const v = byTs.get(ts);
        return v == null ? null : v * scale;
      }));
    }
    return [t, ...cols] as uPlot.AlignedData;
  }, [points, bucketS, duty, compare]);

  const logScale = useMemo(() => !duty && preferLogScale(unit, points), [duty, unit, points]);

  const options = useMemo<uPlot.Options>(() => {
    const muted = token("--mast-muted") || "#94a3b8";
    const grid = token("--mast-border") || "#2c3440";
    const accent = token("--mast-accent") || "#28d0e6";
    const yLabel = duty ? tr("仪器空闲占比 (%)", "Instrument idle fraction (%)") : axisLabel(sensor, unit);
    return {
      width,
      height,
      // Absolute unix seconds on x — uPlot's time scale reads seconds natively.
      scales: {
        x: { time: true },
        y: logScale ? { distr: 3 } : {},
      },
      legend: { show: false },
      cursor: {
        drag: { x: true, y: false },
        points: { show: false },
      },
      axes: [
        { stroke: muted, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid } },
        {
          stroke: muted,
          label: yLabel,
          labelSize: 40,
          grid: { stroke: grid, width: 1 },
          ticks: { stroke: grid },
        },
      ],
      series: [
        {},
        // [high, low] — band edges, drawn invisibly. The shading between them
        // comes from `bands` below.
        { stroke: "transparent", points: { show: false }, label: "max" },
        { stroke: "transparent", points: { show: false }, label: "min" },
        { stroke: accent, width: 1.5, points: { show: false }, label: "mean" },
        // Dashed and in a different colour, with no band of its own: the band
        // belongs to the series it was computed for, and a second one would
        // just be two overlapping washes.
        ...(compare
          ? [{
              stroke: token("--mast-dream") || "#a78bfa",
              width: 1.3,
              dash: [5, 3],
              points: { show: false },
              label: compare.sensor,
            } as uPlot.Series]
          : []),
      ],
      // Use `bands` to fill between the two series only. `series.fill` fills to
      // the baseline, not to the next series, so it would shade the whole region
      // under the curve.
      bands: [{ series: [1, 2], fill: withAlpha(accent, 0.18) }],
      hooks: {
        setCursor: [
          (u: uPlot) => {
            const i = u.cursor.idx;
            if (i == null) {
              setHover(null);
              return;
            }
            const t = (u.data[0]?.[i] ?? null) as number | null;
            const hi = (u.data[1]?.[i] ?? null) as number | null;
            const lo = (u.data[2]?.[i] ?? null) as number | null;
            const mean = (u.data[3]?.[i] ?? null) as number | null;
            setHover(t == null ? null : { t, mean, lo, hi });
          },
        ],
      },
      plugins: [],
    } as uPlot.Options;
    // `theme` is in the deps because the tokens above only change with it.
    // `compare?.sensor` rather than `compare`: the object identity changes on
    // every refetch, and rebuilding the uPlot options on each poll throws away
    // the cursor and any zoom the operator had dragged.
  }, [width, height, sensor, unit, duty, logScale, theme, compare?.sensor]);

  if (!points.length) {
    // Still hand the measuring div back, so the width is already known when
    // points do arrive and the first painted frame is the right size.
    return (
      <div ref={boxRef}>
        <EmptyNote label={tr("这段时间没有记录。", "No records in this period.")} />
      </div>
    );
  }

  const first = points[0];
  const last = points[points.length - 1];
  const span = first && last && points.length > 1 ? last.ts - first.ts : bucketS;
  const suffix = duty ? "%" : unit ? ` ${unit}` : "";

  return (
    <div ref={boxRef}>
      <UplotReact options={options} data={data} />
      <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-mast-muted">
        <span>
          {tr(
            `${points.length} 点 · 每点 ${fmtSpan(bucketS)} · 跨度 ${fmtSpan(span)}`,
            `${points.length} points · ${fmtSpan(bucketS)} each · span ${fmtSpan(span)}`,
          )}
        </span>
        {thinned && (
          <span
            title={tr(
              "点数超过上限，服务端按加权合并成更粗的桶。极值与最差状态都被保留，不是抽样。",
              "The point count exceeded the limit, so the server merged buckets with weighting. Extremes and worst states are preserved; this is not sampling.",
            )}
          >
            {tr("概览视图（已合并）", "Overview (merged)")}
          </span>
        )}
        {hover && (
          <span className="ml-auto tabular-nums">
            {fmtTs(hover.t)} · {tr("均", "mean")} {fmtNum(hover.mean)}
            {suffix} · {tr("区间", "range")} {fmtNum(hover.lo)}–{fmtNum(hover.hi)}
            {suffix}
          </span>
        )}
      </div>
    </div>
  );
}

function fmtNum(v: number | null | undefined): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-3 || a >= 1e5)) return v.toExponential(3);
  return String(Math.round(v * 1e4) / 1e4);
}

/** `#rrggbb` → `rgba(...)`. Falls back to the input when the token is already
 *  a function form, which is fine for a fill. */
function withAlpha(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(color.trim());
  if (!m) return color;
  const n = parseInt(m[1] ?? "0", 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}
