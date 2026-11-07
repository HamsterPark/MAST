import { useMemo, useState } from "react";
import UplotReact from "uplot-react";
import type uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { useUiStore } from "@/store";
import { useElementWidth, useViewportHeight } from "@/hooks/useElementWidth";
import { TREND_RATIO, sizeChart } from "@/lib/chartSize";
import { tr } from "@/lib/i18n";

// uPlot paints to <canvas>, which cannot resolve CSS vars, so we read the
// design token's computed value off <html> and feed concrete colors. Keyed on
// the active theme so the axes/grid/series recolor on light/dark toggle.
function token(name: string): string {
  if (typeof window === "undefined") return "";
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Time-domain trace plot (time x value) via uPlot, the companion to FftChart in
// the signal-capture panel. Huge traces are decimated; current channels (unit "A")
// are auto-scaled to pA. Hover gives a cursor readout. Empty data renders a note,
// not a blank canvas.

// Above this point count we min-max decimate before plotting so the chart stays
// smooth on long traces.
const MAX_PLOT_POINTS = 50_000;

/** Min-max decimation: keep the visual envelope of a huge trace (spikes survive
 *  while the point count drops). */
function decimate(ts: number[], ys: number[], maxPoints: number): [number[], number[]] {
  const n = ys.length;
  if (n <= maxPoints || n === 0) return [ts, ys];
  const buckets = Math.max(1, Math.floor(maxPoints / 2));
  const step = n / buckets;
  const outT: number[] = [];
  const outY: number[] = [];
  for (let b = 0; b < buckets; b++) {
    const lo = Math.floor(b * step);
    const hi = Math.min(n, Math.floor((b + 1) * step));
    if (hi <= lo) continue;
    let iMin = lo;
    let iMax = lo;
    for (let i = lo + 1; i < hi; i++) {
      if (ys[i]! < ys[iMin]!) iMin = i;
      if (ys[i]! > ys[iMax]!) iMax = i;
    }
    for (const i of [iMin, iMax].sort((a, c) => a - c)) {
      outT.push(ts[i]!);
      outY.push(ys[i]!);
    }
  }
  return [outT, outY];
}

function fmtTime(s: number): string {
  if (!Number.isFinite(s)) return "—";
  if (Math.abs(s) >= 1) return `${s.toFixed(4)} s`;
  return `${(s * 1e3).toFixed(3)} ms`;
}

function fmtVal(v: number, unit: string): string {
  if (!Number.isFinite(v)) return "—";
  const txt = v !== 0 && (Math.abs(v) < 1e-3 || Math.abs(v) >= 1e5) ? v.toExponential(4) : v.toPrecision(5);
  return `${txt} ${unit}`;
}

export function TimeTraceChart({
  timestampsS,
  samples,
  unit = "A",
  channelName = "signal",
}: {
  timestampsS: number[];
  samples: number[];
  unit?: string;
  channelName?: string;
}) {
  const [hover, setHover] = useState<{ t: number; v: number } | null>(null);
  const theme = useUiStore((s) => s.theme);
  const [boxRef, measuredW] = useElementWidth<HTMLDivElement>();
  const viewportH = useViewportHeight();
  const { width, height } = sizeChart(measuredW, 720, {
    ratio: TREND_RATIO,
    minHeight: 220,
    maxHeight: 460,
    viewportH,
  });

  // Current channels read in tiny amperes, so plot in pA when unit === "A";
  // otherwise plot the raw value in its unit.
  const isCurrent = unit === "A";
  const displayUnit = isCurrent ? "pA" : unit;

  const { data, n, decimated } = useMemo(() => {
    const ys0 = samples ?? [];
    const ts0 =
      timestampsS && timestampsS.length === ys0.length
        ? timestampsS
        : ys0.map((_, i) => i);
    const ysScaled = isCurrent ? ys0.map((y) => y * 1e12) : ys0;
    const [tD, yD] = decimate(ts0, ysScaled, MAX_PLOT_POINTS);
    return {
      data: [tD, yD] as uPlot.AlignedData,
      n: ys0.length,
      decimated: ys0.length > MAX_PLOT_POINTS,
    };
  }, [timestampsS, samples, isCurrent]);

  const options = useMemo<uPlot.Options>(
    () => {
      // resolved design tokens (canvas can't read CSS vars) — recomputed on theme
      const axisStroke = token("--mast-muted") || "#94a3b8";
      const gridStroke = token("--mast-border") || "#1e293b";
      const tickStroke = token("--mast-border-strong") || "#334155";
      const seriesStroke = token("--mast-auto") || "#34d399";
      return {
      width,
      height,
      scales: { x: { time: false }, y: {} },
      axes: [
        {
          stroke: axisStroke,
          grid: { stroke: gridStroke },
          ticks: { stroke: tickStroke },
          label: tr("时间 (s)", "Time (s)"),
          labelGap: 6,
        },
        {
          stroke: axisStroke,
          grid: { stroke: gridStroke },
          ticks: { stroke: tickStroke },
          label: tr(`幅值 (${displayUnit})`, `Amplitude (${displayUnit})`),
          labelGap: 6,
        },
      ],
      series: [
        { label: "s" },
        // Stroke only. A `fill` on a uPlot series floods everything between the
        // line and the baseline, which for a signal that swings either side of
        // zero shades an arbitrary half of the trace and implies meaning.
        { label: channelName || "signal", stroke: seriesStroke, width: 1 },
      ],
      legend: { show: true },
      cursor: { drag: { x: true, y: false } },
      hooks: {
        setCursor: [
          (u: uPlot) => {
            const idx = u.cursor.idx;
            if (idx == null) {
              setHover(null);
              return;
            }
            const t = u.data[0]?.[idx];
            const v = u.data[1]?.[idx];
            if (typeof t === "number" && typeof v === "number") setHover({ t, v });
            else setHover(null);
          },
        ],
      },
      };
    },
    [width, height, displayUnit, channelName, theme],
  );

  if (!data[0]?.length) {
    return <p className="text-sm text-mast-muted">{tr("暂无时域 trace。", "No time-domain trace.")}</p>;
  }

  return (
    <div className="space-y-2">
      <div ref={boxRef} className="overflow-x-auto rounded-lg border border-mast-border bg-mast-bg p-2">
        <UplotReact options={options} data={data} />
      </div>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-mast-muted">
        <span>
          {tr("游标：", "Cursor: ")}
          {hover ? (
            <span className="ml-1 font-mono text-mast-text">
              {fmtTime(hover.t)} → {fmtVal(isCurrent ? hover.v / 1e12 : hover.v, unit)}
            </span>
          ) : (
            <span className="ml-1">{tr("移动鼠标到曲线上读取", "Hover over the curve to read values")}</span>
          )}
        </span>
        <span>{n} {tr("点", "points")}</span>
        {decimated && <span className="text-mast-warn">{tr("已抽稀显示（导出仍为全量）", "Decimated for display (export remains full resolution)")}</span>}
      </div>
    </div>
  );
}
